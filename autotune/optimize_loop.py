"""Pattern-based optimization loop for autotune parameter search.

Analyzes cost drivers from evaluation results, proposes parameter
adjustments, and iterates until convergence. No LLM calls -- pure
heuristic analysis.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evaluate import evaluate_multi_day, load_config, load_days
from .metric import BATTERY_CAPACITY_KWH, score_period
from .report import generate_report
from .types import DayResult, EvalResult

CONFIG_DIR = Path(__file__).parent


def analyze_cost_drivers(result: EvalResult) -> list[dict]:
    """Examine per-day results for cost driver patterns.

    Args:
        result: EvalResult from evaluate_multi_day.

    Returns:
        List of driver dicts with keys: driver, param, direction, magnitude.
    """
    drivers: list[dict] = []
    days = result.per_day
    if not days:
        return drivers

    total_floor = sum(r.floor_violations for r in days)
    if total_floor > 0:
        drivers.append({
            "driver": "floor_violations",
            "param": "overnight_hold_reward",
            "direction": "+",
            "magnitude": min(0.02, total_floor * 0.001),
        })
        drivers.append({
            "driver": "floor_violations",
            "param": "overnight_min_soc_pct",
            "direction": "+",
            "magnitude": min(5.0, total_floor * 0.5),
        })

    low_sunset = [r for r in days if r.sunset_soc_pct < 80.0]
    if low_sunset:
        avg_deficit = sum(80.0 - r.sunset_soc_pct for r in low_sunset) / len(low_sunset)
        drivers.append({
            "driver": "low_sunset_soc",
            "param": "sunset_reward",
            "direction": "+",
            "magnitude": min(0.02, avg_deficit * 0.001),
        })

    total_discharge = sum(r.total_discharge_kwh for r in days)
    avg_daily = total_discharge / len(days) if days else 0
    if avg_daily > BATTERY_CAPACITY_KWH:
        drivers.append({
            "driver": "excessive_cycling",
            "param": "battery_wear_cost",
            "direction": "+",
            "magnitude": min(0.01, (avg_daily - BATTERY_CAPACITY_KWH) * 0.001),
        })

    total_grid = sum(r.grid_cost for r in days)
    composite = result.composite_metric
    if composite > 0 and total_grid / composite > 0.70:
        drivers.append({
            "driver": "high_grid_cost",
            "param": "soc_profile_pre_peak",
            "direction": "+",
            "magnitude": 0.01,
        })

    return drivers


def propose_change(
    driver: dict,
    current: dict[str, float],
    config: dict,
) -> dict[str, float]:
    """Apply a suggested parameter change, clamping to bounds.

    Args:
        driver: Cost driver dict from analyze_cost_drivers.
        current: Current parameter values.
        config: Raw train_config.json dict with bounds.

    Returns:
        New tunables dict with the proposed change applied.
    """
    new_tunables = dict(current)
    param = driver["param"]

    if param not in config.get("parameters", {}):
        return new_tunables

    spec = config["parameters"][param]
    lo = spec["min"]
    hi = spec["max"]
    step = spec.get("step", driver["magnitude"])

    current_val = current.get(param, spec["value"])
    delta = driver["magnitude"] if driver["direction"] == "+" else -driver["magnitude"]

    # Round to step size
    new_val = current_val + delta
    new_val = max(lo, min(hi, new_val))
    new_tunables[param] = round(new_val, 6)

    return new_tunables


def run_optimization_loop(
    iterations: int,
    data_dir: Path,
    config_path: Path,
    *,
    dry_run: bool = False,
) -> str:
    """Main optimization loop entry point.

    Loads data, evaluates baseline, iteratively proposes and tests changes.

    Args:
        iterations: Maximum number of optimization iterations.
        data_dir: Directory containing day data JSON files.
        config_path: Path to train_config.json.
        dry_run: If True, do not update config file.

    Returns:
        Formatted report string.
    """
    days = load_days(data_dir)
    if not days:
        return "ERROR: No day data found in " + str(data_dir)

    with open(config_path) as f:
        raw_config = json.load(f)

    current_tunables = load_config(config_path)
    baseline_tunables = dict(current_tunables)

    # Baseline evaluation
    baseline_result = evaluate_multi_day(days, current_tunables)
    best_result = baseline_result
    best_tunables = dict(current_tunables)

    for i in range(iterations):
        drivers = analyze_cost_drivers(best_result)
        if not drivers:
            break

        improved = False
        for driver in drivers:
            candidate = propose_change(driver, best_tunables, raw_config)

            if candidate == best_tunables:
                continue

            candidate_result = evaluate_multi_day(days, candidate)

            if candidate_result.composite_metric < best_result.composite_metric:
                best_result = candidate_result
                best_tunables = candidate
                improved = True
                break  # Restart analysis with new best

        if not improved:
            break

    # Update config if improved and not dry_run
    if not dry_run and best_tunables != baseline_tunables:
        for name, value in best_tunables.items():
            if name in raw_config["parameters"]:
                raw_config["parameters"][name]["value"] = value
        with open(config_path, "w") as f:
            json.dump(raw_config, f, indent=2)
            f.write("\n")

    report = generate_report(
        baseline=baseline_result,
        optimized=best_result,
        before_params=baseline_tunables,
        after_params=best_tunables,
    )

    return report


def main() -> None:
    """CLI entry point for optimize_loop.

    Usage:
        python -m autotune.optimize_loop --iterations 10 --data-dir autotune/data/ [--dry-run]
    """
    parser = argparse.ArgumentParser(
        description="Run autotune optimization loop"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Maximum optimization iterations (default: 10)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=CONFIG_DIR / "data",
        help="Directory containing day data JSON files",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_DIR / "train_config.json",
        help="Path to train_config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not update config file",
    )
    args = parser.parse_args()

    output = run_optimization_loop(
        iterations=args.iterations,
        data_dir=args.data_dir,
        config_path=args.config,
        dry_run=args.dry_run,
    )
    print(output)


if __name__ == "__main__":
    main()
