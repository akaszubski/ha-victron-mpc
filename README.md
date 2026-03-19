# Victron MPC Battery Optimizer

[![HACS Validation](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hacs.yaml/badge.svg)](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hacs.yaml)
[![Hassfest](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hassfest.yaml)
[![Tests](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/tests.yaml/badge.svg)](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/tests.yaml)

A HACS custom integration that uses **Model Predictive Control** to minimize electricity costs for **Victron ESS** systems with **Amber Electric** wholesale pricing. Every 5 minutes, a Linear Program computes the optimal 24-hour battery dispatch plan and writes Modbus registers directly to your Victron Cerbo GX.

## Why this integration?

If you have a Victron battery system and Amber Electric, you are exposed to volatile wholesale pricing -- spikes above $5/kWh, negative prices where you are paid to consume, and everything in between. The built-in Victron ESS "minimum SoC" setting is static and cannot respond to price signals.

This integration replaces that static setting with a rolling 24-hour optimizer that:

- **Charges** when grid power is cheap (or negative)
- **Discharges** during expensive periods and price spikes
- **Holds** when solar is expected to charge for free
- **Exports** excess to the grid when feed-in rates justify it

Typical savings are 20-40% on electricity costs compared to a fixed SoC strategy.

## Features

- **LP-optimized dispatch** -- 288-step (5-min) rolling horizon via scipy HiGHS solver (~50ms per solve)
- **Amber Electric integration** -- wholesale buy/sell pricing, 30h forecast, spike detection
- **Direct Modbus writes** -- R2901 (ESS min SoC) and R2706 (max grid feed-in) via Victron Cerbo GX
- **Solcast solar forecast** -- auto-detects [ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) for satellite-based rooftop forecasts (optional, highest priority), always capped by VRM P90 per-hour shading envelope
- **Weather-classified solar forecast** -- VRM historical percentiles (P90/P70/P40/P15) selected by day type
- **Cloud layer derating** -- Open-Meteo low/mid/high altitude cloud weighting for accurate solar adjustment
- **Override safety** -- automatic spike discharge, negative pricing charge, stale data fallback
- **Overnight preservation** -- price-scaled hold reward + configurable hard SoC floor (22:00-06:00)
- **Cell balancing** -- periodic full charge every 14 days for BMS health
- **Seasonal load adjustment** -- VRM monthly consumption patterns + outdoor temperature correction
- **AC demand detection** -- indoor temperature sensors + climate entity state for real-time load boost
- **Genset cost integration** -- live diesel pricing from PetrolSpy factors into LP decisions
- **HA recorder history fallback** -- uses 7 days of local solar/load history when VRM and Solcast are unavailable
- **Amber-down defensive discharge** -- assumes spike-risk pricing during evening peak (17:00-21:00) when Amber API is unavailable
- **Modbus health monitoring** -- tracks consecutive write failures, alerts via persistent notification, binary sensor for dashboards
- **Shadow mode** -- validate all decisions without writing registers (enabled by default)
- **Adjustable tunables** -- modify optimization parameters from the HA UI, no config files needed

## Requirements

| Requirement | Details |
|------------|---------|
| **Home Assistant** | 2024.8 or later |
| **Victron hardware** | Cerbo GX (or similar GX device) with Modbus TCP enabled |
| **Inverter** | Victron MultiPlus / Quattro with ESS Assistant installed |
| **Battery** | Any Victron-compatible battery (LiFePO4 recommended) |
| **Amber Electric** | Active account with the [HA Amber integration](https://www.home-assistant.io/integrations/amber/) configured |
| **HA Modbus** | [Modbus integration](https://www.home-assistant.io/integrations/modbus/) configured for your Cerbo GX |
| **VRM account** | Optional but recommended -- provides 180-day solar production history for forecasting |
| **ha-solcast-solar** | Optional -- [HACS integration](https://github.com/BJReplay/ha-solcast-solar) for satellite-based solar forecasts (highest accuracy, free hobbyist tier: 10 API calls/day) |

## Installation

### Via HACS (recommended)

1. Open HACS in your Home Assistant instance
2. Click the three dots menu (top right) and select **Custom repositories**
3. Enter `https://github.com/akaszubski/ha-victron-mpc` and select **Integration** as the category
4. Click **Add**, then find "Victron MPC Battery Optimizer" in the HACS store
5. Click **Download**
6. **Restart Home Assistant**

### Manual

1. Copy the `custom_components/victron_mpc` folder into your `config/custom_components/` directory
2. Restart Home Assistant

## Quick Start

After installation:

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for **Victron MPC Battery Optimizer**
3. Follow the 5-step config flow:
   - **Step 1**: Victron Modbus connection (Cerbo GX IP, port, unit IDs)
   - **Step 2**: Battery specifications (capacity, charge/discharge rates)
   - **Step 3**: Amber Electric entity selection (price, forecast, spike sensors)
   - **Step 4**: Victron sensor entity selection (SoC, solar, load, grid, optional Solcast entity)
   - **Step 5**: VRM API credentials (optional -- for solar forecast accuracy)
4. The integration starts in **shadow mode** -- it logs decisions without writing registers
5. Review decisions for a few days via the Battery Plan and Decision sensors
6. When confident, turn off Shadow Mode to enable live register writes

See [docs/SETUP.md](docs/SETUP.md) for detailed setup instructions.

## Entity Reference

All entities are grouped under a single **Victron MPC Battery Optimizer** device.

### Sensors

| Entity | Name | Unit | Description |
|--------|------|------|-------------|
| `sensor.victron_mpc_battery_optimizer_battery_plan` | Battery Plan | % | SoC floor/target with mode, reason, register values, SoC trajectory |
| `sensor.victron_mpc_battery_optimizer_decision` | Decision | -- | Current mode with full context: prices, weather, forecast source, overrides |
| `sensor.victron_mpc_battery_optimizer_effective_price` | Effective Price | $/kWh | Weighted price the optimizer used for its decision |
| `sensor.victron_mpc_battery_optimizer_24h_projected_cost` | 24h Projected Cost | $ | Projected cost with grid/export/wear breakdown |
| `sensor.victron_mpc_battery_optimizer_solar_input` | Solar Input | W | Current solar production at decision time |
| `sensor.victron_mpc_battery_optimizer_load_input` | Load Input | W | Current household load at decision time |
| `sensor.victron_mpc_battery_optimizer_buy_price` | Buy Price | $/kWh | Current Amber buy price with spike indicator |
| `sensor.victron_mpc_battery_optimizer_sell_price` | Sell Price | $/kWh | Current Amber feed-in price |
| `sensor.victron_mpc_battery_optimizer_cloud_coverage` | Cloud Coverage | % | Effective cloud percentage with per-layer breakdown |
| `sensor.victron_mpc_battery_optimizer_solar_forecast_today` | Solar Forecast Today | kWh | Forecasted solar production with source, day type, derate factor |
| `sensor.victron_mpc_battery_optimizer_solver_time` | Solver Time | ms | LP solver execution time |

### Number Entities (Tunables)

All decision thresholds are configurable -- no hardcoded values in the logic.

| Entity | Name | Range | Default | Description |
|--------|------|-------|---------|-------------|
| `number.victron_mpc_battery_optimizer_battery_wear_cost` | Battery Wear Cost | $0.01-0.10/kWh | $0.05 | Penalty for battery cycling |
| `number.victron_mpc_battery_optimizer_sunset_reward` | Sunset Reward | $0.01-0.10/kWh | $0.04 | Incentive for full battery at sunset |
| `number.victron_mpc_battery_optimizer_overnight_hold_reward` | Overnight Hold Reward | $0.02-0.20/kWh | $0.10 | Max overnight preservation incentive (price-scaled) |
| `number.victron_mpc_battery_optimizer_soc_floor` | SoC Floor | 15-30% | 20% | Minimum daytime battery level |
| `number.victron_mpc_battery_optimizer_overnight_min_soc` | Overnight Min SoC | 20-45% | 30% | Hard overnight floor (22:00-06:00) |
| `number.victron_mpc_battery_optimizer_load_inflation` | Load Inflation | 5-25% | 10% | Safety margin on load forecasts |
| `number.victron_mpc_battery_optimizer_spike_threshold` | Spike Threshold | $0.50-5.00/kWh | $1.00 | Price above which spike discharge is forced |
| `number.victron_mpc_battery_optimizer_defensive_price` | Defensive Price | $0.50-5.00/kWh | $2.00 | Assumed price during Amber outage (evening peak) |
| `number.victron_mpc_battery_optimizer_amber_blip_minutes` | Amber Blip Minutes | 1-15 min | 5 | Minutes of Amber unavailability before defensive mode |
| `number.victron_mpc_battery_optimizer_feedin_export_threshold` | Feed-in Export Threshold | $0.01-0.50/kWh | $0.10 | Minimum FIT price to allow spike export |

### Switches

| Entity | Name | Default | Description |
|--------|------|---------|-------------|
| `switch.victron_mpc_battery_optimizer_shadow_mode` | Shadow Mode | ON | When on, logs decisions without writing Modbus registers |

### Binary Sensors

| Entity | Name | Description |
|--------|------|-------------|
| `binary_sensor.victron_mpc_battery_optimizer_data_stale` | Data Stale | Problem indicator -- no successful update in 10+ minutes |
| `binary_sensor.victron_mpc_battery_optimizer_spike_override_active` | Spike Override Active | Price spike override is currently forcing discharge |
| `binary_sensor.victron_mpc_battery_optimizer_modbus_connected` | Modbus Connected | Connectivity indicator -- OFF after 3+ consecutive Modbus write failures |

See [docs/ENTITIES.md](docs/ENTITIES.md) for detailed attribute documentation and example dashboard cards.

## How It Works (Brief)

Every 5 minutes, the DataUpdateCoordinator runs a full optimization cycle:

1. **Reads state** from HA entities (battery SoC, solar power, load, Amber prices)
2. **Fetches forecasts** from Solcast (if available), VRM (solar history), Open-Meteo (cloud layers), met.no (weather)
3. **Classifies the day** as clear/partly_cloudy/overcast/rain based on effective cloud coverage
4. **Builds a 24-hour forecast** of solar production, household load, and buy/sell prices
5. **Solves a Linear Program** (scipy HiGHS, 288 timesteps) minimizing total electricity cost
6. **Applies safety overrides** (spike = discharge, negative pricing = charge)
7. **Writes Modbus registers** R2901 (ESS min SoC) and R2706 (max feed-in) to the Cerbo GX. For solar_charge/hold/discharge modes, R2901 is set BELOW current SoC (as a floor). Only grid_charge sets R2901 ABOVE current SoC.

The LP objective minimizes:
```
grid_import x buy_price - grid_export x sell_price
  + discharge x wear_cost
  - sunset_reward x soc_at_sunset
  - overnight_hold_reward x soc_at_morning
```

Subject to power balance, SoC bounds, and physical rate limits at each timestep.

See [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) for the full technical explanation.

## Documentation

| Document | Description |
|----------|-------------|
| [docs/SETUP.md](docs/SETUP.md) | Prerequisites, config flow walkthrough, post-setup verification |
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | LP optimizer, solar forecast chain, override logic, register mapping |
| [docs/PARAMETERS.md](docs/PARAMETERS.md) | Definitive reference for every configurable field (52 parameters with defaults, ranges, effects) |
| [docs/TUNING.md](docs/TUNING.md) | Tunable parameters, common scenarios, recommended adjustments |
| [docs/ENTITIES.md](docs/ENTITIES.md) | Complete entity reference with attributes and example Lovelace cards |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common issues, debug logging, diagnostics, rollback |
| [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md) | 48-scenario failure matrix with coverage status |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## License

MIT
