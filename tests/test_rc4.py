"""rc4: day context, daily summary, early warnings, DB backup, CSV export, PWA MIME types."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridge import insights as ins
from bridge.api import Api, Sessions
from bridge.backup import Backup
from bridge.commands import CommandExecutor
from bridge.config import Config, ConfigError, validate_editable
from bridge.daily import DailyJobs
from bridge.history import DB_NAME, History
from bridge.logbuffer import RingBufferHandler
from bridge.state import Persistence

from .test_cache_publish import fill
from .test_history import Now, T, vals

UTC = timezone.utc


def ep(dt):
    return int(dt.timestamp())


def dbexec(cfg, sql, args=()):
    c = sqlite3.connect(cfg.data_dir / DB_NAME)
    try:
        r = c.execute(sql, args).fetchall()
        c.commit()
        return r
    finally:
        c.close()


def put_daily(cfg, day, y, **kw):
    cols = {"date": day, "yield_kwh": y, **kw}
    dbexec(cfg, f"INSERT OR REPLACE INTO daily ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
           tuple(cols.values()))


@pytest.fixture
async def hist(cfg):
    now = Now(T(2026, 7, 1, 12))
    h = History(cfg, "UTC", now=now)
    await h.start()
    yield h
    await h.close()


# ------------------------------------------------------------------ context
def rows_for(pairs):
    return [{"date": d, "yield_kwh": y} for d, y in pairs]


def test_context_ranks_year_and_avg():
    rows = rows_for([("2025-07-01", 40.0), ("2026-06-30", 30.0), ("2026-07-01", 50.0),
                     ("2026-07-02", 60.0), ("2025-07-02", 10.0), ("2024-07-03", 20.0),
                     ("2024-07-04", 25.0), ("2024-07-05", 35.0), ("2024-01-01", 99.0)])
    c = ins.context(rows, "2026-07-01")
    assert c["yield_kwh"] == 50.0 and c["yesterday_kwh"] == 30.0
    assert c["last_year_same_day_kwh"] == 40.0
    # July others: 40, 60, 10, 20, 25, 35 (6 days) -> mean 31.67
    assert c["avg_month_kwh"] == 31.67
    assert c["rank_all_time"] == 3          # 99 and 60 are better
    assert c["rank_month"] == 2             # only 60
    assert c["days_all_time"] == 9 and c["days_month"] == 7


def test_context_leap_day_null_and_few_days():
    c = ins.context(rows_for([("2023-02-28", 7.0), ("2024-02-29", 9.0)]), "2024-02-29")
    assert c["last_year_same_day_kwh"] == 7.0       # 29.02. -> 28.02. of the previous year
    assert c["avg_month_kwh"] is None               # < 5 other days in the month
    assert c["rank_all_time"] == 1
    d = ins.context(rows_for([("2026-03-01", 4.0)]), "2026-03-01")
    assert d["last_year_same_day_kwh"] is None and d["yesterday_kwh"] is None
    assert ins.context([], "2026-03-01")["rank_all_time"] is None       # no data: robust
    # rows without a yield (NULL) do not count
    e = ins.context([{"date": "2026-03-01", "yield_kwh": None}], "2026-03-01")
    assert e["yield_kwh"] is None and e["days_all_time"] == 0


def test_same_day_last_year():
    assert ins.same_day_last_year(date(2024, 2, 29)) == date(2023, 2, 28)
    assert ins.same_day_last_year(date(2026, 3, 1)) == date(2025, 3, 1)


async def test_history_context_includes_live_day(hist, cfg):
    put_daily(cfg, "2026-06-30", 20.0)
    hist.record(T(2026, 7, 1, 10), vals(daily_yield_kwh=33.0))
    c = await hist.context()
    assert c["date"] == "2026-07-01" and c["yield_kwh"] == 33.0 and c["yesterday_kwh"] == 20.0
    assert c["rank_all_time"] == 1
    from bridge.history import RequestError
    with pytest.raises(RequestError):
        await hist.context("nonsense")


# ------------------------------------------------------------------ summary
class Bus:
    def __init__(self):
        self.sent, self.ok = [], True

    def __call__(self, suffix, payload, retain):
        if not self.ok:
            return False
        self.sent.append((suffix, payload, retain))
        return True


class Probe:
    def __init__(self):
        self.v = ("online", 1000.0, False)

    def __call__(self):
        return self.v


@pytest.fixture
def jobs_env(cfg, hist):
    bus, probe = Bus(), Probe()
    persist = Persistence(cfg.data_dir)
    jobs = DailyJobs(cfg, persist, hist, bus, probe, now=hist._now)
    return jobs, bus, probe, hist


async def test_summary_after_30_min_idle_once(jobs_env, cfg):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 17), vals(daily_yield_kwh=12.5, peak_power_today_w=4000))
    await jobs.tick()
    summaries = lambda: [m for m in bus.sent if m[0] == "daily/summary"]   # noqa: E731
    assert summaries() == []
    probe.v = ("online", 0.0, False)
    h._now.t = T(2026, 7, 1, 19, 0)
    await jobs.tick()
    h._now.t = T(2026, 7, 1, 19, 29)
    await jobs.tick()
    assert summaries() == []
    h._now.t = T(2026, 7, 1, 19, 31)
    await jobs.tick()
    (suffix, p, retain), = [m for m in bus.sent if m[0] == "daily/summary"]
    assert retain is True and p["date"] == "2026-07-01" and p["yield_kwh"] == 12.5
    assert p["money"] == 1.0 and p["co2_kg"] == 4.8 and p["currency"] == "EUR"   # 12.5*.08 / *.38
    assert p["peak_power_w"] == 4000 and p["ts"].endswith("Z") and "rank_all_time" in p
    h._now.t = T(2026, 7, 1, 21)
    await jobs.tick()
    assert len([m for m in bus.sent if m[0] == "daily/summary"]) == 1


async def test_summary_resumed_production_resets_idle(jobs_env):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 12), vals(daily_yield_kwh=3.0))
    probe.v = ("online", 0.0, False)
    await jobs.tick()
    h._now.t = T(2026, 7, 1, 12, 20)
    probe.v = ("online", 500.0, False)
    await jobs.tick()
    probe.v = ("online", 0.0, False)
    h._now.t = T(2026, 7, 1, 12, 40)
    await jobs.tick()
    h._now.t = T(2026, 7, 1, 13, 5)       # 25 min since the idle restarted
    await jobs.tick()
    assert [m for m in bus.sent if m[0] == "daily/summary"] == []


@pytest.mark.parametrize("probe_value", [("offline", None, False), ("online", 5.0, True)])
async def test_summary_offline_or_standby(jobs_env, probe_value):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 12), vals(daily_yield_kwh=3.0))
    probe.v = ("online", 120.0, False)    # evening: little power left before the inverter sleeps
    await jobs.tick()
    probe.v = probe_value
    await jobs.tick()
    h._now.t = T(2026, 7, 1, 12, 31)
    await jobs.tick()
    assert any(m[0] == "daily/summary" for m in bus.sent)


async def test_summary_not_on_dongle_outage_at_noon(jobs_env):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 12), vals(daily_yield_kwh=20.0))
    probe.v = ("online", 6500.0, False)   # full production, then the dongle drops out
    await jobs.tick()
    probe.v = ("offline", None, False)
    for hh, mm in ((12, 5), (12, 40), (13, 30)):
        h._now.t = T(2026, 7, 1, hh, mm)
        await jobs.tick()
    assert not any(m[0] == "daily/summary" for m in bus.sent)


async def test_summary_not_without_yield(jobs_env):
    jobs, bus, probe, h = jobs_env
    probe.v = ("online", 0.0, False)
    await jobs.tick()
    h._now.t = T(2026, 7, 1, 14)
    await jobs.tick()
    assert not any(m[0] == "daily/summary" for m in bus.sent)


async def test_summary_no_duplicate_after_restart(jobs_env, cfg):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 12), vals(daily_yield_kwh=3.0))
    await h.flush()
    probe.v = ("online", 0.0, False)
    await jobs.tick()
    h._now.t = T(2026, 7, 1, 12, 31)
    await jobs.tick()
    assert sum(m[0] == "daily/summary" for m in bus.sent) == 1
    # "restart": new objects over the same DATA_DIR
    bus2 = Bus()
    jobs2 = DailyJobs(cfg, Persistence(cfg.data_dir), h, bus2, probe, now=h._now)
    await jobs2.tick()
    h._now.t = T(2026, 7, 1, 13, 5)
    await jobs2.tick()
    h._now.t = T(2026, 7, 1, 14, 0)
    await jobs2.tick()
    assert not any(m[0] == "daily/summary" for m in bus2.sent)


async def test_summary_on_day_change_and_not_connected(jobs_env, cfg):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 20), vals(daily_yield_kwh=8.0))
    await jobs.tick()                              # day 1 known
    bus.ok = False
    h._now.t = T(2026, 7, 2, 0, 5)
    await jobs.tick()                              # day change, broker down: nothing, no marker
    assert jobs.persist.summary_sent is None
    assert not any(m[0] == "daily/summary" for m in bus.sent)
    bus.ok = True
    jobs._date = "2026-07-01"                      # (retry path: catch up for yesterday on restart)
    jobs2 = DailyJobs(cfg, Persistence(cfg.data_dir), h, bus, probe, now=h._now)
    await h.flush()
    await jobs2.tick()
    (s, p, r), = [m for m in bus.sent if m[0] == "daily/summary"]
    assert p["date"] == "2026-07-01" and p["yield_kwh"] == 8.0 and r is True
    assert jobs2.persist.summary_sent == "2026-07-01"


async def test_summary_day_change_publishes_previous_day(jobs_env):
    jobs, bus, probe, h = jobs_env
    h.record(T(2026, 7, 1, 20), vals(daily_yield_kwh=8.0))
    await jobs.tick()
    h._now.t = T(2026, 7, 2, 0, 1)
    await jobs.tick()
    (_, p, _), = [m for m in bus.sent if m[0] == "daily/summary"]
    assert p["date"] == "2026-07-01"


# ------------------------------------------------------------------ warnings
D0 = date(2026, 7, 20)          # day under test


def seed(cfg, day, *, ins_v=None, pv1=None, pv2=None, power=500.0, table="samples_5m", yield_kwh=20.0):
    """One production block (12 values) of ``day`` plus its daily row."""
    base = ep(datetime.combine(day, datetime.min.time(), tzinfo=UTC)) + 10 * 3600
    step = 300 if table == "samples_5m" else 10
    for i in range(12):
        dbexec(cfg, f"INSERT OR REPLACE INTO {table} (ts, input_power_w, "
                    "insulation_resistance_mohm, pv1_power_w, pv2_power_w) VALUES (?,?,?,?,?)",
               (base + i * step, power, ins_v, pv1, pv2))
    put_daily(cfg, day.isoformat(), yield_kwh)


def seed_range(cfg, start, end, **kw):
    d = start
    while d <= end:
        seed(cfg, d, **kw)
        d += timedelta(days=1)


@pytest.fixture
async def wenv(cfg):
    h = History(cfg, "UTC", now=Now(datetime.combine(D0 + timedelta(days=1), datetime.min.time(),
                                                      tzinfo=UTC) + timedelta(hours=1)))
    await h.start()
    bus = Bus()
    jobs = DailyJobs(cfg, Persistence(cfg.data_dir), h, bus, Probe(), now=h._now)
    yield jobs, bus, h
    await h.close()


def active(jobs):
    return {a["id"]: a for a in jobs.warnings_view()["active"]}


async def test_insulation_trigger_and_clear(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=30), D0 - timedelta(days=1), ins_v=10.0)
    seed(cfg, D0, ins_v=3.0, table="samples")            # fine level for the last day
    await jobs.run_warnings(D0.isoformat())
    a = active(jobs)["insulation_low"]
    assert a["value"] == 3.0 and a["baseline"] == 10.0 and a["unit"] == "MΩ"
    assert a["since"].endswith("Z")
    topic, payload, retain = bus.sent[-1]
    assert topic == "warnings" and retain is True and payload["checked"].endswith("Z")
    assert payload["active"][0]["id"] == "insulation_low"
    since = a["since"]
    # idempotent for the same day; next day fine again -> cleared
    await jobs.run_warnings(D0.isoformat())
    nxt = D0 + timedelta(days=1)
    seed(cfg, nxt, ins_v=9.0, table="samples")
    await jobs.run_warnings(nxt.isoformat())
    assert active(jobs) == {} and bus.sent[-1][1]["active"] == []
    assert since


async def test_insulation_absolute_limit_and_ratio_env(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=30), D0 - timedelta(days=1), ins_v=0.15)
    seed(cfg, D0, ins_v=0.09)          # 0.09 > 0.5 * 0.15 but < 0.1 MOhm
    await jobs.run_warnings(D0.isoformat())
    assert "insulation_low" in active(jobs)


async def test_insulation_ratio_threshold_not_reached(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=30), D0 - timedelta(days=1), ins_v=10.0)
    seed(cfg, D0, ins_v=6.0)
    await jobs.run_warnings(D0.isoformat())
    assert active(jobs) == {}
    cfg.warn_insulation_ratio = 0.7
    jobs.persist.warnings = {}
    await jobs.run_warnings(D0.isoformat())
    assert "insulation_low" in active(jobs)


async def test_warnings_too_little_baseline_no_check(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=6), D0 - timedelta(days=1), ins_v=10.0, pv1=1000.0, pv2=1000.0)
    seed(cfg, D0, ins_v=1.0, pv1=3000.0, pv2=100.0)
    await jobs.run_warnings(D0.isoformat())
    assert active(jobs) == {}
    assert jobs.warnings_view()["checked"] is not None


async def test_insulation_only_counts_production(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=30), D0 - timedelta(days=1), ins_v=10.0)
    seed(cfg, D0, ins_v=0.01, power=50.0)         # below 100 W: not evaluated (no value)
    await jobs.run_warnings(D0.isoformat())
    assert active(jobs) == {}


async def test_string_imbalance_two_day_rule_and_clear(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=31), D0 - timedelta(days=2), pv1=1000.0, pv2=1000.0)
    d1 = D0 - timedelta(days=1)
    seed(cfg, d1, pv1=1500.0, pv2=500.0, table="samples")     # 75 % instead of 50 %
    seed(cfg, D0, pv1=1500.0, pv2=500.0, table="samples")
    await jobs.run_warnings(d1.isoformat())
    assert active(jobs) == {}                                  # first day only: not yet
    await jobs.run_warnings(D0.isoformat())
    a = active(jobs)["string_imbalance"]
    assert a["unit"] == "%" and a["value"] == 75.0 and a["baseline"] == 50.0
    nxt = D0 + timedelta(days=1)
    seed(cfg, nxt, pv1=1000.0, pv2=1000.0, table="samples")
    await jobs.run_warnings(nxt.isoformat())
    assert active(jobs) == {}


async def test_string_single_deviation_no_warning_and_low_yield_unscored(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=31), D0 - timedelta(days=2), pv1=1000.0, pv2=1000.0)
    d1 = D0 - timedelta(days=1)
    seed(cfg, d1, pv1=1000.0, pv2=1000.0, table="samples")
    seed(cfg, D0, pv1=1500.0, pv2=500.0, table="samples")
    await jobs.run_warnings(D0.isoformat())
    assert active(jobs) == {}                                  # previous scored day was fine
    # low-yield day (< 30 % of the median) is not scored -> state unchanged
    nxt = D0 + timedelta(days=1)
    seed(cfg, nxt, pv1=3000.0, pv2=10.0, table="samples", yield_kwh=2.0)
    await jobs.run_warnings(nxt.isoformat())
    assert active(jobs) == {}


async def test_warnings_with_null_import_data_and_state_persists(wenv, cfg):
    jobs, bus, h = wenv
    # imported 5-minute rows: only power/yield columns filled, everything else NULL
    seed_range(cfg, D0 - timedelta(days=40), D0, ins_v=None, pv1=None, pv2=None)
    await jobs.run_warnings(D0.isoformat())
    assert active(jobs) == {}
    again = Persistence(cfg.data_dir)
    assert again.warnings["checked_date"] == D0.isoformat()


async def test_warnings_use_sql_aggregation_both_levels(wenv, cfg):
    jobs, bus, h = wenv
    # insulation median: 5-minute and fine rows on the same day count, but only once
    seed(cfg, D0, ins_v=4.0, table="samples")
    dbexec(cfg, "INSERT INTO samples_5m (ts, input_power_w, insulation_resistance_mohm) "
                "VALUES (?, 500, 99)", (ep(datetime.combine(D0, datetime.min.time(), tzinfo=UTC)) + 10 * 3600,))
    med, share = await h._run_db(h._query_metrics, [D0])
    assert med[D0.isoformat()] == 4.0          # the overlapping 5m bucket is ignored


async def test_catch_up_runs_warnings_for_yesterday_at_start(wenv, cfg):
    jobs, bus, h = wenv
    seed_range(cfg, D0 - timedelta(days=30), D0 - timedelta(days=1), ins_v=10.0)
    seed(cfg, D0, ins_v=2.0)
    await jobs.tick()                           # now = D0+1 01:00 -> "yesterday" = D0
    assert "insulation_low" in active(jobs)
    assert bus.sent[-1][0] == "warnings"


# ------------------------------------------------------------------ backup
async def test_backup_file_consistent_and_no_tmp(hist, cfg):
    hist.record(T(2026, 7, 1, 10), vals())
    b = Backup(cfg, hist, UTC, now=hist._now)
    assert await b.run_backup() is True
    f = cfg.data_dir / "backup" / "huawei2mqtt-2026-07-01.db"
    assert f.is_file() and not list((cfg.data_dir / "backup").glob("*.tmp"))
    c = sqlite3.connect(f)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1       # buffer flushed first
        assert c.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1
    finally:
        c.close()
    st = b.status()
    assert st["enabled"] and st["keep"] == 7 and st["count"] == 1
    assert st["last_file"] == f.name and st["last_size_bytes"] == f.stat().st_size
    assert st["last_error"] is None and st["last_at"].endswith("Z")


async def test_backup_rotation_keeps_newest_only_own_names(hist, cfg):
    cfg.backup_keep = 3
    bdir = cfg.data_dir / "backup"
    bdir.mkdir()
    (bdir / "notes.txt").write_text("mine")
    (bdir / "huawei2mqtt-2026-07-01.db.bak").write_text("mine too")
    b = Backup(cfg, hist, UTC, now=hist._now)
    for day in range(1, 7):
        hist._now.t = T(2026, 7, day, 4)
        await b.run_backup()
    names = sorted(p.name for p in bdir.glob("huawei2mqtt-*.db"))
    assert names == [f"huawei2mqtt-2026-07-0{d}.db" for d in (4, 5, 6)]
    assert (bdir / "notes.txt").exists() and (bdir / "huawei2mqtt-2026-07-01.db.bak").exists()
    assert b.status()["count"] == 3


async def test_backup_keep_zero_is_off(hist, cfg):
    cfg.backup_keep = 0
    b = Backup(cfg, hist, UTC, now=hist._now)
    await b.tick()
    assert not (cfg.data_dir / "backup").exists()
    assert b.status()["enabled"] is False


async def test_backup_failure_is_not_fatal(hist, cfg):
    (cfg.data_dir / "backup").write_text("a file where the directory should be")
    b = Backup(cfg, hist, UTC, now=hist._now)
    assert await b.run_backup() is False
    assert b.status()["last_error"]


async def test_backup_schedule(hist, cfg):
    h = hist
    h._now.t = T(2026, 7, 1, 10)
    b = Backup(cfg, h, UTC, now=h._now)
    await b.tick()                                   # none exists -> at start
    assert b.status()["count"] == 1
    await b.tick()
    h._now.t = T(2026, 7, 2, 2, 0)
    await b.tick()                                   # before 03:00: nothing
    assert b.status()["count"] == 1
    h._now.t = T(2026, 7, 2, 3, 5)
    await b.tick()
    assert b.status()["count"] == 2 and b.status()["last_file"].endswith("2026-07-02.db")
    await b.tick()
    assert b.status()["count"] == 2                  # once a day


async def test_backup_not_at_start_when_recent(hist, cfg):
    h = hist
    h._now.t = T(2026, 7, 2, 3, 5)
    b0 = Backup(cfg, h, UTC, now=h._now)
    await b0.run_backup()
    h._now.t = T(2026, 7, 2, 9, 0)
    b = Backup(cfg, h, UTC, now=h._now)              # restart, newest backup is fresh
    await b.tick()
    assert b.status()["count"] == 1
    h._now.t = T(2026, 7, 3, 3, 10)
    await b.tick()
    assert b.status()["count"] == 2


# ------------------------------------------------------------------ API: CSV, context, status, MIME
@pytest.fixture
async def web(cfg, dev, cache, rt, tmp_path):
    cfg.portal_password = "pw"
    wd = tmp_path / "webroot"
    wd.mkdir()
    (wd / "index.html").write_text("<html></html>")
    (wd / "manifest.webmanifest").write_text('{"name":"huawei2mqtt"}')
    (wd / "sw.js").write_text("self.addEventListener('fetch',()=>{});")
    (wd / "app.js").write_text("1;")
    (wd / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    cfg.web_dir = wd
    await fill(cache, dev)
    rt.set_serial("SN12345")
    now = Now(T(2026, 7, 1, 12))
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    for i in range(3):
        h.record(T(2026, 7, 1, 10) + timedelta(minutes=10 * i),
                 vals(input_power_w=1000 * i + 0.5, daily_yield_kwh=i + 0.25))
    await h.flush()
    api = Api(cfg, rt, cache, CommandExecutor(cfg, dev, cache, rt), RingBufferHandler(),
              Sessions(b"k" * 32, lambda: cfg.portal_password), history=h)
    api.backup = Backup(cfg, h, UTC, now=now)
    api.jobs = DailyJobs(cfg, Persistence(cfg.data_dir), h, Bus(), Probe(), now=now)
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    yield client, h
    await client.close()
    await h.close()


async def test_mime_types_and_sw_header(web):
    c, _ = web
    r = await c.get("/manifest.webmanifest")
    assert r.status == 200 and r.content_type == "application/manifest+json"
    r = await c.get("/sw.js")
    assert r.content_type == "text/javascript" and r.headers["Service-Worker-Allowed"] == "/"
    assert (await c.get("/icon-192.png")).content_type == "image/png"
    r = await c.get("/app.js")
    assert r.content_type == "application/javascript" and "Service-Worker-Allowed" not in r.headers
    assert (await c.get("/nope.png")).status == 404


async def test_csv_samples_comma(web):
    c, _ = web
    r = await c.get("/api/export/samples.csv?from=2026-07-01T10:00:00Z&to=2026-07-01T11:00:00Z"
                    "&fields=input_power_w,daily_yield_kwh")
    assert r.status == 200 and r.content_type == "text/csv"
    assert r.headers["Content-Disposition"].startswith('attachment; filename="huawei2mqtt-samples-')
    raw = await r.read()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == "time_local,epoch,input_power_w,daily_yield_kwh"
    assert lines[1] == f"2026-07-01 12:00:00,{ep(T(2026, 7, 1, 10))},0.5,0.25"
    assert len(lines) == 4


async def test_csv_samples_semicolon_decimal_comma(web):
    c, _ = web
    r = await c.get("/api/export/samples.csv?from=2026-07-01T10:00:00Z&to=2026-07-01T11:00:00Z"
                    "&fields=input_power_w&sep=semicolon")
    lines = (await r.read()).decode("utf-8-sig").splitlines()
    assert lines[0] == "time_local;epoch;input_power_w"
    assert lines[1].endswith(";0,5") and lines[2].endswith(";1000,5")


async def test_csv_samples_both_levels_in_order_and_default_fields(web, cfg):
    c, h = web
    dbexec(cfg, "INSERT INTO samples_5m (ts, input_power_w) VALUES (?, 77)", (ep(T(2026, 6, 1, 10)),))
    r = await c.get("/api/export/samples.csv?from=2026-06-01&to=2026-07-01")
    lines = (await r.read()).decode("utf-8-sig").splitlines()
    assert "2026-07-01" in r.headers["Content-Disposition"] and "2026-06-01" in r.headers["Content-Disposition"]
    head = lines[0].split(",")
    assert head[:2] == ["time_local", "epoch"] and "input_power_w" in head and len(head) > 20
    epochs = [int(x.split(",")[1]) for x in lines[1:]]
    assert epochs == sorted(epochs) and len(epochs) == 4
    assert epochs[0] == ep(T(2026, 6, 1, 10))              # 5-minute row first, then the fine ones
    i = head.index("input_power_w")
    assert lines[1].split(",")[i] == "77" and lines[1].split(",")[head.index("pv1_power_w")] == ""


async def test_csv_limit_400_days_and_errors(web):
    c, _ = web
    r = await c.get("/api/export/samples.csv?from=2025-01-01T00:00:00Z&to=2026-07-01T00:00:00Z")
    assert r.status == 400 and (await r.json())["error"] == "range_too_large"
    ok = await c.get("/api/export/samples.csv?from=2025-06-01T00:00:00Z&to=2026-07-05T00:00:00Z"
                     "&fields=input_power_w")                       # 399 days
    assert ok.status == 200
    for q, err in (("fields=nope", "unknown_field"), ("sep=tab", "invalid_sep"),
                   ("from=x", "invalid_from")):
        r = await c.get(f"/api/export/samples.csv?{q}")
        assert r.status == 400 and (await r.json())["error"] == err


async def test_csv_daily_both_separators(web, cfg):
    c, h = web
    put_daily(cfg, "2026-06-30", 10.5, total_yield_kwh=100.25, peak_power_w=4200, peak_ts=ep(T(2026, 6, 30, 10)),
              first_production_ts=ep(T(2026, 6, 30, 3)), last_production_ts=ep(T(2026, 6, 30, 18)),
              source="import", start_observed=1, end_observed=1)
    r = await c.get("/api/export/daily.csv?from=2026-06-30&to=2026-07-01")
    assert r.content_type == "text/csv" and "huawei2mqtt-daily-2026-06-30_2026-07-01.csv" in r.headers[
        "Content-Disposition"]
    lines = (await r.read()).decode("utf-8-sig").splitlines()
    assert lines[0] == ("date,yield_kwh,total_yield_kwh,peak_power_w,peak_time_local,"
                        "first_production_local,last_production_local,production_h")
    assert lines[1] == "2026-06-30,10.5,100.25,4200,12:00:00,05:00:00,20:00:00,15"
    r = await c.get("/api/export/daily.csv?from=2026-06-30&to=2026-07-01&sep=semicolon")
    lines = (await r.read()).decode("utf-8-sig").splitlines()
    assert lines[1] == "2026-06-30;10,5;100,25;4200;12:00:00;05:00:00;20:00:00;15"
    assert lines[2].startswith("2026-07-01;2,25;")             # live day, NULL cells empty


async def test_api_daily_context_and_status(web, cfg):
    c, h = web
    put_daily(cfg, "2026-06-30", 10.0)
    d = await (await c.get("/api/daily/context")).json()
    assert d["date"] == "2026-07-01" and d["yield_kwh"] == 2.25 and d["yesterday_kwh"] == 10.0
    d = await (await c.get("/api/daily/context?date=2026-06-30")).json()
    assert d["yield_kwh"] == 10.0 and d["rank_all_time"] == 1
    assert (await c.get("/api/daily/context?date=bad")).status == 400
    st = await (await c.get("/api/status")).json()
    assert st["warnings"] == {"checked": None, "active": []}
    assert st["backup"] == {"enabled": True, "keep": 7, "count": 0, "last_at": None,
                            "last_file": None, "last_size_bytes": None, "last_error": None}


# ------------------------------------------------------------------ config
def test_config_rc4_keys():
    base = {"HUAWEI_HOST": "h", "MQTT_HOST": "m", "DATA_DIR": "/tmp/x"}
    c = Config.from_env(base)
    assert (c.backup_keep, c.warn_insulation_ratio, c.warn_string_deviation) == (7, 0.5, 0.10)
    c = Config.from_env({**base, "BACKUP_KEEP": "0", "WARN_INSULATION_RATIO": "0.3",
                         "WARN_STRING_DEVIATION": "0.2"})
    assert (c.backup_keep, c.warn_insulation_ratio, c.warn_string_deviation) == (0, 0.3, 0.2)
    for bad in ({"BACKUP_KEEP": "91"}, {"WARN_INSULATION_RATIO": "0.05"},
                {"WARN_INSULATION_RATIO": "0.95"}, {"WARN_STRING_DEVIATION": "0.01"},
                {"WARN_STRING_DEVIATION": "0.6"}):
        with pytest.raises(ConfigError):
            Config.from_env({**base, **bad})
    assert validate_editable({"backup_keep": 90})[0] == {"backup_keep": 90}
    assert "backup_keep" in validate_editable({"backup_keep": 91})[1]
    assert "backup_keep" in validate_editable({"backup_keep": True})[1]
    assert "warn_insulation_ratio" in validate_editable({"warn_insulation_ratio": 0.5})[1]


def test_version():
    import bridge
    assert bridge.__version__ == "1.0.0"
