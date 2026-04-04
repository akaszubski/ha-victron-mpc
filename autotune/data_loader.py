"""Cached data loader for autotune day data files.

Loads, validates, and queries DayData JSON files from disk.
Zero HA imports -- stdlib only.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .types import DayData

BATTERY_CAPACITY_KWH = 14.2
EXPECTED_STEPS = 288


def load_day(path: Path) -> DayData:
    """Load a single day JSON file into a DayData instance.

    Args:
        path: Path to the JSON file.

    Returns:
        DayData populated from the JSON contents.

    Raises:
        FileNotFoundError: If path does not exist.
        KeyError: If required fields are missing.
    """
    with open(path) as f:
        raw = json.load(f)

    return DayData(
        date=raw["date"],
        solar_kw_5min=raw["solar_kw_5min"],
        load_kw_5min=raw["load_kw_5min"],
        buy_price_5min=raw["buy_price_5min"],
        sell_price_5min=raw["sell_price_5min"],
        sunset_step=raw["sunset_step"],
        overnight_steps=raw.get("overnight_steps", []),
        start_soc_kwh=raw.get("start_soc_kwh", 7.1),
        price_bands=raw.get("price_bands"),
    )


def load_period(data_dir: Path, start: str, end: str) -> list[DayData]:
    """Load DayData files for a date range [start, end] inclusive.

    Args:
        data_dir: Directory containing YYYY-MM-DD.json files.
        start: Start date string (YYYY-MM-DD), inclusive.
        end: End date string (YYYY-MM-DD), inclusive.

    Returns:
        List of DayData sorted by date.
    """
    days: list[DayData] = []
    for path in sorted(data_dir.glob("*.json")):
        date_str = path.stem
        if start <= date_str <= end:
            days.append(load_day(path))
    return days


def validate_day(day: DayData) -> list[str]:
    """Run quality checks on a DayData instance.

    Args:
        day: DayData to validate.

    Returns:
        List of warning strings. Empty list means data is clean.
    """
    warnings: list[str] = []

    # Array length checks
    for name in ("solar_kw_5min", "load_kw_5min", "buy_price_5min", "sell_price_5min"):
        arr = getattr(day, name)
        if len(arr) != EXPECTED_STEPS:
            warnings.append(f"{name} has {len(arr)} entries, expected {EXPECTED_STEPS}")

    # sunset_step range
    if not (0 <= day.sunset_step <= 287):
        warnings.append(f"sunset_step={day.sunset_step} out of range 0-287")

    # overnight_steps range
    for step in day.overnight_steps:
        if not (0 <= step <= 287):
            warnings.append(f"overnight_step={step} out of range 0-287")
            break  # One warning is enough

    # NaN/inf checks on numeric arrays
    for name in ("solar_kw_5min", "load_kw_5min", "buy_price_5min", "sell_price_5min"):
        arr = getattr(day, name)
        for i, v in enumerate(arr):
            if math.isnan(v) or math.isinf(v):
                warnings.append(f"{name}[{i}] is NaN or inf")
                break  # One warning per array is enough

    # Solar non-negative
    for i, v in enumerate(day.solar_kw_5min):
        if v < 0:
            warnings.append(f"solar_kw_5min[{i}] is negative ({v})")
            break

    # Load non-negative
    for i, v in enumerate(day.load_kw_5min):
        if v < 0:
            warnings.append(f"load_kw_5min[{i}] is negative ({v})")
            break

    # Buy price reasonable range
    for i, v in enumerate(day.buy_price_5min):
        if not (-1.0 <= v <= 10.0):
            warnings.append(f"buy_price_5min[{i}]={v} outside range [-1.0, 10.0]")
            break

    # start_soc_kwh range
    if not (0 <= day.start_soc_kwh <= BATTERY_CAPACITY_KWH):
        warnings.append(
            f"start_soc_kwh={day.start_soc_kwh} outside range [0, {BATTERY_CAPACITY_KWH}]"
        )

    return warnings


def get_available_days(data_dir: Path) -> list[str]:
    """Return sorted list of YYYY-MM-DD date strings from JSON filenames.

    Args:
        data_dir: Directory containing YYYY-MM-DD.json files.

    Returns:
        Sorted list of date strings.
    """
    dates: list[str] = []
    for path in sorted(data_dir.glob("*.json")):
        dates.append(path.stem)
    return dates
