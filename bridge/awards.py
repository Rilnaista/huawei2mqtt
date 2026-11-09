"""Awards: pure functions over the ``daily`` / ``milestones`` rows (topic-spec rc2 "Awards-Schema").

Input rows are plain dicts as stored by history.py (timestamps = epoch seconds).
No I/O here: everything is deterministic given ``now``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Callable, Iterable

TOP = 3


def iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(r: dict[str, Any], key: str, ts_key: str) -> bool:
    v = r.get(key)
    if v is not None:
        return bool(v)
    # unknown flag: only imported rows (history from the inverter/portal) count as observed
    return r.get("source") == "import" and r.get(ts_key) is not None


def start_observed(r: dict[str, Any]) -> bool:
    return _flag(r, "start_observed", "first_production_ts")


def end_observed(r: dict[str, Any]) -> bool:
    return _flag(r, "end_observed", "last_production_ts")


def _top(items: Iterable[tuple[Any, dict[str, Any]]], reverse: bool = True) -> list[dict[str, Any]]:
    """items = (sort_value, payload-without-rank). Ties keep input order (earlier date first)."""
    ordered = sorted(items, key=lambda it: it[0], reverse=reverse)   # sort is stable
    return [{"rank": i + 1, **payload} for i, (_, payload) in enumerate(ordered[:TOP])]


def _periods(rows: list[dict[str, Any]], keyf: Callable[[date], str]) -> dict[str, float]:
    sums: dict[str, float] = defaultdict(float)
    for r in rows:
        y = r.get("yield_kwh")
        if y:
            sums[keyf(date.fromisoformat(r["date"]))] += float(y)
    return sums


def _period_rank(sums: dict[str, float]) -> list[dict[str, Any]]:
    items = [(v, {"value": round(v, 2), "unit": "kWh", "period": k})
             for k, v in sorted(sums.items()) if v > 0]
    return _top(items)


def compute(rows: list[dict[str, Any]], milestone_rows: list[dict[str, Any]], *,
            now: datetime, tz: tzinfo, step_kwh: int, total_kwh: float | None,
            tariff: float, currency: str, co2_factor: float) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: r["date"])
    today = now.astimezone(tz).date()
    t_s, m_s, y_s = today.isoformat(), today.strftime("%Y-%m"), today.strftime("%Y")

    best_day = _top([(float(r["yield_kwh"]), {"value": round(float(r["yield_kwh"]), 2),
                                              "unit": "kWh", "date": r["date"]})
                     for r in rows if r.get("yield_kwh")])
    best_week = _period_rank(_periods(
        rows, lambda d: f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"))
    best_month = _period_rank(_periods(rows, lambda d: d.strftime("%Y-%m")))
    best_year = _period_rank(_periods(rows, lambda d: d.strftime("%Y")))

    peak = _top([(float(r["peak_power_w"]), {"value": int(round(float(r["peak_power_w"]))),
                                             "unit": "W", "ts": iso(r.get("peak_ts")),
                                             "date": r["date"]})
                 for r in rows if r.get("peak_power_w") and r["peak_power_w"] > 0])

    longest = []
    for r in rows:
        a, b = r.get("first_production_ts"), r.get("last_production_ts")
        if a is not None and b is not None and b > a and start_observed(r) and end_observed(r):
            longest.append((b - a, {"value": int(b - a), "unit": "s", "date": r["date"],
                                    "start": iso(a), "end": iso(b)}))
    longest_day = _top(longest)

    earliest = []
    for r in rows:
        a = r.get("first_production_ts")
        if a is None or not start_observed(r) or not r["date"].startswith(y_s):
            continue
        local = datetime.fromtimestamp(a, tz=timezone.utc).astimezone(tz)
        earliest.append((local.hour * 3600 + local.minute * 60 + local.second,
                         {"value": local.strftime("%H:%M"), "date": r["date"], "ts": iso(a)}))
    earliest_start = _top(earliest, reverse=False)

    # milestones: all multiples up to the device counter; timestamp only where we saw the crossing
    seen = {int(m["kwh"]): iso(m.get("ts")) for m in milestone_rows}
    step = int(step_kwh)
    nxt: dict[str, Any] | None = None
    if total_kwh is not None and step > 0:
        n = int(total_kwh // step)
        reached = [{"kwh": k * step, "ts": seen.get(k * step)} for k in range(1, n + 1)]
        nxt = {"kwh": (n + 1) * step, "remaining_kwh": round((n + 1) * step - total_kwh, 2)}
    else:
        reached = [{"kwh": k, "ts": v} for k, v in sorted(seen.items())]

    def ysum(prefix: str) -> float:
        return sum(float(r["yield_kwh"] or 0) for r in rows if r["date"].startswith(prefix))

    today_kwh, month_kwh, year_kwh = ysum(t_s), ysum(m_s), ysum(y_s)
    total = float(total_kwh) if total_kwh is not None else 0.0

    return {
        "generated": iso(now.timestamp()),
        "tracking_since": rows[0]["date"] if rows else None,
        "rankings": {"best_day": best_day, "best_week": best_week, "best_month": best_month,
                     "best_year": best_year, "peak_power": peak, "longest_day": longest_day,
                     "earliest_start": earliest_start},
        "milestones": {"step_kwh": step, "reached": reached, "next": nxt},
        "co2": {"factor_kg_kwh": co2_factor,
                "today_kg": round(today_kwh * co2_factor, 1),
                "year_kg": round(year_kwh * co2_factor, 1),
                "total_kg": round(total * co2_factor, 1)},
        "money": {"tariff_per_kwh": tariff, "currency": currency,
                  "today": round(today_kwh * tariff, 2), "month": round(month_kwh * tariff, 2),
                  "year": round(year_kwh * tariff, 2), "total": round(total * tariff, 2)},
    }
