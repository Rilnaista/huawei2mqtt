"""Mock-Server fuer die lokale Vorschau des Portals (nur Python-Stdlib).

Start:  python3 tests/ui/mock_server.py [--port 8099] [--password demo] [--state online|stale|offline]
        [--no-password] [--shutdown-allowed] [--real-time] [--night] [--dropouts N]
        [--sun-now HH:MM] [--peak-ts-null] [--empty] [--legacy] [--long]
Dann http://127.0.0.1:8099/ oeffnen. Liefert die Portal-API aus topic-spec.md (rc1 + Erweiterung rc2)
mit realistischen Tagwerten eines 10-kW-Geraets. Zusaetzlich:
  --dropouts N     inverter.dropouts_24h (1-2 = Hinweis, ab 3 gelb)
  --sun-now HH:MM  Tageskurve im Verlauf so verschieben, dass "jetzt" dieser Uhrzeit entspricht (Screenshots)
  --peak-ts-null   energy.peak_power_today_ts = null
  --empty          frische Installation: Verlauf/Tageswerte/Awards leer
  --long           rc3: 2 Jahre 5-Minuten-Daten (samples_5m, saisonal), Feindaten nur history_days; resolution_s
  --legacy         altes Backend: /api/history, /api/daily, /api/awards, /api/history/clear liefern 404
"""
import argparse
import json
import math
import re
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

CFG = {"password": "demo", "state": "online", "shutdown": False, "real_time": False, "dropouts": 0,
       "sun_shift": 0.0, "peak_ts_null": False, "empty": False, "legacy": False, "long": False}
LONG_DAYS = 730
SESSIONS = set()
FAILS = []  # Zeitstempel fehlgeschlagener Logins
LOGS = []
SETTINGS = {"poll_interval_live": 10, "poll_interval_static": 300, "log_level": "INFO",
            "mqtt_retain": True, "mqtt_flat_topics": True,
            "history_days": 90, "feed_in_tariff": 0.081, "currency": "EUR", "co2_factor": 0.38,
            "milestone_step_kwh": 1000}
LIMITS = {"poll_interval_live": {"min": 5, "max": 300}, "poll_interval_static": {"min": 60, "max": 3600},
          "log_level": {"choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
          "history_days": {"min": 1, "max": 365}, "feed_in_tariff": {"min": 0, "max": 5},
          "co2_factor": {"min": 0, "max": 2}, "milestone_step_kwh": {"min": 100, "max": 100000}}
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
    if CFG.get("night"):
        return 0.0
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
        "energy": {"daily_yield_kwh": 31.42, "total_yield_kwh": 18234.77, "peak_power_today_w": 8120,
                   "peak_power_today_ts": None if CFG["peak_ts_null"] else iso(time.time() - 7200), "ts": ts},
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
    """Wie die echte API: name = Feld-Key (z. B. pv1_voltage_v); Klartext liefert das Portal per i18n."""
    spec = [  # (Adresse, Gruppe, Feld, Einheit, Gain)
        (32016, "pv", "pv1_voltage_v", "V", 10), (32017, "pv", "pv1_current_a", "A", 100),
        (32018, "pv", "pv2_voltage_v", "V", 10), (32019, "pv", "pv2_current_a", "A", 100),
        (32064, "pv", "input_power_w", "W", 1),
        (32080, "grid", "active_power_w", "W", 1), (32082, "grid", "reactive_power_var", "var", 1),
        (32084, "grid", "power_factor", "", 1000), (32085, "grid", "frequency_hz", "Hz", 100),
        (32066, "grid", "voltage_l1_l2_v", "V", 10), (32067, "grid", "voltage_l2_l3_v", "V", 10),
        (32068, "grid", "voltage_l3_l1_v", "V", 10),
        (32069, "grid", "voltage_l1_v", "V", 10), (32070, "grid", "voltage_l2_v", "V", 10),
        (32071, "grid", "voltage_l3_v", "V", 10),
        (32072, "grid", "current_l1_a", "A", 1000), (32074, "grid", "current_l2_a", "A", 1000),
        (32076, "grid", "current_l3_a", "A", 1000),
        (32114, "energy", "daily_yield_kwh", "kWh", 100), (32106, "energy", "total_yield_kwh", "kWh", 100),
        (32078, "energy", "peak_power_today_w", "W", 1),
        (32089, "inverter", "status_code", "", 1), (32086, "inverter", "efficiency_pct", "%", 100),
        (32087, "inverter", "internal_temperature_c", "°C", 10),
        (32088, "inverter", "insulation_resistance_mohm", "MΩ", 1000), (32090, "inverter", "fault_code", "", 1),
        (32000, "inverter", "state_1", "", 1), (32001, "inverter", "state_2", "", 1), (32002, "inverter", "state_3", "", 1),
        (32008, "inverter", "alarm_1", "", 1), (32009, "inverter", "alarm_2", "", 1), (32010, "inverter", "alarm_3", "", 1),
        (30000, "device", "model", "", None), (30015, "device", "serial_number", "", None),
        (30025, "device", "part_number", "", None), (30035, "device", "software_version", "", None),
        (31025, "device", "firmware_version", "", None), (30070, "device", "model_id", "", 1),
        (30071, "device", "pv_strings", "", 1), (30072, "device", "mppt_count", "", 1),
        (30073, "device", "rated_power_w", "W", 1), (30075, "device", "max_power_w", "W", 1),
        (40125, "control", "power_limit_pct", "%", 10), (40126, "control", "power_limit_w", "W", 1),
    ]
    rows = []
    for addr, grp, field, unit, gain in spec:
        val = v[grp][field]
        raw = val if gain is None else round(val * gain)
        rows.append({"address": addr, "name": field, "raw": raw, "value": val, "unit": unit, "group": grp})
    return rows


def poll_info():
    now = time.time()
    if CFG["state"] == "online":
        last = math.floor(now / 10) * 10
        return {"live_interval_s": 10, "last_live_at": iso(last), "next_live_at": iso(last + 10)}
    last = now - {"stale": 40, "offline": 900}[CFG["state"]]
    return {"live_interval_s": 10, "last_live_at": iso(last), "next_live_at": iso((math.floor(now / 20) + 1) * 20)}


def history_info():
    if CFG["empty"] or HIST["cleared"]:
        return {"days": SETTINGS["history_days"], "samples": 0, "samples_5m": 0, "oldest": None,
                "detail_oldest": None, "db_size_bytes": 24576}
    days = SETTINGS["history_days"]
    n5 = int(max(0, LONG_DAYS - days) * 288 * 0.42) if CFG["long"] else 0
    return {"days": days, "samples": int(days * 8640 * 0.96), "samples_5m": n5,
            "oldest": iso(time.time() - (LONG_DAYS if CFG["long"] else days) * 86400),
            "detail_oldest": iso(time.time() - days * 86400),
            "db_size_bytes": int(days * 8640 * 0.96 * 118 + n5 * 118)}


def status():
    s = CFG["state"]
    return {
        "bridge": {"version": "0.1.0-mock", "uptime_s": int((datetime.now(timezone.utc) - STARTED).total_seconds()),
                   "started": STARTED.isoformat(timespec="seconds")},
        "inverter": {"state": s, "last_success": iso(time.time() - {"online": 2, "stale": 40, "offline": 900}[s]),
                     "last_error": None if s == "online" else "timeout",
                     "dropouts_24h": CFG["dropouts"], "last_dropout": iso(time.time() - 5400) if CFG["dropouts"] else None},
        "poll": poll_info(), "history": history_info(),
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
                         "allow_shutdown": CFG["shutdown"], "base_topic": "huawei/HV2260000000",
                         "software_version": "V100R001C00SPC130", "firmware_version": "V100R001C00SPC130"}}


# ---------------------------------------------------------------- rc2: Verlauf / Tageswerte / Awards
HIST = {"cleared": False, "since": 0.0}
DAILY_CLEARED = {"v": False}


def _noise(k):
    return (math.sin(k * 12.9898) * 43758.5453) % 1.0


def _day_factor(day):
    return 0.30 + 0.70 * _noise(day.toordinal() * 1.7)


def pv_power(ts):
    """Synthetische PV-Leistung (W) zum Zeitpunkt ts: Sonnenkurve, Tageswetter, schnelle Wolkenspruenge."""
    lt = datetime.fromtimestamp(ts + CFG["sun_shift"])
    hr = lt.hour + lt.minute / 60 + lt.second / 3600
    if not 5.8 < hr < 19.8:
        return 0.0
    base = math.sin((hr - 5.8) / 14 * math.pi) ** 1.25
    slow = 0.85 + 0.15 * math.sin(ts / 1500 + lt.toordinal())
    fast = 1.0 - 0.45 * max(0.0, _noise(int(ts // 10)) - 0.72) / 0.28 * (0.5 + 0.5 * math.sin(ts / 330))
    seas = 1.0
    if CFG["long"]:  # saisonal: Sommer ~1.0, Winter ~0.25
        seas = 0.25 + 0.75 * (0.5 - 0.5 * math.cos(2 * math.pi * (lt.timetuple().tm_yday - 15) / 365))
    return max(0.0, 10150 * base * _day_factor(lt.date()) * slow * fast * seas)


def yield_so_far(ts):
    lt = datetime.fromtimestamp(ts + CFG["sun_shift"])
    hr = lt.hour + lt.minute / 60
    x = min(1.0, max(0.0, (hr - 5.8) / 14))
    return 0.5 * (1 - math.cos(math.pi * x)) * 62 * _day_factor(lt.date())


FIELDS = [  # (Name, Einheit, Gruppe)
    ("input_power_w", "W", "pv"), ("pv1_power_w", "W", "pv"), ("pv2_power_w", "W", "pv"),
    ("pv1_voltage_v", "V", "pv"), ("pv2_voltage_v", "V", "pv"), ("pv1_current_a", "A", "pv"), ("pv2_current_a", "A", "pv"),
    ("active_power_w", "W", "grid"), ("frequency_hz", "Hz", "grid"), ("voltage_l1_v", "V", "grid"),
    ("current_l1_a", "A", "grid"),
    ("daily_yield_kwh", "kWh", "energy"),
    ("efficiency_pct", "%", "inverter"), ("internal_temperature_c", "°C", "inverter"),
]
UNIT = {f: u for f, u, _ in FIELDS}


def field_value(f, ts):
    p = pv_power(ts)
    on = p > 30
    if f == "input_power_w":
        return round(p)
    if f == "active_power_w":
        return round(p * 0.985)
    if f == "pv1_power_w":
        return round(p * 0.54)
    if f == "pv2_power_w":
        return round(p * 0.46)
    if f in ("pv1_voltage_v", "pv2_voltage_v"):
        return round((590 if f == "pv1_voltage_v" else 612) + 25 * (p / 10000) + 3 * _noise(ts // 10), 1) if on else 0
    if f in ("pv1_current_a", "pv2_current_a"):
        return round(p * (0.54 if f == "pv1_current_a" else 0.46) / 600, 2)
    if f == "frequency_hz":
        return round(50 + 0.04 * math.sin(ts / 200) + 0.02 * (_noise(ts // 10) - 0.5), 2)
    if f == "voltage_l1_v":
        return round(231 + 2.5 * math.sin(ts / 900) + 1.5 * (p / 10000), 1)
    if f == "current_l1_a":
        return round(p * 0.985 / 3 / 231, 2)
    if f == "daily_yield_kwh":
        return round(yield_so_far(ts), 2)
    if f == "efficiency_pct":
        return 98.4 if on else 0
    if f == "internal_temperature_c":
        return round(21 + 24 * (p / 10000) + 0.5 * math.sin(ts / 700), 1)
    return 0


def history_payload(q):
    now = time.time()
    t1 = _parse_iso(q.get("to", [None])[0], now)
    t0 = _parse_iso(q.get("from", [None])[0], datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    fields = [f for f in (q.get("fields", [""])[0] or "input_power_w,active_power_w").split(",") if f]
    pts = max(2, min(2000, int(q.get("points", ["500"])[0] or 500)))
    if t1 <= t0:
        return None
    step = (t1 - t0) / pts
    detail0 = now - SETTINGS["history_days"] * 86400
    lo_limit = max(now - (LONG_DAYS if CFG["long"] else SETTINGS["history_days"]) * 86400, HIST["since"])
    empty = CFG["empty"] or HIST["cleared"]
    t, series = [], {f: {"avg": [], "min": [], "max": []} for f in fields if f in UNIT}
    for i in range(pts):
        b0 = t0 + i * step
        t.append(int(b0 * 1000))
        inside = not empty and b0 + step > lo_limit and b0 <= now and not (CFG["state"] != "online" and b0 > now - 900)
        n = max(1, min(96, int(step // 10)))
        pts_ts = [min(b0 + (k + 0.5) * step / n, now) for k in range(n)] if inside else []
        if inside and b0 < detail0:  # verdichteter Bereich: nur 300-s-Raster, sonst Bucket leer
            g0 = math.ceil(b0 / 300) * 300
            grid = [g0 + 300 * k for k in range(int((min(b0 + step, detail0) - g0) // 300) + 1) if g0 + 300 * k < min(b0 + step, detail0)]
            if len(grid) > 96:
                grid = grid[:: math.ceil(len(grid) / 96)]
            pts_ts = [x + 150 for x in grid] + [x for x in pts_ts if x >= detail0]
        pts_ts = [x for x in pts_ts if x >= lo_limit]
        for f in series:
            if not pts_ts:
                for k in ("avg", "min", "max"):
                    series[f][k].append(None)
                continue
            vals = [field_value(f, x) for x in pts_ts]
            series[f]["avg"].append(round(sum(vals) / len(vals), 2))
            series[f]["min"].append(min(vals))
            series[f]["max"].append(max(vals))
    return {"from": iso(t0), "to": iso(t1), "step_s": round(step, 2), "fields": list(series), "t": t, "series": series,
            "resolution_s": 300 if (CFG["long"] and t1 <= detail0 and not empty) else (
                "mixed" if CFG["long"] and t0 < detail0 and not empty else 10),
            "available": [{"name": f, "unit": u, "group": g} for f, u, g in FIELDS]}


def _parse_iso(v, default):
    if not v:
        return default
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def build_daily():
    """Gut ein Jahr Tageswerte (Saison + Wetter); aeltere ~120 Tage als 'import' ohne Peak-Zeit."""
    today = datetime.now().date()
    n = 430
    rows, total = [], 18234.77 - 31.42
    seq = []
    for i in range(1, n + 1):
        d = today - timedelta(days=i)
        season = 0.5 - 0.5 * math.cos(2 * math.pi * (d.timetuple().tm_yday - 15) / 365)
        y = round((5 + 58 * season) * (0.35 + 0.65 * _noise(d.toordinal() * 1.7)) , 2)
        seq.append((d, y))
    # rueckwaerts den Zaehlerstand ableiten
    totals = {}
    t = total
    for d, y in seq:
        totals[d] = round(t, 2)
        t -= y
    for d, y in reversed(seq):
        season = 0.5 - 0.5 * math.cos(2 * math.pi * (d.timetuple().tm_yday - 15) / 365)
        start_h = 7.4 - 2.2 * season + 0.5 * _noise(d.toordinal())
        end_h = 16.8 + 3.0 * season + 0.4 * _noise(d.toordinal() + 3)
        imp = (today - d).days > 300
        peak = round((3400 + 6500 * min(1, y / 55)) + 120 * _noise(d.toordinal() + 9))

        def at(h):
            return datetime(d.year, d.month, d.day, int(h), int((h % 1) * 60)).astimezone(timezone.utc).isoformat(timespec="seconds")
        rows.append({"date": d.isoformat(), "yield_kwh": y, "total_yield_kwh": totals[d],
                     "peak_power_w": peak, "peak_ts": None if imp else at(start_h + (end_h - start_h) * 0.5),
                     "first_production_ts": at(start_h), "last_production_ts": at(end_h),
                     "production_s": int((end_h - start_h) * 3600), "source": "import" if imp else "bridge"})
    rows.append({"date": today.isoformat(), "yield_kwh": 31.42, "total_yield_kwh": 18234.77, "peak_power_w": 8120,
                 "peak_ts": iso(time.time() - 7200), "first_production_ts": iso(time.time() - 3600 * 6),
                 "last_production_ts": None, "production_s": 21000, "source": "bridge"})
    return rows


DAILY = build_daily()


def daily_rows(q=None):
    if CFG["empty"] or DAILY_CLEARED["v"]:
        return []
    rows = DAILY
    if q:
        f, to = q.get("from", [None])[0], q.get("to", [None])[0]
        rows = [r for r in rows if (not f or r["date"] >= f) and (not to or r["date"] <= to)]
    return rows


def awards_payload():
    rows = daily_rows()
    step = SETTINGS["milestone_step_kwh"]
    cur = SETTINGS["currency"]
    tar = SETTINGS["feed_in_tariff"]
    fac = SETTINGS["co2_factor"]
    total = 18234.77
    today = datetime.now().date()
    r2 = lambda v: round(v, 2)
    top = lambda lst, key, extra: [dict(rank=i + 1, **extra(x)) for i, x in enumerate(sorted(lst, key=key, reverse=True)[:3])]
    out = {"generated": iso(time.time()), "tracking_since": rows[0]["date"] if rows else None,
           "rankings": {"best_day": [], "best_week": [], "best_month": [], "best_year": [], "peak_power": [],
                        "longest_day": [], "earliest_start": []},
           "milestones": {"step_kwh": step, "reached": [], "next": {"kwh": (int(total // step) + 1) * step,
                          "remaining_kwh": round((int(total // step) + 1) * step - total, 1)}},
           "co2": {"factor_kg_kwh": fac, "today_kg": round(31.42 * fac, 1), "year_kg": 0.0, "total_kg": round(total * fac, 1)},
           "money": {"tariff_per_kwh": tar, "currency": cur, "today": r2(31.42 * tar), "month": 0.0, "year": 0.0,
                     "total": r2(total * tar)}}
    if not rows:
        return out
    rk = out["rankings"]
    rk["best_day"] = top(rows, lambda r: r["yield_kwh"], lambda r: {"value": r["yield_kwh"], "unit": "kWh", "date": r["date"]})
    for key, fmt_ in (("best_week", lambda d: "%d-W%02d" % d.isocalendar()[:2]), ("best_month", lambda d: d.strftime("%Y-%m")),
                      ("best_year", lambda d: str(d.year))):
        agg = {}
        for r in rows:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            agg[fmt_(d)] = agg.get(fmt_(d), 0) + r["yield_kwh"]
        rk[key] = [dict(rank=i + 1, value=r2(v), unit="kWh", period=p)
                   for i, (p, v) in enumerate(sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:3])]
    rk["peak_power"] = top(rows, lambda r: r["peak_power_w"], lambda r: {"value": r["peak_power_w"], "unit": "W", "ts": r["peak_ts"], "date": r["date"]})
    full = [r for r in rows if r["first_production_ts"] and r["last_production_ts"]]
    rk["longest_day"] = top(full, lambda r: r["production_s"], lambda r: {"value": r["production_s"], "unit": "s", "date": r["date"],
                            "start": r["first_production_ts"], "end": r["last_production_ts"]})
    yr = [r for r in full if r["date"].startswith(str(today.year))]
    loc = lambda s: datetime.fromisoformat(s).astimezone()
    early = sorted(yr, key=lambda r: loc(r["first_production_ts"]).time())[:3]
    rk["earliest_start"] = [{"rank": i + 1, "value": loc(r["first_production_ts"]).strftime("%H:%M"), "date": r["date"],
                             "ts": r["first_production_ts"]} for i, r in enumerate(early)]
    first_total = rows[0]["total_yield_kwh"] - rows[0]["yield_kwh"]
    reached = []
    k = step
    while k <= total:
        ts = None
        if k > first_total:
            hit = next((r for r in rows if r["total_yield_kwh"] >= k), None)
            ts = hit["last_production_ts"] or hit["first_production_ts"] if hit else None
        reached.append({"kwh": k, "ts": ts})
        k += step
    out["milestones"]["reached"] = reached
    ytd = sum(r["yield_kwh"] for r in rows if r["date"].startswith(str(today.year)))
    mtd = sum(r["yield_kwh"] for r in rows if r["date"].startswith(today.strftime("%Y-%m")))
    out["co2"]["year_kg"] = round(ytd * fac, 1)
    out["money"].update(month=r2(mtd * tar), year=r2(ytd * tar))
    return out


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
        if p in ("/api/history", "/api/daily", "/api/awards"):
            if CFG["legacy"]:
                return self._send(404, {"error": "not_found"})
            q = parse_qs(u.query)
            if p == "/api/history":
                pl = history_payload(q)
                return self._send(200 if pl else 400, pl or {"error": "bad_range"})
            if p == "/api/daily":
                return self._send(200, {"days": daily_rows(q)})
            return self._send(200, awards_payload())
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
        if p == "/api/history/clear":
            if CFG["legacy"]:
                return self._send(404, {"error": "not_found"})
            scope = self._body().get("scope")
            if scope not in ("samples", "daily", "all"):
                return self._send(400, {"error": "invalid_scope"})
            had = not (CFG["empty"] or HIST["cleared"])
            d = {"samples": 0, "daily": 0, "milestones": 0}
            if scope in ("samples", "all"):
                d["samples"] = int(SETTINGS["history_days"] * 8640 * 0.96) if had else 0
                d["samples_5m"] = history_info()["samples_5m"] if had else 0
                HIST["cleared"] = True
                HIST["since"] = time.time()
            if scope in ("daily", "all"):
                d["daily"] = len(daily_rows())
                d["milestones"] = len(awards_payload()["milestones"]["reached"]) if d["daily"] else 0
                DAILY_CLEARED["v"] = True
            log("INFO", "history", f"Speicher geleert: {scope}")
            return self._send(200, {"status": "ok", "deleted": d})
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
            elif k == "currency" and not (isinstance(v, str) and re.fullmatch(r"[A-Z]{3}", v)):
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
    ap.add_argument("--night", action="store_true", help="Nacht: 0 W, Status standby_no_irradiation")
    ap.add_argument("--real-time", action="store_true", help="Tageskurve nach echter Uhrzeit (nachts 0 W)")
    ap.add_argument("--dropouts", type=int, default=0, help="inverter.dropouts_24h")
    ap.add_argument("--sun-now", default="", help="HH:MM: Tageskurve des Verlaufs so verschieben, dass jetzt = diese Uhrzeit")
    ap.add_argument("--peak-ts-null", action="store_true")
    ap.add_argument("--empty", action="store_true", help="frische Installation ohne Verlauf/Tageswerte")
    ap.add_argument("--long", action="store_true", help="rc3: 2 Jahre 5-Minuten-Daten")
    ap.add_argument("--legacy", action="store_true", help="aelteres Backend: neue Endpunkte -> 404")
    a = ap.parse_args()
    if a.sun_now:
        hh, mm = (int(x) for x in a.sun_now.split(":"))
        n = datetime.now()
        CFG["sun_shift"] = (hh * 3600 + mm * 60) - (n.hour * 3600 + n.minute * 60 + n.second)
    CFG.update(dropouts=a.dropouts, peak_ts_null=a.peak_ts_null, empty=a.empty, legacy=a.legacy, long=a.long)
    CFG.update(password="" if a.no_password else a.password, state=a.state, shutdown=a.shutdown_allowed, real_time=a.real_time, night=a.night)
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
