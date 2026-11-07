"""MQTT client service: connect with LWT, backoff 2..60 s, publish queue, set-topic receiver."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .config import Config
from .state import Backoff, Runtime

log = logging.getLogger("bridge.mqtt")

OnMessage = Callable[[str, bytes, bool], Awaitable[None]]


def default_client_factory(cfg: Config, rt: Runtime, will: tuple[str, str]) -> Any:
    import aiomqtt
    tls = None
    if cfg.mqtt_tls:
        tls = aiomqtt.TLSParameters(ca_certs=cfg.mqtt_ca_file or None)
    return aiomqtt.Client(
        hostname=cfg.mqtt_host, port=cfg.mqtt_port,
        username=cfg.mqtt_username or None, password=cfg.mqtt_password or None,
        identifier=cfg.mqtt_client_id or f"huawei2mqtt-{rt.device_id or 'unknown'}",
        keepalive=cfg.mqtt_keepalive,
        will=aiomqtt.Will(topic=will[0], payload=will[1], qos=1, retain=True),
        tls_params=tls, tls_insecure=cfg.mqtt_tls_insecure if cfg.mqtt_tls else None,
    )


class MqttService:
    def __init__(self, cfg: Config, rt: Runtime, on_message: OnMessage,
                 subscriptions: Callable[[str], list[str]],
                 on_connected: Callable[[], Awaitable[None]] | None = None,
                 client_factory: Callable[..., Any] = default_client_factory,
                 backoff: Backoff | None = None) -> None:
        self.cfg, self.rt = cfg, rt
        self._on_message = on_message
        self._subs = subscriptions
        self._on_connected = on_connected
        self._factory = client_factory
        self._backoff = backoff or Backoff()
        self._queue: asyncio.Queue[tuple[str, str, int, bool]] = asyncio.Queue(maxsize=2000)
        self._stop = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    # -- public API ------------------------------------------------------------
    def publish(self, topic: str, payload: str, retain: bool, qos: int | None = None) -> None:
        """Queue a message. Dropped while disconnected (a snapshot is sent on connect)."""
        if not self.rt.mqtt_connected:
            return
        item = (topic, payload, self.cfg.mqtt_qos if qos is None else qos, retain)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def request_reconnect(self) -> None:
        """Base topic changed (serial number learned): reconnect with new LWT."""
        self._reconnect.set()

    def stop(self) -> None:
        self._stop.set()

    # -- main loop -------------------------------------------------------------
    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._session()
                if self._stop.is_set():
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never crash because of the broker
                self.rt.mqtt_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("mqtt connection lost/failed: %s", self.rt.mqtt_error)
            finally:
                self.rt.mqtt_connected = False
            if self._reconnect.is_set() and not self._stop.is_set():
                self._reconnect.clear()
                continue
            delay = self._backoff.next()
            log.info("mqtt reconnect in %.0f s", delay)
            await self._wait(delay)

    async def _wait(self, delay: float) -> None:
        waiters = [asyncio.ensure_future(self._stop.wait()),
                   asyncio.ensure_future(self._reconnect.wait())]
        try:
            await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        self._reconnect.clear()

    async def _session(self) -> None:
        avail = self.rt.availability_topic
        self._reconnect.clear()
        client_cm = self._factory(self.cfg, self.rt, (avail, "offline"))
        async with client_cm as client:
            self.rt.mqtt_connected = True
            self.rt.mqtt_error = None
            self._backoff.reset()
            log.info("mqtt connected to %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
            while not self._queue.empty():
                self._queue.get_nowait()
            await client.publish(avail, "online", qos=1, retain=True)
            base = self.rt.base
            if base:
                for topic in self._subs(base):
                    await client.subscribe(topic, qos=1)
            if self._on_connected:
                await self._on_connected()

            tasks = [asyncio.ensure_future(self._sender(client)),
                     asyncio.ensure_future(self._receiver(client)),
                     asyncio.ensure_future(self._stop.wait()),
                     asyncio.ensure_future(self._reconnect.wait())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._stop.is_set() or self._reconnect.is_set():
                # clean disconnect does not fire the LWT: say goodbye explicitly
                try:
                    await asyncio.wait_for(client.publish(avail, "offline", qos=1, retain=True), 3)
                except Exception as exc:  # noqa: BLE001
                    log.debug("could not publish offline: %s", exc)
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    raise exc

    async def _sender(self, client: Any) -> None:
        while True:
            topic, payload, qos, retain = await self._queue.get()
            await client.publish(topic, payload, qos=qos, retain=retain)

    async def _receiver(self, client: Any) -> None:
        async for msg in client.messages:
            topic = msg.topic.value if hasattr(msg.topic, "value") else str(msg.topic)
            if msg.retain:
                log.info("ignoring retained message on %s", topic)
                continue
            task = asyncio.ensure_future(self._dispatch(topic, bytes(msg.payload or b"")))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, topic: str, payload: bytes) -> None:
        try:
            await self._on_message(topic, payload, False)
        except Exception:  # noqa: BLE001
            log.exception("error handling message on %s", topic)
