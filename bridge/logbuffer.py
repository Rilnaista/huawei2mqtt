"""Structured logging: JSON lines on stdout, ring buffer (500) for /api/logs, secret redaction."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


class SecretFilter(logging.Filter):
    """Defence in depth: remove known secret values from log messages."""

    def __init__(self, secrets: Callable[[], Iterable[str]]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = [s for s in self._secrets() if s]
        if secrets:
            msg = record.getMessage()
            for s in secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
        return True


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock2:
            self._seq += 1
            self._buf.append({
                "seq": self._seq,
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })

    def entries(self, since: int = 0) -> list[dict[str, Any]]:
        with self._lock2:
            return [e for e in self._buf if e["seq"] > since]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: str, secrets: Callable[[], Iterable[str]],
                  stream: Any = None) -> RingBufferHandler:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    flt = SecretFilter(secrets)
    ring = RingBufferHandler(500)
    out = logging.StreamHandler(stream or sys.stdout)
    out.setFormatter(JsonFormatter())
    for h in (ring, out):
        h.addFilter(flt)
        root.addHandler(h)
    root.setLevel(level)
    # third-party noise
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)   # no hex dumps; bridge logs itself
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return ring


def set_level(level: str) -> None:
    logging.getLogger().setLevel(level)
