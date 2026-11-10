"""Daily jobs (rc4): `<base>/daily/summary` and early warnings `<base>/warnings`.

Driven by ``tick()`` (every few seconds from the app). No Modbus access; everything comes from the
cache/DB. Persisted in state.json: ``summary_sent`` (date of the last summary) and ``warnings``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from . import insights as ins
from .awards import iso
from .config import Config
from .history import History, RequestError
from .state import Persistence

log = logging.getLogger("bridge.daily")

IDLE_S = 30 * 60
INSULATION_UNIT = "MΩ"

# publish(topic_suffix, payload, retain) -> True if handed to the broker connection
Publish = Callable[[str, dict[str, Any], bool], bool]
# probe() -> (inverter state, input_power_w | None, standby)
OFFLINE_IDLE_MAX_W = 300.0
Probe = Callable[[], tuple[str, float | None, bool]]


class DailyJobs:
    def __init__(self, cfg: Config, persist: Persistence, history: History, publish: Publish,
                 probe: Probe, now: Callable[[], datetime] | None = None,
                 idle_s: float = IDLE_S, ready: Callable[[], bool] = lambda: True) -> None:
        self.cfg, self.persist, self.history = cfg, persist, history
        self._publish, self._probe, self._ready = publish, probe, ready
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.idle_s = idle_s
        self._idle_since: datetime | None = None
        self._last_pw: float | None = None  # last input power seen while online
        self._date: str | None = None
        self._busy = False

    # -- helpers -----------------------------------------------------------------
    def _today(self) -> str:
        return self.history.today_str()

    def _sent(self, day: str) -> bool:
        s = self.persist.summary_sent
        return s is not None and s >= day

    def warnings_view(self) -> dict[str, Any]:
        w = self.persist.warnings
        return {"checked": w.get("checked"), "active": list(w.get("active", []))}

    def publish_warnings(self) -> None:
        if self.persist.warnings.get("checked"):
            self._publish("warnings", self.warnings_view(), True)

    # -- summary ---------------------------------------------------------------------
    async def send_summary(self, day: str) -> bool:
        """Publish the summary of ``day`` once; True if published (marker persisted)."""
        if self._sent(day) or not self._ready():
            return False
        await self.history.flush()
        try:
            payload = await self.history.summary(day)
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("could not build daily summary: %s", exc)
            return False
        if not self._publish("daily/summary", payload, True):
            return False        # not connected: retry on a later tick
        self.persist.summary_sent = day
        self.persist.save()
        log.info("daily summary published for %s", day)
        await self.run_warnings(day)
        return True

    # -- warnings --------------------------------------------------------------------------
    async def run_warnings(self, day: str) -> None:
        """Evaluate the rules for ``day`` (idempotent per day) and publish the result."""
        w = self.persist.warnings
        if w.get("checked_date") and w["checked_date"] >= day:
            return
        try:
            data = await self.history.warning_inputs(day)
        except Exception as exc:  # noqa: BLE001
            log.warning("early-warning check failed: %s", exc)
            return
        cfg = self.cfg
        active = {a["id"]: a for a in w.get("active", []) if isinstance(a, dict) and "id" in a}
        now_iso = iso(self._now().timestamp())
        results = {
            "insulation_low": (ins.eval_insulation(day, data["insulation"],
                                                   cfg.warn_insulation_ratio), INSULATION_UNIT, 3),
            "string_imbalance": (ins.eval_string(day, data["share"], data["yields"],
                                                 cfg.warn_string_deviation), "%", 1),
        }
        for wid, ((status, value, base), unit, nd) in results.items():
            if status == "hit":
                old = active.get(wid)
                if old is None:
                    log.info("early warning %s active (value %s, baseline %s %s)",
                             wid, round(value, nd), round(base, nd), unit)
                active[wid] = {"id": wid, "since": old["since"] if old else now_iso,
                               "value": round(value, nd), "baseline": round(base, nd),
                               "unit": unit}
            elif status == "ok" and wid in active:
                del active[wid]
                log.info("early warning %s cleared", wid)
        self.persist.warnings = {"checked": now_iso, "checked_date": day,
                                 "active": [active[k] for k in ("insulation_low",
                                                                "string_imbalance") if k in active]}
        self.persist.save()
        self.publish_warnings()

    # -- scheduler ---------------------------------------------------------------------------
    def _idle(self) -> bool:
        state, pw, standby = self._probe()
        if state == "online":
            if pw is not None:
                self._last_pw = pw
            return standby or (pw is not None and pw <= 0)
        # unreachable: only counts as end of production if the plant was already (nearly) idle
        # before - a dongle outage at noon must not trigger the daily summary (night = inverter asleep)
        return self._last_pw is not None and self._last_pw < OFFLINE_IDLE_MAX_W

    async def tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self._tick()
        except Exception:  # noqa: BLE001
            log.exception("daily jobs tick failed")
        finally:
            self._busy = False

    async def _tick(self) -> None:
        now = self._now()
        today = self._today()
        if self._date is None:             # start: catch up for yesterday
            self._date = today
            yday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
            await self._summary_if_produced(yday)
            await self.run_warnings(yday)
        elif today != self._date:          # day change: the previous day at the latest
            prev, self._date = self._date, today
            self._idle_since = None
            await self._summary_if_produced(prev)
            await self.run_warnings(prev)
        if self._idle():
            if self._idle_since is None:
                self._idle_since = now
            elif (now - self._idle_since).total_seconds() >= self.idle_s \
                    and not self._sent(today) and (self.history.day_yield(today) or 0) > 0:
                await self.send_summary(today)
        else:
            self._idle_since = None

    async def _summary_if_produced(self, day: str) -> None:
        if self._sent(day):
            return
        try:
            ctx = await self.history.context(day)
        except Exception:  # noqa: BLE001
            return
        if (ctx.get("yield_kwh") or 0) > 0:
            await self.send_summary(day)
