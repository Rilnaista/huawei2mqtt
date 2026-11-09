import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridge.api import COOKIE, Api, Sessions
from bridge.commands import CommandExecutor
from bridge.logbuffer import RingBufferHandler

from .test_cache_publish import fill


async def make_client(cfg, dev, cache, rt, password="pw", web=None):
    cfg.portal_password = password
    if web is not None:
        cfg.web_dir = web
    await fill(cache, dev)
    rt.set_serial("SN12345")
    exe = CommandExecutor(cfg, dev, cache, rt)
    ring = RingBufferHandler()
    api = Api(cfg, rt, cache, exe, ring, Sessions(b"k" * 32, lambda: cfg.portal_password))
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    return client, api


@pytest.fixture
async def client(cfg, dev, cache, rt):
    c, api = await make_client(cfg, dev, cache, rt)
    c.api = api
    yield c
    await c.close()


async def login(c, pw="pw"):
    r = await c.post("/api/login", json={"password": pw})
    return r


async def test_healthz_and_readonly_endpoints_need_no_login(client):
    assert (await (await client.get("/healthz")).json()) == {"status": "ok"}
    st = await (await client.get("/api/status")).json()
    assert st["auth"] == {"password_set": True, "logged_in": False}
    assert st["inverter"]["state"] == "offline" and st["dongle"]["port"] == 502
    assert st["mqtt"]["base_topic"] == "huawei/SN12345" and st["write_access"] == "unverified"
    v = await (await client.get("/api/values")).json()
    assert v["pv"]["pv1_voltage_v"] == 300.0 and v["registers"][0]["address"] == 30000
    assert set(v) >= {"pv", "grid", "energy", "inverter", "device", "control", "registers"}
    logs = await (await client.get("/api/logs?since=0")).json()
    assert "entries" in logs
    assert (await client.get("/api/logs?since=x")).status == 400


async def test_write_endpoints_require_login(client, dev):
    for method, path, body in [("put", "/api/settings", {"log_level": "DEBUG"}),
                               ("post", "/api/command", {"command": "power", "value": "on"}),
                               ("post", "/api/write-test", {}), ("post", "/api/logout", {})]:
        r = await getattr(client, method)(path, json=body)
        assert r.status == 401, path
    assert dev.writes == []


async def test_403_without_portal_password(cfg, dev, cache, rt):
    c, _ = await make_client(cfg, dev, cache, rt, password="")
    try:
        for method, path, body in [("put", "/api/settings", {"log_level": "DEBUG"}),
                                   ("post", "/api/command", {"command": "power", "value": "on"}),
                                   ("post", "/api/write-test", {}),
                                   ("post", "/api/login", {"password": ""})]:
            assert (await getattr(c, method)(path, json=body)).status == 403, path
        assert (await c.get("/api/status")).status == 200
        assert dev.writes == []
    finally:
        await c.close()


async def test_login_cookie_flags_and_logout(client):
    assert (await login(client, "wrong")).status == 401
    r = await login(client)
    assert r.status == 200
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    st = await (await client.get("/api/status")).json()
    assert st["auth"]["logged_in"] is True
    r = await client.post("/api/logout")
    assert r.status == 204
    client.session.cookie_jar.clear()
    assert (await (await client.get("/api/status")).json())["auth"]["logged_in"] is False


async def test_cookie_secure_behind_https_proxy(client):
    r = await client.post("/api/login", json={"password": "pw"},
                          headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["Set-Cookie"]


async def test_forged_cookie_rejected(client):
    client.session.cookie_jar.update_cookies({COOKIE: "e30=.deadbeef"})
    assert (await client.post("/api/write-test")).status == 401


async def test_login_rate_limit_per_ip(client):
    for _ in range(5):
        assert (await client.post("/api/login", json={"password": "x"},
                                  headers={"X-Forwarded-For": "1.2.3.4"})).status == 401
    r = await client.post("/api/login", json={"password": "pw"},
                          headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.status == 429
    r = await client.post("/api/login", json={"password": "pw"},
                          headers={"X-Forwarded-For": "5.6.7.8"})
    assert r.status == 200


async def test_settings_get_hides_secrets(client, cfg):
    cfg.mqtt_password = "brokersecret"
    cfg.mqtt_username = "user"
    text = await (await client.get("/api/settings")).text()
    assert "brokersecret" not in text and "pw" not in json.loads(text)["readonly"].values()
    d = json.loads(text)
    assert d["readonly"]["mqtt_password_set"] is True
    assert d["readonly"]["mqtt_username_set"] is True
    assert d["readonly"]["portal_password_set"] is True
    assert d["limits"]["poll_interval_live"]["min"] == 5
    assert set(d["editable"]) == {"poll_interval_live", "poll_interval_static", "log_level",
                                  "mqtt_retain", "mqtt_flat_topics", "history_days",
                                      "feed_in_tariff", "currency", "co2_factor",
                                      "milestone_step_kwh"}
    assert not any(k in d["readonly"] for k in ("mqtt_password", "portal_password",
                                                "mqtt_username"))


async def test_settings_put_validates_applies_and_persists(client, cfg):
    await login(client)
    r = await client.put("/api/settings", json={"poll_interval_live": 2, "foo": 1})
    assert r.status == 400
    body = await r.json()
    assert set(body["fields"]) == {"poll_interval_live", "foo"}
    assert cfg.poll_interval_live == 10
    r = await client.put("/api/settings", json={"poll_interval_live": 30, "mqtt_retain": False})
    assert r.status == 200 and cfg.poll_interval_live == 30 and cfg.mqtt_retain is False
    saved = json.loads((cfg.data_dir / "settings.json").read_text())
    assert saved["poll_interval_live"] == 30
    assert (await client.put("/api/settings", data="notjson")).status == 400


async def test_command_and_write_test(client, dev, rt):
    await login(client)
    r = await client.post("/api/command", json={"command": "power_limit_pct", "value": 60})
    body = await r.json()
    assert r.status == 200 and body["status"] == "ok" and body["value"] == 60
    assert dev.writes == [(40125, [600])] and rt.write_access == "verified"
    r = await client.post("/api/command", json={"command": "power_limit_pct", "value": 10})
    assert (await r.json())["reason"] == "rate_limited"
    r = await client.post("/api/command", json={"command": "reset", "value": 1})
    assert r.status == 400
    assert (await client.post("/api/command", json={"command": "power"})).status == 400


async def test_static_files_and_placeholder(cfg, dev, cache, rt, tmp_path):
    web = tmp_path / "web"
    (web / "i18n").mkdir(parents=True)
    (web / "index.html").write_text("<html>hi ä</html>", encoding="utf-8")
    (web / "app.js").write_text("x=1", encoding="utf-8")
    (web / "i18n" / "de.json").write_text('{"a":"ä"}', encoding="utf-8")
    c, _ = await make_client(cfg, dev, cache, rt, web=web)
    try:
        r = await c.get("/")
        assert r.status == 200 and "charset=utf-8" in r.headers["Content-Type"]
        assert "hi ä" in await r.text()
        assert (await c.get("/static/app.js")).status == 200
        assert (await c.get("/app.js")).status == 200
        assert (await c.get("/i18n/de.json")).status == 200
        assert (await c.get("/static/../../etc/passwd")).status == 404
        assert (await c.get("/nonexistent")).status == 404
    finally:
        await c.close()
    cfg.web_dir = tmp_path / "missing"
    c, _ = await make_client(cfg, dev, cache, rt, web=tmp_path / "missing")
    try:
        assert (await c.get("/")).status == 200
    finally:
        await c.close()
