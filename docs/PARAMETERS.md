# Parameter Reference

> Definitive reference for every configurable field in the Victron MPC Battery Optimizer.
> Generated from `config.py`, `const.py`, `number.py`, `coordinator.py`, `forecasts.py`, `utils.py`, and `optimizer.py`.

**Total parameters: 52** (12 VictronSystem + 40 MPCTunables)

---

## Table of Contents

1. [Battery System (VictronSystem)](#1-battery-system-victronsystem)
2. [LP Optimizer Cost Factors](#2-lp-optimizer-cost-factors)
3. [SoC Constraints](#3-soc-constraints)
4. [Overnight Strategy](#4-overnight-strategy)
5. [Solar Forecasting](#5-solar-forecasting)
6. [Load Forecasting](#6-load-forecasting)
7. [Safety & Overrides](#7-safety--overrides)
8. [Forecast Engine](#8-forecast-engine)
9. [Genset](#9-genset)

---

## 1. Battery System (VictronSystem)

Hardware specifications that define the physical limits of the system. Set during config flow Step 2 or via options flow. These values become hard constraints in the LP.

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `battery_capacity_kwh` | 14.2 | >0 | kWh | Config Flow Step 2 | `coordinator.py` (SoC to kWh conversion), `optimizer.py` (SoC trajectory, bounds) | Total usable energy storage. All SoC percentages are converted to kWh using this value. Larger capacity means more flexibility for arbitrage. | 14.2 kWh = 296Ah at 48V. At 80% DoD, ~11.4 kWh usable. |
| `max_charge_kw` | 3.5 | >0 | kW | Config Flow Step 2 | `optimizer.py` (variable bounds on `p_charge`) | Maximum rate the battery can charge from grid or solar. Limits how fast you can absorb cheap power. Increase if your inverter supports it. | At 3.5 kW, charging from 20% to 100% takes ~3.2 hours. |
| `max_discharge_kw` | 4.5 | >0 | kW | Config Flow Step 2 | `optimizer.py` (variable bounds on `p_discharge`) | Maximum rate the battery can supply loads. Limits how much grid import you can offset during spikes. | At 4.5 kW discharge with 2 kW load, 2.5 kW excess is exported or curtailed. |
| `max_solar_kw` | 7.0 | >0 | kW | Not yet in UI | `forecasts.py` (sanity cap on solar profiles) | PV array nameplate peak. Used as a sanity ceiling on solar forecasts. Does not directly affect the LP. | 7 kW array with shading produces ~5 kW real peak. |
| `max_grid_import_kw` | 10.0 | >0 | kW | Not yet in UI | `optimizer.py` (variable bounds on `grid_import`) | Maximum power the house can draw from the grid. Typically your main breaker limit. | 10 kW allows simultaneous 3.5 kW battery charge + 6.5 kW household load. |
| `max_grid_export_kw` | 5.0 | >0 | kW | Not yet in UI | `optimizer.py` (variable bounds on `grid_export`) | Maximum power you can push to the grid. Set by your distributor's feed-in limit. | 5 kW export limit means excess solar above house load + charge is curtailed. |
| `inverter_max_kw` | 5.0 | >0 | kW | Not yet in UI | Reference only (not directly in LP) | Continuous power rating of the Victron MultiPlus/Quattro. Informational; the LP uses charge/discharge limits instead. | Quattro 48/5000 = 5 kW continuous, 10 kW peak. |
| `soc_min_pct` | 10.0 | 0-100 | % | Not yet in UI | `coordinator.py` (hard floor calculation) | Absolute hardware minimum SoC. The battery BMS will cut off below this. The optimizer never plans below `max(soc_min_pct, soc_floor_pct)`. | At 10%, a 14.2 kWh battery has 1.42 kWh emergency reserve. |
| `soc_max_pct` | 100.0 | 0-100 | % | Not yet in UI | `coordinator.py` (soc_max_kwh), `optimizer.py` (upper SoC constraint) | Maximum SoC the optimizer will target. Reduce below 100% if you want to leave headroom for solar absorption. | Setting to 95% leaves 0.71 kWh of headroom for unexpected solar. |
| `charge_efficiency` | 0.95 | 0-1 | ratio | Not yet in UI | `optimizer.py` (SoC dynamics: `eta_c * p_charge`) | Fraction of grid/solar energy that reaches the battery. Combined with discharge efficiency gives round-trip efficiency. | 0.95 means 5% loss during charging. Charging 3.5 kW for 1h stores 3.325 kWh. |
| `discharge_efficiency` | 0.95 | 0-1 | ratio | Not yet in UI | `optimizer.py` (SoC dynamics: `p_discharge / eta_d`) | Fraction of stored energy available when discharging. Lower values make cycling more expensive. | 0.95 means discharging 3.325 kWh delivers 3.159 kWh. Round-trip = 0.95 x 0.95 = 90.25%. |

### Derived Properties

| Property | Formula | Description |
|----------|---------|-------------|
| `genset_cost_per_kwh` | `(diesel_price x consumption_lph) / output_kw + maintenance` | Dynamic genset cost; see [Genset](#9-genset) section. |

---

## 2. LP Optimizer Cost Factors

These values are added to the LP objective function as $/kWh terms. They shape which actions the optimizer prefers. All are comparable in magnitude to electricity prices ($0.05-$0.50/kWh).

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `battery_wear_cost` | 0.05 | 0.01-0.10 | $/kWh | Number Entity `mpc_battery_wear_cost` | `optimizer.py` objective: `c[pd(t)] = wear_cost * dt` | Penalty applied to every kWh discharged. Prevents unnecessary cycling. **Increase**: battery discharges less (saves cycle life, but may miss profitable arbitrage). **Decrease**: more aggressive discharge (captures small price differentials, but wears battery faster). | At $0.05 wear, the optimizer only discharges when grid price > $0.05 + sell_price. If grid is $0.30 and sell is $0.06, discharge profit is $0.30 - $0.06 - $0.05 = $0.19/kWh. |
| `grid_import_penalty` | 0.02 | 0.00-0.05 | $/kWh | Not yet in UI (auto-tunable) | `optimizer.py` objective: `c[gi(t)] += import_penalty * dt` | Small penalty on all grid imports, nudging the optimizer toward self-consumption even when grid is slightly cheaper than alternatives. **Increase**: stronger preference for solar/battery over grid. **Decrease**: pure cost optimization with no self-sufficiency bias. | At $0.02 penalty, the optimizer treats a $0.20/kWh grid price as effectively $0.22/kWh, making solar slightly more attractive. |
| `sunset_reward` | 0.04 | 0.01-0.10 | $/kWh | Number Entity `mpc_sunset_reward` | `optimizer.py` objective: modifies `c[pc(k)]` and `c[pd(k)]` for `k < sunset_step` | Reward per kWh of SoC at sunset. Encourages the optimizer to have a full battery entering evening peak. **Increase**: more aggressive pre-sunset charging (may charge from grid at moderate prices). **Decrease**: sunset SoC determined purely by cost arbitrage. | At $0.04 reward with a 14.2 kWh battery, the optimizer values a full battery at sunset at $0.57 vs empty. This tips the balance toward holding solar charge rather than exporting at $0.06 FIT. |
| `terminal_reward` | 0.03 | 0.01-0.10 | $/kWh | Not yet in UI (auto-tunable) | `optimizer.py` objective: modifies `c[pc(k)]` and `c[pd(k)]` for all `k` | Reward for SoC at the end of the 24h horizon. Prevents the optimizer from draining to zero in the last few hours (horizon effect). **Increase**: more conservative end-of-horizon SoC. **Decrease**: optimizer drains battery freely in final hours. | At $0.03/kWh, the optimizer values 14.2 kWh at end-of-horizon at $0.43 total. Prevents "drain everything in the last hour" behavior. |
| `force_full_charge` reward | 2.00 | Fixed | $/kWh | Triggered by `full_charge_interval_days` | `optimizer.py`: when `force_full_charge=True`, adds $2.00/kWh terminal-like reward | Extremely strong reward that dominates all other objectives, forcing the optimizer to charge to 100% for cell balancing. Not user-configurable; activated automatically. | When triggered, $2.00/kWh is 4-10x higher than any electricity price, guaranteeing the optimizer charges fully regardless of cost. |

---

## 3. SoC Constraints

Hard limits on battery state of charge. These become inequality constraints in the LP, not soft costs.

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `soc_floor_pct` | 30.0 | 15-30 | % | Number Entity `mpc_soc_floor` | `coordinator.py` (daytime_min_kwh), `optimizer.py` (soc_min_schedule lower bound) | Minimum daytime operating SoC. The LP cannot plan below this. This is the OPERATING floor, not the hardware floor (10%) or genset trigger (~15-20%). At 30% on a 14.2 kWh battery, keeps ~4.3 kWh (1.4 kWh above hardware minimum) as emergency reserve at all times. Grid-down exception: Victron ESS handles islanding independently, genset auto-starts. **Increase**: more emergency reserve, less usable capacity. **Decrease**: more usable capacity, less safety margin. | At 30% floor on 14.2 kWh, the optimizer has 14.2 - 4.26 = 9.94 kWh of usable range. |
| `overnight_min_soc_pct` | 30.0 | 20-45 | % | Number Entity `mpc_overnight_min_soc` | `coordinator.py` (overnight_min_kwh, soc_min_schedule), `optimizer.py` (per-step SoC floor) | Hard floor during overnight hours (22:00-06:00). Prevents the battery from being drained below this level overnight, even if prices would justify it. Always >= `soc_floor_pct`. **Increase**: more overnight safety, less ability to discharge during evening peak. **Decrease**: allows deeper evening discharge. | At 30% overnight floor, 4.26 kWh is reserved. If a price spike at 23:00 occurs, the optimizer discharges to 30% and no further. A hot night with AC running at 2 kW will hit this floor in ~4 hours. |

---

## 4. Overnight Strategy

Parameters controlling battery preservation from evening through morning. The overnight hold reward is price-scaled via `utils.scale_overnight_hold_reward()`.

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `overnight_hold_reward` | 0.10 | 0.02-0.20 | $/kWh | Number Entity `mpc_overnight_hold_reward` | `utils.scale_overnight_hold_reward()`, `optimizer.py` objective: reward for SoC at morning boundary | Base reward for maintaining SoC at end of overnight period (morning). Scaled by average overnight price: full reward when grid < `price_low`, zero when > `price_high`. **Increase**: stronger overnight preservation, battery held for morning spike risk. **Decrease**: allows overnight discharge if prices justify it. | At $0.10 base with overnight avg price $0.12 (below $0.15 low threshold), full $0.10 reward applies. The optimizer values 14.2 kWh at morning at $1.42, discouraging any overnight discharge. |
| `overnight_start_hour` | 22 | 0-23 | hour | Not yet in UI | `coordinator._compute_overnight_steps()` | Hour when overnight preservation begins. The SoC floor switches from `soc_floor_pct` to `overnight_min_soc_pct`, and the hold reward becomes active. | Setting to 22 means overnight mode activates at 10 PM. Earlier values protect against early evening spikes but reduce discharge flexibility. |
| `overnight_end_hour` | 6 | 0-23 | hour | Not yet in UI | `coordinator._compute_overnight_steps()` | Hour when overnight preservation ends. After this, the daytime SoC floor resumes. | Setting to 6 means overnight protection lasts until 6 AM. The optimizer is then free to discharge if morning prices are high. |
| `overnight_price_low` | 0.15 | 0.05-0.50 | $/kWh | Number Entity `mpc_overnight_hold_price_full` | `utils.scale_overnight_hold_reward()` (price_low threshold) | Full overnight hold reward applies when average overnight price is below this. When grid power is cheap overnight, preserving battery for morning is clearly better. | At avg overnight price $0.10 (< $0.15), the full $0.10 hold reward applies. Battery is strongly preserved. |
| `overnight_price_high` | 0.25 | 0.10-1.00 | $/kWh | Number Entity `mpc_overnight_hold_price_zero` | `utils.scale_overnight_hold_reward()` (price_high threshold) | Overnight hold reward drops to zero when average overnight price exceeds this. When overnight power is expensive, discharging the battery is the right move. Linear interpolation between `price_low` and `price_high`. | At avg overnight price $0.30 (> $0.25), hold reward = $0.00. The optimizer freely discharges overnight if it saves money. At $0.20 (midpoint), reward is scaled to $0.05. |
| `full_charge_interval_days` | 14 | >=0 | days | Not yet in UI | `coordinator._check_full_charge_needed()` | Days between forced full charges for cell balancing. When due, the optimizer receives `force_full_charge=True` and charges to 100% regardless of cost. Set to 0 to disable. | Every 14 days, the optimizer is forced to charge to 100% even if prices are high. This takes ~3.2 hours at 3.5 kW from 20%. At $0.30/kWh, the balancing charge costs ~$3.40. |

---

## 5. Solar Forecasting

Parameters that control how solar production is predicted and adjusted.

### Solar Derating

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `solar_derating` | True | -- | bool | Not yet in UI | `forecasts._build_solar_forecast()` (master switch for all derating layers) | Master switch for solar forecast derating. When True, cloud/weather derating is applied. When False, raw forecasts are used as-is. | Disable this only for testing. Raw VRM P90 forecasts without derating will over-predict by 20-50% on cloudy days. |
| `solar_derating_days` | 7 | >=1 | days | Not yet in UI | `forecasts._compute_solar_derate()` (how many days of VRM history to compare) | Number of days of historical data used to compute the rolling accuracy derate factor. More days = smoother but slower to react; fewer days = responsive but noisy. | 7 days smooths out day-to-day weather variation while still adapting to seasonal changes within ~2 weeks. |
| `solar_derating_min` | 0.5 | 0.30-0.70 | ratio | Not yet in UI (auto-tunable) | `forecasts._compute_solar_derate()` (lower clamp on accuracy ratio) | Minimum allowed derate factor from rolling accuracy. Prevents the forecast from being crushed below 50% of nominal, even during extended cloudy periods. | At 0.5 minimum, even a week of heavy overcast only reduces the forecast to 50%, not 20%. This prevents the optimizer from under-estimating solar on the first sunny day after a cloudy week. |
| `solar_derating_max` | 1.0 | -- | ratio | Not yet in UI | `forecasts._compute_solar_derate()` (upper clamp -- never inflate) | Maximum allowed derate factor. Set to 1.0 to ensure forecasts are only ever reduced, never inflated. | At 1.0, even if actual production exceeds forecasts for a week, the derate stays at 1.0 (100%). Forecasts are never inflated beyond their base value. |
| `solar_cloud_impact` | 0.75 | 0.50-0.90 | ratio | Not yet in UI (auto-tunable) | `forecasts._get_cloud_derate_factors()` (cloud-to-solar reduction formula) | How strongly cloud coverage reduces solar output. Formula: `factor = max((1 - cloud% x impact)^0.5, 1 - impact)`. The sqrt dampening prevents total zeroing. **Increase**: clouds reduce solar more aggressively. **Decrease**: more optimistic under clouds. | At 0.75 impact with 80% effective cloud: raw = 1 - 0.8 x 0.75 = 0.40, factor = sqrt(0.40) = 0.63. Solar forecast is reduced to 63% of clear-sky. At 100% cloud: floor = 1 - 0.75 = 0.25, so solar never goes below 25% of forecast. |

### Cloud Layer Weighting

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `solar_cloud_layer_weights` | `{"high": 0.15, "mid": 0.5, "low": 0.9}` | 0-1 per layer | weight | Not yet in UI | `forecasts._effective_cloud_pct()` | Per-layer weights for computing effective cloud coverage from Open-Meteo data. High cirrus clouds barely block solar; low stratus blocks heavily. This prevents 100% cirrus from being treated as overcast. | 100% high cirrus (weight 0.15) = effective cloud 15%. 100% low stratus (weight 0.9) = effective cloud 90%. Mixed 50% low + 50% high = weighted (45 + 7.5)/(90 + 15) = 50% effective. |

### Day Type Classification

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `solar_day_type_percentiles` | `{"clear": 0.90, "partly_cloudy": 0.70, "overcast": 0.40, "rain": 0.15}` | 0-1 | VRM percentile | Not yet in UI | `forecasts._build_solar_forecast()` (selects VRM envelope percentile) | Maps each day type to a VRM historical production percentile. Clear days use P90 (near best-ever), rain uses P15 (near worst). **Increase a percentile**: more optimistic forecast for that day type. **Decrease**: more conservative. | On a clear day, P90 is used: the forecast is near the best production ever seen for this month. On an overcast day, P40 selects a below-average day. This captures ~4x variation in daily production. |
| `solar_day_type_cloud_clear` | 30.0 | 0-100 | % | Not yet in UI | `forecasts._classify_day_type()` (clear threshold) | Maximum effective cloud coverage for a "clear" classification. Below this + low precipitation = clear day type. | With mean cloud 25% and no precipitation, day is classified as "clear" and gets the P90 forecast. |
| `solar_day_type_cloud_overcast` | 70.0 | 0-100 | % | Not yet in UI | `forecasts._classify_day_type()` (overcast threshold) | Minimum effective cloud coverage for an "overcast" classification (when precipitation < heavy). | With mean cloud 75% and 0.5mm precipitation, day is "overcast" -> P40 forecast. |
| `solar_day_type_precip_light` | 1.0 | >=0 | mm | Not yet in UI | `forecasts._classify_day_type()` (clear day requires precip below this) | Maximum total precipitation for a day to qualify as "clear". | 0.5mm dew/mist still classifies as clear. 1.5mm light rain pushes to partly_cloudy. |
| `solar_day_type_precip_heavy` | 2.0 | >=0 | mm | Not yet in UI | `forecasts._classify_day_type()` (rain threshold) | Total precipitation above this forces "rain" classification regardless of cloud. | 3mm rain = "rain" day type -> P15 forecast. This is the most conservative, expecting ~15th percentile production. |

### Intraday Correction

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `intraday_early_hour` | 8.0 | 0-12 | hour | Not yet in UI | `forecasts._maybe_adjust_day_type()` (earliest hour to check yield) | Hour when intraday yield comparison begins. Before this, actual yield is too small to judge. | At 8.0, the system starts comparing actual vs expected yield at 8 AM. |
| `intraday_early_threshold` | 0.30 | 0-1 | ratio | Not yet in UI | `forecasts._maybe_adjust_day_type()` (downgrade threshold before 10am) | Before 10 AM, if actual yield is less than 30% of expected, downgrade the day type one level. Aggressive early detection preserves battery for bad days. | Expected 2 kWh by 9 AM but only 0.4 kWh produced. Ratio = 0.20 < 0.30 -> downgrade "clear" to "partly_cloudy". Forecast drops from P90 to P70. |
| `intraday_standard_threshold` | 0.60 | 0-1 | ratio | Not yet in UI | `forecasts._maybe_adjust_day_type()` (downgrade threshold after 10am) | After 10 AM, if actual yield is less than 60% of expected, downgrade the day type. Less aggressive than early threshold because more data is available. | Expected 8 kWh by 1 PM but only 4 kWh produced. Ratio = 0.50 < 0.60 -> downgrade "partly_cloudy" to "overcast". |
| `intraday_upgrade_threshold` | 1.50 | >1 | ratio | Not yet in UI | `forecasts._maybe_adjust_day_type()` (upgrade threshold) | If actual yield exceeds 150% of expected, upgrade the day type one level. Catches unexpectedly sunny days. | Expected 5 kWh by noon but produced 8 kWh. Ratio = 1.60 > 1.50 -> upgrade "overcast" to "partly_cloudy". Forecast increases from P40 to P70. |
| `cloud_override_low_pct` | 80.0 | 0-100 | % | Not yet in UI | `forecasts._maybe_adjust_day_type()` (low cloud override) | If current low-altitude cloud coverage exceeds this, force "overcast" classification regardless of the weather forecast. Low stratus is thick and blocks most solar. | Real-time Open-Meteo shows 90% low stratus. Even if met.no forecast said "partly_cloudy", the system overrides to "overcast" and switches to P40. |

---

## 6. Load Forecasting

Parameters that adjust the predicted household consumption profile.

### Inflation & History

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `load_inflation_pct` | 10.0 | 5-25 | % | Number Entity `mpc_load_inflation` | `forecasts._build_load_forecast()` (multiplier after seasonal adjustment) | Safety margin applied to load forecasts. The load profile is multiplied by `1 + inflation/100`. **Increase**: more conservative forecasts (less likely to run out of battery). **Decrease**: tighter forecasts (more aggressive dispatch). | At 10% inflation, a forecast of 1.0 kW load becomes 1.1 kW. Over 24h at 1 kW average, this adds 2.4 kWh to the expected consumption. |
| `history_days` | 7 | >=1 | days | Not yet in UI | `forecasts._build_solar_profile_from_history()`, `forecasts._build_load_profile_from_history()` | Number of days of HA recorder history to use for building solar and load profiles when VRM/Solcast data is unavailable. More days = smoother profiles. | 7 days averages a full week of data, capturing both weekday and weekend patterns. |

### Seasonal & Temperature Adjustment

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `seasonal_load_adjustment` | True | -- | bool | Not yet in UI | `forecasts._seasonal_load_scale()` (master switch) | Enable/disable seasonal load scaling from VRM monthly consumption history + temperature. When disabled, returns 1.0 (no seasonal adjustment). | Disable if you have a very stable load profile year-round (no heating/cooling). |
| `temp_base_cool` | 15.0 | -- | C | Not yet in UI | `forecasts._seasonal_load_scale()` (below this temp, heating adjustment applies) | Temperature below which heating load adjustment kicks in. Below this, load increases by `temp_cool_pct_per_degree` per degree below the threshold. | At 10C outdoor temp (5C below threshold) with 1%/degree: load scale increases by 5%. |
| `temp_base_heat` | 26.0 | -- | C | Not yet in UI | `forecasts._seasonal_load_scale()` (above this temp, cooling adjustment applies) | Temperature above which cooling load adjustment kicks in. Above this, load increases by `temp_heat_pct_per_degree` per degree above the threshold. | At 32C outdoor temp (6C above threshold) with 3.3%/degree: load scale increases by 19.8%. |
| `temp_cool_pct_per_degree` | 1.0 | >=0 | %/C | Not yet in UI | `forecasts._seasonal_load_scale()` | Load increase per degree below `temp_base_cool`. Heating is less energy-intensive than cooling in most setups. | At 1%/degree, a 5C cold snap (10C day) adds 5% to the load forecast. |
| `temp_heat_pct_per_degree` | 3.3 | >=0 | %/C | Not yet in UI | `forecasts._seasonal_load_scale()` | Load increase per degree above `temp_base_heat`. AC cooling is energy-intensive; each degree above comfort adds significant load. | At 3.3%/degree, a 35C day (9C above threshold) adds 29.7% to the load forecast. Two AC units at 2 kW each = 4 kW extra. |

### AC Demand Detection

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `indoor_temp_ac_threshold` | 24.0 | -- | C | Not yet in UI | `forecasts._indoor_temp_ac_boost()` (temperature trigger for boost) | Indoor temperature above which AC cooling demand is estimated. For each zone above this threshold, extra load is added. AC sensors often read 1-2C warm. | AC sensor reads 25C (real room ~23C). Excess = 1C. With 2 hot zones at 0.8 kW/C/zone: boost = 2 x 1 x 0.8 = 1.6 kW. |
| `indoor_ac_kw_per_degree` | 0.8 | >=0 | kW/C | Not yet in UI | `forecasts._indoor_temp_ac_boost()` (temperature-based load per zone per degree) | Load added per zone per degree above `indoor_temp_ac_threshold`. Multiplied by (zones_hot x max_excess_temp). | With 2 zones, max excess 3C: boost = 2 x 3 x 0.8 = 4.8 kW (capped at 5 kW). |
| `indoor_ac_boost_hours` | 5 | >=1 | hours | Not yet in UI | `forecasts._build_load_forecast()` (duration of AC boost: 2h full + taper) | Total duration of the AC load boost. First 2 hours at full boost, then linear taper to zero over remaining hours. | 5 hours total: 2h full boost at 2 kW, then 3h linear taper (2 kW -> 0 kW). Total extra energy: 2x2 + 3x1 = 7 kWh. |
| `indoor_ac_running_kw` | 2.0 | >=0 | kW | Not yet in UI | `forecasts._indoor_temp_ac_boost()` (flat load per running AC unit) | Load assumed per AC unit that is confirmed running (climate entity not "off"). This is the flat-rate signal; the system uses the higher of this and the temperature-based signal. | If climate.ac1 state = "cool" and climate.ac2 state = "cool": AC running boost = 2 x 2.0 = 4.0 kW. |

---

## 7. Safety & Overrides

Parameters controlling safety responses to price spikes, API outages, and export decisions. These override the LP result in the coordinator.

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `spike_threshold` | 1.00 | 0.50-5.00 | $/kWh | Number Entity `mpc_spike_threshold` | `coordinator._async_update_data()` (override check: `buy_price > spike_threshold`), `optimizer._build_output()` (lookahead spike detection) | Price above which the system forces discharge regardless of LP result. R2901 is set to 100 (10% = hardware minimum). **Increase**: fewer spike overrides, trusts LP more. **Decrease**: more aggressive spike protection. | At $1.00 threshold, a price of $1.50/kWh triggers immediate discharge. At 4.5 kW discharge for 5 min = 0.375 kWh saved from grid at $1.50 = $0.56 saved per cycle. |
| `defensive_price` | 2.00 | 0.50-5.00 | $/kWh | Number Entity `mpc_defensive_price` | `coordinator._check_amber_health()` (price assumed during extended Amber outage) | When Amber API is unavailable beyond `amber_blip_minutes`, this price is used. A high value forces discharge (assumes spike risk). **Increase**: more aggressive defensive discharge. **Decrease**: more relaxed during outages. | At $2.00, the LP sees every timestep as expensive and immediately discharges. The cost of unnecessary discharge ($0.05/kWh wear) is trivial vs the cost of staying on grid during a real $20/kWh spike. |
| `fallback_price` | 0.30 | >0 | $/kWh | Not yet in UI | `coordinator.py` (initial `_last_known_buy_price`) | Default price used when no price data is available at all. Used as initial value for `_last_known_buy_price` before any Amber data is received. | $0.30 is roughly the average wholesale + network cost in most Australian markets. |
| `amber_blip_minutes` | 5.0 | 1-15 | min | Number Entity `mpc_amber_blip_minutes` | `coordinator._check_amber_health()` (grace period before defensive mode) | Minutes of Amber unavailability tolerated before switching to defensive mode. During the grace period, the last known price is used. **Increase**: more tolerant of brief outages. **Decrease**: faster defensive response. | At 5 minutes, a brief Amber API hiccup (network glitch, server restart) is ignored. After 5 min, the system assumes spike risk and uses `defensive_price`. |
| `feedin_export_threshold` | 0.10 | 0.01-0.50 | $/kWh | Number Entity `mpc_feedin_export_threshold` | `coordinator._compute_feedin_value()` (Rule 3: min FIT for spike export) | Minimum feed-in tariff required to allow export during a price spike (R2706 Rule 3). Prevents exporting at rock-bottom FIT during spikes. **Increase**: only export during spikes if FIT is lucrative. **Decrease**: export during spikes even at low FIT. | During a spike with FIT = $0.60 and threshold = $0.10: export allowed. At FIT = $0.05 (below threshold): export blocked, battery used for self-consumption instead. |
| `feedin_soc_threshold` | 30.0 | >0 | % | Not yet in UI | `coordinator._compute_feedin_value()` (Rule 3: min SoC for spike export) | Minimum SoC required to allow export during spikes. Prevents exporting when battery is low and you might need the reserve. | At SoC 25% (below 30% threshold): even during a spike with high FIT, export is blocked. The battery is preserved for self-consumption. |

---

## 8. Forecast Engine

Parameters that define the optimization horizon and resolution.

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `forecast_hours` | 24 | >=1 | hours | Not yet in UI | `coordinator.py` (horizon_steps), `optimizer.py` (N), all forecast builders | Rolling forecast horizon. Longer horizons let the optimizer plan further ahead but require more forecast data and compute. | 24 hours = 288 five-minute steps. The optimizer sees a full day-night cycle, enabling charge during cheap overnight periods for expensive morning peaks. |
| `dt_minutes` | 5 | >0, divides 60 | min | Not yet in UI | All modules (`dt_hours = dt_minutes/60`, `steps_per_hour = 60/dt_minutes`) | Optimization timestep. Smaller steps give finer control but increase LP size. 5 minutes matches the coordinator update interval. | At 5 min, the LP has 5 x 288 = 1440 decision variables and 2 x 288 = 576 SoC constraints. Solve time ~50ms on HiGHS. Increasing to 1 min would give 1440 steps and much longer solve times. |

### Derived Properties

| Property | Formula | Description |
|----------|---------|-------------|
| `horizon_steps` | `forecast_hours x 60 / dt_minutes` | Total optimization steps (default: 288) |
| `dt_hours` | `dt_minutes / 60` | Timestep in hours (default: 0.0833) |
| `steps_per_hour` | `60 / dt_minutes` | Steps per hour (default: 12) |

---

## 9. Genset

Parameters for the diesel backup generator. The genset is controlled independently by the Victron Cerbo GX; MPC only monitors it and uses its cost in LP decisions.

| Field | Default | Range | Unit | Where Set | Used In | Effect | Example |
|-------|---------|-------|------|-----------|---------|--------|---------|
| `genset_diesel_price_per_litre` | 2.20 | >0 | AUD/L | Not yet in UI (updated live via PetrolSpy) | `VictronSystem.genset_cost_per_kwh` (property), `coordinator.py` (live update from PetrolSpy) | Current diesel price. Updated automatically from PetrolSpy (Melbourne median). The genset cost is used as a price floor: when grid price exceeds genset cost, the genset is the cheaper option and the LP sets its floor accordingly. | At $2.20/L: cost = (2.20 x 1.5) / 4.0 + 0.05 = $0.875/kWh. |
| `genset_consumption_lph` | 1.5 | >0 | L/hr | Not yet in UI | `VictronSystem.genset_cost_per_kwh` (fuel cost numerator) | Diesel consumption at typical 50-75% load. Higher loads consume more fuel. | At 1.5 L/hr and $2.20/L diesel, fuel cost alone is $3.30/hr or $0.825/kW. |
| `genset_output_kw` | 4.0 | >0 | kW | Not yet in UI | `VictronSystem.genset_cost_per_kwh` (fuel cost denominator) | Effective electrical output at typical load. The CD6500 is rated at 5.7 kW but typically runs at 4 kW (70% load) for efficiency and longevity. | At 4.0 kW output, the genset delivers 4 kWh per hour of operation. |
| `genset_maintenance_per_kwh` | 0.05 | >=0 | $/kWh | Not yet in UI | `VictronSystem.genset_cost_per_kwh` (added to fuel cost) | Maintenance allowance covering oil changes, filters, servicing. Added to the per-kWh fuel cost. | $0.05/kWh over 500 hours of operation = $100 maintenance reserve per year. At typical usage (5 hrs/month), $30/year. |

### Derived: `genset_cost_per_kwh`

**Formula**: `(genset_diesel_price_per_litre x genset_consumption_lph) / genset_output_kw + genset_maintenance_per_kwh`

**At defaults**: `(2.20 x 1.5) / 4.0 + 0.05 = $0.875/kWh`

The genset cost tells the optimizer: "grid prices above this are more expensive than running the diesel generator." During extreme spikes ($5-$25/kWh), the genset is dramatically cheaper than grid power, so the optimizer discharges aggressively knowing the genset provides a safety net.

---

## Constants (const.py)

These are fixed values used by the integration. They are not user-configurable but are documented here for reference.

| Constant | Value | Description |
|----------|-------|-------------|
| `REGISTER_ESS_MIN_SOC` | 2901 | Victron Modbus register for ESS minimum SoC. Value = SoC% x 10, range 100-1000. **Critical**: When register >= current SoC, ESS charges from grid. When register < current SoC, battery discharges freely down to register value. Register logic by mode: `grid_charge` = target SoC (above current), `solar_charge` = hard floor 300 (30%), `discharge`/`hold` = LP trajectory floor - 5% buffer (prevents grid import from register being too close to SoC). |
| `REGISTER_MAX_FEED_IN` | 2706 | Victron Modbus register for maximum grid feed-in power. Units = 100W/value (70 = 7000W). |
| `REGISTER_ESS_MIN` | 100 | Minimum valid R2901 value (10% SoC). |
| `REGISTER_ESS_MAX` | 1000 | Maximum valid R2901 value (100% SoC). |
| `REGISTER_FEEDIN_MAX` | 70 | Maximum R2706 value (7000W export). |
| `REGISTER_FEEDIN_BLOCK` | 0 | R2706 value to block all export. |
| `UPDATE_INTERVAL_MINUTES` | 5 | Coordinator update cycle interval. |
| `STALE_THRESHOLD_MINUTES` | 10 | No successful update in this time triggers Data Stale binary sensor. |
| `DEFAULT_MODBUS_FAILURE_THRESHOLD` | 3 | Consecutive Modbus write failures before alerting. |

---

## Auto-Tunable Bounds

The following parameters have defined bounds used for future auto-tuning. Values outside these bounds are clamped.

| Field | Min | Max |
|-------|-----|-----|
| `battery_wear_cost` | 0.01 | 0.10 |
| `grid_import_penalty` | 0.00 | 0.05 |
| `sunset_reward` | 0.01 | 0.10 |
| `terminal_reward` | 0.01 | 0.10 |
| `overnight_hold_reward` | 0.02 | 0.20 |
| `soc_floor_pct` | 15.0 | 30.0 |
| `overnight_min_soc_pct` | 20.0 | 45.0 |
| `load_inflation_pct` | 5.0 | 25.0 |
| `solar_cloud_impact` | 0.50 | 0.90 |
| `solar_derating_min` | 0.30 | 0.70 |
