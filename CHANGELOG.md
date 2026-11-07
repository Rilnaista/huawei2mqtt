# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0-rc1] - 2026-11-07

### Added
- Modbus TCP bridge for Huawei SUN2000 inverters (tested: SUN2000-10KTL-M1 via SDongleA).
- MQTT publishing of PV, grid, energy, inverter status/alarms, device and control values as group JSON and per-value topics; LWT availability and bridge status.
- Commands: active power limit in % and W, inverter on, inverter off (opt-in via `HUAWEI_ALLOW_SHUTDOWN`).
- Write-access test and status (verified / unverified / denied).
- Resilience: independent reconnect with back-off for Modbus and MQTT, stale/offline handling.
- Web portal: overview, all values, log, settings & control; login with `PORTAL_PASSWORD`; 7 languages.
- Docker image (non-root, health check, amd64/arm64), Compose file, GitHub Actions CI and release pipeline (ghcr.io, optional Docker Hub).
