"""History store: schema, flush, retention, day change (TZ), peak_ts, milestones, queries, clear."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bridge import history as hs
from bridge.history import History, RequestError

UTC = timezone.utc


def T(y, m, d, hh=12, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


class Now:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def now():
    return Now(T(2026, 7, 1, 12))


@pytest.fixture
async def hist(cfg, now):
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    yield h
    await h.close()


def vals(**kw):
    base = {"input_power_w": 1000, "active_power_w": 950, "daily_yield_kwh": 1.0,
            "total_yield_kwh": 100.0, "peak_power_today_w": 1000}
    base.update(kw)
    return base


def rows(cfg, sql):
    c = sqlite3.connect(cfg.data_dir / hs.DB_NAME)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


async def test_schema_columns_and_no_strings(cfg, hist):
    cols = [r[1] for r in rows(cfg, "PRAGMA table_info(samples)")]
    assert cols[0] == "ts" and set(cols[1:]) == set(hs.FIELD_NAMES)
    for banned in ("status", "alarms", "startup_time", "shutdown_time", "peak_power_today_ts"):
        assert banned not in cols
    assert {"input_power_w", "pv1_power_w", "running", "daily_yield_kwh"} <= set(cols)
    assert [r[1] for r in rows(cfg, "PRAGMA table_info(daily)")] == [
        "date", "yield_kwh", "total_yield_kwh", "peak_power_w", "peak_ts",
        "first_production_ts", "last_production_ts", "source", "start_observed", "end_observed"]
    assert [r[1] for r in rows(cfg, "PRAGMA table_info(milestones)")] == ["kwh", "ts"]
    assert rows(cfg, "PRAGMA journal_mode")[0][0] == "wal"


async def test_add_column_for_new_field(cfg, now):
    c = sqlite3.connect(cfg.data_dir / hs.DB_NAME)
    c.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, input_power_w REAL)")
    c.commit()
    c.close()
    h = History(cfg, "UTC", now=now)
    await h.start()
    cols = {r[1] for r in rows(cfg, "PRAGMA table_info(samples)")}
    assert set(hs.FIELD_NAMES) <= cols
    await h.close()


async def test_buffer_then_flush_and_shutdown(cfg, hist, now):
    hist.record(now.t, vals(running=True))
    assert rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 0       # RAM only
    await hist.flush()
    r = rows(cfg, "SELECT ts, input_power_w, running FROM samples")
    assert r == [(int(now.t.timestamp()), 1000.0, 1.0)]               # bool -> 1.0
    hist.record(now.t + timedelta(seconds=10), vals(running=True))
    await hist.close()                                                  # final commit
    assert rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 2
    assert rows(cfg, "SELECT running FROM samples ORDER BY ts DESC")[0][0] == 1.0


async def test_db_error_is_not_fatal(cfg, hist, now, monkeypatch):
    hist.record(now.t, vals())

    def boom(*a):
        raise sqlite3.OperationalError("database is full")
    monkeypatch.setattr(hist, "_write", boom)
    await hist.flush()                              # must not raise
    assert len(hist._buf) == 1                      # kept for the next try
    monkeypatch.undo()
    await hist.flush()
    assert rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 1


async def test_start_with_unusable_dir_survives(cfg, now, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cfg.data_dir = blocker / "sub"
    h = History(cfg, "UTC", now=now)
    await h.start()
    h.record(now.t, vals())
    await h.flush()
    await h.close()


async def test_retention_purge(cfg, hist, now):
    cfg.history_days = 2
    for days_ago in (5, 3, 1, 0):
        hist.record(now.t - timedelta(days=days_ago), vals())
    await hist.flush()
    await hist.purge()
    left = rows(cfg, "SELECT ts FROM samples ORDER BY ts")
    assert [r[0] for r in left] == [int((now.t - timedelta(days=d)).timestamp()) for d in (1, 0)]
    await hist.flush()
    assert rows(cfg, "SELECT COUNT(*) FROM daily")[0][0] == 4       # daily is never purged


async def test_day_change_uses_local_time(cfg, hist):
    # 22:30 UTC on 30 Jun = 00:30 local (CEST) on 1 Jul -> next local day
    hist.record(T(2026, 6, 30, 21, 30), vals(daily_yield_kwh=50))       # 23:30 local, 30 Jun
    assert not hist.day_rolled
    hist.record(T(2026, 6, 30, 22, 30), vals(daily_yield_kwh=0.1))      # 00:30 local, 1 Jul
    assert hist.day_rolled
    await hist.flush()
    r = rows(cfg, "SELECT date, yield_kwh FROM daily ORDER BY date")
    assert r == [("2026-06-30", 50.0), ("2026-07-01", 0.1)]


async def test_daily_fields(cfg, hist):
    hist.record(T(2026, 7, 1, 4), vals(input_power_w=0, peak_power_today_w=0, daily_yield_kwh=0))
    hist.record(T(2026, 7, 1, 5), vals(input_power_w=200, peak_power_today_w=200, daily_yield_kwh=0.2))
    hist.record(T(2026, 7, 1, 12), vals(input_power_w=9000, peak_power_today_w=9000, daily_yield_kwh=40))
    hist.record(T(2026, 7, 1, 18), vals(input_power_w=10, peak_power_today_w=9000, daily_yield_kwh=60,
                                        total_yield_kwh=160.5))
    hist.record(T(2026, 7, 1, 19), vals(input_power_w=0, peak_power_today_w=9000, daily_yield_kwh=60.5))
    await hist.flush()
    r = rows(cfg, "SELECT * FROM daily")[0]
    assert r[0] == "2026-07-01" and r[1] == 60.5 and r[2] == 100.0   # last total wins
    assert r[3] == 9000
    assert r[4] == int(T(2026, 7, 1, 12).timestamp())
    assert r[5] == int(T(2026, 7, 1, 5).timestamp())
    assert r[6] == int(T(2026, 7, 1, 18).timestamp())
    assert r[7] == "bridge"


async def test_peak_ts_set_on_rise_and_unknown_at_start(cfg, hist, now):
    # started with peak already > 0 and nothing stored: unknown
    hist.record(now.t, vals(peak_power_today_w=5000))
    assert hist.peak_ts_iso() is None
    hist.record(now.t + timedelta(seconds=10), vals(peak_power_today_w=5000))
    assert hist.peak_ts_iso() is None
    hist.record(now.t + timedelta(seconds=20), vals(peak_power_today_w=6000))
    assert hist.peak_ts_iso() == "2026-07-01T12:00:20Z"
    hist.record(now.t + timedelta(seconds=30), vals(peak_power_today_w=5500))   # lower: unchanged
    assert hist.peak_ts_iso() == "2026-07-01T12:00:20Z"


async def test_peak_ts_survives_restart(cfg, now):
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    h.record(T(2026, 7, 1, 10), vals(peak_power_today_w=100))
    h.record(T(2026, 7, 1, 11), vals(peak_power_today_w=7000))
    await h.close()
    h2 = History(cfg, "Europe/Berlin", now=now)
    await h2.start()
    assert h2.peak_ts_iso() == "2026-07-01T11:00:00Z"        # from the DB, before any sample
    h2.record(T(2026, 7, 1, 12), vals(peak_power_today_w=7000))   # same value: keeps stored ts
    assert h2.peak_ts_iso() == "2026-07-01T11:00:00Z"
    await h2.close()


async def test_peak_ts_none_on_other_day(hist, now):
    hist.record(now.t, vals(peak_power_today_w=0))
    hist.record(now.t + timedelta(seconds=10), vals(peak_power_today_w=100))
    assert hist.peak_ts_iso() is not None
    now.t += timedelta(days=1)
    assert hist.peak_ts_iso() is None


async def test_milestones_crossing(cfg, hist, now):
    cfg.milestone_step_kwh = 1000
    hist.record(now.t, vals(total_yield_kwh=20995.0))          # first value: no crossing known
    hist.record(now.t + timedelta(seconds=10), vals(total_yield_kwh=20999.9))
    hist.record(now.t + timedelta(seconds=20), vals(total_yield_kwh=21000.2))
    hist.record(now.t + timedelta(seconds=30), vals(total_yield_kwh=21001))
    await hist.flush()
    assert rows(cfg, "SELECT kwh, ts FROM milestones") == [(21000, int(now.t.timestamp()) + 20)]


async def test_milestone_prev_total_from_db_after_restart(cfg, now):
    h = History(cfg, "UTC", now=now)
    await h.start()
    h.record(now.t, vals(total_yield_kwh=999.0))
    await h.close()
    h2 = History(cfg, "UTC", now=now)
    await h2.start()
    h2.record(now.t + timedelta(minutes=5), vals(total_yield_kwh=1000.5))
    await h2.close()
    assert [r[0] for r in rows(cfg, "SELECT kwh FROM milestones")] == [1000]


async def test_samples_buckets_and_null(cfg, hist, now):
    base = T(2026, 7, 1, 12)
    for i, v in enumerate([100, 300, None, None, 50]):
        hist.record(base + timedelta(seconds=10 * i), vals(input_power_w=v))
    await hist.flush()
    d = await hist.samples(base.isoformat(), (base + timedelta(seconds=50)).isoformat(),
                           ["input_power_w"], 5)
    assert d["step_s"] == 10 and len(d["t"]) == 5
    assert d["t"][0] == int(base.timestamp() * 1000)
    s = d["series"]["input_power_w"]
    assert s["avg"] == [100, 300, None, None, 50]
    # 2 buckets of 25 s: [100,300,None] and [None,50(at t==to goes to last bucket)]
    d2 = await hist.samples(base.isoformat(), (base + timedelta(seconds=50)).isoformat(),
                            "input_power_w", 2)
    s2 = d2["series"]["input_power_w"]
    assert s2["avg"] == [200, 50] and s2["min"] == [100, 50] and s2["max"] == [300, 50]
    assert {"name": "input_power_w", "unit": "W", "group": "pv"} in d["available"]
    # empty range -> all null
    e = await hist.samples((base - timedelta(days=2)).isoformat(),
                           (base - timedelta(days=1)).isoformat(), None, 4)
    assert e["fields"] == ["input_power_w", "active_power_w"]
    assert e["series"]["input_power_w"]["avg"] == [None] * 4


async def test_samples_include_unflushed_buffer(cfg, hist, now):
    base = T(2026, 7, 1, 12)
    f, t = base.isoformat(), (base + timedelta(seconds=50)).isoformat()
    # only in RAM buffer (no flush)
    hist.record(base, vals(input_power_w=100))
    hist.record(base + timedelta(seconds=10), vals(input_power_w=300))
    d = await hist.samples(f, t, ["input_power_w"], 5)
    s = d["series"]["input_power_w"]
    assert s["avg"] == [100, 300, None, None, None]
    assert hist.status()["samples"] == 0 and rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 0
    # mix DB + buffer in the same bucket
    await hist.flush()
    hist.record(base + timedelta(seconds=11), vals(input_power_w=500))
    hist.record(base + timedelta(seconds=40), vals(input_power_w=70))
    d = await hist.samples(f, t, ["input_power_w"], 5)
    s = d["series"]["input_power_w"]
    assert s["avg"] == [100, 400, None, None, 70]
    assert s["min"] == [100, 300, None, None, 70] and s["max"] == [100, 500, None, None, 70]
    # buffer rows outside the window are ignored
    d = await hist.samples((base + timedelta(seconds=20)).isoformat(), t, ["input_power_w"], 2)
    assert d["series"]["input_power_w"]["avg"] == [None, 70]


async def test_samples_defaults_and_validation(hist, now):
    d = await hist.samples()
    assert d["from"] == "2026-06-30T22:00:00Z"                 # local midnight (CEST)
    assert d["to"] == "2026-07-01T12:00:00Z" and len(d["t"]) == 500
    big = await hist.samples(points=99999)
    assert len(big["t"]) == hs.MAX_POINTS
    for kw, reason in [({"fields": ["nope"]}, "unknown_field"),
                       ({"fields": [f"f{i}" for i in range(11)]}, "too_many_fields"),
                       ({"fields": []}, "invalid_fields"),
                       ({"points": 0}, "invalid_points"),
                       ({"points": "x"}, "invalid_points"),
                       ({"from_": "garbage"}, "invalid_from"),
                       ({"to": 5}, "invalid_to"),
                       ({"from_": "2026-07-02T00:00:00Z"}, "invalid_range")]:
        with pytest.raises(RequestError) as e:
            await hist.samples(**kw)
        assert e.value.reason == reason, kw
    ten = hs.FIELD_NAMES[:10]
    assert (await hist.samples(fields=ten))["fields"] == ten


async def test_daily_query_defaults_limit_and_overlay(cfg, hist, now):
    hist.record(T(2026, 6, 30, 12), vals(daily_yield_kwh=30))
    hist.record(now.t, vals(daily_yield_kwh=5))
    await hist.flush()
    hist.record(now.t + timedelta(seconds=10), vals(daily_yield_kwh=6))   # unflushed, shown anyway
    d = await hist.daily()
    assert [x["date"] for x in d["days"]] == ["2026-06-30", "2026-07-01"]
    assert d["days"][1]["yield_kwh"] == 6
    assert set(d["days"][0]) == {"date", "yield_kwh", "total_yield_kwh", "peak_power_w", "peak_ts",
                                 "first_production_ts", "last_production_ts", "production_s",
                                 "source", "start_observed", "end_observed"}
    one = await hist.daily("2026-06-30", "2026-06-30")
    assert len(one["days"]) == 1
    with pytest.raises(RequestError) as e:
        await hist.daily("2000-01-01", "2026-07-01")
    assert e.value.reason == "range_too_large"
    with pytest.raises(RequestError):
        await hist.daily("2026-07-02", "2026-07-01")


async def test_clear_scopes(cfg, hist, now):
    hist.record(now.t, vals(total_yield_kwh=999.0))
    hist.record(now.t + timedelta(seconds=10), vals(total_yield_kwh=1001.0))
    await hist.flush()
    assert (await hist.clear("samples"))["deleted"] == {"samples": 2, "daily": 0, "milestones": 0}
    assert rows(cfg, "SELECT COUNT(*) FROM daily")[0][0] == 1
    hist.record(now.t + timedelta(seconds=20), vals())
    r = await hist.clear("all")
    assert r["status"] == "ok" and r["deleted"] == {"samples": 0, "daily": 1, "milestones": 1}
    assert hist._buf == [] and hist._dirty == {}
    hist.record(now.t + timedelta(seconds=30), vals())
    await hist.flush()
    r = await hist.clear("daily")
    assert r["deleted"] == {"samples": 0, "daily": 1, "milestones": 0}
    assert rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 1
    with pytest.raises(RequestError):
        await hist.clear("bogus")


async def test_status(cfg, hist, now):
    hist.record(now.t, vals())
    await hist.flush()
    await hist._refresh_stats()
    st = hist.status()
    assert st["days"] == 7 and st["samples"] == 1 and st["oldest"] == "2026-07-01T12:00:00Z"
    assert st["db_size_bytes"] > 0


async def test_day_change_ignores_stale_yesterday_values(cfg, hist):
    hist.record(T(2026, 6, 30, 17), vals(input_power_w=3000, daily_yield_kwh=50, peak_power_today_w=9000))
    # 00:30 local on 1 Jul: inverter still reports yesterday's values, no production
    hist.record(T(2026, 6, 30, 22, 30), vals(input_power_w=0, daily_yield_kwh=50, peak_power_today_w=9000))
    assert hist.peak_ts_iso() is None
    await hist.flush()
    r = rows(cfg, "SELECT date, yield_kwh, peak_power_w, peak_ts FROM daily ORDER BY date")
    assert r[0] == ("2026-06-30", 50.0, 9000.0, None)   # started mid-production
    assert r[1] == ("2026-07-01", None, None, None)
    # production starts, values reset: new day gets correct values
    hist.record(T(2026, 7, 1, 4), vals(input_power_w=100, daily_yield_kwh=0, peak_power_today_w=100))
    await hist.flush()
    r = rows(cfg, "SELECT yield_kwh, peak_power_w FROM daily WHERE date='2026-07-01'")
    assert r == [(0.0, 100.0)]
    assert rows(cfg, "SELECT yield_kwh FROM daily WHERE date='2026-06-30'") == [(50.0,)]


async def test_peak_ts_correct_after_observed_night(hist):
    hist.record(T(2026, 6, 30, 17), vals(input_power_w=3000, peak_power_today_w=9000))
    hist.record(T(2026, 6, 30, 22, 30), vals(input_power_w=0, peak_power_today_w=9000))
    hist.record(T(2026, 7, 1, 4), vals(input_power_w=100, daily_yield_kwh=0.1, peak_power_today_w=100))
    assert hist.peak_ts_iso() == "2026-07-01T04:00:00Z"


async def test_start_mid_production_peak_ts_unknown_then_set(hist):
    hist.record(T(2026, 7, 1, 10), vals(input_power_w=5000, peak_power_today_w=5000))
    assert hist.peak_ts_iso() is None
    hist.record(T(2026, 7, 1, 11), vals(input_power_w=7000, peak_power_today_w=7000))
    assert hist.peak_ts_iso() == "2026-07-01T11:00:00Z"


async def test_restart_keeps_stored_day_values(cfg, now):
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    h.record(T(2026, 7, 1, 10), vals(input_power_w=100, daily_yield_kwh=5, peak_power_today_w=100))
    h.record(T(2026, 7, 1, 11), vals(input_power_w=7000, daily_yield_kwh=20, peak_power_today_w=7000))
    await h.close()
    h2 = History(cfg, "Europe/Berlin", now=now)
    await h2.start()
    h2.record(T(2026, 7, 1, 12), vals(input_power_w=6000, daily_yield_kwh=25, peak_power_today_w=7000))
    assert h2.peak_ts_iso() == "2026-07-01T11:00:00Z"
    await h2.close()
    r = rows(cfg, "SELECT yield_kwh, peak_power_w, peak_ts, first_production_ts FROM daily")
    assert r == [(25.0, 7000.0, int(T(2026, 7, 1, 11).timestamp()), int(T(2026, 7, 1, 10).timestamp()))]


# --- REQ-025: observed production start/end -----------------------------------------------
def _flags(cfg):
    return rows(cfg, "SELECT start_observed, end_observed FROM daily ORDER BY date")


async def test_start_mid_production_not_observed(cfg, hist, now):
    hist.record(T(2026, 7, 1, 9), vals())
    hist.record(T(2026, 7, 1, 9, 0, 10), vals())
    hist.record(T(2026, 7, 1, 17), vals(input_power_w=0))
    await hist.flush()
    assert _flags(cfg) == [(0, 1)]
    now.t = T(2026, 7, 1, 18)
    d = (await hist.daily())["days"][0]
    assert d["start_observed"] is False and d["end_observed"] is True and d["production_s"] is None
    a = await hist.awards(None)
    assert a["rankings"]["longest_day"] == [] and a["rankings"]["earliest_start"] == []


async def test_full_day_observed(cfg, hist, now):
    hist.record(T(2026, 7, 1, 2), vals(input_power_w=0))
    hist.record(T(2026, 7, 1, 5), vals())
    hist.record(T(2026, 7, 1, 12), vals())
    hist.record(T(2026, 7, 1, 19), vals(input_power_w=0))
    await hist.flush()
    assert _flags(cfg) == [(1, 1)]
    d = (await hist.daily())["days"][0]
    assert d["start_observed"] and d["end_observed"] and d["production_s"] == 7 * 3600
    a = await hist.awards(None)
    assert a["rankings"]["longest_day"][0]["value"] == 7 * 3600
    assert len(a["rankings"]["earliest_start"]) == 1


async def test_restart_keeps_start_observed(cfg, now):
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    h.record(T(2026, 7, 1, 2), vals(input_power_w=0))
    h.record(T(2026, 7, 1, 5), vals())
    await h.close()
    now.t = T(2026, 7, 1, 12)
    h2 = History(cfg, "Europe/Berlin", now=now)
    await h2.start()
    h2.record(T(2026, 7, 1, 12), vals())
    h2.record(T(2026, 7, 1, 19), vals(input_power_w=0))   # first sample after restart is producing
    await h2.close()
    assert _flags(cfg) == [(1, 1)]


async def test_production_resume_resets_end(cfg, hist):
    hist.record(T(2026, 7, 1, 2), vals(input_power_w=0))
    hist.record(T(2026, 7, 1, 5), vals())
    hist.record(T(2026, 7, 1, 11), vals(input_power_w=0))   # cloud
    hist.record(T(2026, 7, 1, 12), vals())
    await hist.flush()
    assert _flags(cfg) == [(1, 0)]


async def test_import_row_without_flags_counts(cfg, now):
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    await h.close()
    c = sqlite3.connect(cfg.data_dir / hs.DB_NAME)
    c.execute("INSERT INTO daily (date, yield_kwh, first_production_ts, last_production_ts, source) "
              "VALUES ('2026-06-01', 40, ?, ?, 'import')",
              (int(T(2026, 6, 1, 4).timestamp()), int(T(2026, 6, 1, 18).timestamp())))
    c.commit()
    c.close()
    h = History(cfg, "Europe/Berlin", now=now)
    await h.start()
    d = (await h.daily("2026-06-01", "2026-06-01"))["days"][0]
    assert d["start_observed"] and d["end_observed"] and d["production_s"] == 14 * 3600
    a = await h.awards(None)
    assert a["rankings"]["longest_day"][0]["value"] == 14 * 3600
    assert a["rankings"]["earliest_start"][0]["date"] == "2026-06-01"
    await h.close()
