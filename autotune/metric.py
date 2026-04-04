"""Anti-gaming composite metric for autotune evaluation.

All scoring uses FIXED constants that are independent of the tunables
being optimised. The tunables affect LP decisions; this module scores
the outcomes with immutable real-world costs.
"""

from __future__ import annotations

from .types import DayResult, EvalResult

# Battery capacity for conversions
BATTERY_CAPACITY_KWH = 14.2

# FIXED evaluation costs -- NOT from tunables, cannot be gamed
EVAL_WEAR_COST = 0.02  # $/kWh -- actual Pylontech LFP wear
EVAL_FLOOR_VIOLATION_PENALTY = 0.50  # $ per 5-min step below 30% SoC
EVAL_SUNSET_PENALTY = 0.10  # $ per % below 80% at sunset
EVAL_SOC_CONTINUITY_PENALTY = 0.05  # $ per % deviation start->end over period
EVAL_EXCESSIVE_CYCLING_PENALTY = 0.01  # $ per kWh above 1 full cycle/day


def score_day(day_result: DayResult) -> float:
    """Score a single day using fixed evaluation constants.

    Args:
        day_result: Result from evaluate_day().

    Returns:
        Total cost for the day ($ AUD, lower is better).
    """
    # Grid cost and export revenue (from LP at actual prices)
    cost = day_result.grid_cost - day_result.export_revenue

    # Fixed wear cost (NOT the tunable value)
    cost += day_result.total_discharge_kwh * EVAL_WEAR_COST

    # Floor violation penalty
    cost += day_result.floor_violations * EVAL_FLOOR_VIOLATION_PENALTY

    # Sunset penalty: penalize each % below 80% at sunset
    if day_result.sunset_soc_pct < 80.0:
        cost += (80.0 - day_result.sunset_soc_pct) * EVAL_SUNSET_PENALTY

    return round(cost, 4)


def score_period(
    day_results: list[DayResult],
    start_soc_kwh: float,
    end_soc_kwh: float,
    days_count: int,
) -> EvalResult:
    """Score a multi-day evaluation period.

    Args:
        day_results: Results from each day.
        start_soc_kwh: SoC at start of first day.
        end_soc_kwh: SoC at end of last day.
        days_count: Number of days evaluated.

    Returns:
        EvalResult with composite metric and detailed breakdown.
    """
    # Per-day scores
    day_scores = [score_day(r) for r in day_results]
    total_day_cost = sum(day_scores)

    # Period-level penalties
    start_pct = start_soc_kwh / BATTERY_CAPACITY_KWH * 100
    end_pct = end_soc_kwh / BATTERY_CAPACITY_KWH * 100
    continuity_penalty = abs(end_pct - start_pct) * EVAL_SOC_CONTINUITY_PENALTY

    total_discharge = sum(r.total_discharge_kwh for r in day_results)
    max_daily_cycles = days_count * BATTERY_CAPACITY_KWH
    cycling_excess = max(0.0, total_discharge - max_daily_cycles)
    cycling_penalty = cycling_excess * EVAL_EXCESSIVE_CYCLING_PENALTY

    composite = total_day_cost + continuity_penalty + cycling_penalty

    # Detailed breakdown
    total_grid = sum(r.grid_cost for r in day_results)
    total_export = sum(r.export_revenue for r in day_results)
    total_wear = sum(r.total_discharge_kwh for r in day_results) * EVAL_WEAR_COST
    total_floor = sum(r.floor_violations for r in day_results)
    floor_penalty = total_floor * EVAL_FLOOR_VIOLATION_PENALTY

    sunset_penalty = 0.0
    for r in day_results:
        if r.sunset_soc_pct < 80.0:
            sunset_penalty += (80.0 - r.sunset_soc_pct) * EVAL_SUNSET_PENALTY

    breakdown = {
        "grid_cost": round(total_grid, 4),
        "export_revenue": round(total_export, 4),
        "wear_cost_fixed": round(total_wear, 4),
        "floor_violations": total_floor,
        "floor_penalty": round(floor_penalty, 4),
        "sunset_penalty": round(sunset_penalty, 4),
        "continuity_penalty": round(continuity_penalty, 4),
        "cycling_penalty": round(cycling_penalty, 4),
        "composite": round(composite, 4),
    }

    return EvalResult(
        composite_metric=round(composite, 4),
        breakdown=breakdown,
        per_day=day_results,
    )
