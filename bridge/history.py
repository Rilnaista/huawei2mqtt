"""History store (SQLite, stdlib): samples / samples_5m / daily / milestones. Contract: topic-spec rc2+rc3.

* Fed ONLY from successful LIVE cycles (``record``) - no extra Modbus traffic.
* RAM buffer, committed every ``flush_interval`` s (60) and at shutdown (SD card friendly).
* All DB I/O runs in a worker thread (asyncio.to_thread); DB errors are never fatal.
* ``daily`` and ``samples_5m`` are kept forever. ``samples`` (10 s) older than ``history_days``
  are condensed into 5-minute means in ``samples_5m`` (rc3) instead of being deleted.
* ``daily.start_observed`` / ``end_observed`` (INTEGER 0/1, NULL = unknown): production start/end
  were really observed. Rule: start observed = this day a NON-producing sample (input_power_w <= 0)
  was seen before the first producing one; end observed = a producing sample was directly followed
  (same process run, same day) by a non-producing one; later production resets it to 0. A bridge
  start in the middle of production therefore yields 0. ``first/last_production_ts`` stay internal
  (they also gate "daily values only after production start"). NULL flags count as observed only
  for ``source='import'`` rows that have the respective timestamps; an importer may also set the
  flags explicitly to 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Callable

from . import awards as aw
from . import insights as ins
from . import registers as rg
from .config import Config

log = logging.getLogger("bridge.history")

DB_NAME = "huawei2mqtt.db"
DEFAULT_FIELDS = ["input_power_w", "active_power_w"]
MAX_POINTS = 2000
DEFAULT_POINTS = 500
MAX_FIELDS = 10
MAX_DAILY_SPAN = 3660
DEFAULT_DAILY_SPAN = 31
BUFFER_CAP = 5000            # samples kept in RAM while the DB is failing
MAX_MILESTONES_PER_SAMPLE = 50
BUCKET_S = 300               # samples_5m resolution
MAX_EXPORT_DAYS = 400
COMPACT_CHUNK_S = 86400      # one transaction per day of backlog

_NUMERIC_DERIVED = {"pv1_power_w": "W", "pv2_power_w": "W", "running": ""}
_IDENT = re.compile(r"^[a-z0-9_]+$")


def _build_fields() -> list[tuple[str, str, str]]:
    """(name, unit, group) of every numeric LIVE field (samples columns)."""
    out: list[tuple[str, str, str]] = []
    for group in ("pv", "grid", "energy", "inverter"):
        for name in rg.GROUP_FIELDS[group]:
            reg = rg.BY_FIELD.get(name)
            if reg is not None:
                if reg.typ in ("STR", "EPOCH"):
                    continue
                out.append((name, reg.unit, group))
            elif name in _NUMERIC_DERIVED:
                out.append((name, _NUMERIC_DERIVED[name], group))
    return out


SAMPLE_FIELDS = _build_fields()
FIELD_NAMES = [f[0] for f in SAMPLE_FIELDS]
AVAILABLE = [{"name": n, "unit": u, "group": g} for n, u, g in SAMPLE_FIELDS]
assert all(_IDENT.match(n) for n in FIELD_NAMES)


class RequestError(Exception):
    """Invalid history request; ``reason`` is a stable machine key."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class HistoryUnavailable(Exception):
    pass


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _fmt_step(step: float) -> float | int:
    return int(step) if step == int(step) else round(step, 3)


class History:
    def __init__(self, cfg: Config, tz_name: str = "Europe/Berlin",
                 now: Callable[[], datetime] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.path = cfg.data_dir / DB_NAME
        self._tz: tzinfo = rg.local_tz(tz_name) or timezone.utc
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._clock = clock
        self.flush_interval = 60.0
        self.purge_interval = 3600.0
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._buf: list[tuple[int, dict[str, float | None]]] = []
        self._ms_buf: list[tuple[int, int]] = []
        self._dirty: dict[str, dict[str, Any]] = {}
        self._day: dict[str, Any] | None = None
        self._seen = False
        self._nonprod_date: str | None = None   # day on which a non-producing sample was seen
        self._prev_prod = False                 # previous sample (this run) was producing
        self._last_total: float | None = None
        self.day_rolled = False
        self._stop = asyncio.Event()
        self._fail_logged = 0
        self._stats: dict[str, Any] = {"samples": 0, "oldest": None, "db_size_bytes": 0}

    # ------------------------------------------------------------------ helpers
    def _local_date(self, dt: datetime) -> str:
        return dt.astimezone(self._tz).date().isoformat()

    def _parse_dt(self, val: Any, err: str) -> datetime:
        if not isinstance(val, str) or not val.strip():
            raise RequestError(err)
        try:
            dt = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
        except ValueError:
            raise RequestError(err) from None
        return dt.replace(tzinfo=self._tz) if dt.tzinfo is None else dt

    def _parse_day(self, val: Any, err: str) -> date:
        if not isinstance(val, str):
            raise RequestError(err)
        try:
            return date.fromisoformat(val.strip())
        except ValueError:
            raise RequestError(err) from None

    # ------------------------------------------------------------------ DB (sync, worker thread)
    def _open(self) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cols = ", ".join(f'"{n}" REAL' for n in FIELD_NAMES)
            for tbl in ("samples", "samples_5m"):
                conn.execute(f"CREATE TABLE IF NOT EXISTS {tbl} (ts INTEGER PRIMARY KEY, {cols})")
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
                for n in FIELD_NAMES:
                    if n not in have:
                        conn.execute(f'ALTER TABLE {tbl} ADD COLUMN "{n}" REAL')
            conn.execute("CREATE TABLE IF NOT EXISTS daily (date TEXT PRIMARY KEY, yield_kwh REAL, "
                         "total_yield_kwh REAL, peak_power_w REAL, peak_ts INTEGER, "
                         "first_production_ts INTEGER, last_production_ts INTEGER, "
                         "source TEXT NOT NULL DEFAULT 'bridge', "
                         "start_observed INTEGER, end_observed INTEGER)")
            have_d = {r[1] for r in conn.execute("PRAGMA table_info(daily)")}
            for n in ("start_observed", "end_observed"):
                if n not in have_d:
                    conn.execute(f"ALTER TABLE daily ADD COLUMN {n} INTEGER")
            conn.execute("CREATE TABLE IF NOT EXISTS milestones (kwh INTEGER PRIMARY KEY, "
                         "ts INTEGER)")
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    def _write(self, rows: list[tuple[int, dict[str, Any]]], days: dict[str, dict[str, Any]],
               ms: list[tuple[int, int]]) -> None:
        with self._lock:
            c = self._db()
            try:
                if rows:
                    names = ", ".join(f'"{n}"' for n in FIELD_NAMES)
                    marks = ", ".join("?" * (len(FIELD_NAMES) + 1))
                    c.executemany(f"INSERT OR REPLACE INTO samples (ts, {names}) VALUES ({marks})",
                                  [(ts, *[r.get(n) for n in FIELD_NAMES]) for ts, r in rows])
                for d in days.values():
                    c.execute(
                        "INSERT INTO daily (date, yield_kwh, total_yield_kwh, peak_power_w, "
                        "peak_ts, first_production_ts, last_production_ts, source, "
                        "start_observed, end_observed) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                        "yield_kwh=excluded.yield_kwh, total_yield_kwh=excluded.total_yield_kwh, "
                        "peak_power_w=excluded.peak_power_w, peak_ts=excluded.peak_ts, "
                        "first_production_ts=excluded.first_production_ts, "
                        "last_production_ts=excluded.last_production_ts, source=excluded.source, "
                        "start_observed=excluded.start_observed, "
                        "end_observed=excluded.end_observed",
                        (d["date"], d["yield_kwh"], d["total_yield_kwh"], d["peak_power_w"],
                         d["peak_ts"], d["first_production_ts"], d["last_production_ts"],
                         d["source"], d.get("start_observed"), d.get("end_observed")))
                if ms:
                    c.executemany("INSERT OR IGNORE INTO milestones (kwh, ts) VALUES (?, ?)", ms)
                c.commit()
            except Exception:
                c.rollback()
                raise

    def _load_state(self, today: str) -> None:
        with self._lock:
            c = self._db()
            row = c.execute("SELECT date, yield_kwh, total_yield_kwh, peak_power_w, peak_ts, "
                            "first_production_ts, last_production_ts, source, start_observed, end_observed "
                "FROM daily "
                            "WHERE date=?", (today,)).fetchone()
            last = c.execute("SELECT total_yield_kwh FROM daily WHERE total_yield_kwh IS NOT NULL "
                             "ORDER BY date DESC LIMIT 1").fetchone()
        if row:
            self._day = dict(zip(_DAY_KEYS, row))
        if last:
            self._last_total = last[0]

    def _compact_chunk(self, cutoff: int) -> int:
        """Condense the oldest day of ``samples`` older than ``cutoff`` (multiple of 300) into
        ``samples_5m`` (existing buckets win) and delete it; one transaction. Returns rows moved."""
        with self._lock:
            c = self._db()
            m = c.execute("SELECT MIN(ts) FROM samples WHERE ts < ?", (cutoff,)).fetchone()[0]
            if m is None:
                return 0
            start = m // BUCKET_S * BUCKET_S
            end = min(start + COMPACT_CHUNK_S, cutoff)
            names = ", ".join(f'"{n}"' for n in FIELD_NAMES)
            avgs = ", ".join(f'AVG("{n}")' for n in FIELD_NAMES)
            try:
                c.execute(f"INSERT OR IGNORE INTO samples_5m (ts, {names}) "
                          f"SELECT (ts / {BUCKET_S}) * {BUCKET_S}, {avgs} FROM samples "
                          f"WHERE ts >= ? AND ts < ? GROUP BY ts / {BUCKET_S}", (start, end))
                n = c.execute("DELETE FROM samples WHERE ts >= ? AND ts < ?", (start, end)).rowcount
                c.commit()
            except Exception:
                c.rollback()
                raise
            return n

    def _collect_stats(self) -> dict[str, Any]:
        with self._lock:
            c = self._db()
            n, detail = c.execute("SELECT COUNT(*), MIN(ts) FROM samples").fetchone()
            n5, old5 = c.execute("SELECT COUNT(*), MIN(ts) FROM samples_5m").fetchone()
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += self.path.with_name(self.path.name + suffix).stat().st_size
            except OSError:
                pass
        olds = [x for x in (detail, old5) if x is not None]
        return {"samples": n, "samples_5m": n5, "oldest": aw.iso(min(olds) if olds else None),
                "detail_oldest": aw.iso(detail), "db_size_bytes": size}

    def _bucket_acc(self, table: str, f: int, t: int, step: float, points: int,
                    fields: list[str], acc: dict[tuple[int, str], list[float]]) -> None:
        sel = ", ".join(f'AVG("{n}"), MIN("{n}"), MAX("{n}"), COUNT("{n}")' for n in fields)
        sql = (f"SELECT MIN(CAST((ts - ?) / ? AS INTEGER), ?) AS b, {sel} FROM {table} "
               "WHERE ts >= ? AND ts <= ? GROUP BY b")
        with self._lock:
            rows = self._db().execute(sql, (f, step, points - 1, f, t)).fetchall()
        for row in rows:
            b = row[0]
            for i, n in enumerate(fields):
                avg, lo, hi, cnt = row[1 + 4 * i: 5 + 4 * i]
                if avg is None or not cnt:
                    continue
                a = acc.get((b, n))
                if a is None:
                    acc[(b, n)] = [avg * cnt, lo, hi, cnt]
                else:
                    a[0] += avg * cnt
                    a[1] = min(a[1], lo)
                    a[2] = max(a[2], hi)
                    a[3] += cnt

    def _query_samples(self, f: int, t: int, fields: list[str], points: int,
                       buf: list[tuple[int, dict[str, Any]]] | None = None) -> dict[str, Any]:
        """DB buckets (avg/min/max) merged with ``buf`` (snapshot of not yet committed rows)."""
        step = (t - f) / points
        series = {n: {"avg": [None] * points, "min": [None] * points, "max": [None] * points}
                  for n in fields}
        with self._lock:
            detail = self._db().execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        buf_min = min((ts for ts, _ in buf or ()), default=None)
        cands = [x for x in (detail, buf_min) if x is not None]
        d0 = min(cands) if cands else None          # oldest fine-resolution timestamp
        # acc[(bucket, field)] = [sum, min, max, count]
        acc: dict[tuple[int, str], list[float]] = {}
        if d0 is None or d0 > f:
            self._bucket_acc("samples_5m", f, t if d0 is None else min(t, d0 - 1),
                             step, points, fields, acc)
        if d0 is not None and d0 <= t:
            self._bucket_acc("samples", f, t, step, points, fields, acc)
        res = 300 if (d0 is None or d0 > t) else (10 if d0 <= f else "mixed")
        for ts, r in buf or ():
            if ts < f or ts > t:
                continue
            b = min(int((ts - f) / step), points - 1)
            for n in fields:
                v = r.get(n)
                if v is None:
                    continue
                a = acc.get((b, n))
                if a is None:
                    acc[(b, n)] = [v, v, v, 1]
                else:
                    a[0] += v
                    a[1] = min(a[1], v)
                    a[2] = max(a[2], v)
                    a[3] += 1
        for (b, n), (sm, lo, hi, cnt) in acc.items():
            series[n]["avg"][b] = round(sm / cnt, 3)
            series[n]["min"][b] = round(lo, 3)
            series[n]["max"][b] = round(hi, 3)
        return {"from": aw.iso(f), "to": aw.iso(t), "step_s": _fmt_step(step), "fields": fields,
                "t": [int((f + i * step) * 1000) for i in range(points)], "series": series,
                "resolution_s": res, "available": AVAILABLE}

    def _query_daily(self, d_from: str, d_to: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db().execute(
                "SELECT date, yield_kwh, total_yield_kwh, peak_power_w, peak_ts, "
                "first_production_ts, last_production_ts, source, start_observed, end_observed "
                "FROM daily "
                "WHERE date >= ? AND date <= ? ORDER BY date", (d_from, d_to)).fetchall()
        return [dict(zip(_DAY_KEYS, r)) for r in rows]

    def _query_milestones(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db().execute("SELECT kwh, ts FROM milestones ORDER BY kwh").fetchall()
        return [{"kwh": k, "ts": t} for k, t in rows]

    def _clear(self, scope: str) -> dict[str, int]:
        out = {"samples": 0, "samples_5m": 0, "daily": 0, "milestones": 0}
        with self._lock:
            c = self._db()
            if scope in ("samples", "all"):
                out["samples"] = c.execute("DELETE FROM samples").rowcount
                out["samples_5m"] = c.execute("DELETE FROM samples_5m").rowcount
            if scope in ("daily", "all"):
                out["daily"] = c.execute("DELETE FROM daily").rowcount
                out["milestones"] = c.execute("DELETE FROM milestones").rowcount
            c.commit()
            try:
                c.execute("VACUUM")
            except sqlite3.Error:
                pass
        return out

    def _close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------ recording (event loop, RAM only)
    def _new_day(self, d: str) -> dict[str, Any]:
        return {"date": d, "yield_kwh": None, "total_yield_kwh": None, "peak_power_w": None,
                "peak_ts": None, "first_production_ts": None, "last_production_ts": None,
                "source": "bridge", "start_observed": 0, "end_observed": 0}

    def record(self, ts: datetime, v: dict[str, Any]) -> None:
        """One successful LIVE cycle. ``v`` = flat dict of the LIVE group fields."""
        epoch = int(ts.timestamp())
        d = self._local_date(ts)
        day = self._day
        if day is None or day["date"] != d:
            if day is not None:
                self.day_rolled = True
            day = self._day = self._new_day(d)
        pw = _num(v.get("input_power_w"))
        if pw is not None and pw > 0:
            if day["first_production_ts"] is None:
                day["first_production_ts"] = epoch
                day["start_observed"] = 1 if self._nonprod_date == d else 0
            day["last_production_ts"] = epoch
            day["end_observed"] = 0          # production (again) -> end not yet seen
            self._prev_prod = True
        elif pw is not None:
            self._nonprod_date = d
            if self._prev_prod and day["last_production_ts"] is not None:
                day["end_observed"] = 1
            self._prev_prod = False
        # After midnight the inverter keeps reporting yesterday's daily values until it resets
        # them (at the latest at production start): only accept them once production was seen today.
        if day["first_production_ts"] is not None:
            y = _num(v.get("daily_yield_kwh"))
            if y is not None:
                day["yield_kwh"] = max(day["yield_kwh"] or 0.0, y)
            p = _num(v.get("peak_power_today_w"))
            if p is not None:
                known = day["peak_power_w"]
                if known is None:
                    day["peak_power_w"] = p
                    # bridge started in the middle of production without stored peak_ts: unknown
                    day["peak_ts"] = epoch if (self._seen or not pw or pw <= 0 or p <= 0) else None
                elif p > known:
                    day["peak_power_w"] = p
                    day["peak_ts"] = epoch
        total = _num(v.get("total_yield_kwh"))
        if total is not None:
            day["total_yield_kwh"] = total
            self._check_milestones(self._last_total, total, epoch)
            self._last_total = total
        self._seen = True
        self._dirty[d] = dict(day)
        self._buf.append((epoch, {n: _num(v.get(n)) for n in FIELD_NAMES}))
        if len(self._buf) > BUFFER_CAP:
            del self._buf[: len(self._buf) - BUFFER_CAP]

    def _check_milestones(self, prev: float | None, cur: float, epoch: int) -> None:
        step = int(self.cfg.milestone_step_kwh)
        if prev is None or step <= 0 or cur <= prev:
            return
        lo, hi = int(prev // step), int(cur // step)
        if hi > lo:
            for k in range(max(lo + 1, hi - MAX_MILESTONES_PER_SAMPLE + 1), hi + 1):
                self._ms_buf.append((k * step, epoch))
                log.info("milestone reached: %d kWh", k * step)

    def peak_ts_iso(self) -> str | None:
        """Time of today's peak (energy.peak_power_today_ts); None if unknown."""
        day = self._day
        if day is None or day["date"] != self._local_date(self._now()):
            return None
        return aw.iso(day["peak_ts"])

    # ------------------------------------------------------------------ lifecycle (async)
    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._open)
            await asyncio.to_thread(self._load_state, self._local_date(self._now()))
            await self.purge()
            await self._refresh_stats()
            log.info("history database ready: %s", self.path)
        except Exception as exc:  # noqa: BLE001 - never fatal
            self._conn = None
            log.warning("history database unavailable (%s: %s); continuing without storage",
                        type(exc).__name__, exc)

    def _warn_failure(self, what: str, exc: Exception) -> None:
        if self._fail_logged % 10 == 0:
            log.warning("history %s failed (%s: %s); will retry", what, type(exc).__name__, exc)
        self._fail_logged += 1

    async def flush(self) -> None:
        rows, self._buf = self._buf, []
        days, self._dirty = self._dirty, {}
        ms, self._ms_buf = self._ms_buf, []
        if not (rows or days or ms):
            return
        try:
            await asyncio.to_thread(self._write, rows, days, ms)
            self._fail_logged = 0
        except Exception as exc:  # noqa: BLE001 - DB full / locked / ...: keep going
            self._warn_failure("write", exc)
            self._buf = (rows + self._buf)[-BUFFER_CAP:]
            for k, d in days.items():
                self._dirty.setdefault(k, d)
            self._ms_buf = ms + self._ms_buf

    async def purge(self) -> None:
        """Condense old fine samples into 5-minute means (nothing is deleted; name kept)."""
        now = int(self._now().timestamp())
        cutoff = (now - int(self.cfg.history_days) * 86400) // BUCKET_S * BUCKET_S
        total = 0
        try:
            while True:
                n = await asyncio.to_thread(self._compact_chunk, cutoff)
                if not n:
                    break
                total += n
            if total:
                log.info("history: condensed %d samples into 5-minute values", total)
        except Exception as exc:  # noqa: BLE001
            self._warn_failure("compaction", exc)

    async def _refresh_stats(self) -> None:
        try:
            self._stats = await asyncio.to_thread(self._collect_stats)
        except Exception as exc:  # noqa: BLE001
            self._warn_failure("stats", exc)

    def status(self) -> dict[str, Any]:
        return {"days": self.cfg.history_days, **self._stats}

    async def run(self) -> None:
        last_purge = self._clock()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self.flush_interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            await self.flush()
            if self._clock() - last_purge >= self.purge_interval:
                last_purge = self._clock()
                await self.purge()
            await self._refresh_stats()

    async def close(self) -> None:
        self._stop.set()
        await self.flush()
        await asyncio.to_thread(self._close)

    # ------------------------------------------------------------------ queries (async)
    def _require_db(self) -> None:
        if self._conn is None:
            try:
                self._open()
            except Exception as exc:  # noqa: BLE001
                raise HistoryUnavailable(str(exc)) from exc

    async def _run_db(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(self._guarded, fn, *args)
        except HistoryUnavailable:
            raise
        except sqlite3.Error as exc:
            raise HistoryUnavailable(str(exc)) from exc

    def _guarded(self, fn: Callable[..., Any], *args: Any) -> Any:
        self._require_db()
        return fn(*args)

    def parse_samples(self, from_: Any, to: Any, fields: Any, points: Any) -> tuple[int, int, list[str], int]:
        now = self._now()
        t = self._parse_dt(to, "invalid_to") if to is not None else now
        if from_ is not None:
            f = self._parse_dt(from_, "invalid_from")
        else:
            f = datetime.combine(t.astimezone(self._tz).date(), dtime.min, tzinfo=self._tz)
        fi, ti = int(f.timestamp()), int(t.timestamp())
        if ti <= fi:
            raise RequestError("invalid_range")
        if fields is None:
            names = list(DEFAULT_FIELDS)
        else:
            if isinstance(fields, str):
                names = [x.strip() for x in fields.split(",") if x.strip()]
            elif isinstance(fields, list) and all(isinstance(x, str) for x in fields):
                names = list(fields)
            else:
                raise RequestError("invalid_fields")
            if not names:
                raise RequestError("invalid_fields")
            if len(set(names)) > MAX_FIELDS:
                raise RequestError("too_many_fields")
            names = list(dict.fromkeys(names))
            if any(n not in FIELD_NAMES for n in names):
                raise RequestError("unknown_field")
        if points is None:
            pts = DEFAULT_POINTS
        elif isinstance(points, bool):
            raise RequestError("invalid_points")
        elif isinstance(points, (int, float)) and points == int(points):
            pts = int(points)
        elif isinstance(points, str) and points.strip().isdigit():
            pts = int(points)
        else:
            raise RequestError("invalid_points")
        if pts < 1:
            raise RequestError("invalid_points")
        pts = max(1, min(pts, MAX_POINTS, ti - fi))   # at least 1 s per bucket
        return fi, ti, names, pts

    async def samples(self, from_: Any = None, to: Any = None, fields: Any = None,
                      points: Any = None) -> dict[str, Any]:
        args = self.parse_samples(from_, to, fields, points)
        snapshot = list(self._buf)      # taken in the event loop; DB I/O runs in a worker thread
        return await self._run_db(self._query_samples, *args, snapshot)

    def _overlay(self, rows: list[dict[str, Any]], d_from: str, d_to: str) -> list[dict[str, Any]]:
        """Replace/add rows with the (newer) in-RAM state of days not yet committed."""
        by = {r["date"]: r for r in rows}
        mem = list(self._dirty.values()) + ([self._day] if self._day else [])
        for d in mem:
            if d_from <= d["date"] <= d_to:
                by[d["date"]] = dict(d)
        return [by[k] for k in sorted(by)]

    @staticmethod
    def format_day(r: dict[str, Any]) -> dict[str, Any]:
        a, b = r.get("first_production_ts"), r.get("last_production_ts")
        so, eo = aw.start_observed(r), aw.end_observed(r)
        r2 = lambda x, n: None if x is None else round(x, n)   # noqa: E731
        return {"date": r["date"], "yield_kwh": r2(r.get("yield_kwh"), 2),
                "total_yield_kwh": r2(r.get("total_yield_kwh"), 2),
                "peak_power_w": r2(r.get("peak_power_w"), 0),
                "peak_ts": aw.iso(r.get("peak_ts")),
                "first_production_ts": aw.iso(a), "last_production_ts": aw.iso(b),
                "start_observed": so, "end_observed": eo,
                "production_s": (int(b - a) if so and eo and a is not None and b is not None
                                 and b >= a else None),
                "source": r.get("source") or "bridge"}

    async def daily(self, from_: Any = None, to: Any = None) -> dict[str, Any]:
        today = self._now().astimezone(self._tz).date()
        d_to = self._parse_day(to, "invalid_to") if to is not None else today
        d_from = (self._parse_day(from_, "invalid_from") if from_ is not None
                  else d_to - timedelta(days=DEFAULT_DAILY_SPAN - 1))
        if d_from > d_to:
            raise RequestError("invalid_range")
        if (d_to - d_from).days + 1 > MAX_DAILY_SPAN:
            raise RequestError("range_too_large")
        a, b = d_from.isoformat(), d_to.isoformat()
        rows = self._overlay(await self._run_db(self._query_daily, a, b), a, b)
        return {"days": [self.format_day(r) for r in rows]}

    async def awards(self, total_kwh: float | None) -> dict[str, Any]:
        rows = await self._run_db(self._query_daily, "0000-01-01", "9999-12-31")
        rows = self._overlay(rows, "0000-01-01", "9999-12-31")
        ms = await self._run_db(self._query_milestones)
        if total_kwh is None:       # fall back to the last stored device counter
            totals = [r["total_yield_kwh"] for r in rows if r.get("total_yield_kwh") is not None]
            total_kwh = totals[-1] if totals else None
        cfg = self.cfg
        return aw.compute(rows, ms, now=self._now(), tz=self._tz, step_kwh=cfg.milestone_step_kwh,
                          total_kwh=total_kwh, tariff=cfg.feed_in_tariff, currency=cfg.currency,
                          co2_factor=cfg.co2_factor)

    # ------------------------------------------------------------------ rc4: context / summary
    async def _all_days(self) -> list[dict[str, Any]]:
        rows = await self._run_db(self._query_daily, "0000-01-01", "9999-12-31")
        return self._overlay(rows, "0000-01-01", "9999-12-31")

    def today_str(self) -> str:
        return self._local_date(self._now())

    def day_yield(self, day: str) -> float | None:
        """Yield of ``day`` from the in-RAM state (live), None if not known there."""
        for d in list(self._dirty.values()) + ([self._day] if self._day else []):
            if d["date"] == day and d.get("yield_kwh") is not None:
                return d["yield_kwh"]
        return None

    async def context(self, day: Any = None) -> dict[str, Any]:
        d = self._parse_day(day, "invalid_date") if day is not None else self._now().astimezone(
            self._tz).date()
        return ins.context(await self._all_days(), d.isoformat())

    async def summary(self, day: str) -> dict[str, Any]:
        rows = await self._all_days()
        ctx = ins.context(rows, day)
        row = next((r for r in rows if r["date"] == day), None)
        cfg = self.cfg
        return ins.summary_payload(ctx, self.format_day(row) if row else None,
                                   tariff=cfg.feed_in_tariff, currency=cfg.currency,
                                   co2_factor=cfg.co2_factor,
                                   now_epoch=self._now().timestamp())

    # ------------------------------------------------------------------ rc4: warning metrics (SQL)
    def _day_bounds(self, day: date) -> tuple[int, int]:
        t0 = datetime.combine(day, dtime.min, tzinfo=self._tz)
        t1 = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=self._tz)
        return int(t0.timestamp()), int(t1.timestamp())

    def _query_metrics(self, days: list[date]) -> tuple[dict[str, float], dict[str, float]]:
        """Per local day: median insulation resistance during production (input > 100 W) and the
        energy share of string 1. Aggregated in SQL (window functions), both history levels; the
        5-minute level is only used before the oldest fine sample (no double counting)."""
        vals, params = [], []
        for d in days:
            t0, t1 = self._day_bounds(d)
            vals.append("(?,?,?)")
            params += [d.isoformat(), t0, t1]
        d_cte = "d(day,t0,t1) AS (VALUES " + ",".join(vals) + ")"
        lim = "(SELECT COALESCE(MIN(ts), 9223372036854775807) FROM samples)"
        sql_ins = (
            f"WITH {d_cte}, v AS ("
            "SELECT d.day AS day, s.insulation_resistance_mohm AS val FROM d JOIN samples s "
            "ON s.ts >= d.t0 AND s.ts < d.t1 WHERE s.input_power_w > 100 "
            "AND s.insulation_resistance_mohm IS NOT NULL "
            "UNION ALL SELECT d.day, s.insulation_resistance_mohm FROM d JOIN samples_5m s "
            f"ON s.ts >= d.t0 AND s.ts < d.t1 WHERE s.ts < {lim} AND s.input_power_w > 100 "
            "AND s.insulation_resistance_mohm IS NOT NULL), "
            "r AS (SELECT day, val, ROW_NUMBER() OVER (PARTITION BY day ORDER BY val) AS rn, "
            "COUNT(*) OVER (PARTITION BY day) AS n FROM v) "
            "SELECT day, AVG(val) FROM r WHERE rn IN ((n + 1) / 2, (n + 2) / 2) GROUP BY day")
        sql_str = (
            f"WITH {d_cte}, v AS ("
            "SELECT d.day AS day, s.pv1_power_w * 10.0 AS p1, s.pv2_power_w * 10.0 AS p2 "
            "FROM d JOIN samples s ON s.ts >= d.t0 AND s.ts < d.t1 "
            "WHERE s.pv1_power_w IS NOT NULL AND s.pv2_power_w IS NOT NULL "
            "UNION ALL SELECT d.day, s.pv1_power_w * 300.0, s.pv2_power_w * 300.0 "
            "FROM d JOIN samples_5m s ON s.ts >= d.t0 AND s.ts < d.t1 "
            f"WHERE s.ts < {lim} AND s.pv1_power_w IS NOT NULL AND s.pv2_power_w IS NOT NULL) "
            "SELECT day, SUM(p1), SUM(p2) FROM v GROUP BY day")
        with self._lock:
            c = self._db()
            med = {r[0]: float(r[1]) for r in c.execute(sql_ins, params) if r[1] is not None}
            share = {r[0]: r[1] / (r[1] + r[2]) for r in c.execute(sql_str, params)
                     if r[1] is not None and r[2] is not None and r[1] + r[2] > 0}
        return med, share

    async def warning_inputs(self, day: str) -> dict[str, Any]:
        """Metrics for ``day`` and the 61 days before it (baseline + previous scored day)."""
        d = date.fromisoformat(day)
        days = [d - timedelta(days=i) for i in range(61, -1, -1)]
        med, share = await self._run_db(self._query_metrics, days)
        a, b = days[0].isoformat(), day
        rows = self._overlay(await self._run_db(self._query_daily, a, b), a, b)
        yields = {r["date"]: r["yield_kwh"] for r in rows if r.get("yield_kwh") is not None}
        return {"insulation": med, "share": share, "yields": yields}

    # ------------------------------------------------------------------ rc4: CSV export
    def export_range(self, from_: Any, to: Any) -> tuple[int, int, str, str]:
        """-> (from_epoch, to_epoch, from_date, to_date) in local dates for the file name.
        A date-only ``to`` includes that whole day."""
        now = self._now()
        if to is None:
            t = now
        else:
            t = self._parse_dt(to, "invalid_to")
            if isinstance(to, str) and len(to.strip()) == 10:
                t += timedelta(days=1)
        if from_ is not None:
            f = self._parse_dt(from_, "invalid_from")
        else:
            f = datetime.combine(t.astimezone(self._tz).date(), dtime.min, tzinfo=self._tz)
        fi, ti = int(f.timestamp()), int(t.timestamp())
        if ti <= fi:
            raise RequestError("invalid_range")
        if ti - fi > MAX_EXPORT_DAYS * 86400:
            raise RequestError("range_too_large")
        last = t.astimezone(self._tz).date() - (timedelta(days=1) if (
            isinstance(to, str) and len(to.strip()) == 10) else timedelta(0))
        return fi, ti, f.astimezone(self._tz).date().isoformat(), last.isoformat()

    def export_fields(self, fields: Any) -> list[str]:
        if fields is None or (isinstance(fields, str) and not fields.strip()):
            return list(FIELD_NAMES)
        names = [x.strip() for x in str(fields).split(",") if x.strip()]
        if not names:
            raise RequestError("invalid_fields")
        names = list(dict.fromkeys(names))
        if any(n not in FIELD_NAMES for n in names):
            raise RequestError("unknown_field")
        return names

    def _export_slice(self, f: int, t: int, fields: list[str]) -> list[tuple[Any, ...]]:
        sel = ", ".join(f'"{n}"' for n in fields)
        with self._lock:
            c = self._db()
            d0 = c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
            rows = []
            if d0 is None or d0 > f:
                rows += c.execute(f"SELECT ts, {sel} FROM samples_5m WHERE ts >= ? AND ts <= ? "
                                  "AND ts < ? ORDER BY ts",
                                  (f, t, d0 if d0 is not None else 2**62)).fetchall()
            if d0 is not None and d0 <= t:
                rows += c.execute(f"SELECT ts, {sel} FROM samples WHERE ts >= ? AND ts <= ? "
                                  "ORDER BY ts", (f, t)).fetchall()
        return rows

    async def export_samples(self, f: int, t: int, fields: list[str]):
        """Async generator of raw rows ``(ts, *values)`` of both levels, one day per DB call
        (the lock is never held for a whole export)."""
        await self.flush()
        cur = f
        while cur <= t:
            end = min(cur + 86400 - 1, t)
            for row in await self._run_db(self._export_slice, cur, end, fields):
                yield row
            cur = end + 1

    def local_str(self, epoch: float | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        if epoch is None:
            return ""
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(self._tz).strftime(fmt)

    async def clear(self, scope: str) -> dict[str, Any]:
        if scope not in ("samples", "daily", "all"):
            raise RequestError("invalid_scope")
        if scope in ("samples", "all"):
            self._buf = []
        if scope in ("daily", "all"):
            self._dirty, self._ms_buf = {}, []
            self._day, self._last_total, self._seen = None, None, False
            self._nonprod_date, self._prev_prod = None, False
        deleted = await self._run_db(self._clear, scope)
        await self._refresh_stats()
        log.info("history cleared (scope=%s): %s", scope, deleted)
        return {"status": "ok", "deleted": deleted}


_DAY_KEYS = ("date", "yield_kwh", "total_yield_kwh", "peak_power_w", "peak_ts",
             "first_production_ts", "last_production_ts", "source", "start_observed", "end_observed")


async def handle_request(history: History, payload: bytes, total_kwh: float | None) -> dict[str, Any]:
    """`<base>/history/get` payload -> `<base>/history/result` dict (never raises)."""
    try:
        req = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"id": None, "type": None, "status": "rejected", "reason": "invalid_json"}
    if not isinstance(req, dict):
        return {"id": None, "type": None, "status": "rejected", "reason": "invalid_json"}
    rid = req.get("id")
    if not isinstance(rid, (str, int)) or isinstance(rid, bool):
        rid = None
    typ = req.get("type")
    base: dict[str, Any] = {"id": rid, "type": typ if isinstance(typ, str) else None}
    try:
        if typ == "samples":
            data = await history.samples(req.get("from"), req.get("to"), req.get("fields"),
                                         req.get("points"))
        elif typ == "daily":
            data = await history.daily(req.get("from"), req.get("to"))
        elif typ == "awards":
            data = await history.awards(total_kwh)
        else:
            raise RequestError("unknown_type")
    except RequestError as exc:
        return {**base, "status": "rejected", "reason": exc.reason}
    except HistoryUnavailable:
        return {**base, "status": "rejected", "reason": "history_unavailable"}
    return {**base, "status": "ok", **data}
