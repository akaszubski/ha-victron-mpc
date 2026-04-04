"""Shared dataclasses for autotune evaluation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DayData:
    """One day of historical data for offline evaluation.

    All arrays are 288 entries (5-min timesteps over 24h).
    """

    date: str  # ISO date e.g. "2026-03-15"
    solar_kw_5min: list[float]  # 288 entries
    load_kw_5min: list[float]  # 288 entries
    buy_price_5min: list[float]  # 288 entries -- $/kWh
    sell_price_5min: list[float]  # 288 entries -- $/kWh
    sunset_step: int  # Step index of sunset (0-287)
    overnight_steps: list[int]  # Step indices for 22:00-06:00
    start_soc_kwh: float = 7.1  # Default 50% of 14.2kWh
    price_bands: list[str] | None = None  # Optional Amber band labels


@dataclass
class DayResult:
    """Evaluation result for a single day."""

    date: str
    grid_cost: float  # $ spent on grid import
    export_revenue: float  # $ earned from export
    wear_cost_fixed: float  # Wear cost at FIXED rate (anti-gaming)
    floor_violations: int  # Steps where SoC < soft floor
    min_soc_pct: float  # Lowest SoC reached (%)
    sunset_soc_pct: float  # SoC at sunset step (%)
    end_soc_kwh: float  # SoC at end of day (kWh) -- chains to next day
    total_discharge_kwh: float = 0.0  # Total discharge over the day (kWh)
    solver_status: str = "optimal"


@dataclass
class EvalResult:
    """Aggregate evaluation across all days."""

    composite_metric: float  # Single scalar -- lower is better
    breakdown: dict[str, float] = field(default_factory=dict)
    per_day: list[DayResult] = field(default_factory=list)
