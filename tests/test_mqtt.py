import asyncio

from bridge.mqtt import MqttService
from bridge.state import Backoff

from .conftest import FakeMqttClient


def factory(fail_first=0):
    state = {"n": 0}

    def make(cfg, rt, will):
        state["n"] += 1
        fail = OSError("refused") if state["n"] <= fail_first else None
        return FakeMqttClient(will, fail)
    return make


async def wait_for(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timeout")


async def test_lwt_online_subscribe_publish_and_clean_shutdown(cfg, rt):
    rt.set_serial("SN1")
    got = []

    async def on_msg(topic, payload, retain):
        got.append((topic, payload, retain))
    svc = MqttService(cfg, rt, on_msg, lambda b: [f"{b}/control/power/set"], None, factory())
    task = asyncio.create_task(svc.run())
    await wait_for(lambda: FakeMqttClient.instances and rt.mqtt_connected)
    c = FakeMqttClient.instances[0]
    assert c.will == ("huawei/SN1/availability", "offline")
    assert ("huawei/SN1/availability", "online", 1, True) in c.published
    assert c.subscribed == [("huawei/SN1/control/power/set", 1)]
    svc.publish("huawei/SN1/pv", "{}", True)
    await wait_for(lambda: ("huawei/SN1/pv", "{}", 0, True) in c.published)
    # retained set messages are ignored, live ones delivered
    c.inject("huawei/SN1/control/power/set", b"off", retain=True)
    c.inject("huawei/SN1/control/power/set", b"on")
    await wait_for(lambda: got)
    assert got == [("huawei/SN1/control/power/set", b"on", False)]
    svc.stop()
    await asyncio.wait_for(task, 3)
    assert c.published[-1] == ("huawei/SN1/availability", "offline", 1, True)
    assert not rt.mqtt_connected


async def test_broker_down_backoff_and_no_crash(cfg, rt):
    delays = []
    b = Backoff()
    real_next = b.next
    b.next = lambda: (delays.append(real_next()), 0.01)[1]
    svc = MqttService(cfg, rt, None, lambda b: [], None, factory(fail_first=3), backoff=b)
    task = asyncio.create_task(svc.run())
    await wait_for(lambda: rt.mqtt_connected)
    assert len(delays) == 3 and delays == [2, 4, 8]
    svc.stop()
    await asyncio.wait_for(task, 3)


async def test_publish_dropped_while_disconnected(cfg, rt):
    svc = MqttService(cfg, rt, None, lambda b: [], None, factory())
    svc.publish("t", "p", True)
    assert svc._queue.empty()


async def test_serial_learned_reconnects_with_new_lwt(cfg, rt):
    svc = MqttService(cfg, rt, None, lambda b: [], None, factory())
    rt.on_serial_change = svc.request_reconnect
    task = asyncio.create_task(svc.run())
    await wait_for(lambda: rt.mqtt_connected)
    first = FakeMqttClient.instances[0]
    assert first.will[0] == "huawei/unknown/availability"
    rt.set_serial("SN9")
    await wait_for(lambda: len(FakeMqttClient.instances) == 2 and rt.mqtt_connected)
    assert ("huawei/unknown/availability", "", 1, True) in first.published
    assert FakeMqttClient.instances[1].will[0] == "huawei/SN9/availability"
    svc.stop()
    await asyncio.wait_for(task, 3)


async def test_old_unknown_availability_cleared_after_serial_learned(cfg, rt):
    svc = MqttService(cfg, rt, None, lambda b: [], None, factory())
    task = asyncio.create_task(svc.run())
    await wait_for(lambda: FakeMqttClient.instances and rt.mqtt_connected)
    c = FakeMqttClient.instances[0]
    assert c.will[0] == "huawei/unknown/availability"
    rt.set_serial("SN9")
    svc.request_reconnect()
    await wait_for(lambda: len(FakeMqttClient.instances) == 2 and rt.mqtt_connected)
    assert ("huawei/unknown/availability", "", 1, True) in c.published
    assert ("huawei/unknown/availability", "offline", 1, True) not in c.published
    c2 = FakeMqttClient.instances[1]
    assert c2.will == ("huawei/SN9/availability", "offline")
    assert ("huawei/SN9/availability", "online", 1, True) in c2.published
    svc.stop()
    await asyncio.wait_for(task, 3)
