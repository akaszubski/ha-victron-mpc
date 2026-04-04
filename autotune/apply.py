"""Production deployment preview -- NEVER auto-applies.

This module is intentionally read-only. It compares optimized values
against production but never writes to any external system.

No network library imports (urllib, requests, socket, http.client).
"""

from __future__ import annotations

import json
from pathlib import Path


def preview_changes(
    train_config_path: Path,
    production_values: dict[str, float],
) -> str:
    """Show diff between optimised and current production values.

    NEVER writes to production. Read-only comparison.

    Args:
        train_config_path: Path to train_config.json.
        production_values: Current production parameter values.

    Returns:
        Formatted string showing what would change.
    """
    with open(train_config_path) as f:
        config = json.load(f)

    lines = ["=== PRODUCTION CHANGE PREVIEW ===", ""]
    lines.append(
        f"{'Parameter':30s} {'Production':>12s} {'Optimized':>12s} "
        f"{'Delta':>10s} {'Bounds':>12s}"
    )
    lines.append("-" * 80)

    any_changed = False
    for name, spec in config["parameters"].items():
        opt_val = spec["value"]
        prod_val = production_values.get(name, opt_val)
        delta = opt_val - prod_val
        in_bounds = spec["min"] <= opt_val <= spec["max"]
        bounds_str = "OK" if in_bounds else "OUT OF BOUNDS"
        marker = " *" if abs(delta) > 1e-6 else ""
        lines.append(
            f"{name:30s} {prod_val:12.4f} {opt_val:12.4f} "
            f"{delta:+10.4f} {bounds_str:>12s}{marker}"
        )
        if abs(delta) > 1e-6:
            any_changed = True

    lines.append("")
    if any_changed:
        lines.append("* = changed parameter")
        lines.append(
            "\nTo apply: Settings > Devices & Services > Victron MPC > Configure"
        )
    else:
        lines.append("No parameter changes to apply.")

    return "\n".join(lines)


def generate_options_flow_values(train_config_path: Path) -> dict[str, float]:
    """Output values for HA options flow update.

    Args:
        train_config_path: Path to train_config.json.

    Returns:
        Dict of parameter_name -> value for manual HA config entry.
    """
    with open(train_config_path) as f:
        config = json.load(f)

    return {name: spec["value"] for name, spec in config["parameters"].items()}
