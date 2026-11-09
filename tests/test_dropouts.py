"""REQ-027: one immediate retry, dropout counting, quiet logs."""

import asyncio
import logging

import pytest

from bridge import registers as rg
from bridge.modbus import DeviceCommError, ModbusDevice, ModbusExceptionResponse, describe
from bridge.poller import Poller

from .conftest import FakeDevice
from .test_app_modbus import Rsp


class ScriptClient:
    connected = True

    def __init__(self, script):
        self.script = list(script)    # items: "hang" | Rsp | None
        self.calls = 0

    async def connect(self):
        return True

    def close(self):
        pass

    async def read_holding_registers(self, address, *, count, device_id):
        self.calls += 1
        item = self.script.pop(0)
        if item == "hang":
            await asyncio.sleep(10)
        return item

    async def write_registers(self, address, values, *, device_id):
        self.calls += 1
        item = self.script.pop(0)
        if item == "hang":
            await asyncio.sleep(10)
        return item


async def device(script):
    fc = ScriptClient(script)
    d = ModbusDevice("h", 502, 1, 0.05, 0, connect_wait=0, client_factory=lambda: fc)
    await d.connect()
    return d, fc


async def test_single_retry_succeeds():
    d, fc = await device(["hang", Rsp([5])])
    assert await d.read(1, 1) == [5]
    assert fc.calls == 2


async def test_no_answer_retried_once_then_fails():
    d, fc = await device([None, None, Rsp([1])])
    with pytest.raises(DeviceCommError) as e:
        await d.read(1, 1)
    assert str(e.value) == "no_response" and fc.calls == 2         # exactly one retry


async def test_two_timeouts_fail_after_exactly_two_attempts():
    d, fc = await device(["hang", "hang", Rsp([1])])
    with pytest.raises(DeviceCommError) as e:
        await d.read(1, 1)
    assert str(e.value) == "timeout" and fc.calls == 2


async def test_exception_response_not_retried():
    d, fc = await device([Rsp(code=6), Rsp([1])])
    with pytest.raises(ModbusExceptionResponse):
        await d.read(1, 1)
    assert fc.calls == 1


async def test_writes_are_not_retried():
    d, fc = await device(["hang", Rsp([1])])
    with pytest.raises(DeviceCommError):
        await d.write(40125, [500])
    assert fc.calls == 1


def test_describe_no_hex():
    assert describe(DeviceCommError("timeout")) == "inverter did not answer (timeout)"
    assert "0x" not in describe(DeviceCommError("ConnectionException"))


async def mk(cfg, dev, cache, rt):
    return Poller(cfg, dev, cache, rt, None)


async def test_dropout_counted_on_reconnect_not_on_connect_failures(cfg, dev, cache, rt, clock, caplog):
    p = await mk(cfg, dev, cache, rt)
    await p.step()
    assert rt.dropouts_24h == 0 and rt.last_dropout is None
    dev.fail = DeviceCommError("timeout")
    clock.t += 11
    with caplog.at_level(logging.INFO):
        await p.step()
    assert rt.dropouts_24h == 1 and rt.last_dropout is not None
    assert any("inverter did not answer (timeout), reconnecting" in r.getMessage()
               for r in caplog.records)
    dev.fail = None
    dev.connect_fails = 3
    for _ in range(3):
        await p.step()
    assert rt.dropouts_24h == 1                       # failed connects are not new dropouts
    assert rt.bridge_payload()["dropouts_24h"] == 1


async def test_busy_exception_is_no_dropout_no_reconnect(cfg, dev, cache, rt, clock, caplog):
    p = await mk(cfg, dev, cache, rt)
    dev.fail = ModbusExceptionResponse(0x06)
    with caplog.at_level(logging.DEBUG):
        await p.step()
    assert dev.connected and dev.connects == 1 and rt.dropouts_24h == 0
    recs = [r for r in caplog.records if "rejected read" in r.getMessage()]
    assert recs and all(r.levelno <= logging.INFO for r in recs)


async def test_dropouts_expire_after_24h(rt, clock):
    rt.record_dropout()
    clock.t += 3600
    rt.record_dropout()
    assert rt.dropouts_24h == 2
    clock.t += 86400 - 3600 + 1
    assert rt.dropouts_24h == 1
    clock.t += 3600
    assert rt.dropouts_24h == 0


async def test_next_live_schedule(cfg, dev, cache, rt, clock):
    p = await mk(cfg, dev, cache, rt)
    await p.step()
    assert rt.last_live_at is not None and rt.next_live_at is not None
    assert 8 < (rt.next_live_at - rt.last_live_at).total_seconds() <= 10.5
    dev.connect_fails = 1
    await p.device.close()
    await p.step()                                    # connect fails: next attempt = backoff
    assert 0 < (rt.next_live_at - rt.last_live_at).total_seconds() <= 3


def test_pymodbus_logger_silenced():
    from bridge.logbuffer import setup_logging
    import io
    setup_logging("INFO", lambda: [], io.StringIO())
    assert logging.getLogger("pymodbus").level == logging.CRITICAL
