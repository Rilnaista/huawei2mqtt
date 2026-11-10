"""Pure helpers for the rc4 insights (no I/O): day context, daily summary, early-warning rules.

Contract: topic-spec.md "Erweiterung rc4".
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from . import awards as aw

MIN_BASELINE_DAYS = 7
BASELINE_DAYS = 30
MIN_MONTH_DAYS = 5
INSULATION_MIN_MOHM = 0.1
YIELD_MIN_FRACTION = 0.30


def _r(x: float | None, n: int) -> float | None:
    return None if x is None else round(float(x), n)


def same_day_last_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:          # 29.02. -> 28.02.
        return d.replace(year=d.year - 1, day=28)


def context(rows: list[dict[str, Any]], day: str) -> dict[str, Any]:
    """Day figures from ``daily`` rows (plus live overlay done by the caller).

    Only rows with a non-NULL ``yield_kwh`` count (rows from nights without production have none).
    Ranks: 1 + number of strictly better days (ties share the rank).
    """
    d = date.fromisoformat(day)
    by = {r["date"]: r["yield_kwh"] for r in rows if r.get("yield_kwh") is not None}
    cur = by.get(day)
    prev = by.get((d - timedelta(days=1)).isoformat())
    ly = by.get(same_day_last_year(d).isoformat())
    month_vals = [(k, v) for k, v in by.items() if int(k[5:7]) == d.month]
    others = [v for k, v in month_vals if k != day]
    avg = sum(others) / len(others) if len(others) >= MIN_MONTH_DAYS else None
    rank_all = rank_month = None
    if cur is not None:
        rank_all = 1 + sum(1 for v in by.values() if v > cur)
        rank_month = 1 + sum(1 for _, v in month_vals if v > cur)
    return {"date": day, "yield_kwh": _r(cur, 2), "yesterday_kwh": _r(prev, 2),
            "last_year_same_day_kwh": _r(ly, 2), "avg_month_kwh": _r(avg, 2),
            "rank_all_time": rank_all, "rank_month": rank_month,
            "days_all_time": len(by), "days_month": len(month_vals)}


def summary_payload(ctx: dict[str, Any], formatted_day: dict[str, Any] | None, *, tariff: float,
                    currency: str, co2_factor: float, now_epoch: float) -> dict[str, Any]:
    fd = formatted_day or {}
    y = ctx.get("yield_kwh") or 0.0
    return {**ctx, "peak_power_w": fd.get("peak_power_w"), "peak_ts": fd.get("peak_ts"),
            "first_production_ts": fd.get("first_production_ts"),
            "last_production_ts": fd.get("last_production_ts"),
            "production_s": fd.get("production_s"),
            "money": round(y * tariff, 2), "co2_kg": round(y * co2_factor, 1),
            "currency": currency, "ts": aw.iso(now_epoch)}


# --------------------------------------------------------------------------- warnings
def _prior(day: date, n: int = BASELINE_DAYS) -> list[str]:
    return [(day - timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def eval_insulation(day: str, med: dict[str, float], ratio: float
                    ) -> tuple[str, float | None, float | None]:
    """-> (status, value, baseline); status = hit | ok | skip."""
    val = med.get(day)
    base_vals = [med[k] for k in _prior(date.fromisoformat(day)) if k in med]
    if val is None or len(base_vals) < MIN_BASELINE_DAYS:
        return "skip", val, None
    base = statistics.median(base_vals)
    hit = val < ratio * base or val < INSULATION_MIN_MOHM
    return ("hit" if hit else "ok"), val, base


class _String:
    """Scored days and baselines for the string-imbalance rule."""

    def __init__(self, share: dict[str, float], yields: dict[str, float], dev: float) -> None:
        self.share, self.yields, self.dev = share, yields, dev
        self._scored: dict[str, bool] = {}

    def scored(self, day: str) -> bool:
        if day not in self._scored:
            ok = False
            if day in self.share and self.yields.get(day) is not None:
                prior = [self.yields[k] for k in _prior(date.fromisoformat(day))
                         if self.yields.get(k)]
                ok = bool(prior) and self.yields[day] >= YIELD_MIN_FRACTION * statistics.median(prior)
            self._scored[day] = ok
        return self._scored[day]

    def baseline(self, day: str) -> float | None:
        vals = [self.share[k] for k in _prior(date.fromisoformat(day)) if self.scored(k)]
        return statistics.median(vals) if len(vals) >= MIN_BASELINE_DAYS else None

    def hit(self, day: str) -> bool | None:
        b = self.baseline(day)
        if b is None or not self.scored(day):
            return None
        return abs(self.share[day] - b) > self.dev


def eval_string(day: str, share: dict[str, float], yields: dict[str, float], dev: float
                ) -> tuple[str, float | None, float | None]:
    """Share = energy fraction of string 1 (0..1). Active needs a hit on this AND the previous
    scored day (2-day rule). status = hit | ok | skip; value/baseline in percent."""
    st = _String(share, yields, dev)
    h = st.hit(day)
    if h is None:
        return "skip", None, None
    value, base = share[day] * 100, st.baseline(day) * 100        # type: ignore[operator]
    if not h:
        return "ok", value, base
    d = date.fromisoformat(day)
    for i in range(1, 31):
        p = (d - timedelta(days=i)).isoformat()
        if st.scored(p):
            if st.hit(p):
                return "hit", value, base
            break
    return "ok", value, base
