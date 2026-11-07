import json
from datetime import datetime, timezone

from bridge import registers as rg
from bridge.mqtt import MqttService
from bridge.publish import Publisher, group_messages
from bridge.state import Persistence, Runtime

from .conftest import FakeDevice  # noqa: F401


async def fill(cache, dev, klass=None):
    for blk in rg.blocks_for(klass=klass):
        cache.update_block(blk, await dev.read(blk.start, blk.count))


async def test_group_payloads(cache, dev):
    await fill(cache, dev)
    pv = cache.group_payload("pv")
    assert list(pv) == ["pv1_voltage_v", "pv1_current_a", "pv2_voltage_v", "pv2_current_a",
                        "pv1_power_w", "pv2_power_w", "input_power_w", "ts"]
    assert pv["pv1_voltage_v"] == 300.0 and pv["pv1_power_w"] == 1500
    assert pv["pv2_power_w"] == 1000 and pv["input_power_w"] == 3500
    assert pv["ts"].endswith("Z")
    inv = cache.group_payload("inverter")
    assert inv["status"] == "on_grid" and inv["running"] is True
    assert inv["insulation_resistance_mohm"] is None
    assert inv["alarms"] == [] and inv["efficiency_pct"] == 98.6
    ctl = cache.group_payload("control")
    assert ctl == {**ctl, "power_limit_pct": 100.0, "power_limit_w": 11000,
                   "shutdown_allowed": False}
    assert cache.group_payload("device")["serial_number"] == "SN12345"


async def test_unread_group_is_none(cache):
    assert cache.group_payload("pv") is None


async def test_registers_list(cache, dev):
    await fill(cache, dev, rg.LIVE)
    row = next(r for r in cache.registers() if r["address"] == 32016)
    assert row == {"address": 32016, "name": "pv1_voltage_v", "raw": 3000, "value": 300.0,
                   "unit": "V", "group": "pv"}


def test_group_messages_flat_skips_null():
    msgs = dict(group_messages("huawei/X", "inverter",
                               {"status": "on_grid", "running": True, "alarms": [],
                                "insulation_resistance_mohm": None, "ts": "t"}, True))
    assert json.loads(msgs["huawei/X/inverter"])["insulation_resistance_mohm"] is None
    assert msgs["huawei/X/inverter/status"] == "on_grid"
    assert msgs["huawei/X/inverter/running"] == "true"
    assert msgs["huawei/X/inverter/alarms"] == "[]"
    assert "huawei/X/inverter/insulation_resistance_mohm" not in msgs
    assert "huawei/X/inverter/ts" not in msgs
    assert len(group_messages("b", "pv", {"a": 1, "ts": "t"}, False)) == 1


class Sink(MqttService):
    def __init__(self, cfg, rt):
        super().__init__(cfg, rt, None, lambda b: [])
        self.sent = []

    def publish(self, topic, payload, retain, qos=None):
        self.sent.append((topic, payload, retain))


async def test_publisher_topics_retain_and_flat(cfg, rt, cache, dev):
    await fill(cache, dev)
    rt.set_serial("SN12345")
    sink = Sink(cfg, rt)
    pub = Publisher(cfg, cache, rt, sink)
    pub.publish_class(rg.LIVE)
    topics = {t: (p, r) for t, p, r in sink.sent}
    assert topics["huawei/SN12345/pv"][1] is True
    assert json.loads(topics["huawei/SN12345/grid"][0])["active_power_w"] == 3400
    assert topics["huawei/SN12345/grid/frequency_hz"][0] == "50.0"
    assert "huawei/SN12345/device" not in topics
    cfg.mqtt_flat_topics = False
    cfg.mqtt_retain = False
    sink.sent.clear()
    pub.publish_class(rg.STATIC)
    assert {t for t, _, _ in sink.sent} == {"huawei/SN12345/device", "huawei/SN12345/control"}
    assert all(r is False for _, _, r in sink.sent)


async def test_nothing_published_without_known_serial(cfg, cache, dev, tmp_path):
    await fill(cache, dev)
    rt2 = Runtime(cfg, Persistence(tmp_path / "other"))
    sink = Sink(cfg, rt2)
    pub = Publisher(cfg, cache, rt2, sink)
    pub.publish_class(rg.LIVE)
    pub.publish_bridge()
    assert sink.sent == []
    assert rt2.availability_topic == "huawei/unknown/availability"


async def test_bridge_publish_and_device_id_override(cfg, rt, cache):
    cfg.huawei_device_id = "my inv/1"
    sink = Sink(cfg, rt)
    Publisher(cfg, cache, rt, sink).publish_bridge()
    d = {t: p for t, p, _ in sink.sent}
    assert json.loads(d["huawei/my_inv_1/bridge"])["dongle"] == "unreachable"
    assert d["huawei/my_inv_1/bridge/inverter"] == "offline"
    assert d["huawei/my_inv_1/bridge/write_access"] == "unverified"
    assert "huawei/my_inv_1/bridge/last_success" not in d


def test_cache_keeps_value_when_not_updated(cache):
    blk = rg.blocks_for(group="control")[0]
    cache.update_block(blk, [500, 0, 3000])
    assert cache.value("power_limit_pct") == 50.0
    assert cache.group_ts("control") <= datetime.now(timezone.utc)
