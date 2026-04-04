"""Amber forecast accuracy analysis.

Processes the rolling amber_forecast_log buffer to compute
forecast bias by time-of-day and horizon, plus spike prediction accuracy.

Pure functions only -- no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def compute_forecast_accuracy(log_entries: list[dict]) -> dict:
    """Analyze forecast vs actual prices from the rolling log.

    Args:
        log_entries: List of dicts from coordinator._amber_forecast_log.
            Each entry has: timestamp, hour, actual_buy, +1h, +2h, +3h, +6h

    Returns:
        Analysis dict with bias_by_hour, mae_by_horizon, spike_accuracy, etc.
        Returns empty dict if fewer than 100 entries.
    """
    if len(log_entries) < 100:
        return {}

    # Build lookup: rounded timestamp -> entry for matching forecasts to actuals
    by_timestamp: dict[str, dict] = {}
    for entry in log_entries:
        ts = entry.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            key = dt.strftime("%Y-%m-%d %H:%M")
            by_timestamp[key] = entry
        except (ValueError, TypeError):
            continue

    # Compute bias and MAE per horizon
    horizons = {"+1h": 12, "+2h": 24, "+3h": 36, "+6h": 72}  # steps at 5-min
    bias_by_horizon: dict[str, list[float]] = {}
    mae_by_horizon: dict[str, list[float]] = {}
    errors_by_hour: dict[int, list[float]] = {h: [] for h in range(24)}

    for entry in log_entries:
        ts = entry.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue

        for label, steps in horizons.items():
            forecast_price = entry.get(label)
            if forecast_price is None:
                continue

            # Find actual price `steps * 5 minutes` later
            target_dt = dt + timedelta(minutes=steps * 5)
            target_key = target_dt.strftime("%Y-%m-%d %H:%M")
            target_entry = by_timestamp.get(target_key)
            if target_entry is None:
                continue

            actual_price = target_entry.get("actual_buy")
            if actual_price is None:
                continue

            error = float(forecast_price) - float(actual_price)

            bias_by_horizon.setdefault(label, []).append(error)
            mae_by_horizon.setdefault(label, []).append(abs(error))

            target_hour = target_dt.hour
            errors_by_hour[target_hour].append(error)

    # Aggregate bias/MAE by horizon
    result_bias_horizon: dict[str, float] = {}
    result_mae_horizon: dict[str, float] = {}
    for label in horizons:
        if label in bias_by_horizon and bias_by_horizon[label]:
            vals = bias_by_horizon[label]
            result_bias_horizon[label] = round(sum(vals) / len(vals), 4)
        if label in mae_by_horizon and mae_by_horizon[label]:
            vals = mae_by_horizon[label]
            result_mae_horizon[label] = round(sum(vals) / len(vals), 4)

    # Bias by hour of day
    result_bias_hour: dict[int, float] = {}
    for h in range(24):
        if errors_by_hour[h]:
            vals = errors_by_hour[h]
            result_bias_hour[h] = round(sum(vals) / len(vals), 4)

    # Spike prediction accuracy
    spike_tp = 0
    spike_fp = 0
    spike_fn = 0
    spike_tn = 0

    for entry in log_entries:
        actual_spike = entry.get("spike_actual", "none")
        is_spike = actual_spike not in ("none", None, "")

        # Check if any horizon predicted spike
        predicted_spike = False
        for label in ["+1h", "+2h", "+3h", "+6h"]:
            spike_val = entry.get(f"{label}_spike", "none")
            if spike_val not in ("none", None, ""):
                predicted_spike = True
                break

        if predicted_spike and is_spike:
            spike_tp += 1
        elif predicted_spike and not is_spike:
            spike_fp += 1
        elif not predicted_spike and is_spike:
            spike_fn += 1
        else:
            spike_tn += 1

    spike_accuracy = {
        "true_positive_rate": round(
            spike_tp / max(1, spike_tp + spike_fn), 4
        ),
        "false_positive_rate": round(
            spike_fp / max(1, spike_fp + spike_tn), 4
        ),
        "true_positives": spike_tp,
        "false_positives": spike_fp,
        "false_negatives": spike_fn,
        "total_entries": spike_tp + spike_fp + spike_fn + spike_tn,
    }

    # Coverage in hours
    coverage_hours = 0.0
    if log_entries:
        try:
            first = datetime.fromisoformat(log_entries[0].get("timestamp", ""))
            last = datetime.fromisoformat(log_entries[-1].get("timestamp", ""))
            coverage_hours = round((last - first).total_seconds() / 3600, 1)
        except (ValueError, TypeError):
            coverage_hours = 0.0

    return {
        "bias_by_hour": result_bias_hour,
        "mae_by_horizon": result_mae_horizon,
        "bias_by_horizon": result_bias_horizon,
        "spike_accuracy": spike_accuracy,
        "entry_count": len(log_entries),
        "matched_pairs": sum(len(v) for v in bias_by_horizon.values()),
        "coverage_hours": coverage_hours,
        "last_updated": datetime.now().isoformat(),
    }
