"""Nightly SQLite backup (rc4, CR-018): sqlite3 backup API -> temp file -> rename; rotation.

Runs in a worker thread, reads through its own connection (WAL: no blocking of the writer).
Schedule: once a day after 03:00 local time; not at start unless the newest backup is > 26 h old.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable

from .cache import iso
from .config import Config
from .history import DB_NAME, History

log = logging.getLogger("bridge.backup")

NAME_RE = re.compile(r"^huawei2mqtt-(\d{4}-\d{2}-\d{2})\.db$")
STALE_S = 26 * 3600
HOUR = 3


class Backup:
    def __init__(self, cfg: Config, history: History, tz: tzinfo,
                 now: Callable[[], datetime] | None = None, interval: float = 60.0) -> None:
        self.cfg, self.history, self._tz = cfg, history, tz
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.interval = interval
        self.dir = cfg.data_dir / "backup"
        self._stop = asyncio.Event()
        self._first = True
        self._not_before: date | None = None
        self._last_date: str | None = None
        self.last_at: datetime | None = None
        self.last_file: str | None = None
        self.last_size: int | None = None
        self.last_error: str | None = None
        self._scan()

    # -- files ---------------------------------------------------------------------
    def _files(self) -> list[Path]:
        try:
            return sorted((p for p in self.dir.iterdir() if NAME_RE.match(p.name)),
                          key=lambda p: p.name)
        except OSError:
            return []

    def _scan(self) -> None:
        files = self._files()
        if files and self.last_at is None:
            p = files[-1]
            try:
                st = p.stat()
                self.last_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                self.last_size = st.st_size
            except OSError:
                return
            self.last_file = p.name

    def status(self) -> dict[str, Any]:
        keep = self.cfg.backup_keep
        return {"enabled": keep > 0, "keep": keep, "count": len(self._files()),
                "last_at": iso(self.last_at) if self.last_at else None,
                "last_file": self.last_file, "last_size_bytes": self.last_size,
                "last_error": self.last_error}

    # -- work (worker thread) ----------------------------------------------------------
    def _backup_sync(self, name: str) -> int:
        self.dir.mkdir(parents=True, exist_ok=True)
        final = self.dir / name
        tmp = self.dir / (name + ".tmp")
        src = sqlite3.connect(self.cfg.data_dir / DB_NAME, timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        except Exception:
            src.close()
            tmp.unlink(missing_ok=True)
            raise
        src.close()
        os.replace(tmp, final)
        return final.stat().st_size

    def _rotate_sync(self) -> int:
        keep = self.cfg.backup_keep
        if keep <= 0:
            return 0
        n = 0
        for p in self._files()[:-keep]:
            try:
                p.unlink()
                n += 1
            except OSError as exc:
                log.warning("backup rotation: cannot delete %s: %s", p.name, exc)
        return n

    async def run_backup(self) -> bool:
        """One backup now (unconditional). Never raises."""
        local = self._now().astimezone(self._tz)
        name = f"huawei2mqtt-{local.date().isoformat()}.db"
        try:
            await self.history.flush()
            size = await asyncio.to_thread(self._backup_sync, name)
            removed = await asyncio.to_thread(self._rotate_sync)
        except Exception as exc:  # noqa: BLE001 - never fatal
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("database backup failed (%s)", self.last_error)
            return False
        self.last_error = None
        self.last_at, self.last_file, self.last_size = self._now(), name, size
        self._last_date = local.date().isoformat()
        log.info("database backup written: %s (%d bytes), rotated %d old", name, size, removed)
        return True

    # -- schedule --------------------------------------------------------------------------
    async def tick(self) -> None:
        if self.cfg.backup_keep <= 0:
            return
        now = self._now()
        local = now.astimezone(self._tz)
        today = local.date()
        if self._first:
            self._first = False
            if self.last_at is None or (now - self.last_at).total_seconds() > STALE_S:
                await self.run_backup()
                self._not_before = today + timedelta(days=1) if local.hour >= HOUR else today
                return
            # not at start: next regular slot
            self._not_before = today + timedelta(days=1) if local.hour >= HOUR else today
            self._last_date = (self.last_at.astimezone(self._tz).date().isoformat())
        if self._not_before is not None and today < self._not_before:
            return
        if local.hour >= HOUR and self._last_date != today.isoformat():
            await self.run_backup()
        elif await asyncio.to_thread(self._rotate_sync):
            pass       # keep lowered at runtime: trim

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("backup tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
