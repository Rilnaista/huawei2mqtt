"""/api/history|daily|awards|history/clear, MQTT history/get, status extensions, settings."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridge.api import Api, Sessions
from bridge.app import App
from bridge.commands import CommandExecutor
from bridge.config import Config, ConfigError, validate_editable
from bridge.history import History
from bridge.logbuffer import RingBufferHandler

from .conftest import FakeDevice, FakeMqttClient
from .test_cache_publish import fill
from .test_history import Now, T, vals

NOW = T(2026, 7, 1, 12)


@pytest.fixture
async def env(cfg, dev, cache, rt):
    cfg.portal_password = "pw"
    await fill(cache, dev)
    rt.set_serial("SN12345")
    now = Now(NOW)
    hist = History(cfg, "Europe/Berlin", now=now)
    await hist.start()
    for i in range(6):
        hist.record(NOW - timedelta(minutes=60 - 10 * i),
                    vals(input_power_w=1000 * i, daily_yield_kwh=i))
    await hist.flush()
    api = Api(cfg, rt, cache, CommandExecutor(cfg, dev, cache, rt), RingBufferHandler(),
              Sessions(b"k" * 32, lambda: cfg.portal_password), history=hist)
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    yield client, hist
    await client.close()
    await hist.close()


async def login(c):
    assert (await c.post("/api/login", json={"password": "pw"})).status == 200


async def test_api_history_buckets(env):
    c, _ = env
    q = "from=2026-07-01T11:00:00Z&to=2026-07-01T12:00:00Z&fields=input_power_w&points=6"
    d = await (await c.get(f"/api/history?{q}")).json()
    assert d["step_s"] == 600 and len(d["t"]) == 6
    assert d["series"]["input_power_w"]["avg"] == [0, 1000, 2000, 3000, 4000, 5000]
    assert any(a["name"] == "input_power_w" and a["unit"] == "W" for a in d["available"])
    one = await (await c.get(
        "/api/history?from=2026-07-01T11:00:00Z&to=2026-07-01T12:00:00Z&fields=input_power_w&points=1")
    ).json()
    s = one["series"]["input_power_w"]
    assert s["avg"] == [2500] and s["min"] == [0] and s["max"] == [5000]


async def test_api_history_errors_and_defaults(env):
    c, _ = env
    r = await c.get("/api/history?fields=bogus")
    assert r.status == 400 and (await r.json())["error"] == "unknown_field"
    assert (await c.get("/api/history?points=0")).status == 400
    d = await (await c.get("/api/history")).json()
    assert d["fields"] == ["input_power_w", "active_power_w"]
    assert (await c.get("/api/daily?from=x")).status == 400
    assert (await c.get("/api/daily?from=2026-01-01&to=2026-07-01")).status == 200


async def test_api_daily_and_awards(env):
    c, _ = env
    d = await (await c.get("/api/daily")).json()
    assert d["days"][-1]["date"] == "2026-07-01" and d["days"][-1]["yield_kwh"] == 5
    a = await (await c.get("/api/awards")).json()
    assert a["rankings"]["best_day"][0]["value"] == 5
    assert a["milestones"]["step_kwh"] == 1000 and a["money"]["currency"] == "EUR"


async def test_clear_requires_login_and_scopes(env):
    c, hist = env
    assert (await c.post("/api/history/clear", json={"scope": "all"})).status == 401
    await login(c)
    r = await c.post("/api/history/clear", json={"scope": "bogus"})
    assert r.status == 400
    r = await (await c.post("/api/history/clear", json={"scope": "samples"})).json()
    assert r == {"status": "ok", "deleted": {"samples": 6, "samples_5m": 0, "daily": 0, "milestones": 0}}
    r = await (await c.post("/api/history/clear", json={"scope": "all"})).json()
    assert r["deleted"]["daily"] == 1
    a = await (await c.get("/api/awards")).json()
    assert a["rankings"]["best_day"] == []


async def test_clear_forbidden_without_password(env, cfg):
    c, _ = env
    cfg.portal_password = ""
    assert (await c.post("/api/history/clear", json={"scope": "all"})).status == 403


async def test_status_and_settings_extensions(env, rt, cache, cfg):
    c, hist = env
    await hist._refresh_stats()
    rt.live_ok()
    rt.last_live_at = datetime.now(timezone.utc)
    rt.schedule_next_live(7)
    rt.record_dropout()
    st = await (await c.get("/api/status")).json()
    assert st["poll"]["live_interval_s"] == 10
    assert st["poll"]["last_live_at"].endswith("Z") and st["poll"]["next_live_at"].endswith("Z")
    assert st["inverter"]["dropouts_24h"] == 1 and st["inverter"]["last_dropout"].endswith("Z")
    assert st["history"]["days"] == 90 and st["history"]["samples"] == 6
    assert st["history"]["oldest"] and st["history"]["db_size_bytes"] > 0
    s = await (await c.get("/api/settings")).json()
    assert s["readonly"]["firmware_version"] == "V100R001C00B352"
    assert s["readonly"]["software_version"] == "V100R001-02"
    assert s["editable"]["history_days"] == 90 and s["editable"]["currency"] == "EUR"
    assert s["limits"]["history_days"] == {"min": 1, "max": 365}


async def test_put_settings_new_fields(env, cfg):
    c, _ = env
    await login(c)
    body = {"history_days": 30, "feed_in_tariff": 0.081, "currency": "CHF", "co2_factor": 0.4,
            "milestone_step_kwh": 500}
    r = await c.put("/api/settings", json=body)
    assert r.status == 200
    assert (cfg.history_days, cfg.feed_in_tariff, cfg.currency) == (30, 0.081, "CHF")
    assert json.loads((cfg.data_dir / "settings.json").read_text())["milestone_step_kwh"] == 500


def test_settings_validation():
    ok, err = validate_editable({"history_days": 365, "feed_in_tariff": 0, "currency": "EUR",
                                 "co2_factor": 2, "milestone_step_kwh": 100})
    assert not err and ok["feed_in_tariff"] == 0.0
    bad = {"history_days": 0, "feed_in_tariff": 5.1, "currency": "eur", "co2_factor": -1,
           "milestone_step_kwh": 99999999}
    _, err = validate_editable(bad)
    assert set(err) == set(bad)
    _, err = validate_editable({"history_days": 1.5, "currency": "EURO", "feed_in_tariff": "1",
                                "co2_factor": True, "milestone_step_kwh": "100"})
    assert len(err) == 5


def test_env_keys_and_defaults(tmp_path):
    base = {"HUAWEI_HOST": "h", "MQTT_HOST": "m", "DATA_DIR": str(tmp_path)}
    c = Config.from_env(base)
    assert (c.history_days, c.feed_in_tariff, c.currency, c.co2_factor, c.milestone_step_kwh) == \
        (90, 0.08, "EUR", 0.38, 1000)
    c = Config.from_env({**base, "HISTORY_DAYS": "30", "FEED_IN_TARIFF": "0.081",
                         "CURRENCY": "USD", "CO2_FACTOR": "0.5", "MILESTONE_STEP_KWH": "250"})
    assert (c.history_days, c.feed_in_tariff, c.currency, c.co2_factor, c.milestone_step_kwh) == \
        (30, 0.081, "USD", 0.5, 250)
    for bad in ({"HISTORY_DAYS": "366"}, {"CURRENCY": "euro"}, {"FEED_IN_TARIFF": "9"},
                {"MILESTONE_STEP_KWH": "10"}):
        with pytest.raises(ConfigError):
            Config.from_env({**base, **bad})


# --- MQTT request/response -------------------------------------------------------------
@pytest.fixture
async def app(cfg, clock):
    a = App(cfg, RingBufferHandler(), device=FakeDevice(),
            mqtt_factory=lambda c, r, will: FakeMqttClient(will))
    a.rt.clock = clock
    a.rt.set_serial("SN12345")
    a.history._now = Now(NOW)
    await a.history.start()
    for i in range(3):
        a.history.record(NOW - timedelta(minutes=30 - 10 * i), vals(input_power_w=100 * i))
    await a.history.flush()
    a.sent = []
    a.mqtt.publish = lambda t, p, r, qos=None: a.sent.append((t, json.loads(p), r))
    yield a
    await a.history.close()


BASE = "huawei/SN12345"


async def ask(app, req, retain=False):
    app.sent.clear()
    payload = req if isinstance(req, bytes) else json.dumps(req).encode()
    await app._on_mqtt_message(f"{BASE}/history/get", payload, retain)
    return app.sent


async def test_subscribed(app):
    assert f"{BASE}/history/get" in app._subscriptions(BASE)


async def test_mqtt_samples_result_equals_api_data(app, clock):
    sent = await ask(app, {"id": "a1", "type": "samples", "from": "2026-07-01T11:30:00Z",
                           "to": "2026-07-01T12:00:00Z", "fields": ["input_power_w"], "points": 3})
    (topic, res, retain), = sent
    assert topic == f"{BASE}/history/result" and retain is False
    assert res["id"] == "a1" and res["type"] == "samples" and res["status"] == "ok"
    assert res["series"]["input_power_w"]["avg"] == [0, 100, 200]
    direct = await app.history.samples("2026-07-01T11:30:00Z", "2026-07-01T12:00:00Z",
                                       ["input_power_w"], 3)
    assert {k: res[k] for k in direct} == direct


async def test_mqtt_daily_and_awards(app, clock):
    (_, res, _), = await ask(app, {"id": 7, "type": "daily"})
    assert res["status"] == "ok" and res["id"] == 7 and res["days"][0]["date"] == "2026-07-01"
    clock.t += 3
    (_, res, _), = await ask(app, {"id": "x", "type": "awards"})
    assert res["status"] == "ok" and "rankings" in res and "generated" in res


async def test_mqtt_retained_ignored_and_rate_limit(app, clock):
    assert await ask(app, {"id": "r", "type": "daily"}, retain=True) == []
    (_, res, _), = await ask(app, {"id": "1", "type": "daily"})
    assert res["status"] == "ok"
    clock.t += 1.9
    (_, res, _), = await ask(app, {"id": "2", "type": "daily"})
    assert res == {"id": "2", "type": "daily", "status": "rejected", "reason": "rate_limited"}
    clock.t += 0.2
    (_, res, _), = await ask(app, {"id": "3", "type": "daily"})
    assert res["status"] == "ok"


async def test_mqtt_errors(app, clock):
    cases = [(b"not json", "invalid_json"), (b"[1]", "invalid_json"),
             ({"id": "e", "type": "nope"}, "unknown_type"), ({"id": "e"}, "unknown_type"),
             ({"id": "e", "type": "samples", "fields": ["zzz"]}, "unknown_field"),
             ({"id": "e", "type": "samples", "fields": [f"f{i}" for i in range(11)]},
              "too_many_fields"),
             ({"id": "e", "type": "samples", "points": -1}, "invalid_points"),
             ({"id": "e", "type": "samples", "from": "bad"}, "invalid_from"),
             ({"id": "e", "type": "daily", "from": "2000-01-01"}, "range_too_large")]
    for req, reason in cases:
        clock.t += 3
        (_, res, retain), = await ask(app, req)
        assert res["status"] == "rejected" and res["reason"] == reason, req
        assert retain is False


async def test_awards_published_retained_and_throttled(app, clock):
    await app._publish_awards(force=True)
    (t, data, retain), = app.sent
    assert t == f"{BASE}/awards" and retain is True and data["milestones"]["step_kwh"] == 1000
    app.sent.clear()
    await app._publish_awards()                 # within STATIC interval: nothing
    assert app.sent == []
    clock.t += app.cfg.poll_interval_static + 1
    await app._publish_awards()                 # unchanged content: nothing
    assert app.sent == []
    app.cfg.feed_in_tariff = 0.5
    clock.t += app.cfg.poll_interval_static + 1
    await app._publish_awards()                 # changed: published
    assert len(app.sent) == 1


async def test_end_to_end_live_cycle_feeds_history_and_publishes(cfg):
    import asyncio
    import sqlite3
    from .test_mqtt import wait_for
    cfg.portal_listen_port, cfg.portal_host, cfg.poll_interval_live = 18998, "127.0.0.1", 5
    cfg.backup_keep = 0        # the startup backup would flush the RAM buffer this test inspects
    dev = FakeDevice()
    a = App(cfg, RingBufferHandler(), device=dev,
            mqtt_factory=lambda c, r, will: FakeMqttClient(will))
    task = asyncio.create_task(a.run(install_signals=False))
    await wait_for(lambda: a.rt.inverter == "online" and a.rt.mqtt_connected
                   and any(c.will[0] == "huawei/SN12345/availability"
                           for c in FakeMqttClient.instances), 5)
    await wait_for(lambda: a.history._buf or a.history._dirty, 3)
    c = FakeMqttClient.instances[-1]
    a.pub.publish_snapshot()
    await wait_for(lambda: any(t == "huawei/SN12345/energy" for t, *_ in c.published))
    energy = next(json.loads(p) for t, p, *_ in c.published if t == "huawei/SN12345/energy")
    assert "peak_power_today_ts" in energy and energy["peak_power_today_ts"] is None
    await wait_for(lambda: any(t == "huawei/SN12345/awards" and r for t, _, _, r in c.published))
    assert ("huawei/SN12345/history/get", 1) in c.subscribed
    modbus_reads = len(dev.reads)
    a.request_stop()
    await asyncio.wait_for(task, 10)
    db = sqlite3.connect(cfg.data_dir / "huawei2mqtt.db")
    assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 1      # flushed at shutdown
    assert db.execute("SELECT input_power_w FROM samples").fetchone()[0] == 3500
    db.close()
    assert len(dev.reads) == modbus_reads                                      # history = no extra reads


async def test_rate_limited_carries_request_type(app, clock):
    await ask(app, {"id": "1", "type": "daily"})
    clock.t += 0.5
    (_, res, _), = await ask(app, {"id": "2", "type": "samples"})
    assert res == {"id": "2", "type": "samples", "status": "rejected", "reason": "rate_limited"}
    clock.t += 0.5
    (_, res, _), = await ask(app, b"garbage")
    assert res["type"] is None and res["reason"] == "rate_limited"


async def test_awards_only_after_first_live_cycle(app, clock):
    app.mqtt.publish = lambda t, p, r, qos=None: app.sent.append((t, p, r))   # raw payloads
    await app._on_mqtt_connected()
    assert not any(t.endswith("/awards") for t, _, _ in app.sent)   # not at connect
    app.sent.clear()
    await app._on_cycle("live", True)
    assert [t for t, _, _ in app.sent if t.endswith("/awards")] == [f"{BASE}/awards"]
    app.sent.clear()
    await app._on_mqtt_connected()                                   # reconnect after live ok
    assert any(t.endswith("/awards") for t, _, _ in app.sent)
