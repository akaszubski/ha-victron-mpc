"""Deterministic hill-climbing runner for autotune parameter optimization.

Iterates through each tunable parameter, tries +/- step, and commits
improvements to train_config.json with git.

Usage:
    python -m autotune.runner --max-rounds 10 --data-dir autotune/data/ [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .evaluate import evaluate_multi_day, load_config, load_days

CONFIG_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = CONFIG_DIR / "train_config.json"


def _load_raw_config(config_path: Path) -> dict:
    """Load the raw train_config.json with full parameter specs."""
    with open(config_path) as f:
        return json.load(f)


def _save_config(config_path: Path, raw_config: dict) -> None:
    """Save train_config.json with consistent formatting."""
    with open(config_path, "w") as f:
        json.dump(raw_config, f, indent=2)
        f.write("\n")


def _git_commit(config_path: Path, message: str, *, dry_run: bool = False) -> None:
    """Stage and commit train_config.json with the given message.

    Args:
        config_path: Path to the config file to commit.
        message: Commit message.
        dry_run: If True, print message but don't actually commit.
    """
    if dry_run:
        print(f"[dry-run] Would commit: {message}")
        return

    try:
        subprocess.run(
            ["git", "add", str(config_path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Warning: git commit failed: {e.stderr.decode().strip()}", file=sys.stderr)


def run(
    *,
    max_rounds: int = 10,
    data_dir: Path,
    config_path: Path = DEFAULT_CONFIG_PATH,
    dry_run: bool = False,
) -> dict[str, float]:
    """Run the hill-climbing optimization loop.

    For each round, iterates through every parameter in train_config.json:
    1. Try current + step: if cost improves, commit and update baseline
    2. Else try current - step: if cost improves, commit and update baseline
    3. Else skip

    Stops when no parameter improved in a round (converged).

    Args:
        max_rounds: Maximum number of optimization rounds.
        data_dir: Directory containing day data JSON files.
        config_path: Path to train_config.json.
        dry_run: If True, don't write config or git commit.

    Returns:
        Final tunable values as dict.
    """
    days = load_days(data_dir)
    if not days:
        print("Error: no day data found", file=sys.stderr)
        sys.exit(1)

    raw_config = _load_raw_config(config_path)
    tunables = load_config(config_path)

    # Baseline evaluation
    baseline_result = evaluate_multi_day(days, tunables)
    baseline_cost = baseline_result.composite_metric
    print(f"Baseline cost: ${baseline_cost:.4f} ({len(days)} days)")

    total_improvements = 0

    for round_num in range(1, max_rounds + 1):
        round_improvements = 0
        print(f"\n--- Round {round_num}/{max_rounds} ---")

        for param_name, spec in raw_config["parameters"].items():
            old_value = tunables[param_name]
            step = spec["step"]
            lo = spec["min"]
            hi = spec["max"]

            improved = False

            # Try +step
            candidate_up = min(hi, round(old_value + step, 6))
            if candidate_up != old_value:
                tunables[param_name] = candidate_up
                result_up = evaluate_multi_day(days, tunables)
                if result_up.composite_metric < baseline_cost:
                    msg = (
                        f"autotune: {param_name} {old_value} -> {candidate_up} "
                        f"(${baseline_cost:.4f} -> ${result_up.composite_metric:.4f})"
                    )
                    print(f"  [+] {msg}")
                    baseline_cost = result_up.composite_metric

                    # Persist
                    spec["value"] = candidate_up
                    if not dry_run:
                        _save_config(config_path, raw_config)
                    _git_commit(config_path, msg, dry_run=dry_run)

                    improved = True
                    round_improvements += 1
                else:
                    tunables[param_name] = old_value

            # Try -step if +step didn't help
            if not improved:
                candidate_down = max(lo, round(old_value - step, 6))
                if candidate_down != old_value:
                    tunables[param_name] = candidate_down
                    result_down = evaluate_multi_day(days, tunables)
                    if result_down.composite_metric < baseline_cost:
                        msg = (
                            f"autotune: {param_name} {old_value} -> {candidate_down} "
                            f"(${baseline_cost:.4f} -> ${result_down.composite_metric:.4f})"
                        )
                        print(f"  [-] {msg}")
                        baseline_cost = result_down.composite_metric

                        # Persist
                        spec["value"] = candidate_down
                        if not dry_run:
                            _save_config(config_path, raw_config)
                        _git_commit(config_path, msg, dry_run=dry_run)

                        round_improvements += 1
                    else:
                        tunables[param_name] = old_value

        total_improvements += round_improvements

        if round_improvements == 0:
            print(f"\nConverged after {round_num} rounds (no improvements)")
            break

    print(f"\nFinal cost: ${baseline_cost:.4f}")
    print(f"Total improvements: {total_improvements}")
    return tunables


def main() -> None:
    """CLI entry point for the autotune runner."""
    parser = argparse.ArgumentParser(
        description="Deterministic hill-climbing optimizer for MPC tunables"
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=10,
        help="Maximum optimization rounds (default: 10)",
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
        default=DEFAULT_CONFIG_PATH,
        help="Path to train_config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing config or committing",
    )
    args = parser.parse_args()

    run(
        max_rounds=args.max_rounds,
        data_dir=args.data_dir,
        config_path=args.config,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
