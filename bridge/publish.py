"""Topic/payload construction for getters (group JSON + flat topics)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import registers as rg
from .cache import Cache, iso
from .config import Config
from .mqtt import MqttService
from .state import Runtime

BRIDGE_FIELDS = ["inverter", "dongle", "write_access", "last_success", "version",
                 "dropouts_24h"]


def flat_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def group_messages(base: str, group: str, payload: dict[str, Any],
                   flat: bool) -> list[tuple[str, str]]:
    msgs = [(f"{base}/{group}", json.dumps(payload, ensure_ascii=False))]
    if flat:
        for k, v in payload.items():
            if k == "ts" or v is None:   # invalid values: no flat topic
                continue
            msgs.append((f"{base}/{group}/{k}", flat_value(v)))
    return msgs


class Publisher:
    def __init__(self, cfg: Config, cache: Cache, rt: Runtime, mqtt: MqttService) -> None:
        self.cfg, self.cache, self.rt, self.mqtt = cfg, cache, rt, mqtt

    def _send(self, msgs: list[tuple[str, str]], retain: bool | None = None) -> None:
        r = self.cfg.mqtt_retain if retain is None else retain
        for topic, payload in msgs:
            self.mqtt.publish(topic, payload, r)

    def publish_group(self, group: str) -> None:
        base = self.rt.base
        payload = self.cache.group_payload(group)
        if base is None or payload is None:
            return
        self._send(group_messages(base, group, payload, self.cfg.mqtt_flat_topics))

    def publish_class(self, klass: str) -> None:
        for group, k in rg.GROUP_CLASS.items():
            if k == klass:
                self.publish_group(group)

    def publish_bridge(self) -> None:
        base = self.rt.base
        if base is None:
            return
        payload = self.rt.bridge_payload()
        msgs = group_messages(base, "bridge", payload, False)
        if self.cfg.mqtt_flat_topics:
            msgs += [(f"{base}/bridge/{k}", flat_value(payload[k]))
                     for k in BRIDGE_FIELDS if payload.get(k) is not None]
        self._send(msgs)

    def publish_snapshot(self) -> None:
        """After (re)connect: static groups always, live groups only while online."""
        self.publish_class(rg.STATIC)
        if self.rt.inverter == "online":
            self.publish_class(rg.LIVE)
        self.publish_bridge()

    def publish_result(self, result: dict[str, Any]) -> None:
        base = self.rt.base
        if base is None:
            return
        self.mqtt.publish(f"{base}/control/result", json.dumps(result, ensure_ascii=False),
                          False)


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))
