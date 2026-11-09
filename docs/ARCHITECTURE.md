# Architecture

huawei2mqtt is **one asyncio Python process** in one container. Three services share
an in-memory cache and a small persistent state:

```
              ┌──────────── huawei2mqtt (one process) ────────────┐
 SDongle  ◄──►│ modbus.py ◄─ poller.py ──► cache.py ──► publish.py ─┼──► MQTT broker
 Modbus TCP   │  (1 conn,      (LIVE/STATIC,   (last known   (group JSON │    (external)
 :502         │   1 lock)       backoff,        values)       + flat)    │
              │      ▲          stale)                                   │
              │      └── commands.py ◄── mqtt.py (set topics) ◄──────────┼─── set topics
              │      └── commands.py ◄── api.py (/api/command) ◄─────────┼─── browser
              │  state.py (/data: settings.json, state.json)  api.py ───┼──► portal (web/)
              └────────────────────────────────────────────────────────┘
```

## Modules (`bridge/`)

| Module | Responsibility |
|---|---|
| `registers.py` | Declarative register map (`R(address, field, group, type, …)`) and group field order |
| `codes.py` | Status code (32089) and alarm bit (32008–32010) tables → text keys |
| `modbus.py` | Exactly **one** Modbus TCP connection; every read/write is serialised by one lock; 1 s settle time after connect |
| `poller.py` | Builds read blocks from the map (≤ 64 registers), LIVE and STATIC schedule, back-off 2 s → 60 s, `online`/`stale`/`offline` |
| `cache.py` | Decoded values + raw values; keeps last known values on failure |
| `publish.py` | Topic and payload construction (group JSON with `ts`, flat topics, `null` handling) |
| `mqtt.py` | MQTT client: LWT, reconnect with back-off, publish queue, set-topic receiver (ignores retained) |
| `commands.py` | The three setters: validation, rate limit (1 / 2 s), whitelisted writes, `write_access` tracking |
| `history.py` | SQLite store (`/data/huawei2mqtt.db`, WAL): 10-s samples, condensed 5-min samples (`samples_5m`), daily values, milestones; buffered writes (commit every 60 s), condensing after `HISTORY_DAYS` (nothing is deleted), bucketed queries across both levels (avg/min/max), MQTT `history/get` |
| `awards.py` | Pure functions: rankings (top 3), milestones, CO₂ and money from the daily values |
| `api.py` | aiohttp: portal API + static files from `WEB_DIR` |
| `config.py` | `.env` overlaid by `DATA_DIR/settings.json` (only the editable fields) |
| `state.py` | Persistence (`write_access`, last serial number, session secret) and runtime status |
| `logbuffer.py` | JSON logs on stdout, 500-entry ring buffer for the portal, secret redaction |
| `app.py` / `__main__.py` | Wiring and clean SIGTERM shutdown (publishes `offline`) |

## Design decisions

- **One Modbus connection, serialised.** The SDongle handles a single client reliably; polls and commands share one lock, with a pause (`MODBUS_REQUEST_DELAY_MS`) between requests.
- **Block reads.** Registers close to each other are read in one request (gaps up to 16 words are bridged), so a live cycle needs only a few requests.
- **History without extra load.** History, daily values and awards are fed only from the regular live polls — the bridge never issues additional Modbus requests for them, and never uses Huawei's Modbus file upload (it destabilised an SDongle in testing).
- **Robust daily values.** At night the inverter still reports yesterday's daily yield and peak; daily values are therefore taken only after production has started that day. Start/end of production count for awards only when the bridge actually observed them.
- **Gentle error handling.** A request without answer is retried once; only then the bridge reconnects (counted as a drop-out). "Busy" exceptions (0x04/0x06) are no drop-outs. pymodbus' own logging is silenced in favour of one readable line.
- **Decoupled failures.** Modbus and MQTT reconnect independently; the process never exits because a device is unavailable.
- **Safety first for writes.** Only whitelisted registers can be written; retained set messages are ignored; shutdown needs an explicit opt-in.
- **No build step for the portal.** Plain HTML/CSS/JS, no framework, no CDN — works offline, tiny image.
- **Translations** live in `web/i18n/<lang>.json`; tests ensure all languages have the same keys.

## Adding a register

1. Find the register in Huawei's *SUN2000 Modbus Interface Definitions* (address, type, gain, unit).
2. Add one row to `REGISTERS` in `bridge/registers.py`, e.g.
   ```python
   R(32087, "internal_temperature_c", "inverter", "I16", gain=10, unit="°C", decimals=1),
   ```
   `klass=STATIC` for rarely changing values.
3. Add the field to the group order in `GROUP_FIELDS` (same file).
4. Add a label `field.<field>` to all files in `web/i18n/`.
5. Run `pytest` — done. The value appears in MQTT, `/api/values` and the portal.

New **commands** are intentionally more work: they need an entry in `commands.py`
with validation and must be documented in the README's command table.

## Portal API

All responses are JSON (UTF-8). Reading never requires a login. Writing requires a
session (login with `PORTAL_PASSWORD`); without a configured password all writing
endpoints return `403`.

| Method & path | Auth | Description |
|---|---|---|
| `GET /healthz` | – | `{"status":"ok"}` while the process is alive |
| `GET /api/status` | – | Bridge, inverter, dongle and MQTT status, `write_access`, `shutdown_allowed`, auth info |
| `GET /api/values` | – | All groups (as published on MQTT) + `registers` list (address, field, raw, value, unit, group) |
| `GET /api/logs?since=<seq>` | – | Log entries from the ring buffer (last 500) |
| `POST /api/login` | – | `{"password"}` → session cookie; 5 failures/min per IP → `429` |
| `POST /api/logout` | session | Ends the session |
| `GET /api/settings` | – | Editable and read-only settings, limits; secrets only as `*_set` booleans |
| `PUT /api/settings` | session | Change editable settings (validated, persisted in `/data`) |
| `POST /api/command` | session | `{"command":"power_limit_pct"\|"power_limit_w"\|"power","value":…}` — same rules as MQTT |
| `POST /api/write-test` | session | Reads 40125 and writes the same value back |
| `GET /api/history?from=&to=&fields=a,b&points=N` | – | Bucketed samples: `t[]` (epoch ms) and per field `avg[]`, `min[]`, `max[]`; `available` lists all fields (max. 2000 points, 10 fields) |
| `GET /api/daily?from=&to=` | – | Daily values (yield, total, peak + time, first/last production, observation flags) |
| `GET /api/awards` | – | Rankings, milestones, CO₂, money (same JSON as MQTT `…/awards`) |
| `POST /api/history/clear` | session | `{"scope":"samples"\|"daily"\|"all"}` — clears history only, daily values + milestones, or both |

`GET /api/status` additionally contains `poll` (`live_interval_s`, `last_live_at`, `next_live_at`),
`inverter.dropouts_24h` / `last_dropout` and `history` (`days`, `samples`, `samples_5m`, `oldest`, `detail_oldest`, `db_size_bytes`). `/api/history` additionally returns `resolution_s` (10, 300 or `"mixed"`).

## Persistent data (`/data`)

| File | Content |
|---|---|
| `settings.json` | Settings changed in the portal (override `.env`) |
| `state.json` | `write_access`, last known serial number |
| `huawei2mqtt.db` | SQLite: `samples` (one row per live poll, kept `HISTORY_DAYS`), `samples_5m` (5-min averages of older samples, kept forever), `daily` (kept forever), `milestones` |
| session secret | Signing key for portal sessions (generated on first start) |

Deleting the volume also deletes history, daily values and awards. Deleting it resets the portal settings to `.env` and the write-access status to "unverified".
