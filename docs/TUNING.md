# Tuning Guide

## Overview

All key optimization parameters are exposed as **number entities** in the HA UI. Changes take effect on the next 5-minute optimization cycle -- no restarts required.

Additional tunables are available in the integration's **Options** flow (Settings > Devices & Services > Victron MPC > Configure).

---

## Cost Factors

These control the LP's trade-offs. All values are in $/kWh to be directly comparable with electricity prices.

### Battery Wear Cost -- Default: $0.05

**Entity**: `number.victron_mpc_battery_optimizer_battery_wear_cost`

Penalty for discharging. Discourages unnecessary cycling.

Calculation: $4000 battery cost / (14.2 kWh x 6000 warranted cycles x 2) ~ $0.023, doubled for a conservative estimate.

| Value | Effect |
|-------|--------|
| Lower ($0.02) | More aggressive cycling, uses battery more freely |
| Higher ($0.08) | Less cycling, only discharges for clear savings |

**Range**: $0.01 - $0.10

### Sunset Reward -- Default: $0.04

**Entity**: `number.victron_mpc_battery_optimizer_sunset_reward`

Reward for having a full battery at sunset (evening peak preparation).

| Value | Effect |
|-------|--------|
| Lower ($0.02) | Less urgency to fill battery before evening |
| Higher ($0.08) | Stronger drive to reach 100% by sunset |

**Range**: $0.01 - $0.10

### Overnight Hold Reward -- Default: $0.10 (maximum)

**Entity**: `number.victron_mpc_battery_optimizer_overnight_hold_reward`

Maximum reward for preserving battery charge during overnight hours. This is **price-scaled** -- the value you set is the maximum, not a fixed value.

Before each optimization, the reward is scaled based on average overnight grid price:
- Grid <= $0.15 -> full value (preserve for morning spikes)
- Grid $0.22 -> ~50% of value (moderate hold)
- Grid >= $0.30 -> $0.00 (no hold -- discharging saves money)

| Value | Effect |
|-------|--------|
| Lower max ($0.05) | Less overnight preservation overall |
| Higher max ($0.15) | Stronger hold, even at moderate grid prices |

**Range**: $0.02 - $0.20

---

## SoC Constraints

### SoC Floor -- Default: 20%

**Entity**: `number.victron_mpc_battery_optimizer_soc_floor`

Daytime minimum SoC. The optimizer will not plan to discharge below this level during the day (6:00-22:00).

**Range**: 15% - 30%

### Overnight Min SoC -- Default: 30%

**Entity**: `number.victron_mpc_battery_optimizer_overnight_min_soc`

Hard constraint during overnight hours (22:00-06:00). The LP physically cannot plan below this level overnight, regardless of price signals.

**Range**: 20% - 45%

---

## Load Forecast

### Load Inflation -- Default: 10%

**Entity**: `number.victron_mpc_battery_optimizer_load_inflation`

Safety margin applied to all load forecasts. Prevents the optimizer from underestimating demand.

| Value | Effect |
|-------|--------|
| Lower (5%) | Tighter forecast, more efficient but riskier |
| Higher (20%) | More conservative, keeps extra battery buffer |

**Range**: 5% - 25%

---

## Options Flow Tunables

These are available via **Settings** > **Devices & Services** > **Victron MPC** > **Configure**:

| Parameter | Default | Description |
|-----------|---------|-------------|
| Battery Wear Cost | $0.05 | Same as the number entity |
| Sunset Reward | $0.04 | Same as the number entity |
| Overnight Hold Reward | $0.10 | Same as the number entity |
| SoC Floor (%) | 20 | Same as the number entity |
| Overnight Minimum SoC (%) | 30 | Same as the number entity |
| Shadow Mode | ON | Log decisions without writing registers |

Changes made in the Options flow are immediately reflected in the corresponding number/switch entities, and vice versa.

---

## Tunable Bounds Reference

All tunables are clamped to safe ranges to prevent misconfiguration:

| Parameter | Min | Max | Step | Unit |
|-----------|-----|-----|------|------|
| Battery Wear Cost | 0.01 | 0.10 | 0.01 | $/kWh |
| Sunset Reward | 0.01 | 0.10 | 0.01 | $/kWh |
| Overnight Hold Reward | 0.02 | 0.20 | 0.01 | $/kWh |
| SoC Floor | 15 | 30 | 1 | % |
| Overnight Min SoC | 20 | 45 | 1 | % |
| Load Inflation | 5 | 25 | 1 | % |

---

## Common Tuning Scenarios

### "Battery draining overnight too much"

1. **Increase Overnight Min SoC** -- raise the hard floor (e.g., 30% -> 35%)
2. **Increase Overnight Hold Reward** -- raise the maximum (e.g., $0.10 -> $0.15) to hold more aggressively at moderate prices
3. **Increase Load Inflation** -- the optimizer may be underestimating overnight load

Check the Decision sensor's `buy_price_actual` attribute to see what overnight prices look like. If prices are consistently above $0.25, the hold reward is correctly scaling down -- consider raising the max.

### "Not using battery enough during expensive periods"

1. **Decrease Battery Wear Cost** -- reduce the cycling penalty (e.g., $0.05 -> $0.03) to allow more liberal discharge
2. **Decrease SoC Floor** -- allow deeper discharge (e.g., 20% -> 15%)
3. Check the Battery Plan sensor's `mode` -- if it says `hold` when prices are high, the wear cost is likely too high relative to the price differential

### "Solar forecast too optimistic"

1. Check the Solar Forecast Today sensor's `solar_forecast_source` attribute
   - If `solcast_ha`: Solcast is active and should be highly accurate. If still overestimating, check your Solcast rooftop configuration (panel orientation, tilt, shading) at [solcast.com](https://toolkit.solcast.com.au/)
   - If using VRM sources: check `solar_day_type` -- is the classification matching reality?
2. If the Cloud Coverage sensor shows `cloud_source: met.no_total` instead of `open-meteo_layers`, Open-Meteo may be down and the fallback is less accurate
3. Check `effective_cloud_pct` vs actual conditions -- high cirrus should show low effective cloud (10-15%), not 100%

**Note**: If you have Solcast installed, cloud layer tuning is less critical for solar forecasting because Solcast already accounts for clouds, shading, and panel orientation in its satellite-based model. The cloud coverage sensor and Open-Meteo data still update for dashboard display and day-type classification.

### "Solar forecast too pessimistic"

1. Check if Solcast is available -- if `solar_forecast_source` is `solcast_ha`, the forecast should be well-calibrated. If Solcast is installed but not being used, check the Troubleshooting guide for Solcast entity issues
2. If using VRM sources, check if the mid-day adjustment is working -- the `solar_forecast_source` should change from e.g., `clearsky_p40` to `clearsky_p70` if actual production exceeds the forecast
3. This typically self-corrects after 10am when the mid-day adjustment fires

### "Grid charging at too-high prices"

1. **Increase Grid Import Penalty** -- this is not exposed as a number entity but can be adjusted in the Options flow or by modifying the integration's config. The default $0.02 nudges toward self-sufficiency
2. Verify the hold reward scaling -- is the optimizer charging because it thinks overnight will be expensive?

### "AC load catching system off guard"

The integration detects AC demand via indoor temperature sensors and climate entities. If you are not using the AC detection feature (no indoor temp sensors configured), the optimizer relies purely on the outdoor temperature correction.

Consider adding indoor temperature entities to your setup to enable real-time AC demand detection.

---

## Monitoring Tuning Impact

After making a change, monitor the following sensors over the next 24 hours:

| Sensor | What to Watch |
|--------|--------------|
| Battery Plan | Does the `mode` change appropriately for different price periods? |
| Decision | Is `target_soc_pct` reasonable? Check `override_applied` to see if overrides are masking the LP decision |
| 24h Projected Cost | Is the projected cost decreasing over time? |
| Solar Forecast Today | Does `solar_day_type` match actual weather? |

The Solver Time sensor should stay under 100ms. If it increases significantly, the optimization problem may be poorly conditioned.
