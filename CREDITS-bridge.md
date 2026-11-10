# Credits: Bridge-Kern (bridge/)

Runtime dependencies (requirements.txt):

| Library | Version | License |
|---|---|---|
| pymodbus | 3.16.2 | BSD-3-Clause |
| aiomqtt | 2.5.1 | BSD-3-Clause |
| paho-mqtt | 2.2.0 | EPL-2.0 OR BSD-3-Clause (EDL-1.0) |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| aiohappyeyeballs (transitive) | 2.7.1 | PSF-2.0 |
| aiosignal (transitive) | 1.4.0 | Apache-2.0 |
| attrs (transitive) | 26.1.0 | MIT |
| frozenlist (transitive) | 1.8.0 | Apache-2.0 |
| idna (transitive) | 3.20 | BSD-3-Clause |
| multidict (transitive) | 7.2.1 | Apache-2.0 |
| propcache (transitive) | 0.5.4 | Apache-2.0 |
| typing_extensions (transitive) | 4.16.0 | PSF-2.0 |
| yarl (transitive) | 1.25.1 | Apache-2.0 |
| tzdata (time zone database for zoneinfo) | 2026.6 | Apache-2.0 |

Development / tests only (requirements-dev.txt, not shipped):

| Library | Version | License |
|---|---|---|
| pytest | 9.2.0 | MIT |
| pytest-asyncio | 1.4.1.post1 | Apache-2.0 |

Runtime: Python 3.12 (PSF License). The history store uses the standard library `sqlite3`
(SQLite, public domain) - no additional dependency since rc2.

## Protocol references (no code or dependency taken over)
- Huawei "SUN2000 Modbus Interface Definitions": register addresses, gains, status codes
  and alarm bits (facts about the device interface). Text keys in `bridge/codes.py` are our own.
- huawei-solar-lib (wlcrs/huawei-solar-lib, AGPL-3.0): consulted read-only to cross-check
  register addresses and code tables. It is NOT a dependency and no code or table text was copied.

## Container base image
| Component | Version | License |
|---|---|---|
| python:3.12-alpine (Docker official image) | 3.12 | Python: PSF-2.0; Alpine Linux userland: MIT/GPL/others per package (not modified) |

rc4 (0.4.0): no new dependencies (backup, CSV export and warnings use only the Python standard library: sqlite3, statistics, csv-free streaming via aiohttp).
