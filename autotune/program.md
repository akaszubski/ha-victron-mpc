# AutoTune Program — Constraints & Safety Rules

**Purpose**: Define the fixed rules and bounds that the autotune loop must respect.
This file is human-edited only. The runner never modifies it.

## Battery Specifications

- Capacity: 14.2 kWh (296Ah @ 48V LFP)
- Max charge: 7.1 kW (4× Pylontech Force-L2 @ 37A)
- Max discharge: 7.1 kW
- Charge efficiency: 95%
- Discharge efficiency: 95%
- Hardware SoC floor: 10%

## Parameter Bounds

These bounds are enforced by clamping — out-of-range values are silently clamped,
never rejected.

| Parameter | Min | Max | Unit |
|-----------|-----|-----|------|
| battery_wear_cost | 0.01 | 0.10 | $/kWh |
| grid_import_penalty | 0.00 | 0.05 | $/kWh |
| sunset_reward | 0.01 | 0.10 | $/kWh |
| terminal_reward | 0.01 | 0.10 | $/kWh |
| overnight_hold_reward | 0.02 | 0.20 | $/kWh |
| soc_floor_pct | 10.0 | 35.0 | % |
| overnight_min_soc_pct | 20.0 | 50.0 | % |
| load_inflation_pct | 5.0 | 25.0 | % |
| solar_cloud_impact | 0.50 | 0.90 | ratio |
| solar_derating_min | 0.30 | 0.70 | ratio |
| soc_profile_peak | 0.05 | 0.30 | $/kWh |
| soc_profile_pre_peak | 0.10 | 0.40 | $/kWh |
| soc_profile_morning | 0.03 | 0.20 | $/kWh |
| soc_profile_overnight | 0.01 | 0.10 | $/kWh |
| soc_profile_default | 0.02 | 0.15 | $/kWh |
| grid_charge_boost | 0.05 | 0.30 | $/kWh |
| soft_floor_penalty | 0.05 | 0.30 | $/kWh/h |

## Safety Rules

1. `soc_floor_pct` must be >= 10.0 (hardware minimum)
2. `overnight_min_soc_pct` must be >= 20.0 (emergency reserve)
3. `battery_wear_cost` must be >= 0.01 (prevent unlimited cycling)
4. `soc_profile_pre_peak` should exceed typical grid cost (~$0.17) for LP to grid-charge

## Evaluation Rules

1. **Fixed evaluation wear cost**: Wear cost in the metric is computed at a fixed
   $0.02/kWh, NOT the tunable value. This prevents gaming (lowering wear cost
   to reduce metric without improving actual cost).
2. **Multi-day window**: Evaluate over 14-30 consecutive days minimum.
3. **SoC continuity**: End SoC of day N becomes start SoC of day N+1.
4. **No HA dependency**: evaluate.py must work offline with cached data only.
