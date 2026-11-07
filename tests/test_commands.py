import json

import pytest

from bridge import registers as rg
from bridge.commands import CommandExecutor
from bridge.modbus import DeviceCommError, ModbusExceptionResponse
from bridge.state import Persistence, Runtime

from .test_cache_publish import fill


@pytest.fixture
async def exe(cfg, dev, cache, rt, clock):
    await fill(cache, dev)
    results = []

    async def after(res):
        results.append(res)
    e = CommandExecutor(cfg, dev, cache, rt, after_write=after)
    e.results = results
    return e


async def test_power_limit_pct_ok_and_scaling(exe, dev, clock):
    r = await exe.execute("power_limit_pct", "50")
    assert r["status"] == "ok" and r["value"] == 50 and r["command"] == "power_limit_pct"
    assert dev.writes == [(40125, [500])]
    clock.t += 3
    r = await exe.execute("power_limit_pct", "33.34")
    assert dev.writes[-1] == (40125, [333])
    assert exe.results[-1] is r


@pytest.mark.parametrize("val", ["-1", "100.1", "abc", "nan", "inf", "", None, True])
async def test_power_limit_pct_rejected(exe, dev, val):
    r = await exe.execute("power_limit_pct", val)
    assert r["status"] == "rejected" and dev.writes == []


async def test_power_limit_w(exe, dev, clock):
    r = await exe.execute("power_limit_w", "5000")
    assert r["status"] == "ok"
    assert dev.writes == [(40126, [0, 5000])]
    clock.t += 3
    assert (await exe.execute("power_limit_w", "11001"))["reason"] == "out_of_range"
    assert (await exe.execute("power_limit_w", "-5"))["reason"] == "out_of_range"
    assert (await exe.execute("power_limit_w", "10.5"))["reason"] == "invalid_value"
    assert len(dev.writes) == 1


async def test_power_on_off_and_shutdown_lock(exe, dev, cfg, clock):
    r = await exe.execute("power", "off")
    assert r["status"] == "rejected" and r["reason"] == "shutdown_not_allowed"
    assert dev.writes == []
    r = await exe.execute("power", "on")
    assert r["status"] == "ok" and dev.writes == [(40200, [0])]
    cfg.allow_shutdown = True
    clock.t += 3
    r = await exe.execute("power", "OFF")
    assert r["status"] == "ok" and dev.writes[-1] == (40201, [0])
    clock.t += 3
    assert (await exe.execute("power", "restart"))["status"] == "rejected"


async def test_rate_limit(exe, dev, clock):
    assert (await exe.execute("power_limit_pct", "10"))["status"] == "ok"
    clock.t += 1.9
    r = await exe.execute("power_limit_pct", "20")
    assert r["status"] == "rejected" and r["reason"] == "rate_limited"
    clock.t += 0.2
    assert (await exe.execute("power_limit_pct", "20"))["status"] == "ok"
    assert len(dev.writes) == 2


async def test_unknown_command_never_writes(exe, dev):
    for cmd in ("reset", "47415", "firmware", "power_limit"):
        assert (await exe.execute(cmd, 1))["status"] == "rejected"
    assert dev.writes == []


async def test_write_access_verified_and_persisted(exe, dev, cfg, rt):
    assert rt.write_access == "unverified"
    await exe.execute("power_limit_pct", "100")
    assert rt.write_access == "verified"
    assert Persistence(cfg.data_dir).write_access == "verified"
    assert json.loads((cfg.data_dir / "state.json").read_text())["write_access"] == "verified"


async def test_write_denied_illegal_function(exe, dev, rt):
    dev.write_fail = ModbusExceptionResponse(1)
    r = await exe.execute("power_limit_pct", "50")
    assert r["status"] == "error" and r["reason"] == "modbus_exception_0x01"
    assert rt.write_access == "denied"


async def test_write_error_other_exception_and_timeout(exe, dev, rt, clock):
    dev.write_fail = ModbusExceptionResponse(3)
    assert (await exe.execute("power_limit_pct", "50"))["status"] == "error"
    assert rt.write_access == "unverified"
    clock.t += 3
    dev.write_fail = DeviceCommError("timeout")
    r = await exe.execute("power_limit_pct", "50")
    assert r["status"] == "error" and r["reason"] == "timeout"
    assert rt.write_access == "unverified"


async def test_write_test_is_idempotent(exe, dev, rt):
    dev.mem[40125] = 777
    r = await exe.write_test()
    assert r["status"] == "ok" and dev.writes == [(40125, [777])]
    assert rt.write_access == "verified"


async def test_write_test_denied(exe, dev, rt):
    dev.write_fail = ModbusExceptionResponse(1)
    assert (await exe.write_test())["status"] == "error"
    assert rt.write_access == "denied"


async def test_mqtt_entry_ignores_retained_and_unknown(exe, dev, clock):
    base = "huawei/SN"
    await exe.handle_mqtt(base, f"{base}/control/power_limit_pct/set", b"40", True)
    await exe.handle_mqtt(base, f"{base}/control/reset/set", b"1", False)
    await exe.handle_mqtt(base, f"{base}/control/power_limit_pct", b"40", False)
    assert dev.writes == []
    await exe.handle_mqtt(base, f"{base}/control/power_limit_pct/set", b" 40.5 \n", False)
    assert dev.writes == [(40125, [405])]
    assert exe.results[-1]["value"] == 40.5


async def test_shutdown_never_wired_to_other_registers(exe, dev, cfg, clock):
    cfg.allow_shutdown = True
    for cmd, val in [("power_limit_pct", "1"), ("power_limit_w", "1"), ("power", "on"),
                     ("power", "off")]:
        clock.t += 3
        await exe.execute(cmd, val)
    assert {a for a, _ in dev.writes} <= {rg.REG_POWER_LIMIT_PCT, rg.REG_POWER_LIMIT_W,
                                          rg.REG_STARTUP, rg.REG_SHUTDOWN}
