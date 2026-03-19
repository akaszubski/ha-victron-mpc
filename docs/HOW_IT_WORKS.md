# How It Works

## Core Goal

**Minimize total electricity cost over the next 24 hours** by optimally dispatching the battery -- charging when power is cheap, discharging when it is expensive, and using solar production effectively.

The integration runs a full optimization cycle every 5 minutes via a `DataUpdateCoordinator`, solving a Linear Program with 288 timesteps (5-minute intervals over 24 hours).

---

## Architecture

```
DataUpdateCoordinator (every 5 minutes)
  |
  |-- Phase 1: Read HA entity state (SoC, solar, load, prices)
  |-- Phase 2: Fetch external APIs (VRM, Open-Meteo) with caching
  |-- Phase 3: Build forecasts (solar/load/price, 288 steps)
  |-- Phase 4: Run LP optimizer in executor thread (~50ms)
  |-- Phase 5: Apply overrides (spike -> discharge, negative -> charge)
  |-- Phase 6: Compute feed-in register value (R2706)
  |-- Phase 7: Write Modbus registers (if not shadow mode)
  |-- Phase 8: Return data dict -> sensor entities auto-update
```

Everything runs natively inside Home Assistant. There is no external daemon, no separate server, and no REST API pushing. The coordinator reads HA entities for inputs and writes Modbus registers for outputs.

---

## The LP Optimizer

Every 5 minutes, a Linear Program minimizes:

```
min  SUM (grid_import x buy_price - grid_export x sell_price
         + discharge x wear_cost + grid_import x import_penalty) x dt
     - sunset_reward x soc[sunset]
     - terminal_reward x soc[end]
     - overnight_hold_reward x soc[morning]
```

Subject to power balance, SoC bounds, and physical limits at each of the 288 timesteps.

The solver is scipy's `linprog` with the HiGHS backend, running in an executor thread to avoid blocking the HA event loop. Typical solve time is ~50ms.

### Cost Factors

All values are in $/kWh to be directly comparable with electricity prices.

| Factor | Default | Purpose |
|--------|---------|---------|
| Battery Wear Cost | $0.05 | Discourages unnecessary cycling |
| Grid Import Penalty | $0.02 | Nudges toward self-sufficiency |
| Sunset Reward | $0.04 | Incentivizes full battery before evening peak |
| Terminal Reward | $0.03 | Prevents drain-to-zero at horizon end |
| Overnight Hold Reward | $0.10 (max) | Preserves charge for morning -- price-scaled (see below) |

### Price-Scaled Overnight Hold Reward

The overnight hold reward is not a fixed value. Before each optimization, it is scaled based on the average overnight grid price:

| Overnight Price | Scaled Reward | Effect |
|----------------|---------------|--------|
| <= $0.15/kWh | Full $0.10 | Preserve battery -- grid is genuinely cheap overnight |
| $0.22/kWh | ~$0.05 | Moderate hold |
| >= $0.30/kWh | $0.00 | No hold incentive -- discharging overnight saves money |

This means the optimizer naturally discharges during moderate overnight pricing and holds when overnight rates are genuinely cheap.

### Constraints

| Constraint | Value | Notes |
|------------|-------|-------|
| Daytime SoC floor | Configurable (default 20%) | Adjustable via SoC Floor number entity |
| Overnight SoC floor | Configurable (default 30%, 22:00-06:00) | Hard constraint -- LP cannot plan below this overnight |
| Max charge rate | From config (default 3.5 kW) | Grid-to-battery limit |
| Max discharge rate | From config (default 4.5 kW) | Battery-to-load limit |
| Charge efficiency | 95% | Round-trip ~90% |
| Cell balancing | Every 14 days | Forces full charge for BMS health |

---

## Solar Forecast

The integration uses a multi-level forecast priority chain, starting with the most accurate source available.

### Priority 0: Solcast (ha-solcast-solar)

If the [ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar) HACS integration is installed and configured, MPC auto-detects it and uses it as the primary solar forecast source. Solcast provides satellite-based forecasts calibrated to your specific rooftop, including panel orientation, tilt, and local shading.

The entity `sensor.solcast_pv_forecast_forecast_today` provides 30-minute resolution power forecasts (kW) with `pv_estimate`, `pv_estimate10`, and `pv_estimate90` fields in its `detailedForecast` attribute. MPC interpolates these to 5-minute steps.

Because Solcast already accounts for clouds, shading, and panel orientation, the Open-Meteo cloud derating is **not applied** when Solcast is the active source (the cloud_coverage sensor still updates independently for dashboard use).

When Solcast data is unavailable (entity missing, stale, or API rate-limited), the integration automatically falls through to VRM-based forecasting.

### Priority 1-4: VRM and Fallbacks

When Solcast is not available, solar forecasting uses actual production history from VRM, classified by weather conditions.

### Step 1: Classify Day Type

The integration computes **effective cloud coverage** over remaining daylight hours using per-layer cloud data from Open-Meteo:

| Cloud Layer | Weight | Why |
|-------------|--------|-----|
| **Low** (stratus, fog) | 0.9 | Thick, close -- blocks most direct radiation |
| **Mid** (altostratus) | 0.5 | Moderate blocking, some diffuse passes |
| **High** (cirrus) | 0.15 | Thin ice crystals -- barely affects solar |

```
effective_cloud = (low x 0.9 + mid x 0.5 + high x 0.15) / (0.9 + 0.5 + 0.15)
```

If Open-Meteo is unavailable, falls back to met.no total cloud coverage.

Classification rules:

| Day Type | Effective Cloud | Precipitation | VRM Percentile |
|----------|----------------|---------------|----------------|
| `clear` | < 30% | < 1mm | P90 (top 10%) |
| `partly_cloudy` | 30-70% | < 1mm | P70 |
| `overcast` | > 70% | < 2mm | P40 |
| `rain` | any | >= 2mm | P15 (bottom 15%) |

### Step 2: Select VRM Historical Percentile

Fetches 180 days of hourly solar actuals from VRM, groups by (month, hour-of-day), and computes the selected percentile per slot. This naturally models physical shading, sunrise/sunset timing, and seasonal sun angle changes.

### Step 3: Mid-Day Adjustment

After 10am, compares actual solar yield to expected yield:

| Actual vs Expected | Action | Example |
|-------------------|--------|---------|
| > 150% | Upgrade one level | overcast -> partly_cloudy |
| 60-150% | No change | Within normal range |
| < 60% | Downgrade one level | partly_cloudy -> overcast |

### Step 4: Cloud Derating Overlay

After selecting the VRM percentile shape, per-hour cloud derating is applied:

```
factor = max(sqrt(1 - effective_cloud% x impact), floor)
```

Where `impact` defaults to 0.75 and `floor` defaults to 0.50.

### Forecast Priority Chain

The integration tries each source in order, falling through when one is unavailable:

0. **ha-solcast-solar** (satellite, most accurate) -- rooftop-calibrated, cloud-aware, no derating needed
1. **Weather-classified VRM envelope** -- P90/P70/P40/P15 based on day type
2. **VRM 30-day actual average** -- scaled by VRM daily total
3. **HA recorder history** -- 7 days of local solar power sensor data from the HA database (requires recorder integration with sufficient history retention)
4. **Synthetic bell curve** (last resort) -- Gaussian scaled to estimated daily kWh

The active source is reported in the Solar Forecast Today sensor's `solar_forecast_source` attribute.

---

## Load Forecast

### Base Profile
VRM's ML-powered hourly consumption forecast (learns from historical data).

### Seasonal Scaling
VRM monthly consumption data adjusts for macro patterns.

### Temperature Correction
- Below 15C max temp: +1.0% per degree (heating-related load)
- Above 26C max temp: +3.3% per degree (AC is purely electrical, 2-3kW each)

### AC Demand Detection
Indoor temperature sensors and climate entities detect AC demand in real-time:

| Signal | Condition | Boost |
|--------|-----------|-------|
| AC confirmed running | Climate entity state = cooling/heating | +2kW per unit (flat) |
| Room hot | Indoor sensor > 24C | +0.8kW per degree above threshold per zone |

The higher of the two signals is used. Applied to the next 5 hours (2h full + taper).

### Safety Margin
A configurable load inflation percentage (default 10%) is applied to all load forecasts.

---

## Override Logic

The coordinator applies hard overrides after the LP solver runs. These take priority over the optimizer's decision.

### Register 2901 (ESS Minimum SoC)

Register 2901 is a **threshold, not a target**:

| Battery vs Threshold | Inverter Behavior |
|---------------------|-------------------|
| SoC < threshold | Grid powers loads + charges battery UP to threshold |
| SoC = threshold | Grid powers loads, battery holds |
| SoC > threshold | Battery discharges to power loads, down to threshold |

Solar always charges regardless of threshold.

Override priority (first match wins). All thresholds are configurable via number entities -- no hardcoded values in the logic.

| Priority | Condition | Register | Effect |
|----------|-----------|----------|--------|
| 1 | Buy price < $0 (negative) | 1000 | Charge to 100% -- paid to consume |
| 2 | Spike active or price > spike_threshold (default $1.00) | 100 | Discharge to 10% -- drain battery |
| 3 | Normal | LP decision | Optimizer's computed target |

The **Spike Threshold** (`number.victron_mpc_battery_optimizer_spike_threshold`) controls when override #2 fires. Lower it to be more aggressive, raise it to only react to extreme spikes.

### Register 2706 (Max Grid Feed-In)

Controls maximum export power. Units = 100W per register value (70 = 7000W, 0 = block).

Feed-in rules (first match wins):

| # | Condition | Value | Rationale |
|---|-----------|-------|-----------|
| 1 | Negative buy price | 70 | Open -- we are paid to consume |
| 2 | Grid charge mode | 70 | Inverter needs grid access |
| 3 | Spike + FIT > feedin_export_threshold (default $0.10) + SoC > feedin_soc_threshold (default 30%) | 70 | Export for profit |
| 4 | SoC > 95% + FIT > $0 | 70 | Battery full, export excess |
| 5 | Otherwise | 0 | Block export, self-consume |

The **Feed-in Export Threshold** (`number.victron_mpc_battery_optimizer_feedin_export_threshold`) and **Feed-in SoC Threshold** (options flow) control when rule #3 allows spike export.

The conservative default (block export) is intentional. Household demand can spike suddenly (dryer, oven, AC). Only export when the battery is essentially full or pricing clearly justifies it.

### Stale Data Safety

If the coordinator fails to update for 10+ minutes (2 missed cycles), the Data Stale binary sensor turns on. In a future release, stale safety will automatically set conservative register values.

---

## Failure Resilience

The integration is designed to operate safely when external services fail. Three safety systems provide defense-in-depth:

### HA Recorder History Fallback (Solar + Load)

When both Solcast and VRM are unavailable (internet outage, API down, token expired), the integration queries the local HA recorder database for 7 days of historical sensor data. It groups readings by hour-of-day to build a profile that captures your site's actual patterns (shading, usage habits, seasonal timing).

The fallback chain for solar forecasting:

| Priority | Source | Requires | Accuracy |
|----------|--------|----------|----------|
| 0 | Solcast (ha-solcast-solar) | Internet + API key | Best -- satellite calibrated |
| 1 | VRM weather-classified envelope | Internet + VRM token | Good -- historical percentiles |
| 2 | VRM 30-day average | Internet + VRM token | Fair -- no weather classification |
| 3 | HA recorder history | Local database only | Fair -- 7-day average profile |
| 4 | Synthetic bell curve | Nothing | Rough -- generic Gaussian |

Load forecasting follows the same pattern: VRM ML forecast, then HA recorder history, then a typical residential curve.

The active source is reported in the Solar Forecast Today sensor's `solar_forecast_source` attribute (`solcast_ha`, `clearsky_p90`, `vrm_30d_avg`, `ha_history`, or `bell_curve`).

### Amber-Down Defensive Discharge

When the Amber Electric API becomes unavailable for more than the configured **Amber Blip Minutes** (default 5 minutes, adjustable via `number.victron_mpc_battery_optimizer_amber_blip_minutes`), the integration cannot detect price spikes. Rather than assuming flat pricing at all times (which would miss expensive evening peaks), it applies time-of-day defensive pricing:

| Time Period | Assumed Price | Rationale |
|-------------|--------------|-----------|
| 17:00-21:00 (evening peak) | Defensive Price (default $2.00/kWh) | Peak demand window -- highest spike probability |
| All other hours | Fallback Price (default $0.30/kWh) | Conservative hold -- typical off-peak rate |

Both the **Defensive Price** (`number.victron_mpc_battery_optimizer_defensive_price`) and **Fallback Price** (options flow) are configurable. The **Amber Blip Minutes** threshold controls how long the integration waits before switching to defensive mode, allowing brief Amber glitches to resolve without triggering unnecessary defensive discharge.

This means that during an Amber outage:
- **Evening**: The optimizer sees the defensive price and discharges the battery to power loads, avoiding potential grid import at spike rates
- **Off-peak**: The optimizer holds conservatively at the fallback price, preserving battery for the next potential peak
- **Recovery**: When Amber comes back online, the integration immediately returns to real pricing

A persistent notification alerts you when Amber has been down for more than the configured blip tolerance.

### Modbus Health Monitoring

The integration tracks consecutive Modbus write failures. After 3 consecutive failures:

1. The `binary_sensor.victron_mpc_battery_optimizer_modbus_connected` entity turns OFF
2. A persistent notification is created: "Cannot write to Victron Cerbo GX"
3. The `modbus_healthy` and `modbus_failures` attributes update on the coordinator data

When communication is restored, the binary sensor returns to ON and a recovery notification is sent.

Common causes of Modbus failure:
- Cerbo GX rebooted or lost network
- Modbus TCP disabled on the Cerbo GX
- Network switch/router issue between HA and Cerbo GX

Note: The integration cannot auto-fix Modbus failures -- the registers remain at their last written values. This is a hardware/network issue requiring human intervention. The monitoring ensures you are alerted quickly.

See [TEST_SCENARIOS.md](TEST_SCENARIOS.md) for the full 48-scenario failure matrix covering all combinations of API failures, price events, and hardware issues.

---

## MPC Modes

The optimizer outputs one of these modes, visible in the Decision sensor:

| Mode | Meaning | Register Effect |
|------|---------|-----------------|
| `hold` | Maintain SoC, use grid for loads | Near current SoC |
| `discharge` | Use battery for loads (expensive period) | Low floor (200-400) |
| `solar_charge` | Charging from solar excess | Low floor, solar does work |
| `grid_charge` | Charge from grid (cheap period) | High (800-1000) |
| `export` | Exporting excess to grid | Low floor, R2706=70 |

---

## Data Sources

| Source | What | How |
|--------|------|-----|
| **Amber Electric** | Wholesale pricing, 30h forecast, spike detection | HA Amber integration entities |
| **Solcast (ha-solcast-solar)** | Satellite-based solar forecast (optional, Priority 0) | HA entity auto-detection |
| **VRM Portal** | 180-day solar history, consumption forecasts | API via VRM access token |
| **Open-Meteo** | Cloud layer data (low/mid/high altitude) | Free API, no key, uses HA lat/lon |
| **met.no** | Weather forecast (cloud, precipitation) | HA weather entity |
| **PetrolSpy** | Live diesel prices for genset cost | Free API, cached 24h |
| **HA entities** | Battery SoC, solar power, load, grid power | Configured in setup |

---

## Genset Integration

If you have a diesel generator with auto-start via the Cerbo GX, the integration factors genset cost into LP decisions:

- Diesel price fetched from PetrolSpy (free, cached 24h)
- Cost formula: `(diesel_$/L x 1.5 L/hr) / 4.0 kW + $0.05 maintenance`
- During spikes above genset cost, the LP knows the genset is a cheaper backup
- The integration does NOT control the genset -- it only monitors and factors cost

---

## Real-World Examples

### Sunny Day, Moderate Prices
```
10:00am, SoC=45%, Solar=2.5kW, Amber=$0.30 now/$0.48 at 6pm
Solar forecast: 18 kWh remaining (P90 clear day)

Decision: HOLD at 45%
  Solar will charge to ~90% by sunset (free energy)
  Evening peak at $0.48 -> battery discharge saves $0.48/kWh
  R2901=450, R2706=0
```

### Cheap Overnight, Cloudy Tomorrow
```
2:00am, SoC=35%, Solar=0W, Amber=$0.12 now/$0.35 at 7am
Tomorrow: overcast, 8 kWh solar forecast

Decision: GRID CHARGE to 80%
  Cheapest power in 24h window
  Little solar tomorrow to charge battery
  R2901=800, R2706=70
```

### Price Spike
```
6:00pm, SoC=40%, Solar=0W, Amber=$0.50 now/$5.00 at 10pm

Decision: GRID CHARGE now at $0.50
  LP sees $5 tonight -> pre-charges at 10x cheaper rate
  Discharges through spike, saves ~$30
  R2901=800 (charge), then 100 during spike
```

### Negative Pricing
```
1:00pm, SoC=70%, Solar=4kW, Amber=-$0.08

Override: CHARGE TO 100%
  R2901=1000 (override, not LP)
  R2706=70 (open feed-in)
  We are literally paid to consume electricity
```
