"""Persistence (/data) and runtime state (inverter / dongle / mqtt status)."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cache import iso
from .config import Config

log = logging.getLogger("bridge.state")

WRITE_ACCESS = ("verified", "unverified", "denied")


class Backoff:
    """Exponential backoff 2 s -> 60 s."""

    def __init__(self, initial: float = 2.0, maximum: float = 60.0) -> None:
        self.initial, self.maximum = initial, maximum
        self._cur = initial

    def next(self) -> float:
        d = self._cur
        self._cur = min(self._cur * 2, self.maximum)
        return d

    def reset(self) -> None:
        self._cur = self.initial


def inverter_state(last_success: float | None, last_ok: bool, now: float,
                   live_interval: float) -> str:
    """online = last LIVE poll ok; stale = failing, last values younger than 3x LIVE; else offline."""
    if last_success is None:
        return "offline"
    if last_ok:
        return "online"
    return "stale" if now - last_success < 3 * live_interval else "offline"


def safe_topic_part(s: str) -> str:
    return re.sub(r"[\s/+#\x00]+", "_", s.strip()) or "unknown"


class Persistence:
    """state.json (write_access, last serial number) and session secret in DATA_DIR."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.write_access = "unverified"
        self.serial: str | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self.dir / "state.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            if raw.get("write_access") in WRITE_ACCESS:
                self.write_access = raw["write_access"]
            if isinstance(raw.get("serial_number"), str) and raw["serial_number"]:
                self.serial = raw["serial_number"]

    def save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"write_access": self.write_access,
                                       "serial_number": self.serial}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot persist state: %s", exc)

    def session_secret(self) -> bytes:
        p = self.dir / "session_secret"
        try:
            return bytes.fromhex(p.read_text().strip())
        except (OSError, ValueError):
            pass
        sec = secrets.token_bytes(32)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(sec.hex())
        except OSError as exc:
            log.warning("cannot persist session secret (sessions end on restart): %s", exc)
        return sec


class Runtime:
    """Mutable runtime status shared by poller, MQTT, commands and API."""

    def __init__(self, cfg: Config, persist: Persistence,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.persist, self.clock = cfg, persist, clock
        self.started = datetime.now(timezone.utc)
        self._t0 = clock()
        self.version = __version__
        # inverter
        self.last_success: float | None = None
        self.last_success_iso: str | None = None
        self.last_ok = False
        self.last_error: str | None = None
        self.dongle_reachable = False
        # mqtt
        self.mqtt_connected = False
        self.mqtt_error: str | None = None
        self.on_serial_change: Callable[[], None] | None = None

    # -- identity / topics -----------------------------------------------------
    @property
    def device_id(self) -> str | None:
        raw = self.cfg.huawei_device_id or self.persist.serial
        return safe_topic_part(raw) if raw else None

    @property
    def base(self) -> str | None:
        did = self.device_id
        return None if did is None else f"{self.cfg.mqtt_base_topic}/{did}"

    @property
    def availability_topic(self) -> str:
        return f"{self.base or self.cfg.mqtt_base_topic + '/unknown'}/availability"

    def set_serial(self, serial: str) -> None:
        if serial and serial != self.persist.serial:
            old = self.base
            self.persist.serial = serial
            self.persist.save()
            if self.base != old and self.on_serial_change:
                self.on_serial_change()

    # -- inverter state ----------------------------------------------------------
    def live_ok(self) -> None:
        self.last_success = self.clock()
        self.last_success_iso = iso(datetime.now(timezone.utc))
        self.last_ok = True
        self.last_error = None

    def live_failed(self, error: str) -> None:
        self.last_ok = False
        self.last_error = error

    @property
    def inverter(self) -> str:
        return inverter_state(self.last_success, self.last_ok, self.clock(),
                              self.cfg.poll_interval_live)

    @property
    def dongle(self) -> str:
        return "reachable" if self.dongle_reachable else "unreachable"

    @property
    def uptime_s(self) -> int:
        return int(self.clock() - self._t0)

    # -- write access ------------------------------------------------------------
    @property
    def write_access(self) -> str:
        return self.persist.write_access

    def set_write_access(self, value: str) -> bool:
        assert value in WRITE_ACCESS
        if value == self.persist.write_access:
            return False
        self.persist.write_access = value
        self.persist.save()
        return True

    def bridge_payload(self) -> dict[str, Any]:
        return {"inverter": self.inverter, "dongle": self.dongle,
                "write_access": self.write_access, "last_success": self.last_success_iso,
                "version": self.version, "ts": iso(datetime.now(timezone.utc))}
