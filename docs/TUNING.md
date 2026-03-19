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

## Safety & Override Thresholds

These control when the integration bypasses the LP optimizer and applies hard safety overrides. All are adjustable from the HA UI as number entities.

### Spike Threshold -- Default: $1.00

**Entity**: `number.victron_mpc_battery_optimizer_spike_threshold`

Price above which the override logic forces immediate discharge (R2901=100). This bypasses the LP entirely.

| Value | Effect |
|-------|--------|
| Lower ($0.50) | More aggressive -- treats anything above $0.50 as a spike |
| Higher ($2.00) | Only discharges during extreme spikes |

**Range**: $0.50 - $5.00

### Defensive Price -- Default: $2.00

**Entity**: `number.victron_mpc_battery_optimizer_defensive_price`

The assumed buy price during evening peak hours (17:00-21:00) when the Amber API is unavailable. The optimizer uses this to decide whether to discharge defensively during an Amber outage.

| Value | Effect |
|-------|--------|
| Lower ($1.00) | Less aggressive defensive discharge |
| Higher ($3.00) | Stronger defensive discharge -- assumes worse spike risk |

**Range**: $0.50 - $5.00

### Amber Blip Minutes -- Default: 5

**Entity**: `number.victron_mpc_battery_optimizer_amber_blip_minutes`

How many minutes of continuous Amber unavailability before defensive mode activates. Brief Amber glitches shorter than this threshold use the last known price.

| Value | Effect |
|-------|--------|
| Lower (2 min) | Faster defensive trigger -- reacts to brief outages |
| Higher (10 min) | More tolerant -- ignores transient Amber blips |

**Range**: 1 - 15 minutes

### Feed-in Export Threshold -- Default: $0.10

**Entity**: `number.victron_mpc_battery_optimizer_feedin_export_threshold`

Minimum feed-in tariff (FIT) required to allow grid export during a spike. Prevents exporting battery at negligible FIT rates where the revenue does not justify the wear.

| Value | Effect |
|-------|--------|
| Lower ($0.05) | Exports during spikes even at low FIT |
| Higher ($0.20) | Only exports when FIT is substantial |

**Range**: $0.01 - $0.50

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
| Spike Threshold | $1.00 | Same as the number entity |
| Defensive Price | $2.00 | Same as the number entity |
| Amber Blip Minutes | 5 | Same as the number entity |
| Feed-in Export Threshold | $0.10 | Same as the number entity |
| Overnight Price Low | $0.15 | Full hold reward below this overnight price |
| Overnight Price High | $0.25 | Zero hold reward above this overnight price |
| Feed-in SoC Threshold | 30% | Min SoC to allow spike export |
| Fallback Price | $0.30 | Assumed price when no data at all |
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
| Spike Threshold | 0.50 | 5.00 | 0.10 | $/kWh |
| Defensive Price | 0.50 | 5.00 | 0.10 | $/kWh |
| Amber Blip Minutes | 1 | 15 | 1 | min |
| Feed-in Export Threshold | 0.01 | 0.50 | 0.01 | $/kWh |

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

### "Want to be more aggressive on spike discharge"

1. **Lower Spike Threshold** -- reduce from $1.00 to $0.50 to trigger spike discharge at lower prices
2. **Lower SoC Floor** -- allow deeper discharge during spikes (e.g., 20% -> 15%)
3. Check the Decision sensor's `spike` attribute to see if spikes are being detected

### "Amber goes down too often, false alarms"

1. **Increase Amber Blip Minutes** -- raise from 5 to 10 or 15 minutes to tolerate longer Amber glitches before defensive mode activates
2. Check persistent notifications for "Amber Pricing Unavailable" -- if these fire frequently for brief outages, a longer blip tolerance will reduce false triggers
3. The defensive discharge is intentionally aggressive during evening peak (17:00-21:00) -- increasing the blip minutes gives Amber more time to recover before the integration assumes the worst

### "Want higher defensive price assumption"

1. **Increase Defensive Price** -- raise from $2.00 to $3.00 or higher to make defensive discharge more aggressive during Amber outages
2. This only affects the evening peak window (17:00-21:00) when Amber is unavailable
3. A higher value means the optimizer will discharge more battery during an Amber outage, protecting against the risk of a large undetected spike
4. A lower value (e.g., $1.00) is appropriate if spikes in your area rarely exceed $1/kWh

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
