"""Declarative register map. A new register is a new ``R(...)`` row in REGISTERS.

Group payload order is given by GROUP_FIELDS (fields are register fields or DERIVED).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

from . import codes

LIVE = "live"
STATIC = "static"

MAX_BLOCK_WORDS = 64   # Modbus limit we stay well below (<= 64 registers/request)
MAX_BLOCK_GAP = 16     # unread words we tolerate inside one block

# Invalid markers per type (raw value -> "no value").
_INVALID = {"U16": 0xFFFF, "I16": 0x7FFF, "U32": 0xFFFFFFFF, "I32": 0x7FFFFFFF}
_WORDS = {"U16": 1, "I16": 1, "U32": 2, "I32": 2, "EPOCH": 2}


@dataclass(frozen=True)
class Reg:
    address: int
    field: str
    group: str
    typ: str                 # U16 I16 U32 I32 STR EPOCH
    klass: str = LIVE
    gain: int = 1
    unit: str = ""
    decimals: int = 0
    length: int = 0          # STR: number of words
    invalid_marker: bool = True   # False for bitfields (all bits may legitimately be set)

    @property
    def words(self) -> int:
        return self.length if self.typ == "STR" else _WORDS[self.typ]

    @property
    def end(self) -> int:
        return self.address + self.words


def R(address: int, field: str, group: str, typ: str, klass: str = LIVE, gain: int = 1,
      unit: str = "", decimals: int = 0, length: int = 0, bits: bool = False) -> Reg:
    return Reg(address, field, group, typ, klass, gain, unit, decimals, length, not bits)


REGISTERS: list[Reg] = [
    # pv
    R(32016, "pv1_voltage_v", "pv", "I16", gain=10, unit="V", decimals=1),
    R(32017, "pv1_current_a", "pv", "I16", gain=100, unit="A", decimals=2),
    R(32018, "pv2_voltage_v", "pv", "I16", gain=10, unit="V", decimals=1),
    R(32019, "pv2_current_a", "pv", "I16", gain=100, unit="A", decimals=2),
    R(32064, "input_power_w", "pv", "I32", unit="W"),
    # grid
    R(32066, "voltage_l1_l2_v", "grid", "U16", gain=10, unit="V", decimals=1),
    R(32067, "voltage_l2_l3_v", "grid", "U16", gain=10, unit="V", decimals=1),
    R(32068, "voltage_l3_l1_v", "grid", "U16", gain=10, unit="V", decimals=1),
    R(32069, "voltage_l1_v", "grid", "U16", gain=10, unit="V", decimals=1),
    R(32070, "voltage_l2_v", "grid", "U16", gain=10, unit="V", decimals=1),
    R(32071, "voltage_l3_v", "grid", "U16", gain=10, unit="V", decimals=1),
    R(32072, "current_l1_a", "grid", "I32", gain=1000, unit="A", decimals=3),
    R(32074, "current_l2_a", "grid", "I32", gain=1000, unit="A", decimals=3),
    R(32076, "current_l3_a", "grid", "I32", gain=1000, unit="A", decimals=3),
    R(32080, "active_power_w", "grid", "I32", unit="W"),
    R(32082, "reactive_power_var", "grid", "I32", unit="var"),
    R(32084, "power_factor", "grid", "I16", gain=1000, decimals=3),
    R(32085, "frequency_hz", "grid", "U16", gain=100, unit="Hz", decimals=2),
    # energy
    R(32078, "peak_power_today_w", "energy", "I32", unit="W"),
    R(32106, "total_yield_kwh", "energy", "U32", gain=100, unit="kWh", decimals=2),
    R(32114, "daily_yield_kwh", "energy", "U32", gain=100, unit="kWh", decimals=2),
    # inverter
    # NOTE: contract lists state_1..3 as 32000..32002 U16; Huawei doc has state_2 at 32002 (U16)
    # and state_3 at 32003 (U32). Doc wins (see report).
    R(32000, "state_1", "inverter", "U16", unit="", bits=True),
    R(32002, "state_2", "inverter", "U16", unit="", bits=True),
    R(32003, "state_3", "inverter", "U32", unit="", bits=True),
    R(32008, "alarm_1", "inverter", "U16", bits=True),
    R(32009, "alarm_2", "inverter", "U16", bits=True),
    R(32010, "alarm_3", "inverter", "U16", bits=True),
    R(32086, "efficiency_pct", "inverter", "U16", gain=100, unit="%", decimals=1),
    R(32087, "internal_temperature_c", "inverter", "I16", gain=10, unit="°C", decimals=1),
    R(32088, "insulation_resistance_mohm", "inverter", "U16", gain=1000, unit="MΩ", decimals=3),
    R(32089, "status_code", "inverter", "U16"),
    R(32090, "fault_code", "inverter", "U16"),
    # NOTE: Huawei EPOCH registers hold LOCAL wall-clock time (device time zone), not UTC.
    R(32091, "startup_time", "inverter", "EPOCH"),
    R(32093, "shutdown_time", "inverter", "EPOCH"),
    # device (static)
    R(30000, "model", "device", "STR", STATIC, length=15),
    R(30015, "serial_number", "device", "STR", STATIC, length=10),
    R(30025, "part_number", "device", "STR", STATIC, length=10),
    R(30035, "software_version", "device", "STR", STATIC, length=15),
    R(31025, "firmware_version", "device", "STR", STATIC, length=15),
    R(30070, "model_id", "device", "U16", STATIC),
    # internal only (no group payload / contract field): device time zone offset, minutes
    R(43006, "timezone_offset_min", "internal", "I16", STATIC),
    R(30071, "pv_strings", "device", "U16", STATIC),
    R(30072, "mppt_count", "device", "U16", STATIC),
    R(30073, "rated_power_w", "device", "U32", STATIC, unit="W"),
    R(30075, "max_power_w", "device", "U32", STATIC, unit="W"),
    # control (static + after every setter)
    R(40125, "power_limit_pct", "control", "U16", STATIC, gain=10, unit="%", decimals=1),
    R(40126, "power_limit_w", "control", "U32", STATIC, unit="W"),
]

# Registers used by the setters
REG_POWER_LIMIT_PCT = 40125
REG_POWER_LIMIT_W = 40126
REG_STARTUP = 40200
REG_SHUTDOWN = 40201

BY_FIELD: dict[str, Reg] = {r.field: r for r in REGISTERS}


# --- derived fields (computed from register values / config context) ---------
def _mul(u: Any, i: Any) -> int | None:
    return None if u is None or i is None else round(u * i)


DERIVED: dict[str, Callable[[dict[str, Any], dict[str, Any]], Any]] = {
    "pv1_power_w": lambda v, c: _mul(v.get("pv1_voltage_v"), v.get("pv1_current_a")),
    "pv2_power_w": lambda v, c: _mul(v.get("pv2_voltage_v"), v.get("pv2_current_a")),
    "status": lambda v, c: codes.status_key(v.get("status_code")),
    "running": lambda v, c: codes.is_running(v.get("status_code")),
    "alarms": lambda v, c: codes.decode_alarms(
        {n: v.get(f"alarm_{n}") for n in (1, 2, 3)}),
    "shutdown_allowed": lambda v, c: bool(c.get("allow_shutdown")),
    # ISO time of today's peak (from the history module via context); null if unknown
    "peak_power_today_ts": lambda v, c: c.get("peak_power_today_ts"),
}

# Payload order per group (== order in topic-spec).
GROUP_FIELDS: dict[str, list[str]] = {
    "pv": ["pv1_voltage_v", "pv1_current_a", "pv2_voltage_v", "pv2_current_a",
           "pv1_power_w", "pv2_power_w", "input_power_w"],
    "grid": ["active_power_w", "reactive_power_var", "power_factor", "frequency_hz",
             "voltage_l1_l2_v", "voltage_l2_l3_v", "voltage_l3_l1_v",
             "voltage_l1_v", "voltage_l2_v", "voltage_l3_v",
             "current_l1_a", "current_l2_a", "current_l3_a"],
    "energy": ["daily_yield_kwh", "total_yield_kwh", "peak_power_today_w",
               "peak_power_today_ts"],
    "inverter": ["status_code", "status", "running", "efficiency_pct", "internal_temperature_c",
                 "insulation_resistance_mohm", "fault_code", "state_1", "state_2", "state_3",
                 "alarm_1", "alarm_2", "alarm_3", "alarms", "startup_time", "shutdown_time"],
    "device": ["model", "serial_number", "part_number", "software_version", "firmware_version",
               "model_id", "pv_strings", "mppt_count", "rated_power_w", "max_power_w"],
    "control": ["power_limit_pct", "power_limit_w", "shutdown_allowed"],
}
GROUP_CLASS = {"pv": LIVE, "grid": LIVE, "energy": LIVE, "inverter": LIVE,
               "device": STATIC, "control": STATIC}


# --- decoding -------------------------------------------------------------------
def local_tz(name: str) -> tzinfo | None:
    """Resolve an IANA zone name (None if invalid / no tz database)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, ...
        return None


def epoch_to_utc_iso(raw: int, tz: tzinfo | None) -> str:
    """Huawei epoch = seconds of LOCAL wall-clock time since 1970 -> UTC ISO-8601."""
    local = (datetime(1970, 1, 1) + timedelta(seconds=raw)).replace(tzinfo=tz or timezone.utc)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decode(reg: Reg, words: list[int], tz: tzinfo | None = None) -> tuple[Any, Any]:
    """Return (raw, value). value is None for invalid markers.

    ``tz``: zone in which EPOCH registers (device local time) are interpreted.
    """
    if reg.typ == "STR":
        data = b"".join(w.to_bytes(2, "big") for w in words[: reg.words])
        text = data.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        return text, text
    if reg.typ in ("U16", "I16"):
        raw = words[0] & 0xFFFF
        bits = 16
    else:
        raw = ((words[0] & 0xFFFF) << 16) | (words[1] & 0xFFFF)
        bits = 32
    if reg.typ == "EPOCH":
        if raw in (0, 0xFFFFFFFF):
            return raw, None
        return raw, epoch_to_utc_iso(raw, tz)
    signed = reg.typ.startswith("I")
    if signed and raw >= 1 << (bits - 1):
        raw -= 1 << bits
    marker = _INVALID.get(reg.typ)
    if reg.invalid_marker and marker is not None and (raw & ((1 << bits) - 1)) == marker:
        return raw, None
    if reg.gain == 1 and reg.decimals == 0:
        return raw, raw
    return raw, round(raw / reg.gain, reg.decimals)


@dataclass(frozen=True)
class Block:
    start: int
    count: int
    regs: tuple[Reg, ...]


def build_blocks(regs: list[Reg], max_words: int = MAX_BLOCK_WORDS,
                 max_gap: int = MAX_BLOCK_GAP) -> list[Block]:
    blocks: list[Block] = []
    cur: list[Reg] = []
    start = end = 0
    for r in sorted(regs, key=lambda x: x.address):
        if cur and r.address - end <= max_gap and r.end - start <= max_words:
            cur.append(r)
            end = max(end, r.end)
        else:
            if cur:
                blocks.append(Block(start, end - start, tuple(cur)))
            cur, start, end = [r], r.address, r.end
    if cur:
        blocks.append(Block(start, end - start, tuple(cur)))
    return blocks


def blocks_for(klass: str | None = None, group: str | None = None) -> list[Block]:
    sel = [r for r in REGISTERS
           if (klass is None or r.klass == klass) and (group is None or r.group == group)]
    return build_blocks(sel)
