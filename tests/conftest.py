"""Shared fakes: Modbus device and MQTT client are mocked, no network."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.cache import Cache
from bridge.config import Config
from bridge.modbus import DeviceCommError, ModbusExceptionResponse
from bridge.state import Persistence, Runtime


def str_words(text: str, n: int) -> list[int]:
    data = text.encode().ljust(n * 2, b"\x00")
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, n * 2, 2)]


def u32(v: int) -> list[int]:
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


def make_memory() -> dict[int, int]:
    mem: dict[int, int] = {}

    def put(addr: int, words: list[int]) -> None:
        for i, w in enumerate(words):
            mem[addr + i] = w
    put(30000, str_words("SUN2000-10KTL-M1", 15))
    put(30015, str_words("SN12345", 10))
    put(30025, str_words("PN-1", 10))
    put(30035, str_words("V100R001-02", 15))
    put(31025, str_words("V100R001C00B352", 15))
    put(30070, [429, 2, 2] + u32(10000) + u32(11000))
    put(32016, [3000, 500, 2500, 400])          # 300.0 V 5.00 A 250.0 V 4.00 A
    put(32064, u32(3500))
    put(32066, [4000, 4000, 4000, 2300, 2310, 2320])
    put(32072, u32(5000) + u32(5100) + u32(5200))
    put(32078, u32(3600))
    put(32080, u32(3400))
    put(32082, u32(0xFFFFFFFF - 99))           # -100
    put(32084, [950, 5000, 9857, 450, 0xFFFF, 0x0200, 0])
    put(32091, u32(1_700_000_000))
    put(32093, u32(1_700_040_000))
    put(32106, u32(2130789))
    put(32114, u32(1265))
    put(32000, [1])
    put(32002, [0])
    put(32003, u32(0))
    put(32008, [0, 0, 0])
    put(40125, [1000])
    put(40126, u32(11000))
    return mem


class FakeDevice:
    """Stands in for ModbusDevice."""

    def __init__(self) -> None:
        self.mem = make_memory()
        self.connected = False
        self.fail: Exception | None = None       # raised by connect/read/write when set
        self.write_fail: Exception | None = None
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, list[int]]] = []
        self.connects = 0
        self.connect_fails = 0

    async def connect(self) -> None:
        self.connects += 1
        if self.connect_fails:
            self.connect_fails -= 1
            raise DeviceCommError("connect_failed")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def read(self, address: int, count: int) -> list[int]:
        assert 1 <= count <= 64
        if self.fail:
            raise self.fail
        self.reads.append((address, count))
        return [self.mem.get(address + i, 0) for i in range(count)]

    async def write(self, address: int, values: list[int]) -> None:
        if self.write_fail:
            raise self.write_fail
        self.writes.append((address, list(values)))
        for i, v in enumerate(values):
            self.mem[address + i] = v


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.from_env({"HUAWEI_HOST": "h", "MQTT_HOST": "m", "DATA_DIR": str(tmp_path),
                            "HUAWEI_ALLOW_SHUTDOWN": "false"})


@pytest.fixture
def rt(cfg, clock) -> Runtime:
    return Runtime(cfg, Persistence(cfg.data_dir), clock)


@pytest.fixture
def cache(cfg) -> Cache:
    return Cache(context=lambda: {"allow_shutdown": cfg.allow_shutdown})


@pytest.fixture
def dev() -> FakeDevice:
    return FakeDevice()


class FakeMessage:
    def __init__(self, topic: str, payload: bytes, retain: bool = False) -> None:
        self.topic = type("T", (), {"value": topic})()
        self.payload = payload
        self.retain = retain


class FakeMqttClient:
    """Mimics aiomqtt.Client as async context manager."""

    instances: list["FakeMqttClient"] = []

    def __init__(self, will: tuple[str, str], fail: Exception | None = None) -> None:
        self.will = will
        self.fail = fail
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        FakeMqttClient.instances.append(self)

    async def __aenter__(self) -> "FakeMqttClient":
        if self.fail:
            raise self.fail
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def publish(self, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    def inject(self, topic: str, payload: bytes, retain: bool = False) -> None:
        self._inbox.put_nowait(FakeMessage(topic, payload, retain))

    @property
    def messages(self):
        async def gen():
            while True:
                yield await self._inbox.get()
        return gen()


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeMqttClient.instances.clear()
    yield


__all__ = ["FakeDevice", "FakeMqttClient", "ModbusExceptionResponse", "DeviceCommError"]
