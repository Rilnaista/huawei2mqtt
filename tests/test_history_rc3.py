"""rc3: two-level history (samples -> samples_5m compaction), cross-level queries, clear, awards timing."""

import sqlite3
from datetime import timedelta

import pytest

from bridge import history as hs
from bridge.config import Config, ConfigError, validate_editable

from .test_history import Now, T, hist, now, rows, vals  # noqa: F401  (fixtures)

NAMES = ("input_power_w", "active_power_w")


def put(cfg, table, ts, **kw):
    c = sqlite3.connect(cfg.data_dir / hs.DB_NAME)
    try:
        cols = ", ".join(["ts"] + list(kw))
        c.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * (len(kw) + 1))})",
                  (ts, *kw.values()))
        c.commit()
    finally:
        c.close()


def ep(dt):
    return int(dt.timestamp())


async def test_schema_both_tables_and_alter(cfg, now):
    c = sqlite3.connect(cfg.data_dir / hs.DB_NAME)   # rc2 DB: only samples with one column
    c.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, input_power_w REAL)")
    c.execute("INSERT INTO samples VALUES (1, 5)")
    c.commit()
    c.close()
    h = hs.History(cfg, "UTC", now=now)
    await h.start()
    for t in ("samples", "samples_5m"):
        cols = {r[1] for r in rows(cfg, f"PRAGMA table_info({t})")}
        assert set(hs.FIELD_NAMES) <= cols
    # rc2 DB without migration step: the old row was condensed on start, not lost
    assert rows(cfg, "SELECT ts, input_power_w FROM samples_5m") == [(0, 5.0)]
    await h.close()


async def test_compaction_mean_null_bucket_edge(cfg, hist, now):  # noqa: F811
    cfg.history_days = 1
    cutoff = ep(now.t - timedelta(days=1)) // 300 * 300
    b = cutoff - 600                       # bucket start, fully before cutoff
    put(cfg, "samples", b + 0, input_power_w=100.0, active_power_w=None)
    put(cfg, "samples", b + 10, input_power_w=300.0, active_power_w=None)
    put(cfg, "samples", b + 20, input_power_w=None, active_power_w=None)   # NULL ignored in mean
    put(cfg, "samples", b + 299, input_power_w=200.0, active_power_w=None)
    put(cfg, "samples", b + 300, input_power_w=1000.0, active_power_w=4.0)  # next bucket
    put(cfg, "samples", cutoff, input_power_w=7.0)                          # == cutoff: stays fine
    put(cfg, "samples", cutoff + 5, input_power_w=8.0)
    await hist.purge()
    r5 = rows(cfg, "SELECT ts, input_power_w, active_power_w FROM samples_5m ORDER BY ts")
    assert r5 == [(b, 200.0, None), (b + 300, 1000.0, 4.0)]
    assert [r[0] for r in rows(cfg, "SELECT ts FROM samples ORDER BY ts")] == [cutoff, cutoff + 5]
    await hist.purge()                                                       # idempotent
    assert rows(cfg, "SELECT COUNT(*) FROM samples_5m")[0][0] == 2


async def test_compaction_existing_bucket_wins_and_never_deletes(cfg, hist, now):  # noqa: F811
    cfg.history_days = 1
    b = (ep(now.t) - 3 * 86400) // 300 * 300
    put(cfg, "samples_5m", b, input_power_w=999.0)
    put(cfg, "samples", b + 10, input_power_w=1.0)
    put(cfg, "samples", b + 20, input_power_w=3.0)
    await hist.purge()
    assert rows(cfg, "SELECT ts, input_power_w FROM samples_5m") == [(b, 999.0)]
    assert rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 0


async def test_compaction_backlog_over_days(cfg, hist, now):  # noqa: F811
    cfg.history_days = 2
    base = (ep(now.t) - 10 * 86400) // 300 * 300
    for d in range(5):
        for k in range(3):
            put(cfg, "samples", base + d * 86400 + k * 60, input_power_w=float(d * 10 + k))
    put(cfg, "samples", ep(now.t) - 100, input_power_w=1.0)         # recent: stays
    await hist.purge()
    r5 = rows(cfg, "SELECT ts, input_power_w FROM samples_5m ORDER BY ts")
    assert [r[1] for r in r5] == [1.0, 11.0, 21.0, 31.0, 41.0]
    assert rows(cfg, "SELECT COUNT(*) FROM samples")[0][0] == 1


async def test_query_across_boundary_and_resolution(cfg, hist, now):  # noqa: F811
    t0 = ep(now.t) // 300 * 300
    for k in range(-12, 0):                           # 5m rows t0-3600 .. t0-300
        put(cfg, "samples_5m", t0 + k * 300, input_power_w=100.0)
    for k in range(0, 6):                             # fine rows from t0 on
        put(cfg, "samples", t0 + k * 10, input_power_w=500.0)
    iso = lambda e: hs.aw.iso(e)  # noqa: E731
    mixed = await hist.samples(iso(t0 - 3600), iso(t0 + 60), ["input_power_w"], 4)
    assert mixed["resolution_s"] == "mixed"
    av = mixed["series"]["input_power_w"]["avg"]
    assert av[0] == 100.0 and av[-1] > 100.0
    old = await hist.samples(iso(t0 - 3600), iso(t0 - 300), ["input_power_w"], 2)
    assert old["resolution_s"] == 300
    new = await hist.samples(iso(t0), iso(t0 + 60), ["input_power_w"], 2)
    assert new["resolution_s"] == 10
    # one year back, unlimited range, finishes and respects points
    yr = await hist.samples(iso(t0 - 365 * 86400), iso(t0 + 60), ["input_power_w"], 2000)
    assert len(yr["t"]) == 2000 and yr["resolution_s"] == "mixed"


async def test_query_year_of_5m_is_fast(cfg, hist, now):  # noqa: F811
    import time
    t0 = ep(now.t) // 300 * 300 - 400 * 86400
    c = sqlite3.connect(cfg.data_dir / hs.DB_NAME)
    c.executemany("INSERT INTO samples_5m (ts, input_power_w, active_power_w) VALUES (?,?,?)",
                  [(t0 + i * 300, float(i % 100), 1.0) for i in range(105_000)])
    c.commit()
    c.close()
    st = time.monotonic()
    d = await hist.samples(hs.aw.iso(t0), hs.aw.iso(t0 + 105_000 * 300), None, 2000)
    assert time.monotonic() - st < 3 and d["resolution_s"] in (300, "mixed")
    assert sum(v is not None for v in d["series"]["input_power_w"]["avg"]) > 1900


async def test_ram_buffer_counts_as_fine(cfg, hist, now):  # noqa: F811
    hist.record(now.t, vals(input_power_w=777))
    d = await hist.samples(hs.aw.iso(ep(now.t)), hs.aw.iso(ep(now.t) + 60), ["input_power_w"], 2)
    assert d["resolution_s"] == 10 and 777 in d["series"]["input_power_w"]["avg"]


async def test_status_and_clear_both_levels(cfg, hist, now):  # noqa: F811
    put(cfg, "samples_5m", 600, input_power_w=1.0)
    put(cfg, "samples", ep(now.t), input_power_w=2.0)
    await hist._refresh_stats()
    st = hist.status()
    assert st["samples"] == 1 and st["samples_5m"] == 1
    assert st["oldest"] == "1970-01-01T00:10:00Z" and st["detail_oldest"] == hs.aw.iso(ep(now.t))
    r = await hist.clear("samples")
    assert r["deleted"]["samples"] == 1 and r["deleted"]["samples_5m"] == 1
    put(cfg, "samples_5m", 600, input_power_w=1.0)
    r = await hist.clear("all")
    assert r["deleted"]["samples_5m"] == 1
    assert hist.status()["oldest"] is None


def test_settings_bounds_rc3(tmp_path):
    ok, err = validate_editable({"history_days": 365})
    assert not err and ok["history_days"] == 365
    for v in (0, 366):
        assert "history_days" in validate_editable({"history_days": v})[1]
    base = {"HUAWEI_HOST": "h", "MQTT_HOST": "m", "DATA_DIR": str(tmp_path)}
    assert Config.from_env(base).history_days == 90
    assert Config.from_env({**base, "HISTORY_DAYS": "365"}).history_days == 365
    with pytest.raises(ConfigError):
        Config.from_env({**base, "HISTORY_DAYS": "366"})
