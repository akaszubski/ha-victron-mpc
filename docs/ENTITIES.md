# Entity Reference

All entities are grouped under a single **Victron MPC Battery Optimizer** device. Entity IDs follow the pattern `{domain}.victron_mpc_battery_optimizer_{name}`.

---

## Sensors

### Battery Plan

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_battery_plan` |
| **State** | Target SoC percentage (e.g., `45.0`) |
| **Unit** | % |
| **Device Class** | battery |
| **Icon** | mdi:battery-charging |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `mode` | string | Current mode: `hold`, `discharge`, `solar_charge`, `grid_charge`, `export` |
| `reason` | string | Human-readable explanation of the decision |
| `target_register` | int | R2901 value that was/would be written (100-1000) |
| `feedin_register` | int | R2706 value that was/would be written (0 or 70) |
| `shadow_mode` | bool | Whether shadow mode is active |
| `last_push` | int | Unix timestamp of last update |
| `soc_1h_pct` | float | Planned SoC in 1 hour |
| `soc_2h_pct` | float | Planned SoC in 2 hours |
| `soc_3h_pct` | float | Planned SoC in 3 hours |
| `soc_4h_pct` | float | Planned SoC in 4 hours |

### Decision

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_decision` |
| **State** | Current mode string (e.g., `discharge`) |
| **Icon** | mdi:brain |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `reason` | string | Decision explanation |
| `target_soc_pct` | float | Target SoC from optimizer |
| `target_register` | int | R2901 value |
| `buy_price_actual` | float | Current Amber buy price |
| `sell_price_actual` | float | Current Amber feed-in price |
| `buy_price_forecast` | float | LP's price input for this step |
| `sell_price_forecast` | float | LP's sell price input |
| `spike` | bool | Whether spike override is active |
| `shadow_mode` | bool | Whether shadow mode is active |
| `override_applied` | bool | Whether an override replaced the LP decision |
| `override_reason` | string | Override explanation (empty if no override) |
| `cloud_coverage` | int | Total cloud coverage from weather entity |
| `weather` | string | Weather condition string |
| `solar_forecast_source` | string | Active solar forecast source (e.g., `solcast_ha`, `clearsky_p90`, `clearsky_p40`) |
| `solar_day_type` | string | Weather classification (clear/partly_cloudy/overcast/rain) |
| `battery_soc_pct` | float | Current battery SoC |
| `current_solar_w` | float | Current solar production in watts |
| `current_load_w` | float | Current household load in watts |
| `schedule_30min` | string | JSON array of 30-min SoC/price schedule for dashboard |
| `soc_1h_pct` through `soc_4h_pct` | float | Planned SoC trajectory |

### Effective Price

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_effective_price` |
| **State** | Weighted price used for decision (e.g., `0.2850`) |
| **Unit** | $/kWh |
| **Icon** | mdi:currency-usd |

### 24h Projected Cost

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_24h_projected_cost` |
| **State** | Total projected cost over 24h horizon |
| **Unit** | $ |
| **Device Class** | monetary |
| **Icon** | mdi:cash-multiple |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `grid_cost` | float | Projected grid import cost |
| `export_revenue` | float | Projected export revenue |
| `wear_cost` | float | Projected battery wear cost |

### Solar Input

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_solar_input` |
| **State** | Current solar production (e.g., `2450`) |
| **Unit** | W |
| **Device Class** | power |
| **State Class** | measurement |
| **Icon** | mdi:solar-power |

### Load Input

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_load_input` |
| **State** | Current household load (e.g., `1200`) |
| **Unit** | W |
| **Device Class** | power |
| **State Class** | measurement |
| **Icon** | mdi:home-lightning-bolt |

### Buy Price

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_buy_price` |
| **State** | Current buy price (e.g., `0.2850`) |
| **Unit** | $/kWh |
| **Icon** | mdi:cash-minus |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `spike` | bool | Whether spike is active |
| `mpc_forecast_price` | float | LP's forecast price for this step |

### Sell Price

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_sell_price` |
| **State** | Current feed-in price (e.g., `0.0750`) |
| **Unit** | $/kWh |
| **Icon** | mdi:cash-plus |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `mpc_forecast_price` | float | LP's forecast sell price for this step |

### Cloud Coverage

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_cloud_coverage` |
| **State** | Effective cloud percentage (e.g., `23.5`) |
| **Unit** | % |
| **State Class** | measurement |
| **Icon** | mdi:weather-cloudy |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `weather_condition` | string | HA weather entity state |
| `temperature` | float | Current temperature |
| `humidity` | float | Current humidity |
| `cloud_low_pct` | float | Low cloud layer percentage (stratus) |
| `cloud_mid_pct` | float | Mid cloud layer percentage (altostratus) |
| `cloud_high_pct` | float | High cloud layer percentage (cirrus) |
| `effective_cloud_pct` | float | Weighted effective cloud |
| `cloud_source` | string | `open-meteo_layers` or `met.no_total` |

### Solar Forecast Today

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_solar_forecast_today` |
| **State** | Forecasted total solar production (e.g., `18.5`) |
| **Unit** | kWh |
| **Icon** | mdi:solar-power-variant |

**Attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `solar_derate` | float | Cloud derating factor applied (0.0-1.0) |
| `solar_forecast_source` | string | Active forecast source (`solcast_ha`, `clearsky_p90`, `vrm_30d_avg`, `ha_history`, `bell_curve`) |
| `solar_day_type` | string | Weather classification |
| `load_forecast_source` | string | Load forecast source |
| `seasonal_load_factor` | float | Seasonal load multiplier |
| `forecast_1h_w` through `forecast_4h_w` | int | Hourly solar forecast lookahead in watts |

### Solver Time

| | |
|---|---|
| **Entity ID** | `sensor.victron_mpc_battery_optimizer_solver_time` |
| **State** | LP solve time (e.g., `52.3`) |
| **Unit** | ms |
| **State Class** | measurement |
| **Icon** | mdi:timer-outline |

---

## Number Entities (10 Tunables)

All decision thresholds are configurable via the UI -- no hardcoded values remain in the decision logic. Changes take effect on the next 5-minute optimization cycle.

### Optimization Cost Factors

| Entity ID | Name | Min | Max | Step | Unit | Default | Mode |
|-----------|------|-----|-----|------|------|---------|------|
| `number.…_battery_wear_cost` | Battery Wear Cost | 0.01 | 0.10 | 0.01 | $/kWh | 0.05 | Box |
| `number.…_sunset_reward` | Sunset Reward | 0.01 | 0.10 | 0.01 | $/kWh | 0.04 | Box |
| `number.…_overnight_hold_reward` | Overnight Hold Reward | 0.02 | 0.20 | 0.01 | $/kWh | 0.10 | Box |

### SoC Constraints

| Entity ID | Name | Min | Max | Step | Unit | Default | Mode |
|-----------|------|-----|-----|------|------|---------|------|
| `number.…_soc_floor` | SoC Floor | 15 | 30 | 1 | % | 20 | Slider |
| `number.…_overnight_min_soc` | Overnight Min SoC | 20 | 45 | 1 | % | 30 | Slider |

### Load Forecast

| Entity ID | Name | Min | Max | Step | Unit | Default | Mode |
|-----------|------|-----|-----|------|------|---------|------|
| `number.…_load_inflation` | Load Inflation | 5 | 25 | 1 | % | 10 | Slider |

### Safety & Override Thresholds

| Entity ID | Name | Min | Max | Step | Unit | Default | Mode |
|-----------|------|-----|-----|------|------|---------|------|
| `number.…_spike_threshold` | Spike Threshold | 0.50 | 5.00 | 0.10 | $/kWh | 1.00 | Box |
| `number.…_defensive_price` | Defensive Price | 0.50 | 5.00 | 0.10 | $/kWh | 2.00 | Box |
| `number.…_amber_blip_minutes` | Amber Blip Minutes | 1 | 15 | 1 | min | 5 | Slider |
| `number.…_feedin_export_threshold` | Feed-in Export Threshold | 0.01 | 0.50 | 0.01 | $/kWh | 0.10 | Box |

(Entity IDs abbreviated -- full prefix is `number.victron_mpc_battery_optimizer`)

**Spike Threshold**: The buy price above which the override logic forces an immediate discharge (R2901=100). Lower values trigger spike discharge more aggressively. At the default of $1.00/kWh, anything above $1/kWh is treated as a spike.

**Defensive Price**: The assumed buy price during evening peak hours (17:00-21:00) when the Amber API is unavailable. The optimizer uses this value to decide whether to discharge defensively. A higher value makes defensive discharge more aggressive.

**Amber Blip Minutes**: How many minutes of continuous Amber unavailability before defensive mode activates. Short Amber glitches (under this threshold) use the last known price instead of switching to defensive pricing. Increase this if Amber has brief intermittent outages that cause unnecessary defensive triggers.

**Feed-in Export Threshold**: The minimum feed-in tariff (FIT) price required to allow grid export during a spike. During a spike, the integration only opens feed-in (R2706=70) if the FIT exceeds this value AND SoC is above the feed-in SoC threshold. Prevents exporting at negligible FIT rates.

---

## Switches

### Shadow Mode

| | |
|---|---|
| **Entity ID** | `switch.victron_mpc_battery_optimizer_shadow_mode` |
| **Default** | ON |
| **Icon** | mdi:eye-outline |

When ON, the integration computes and logs decisions but does **not** write Modbus registers. Turn OFF to enable live battery control.

---

## Binary Sensors

### Data Stale

| | |
|---|---|
| **Entity ID** | `binary_sensor.victron_mpc_battery_optimizer_data_stale` |
| **Device Class** | problem |
| **Icon** | mdi:clock-alert-outline |

Turns ON when the coordinator has not successfully updated (data is stale). This typically means an API or entity dependency is unavailable.

### Spike Override Active

| | |
|---|---|
| **Entity ID** | `binary_sensor.victron_mpc_battery_optimizer_spike_override_active` |
| **Icon** | mdi:flash-alert |

Turns ON when a price spike override is actively forcing discharge (R2901=100).

### Modbus Connected

| | |
|---|---|
| **Entity ID** | `binary_sensor.victron_mpc_battery_optimizer_modbus_connected` |
| **Device Class** | connectivity |
| **Icon** | mdi:lan-connect |

Turns OFF when Modbus communication to the Victron Cerbo GX has failed 3 or more consecutive times. This means register writes (R2901, R2706) are NOT being applied. The registers remain at their last successfully written values.

When communication is restored, the sensor returns to ON and a recovery persistent notification is sent.

**Related coordinator data attributes**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `modbus_healthy` | bool | True if fewer than 3 consecutive write failures |
| `modbus_failures` | int | Count of consecutive Modbus write failures (resets on success) |

---

## Example Lovelace Dashboard

### Basic MPC Status Card

```yaml
type: entities
title: MPC Battery Optimizer
entities:
  - entity: sensor.victron_mpc_battery_optimizer_battery_plan
    name: Target SoC
  - entity: sensor.victron_mpc_battery_optimizer_decision
    name: Mode
  - entity: sensor.victron_mpc_battery_optimizer_effective_price
    name: Price
  - entity: sensor.victron_mpc_battery_optimizer_solar_forecast_today
    name: Solar Forecast
  - entity: sensor.victron_mpc_battery_optimizer_24h_projected_cost
    name: 24h Cost
  - entity: switch.victron_mpc_battery_optimizer_shadow_mode
    name: Shadow Mode
  - entity: binary_sensor.victron_mpc_battery_optimizer_data_stale
    name: Health
  - entity: binary_sensor.victron_mpc_battery_optimizer_spike_override_active
    name: Spike Active
  - entity: binary_sensor.victron_mpc_battery_optimizer_modbus_connected
    name: Modbus
```

### Tuning Sliders Card

```yaml
type: entities
title: MPC Tunables
entities:
  - entity: number.victron_mpc_battery_optimizer_battery_wear_cost
    name: Wear Cost
  - entity: number.victron_mpc_battery_optimizer_sunset_reward
    name: Sunset Reward
  - entity: number.victron_mpc_battery_optimizer_overnight_hold_reward
    name: Hold Reward
  - entity: number.victron_mpc_battery_optimizer_soc_floor
    name: SoC Floor
  - entity: number.victron_mpc_battery_optimizer_overnight_min_soc
    name: Overnight Floor
  - entity: number.victron_mpc_battery_optimizer_load_inflation
    name: Load Safety Margin
```

### SoC Trajectory Card (Markdown)

```yaml
type: markdown
title: SoC Trajectory
content: |
  **Mode**: {{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'mode') }}
  **Reason**: {{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'reason') }}

  | Time | SoC |
  |------|-----|
  | Now | {{ states('sensor.victron_battery_state_of_charge') }}% |
  | +1h | {{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'soc_1h_pct') }}% |
  | +2h | {{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'soc_2h_pct') }}% |
  | +3h | {{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'soc_3h_pct') }}% |
  | +4h | {{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'soc_4h_pct') }}% |

  R2901={{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'target_register') }},
  R2706={{ state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'feedin_register') }}
  {% if state_attr('sensor.victron_mpc_battery_optimizer_battery_plan', 'shadow_mode') %}
  **(SHADOW MODE)**
  {% endif %}
```

### Decision Context Card (Markdown)

```yaml
type: markdown
title: MPC Decision Context
content: |
  **Weather**: {{ state_attr('sensor.victron_mpc_battery_optimizer_decision', 'weather') }}
  **Day Type**: {{ state_attr('sensor.victron_mpc_battery_optimizer_decision', 'solar_day_type') }}
  **Forecast Source**: {{ state_attr('sensor.victron_mpc_battery_optimizer_decision', 'solar_forecast_source') }}
  **Cloud**: {{ states('sensor.victron_mpc_battery_optimizer_cloud_coverage') }}% effective

  **Buy**: ${{ states('sensor.victron_mpc_battery_optimizer_buy_price') }}/kWh
  **Sell**: ${{ states('sensor.victron_mpc_battery_optimizer_sell_price') }}/kWh
  **Solar**: {{ states('sensor.victron_mpc_battery_optimizer_solar_input') }}W
  **Load**: {{ states('sensor.victron_mpc_battery_optimizer_load_input') }}W

  {% if state_attr('sensor.victron_mpc_battery_optimizer_decision', 'override_applied') %}
  **OVERRIDE**: {{ state_attr('sensor.victron_mpc_battery_optimizer_decision', 'override_reason') }}
  {% endif %}
```
