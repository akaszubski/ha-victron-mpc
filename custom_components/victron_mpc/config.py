"""Configuration for MPC Battery Optimizer.

Pure dataclasses defining system specs, optimization tunables, and
entity mappings. In the HACS integration, values come from the config
entry (setup wizard + options flow) instead of environment variables.

Ported from scripts/mpc/config.py with connection classes removed
(those are now config entry data).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class VictronSystem:
    """Physical system parameters for Victron ESS."""

    battery_capacity_kwh: float = 14.2  # 296Ah @ 48V
    max_charge_kw: float = 3.5  # Max grid → battery charge rate (Quattro limit)
    max_discharge_kw: float = 4.5  # Max battery → load discharge rate
    max_solar_kw: float = 7.0  # PV array peak
    max_grid_import_kw: float = 10.0  # Main breaker limit
    max_grid_export_kw: float = 5.0  # Feed-in limit
    inverter_max_kw: float = 5.0  # Quattro continuous rating
    soc_min_pct: float = 10.0  # Hard floor (hardware limit)
    soc_max_pct: float = 100.0
    charge_efficiency: float = 0.95  # Round-trip ~90%
    discharge_efficiency: float = 0.95

    # Genset backup — Commodore CD6500 air-cooled diesel, ~5.7kW rated / 8kVA
    # Auto-start via Victron Cerbo GX (2-wire). Genset has its own low-SoC trigger.
    # Fuel: ~1.3-2.0 L/hr diesel depending on load. ~20L tank.
    # Cost = (diesel_price × consumption_lph) / output_kw + maintenance
    # At typical 50-75% load: (~$2.20 × 1.5) / 4.0 + $0.05 ≈ $0.88/kWh
    genset_diesel_price_per_litre: float = 2.20  # AUD — Melbourne retail
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
    battery_wear_cost: float = 0.05

    # Small penalty for grid import — nudges toward self-sufficiency
    grid_import_penalty: float = 0.02

    # Reward for having full battery at sunset (before evening peak)
    sunset_reward: float = 0.04

    # Reward for maintaining SoC at end of horizon (prevents drain-to-zero)
    terminal_reward: float = 0.03

    # Overnight battery preservation
    overnight_hold_reward: float = 0.10

    # Hours that define "overnight" for preservation
    overnight_start_hour: int = 22
    overnight_end_hour: int = 6

    # Cell balancing — periodic full charge for battery health
    full_charge_interval_days: int = 14

    # Configurable SoC floor (above hardware min, for user comfort)
    soc_floor_pct: float = 20.0

    # Overnight minimum SoC — hard constraint during overnight hours
    overnight_min_soc_pct: float = 30.0

    # Safety & override thresholds
    spike_threshold: float = 1.00  # $/kWh — force discharge above this
    defensive_price: float = 2.00  # $/kWh — assumed price when Amber down
    fallback_price: float = 0.30  # $/kWh — used when no price data at all
    amber_blip_minutes: float = 5.0  # Minutes before defensive mode
    feedin_export_threshold: float = 0.10  # $/kWh — min FIT for spike export
    feedin_soc_threshold: float = 30.0  # % — min SoC to allow spike export
    overnight_price_low: float = 0.15  # $/kWh — full hold reward below
    overnight_price_high: float = 0.25  # $/kWh — zero hold reward above

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
    "soc_floor_pct": (15.0, 30.0),
    "overnight_min_soc_pct": (20.0, 45.0),
    "load_inflation_pct": (5.0, 25.0),
    "solar_cloud_impact": (0.50, 0.90),
    "solar_derating_min": (0.30, 0.70),
}
