"""Awards: pure functions over daily/milestones rows."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bridge import awards as aw

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def ep(y, m, d, hh, mm):          # local Berlin wall clock -> epoch
    return int(datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp())


def day(date, y, peak=None, a=None, b=None, pts=None):
    return {"date": date, "yield_kwh": y, "total_yield_kwh": None, "peak_power_w": peak,
            "peak_ts": pts, "first_production_ts": a, "last_production_ts": b, "source": "bridge",
            "start_observed": 1 if a else 0, "end_observed": 1 if b else 0}


def run(rows, ms=(), total=21308.0, **kw):
    args = dict(now=NOW, tz=TZ, step_kwh=1000, total_kwh=total, tariff=0.081, currency="EUR",
                co2_factor=0.38)
    args.update(kw)
    return aw.compute(list(rows), list(ms), **args)


def test_empty_db():
    r = run([], total=None)
    assert r["tracking_since"] is None
    assert all(v == [] for v in r["rankings"].values())
    assert r["milestones"] == {"step_kwh": 1000, "reached": [], "next": None}
    assert r["co2"]["today_kg"] == 0 and r["money"]["total"] == 0
    assert r["generated"] == "2026-07-15T10:00:00Z"


def test_best_day_top3_and_ties():
    rows = [day("2026-07-01", 40), day("2026-07-02", 63.2), day("2026-07-03", 40),
            day("2026-07-04", 10), day("2026-07-05", 55.554)]
    best = run(rows)["rankings"]["best_day"]
    assert [(b["rank"], b["value"], b["date"]) for b in best] == [
        (1, 63.2, "2026-07-02"), (2, 55.55, "2026-07-05"), (3, 40, "2026-07-01")]   # tie: earlier
    assert best[0]["unit"] == "kWh"


def test_weeks_months_years_with_running_period():
    rows = [day("2025-12-29", 10), day("2025-12-31", 10),    # ISO 2026-W01 (Mon 29 Dec 2025)
            day("2026-01-01", 10),
            day("2026-07-13", 30), day("2026-07-14", 30), day("2026-07-15", 20)]   # current week
    r = run(rows)["rankings"]
    assert r["best_week"][0] == {"rank": 1, "value": 80, "unit": "kWh", "period": "2026-W29"}
    assert r["best_week"][1]["period"] == "2026-W01" and r["best_week"][1]["value"] == 30
    assert r["best_month"][0]["period"] == "2026-07" and r["best_month"][0]["value"] == 80
    assert [m["period"] for m in r["best_month"]] == ["2026-07", "2025-12", "2026-01"]
    assert r["best_year"][0] == {"rank": 1, "value": 90, "unit": "kWh", "period": "2026"}
    assert r["best_year"][1]["period"] == "2025"


def test_peak_longest_earliest():
    rows = [
        day("2026-06-01", 50, 9870, ep(2026, 6, 1, 5, 41), ep(2026, 6, 1, 20, 41),
            ep(2026, 6, 1, 13, 0)),
        day("2026-06-02", 40, 8000, ep(2026, 6, 2, 6, 10), ep(2026, 6, 2, 19, 0), None),
        day("2025-06-02", 40, 9999, ep(2025, 6, 2, 4, 0), ep(2025, 6, 2, 21, 0), None),
        day("2026-06-03", 0, 0, None, None),
    ]
    r = run(rows)["rankings"]
    assert r["peak_power"][0] == {"rank": 1, "value": 9999, "unit": "W", "ts": None,
                                  "date": "2025-06-02"}
    assert r["peak_power"][1]["ts"] == aw.iso(ep(2026, 6, 1, 13, 0))
    assert len(r["peak_power"]) == 3 and r["peak_power"][2]["value"] == 8000   # 0 W excluded
    assert r["longest_day"][0]["value"] == 17 * 3600 and r["longest_day"][0]["date"] == "2025-06-02"
    assert r["longest_day"][0]["start"] == aw.iso(ep(2025, 6, 2, 4, 0))
    # earliest start: current year only, local time
    assert [e["date"] for e in r["earliest_start"]] == ["2026-06-01", "2026-06-02"]
    assert r["earliest_start"][0]["value"] == "05:41" and r["earliest_start"][0]["rank"] == 1


def test_milestones_reached_and_next():
    ms = [{"kwh": 21000, "ts": ep(2026, 7, 1, 12, 0)}]
    m = run([], ms, total=21307.9)["milestones"]
    assert m["reached"][-1] == {"kwh": 21000, "ts": aw.iso(ep(2026, 7, 1, 12, 0))}
    assert m["reached"][0] == {"kwh": 1000, "ts": None} and len(m["reached"]) == 21
    assert m["next"] == {"kwh": 22000, "remaining_kwh": 692.1}


def test_co2_and_money():
    rows = [day("2026-07-15", 10), day("2026-07-02", 20), day("2026-01-05", 30),
            day("2025-12-31", 99)]
    r = run(rows, total=1000.0)
    assert r["co2"] == {"factor_kg_kwh": 0.38, "today_kg": 3.8, "year_kg": 22.8, "total_kg": 380.0}
    assert r["money"] == {"tariff_per_kwh": 0.081, "currency": "EUR", "today": 0.81,
                          "month": 2.43, "year": 4.86, "total": 81.0}
    assert r["tracking_since"] == "2025-12-31"
