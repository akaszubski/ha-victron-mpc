"""Human-readable reports for autotune optimization results."""

from __future__ import annotations

from .types import DayResult, EvalResult


def generate_report(
    baseline: EvalResult,
    optimized: EvalResult,
    before_params: dict[str, float],
    after_params: dict[str, float],
) -> str:
    """Generate before/after comparison report.

    Args:
        baseline: Evaluation result with original parameters.
        optimized: Evaluation result with optimized parameters.
        before_params: Original parameter values.
        after_params: Optimized parameter values.

    Returns:
        Formatted multi-line report string.
    """
    lines: list[str] = []
    lines.append("=== AUTOTUNE OPTIMIZATION REPORT ===")
    lines.append("")

    # Summary
    delta = optimized.composite_metric - baseline.composite_metric
    pct = (delta / baseline.composite_metric * 100) if baseline.composite_metric else 0
    days = len(baseline.per_day)
    lines.append(f"Evaluation period: {days} days")
    lines.append(f"Baseline metric:   ${baseline.composite_metric:.4f}")
    lines.append(f"Optimized metric:  ${optimized.composite_metric:.4f}")
    lines.append(f"Delta:             ${delta:+.4f} ({pct:+.1f}%)")

    if days > 0:
        annual = delta * 365 / days
        lines.append(f"Projected annual:  ${annual:+.2f}/year")

    lines.append("")

    # Parameter changes
    changes = compare_configs(before_params, after_params)
    lines.append(changes)

    # Breakdown comparison
    lines.append("")
    lines.append("--- Cost Breakdown ---")
    for key in baseline.breakdown:
        b_val = baseline.breakdown.get(key, 0)
        o_val = optimized.breakdown.get(key, 0)
        if isinstance(b_val, (int, float)) and isinstance(o_val, (int, float)):
            lines.append(f"  {key:25s}  {b_val:>10.4f} -> {o_val:>10.4f}")

    return "\n".join(lines)


def compare_configs(before: dict[str, float], after: dict[str, float]) -> str:
    """Side-by-side parameter comparison.

    Args:
        before: Parameter values before optimization.
        after: Parameter values after optimization.

    Returns:
        Formatted string showing changed parameters.
    """
    lines = ["--- Parameter Changes ---"]
    changed = False
    for key in sorted(set(before) | set(after)):
        b = before.get(key, 0)
        a = after.get(key, 0)
        if abs(a - b) > 1e-6:
            pct = ((a - b) / b * 100) if b else 0
            lines.append(f"  {key:30s}  {b:.4f} -> {a:.4f}  ({pct:+.1f}%)")
            changed = True
    if not changed:
        lines.append("  (no changes)")
    return "\n".join(lines)


def daily_breakdown(result: EvalResult) -> str:
    """Day-by-day cost table.

    Args:
        result: EvalResult containing per-day results.

    Returns:
        Formatted table string with daily cost breakdown.
    """
    lines = [
        "--- Daily Breakdown ---",
        f"{'Date':12s} {'Grid$':>8s} {'Export$':>8s} {'Wear$':>8s} {'Floor':>6s} {'Sunset%':>8s} {'MinSoC%':>8s}",
        "-" * 62,
    ]
    for r in result.per_day:
        lines.append(
            f"{r.date:12s} {r.grid_cost:8.2f} {r.export_revenue:8.2f} "
            f"{r.wear_cost_fixed:8.4f} {r.floor_violations:6d} "
            f"{r.sunset_soc_pct:8.1f} {r.min_soc_pct:8.1f}"
        )
    lines.append("-" * 62)
    total_grid = sum(r.grid_cost for r in result.per_day)
    total_export = sum(r.export_revenue for r in result.per_day)
    lines.append(f"{'TOTAL':12s} {total_grid:8.2f} {total_export:8.2f}")
    lines.append(f"\nComposite metric: ${result.composite_metric:.4f}")
    return "\n".join(lines)
