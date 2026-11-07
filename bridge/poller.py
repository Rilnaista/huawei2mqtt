"""Modbus poller: block reads, LIVE/STATIC schedule, backoff, stale handling."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from . import registers as rg
from .cache import Cache
from .config import Config
from .modbus import DeviceCommError, ModbusDevice, ModbusExceptionResponse
from .state import Backoff, Runtime

log = logging.getLogger("bridge.poller")

OnCycle = Callable[[str, bool], Awaitable[None]]


class Poller:
    def __init__(self, cfg: Config, device: ModbusDevice, cache: Cache, rt: Runtime,
                 on_cycle: OnCycle | None = None, backoff: Backoff | None = None) -> None:
        self.cfg, self.device, self.cache, self.rt = cfg, device, cache, rt
        self.on_cycle = on_cycle
        self.backoff = backoff or Backoff()
        self._last: dict[str, float | None] = {rg.STATIC: None, rg.LIVE: None}  # last cycle (monotonic)
        self._blocks = {rg.STATIC: rg.blocks_for(klass=rg.STATIC),
                        rg.LIVE: rg.blocks_for(klass=rg.LIVE)}
        self._exc_logged: set[int] = set()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def interval(self, klass: str) -> float:
        return float(self.cfg.poll_interval_live if klass == rg.LIVE
                     else self.cfg.poll_interval_static)

    # -- reading ---------------------------------------------------------------
    async def read_blocks(self, blocks: list[rg.Block]) -> int:
        """Read blocks; returns number of successful blocks. Raises DeviceCommError."""
        ok = 0
        for blk in blocks:
            try:
                words = await self.device.read(blk.start, blk.count)
            except ModbusExceptionResponse as exc:
                if blk.start not in self._exc_logged:
                    self._exc_logged.add(blk.start)
                    log.warning("device rejected read %d..%d: %s", blk.start,
                                blk.start + blk.count - 1, exc)
                continue
            self._exc_logged.discard(blk.start)
            self.cache.update_block(blk, words)
            ok += 1
        return ok

    async def refresh_group(self, group: str) -> None:
        """Immediate re-read of one group (control after a setter)."""
        await self.read_blocks(rg.blocks_for(group=group))

    async def run_cycle(self, klass: str) -> bool:
        ok = await self.read_blocks(self._blocks[klass]) > 0
        if klass == rg.LIVE:
            if ok:
                self.rt.live_ok()
            else:
                self.rt.live_failed("device_rejected_requests")
        if self.on_cycle:
            await self.on_cycle(klass, ok)
        return ok

    # -- scheduling ------------------------------------------------------------
    async def step(self) -> float:
        """One scheduler iteration; returns seconds to wait before the next one."""
        clock = self.rt.clock
        if not self.device.connected:
            try:
                await self.device.connect()
            except DeviceCommError as exc:
                return await self._comm_failed(exc, close=False)
            self.rt.dongle_reachable = True
            log.info("connected to %s:%s", self.cfg.huawei_host, self.cfg.huawei_port)
        def due_in(k: str) -> float:   # interval is read live: settings changes apply at once
            last = self._last[k]
            return 0.0 if last is None else last + self.interval(k) - clock()
        due = [k for k in (rg.STATIC, rg.LIVE) if due_in(k) <= 0]
        for klass in due:
            try:
                await self.run_cycle(klass)
            except DeviceCommError as exc:
                return await self._comm_failed(exc)
            self._last[klass] = clock()
            self.backoff.reset()
        return max(0.05, min(due_in(k) for k in self._last))

    async def _comm_failed(self, exc: Exception, close: bool = True) -> float:
        if close:
            await self.device.close()
        else:
            self.rt.dongle_reachable = False
        delay = self.backoff.next()
        self.rt.live_failed(str(exc))
        log.warning("device communication failed (%s); retry in %.0f s", exc, delay)
        if self.on_cycle:
            await self.on_cycle(rg.LIVE, False)
        return delay

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the process must never die because of the device
                log.exception("unexpected poller error")
                delay = self.backoff.next()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), min(delay, 60))
            except asyncio.TimeoutError:
                pass
        await self.device.close()
