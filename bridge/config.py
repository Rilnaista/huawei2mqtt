"""Configuration: .env (environment) overlaid by DATA_DIR/settings.json (editable fields)."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("bridge.config")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Limits of the portal-editable fields (also served as "limits" by the API).
LIMITS: dict[str, dict[str, Any]] = {
    "poll_interval_live": {"min": 5, "max": 3600},
    "poll_interval_static": {"min": 60, "max": 86400},
    "log_level": {"values": list(LOG_LEVELS)},
    "mqtt_retain": {"type": "bool"},
    "mqtt_flat_topics": {"type": "bool"},
    "history_days": {"min": 1, "max": 365},
    "feed_in_tariff": {"min": 0, "max": 5},
    "currency": {"pattern": "^[A-Z]{3}$"},
    "co2_factor": {"min": 0, "max": 2},
    "milestone_step_kwh": {"min": 100, "max": 100000},
}
EDITABLE = tuple(LIMITS)
CURRENCY_RE = re.compile(LIMITS["currency"]["pattern"])


class ConfigError(Exception):
    pass


def _s(env: Mapping[str, str], key: str, default: str = "") -> str:
    v = env.get(key)
    return default if v is None or v.strip() == "" else v.strip()


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    v = _s(env, key)
    if v == "":
        return default
    low = v.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}: not a boolean")


def _int(env: Mapping[str, str], key: str, default: int, lo: int = 0, hi: int = 2**31) -> int:
    v = _s(env, key)
    if v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ConfigError(f"{key}: not an integer") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{key}: out of range {lo}..{hi}")
    return n


def _float(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    v = _s(env, key)
    if v == "":
        return default
    try:
        n = float(v)
    except ValueError:
        raise ConfigError(f"{key}: not a number") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{key}: out of range {lo}..{hi}")
    return n


def validate_editable(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate a subset of the editable fields. Returns (clean, errors)."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, val in data.items():
        if key not in LIMITS:
            errors[key] = "unknown_field"
            continue
        lim = LIMITS[key]
        if key.startswith("poll_interval"):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                errors[key] = "invalid_type"
            elif float(val) != int(val):
                errors[key] = "invalid_type"
            elif not lim["min"] <= int(val) <= lim["max"]:
                errors[key] = "out_of_range"
            else:
                clean[key] = int(val)
        elif key in ("history_days", "milestone_step_kwh"):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or float(val) != int(val):
                errors[key] = "invalid_type"
            elif not lim["min"] <= int(val) <= lim["max"]:
                errors[key] = "out_of_range"
            else:
                clean[key] = int(val)
        elif key in ("feed_in_tariff", "co2_factor"):
            if isinstance(val, bool) or not isinstance(val, (int, float)) \
                    or not math.isfinite(float(val)):
                errors[key] = "invalid_type"
            elif not lim["min"] <= float(val) <= lim["max"]:
                errors[key] = "out_of_range"
            else:
                clean[key] = float(val)
        elif key == "currency":
            if isinstance(val, str) and CURRENCY_RE.match(val):
                clean[key] = val
            else:
                errors[key] = "invalid_value"
        elif key == "log_level":
            if isinstance(val, str) and val.upper() in LOG_LEVELS:
                clean[key] = val.upper()
            else:
                errors[key] = "invalid_value"
        else:  # bool fields
            if isinstance(val, bool):
                clean[key] = val
            else:
                errors[key] = "invalid_type"
    return clean, errors


@dataclass
class Config:
    huawei_host: str
    mqtt_host: str
    huawei_port: int = 502
    huawei_unit_id: int = 1
    huawei_device_id: str = ""
    allow_shutdown: bool = False
    modbus_timeout: float = 5.0
    modbus_request_delay_ms: int = 200
    mqtt_port: int = 1883
    mqtt_username: str = field(default="", repr=False)
    mqtt_password: str = field(default="", repr=False)
    mqtt_client_id: str = ""
    mqtt_tls: bool = False
    mqtt_tls_insecure: bool = False
    mqtt_ca_file: str = ""
    mqtt_keepalive: int = 60
    mqtt_qos: int = 0
    mqtt_base_topic: str = "huawei"
    portal_password: str = field(default="", repr=False)
    portal_host: str = "0.0.0.0"
    portal_listen_port: int = 8080
    data_dir: Path = Path("./data")
    web_dir: Path = Path("web")
    # editable at runtime (settings.json overlays .env)
    poll_interval_live: int = 10
    poll_interval_static: int = 300
    log_level: str = "INFO"
    mqtt_retain: bool = True
    mqtt_flat_topics: bool = True
    history_days: int = 90
    feed_in_tariff: float = 0.08
    currency: str = "EUR"
    co2_factor: float = 0.38
    milestone_step_kwh: int = 1000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        host = _s(env, "HUAWEI_HOST")
        mqtt_host = _s(env, "MQTT_HOST")
        missing = [k for k, v in (("HUAWEI_HOST", host), ("MQTT_HOST", mqtt_host)) if not v]
        if missing:
            raise ConfigError("missing required setting(s): " + ", ".join(missing))
        data_dir = _s(env, "DATA_DIR") or ("/data" if os.path.isdir("/data") else "./data")
        web_dir = _s(env, "WEB_DIR") or str(Path(__file__).resolve().parent.parent / "web")
        level = _s(env, "LOG_LEVEL", "INFO").upper()
        if level not in LOG_LEVELS:
            raise ConfigError("LOG_LEVEL: invalid")
        cfg = cls(
            huawei_host=host,
            mqtt_host=mqtt_host,
            huawei_port=_int(env, "HUAWEI_PORT", 502, 1, 65535),
            huawei_unit_id=_int(env, "HUAWEI_UNIT_ID", 1, 0, 255),
            huawei_device_id=_s(env, "HUAWEI_DEVICE_ID"),
            allow_shutdown=_bool(env, "HUAWEI_ALLOW_SHUTDOWN", False),
            modbus_timeout=_float(env, "MODBUS_TIMEOUT", 5.0, 0.5, 60),
            modbus_request_delay_ms=_int(env, "MODBUS_REQUEST_DELAY_MS", 200, 0, 10000),
            mqtt_port=_int(env, "MQTT_PORT", 1883, 1, 65535),
            mqtt_username=_s(env, "MQTT_USERNAME"),
            mqtt_password=env.get("MQTT_PASSWORD", "") or "",
            mqtt_client_id=_s(env, "MQTT_CLIENT_ID"),
            mqtt_tls=_bool(env, "MQTT_TLS", False),
            mqtt_tls_insecure=_bool(env, "MQTT_TLS_INSECURE", False),
            mqtt_ca_file=_s(env, "MQTT_CA_FILE"),
            mqtt_keepalive=_int(env, "MQTT_KEEPALIVE", 60, 5, 3600),
            mqtt_qos=_int(env, "MQTT_QOS", 0, 0, 2),
            mqtt_base_topic=_s(env, "MQTT_BASE_TOPIC", "huawei").strip("/") or "huawei",
            portal_password=env.get("PORTAL_PASSWORD", "") or "",
            portal_listen_port=_int(env, "PORTAL_LISTEN_PORT", 8080, 1, 65535),
            data_dir=Path(data_dir),
            web_dir=Path(web_dir),
            poll_interval_live=_int(env, "POLL_INTERVAL_LIVE", 10, 1, 86400),
            poll_interval_static=_int(env, "POLL_INTERVAL_STATIC", 300, 1, 86400),
            log_level=level,
            mqtt_retain=_bool(env, "MQTT_RETAIN", True),
            mqtt_flat_topics=_bool(env, "MQTT_FLAT_TOPICS", True),
            history_days=_int(env, "HISTORY_DAYS", 90, 1, 365),
            feed_in_tariff=_float(env, "FEED_IN_TARIFF", 0.08, 0, 5),
            currency=_s(env, "CURRENCY", "EUR"),
            co2_factor=_float(env, "CO2_FACTOR", 0.38, 0, 2),
            milestone_step_kwh=_int(env, "MILESTONE_STEP_KWH", 1000, 100, 100000),
        )
        if not CURRENCY_RE.match(cfg.currency):
            raise ConfigError("CURRENCY: must be 3 upper-case letters")
        for key in ("poll_interval_live", "poll_interval_static"):
            lo = LIMITS[key]["min"]
            if getattr(cfg, key) < lo:
                log.warning("%s below minimum, using %s", key.upper(), lo)
                setattr(cfg, key, lo)
        cfg.load_settings()
        return cfg

    # -- settings.json overlay -------------------------------------------------
    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def load_settings(self) -> None:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("settings.json unreadable, ignored")
            return
        if not isinstance(raw, dict):
            return
        clean, errors = validate_editable(raw)
        if errors:
            log.warning("settings.json: ignoring invalid fields: %s", ", ".join(sorted(errors)))
        for k, v in clean.items():
            setattr(self, k, v)

    def apply_editable(self, clean: Mapping[str, Any]) -> None:
        for k, v in clean.items():
            setattr(self, k, v)
        self.save_settings()

    def save_settings(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(self, k) for k in EDITABLE}
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.settings_path)

    def editable(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in EDITABLE}

    def secrets(self) -> list[str]:
        return [s for s in (self.mqtt_password, self.portal_password) if s]
