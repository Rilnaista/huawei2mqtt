# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

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
