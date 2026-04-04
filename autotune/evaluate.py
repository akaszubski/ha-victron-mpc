"""Core evaluation engine for autotune parameter optimization.

Runs the MPC optimizer offline against historical day data to produce
a composite cost metric. Zero homeassistant.* imports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from custom_components.victron_mpc.optimizer import OptInput, optimize

from .metric import score_period
from .types import DayData, DayResult, EvalResult

# ──────────────────────────────────────────────────────────────────
# VictronSystem defaults (replicated to avoid homeassistant imports)
# ──────────────────────────────────────────────────────────────────

BATTERY_CAPACITY_KWH = 14.2
MAX_CHARGE_KW = 7.1
MAX_DISCHARGE_KW = 7.1
MAX_GRID_IMPORT_KW = 10.0
MAX_GRID_EXPORT_KW = 5.0
CHARGE_EFFICIENCY = 0.95
DISCHARGE_EFFICIENCY = 0.95
SOC_MIN_PCT = 10.0
SOFT_FLOOR_PCT = 30.0
DT_MINUTES = 5
DT_HOURS = DT_MINUTES / 60.0
HORIZON_STEPS = 288  # 24h at 5-min

# Fixed wear cost for anti-gaming in metric calculation
FIXED_WEAR_COST_PER_KWH = 0.02

CONFIG_DIR = Path(__file__).parent


def load_config(config_path: Path | None = None) -> dict[str, float]:
    """Load train_config.json and return dict of param_name -> clamped value.

    Args:
        config_path: Path to train_config.json. Defaults to autotune/train_config.json.

    Returns:
        Dict mapping parameter names to their current values, clamped to [min, max].
    """
    if config_path is None:
        config_path = CONFIG_DIR / "train_config.json"

    with open(config_path) as f:
        raw = json.load(f)

    params: dict[str, float] = {}
    for name, spec in raw["parameters"].items():
        value = spec["value"]
        lo = spec["min"]
        hi = spec["max"]
        params[name] = max(lo, min(hi, value))

    return params


def build_soc_target_reward(
    start_hour: float,
    horizon_steps: int,
    dt_hours: float,
    tunables: dict[str, float],
    buy_prices: list[float],
    price_bands: list[str] | None = None,
    solar_forecast_kw: list[float] | None = None,
) -> list[float]:
    """Build time-varying SoC target reward array.

    Standalone extraction of coordinator._build_soc_target_reward(), using
    a plain dict for tunables and start_hour (float 0.0-23.99) instead of
    a datetime object.

    Args:
        start_hour: Hour of day as float (e.g. 14.5 = 2:30pm).
        horizon_steps: Number of timesteps in the horizon.
        dt_hours: Duration of each timestep in hours.
        tunables: Dict of tunable parameter values.
        buy_prices: Buy price array ($/kWh) for each step.
        price_bands: Optional Amber band labels per step.
        solar_forecast_kw: Optional solar forecast per step (kW).

    Returns:
        List of reward values ($/kWh) per step.
    """
    soc_profile_peak = tunables.get("soc_profile_peak", 0.15)
    soc_profile_pre_peak = tunables.get("soc_profile_pre_peak", 0.20)
    soc_profile_morning = tunables.get("soc_profile_morning", 0.10)
    soc_profile_overnight = tunables.get("soc_profile_overnight", 0.03)
    soc_profile_default = tunables.get("soc_profile_default", 0.05)
    grid_charge_boost = tunables.get("grid_charge_boost", 0.15)

    rewards: list[float] = []
    for i in range(horizon_steps):
        hour_offset = i * dt_hours
        hour_of_day = (start_hour + hour_offset) % 24

        if 17 <= hour_of_day < 21:
            base = soc_profile_peak
        elif 11 <= hour_of_day < 17:
            base = soc_profile_pre_peak
        elif 6 <= hour_of_day < 9:
            base = soc_profile_morning
        elif 22 <= hour_of_day or hour_of_day < 6:
            base = soc_profile_overnight
        else:
            base = soc_profile_default

        # Solar insurance: low solar -> value stored battery at 60% of grid price
        if solar_forecast_kw is not None and i < len(solar_forecast_kw):
            solar_w = solar_forecast_kw[i] * 1000
            if solar_w < 300:
                buy_at_step = buy_prices[i] if i < len(buy_prices) else 0.15
                solar_insurance = buy_at_step * 0.6
                base = max(base, solar_insurance)

        # Price bonus from Amber bands
        band = price_bands[i] if price_bands and i < len(price_bands) else "low"
        if band in ("extremely_low", "very_low"):
            base += grid_charge_boost
        elif band == "low":
            base += grid_charge_boost * 0.5

        rewards.append(round(base, 4))

    return rewards


def evaluate_day(
    day: DayData,
    tunables: dict[str, float],
    *,
    start_soc_kwh: float | None = None,
) -> DayResult:
    """Evaluate one day of data with the given tunable parameters.

    Builds an OptInput from DayData + tunables, runs the LP optimizer,
    and extracts cost metrics. Uses FIXED wear cost in the metric to
    prevent gaming via the wear cost tunable.

    Args:
        day: Historical day data (288 timesteps).
        tunables: Dict of tunable parameter values.
        start_soc_kwh: Override starting SoC. Defaults to day.start_soc_kwh.

    Returns:
        DayResult with cost breakdown and SoC trajectory info.
    """
    n = len(day.solar_kw_5min)
    soc_kwh = start_soc_kwh if start_soc_kwh is not None else day.start_soc_kwh

    battery_wear_cost = tunables.get("battery_wear_cost", 0.02)
    grid_import_penalty = tunables.get("grid_import_penalty", 0.02)
    sunset_reward = tunables.get("sunset_reward", 0.04)
    terminal_reward = tunables.get("terminal_reward", 0.03)
    overnight_hold_reward = tunables.get("overnight_hold_reward", 0.05)
    soft_floor_penalty = tunables.get("soft_floor_penalty", 0.10)
    soc_floor_pct = tunables.get("soc_floor_pct", 10.0)
    overnight_min_soc_pct = tunables.get("overnight_min_soc_pct", 31.0)

    soc_min_kwh = soc_floor_pct / 100.0 * BATTERY_CAPACITY_KWH
    soft_floor_kwh = SOFT_FLOOR_PCT / 100.0 * BATTERY_CAPACITY_KWH

    # Build overnight min schedule
    soc_min_schedule_kwh: list[float] | None = None
    if day.overnight_steps:
        overnight_min_kwh = overnight_min_soc_pct / 100.0 * BATTERY_CAPACITY_KWH
        schedule = [soc_min_kwh] * n
        for step in day.overnight_steps:
            if 0 <= step < n:
                schedule[step] = max(soc_min_kwh, overnight_min_kwh)
        soc_min_schedule_kwh = schedule

    # Build SoC target reward from the start of the day (hour 0)
    soc_target_reward = build_soc_target_reward(
        start_hour=0.0,
        horizon_steps=n,
        dt_hours=DT_HOURS,
        tunables=tunables,
        buy_prices=day.buy_price_5min,
        price_bands=day.price_bands,
        solar_forecast_kw=day.solar_kw_5min,
    )

    opt_input = OptInput(
        horizon_steps=n,
        dt_hours=DT_HOURS,
        battery_soc_kwh=soc_kwh,
        battery_capacity_kwh=BATTERY_CAPACITY_KWH,
        soc_min_kwh=soc_min_kwh,
        soc_max_kwh=BATTERY_CAPACITY_KWH,
        max_charge_kw=MAX_CHARGE_KW,
        max_discharge_kw=MAX_DISCHARGE_KW,
        charge_efficiency=CHARGE_EFFICIENCY,
        discharge_efficiency=DISCHARGE_EFFICIENCY,
        max_grid_import_kw=MAX_GRID_IMPORT_KW,
        max_grid_export_kw=MAX_GRID_EXPORT_KW,
        solar_forecast_kw=day.solar_kw_5min,
        load_forecast_kw=day.load_kw_5min,
        buy_price=day.buy_price_5min,
        sell_price=day.sell_price_5min,
        battery_wear_cost=battery_wear_cost,
        grid_import_penalty=grid_import_penalty,
        sunset_step=day.sunset_step,
        sunset_reward=sunset_reward,
        terminal_reward=terminal_reward,
        overnight_hold_reward=overnight_hold_reward,
        overnight_steps=day.overnight_steps if day.overnight_steps else None,
        soc_soft_floor_kwh=soft_floor_kwh,
        soft_floor_penalty=soft_floor_penalty,
        sunset_soc_target_pct=95.0,
        soc_target_reward=soc_target_reward,
        soc_min_schedule_kwh=soc_min_schedule_kwh,
    )

    result = optimize(opt_input)

    # Extract costs
    grid_cost = result.cost_breakdown.get("grid_cost", 0.0)
    export_revenue = result.cost_breakdown.get("export_revenue", 0.0)

    # FIXED wear cost -- use $0.02/kWh regardless of tunable to prevent gaming
    total_discharge_kwh = sum(result.discharge_schedule_kw) * DT_HOURS
    wear_cost_fixed = total_discharge_kwh * FIXED_WEAR_COST_PER_KWH

    # SoC trajectory analysis
    soc_pct = result.soc_trajectory_pct
    min_soc_pct = min(soc_pct) if soc_pct else 0.0
    sunset_soc_pct = soc_pct[day.sunset_step] if day.sunset_step < len(soc_pct) else soc_pct[-1]
    end_soc_kwh = soc_pct[-1] / 100.0 * BATTERY_CAPACITY_KWH

    # Count soft floor violations
    floor_violations = sum(1 for s in soc_pct if s < SOFT_FLOOR_PCT)

    return DayResult(
        date=day.date,
        grid_cost=round(grid_cost, 4),
        export_revenue=round(export_revenue, 4),
        wear_cost_fixed=round(wear_cost_fixed, 4),
        floor_violations=floor_violations,
        min_soc_pct=round(min_soc_pct, 1),
        sunset_soc_pct=round(sunset_soc_pct, 1),
        end_soc_kwh=round(end_soc_kwh, 4),
        total_discharge_kwh=round(total_discharge_kwh, 4),
        solver_status=result.solver_status,
    )


def evaluate_multi_day(
    days: list[DayData],
    tunables: dict[str, float],
) -> EvalResult:
    """Evaluate multiple days with SoC continuity between days.

    Day N+1 starts with the ending SoC from day N.

    Args:
        days: List of DayData objects, in chronological order.
        tunables: Dict of tunable parameter values.

    Returns:
        EvalResult with composite metric and per-day breakdown.
    """
    results: list[DayResult] = []
    current_soc_kwh: float | None = None

    for day in days:
        day_result = evaluate_day(
            day,
            tunables,
            start_soc_kwh=current_soc_kwh,
        )
        results.append(day_result)
        current_soc_kwh = day_result.end_soc_kwh

    # Use anti-gaming metric for composite scoring
    start_soc = days[0].start_soc_kwh
    end_soc = results[-1].end_soc_kwh
    return score_period(results, start_soc, end_soc, len(days))


def load_days(data_dir: Path) -> list[DayData]:
    """Load day data from JSON files in a directory.

    Each JSON file should contain fields matching DayData.

    Args:
        data_dir: Directory containing .json day data files.

    Returns:
        List of DayData objects sorted by date.
    """
    days: list[DayData] = []
    for path in sorted(data_dir.glob("*.json")):
        with open(path) as f:
            raw = json.load(f)

        days.append(DayData(
            date=raw["date"],
            solar_kw_5min=raw["solar_kw_5min"],
            load_kw_5min=raw["load_kw_5min"],
            buy_price_5min=raw["buy_price_5min"],
            sell_price_5min=raw["sell_price_5min"],
            sunset_step=raw["sunset_step"],
            overnight_steps=raw.get("overnight_steps", []),
            start_soc_kwh=raw.get("start_soc_kwh", 7.1),
            price_bands=raw.get("price_bands"),
        ))

    return days


def main() -> None:
    """CLI entry point for evaluate.py.

    Usage:
        python -m autotune.evaluate --data-dir autotune/data/ [--config autotune/train_config.json]
    """
    parser = argparse.ArgumentParser(description="Evaluate MPC tunables against historical data")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=CONFIG_DIR / "data",
        help="Directory containing day data JSON files",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to train_config.json (default: autotune/train_config.json)",
    )
    args = parser.parse_args()

    tunables = load_config(args.config)
    days = load_days(args.data_dir)

    if not days:
        print(json.dumps({"error": "no day data found", "data_dir": str(args.data_dir)}))
        sys.exit(1)

    result = evaluate_multi_day(days, tunables)

    output = {
        "composite_metric": result.composite_metric,
        "breakdown": result.breakdown,
        "days_evaluated": len(result.per_day),
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
