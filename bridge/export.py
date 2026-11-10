"""CSV export (rc4): UTF-8 with BOM, CRLF lines; ``semicolon`` = ';' and decimal comma (Excel de)."""

from __future__ import annotations

from typing import Any, AsyncIterator

from .history import History, RequestError

BOM = "﻿"


def parse_sep(value: str | None) -> str:
    if value in (None, "", "comma"):
        return ","
    if value == "semicolon":
        return ";"
    raise RequestError("invalid_sep")


def fmt_num(v: Any, sep: str) -> str:
    if v is None:
        return ""
    if isinstance(v, int):
        return str(v)
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s.replace(".", ",") if sep == ";" else s


def _line(cells: list[str], sep: str) -> str:
    return sep.join(cells) + "\r\n"


async def samples_csv(h: History, f: int, t: int, fields: list[str], sep: str
                      ) -> AsyncIterator[bytes]:
    yield (BOM + _line(["time_local", "epoch", *fields], sep)).encode("utf-8")
    buf: list[str] = []
    async for row in h.export_samples(f, t, fields):
        buf.append(_line([h.local_str(row[0]), str(row[0]), *[fmt_num(v, sep) for v in row[1:]]],
                         sep))
        if len(buf) >= 2000:
            yield "".join(buf).encode("utf-8")
            buf = []
    if buf:
        yield "".join(buf).encode("utf-8")


async def daily_csv(h: History, data: dict[str, Any], sep: str) -> AsyncIterator[bytes]:
    """``data`` = result of ``History.daily`` (fetched before streaming so errors can be 400)."""
    yield (BOM + _line(["date", "yield_kwh", "total_yield_kwh", "peak_power_w", "peak_time_local",
                        "first_production_local", "last_production_local", "production_h"],
                       sep)).encode("utf-8")
    for d in data["days"]:
        cells = [d["date"], fmt_num(d["yield_kwh"], sep), fmt_num(d["total_yield_kwh"], sep),
                 fmt_num(d["peak_power_w"], sep)]
        for key in ("peak_ts", "first_production_ts", "last_production_ts"):
            cells.append(_local_time(h, d[key]))
        cells.append("" if d["production_s"] is None else fmt_num(d["production_s"] / 3600, sep))
        yield _line(cells, sep).encode("utf-8")


def _local_time(h: History, iso_utc: str | None) -> str:
    if not iso_utc:
        return ""
    from datetime import datetime, timezone
    ep = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return h.local_str(ep, "%H:%M:%S")
