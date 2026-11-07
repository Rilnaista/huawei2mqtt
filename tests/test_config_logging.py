import io
import json
import logging

import pytest

from bridge.config import Config, ConfigError, validate_editable
from bridge.logbuffer import setup_logging


def env(**kw):
    return {"HUAWEI_HOST": "h", "MQTT_HOST": "m", **kw}


def test_required_and_defaults(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env({"HUAWEI_HOST": "h"})
    c = Config.from_env(env(DATA_DIR=str(tmp_path), HUAWEI_PORT="", MQTT_RETAIN=""))
    assert (c.huawei_port, c.huawei_unit_id, c.mqtt_port) == (502, 1, 1883)
    assert c.poll_interval_live == 10 and c.poll_interval_static == 300
    assert c.mqtt_retain and c.mqtt_flat_topics and not c.allow_shutdown
    assert c.mqtt_base_topic == "huawei" and c.modbus_request_delay_ms == 200


def test_minimum_intervals_clamped(tmp_path):
    c = Config.from_env(env(DATA_DIR=str(tmp_path), POLL_INTERVAL_LIVE="1",
                            POLL_INTERVAL_STATIC="10"))
    assert (c.poll_interval_live, c.poll_interval_static) == (5, 60)


def test_invalid_env(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env(env(DATA_DIR=str(tmp_path), HUAWEI_PORT="abc"))
    with pytest.raises(ConfigError):
        Config.from_env(env(DATA_DIR=str(tmp_path), MQTT_RETAIN="maybe"))


def test_settings_overlay_and_persist(tmp_path):
    c = Config.from_env(env(DATA_DIR=str(tmp_path)))
    c.apply_editable({"poll_interval_live": 30, "mqtt_flat_topics": False})
    c2 = Config.from_env(env(DATA_DIR=str(tmp_path), POLL_INTERVAL_LIVE="7"))
    assert c2.poll_interval_live == 30 and c2.mqtt_flat_topics is False


def test_invalid_settings_file_ignored(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"poll_interval_live": 1, "log_level": "x"}))
    c = Config.from_env(env(DATA_DIR=str(tmp_path)))
    assert c.poll_interval_live == 10


def test_validate_editable():
    ok, err = validate_editable({"poll_interval_live": 5, "log_level": "debug"})
    assert ok == {"poll_interval_live": 5, "log_level": "DEBUG"} and not err
    _, err = validate_editable({"poll_interval_live": 4, "poll_interval_static": 59,
                                "mqtt_retain": "yes", "foo": 1, "log_level": "TRACE"})
    assert set(err) == {"poll_interval_live", "poll_interval_static", "mqtt_retain", "foo",
                        "log_level"}
    _, err = validate_editable({"poll_interval_live": True})
    assert "poll_interval_live" in err


def test_logging_ring_buffer_and_secret_redaction(tmp_path):
    buf = io.StringIO()
    ring = setup_logging("INFO", lambda: ["s3cret-pw"], stream=buf)
    log = logging.getLogger("bridge.test")
    log.info("connect with password %s", "s3cret-pw")
    log.debug("not shown")
    for i in range(600):
        log.info("n%d", i)
    entries = ring.entries()
    assert len(entries) == 500 and entries[-1]["message"] == "n599"
    assert ring.entries(since=entries[-1]["seq"]) == []
    assert "s3cret-pw" not in buf.getvalue()
    first = json.loads(buf.getvalue().splitlines()[0])
    assert first["message"] == "connect with password ***" and first["level"] == "INFO"
