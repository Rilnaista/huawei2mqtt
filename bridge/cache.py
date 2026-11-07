"""In-memory cache of decoded register values (last known values are kept on failure)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

from . import registers as rg


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Entry:
    reg: rg.Reg
    raw: Any
    value: Any
    ts: datetime


class Cache:
    def __init__(self, context: Callable[[], dict[str, Any]] | None = None,
                 now: Callable[[], datetime] | None = None, tz_name: str = "Europe/Berlin") -> None:
        self._tz = rg.local_tz(tz_name)
        self._entries: dict[int, Entry] = {}
        self._context = context or (lambda: {})
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _epoch_tz(self) -> tzinfo | None:
        if self._tz is not None:
            return self._tz
        e = self._entries.get(rg.BY_FIELD["timezone_offset_min"].address)  # fallback: device offset
        if e is not None and e.value is not None:
            return timezone(timedelta(minutes=e.value))
        return None

    def update_block(self, block: rg.Block, words: list[int]) -> None:
        ts = self._now()
        for reg in block.regs:
            off = reg.address - block.start
            raw, value = rg.decode(reg, words[off: off + reg.words], self._epoch_tz())
            self._entries[reg.address] = Entry(reg, raw, value, ts)

    def value(self, field: str) -> Any:
        e = self._entries.get(rg.BY_FIELD[field].address)
        return None if e is None else e.value

    def has_data(self, group: str) -> bool:
        return any(e.reg.group == group for e in self._entries.values())

    def group_ts(self, group: str) -> datetime | None:
        times = [e.ts for e in self._entries.values() if e.reg.group == group]
        return max(times) if times else None

    def group_payload(self, group: str) -> dict[str, Any] | None:
        """Group JSON object incl. ``ts``; None if nothing was read yet."""
        if not self.has_data(group):
            return None
        vals = {e.reg.field: e.value for e in self._entries.values() if e.reg.group == group}
        # derived fields may need values of the same group only (alarm_*, status_code, pv*)
        ctx = self._context()
        out: dict[str, Any] = {}
        for name in rg.GROUP_FIELDS[group]:
            if name in rg.DERIVED:
                out[name] = rg.DERIVED[name](vals, ctx)
            else:
                out[name] = vals.get(name)
        out["ts"] = iso(self.group_ts(group))  # type: ignore[arg-type]
        return out

    def registers(self) -> list[dict[str, Any]]:
        return [
            {"address": e.reg.address, "name": e.reg.field, "raw": e.raw, "value": e.value,
             "unit": e.reg.unit, "group": e.reg.group}
            for e in sorted(self._entries.values(), key=lambda x: x.reg.address)
            if e.reg.group != "internal"  # helper registers (e.g. 43006) are not part of the contract
        ]
