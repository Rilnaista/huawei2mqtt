# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-11

First public release.

### Added
- History: comparison with the same period in previous years (dashed lines for ranges up to 7 days); new **Yields** view with bars per week, month and year, previous years side by side.
- The single-day view zooms to the hours with production (first to last yield ±15 min).
- Overview: today's yield in context (average day of this calendar month, yesterday, same day last year); early-warning hints.
- MQTT `…/daily/summary` (retained): daily summary once production has ended, incl. ranks and comparisons.
- Early warnings `…/warnings`: insulation resistance clearly below its usual value, energy share of the PV strings deviating (`WARN_INSULATION_RATIO`, `WARN_STRING_DEVIATION`).
- Nightly database backup to `/data/backup/` with rotation (`BACKUP_KEEP`, default 7, also in the portal).
- CSV export of history (raw values) and daily values (`/api/export/samples.csv`, `/api/export/daily.csv`), Excel-friendly for non-English languages.
- `/api/daily/context`.
- Portal installable as an app (web manifest, icons, service worker; the API is never cached).
- GitHub issue templates.

### Changed
- "Live" badge only shows when the chart really refreshes live.

## [0.3.0-rc3] - 2026-11-09

### Added
- Permanent history: after `HISTORY_DAYS` the 10-second values are condensed to 5-minute averages (table `samples_5m`) and kept forever instead of being deleted.
- History page: 30 and 90 days, 1 year, all and a free from–to range; hint when older 5-minute averages are shown; storage info in Settings.
- `/api/history` and MQTT `history/get` read seamlessly across detail and 5-minute values (`resolution_s`); `/api/status` `history` gains `samples_5m` and `detail_oldest`.

### Changed
- `HISTORY_DAYS` now means "keep in full detail", default 90 (was 7), range 1–365.
- Power axis in the history chart starts at 0.
- `…/awards` is published after the first successful poll (no empty message on connect); a rate-limited `history/result` carries the request `type`.

## [0.2.0-rc2] - 2026-11-09

### Added
- History: every live value (10 s) stored in SQLite for `HISTORY_DAYS` (default 7); portal page **History** with live line chart (average + min/max band).
- Daily values kept forever; portal page **Awards**: top 3 day/week/month/year, peak power with time, longest sunny day, earliest start; milestones, CO₂ saved, yield in money (`FEED_IN_TARIFF`, `CURRENCY`, `CO2_FACTOR`, `MILESTONE_STEP_KWH`).
- MQTT: `…/awards` (retained), request/response `…/history/get` → `…/history/result`; `energy.peak_power_today_ts`; `bridge.dropouts_24h`.
- Portal: countdown to the next update, time of today's peak, firmware versions, clear notice when the inverter is unreachable, drop-out hint, *clear storage*, "forgot password?" hint.

### Changed
- Portal header removed; language and login moved to Settings; logo in the navigation; unified look of "Connection & system"; unit sizes harmonised; short labels in the mobile navigation.
- A request without answer is retried once before reconnecting; no more pymodbus hex dumps in the log.

## [0.1.0-rc1] - 2026-11-07

### Added
- Modbus TCP bridge for Huawei SUN2000 inverters (tested: SUN2000-10KTL-M1 via SDongleA).
- MQTT publishing of PV, grid, energy, inverter status/alarms, device and control values as group JSON and per-value topics; LWT availability and bridge status.
- Commands: active power limit in % and W, inverter on, inverter off (opt-in via `HUAWEI_ALLOW_SHUTDOWN`).
- Write-access test and status (verified / unverified / denied).
- Resilience: independent reconnect with back-off for Modbus and MQTT, stale/offline handling.
- Web portal: overview, all values, log, settings & control; login with `PORTAL_PASSWORD`; 7 languages.
- Docker image (non-root, health check, amd64/arm64), Compose file, GitHub Actions CI and release pipeline (ghcr.io, optional Docker Hub).
