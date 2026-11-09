"""Setters: validation, rate limit, whitelisted Modbus writes, write_access tracking.

Only the three setters of topic-spec.md exist. Nothing else is ever written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import registers as rg
from .cache import Cache, iso
from .config import Config
from .modbus import DeviceCommError, ModbusDevice, ModbusExceptionResponse
from .state import Runtime

log = logging.getLogger("bridge.commands")

COMMANDS = ("power_limit_pct", "power_limit_w", "power")
SET_SUFFIX = {f"control/{c}/set": c for c in COMMANDS}
MIN_WRITE_INTERVAL = 2.0

AfterWrite = Callable[[dict[str, Any]], Awaitable[None]]


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise Rejected("invalid_value")
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        s = value.strip().strip('"').strip()
        try:
            num = float(s)
        except ValueError:
            raise Rejected("invalid_value") from None
    else:
        raise Rejected("invalid_value")
    if not math.isfinite(num):
        raise Rejected("invalid_value")
    return num


class CommandExecutor:
    def __init__(self, cfg: Config, device: ModbusDevice, cache: Cache, rt: Runtime,
                 after_write: AfterWrite | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.cfg, self.device, self.cache, self.rt = cfg, device, cache, rt
        self.after_write = after_write   # re-read control + publish control/result
        self._clock = clock or rt.clock
        self._last_write: float | None = None

    # -- helpers -----------------------------------------------------------------
    def _result(self, command: str, value: Any, status: str, reason: str | None) -> dict[str, Any]:
        return {"command": command, "value": value, "status": status, "reason": reason,
                "ts": iso(datetime.now(timezone.utc))}

    def _rate_limit(self) -> None:
        now = self._clock()
        if self._last_write is not None and now - self._last_write < MIN_WRITE_INTERVAL:
            raise Rejected("rate_limited")
        self._last_write = now

    def _plan(self, command: str, value: Any) -> tuple[Any, int, list[int]]:
        """Validate -> (normalised value, register, words). Raises Rejected."""
        if command == "power_limit_pct":
            num = _number(value)
            if not 0 <= num <= 100:
                raise Rejected("out_of_range")
            return (int(num) if num == int(num) else num), rg.REG_POWER_LIMIT_PCT, \
                [int(round(num * 10))]
        if command == "power_limit_w":
            num = _number(value)
            if num != int(num):
                raise Rejected("invalid_value")
            maxw = self.cache.value("max_power_w")
            if maxw is None:
                raise Rejected("max_power_unknown")
            if not 0 <= num <= maxw:
                raise Rejected("out_of_range")
            w = int(num)
            return w, rg.REG_POWER_LIMIT_W, [(w >> 16) & 0xFFFF, w & 0xFFFF]
        if command == "power":
            text = str(value).strip().strip('"').strip().lower()
            if text == "on":
                return "on", rg.REG_STARTUP, [0]
            if text == "off":
                if not self.cfg.allow_shutdown:
                    raise Rejected("shutdown_not_allowed")
                return "off", rg.REG_SHUTDOWN, [0]
            raise Rejected("invalid_value")
        raise Rejected("unknown_command")

    def _classify(self, exc: Exception) -> tuple[str, bool]:
        """-> (reason, denied)"""
        if isinstance(exc, ModbusExceptionResponse):
            return str(exc), exc.code == 0x01
        return str(exc) or type(exc).__name__, False

    async def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        log.info("command %s value=%r -> %s%s", result["command"], result["value"],
                 result["status"], f" ({result['reason']})" if result["reason"] else "")
        if self.after_write:
            try:
                await self.after_write(result)
            except Exception:  # noqa: BLE001
                log.exception("post-command refresh failed")
        return result

    # -- public ------------------------------------------------------------------
    async def execute(self, command: str, value: Any) -> dict[str, Any]:
        if command not in COMMANDS:
            return await self._finish(self._result(command, value, "rejected", "unknown_command"))
        try:
            norm, reg, words = self._plan(command, value)
            self._rate_limit()
        except Rejected as rej:
            return await self._finish(self._result(command, value, "rejected", rej.reason))
        try:
            await self.device.write(reg, words)
        except (DeviceCommError, ModbusExceptionResponse) as exc:
            reason, denied = self._classify(exc)
            if isinstance(exc, DeviceCommError):
                await self.device.close()
                self.rt.record_dropout()
            if denied:
                self._set_access("denied")
            return await self._finish(self._result(command, norm, "error", reason))
        self._set_access("verified")
        return await self._finish(self._result(command, norm, "ok", None))

    async def write_test(self) -> dict[str, Any]:
        """Idempotent: read 40125 and write the same value back."""
        cmd = "write_test"
        try:
            self._rate_limit()
        except Rejected as rej:
            return self._result(cmd, None, "rejected", rej.reason)
        try:
            words = await self.device.read(rg.REG_POWER_LIMIT_PCT, 1)
            raw = words[0]
            if raw == 0xFFFF:
                return self._result(cmd, None, "error", "invalid_current_value")
            await self.device.write(rg.REG_POWER_LIMIT_PCT, [raw])
        except (DeviceCommError, ModbusExceptionResponse) as exc:
            reason, denied = self._classify(exc)
            if isinstance(exc, DeviceCommError):
                await self.device.close()
                self.rt.record_dropout()
            if denied:
                self._set_access("denied")
            return await self._finish_test(self._result(cmd, None, "error", reason))
        self._set_access("verified")
        return await self._finish_test(self._result(cmd, round(raw / 10, 1), "ok", None))

    async def _finish_test(self, result: dict[str, Any]) -> dict[str, Any]:
        log.info("write test -> %s%s", result["status"],
                 f" ({result['reason']})" if result["reason"] else "")
        return result

    def _set_access(self, value: str) -> None:
        if self.rt.set_write_access(value):
            log.info("write_access is now %s", value)
            if self.on_access_change:
                self.on_access_change()

    on_access_change: Callable[[], None] | None = None

    # -- MQTT entry --------------------------------------------------------------
    async def handle_mqtt(self, base: str, topic: str, payload: bytes, retain: bool) -> None:
        if retain:
            return
        suffix = topic[len(base) + 1:] if topic.startswith(base + "/") else ""
        command = SET_SUFFIX.get(suffix)
        if command is None:
            return
        text = payload.decode("utf-8", errors="replace").strip()
        await self.execute(command, text)
