"""Mock-Server fuer die lokale Vorschau des Portals (nur Python-Stdlib).

Start:  python3 tests/ui/mock_server.py [--port 8099] [--password demo] [--state online|stale|offline]
        [--no-password] [--shutdown-allowed] [--real-time]
Dann http://127.0.0.1:8099/ oeffnen. Liefert die Portal-API aus topic-spec.md mit
realistischen Tagwerten eines 10-kW-Geraets.
"""
import argparse
import json
import math
import secrets
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB = Path(__file__).resolve().parents[2] / "web"
STARTED = datetime.now(timezone.utc)
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml"}

CFG = {"password": "demo", "state": "online", "shutdown": False, "real_time": False}
SESSIONS = set()
FAILS = []  # Zeitstempel fehlgeschlagener Logins
LOGS = []
SETTINGS = {"poll_interval_live": 10, "poll_interval_static": 300, "log_level": "INFO",
            "mqtt_retain": True, "mqtt_flat_topics": True}
LIMITS = {"poll_interval_live": {"min": 5, "max": 300}, "poll_interval_static": {"min": 60, "max": 3600},
          "log_level": {"choices": ["DEBUG", "INFO", "WARNING", "ERROR"]}}
CONTROL = {"power_limit_pct": 100.0, "power_limit_w": 10000}
WRITE = {"state": "unverified", "last_cmd": 0.0}


def log(level, logger, message):
    LOGS.append({"seq": len(LOGS) + 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "level": level, "logger": logger, "message": message})
    del LOGS[:-500]


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def sun_factor():
    """Tageskurve 0..1 (Maximum gegen 13 Uhr lokaler Zeit), leicht wolkig."""
    now = datetime.now()
    h = now.hour + now.minute / 60 if CFG["real_time"] else 12.0 + 1.5 * math.sin(time.time() / 300)
    base = max(0.0, math.sin((h - 6.5) / 13 * math.pi)) if 6.5 < h < 19.5 else 0.0
    return base * (0.82 + 0.1 * math.sin(time.time() / 40))


def snapshot():
    state = CFG["state"]
    age = {"online": 2, "stale": 40, "offline": 900}[state]
    ts = iso(time.time() - age)
    f = sun_factor() if CFG["state"] == "online" else 0.64
    limit = CONTROL["power_limit_pct"] / 100
    pv_total = 6400 * f / 0.64 if f else 0
    pv_total = min(pv_total, 10000 * limit * 1.02)
    p1, p2 = pv_total * 0.54, pv_total * 0.46
    u1, u2 = (590 + 20 * f, 612 + 14 * f) if f else (0, 0)
    ac = pv_total * 0.985
    status = "on_grid" if ac > 30 else "standby_no_irradiation"
    if limit < 1:
        status = "shutdown_command"
    ph = ac / 3
    return {
        "pv": {"pv1_voltage_v": round(u1, 1), "pv1_current_a": round(p1 / u1, 2) if u1 else 0,
               "pv2_voltage_v": round(u2, 1), "pv2_current_a": round(p2 / u2, 2) if u2 else 0,
               "pv1_power_w": round(p1), "pv2_power_w": round(p2), "input_power_w": round(pv_total), "ts": ts},
        "grid": {"active_power_w": round(ac), "reactive_power_var": 0, "power_factor": 1.0, "frequency_hz": 50.01,
                 "voltage_l1_l2_v": 400.1, "voltage_l2_l3_v": 399.4, "voltage_l3_l1_v": 401.0,
                 "voltage_l1_v": 231.2, "voltage_l2_v": 230.6, "voltage_l3_v": 232.1,
                 "current_l1_a": round(ph / 231, 3), "current_l2_a": round(ph / 230.6, 3),
                 "current_l3_a": round(ph / 232, 3), "ts": ts},
        "energy": {"daily_yield_kwh": 31.42, "total_yield_kwh": 18234.77, "peak_power_today_w": 8120, "ts": ts},
        "inverter": {"status_code": 512 if ac > 30 else 0, "status": status, "running": ac > 30,
                     "efficiency_pct": 98.4, "internal_temperature_c": 41.3, "insulation_resistance_mohm": 6.0,
                     "fault_code": 0, "state_1": 3, "state_2": 0, "state_3": 0,
                     "alarm_1": 0, "alarm_2": 0, "alarm_3": 0, "alarms": [],
                     "startup_time": iso(time.time() - 3600 * 5), "shutdown_time": iso(time.time() - 3600 * 14),
                     "ts": ts},
        "device": {"model": "SUN2000-10KTL-M1", "serial_number": "HV2260000000", "part_number": "02311XXX",
                   "software_version": "V100R001C00SPC130", "firmware_version": "V100R001C00SPC130",
                   "model_id": 428, "pv_strings": 2, "mppt_count": 2, "rated_power_w": 10000, "max_power_w": 11000},
        "control": dict(CONTROL, shutdown_allowed=CFG["shutdown"]),
    }


def registers(v):
    rows = []
    spec = [
        (32016, "PV1 voltage", "pv", "pv1_voltage_v", "V", 10), (32017, "PV1 current", "pv", "pv1_current_a", "A", 100),
        (32018, "PV2 voltage", "pv", "pv2_voltage_v", "V", 10), (32019, "PV2 current", "pv", "pv2_current_a", "A", 100),
        (32064, "Input power", "pv", "input_power_w", "W", 1),
        (32080, "Active power", "grid", "active_power_w", "W", 1), (32085, "Grid frequency", "grid", "frequency_hz", "Hz", 100),
        (32069, "Phase A voltage", "grid", "voltage_l1_v", "V", 10), (32070, "Phase B voltage", "grid", "voltage_l2_v", "V", 10),
        (32071, "Phase C voltage", "grid", "voltage_l3_v", "V", 10),
        (32114, "Daily energy yield", "energy", "daily_yield_kwh", "kWh", 100),
        (32106, "Accumulated energy yield", "energy", "total_yield_kwh", "kWh", 100),
        (32078, "Peak active power of current day", "energy", "peak_power_today_w", "W", 1),
        (32087, "Internal temperature", "inverter", "internal_temperature_c", "°C", 10),
        (32086, "Efficiency", "inverter", "efficiency_pct", "%", 100),
        (32089, "Device status", "inverter", "status_code", "", 1),
        (30000, "Model", "device", "model", "", None), (30015, "Serial number", "device", "serial_number", "", None),
        (30073, "Rated power", "device", "rated_power_w", "W", 1),
        (40125, "Active power derating (percent)", "control", "power_limit_pct", "%", 10),
        (40126, "Active power derating (watt)", "control", "power_limit_w", "W", 1),
    ]
    for addr, name, grp, field, unit, gain in spec:
        val = v[grp][field]
        raw = val if gain is None else round(val * gain)
        rows.append({"address": addr, "name": name, "raw": raw, "value": val, "unit": unit, "group": grp})
    return rows


def status():
    s = CFG["state"]
    return {
        "bridge": {"version": "0.1.0-mock", "uptime_s": int((datetime.now(timezone.utc) - STARTED).total_seconds()),
                   "started": STARTED.isoformat(timespec="seconds")},
        "inverter": {"state": s, "last_success": iso(time.time() - {"online": 2, "stale": 40, "offline": 900}[s]),
                     "last_error": None if s == "online" else "timeout"},
        "dongle": {"state": "reachable" if s != "offline" else "unreachable", "host": "192.168.1.45", "port": 502, "unit_id": 1},
        "mqtt": {"state": "connected", "host": "192.168.1.3", "port": 1883, "last_error": None, "base_topic": "huawei/HV2260000000"},
        "write_access": WRITE["state"], "shutdown_allowed": CFG["shutdown"],
        "auth": {"password_set": bool(CFG["password"]), "logged_in": False},
    }


def settings():
    return {"editable": SETTINGS, "limits": LIMITS,
            "readonly": {"huawei_host": "192.168.1.45", "huawei_port": 502, "huawei_unit_id": 1,
                         "mqtt_host": "192.168.1.3", "mqtt_port": 1883, "mqtt_username_set": True,
                         "mqtt_password_set": True, "mqtt_tls": False, "portal_password_set": bool(CFG["password"]),
                         "allow_shutdown": CFG["shutdown"], "base_topic": "huawei/HV2260000000"}}


class H(BaseHTTPRequestHandler):
    server_version = "mock"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["sid"].value if "sid" in c and c["sid"].value in SESSIONS else None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _static(self, path):
        if path in ("/", ""):
            f = WEB / "index.html"
        elif path.startswith("/static/i18n/") or path.startswith("/i18n/"):
            f = WEB / "i18n" / path.rsplit("/", 1)[1]
        elif path.startswith("/static/"):
            f = WEB / "static" / path[len("/static/"):]
        else:
            return self._send(404, {"error": "not_found"})
        f = f.resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self._send(404, {"error": "not_found"})
        self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/healthz":
            return self._send(200, {"status": "ok"})
        if p == "/api/status":
            s = status()
            s["auth"]["logged_in"] = bool(self._session())
            return self._send(200, s)
        if p == "/api/values":
            v = snapshot()
            v["registers"] = registers(v)
            return self._send(200, v)
        if p == "/api/logs":
            since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
            return self._send(200, {"entries": [e for e in LOGS if e["seq"] > since]})
        if p == "/api/settings":
            return self._send(200, settings())
        self._static(p)

    do_HEAD = do_GET

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/login":
            if not CFG["password"]:
                return self._send(403, {"error": "password_not_set"})
            now = time.time()
            FAILS[:] = [t for t in FAILS if now - t < 60]
            if len(FAILS) >= 5:
                return self._send(429, {"error": "rate_limited"})
            if self._body().get("password") != CFG["password"]:
                FAILS.append(now)
                log("WARNING", "portal", "Login fehlgeschlagen")
                return self._send(401, {"error": "invalid_password"})
            sid = secrets.token_hex(16)
            SESSIONS.add(sid)
            log("INFO", "portal", "Login erfolgreich")
            return self._send(200, {"ok": True}, headers={"Set-Cookie": f"sid={sid}; HttpOnly; SameSite=Strict; Path=/"})
        if p == "/api/logout":
            SESSIONS.discard(self._session())
            return self._send(204, b"")
        if not CFG["password"]:
            return self._send(403, {"error": "password_not_set"})
        if not self._session():
            return self._send(401, {"error": "unauthorized"})
        if p == "/api/write-test":
            WRITE["state"] = "verified"
            log("INFO", "control", "Schreibtest ok")
            return self._send(200, {"command": "write_test", "value": CONTROL["power_limit_pct"], "status": "ok", "reason": "", "ts": iso(time.time())})
        if p == "/api/command":
            return self._command(self._body())
        self._send(404, {"error": "not_found"})

    def _command(self, b):
        cmd, val = b.get("command"), b.get("value")

        def res(status, reason=""):
            log("INFO" if status == "ok" else "WARNING", "control", f"{cmd}={val} -> {status} {reason}")
            return self._send(200, {"command": cmd, "value": val, "status": status, "reason": reason, "ts": iso(time.time())})
        if time.time() - WRITE["last_cmd"] < 2:
            return res("rejected", "rate_limited")
        WRITE["last_cmd"] = time.time()
        if cmd == "power_limit_pct":
            if not isinstance(val, (int, float)) or not 0 <= val <= 100:
                return res("rejected", "out_of_range")
            CONTROL["power_limit_pct"] = round(float(val), 1)
            CONTROL["power_limit_w"] = round(11000 * val / 100)
            return res("ok")
        if cmd == "power_limit_w":
            if not isinstance(val, (int, float)) or not 0 <= val <= 11000:
                return res("rejected", "out_of_range")
            CONTROL["power_limit_w"] = int(val)
            CONTROL["power_limit_pct"] = round(val / 110, 1)
            return res("ok")
        if cmd == "power" and val in ("on", "off"):
            if val == "off" and not CFG["shutdown"]:
                return res("rejected", "shutdown_not_allowed")
            return res("ok")
        return res("rejected", "invalid_value")

    def do_PUT(self):
        if urlparse(self.path).path != "/api/settings":
            return self._send(404, {"error": "not_found"})
        if not CFG["password"]:
            return self._send(403, {"error": "password_not_set"})
        if not self._session():
            return self._send(401, {"error": "unauthorized"})
        b = self._body()
        errors = {}
        for k, v in b.items():
            if k not in SETTINGS:
                errors[k] = "unknown_field"
                continue
            lim = LIMITS.get(k, {})
            if isinstance(SETTINGS[k], bool):
                if not isinstance(v, bool):
                    errors[k] = "invalid_value"
            elif isinstance(SETTINGS[k], (int, float)):
                if not isinstance(v, (int, float)) or not lim["min"] <= v <= lim["max"]:
                    errors[k] = "out_of_range"
            elif "choices" in lim and v not in lim["choices"]:
                errors[k] = "invalid_value"
        if errors:
            return self._send(400, {"errors": errors})
        SETTINGS.update(b)
        log("INFO", "settings", f"Einstellungen geaendert: {', '.join(b)}")
        self._send(200, settings())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--password", default="demo")
    ap.add_argument("--no-password", action="store_true")
    ap.add_argument("--state", choices=["online", "stale", "offline"], default="online")
    ap.add_argument("--shutdown-allowed", action="store_true")
    ap.add_argument("--real-time", action="store_true", help="Tageskurve nach echter Uhrzeit (nachts 0 W)")
    a = ap.parse_args()
    CFG.update(password="" if a.no_password else a.password, state=a.state, shutdown=a.shutdown_allowed, real_time=a.real_time)
    start = time.time() - 120
    for i, (lv, lg, m) in enumerate([
        ("INFO", "bridge", "huawei2mqtt 0.1.0-mock gestartet"), ("INFO", "modbus", "Verbunden mit 192.168.1.45:502"),
        ("INFO", "mqtt", "Verbunden mit Broker 192.168.1.3:1883"), ("DEBUG", "poll", "LIVE-Abfrage ok (21 Register)"),
        ("WARNING", "modbus", "Timeout bei Register 32089, Wiederholung in 4 s"), ("INFO", "poll", "LIVE-Abfrage ok"),
        ("ERROR", "mqtt", "Publish fehlgeschlagen: Verbindung getrennt"), ("INFO", "mqtt", "Wieder verbunden")]):
        LOGS.append({"seq": i + 1, "ts": iso(start + i * 11), "level": lv, "logger": lg, "message": m})
    print(f"Mock-Portal auf http://127.0.0.1:{a.port}/  (Passwort: {CFG['password'] or 'keins'}, Zustand: {a.state})")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
