"""Entry point: ``python -m bridge`` (reads .env from the environment; local: ./.env)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .config import Config, ConfigError
from .logbuffer import setup_logging


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (real environment variables win)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def main() -> int:
    load_dotenv(Path(".env"))
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    ring = setup_logging(cfg.log_level, cfg.secrets)
    from .app import App
    asyncio.run(App(cfg, ring).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
