"""Wiring: poller + MQTT + HTTP in one asyncio process; clean SIGTERM shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
import json
import signal
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from . import registers as rg
from .api import Api, Sessions
from .cache import Cache
from .commands import SET_SUFFIX, CommandExecutor
from .config import Config
from .history import History, handle_request
from .logbuffer import RingBufferHandler
from .modbus import ModbusDevice
from .mqtt import MqttService
from .poller import Poller
from .publish import Publisher
from .state import Persistence, Runtime

log = logging.getLogger("bridge.app")


class App:
    def __init__(self, cfg: Config, ring: RingBufferHandler, device: ModbusDevice | None = None,
                 mqtt_factory: Any = None) -> None:
        self.cfg = cfg
        self.persist = Persistence(cfg.data_dir)
        self.rt = Runtime(cfg, self.persist)
        tz_name = os.environ.get("TZ") or "Europe/Berlin"
        self.history = History(cfg, tz_name)
        self.cache = Cache(context=lambda: {"allow_shutdown": cfg.allow_shutdown,
                                            "peak_power_today_ts": self.history.peak_ts_iso()},
                           tz_name=tz_name)
        self.device = device or ModbusDevice(cfg.huawei_host, cfg.huawei_port, cfg.huawei_unit_id,
                                             cfg.modbus_timeout, cfg.modbus_request_delay_ms)
        kw = {"client_factory": mqtt_factory} if mqtt_factory else {}
        self.mqtt = MqttService(cfg, self.rt, self._on_mqtt_message, self._subscriptions,
                                self._on_mqtt_connected, **kw)
        self.pub = Publisher(cfg, self.cache, self.rt, self.mqtt)
        self.executor = CommandExecutor(cfg, self.device, self.cache, self.rt,
                                        after_write=self._after_write)
        self.poller = Poller(cfg, self.device, self.cache, self.rt, self._on_cycle)
        self.api = Api(cfg, self.rt, self.cache, self.executor, ring,
                       Sessions(self.persist.session_secret(), lambda: cfg.portal_password),
                       history=self.history)
        self.rt.on_serial_change = self.mqtt.request_reconnect
        self.executor.on_access_change = self.pub.publish_bridge
        self.stop_event = asyncio.Event()
        self._last_hist_req: float | None = None
        self._awards_sig: str | None = None
        self._awards_at: float | None = None

    # -- callbacks ---------------------------------------------------------------
    def _subscriptions(self, base: str) -> list[str]:
        return [f"{base}/{s}" for s in SET_SUFFIX] + [f"{base}/history/get"]

    async def _on_mqtt_message(self, topic: str, payload: bytes, retain: bool) -> None:
        base = self.rt.base
        if not base or retain:
            return
        if topic == f"{base}/history/get":
            await self._history_request(base, payload)
            return
        await self.executor.handle_mqtt(base, topic, payload, retain)

    async def _history_request(self, base: str, payload: bytes) -> None:
        now = self.rt.clock()
        if self._last_hist_req is not None and now - self._last_hist_req < 2.0:
            try:
                rid = json.loads(payload).get("id")
            except (ValueError, AttributeError, UnicodeDecodeError):
                rid = None
            result = {"id": rid if isinstance(rid, (str, int)) else None, "type": None,
                      "status": "rejected", "reason": "rate_limited"}
        else:
            self._last_hist_req = now
            result = await handle_request(self.history, payload,
                                          self.cache.value("total_yield_kwh"))
        self.mqtt.publish(f"{base}/history/result", json.dumps(result, ensure_ascii=False), False)

    async def _publish_awards(self, force: bool = False) -> None:
        """`<base>/awards` retained: at start, after a day change, else at most once per STATIC
        interval and only if the content changed."""
        base = self.rt.base
        if base is None:
            return
        now = self.rt.clock()
        if not force and self._awards_at is not None \
                and now - self._awards_at < self.cfg.poll_interval_static:
            return
        self._awards_at = now
        try:
            data = await self.history.awards(self.cache.value("total_yield_kwh"))
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("could not compute awards: %s", exc)
            return
        sig = json.dumps({k: v for k, v in data.items() if k != "generated"}, sort_keys=True)
        if sig == self._awards_sig and not force:
            return
        self._awards_sig = sig
        self.mqtt.publish(f"{base}/awards", json.dumps(data, ensure_ascii=False), True)

    async def _on_mqtt_connected(self) -> None:
        self.pub.publish_snapshot()
        await self._publish_awards(force=True)

    async def _on_cycle(self, klass: str, ok: bool) -> None:
        if ok and klass == rg.STATIC:
            sn = self.cache.value("serial_number")
            if sn:
                self.rt.set_serial(sn)
            self.pub.publish_class(rg.STATIC)
        elif ok and klass == rg.LIVE:
            if self.rt.inverter == "online":
                self._record_history()
            self.pub.publish_class(rg.LIVE)
            rolled, self.history.day_rolled = self.history.day_rolled, False
            await self._publish_awards(force=rolled)
        if klass == rg.LIVE:
            self.pub.publish_bridge()

    def _record_history(self) -> None:
        vals: dict[str, Any] = {}
        for g in ("pv", "grid", "energy", "inverter"):
            vals.update(self.cache.group_payload(g) or {})
        try:
            self.history.record(datetime.now(timezone.utc), vals)
        except Exception:  # noqa: BLE001 - history must never stop polling
            log.exception("history record failed")

    async def _after_write(self, result: dict[str, Any]) -> None:
        self.pub.publish_result(result)
        try:
            await self.poller.refresh_group("control")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not re-read control registers: %s", exc)
        self.pub.publish_group("control")

    async def _status_watch(self) -> None:
        """Publish bridge status on inverter-state change (stale -> offline happens with time)."""
        last = self.rt.inverter
        while True:
            await asyncio.sleep(5)
            cur = self.rt.inverter
            if cur != last:
                last = cur
                self.pub.publish_bridge()

    # -- lifecycle ---------------------------------------------------------------
    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self, install_signals: bool = True) -> None:
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self.request_stop)
                except NotImplementedError:
                    pass
        runner = web.AppRunner(self.api.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.portal_host, self.cfg.portal_listen_port)
        await site.start()
        log.info("portal listening on %s:%s", self.cfg.portal_host, self.cfg.portal_listen_port)
        await self.history.start()
        tasks = [asyncio.create_task(self.poller.run(), name="poller"),
                 asyncio.create_task(self.mqtt.run(), name="mqtt"),
                 asyncio.create_task(self._status_watch(), name="status"),
                 asyncio.create_task(self.history.run(), name="history")]
        await self.stop_event.wait()
        log.info("shutting down")
        self.poller.stop()
        self.mqtt.stop()    # publishes "offline" and disconnects
        tasks[2].cancel()
        for t in (tasks[0], tasks[1]):
            try:
                await asyncio.wait_for(t, 4)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                t.cancel()
        await self.device.close()
        self.history._stop.set()
        try:
            await asyncio.wait_for(tasks[3], 5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            tasks[3].cancel()
        await self.history.close()      # final commit
        await runner.cleanup()
        log.info("stopped at %s", datetime.now(timezone.utc).isoformat())
