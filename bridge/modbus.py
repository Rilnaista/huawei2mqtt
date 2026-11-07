"""Single Modbus TCP connection; every access (poll + setter) is serialised by one lock."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("bridge.modbus")


class DeviceCommError(Exception):
    """Timeout / connection problem: the connection is considered broken."""


class ModbusExceptionResponse(Exception):
    """The device answered with a Modbus exception (connection is fine)."""

    def __init__(self, code: int):
        super().__init__(f"modbus_exception_0x{code:02X}")
        self.code = code


class ModbusDevice:
    def __init__(self, host: str, port: int, unit_id: int, timeout: float,
                 request_delay_ms: int, connect_wait: float = 1.0,
                 client_factory: Callable[[], Any] | None = None) -> None:
        self.host, self.port, self.unit_id = host, port, unit_id
        self.timeout = timeout
        self.delay = request_delay_ms / 1000.0
        self.connect_wait = connect_wait
        self._factory = client_factory or self._default_factory
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._last_end = 0.0

    def _default_factory(self) -> Any:
        from pymodbus.client import AsyncModbusTcpClient
        # retries=0 and reconnect_delay=0: all retry/backoff logic lives in the poller.
        return AsyncModbusTcpClient(self.host, port=self.port, timeout=self.timeout,
                                    retries=0, reconnect_delay=0)

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "connected", False))

    async def connect(self) -> None:
        async with self._lock:
            await self._close_locked()
            client = self._factory()
            try:
                ok = await asyncio.wait_for(client.connect(), self.timeout + 1)
            except Exception as exc:  # noqa: BLE001 (OSError, timeout, ModbusException)
                self._safe_close(client)
                raise DeviceCommError(f"connect_failed: {type(exc).__name__}") from exc
            if not ok or not getattr(client, "connected", True):
                self._safe_close(client)
                raise DeviceCommError("connect_failed")
            self._client = client
            # Huawei quirk: first answers right after connect can be garbage.
            await asyncio.sleep(self.connect_wait)
            self._last_end = time.monotonic()

    @staticmethod
    def _safe_close(client: Any) -> None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    async def _close_locked(self) -> None:
        if self._client is not None:
            self._safe_close(self._client)
            self._client = None

    async def close(self) -> None:
        # Not taking the lock: closing must also work while a request hangs.
        await self._close_locked()

    async def _pace(self) -> None:
        wait = self._last_end + self.delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            client = self._client
            if client is None:
                raise DeviceCommError("not_connected")
            await self._pace()
            try:
                rsp = await asyncio.wait_for(
                    getattr(client, method)(*args, device_id=self.unit_id, **kwargs), self.timeout + 1)
            except asyncio.TimeoutError as exc:
                raise DeviceCommError("timeout") from exc
            except Exception as exc:  # noqa: BLE001 (ModbusException, OSError, ...)
                raise DeviceCommError(f"{type(exc).__name__}") from exc
            finally:
                self._last_end = time.monotonic()
            if rsp is None:
                raise DeviceCommError("no_response")
            if rsp.isError():
                code = getattr(rsp, "exception_code", None)
                if code:
                    raise ModbusExceptionResponse(int(code))
                raise DeviceCommError("error_response")
            return rsp

    async def read(self, address: int, count: int) -> list[int]:
        if not 1 <= count <= 64:
            raise ValueError("count must be 1..64")
        rsp = await self._call("read_holding_registers", address, count=count)
        regs = list(rsp.registers)
        if len(regs) < count:
            raise DeviceCommError("short_response")
        return regs[:count]

    async def write(self, address: int, values: list[int]) -> None:
        """Write holding registers (FC 0x10). Only called by the whitelisted setters."""
        await self._call("write_registers", address, values)
