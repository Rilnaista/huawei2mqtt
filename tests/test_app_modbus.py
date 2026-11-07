import asyncio
import json

import pytest

from bridge.app import App
from bridge.logbuffer import RingBufferHandler
from bridge.modbus import DeviceCommError, ModbusDevice, ModbusExceptionResponse

from .conftest import FakeDevice, FakeMqttClient
from .test_mqtt import wait_for


class Rsp:
    def __init__(self, registers=None, code=0):
        self.registers, self.exception_code = registers or [], code

    def isError(self):
        return bool(self.exception_code)


class FakeClient:
    connected = True

    def __init__(self):
        self.calls = []
        self.next = Rsp([1, 2, 3])
        self.hang = False

    async def connect(self):
        return True

    def close(self):
        self.connected = False

    async def read_holding_registers(self, address, *, count, device_id):
        self.calls.append(("r", address, count, device_id))
        if self.hang:
            await asyncio.sleep(10)
        return self.next

    async def write_registers(self, address, values, *, device_id):
        self.calls.append(("w", address, values, device_id))
        return self.next


async def test_device_read_write_timeout_exception():
    fc = FakeClient()
    d = ModbusDevice("h", 502, 7, 0.05, 0, connect_wait=0, client_factory=lambda: fc)
    await d.connect()
    assert await d.read(100, 3) == [1, 2, 3]
    await d.write(40125, [500])
    assert fc.calls == [("r", 100, 3, 7), ("w", 40125, [500], 7)]
    fc.next = Rsp(code=2)
    with pytest.raises(ModbusExceptionResponse) as e:
        await d.read(100, 3)
    assert e.value.code == 2
    fc.hang = True
    with pytest.raises(DeviceCommError):
        await d.read(100, 3)
    with pytest.raises(ValueError):
        await d.read(1, 65)
    await d.close()
    with pytest.raises(DeviceCommError):
        await d.read(1, 1)


async def test_request_pacing():
    fc = FakeClient()
    d = ModbusDevice("h", 502, 1, 1, 100, connect_wait=0, client_factory=lambda: fc)
    await d.connect()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await d.read(1, 1)
    await d.read(1, 1)
    assert loop.time() - t0 >= 0.18


async def test_end_to_end_poll_publish_set_and_shutdown(cfg, tmp_path):
    cfg.portal_listen_port = 18999
    cfg.portal_host = "127.0.0.1"
    cfg.poll_interval_live = 5
    dev = FakeDevice()
    app = App(cfg, RingBufferHandler(), device=dev,
              mqtt_factory=lambda c, r, will: FakeMqttClient(will))
    task = asyncio.create_task(app.run(install_signals=False))
    await wait_for(lambda: FakeMqttClient.instances and app.rt.inverter == "online"
                   and app.rt.mqtt_connected, 5)
    # first MQTT session used the "unknown" LWT, then reconnected with the serial
    await wait_for(lambda: any(c.will[0] == "huawei/SN12345/availability"
                               for c in FakeMqttClient.instances), 5)
    await wait_for(lambda: app.rt.mqtt_connected)
    c = FakeMqttClient.instances[-1]
    app.pub.publish_snapshot()
    await wait_for(lambda: any(t == "huawei/SN12345/grid" for t, *_ in c.published))
    grid = next(json.loads(p) for t, p, *_ in c.published if t == "huawei/SN12345/grid")
    assert grid["active_power_w"] == 3400
    # setter via MQTT -> device write -> result + control republish
    c.inject("huawei/SN12345/control/power_limit_pct/set", b"50")
    await wait_for(lambda: any(t == "huawei/SN12345/control/result" for t, *_ in c.published))
    res = next(json.loads(p) for t, p, *_ in c.published if t == "huawei/SN12345/control/result")
    assert res["status"] == "ok" and dev.writes == [(40125, [500])]
    await wait_for(lambda: any(t == "huawei/SN12345/control/power_limit_pct" and p == "50.0"
                               for t, p, *_ in c.published))
    app.request_stop()
    await asyncio.wait_for(task, 10)
    assert c.published[-1][:2] == ("huawei/SN12345/availability", "offline")
    assert not dev.connected
