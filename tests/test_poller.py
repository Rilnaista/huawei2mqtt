import pytest

from bridge import registers as rg
from bridge.modbus import DeviceCommError, ModbusExceptionResponse
from bridge.poller import Poller
from bridge.state import Backoff, inverter_state


def test_backoff_sequence():
    b = Backoff()
    assert [b.next() for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    b.reset()
    assert b.next() == 2


def test_inverter_state_logic():
    assert inverter_state(None, False, 100, 10) == "offline"
    assert inverter_state(100, True, 105, 10) == "online"
    assert inverter_state(100, False, 120, 10) == "stale"      # < 3x LIVE
    assert inverter_state(100, False, 130, 10) == "offline"    # >= 3x LIVE


async def make(cfg, dev, cache, rt):
    cycles = []

    async def on_cycle(klass, ok):
        cycles.append((klass, ok))
    return Poller(cfg, dev, cache, rt, on_cycle), cycles


async def test_first_step_reads_static_then_live_in_blocks(cfg, dev, cache, rt):
    p, cycles = await make(cfg, dev, cache, rt)
    delay = await p.step()
    assert cycles == [(rg.STATIC, True), (rg.LIVE, True)]
    assert all(c <= 64 for _, c in dev.reads)
    assert cache.value("serial_number") == "SN12345"
    assert rt.inverter == "online" and rt.dongle == "reachable"
    assert 0.05 <= delay <= 10
    n = len(dev.reads)
    await p.step()                       # nothing due yet
    assert len(dev.reads) == n


async def test_live_rescheduled_by_interval(cfg, dev, cache, rt, clock):
    p, cycles = await make(cfg, dev, cache, rt)
    await p.step()
    cycles.clear()
    clock.t += cfg.poll_interval_live + 1
    await p.step()
    assert cycles == [(rg.LIVE, True)]
    cfg.poll_interval_live = 20            # takes effect immediately
    clock.t += 15
    await p.step()
    assert cycles == [(rg.LIVE, True)]
    clock.t += 10
    await p.step()
    assert len(cycles) == 2


async def test_failure_keeps_cache_goes_stale_then_offline_with_backoff(cfg, dev, cache, rt, clock):
    p, cycles = await make(cfg, dev, cache, rt)
    await p.step()
    value = cache.value("active_power_w")
    assert value == 3400
    dev.mem[32080] = 0
    dev.fail = DeviceCommError("timeout")
    clock.t += cfg.poll_interval_live + 1
    delays = []
    delays.append(await p.step())
    assert rt.inverter == "stale" and rt.last_error == "timeout"
    assert cache.value("active_power_w") == 3400           # last value held
    assert (rg.LIVE, False) in cycles
    assert not dev.connected                               # connection dropped
    dev.fail = None
    dev.connect_fails = 10
    for _ in range(6):
        delays.append(await p.step())
    assert delays == [2, 4, 8, 16, 32, 60, 60]
    assert rt.dongle == "unreachable"
    clock.t += 3 * cfg.poll_interval_live
    assert rt.inverter == "offline"


async def test_recovery_resets_backoff(cfg, dev, cache, rt, clock):
    p, _ = await make(cfg, dev, cache, rt)
    await p.step()
    dev.fail = DeviceCommError("timeout")
    clock.t += 11
    await p.step()
    await p.step()
    dev.fail = None
    await p.step()                       # reconnects and polls
    assert rt.inverter == "online"
    assert p.backoff.next() == 2


async def test_modbus_exception_is_not_a_connection_failure(cfg, dev, cache, rt):
    p, cycles = await make(cfg, dev, cache, rt)
    dev.fail = ModbusExceptionResponse(2)
    await p.step()
    assert dev.connected
    assert (rg.LIVE, False) in cycles and rt.inverter == "offline"


async def test_one_connection_only(cfg, dev, cache, rt, clock):
    p, _ = await make(cfg, dev, cache, rt)
    for _ in range(3):
        clock.t += 400
        await p.step()
    assert dev.connects == 1
