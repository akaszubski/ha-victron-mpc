# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-03-18

### Added

- **Solcast solar forecast integration**: Auto-detects [ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) HACS integration for satellite-based solar forecasts calibrated to your specific rooftop
- Solcast is now Priority 0 in the solar forecast chain (most accurate), with automatic fallthrough to VRM when unavailable
- Optional Solcast entity picker in config flow Step 4 (Victron Sensors)
- Cloud derating is automatically skipped when Solcast is active (Solcast already accounts for clouds, shading, and panel orientation)
- New `solar_forecast_source` value: `solcast_ha`

## [0.1.0] - 2026-03-18

### Added

- Initial release as a HACS custom integration
- **LP Optimizer**: scipy HiGHS solver with 288-step (5-min) rolling 24-hour horizon, running natively in HA via DataUpdateCoordinator
- **Config Flow**: 5-step setup wizard (Modbus connection, battery specs, Amber entities, Victron sensors, VRM API)
- **Sensor entities** (11): Battery Plan, Decision, Effective Price, 24h Projected Cost, Solar Input, Load Input, Buy Price, Sell Price, Cloud Coverage, Solar Forecast Today, Solver Time
- **Number entities** (6): Battery Wear Cost, Sunset Reward, Overnight Hold Reward, SoC Floor, Overnight Min SoC, Load Inflation -- all adjustable from the HA UI
- **Switch entity** (1): Shadow Mode -- log decisions without writing Modbus registers (enabled by default)
- **Binary sensor entities** (2): Data Stale (problem indicator), Spike Override Active
- **Override logic**: Automatic spike discharge (R2901=100), negative pricing charge (R2901=1000), with 5-rule feed-in control for R2706
- **Solar forecast**: Weather-classified VRM historical percentiles (P90/P70/P40/P15), cloud layer derating via Open-Meteo, mid-day adjustment, 4-level fallback chain
- **Load forecast**: VRM ML base profile with seasonal scaling, temperature correction, AC demand detection
- **Genset cost**: Live diesel pricing from PetrolSpy factored into LP decisions
- **Cell balancing**: Periodic full charge tracking (14-day interval)
- **Overnight preservation**: Price-scaled hold reward + configurable hard SoC floor (22:00-06:00)
- **Modbus register writes**: R2901 (ESS min SoC) and R2706 (max grid feed-in) via HA Modbus service
- **Options flow**: Adjust tunables and toggle shadow mode from Settings
- **Diagnostics**: Downloadable diagnostics dump with redacted credentials
- **Documentation**: Setup guide, technical deep-dive, tuning guide, troubleshooting, full entity reference with example Lovelace cards

[0.2.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.2.0
[0.1.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.1.0
