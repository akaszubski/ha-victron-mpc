"""Configuration for MPC Battery Optimizer.

Pure dataclasses defining system specs, optimization tunables, intents,
and entity mappings. In the HACS integration, values come from the config
entry (setup wizard + options flow) instead of environment variables.

Ported from scripts/mpc/config.py with connection classes removed
(those are now config entry data).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .principles import DEFAULT_PRINCIPLES


# ======================================================================
# System Intents — backward-compatible list derived from the operating
# principles defined in principles.py. These are WHAT the system should
# achieve, not HOW.
# ======================================================================

SYSTEM_INTENTS: list[dict[str, str]] = [
    p.to_intent_dict() for p in DEFAULT_PRINCIPLES
]


@dataclass
class VictronSystem:
    """Physical system parameters for Victron ESS."""

    battery_capacity_kwh: float = 14.2  # 296Ah @ 48V
    max_charge_kw: float = 7.1  # 4× Pylontech Force-L2 @ 37A each = 7.1kW
    max_discharge_kw: float = 7.1  # 4× Pylontech Force-L2 @ 37A each = 7.1kW
    max_solar_kw: float = 7.0  # PV array peak
    max_grid_import_kw: float = 10.0  # Main breaker limit
    max_grid_export_kw: float = 5.0  # Feed-in limit
    inverter_max_kw: float = 8.0  # Quattro 48/8000 continuous rating (8kVA)
    soc_min_pct: float = 10.0  # Hard floor (hardware limit)
    soc_max_pct: float = 100.0
    charge_efficiency: float = 0.95  # Round-trip ~90%
    discharge_efficiency: float = 0.95

    # Genset backup — Commodore CD6500 air-cooled diesel, ~5.7kW rated / 8kVA
    # Auto-start via Victron Cerbo GX (2-wire). Genset has its own low-SoC trigger.
    # Fuel: ~1.3-2.0 L/hr diesel depending on load. ~20L tank.
    # Cost = (diesel_price × consumption_lph) / output_kw + maintenance
    # At typical 50-75% load: (~$2.20 × 1.5) / 4.0 + $0.05 ≈ $0.88/kWh
    genset_diesel_price_per_litre: float = 2.20  # AUD — local retail default
    genset_consumption_lph: float = 1.5  # Litres/hour at typical 50-75% load
    genset_output_kw: float = 4.0  # Effective output at typical load
    genset_maintenance_per_kwh: float = 0.05  # Oil, filters, servicing allowance

    @property
    def genset_cost_per_kwh(self) -> float:
        """Dynamic genset cost based on current diesel price."""
        fuel_cost = (self.genset_diesel_price_per_litre * self.genset_consumption_lph) / self.genset_output_kw
        return fuel_cost + self.genset_maintenance_per_kwh

    @classmethod
    def from_config_entry(cls, data: dict[str, Any]) -> VictronSystem:
        """Create from config entry data."""
        return cls(
            battery_capacity_kwh=data.get("battery_capacity_kwh", 14.2),
            max_charge_kw=data.get("max_charge_kw", 3.5),
            max_discharge_kw=data.get("max_discharge_kw", 4.5),
        )


@dataclass
class MPCTunables:
    """Optimizer tuning parameters.

    These control how aggressively the optimizer uses the battery.
    All costs in $/kWh to be comparable with electricity prices.
    """

    # Battery wear cost — discourages unnecessary cycling.
    # Pylontech Force-L2-48-296. $10K / (6000 cycles × 12.8 kWh) = $0.013 at rated.
    # At actual ~60% DoD: ~$0.02/kWh. Stable temp, LFP chemistry.
    battery_wear_cost: float = 0.02

    # Small penalty for grid import — nudges toward self-sufficiency
    grid_import_penalty: float = 0.02

    # Reward for having full battery at sunset (before evening peak)
    sunset_reward: float = 0.04

    # Reward for maintaining SoC at end of horizon (prevents drain-to-zero)
    terminal_reward: float = 0.03

    # SoC floor band — soft floor with penalty, hard floor as absolute minimum.
    soc_soft_floor_pct: float = 30.0  # Soft floor — preferred minimum
    soft_floor_penalty: float = 0.10  # $/kWh/h cost for being below soft floor

    # Overnight battery preservation — reduced from $0.10, was causing
    # hold during $0.28+ evening peaks. $0.05 preserves cheap overnight.
    grid_charge_boost: float = 0.15

    # SoC profile — unified time-varying reward (replaces legacy rewards)
    soc_profile_enabled: bool = True
    soc_profile_default: float = 0.05
    soc_profile_peak: float = 0.15
    soc_profile_pre_peak: float = 0.20  # Must exceed typical grid ($0.15 + $0.02 penalty)
    soc_profile_morning: float = 0.10
    soc_profile_overnight: float = 0.03  # Incentive to charge during cheap grid

    overnight_hold_reward: float = 0.05

    # Hours that define "overnight" for preservation
    overnight_start_hour: int = 22
    overnight_end_hour: int = 6

    # Morning refill window — used by scale_overnight_hold_reward to judge
    # whether discharging overnight makes economic sense vs. refilling in
    # the morning. See GitHub issue #80.
    morning_start_hour: int = 6
    morning_end_hour: int = 9

    # Overnight arbitrage threshold: minimum (overnight_avg - morning_avg)
    # spread in $/kWh before the LP is allowed to discharge freely overnight.
    # Below this spread, the hold reward is preserved to prevent uneconomic
    # wear-cost losses from same-price deferral. Default $0.10 gives ~2.8x
    # safety margin over economic break-even ($0.035/kWh including wear +
    # efficiency losses).
    overnight_arbitrage_threshold: float = 0.10

    # Cell balancing — periodic full charge for battery health
    full_charge_interval_days: int = 14

    # Configurable SoC floor (above hardware min, for user comfort)
    soc_floor_pct: float = 10.0  # Hardware safe minimum

    # Overnight minimum SoC — hard constraint during overnight hours
    overnight_min_soc_pct: float = 31.0

    # Safety & override thresholds
    spike_threshold: float = 1.00  # $/kWh — force discharge above this
    defensive_price: float = 2.00  # $/kWh — assumed price when Amber down
    fallback_price: float = 0.30  # $/kWh — used when no price data at all
    amber_blip_minutes: float = 10.0  # Minutes before defensive mode
    amber_cautious_minutes: float = 15.0  # Tier 2→3 escalation (was 30)
    feedin_export_threshold: float = 0.10  # $/kWh — min FIT for spike export
    feedin_soc_threshold: float = 30.0  # % — min SoC to allow spike export

    # Intraday correction — adjust day type based on actual vs expected yield
    intraday_early_hour: float = 8.0  # Start checking yield from this hour
    intraday_early_threshold: float = 0.30  # Before 10am: downgrade if yield < 30%
    intraday_standard_threshold: float = 0.60  # After 10am: downgrade if yield < 60%
    intraday_upgrade_threshold: float = 1.50  # Upgrade if yield > 150% of expected
    cloud_override_low_pct: float = 80.0  # Force overcast if low cloud > this %

    # Forecast horizon
    forecast_hours: int = 24
    dt_minutes: int = 5  # Optimization timestep

    # Load forecast inflation — safety margin (%)
    load_inflation_pct: float = 10.0

    # How many days of history to use for load/solar profiles
    history_days: int = 7

    # Solar forecast derating
    solar_derating: bool = True
    solar_derating_days: int = 7
    solar_derating_min: float = 0.5
    solar_derating_max: float = 1.0
    solar_cloud_impact: float = 0.75

    # Cloud layer weighting
    solar_cloud_layer_weights: dict[str, float] = field(default_factory=lambda: {
        "high": 0.15,   # Cirrus — thin ice crystals, minimal solar impact
        "mid": 0.5,     # Altostratus/altocumulus — moderate blocking
        "low": 0.9,     # Stratus/cumulus — thick, major solar reduction
    })

    # Weather-classified solar forecast percentiles
    solar_day_type_percentiles: dict[str, float] = field(default_factory=lambda: {
        "clear": 0.90,
        "partly_cloudy": 0.70,
        "overcast": 0.40,
        "rain": 0.15,
    })
    solar_day_type_cloud_clear: float = 30.0
    solar_day_type_cloud_overcast: float = 70.0
    solar_day_type_precip_light: float = 1.0
    solar_day_type_precip_heavy: float = 2.0

    # Weather confidence discount — applied to solar forecast when conditions
    # are poor.  Makes the sunset constraint tighter, forcing earlier grid
    # charging on days when Solcast over-estimates (rain/overcast).
    # 1.0 = no discount, 0.5 = halve the forecast.
    solar_weather_confidence: dict[str, float] = field(default_factory=lambda: {
        "clear": 1.0,
        "partly_cloudy": 0.9,
        "overcast": 0.7,
        "rain": 0.5,
    })

    # Seasonal load adjustment
    seasonal_load_adjustment: bool = True

    # Temperature-based load adjustment
    temp_base_cool: float = 15.0
    temp_base_heat: float = 26.0
    temp_cool_pct_per_degree: float = 1.0
    temp_heat_pct_per_degree: float = 3.3

    # Real-time indoor temperature AC demand correction
    indoor_temp_ac_threshold: float = 24.0
    indoor_ac_kw_per_degree: float = 0.8
    indoor_ac_boost_hours: int = 5
    indoor_ac_running_kw: float = 2.0

    # Scheduled load boost — hot water system
    # Adds expected load during scheduled time window (e.g., morning hot water)
    hot_water_boost_kw: float = 2.5  # kW draw of hot water element
    hot_water_start_hour: float = 6.5  # 06:30 local
    hot_water_duration_minutes: float = 10.0  # Typical cycle duration
    hot_water_enabled: bool = False  # Disabled by default until configured

    # Input validation bounds (Milestone 10 — Resilience)
    # Amber price sanity — reject API errors / data corruption only.
    # NEM prices can legitimately hit $17+/kWh (extreme spikes) and go
    # deeply negative (paid to consume).  These bounds catch data
    # corruption, not real market events.
    price_max_buy: float = 50.0  # $/kWh — NEM cap is ~$17, 50 catches corruption
    price_min_buy: float = -10.0  # $/kWh — deep negatives happen, -10 catches corruption
    # SoC jump detection — physical limit ~4.2%/5min, 15% allows margin
    soc_max_jump_pct: float = 15.0  # Max SoC change per 5-min cycle
    # Solar forecast cap — array is 7kW, 8kW allows margin for overpanel
    solar_forecast_cap_kw: float = 8.0  # Max per-timestep solar kW
    # Load forecast bounds — max reasonable household + AC
    load_forecast_cap_kw: float = 15.0  # Max per-timestep load kW
    load_forecast_min_kw: float = 0.0  # Min per-timestep load kW (no negative)

    @property
    def horizon_steps(self) -> int:
        return self.forecast_hours * 60 // self.dt_minutes

    @property
    def dt_hours(self) -> float:
        return self.dt_minutes / 60.0

    @property
    def steps_per_hour(self) -> int:
        return 60 // self.dt_minutes

    def to_dict(self) -> dict:
        """Serialize tunable floats/ints for storage (skip entities/tuples)."""
        result = {}
        for k, v in asdict(self).items():
            if isinstance(v, (int, float, bool)):
                result[k] = v
        return result

    @classmethod
    def from_dict(cls, d: dict) -> MPCTunables:
        """Create from dict, ignoring unknown keys."""
        defaults = cls()
        valid = {k for k in asdict(defaults)}
        filtered = {k: v for k, v in d.items() if k in valid}
        return cls(**filtered)

    @classmethod
    def from_config_entry(cls, options: dict[str, Any]) -> MPCTunables:
        """Create from config entry options, with defaults for unset values."""
        tunables = cls()
        for key, value in options.items():
            if hasattr(tunables, key) and isinstance(value, (int, float, bool)):
                if key in TUNABLE_BOUNDS:
                    lo, hi = TUNABLE_BOUNDS[key]
                    value = max(lo, min(hi, value))
                setattr(tunables, key, type(getattr(tunables, key))(value))
        return tunables


# Bounds for auto-tuning. Keys must match MPCTunables field names.
TUNABLE_BOUNDS: dict[str, tuple[float, float]] = {
    "battery_wear_cost": (0.01, 0.10),
    "grid_import_penalty": (0.00, 0.05),
    "sunset_reward": (0.01, 0.10),
    "terminal_reward": (0.01, 0.10),
    "overnight_hold_reward": (0.02, 0.20),
    "soc_floor_pct": (10.0, 35.0),
    "overnight_min_soc_pct": (20.0, 50.0),
    "load_inflation_pct": (5.0, 25.0),
    "solar_cloud_impact": (0.50, 0.90),
    "solar_derating_min": (0.30, 0.70),
}
