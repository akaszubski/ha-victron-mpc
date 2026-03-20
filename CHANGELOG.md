# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-03-20

### Changed

- **schedule_30min extended to 48 entries (full 24h)**: Was 16 entries (8h). Now shows the complete LP plan including overnight and morning periods. Dashboard charts can display the full 24-hour SoC/price trajectory.
- **Notifications go to ak_iphone only**: Removed skiphone from notification targets.

### Added

- **Two new number entities for overnight hold price scaling**: Overnight Hold Price (Full) (default $0.15) and Overnight Hold Price (Zero) (default $0.25). These expose the overnight hold reward scaling thresholds as UI-adjustable number entities, replacing the options-flow-only parameters. Total number entities: 12 (was 10).

## [0.5.0] - 2026-03-20

### Changed

- **SoC floor raised from 20% to 30%**: Daytime operating floor is now 30% (default). This is the OPERATING floor, not the hardware floor (10%) or genset trigger (~15-20%). Keeps ~4.3 kWh (1.4 kWh above hardware minimum) as emergency reserve at all times. Grid-down exception: Victron ESS handles islanding independently, genset auto-starts.
- **Three-layer solar forecast architecture**: `Final = Solcast cloud shape x VRM coefficient x VRM hourly mask`. Layer 1 (Solcast): cloud-aware hourly shape from satellite. Layer 2 (VRM coefficient): site shading ratio (~0.65 March, ~0.80 summer, ~0.45 winter), derived from `VRM_best_day / Solcast_clear_sky`. Layer 3 (VRM hourly mask): zeros hours where VRM P90 < 0.2 kW (morning/evening shaded), caps partially shaded hours at VRM P90. Replaces simple VRM P90 per-hour cap.
- **Register logic finalized with 5% buffer**: After 4 iterations, the correct register approach: `grid_charge` = target SoC (above current), `solar_charge` = hard floor 30%, `discharge`/`hold` = LP trajectory floor - 5% buffer. The buffer prevents grid import from register being too close to current SoC.
- **Mac runner permanently stopped**: `com.homeassistant.mpc` launchctl service unloaded. All 12 YAML automations have `initial_state: false`. HACS integration is sole controller.

### Added

- **Tomorrow stitching**: After sunset, appends tomorrow's Solcast forecast for overnight LP planning.
- **Amber forecast logging**: Each 5-min cycle logs actual price, spot price, margin, forecast accuracy at +1h/+2h/+3h/+6h, and spike predicted vs actual. Rolling 7-day buffer (2016 entries). Purpose: identify systematic forecast biases by time of day.
- **Solcast API management**: ha-solcast-solar configured in DAYLIGHT mode (`auto_update=1`), 10 calls/day spread sunrise-sunset. Quota resets at local midnight. Stale data (>12h) degrades gracefully: VRM coefficient + mask + intraday correction compensate.

## [0.4.1] - 2026-03-19

### Fixed

- **CRITICAL: Register 2901 behavior corrected** (#31): For solar_charge, hold, and discharge modes, the register is now always set BELOW the current SoC (to the configured SoC floor, e.g., 200 = 20%). Previously, the register could be set at or near the current SoC, which caused the Victron ESS to charge from grid (interpreting register >= SoC as a grid charge command). Only grid_charge mode now sets the register ABOVE current SoC. This is the single most important correctness fix in the integration.
- **Grid import auto-correction**: The coordinator now monitors `sensor.victron_grid_import` after register writes. If grid import exceeds 200W during a non-grid-charge mode, the integration auto-corrects the register to the SoC floor value and logs a warning.
- **Documentation audit**: All documentation updated to reflect correct register behavior. Previous docs incorrectly described register as "target SoC" for all modes.

### Changed

- **Solcast VRM P90 envelope enforcement**: Solcast forecasts are now always capped per-hour by the VRM P90 historical envelope grouped by month. Solcast over-forecasts approximately 2x for sites with shading; the VRM envelope corrects this. A warning is logged if VRM data is unavailable for the cap.
- **Mac runner stopped**: The `com.homeassistant.mpc` launchd service has been unloaded. All 12 YAML automations (`automation.mpc_*`) now have `initial_state: false` in their YAML to prevent re-enabling on HA restart. The HACS integration is the sole controller of registers.

## [0.4.0] - 2026-03-19

### Added

- **Parameterized safety thresholds** (#30): All safety and override thresholds are now configurable via UI number entities or options flow -- no hardcoded values remain in the decision logic. Four new number entities: Spike Threshold ($0.50-5.00, default $1.00), Defensive Price ($0.50-5.00, default $2.00), Amber Blip Minutes (1-15, default 5), Feed-in Export Threshold ($0.01-0.50, default $0.10). Options flow also exposes: overnight_price_low, overnight_price_high, feedin_soc_threshold, fallback_price.
- Total number entities increased from 6 to 10

## [0.3.0] - 2026-03-19

### Added

- **HA recorder history fallback** (#26): When Solcast and VRM are both unavailable, the integration queries 7 days of local HA recorder data for solar power and AC consumption entities. Builds an hourly profile grouped by hour-of-day to capture site-specific patterns (shading, usage habits). Active source shown as `ha_history` in the `solar_forecast_source` attribute.
- **Amber-down defensive discharge** (#27): When the Amber API is unavailable for more than 5 minutes, the integration applies time-of-day defensive pricing -- $2.00/kWh during evening peak (17:00-21:00) to protect against undetected spikes, $0.30/kWh at other times. Persistent notification alerts when defensive mode activates. Automatically recovers when Amber returns.
- **Modbus health monitoring** (#28): Tracks consecutive Modbus register write failures. After 3 failures, the new `binary_sensor.victron_mpc_battery_optimizer_modbus_connected` entity turns OFF and a persistent notification alerts the user. Recovery notification sent when writes succeed again. Coordinator data includes `modbus_healthy` (bool) and `modbus_failures` (int) attributes.
- New binary sensor: `binary_sensor.victron_mpc_battery_optimizer_modbus_connected` (device class: connectivity)
- Test coverage: 178 tests covering all 48 failure scenarios in the test matrix

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

[0.5.1]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.5.1
[0.5.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.5.0
[0.4.1]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.4.1
[0.4.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.4.0
[0.3.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.3.0
[0.2.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.2.0
[0.1.0]: https://github.com/akaszubski/ha-victron-mpc/releases/tag/v0.1.0
