"""Tests for autotune data pipeline: data_loader and fetch_data modules."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from autotune.data_loader import (
    get_available_days,
    load_day,
    load_period,
    validate_day,
)
from autotune.fetch_data import (
    build_day_json,
    compute_overnight_steps,
    compute_sunset_step,
    derive_price_band,
    interpolate_hourly_to_5min,
)
from autotune.types import DayData

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_day.json"


# ──────────────────────────────────────────────────────────────────
# data_loader tests
# ──────────────────────────────────────────────────────────────────


class TestLoadDay:
    """Tests for load_day function."""

    def test_load_day_from_fixture(self) -> None:
        """Load sample_day.json and verify DayData fields."""
        day = load_day(FIXTURE_PATH)
        assert isinstance(day, DayData)
        assert day.date == "2026-03-15"
        assert day.sunset_step == 216
        assert day.start_soc_kwh == 7.1
        assert day.price_bands is not None
        assert len(day.price_bands) == 288

    def test_load_day_arrays_288(self) -> None:
        """Verify all arrays are 288 entries."""
        day = load_day(FIXTURE_PATH)
        assert len(day.solar_kw_5min) == 288
        assert len(day.load_kw_5min) == 288
        assert len(day.buy_price_5min) == 288
        assert len(day.sell_price_5min) == 288


class TestLoadPeriod:
    """Tests for load_period function."""

    def test_load_period_date_range(self, tmp_path: Path) -> None:
        """Load subset of 3 files by date range."""
        for date in ["2026-03-10", "2026-03-11", "2026-03-12"]:
            data = _make_minimal_day(date)
            (tmp_path / f"{date}.json").write_text(json.dumps(data))

        days = load_period(tmp_path, "2026-03-10", "2026-03-11")
        assert len(days) == 2
        assert days[0].date == "2026-03-10"
        assert days[1].date == "2026-03-11"

    def test_load_period_sorted(self, tmp_path: Path) -> None:
        """Verify returned days are in date order."""
        for date in ["2026-03-12", "2026-03-10", "2026-03-11"]:
            data = _make_minimal_day(date)
            (tmp_path / f"{date}.json").write_text(json.dumps(data))

        days = load_period(tmp_path, "2026-03-10", "2026-03-12")
        dates = [d.date for d in days]
        assert dates == ["2026-03-10", "2026-03-11", "2026-03-12"]


class TestGetAvailableDays:
    """Tests for get_available_days function."""

    def test_get_available_days(self, tmp_path: Path) -> None:
        """Temp dir with 3 files returns sorted date list."""
        for date in ["2026-03-12", "2026-03-10", "2026-03-11"]:
            (tmp_path / f"{date}.json").write_text("{}")

        result = get_available_days(tmp_path)
        assert result == ["2026-03-10", "2026-03-11", "2026-03-12"]


class TestValidateDay:
    """Tests for validate_day function."""

    def test_validate_day_valid(self) -> None:
        """Fixture data returns empty warnings."""
        day = load_day(FIXTURE_PATH)
        warnings = validate_day(day)
        assert warnings == []

    def test_validate_day_wrong_array_length(self) -> None:
        """Array with 100 entries returns warning."""
        day = _make_day_data(solar_kw_5min=[0.0] * 100)
        warnings = validate_day(day)
        assert any("solar_kw_5min has 100 entries" in w for w in warnings)

    def test_validate_day_negative_solar(self) -> None:
        """Negative solar value returns warning."""
        solar = [0.0] * 288
        solar[50] = -1.5
        day = _make_day_data(solar_kw_5min=solar)
        warnings = validate_day(day)
        assert any("negative" in w.lower() for w in warnings)

    def test_validate_day_nan_price(self) -> None:
        """NaN in buy price returns warning."""
        buy = [0.15] * 288
        buy[100] = float("nan")
        day = _make_day_data(buy_price_5min=buy)
        warnings = validate_day(day)
        assert any("NaN" in w or "nan" in w.lower() for w in warnings)

    def test_validate_day_sunset_out_of_range(self) -> None:
        """sunset_step=300 returns warning."""
        day = _make_day_data(sunset_step=300)
        warnings = validate_day(day)
        assert any("sunset_step" in w for w in warnings)

    def test_validate_day_negative_load(self) -> None:
        """Negative load returns warning."""
        load = [1.0] * 288
        load[10] = -0.5
        day = _make_day_data(load_kw_5min=load)
        warnings = validate_day(day)
        assert any("negative" in w.lower() for w in warnings)

    def test_validate_day_extreme_buy_price(self) -> None:
        """Buy price > 10.0 returns warning."""
        buy = [0.15] * 288
        buy[0] = 15.0
        day = _make_day_data(buy_price_5min=buy)
        warnings = validate_day(day)
        assert any("outside range" in w for w in warnings)

    def test_validate_day_soc_out_of_range(self) -> None:
        """start_soc_kwh > 14.2 returns warning."""
        day = _make_day_data(start_soc_kwh=20.0)
        warnings = validate_day(day)
        assert any("start_soc_kwh" in w for w in warnings)


# ──────────────────────────────────────────────────────────────────
# fetch_data pure function tests
# ──────────────────────────────────────────────────────────────────


class TestInterpolateHourly:
    """Tests for interpolate_hourly_to_5min function."""

    def test_interpolate_hourly_to_5min(self) -> None:
        """24 values expand to 288 with constant within each hour."""
        hourly = list(range(24))
        result = interpolate_hourly_to_5min([float(h) for h in hourly])
        assert len(result) == 288
        # Each hour should repeat 12 times
        for h in range(24):
            for offset in range(12):
                assert result[h * 12 + offset] == float(h)

    def test_interpolate_wrong_length_raises(self) -> None:
        """Non-24 input raises ValueError."""
        with pytest.raises(ValueError, match="Expected 24"):
            interpolate_hourly_to_5min([1.0] * 10)


class TestComputeSunsetStep:
    """Tests for compute_sunset_step function."""

    def test_compute_sunset_step_march_equinox(self) -> None:
        """Mid-March Melbourne should be around step 216-222."""
        step = compute_sunset_step("2026-03-15", latitude=-37.81)
        assert 210 <= step <= 228, f"March sunset step {step} outside expected range"

    def test_compute_sunset_step_winter(self) -> None:
        """June Melbourne sunset should be much earlier, around step 180-204."""
        step = compute_sunset_step("2026-06-21", latitude=-37.81)
        assert 180 <= step <= 210, f"Winter sunset step {step} outside expected range"

    def test_compute_sunset_step_summer(self) -> None:
        """December Melbourne sunset should be late, around step 240+."""
        step = compute_sunset_step("2026-12-21", latitude=-37.81)
        assert 228 <= step <= 260, f"Summer sunset step {step} outside expected range"


class TestComputeOvernightSteps:
    """Tests for compute_overnight_steps function."""

    def test_compute_overnight_steps(self) -> None:
        """Verify 96 steps total covering hours 0-5 and 22-23."""
        steps = compute_overnight_steps()
        assert len(steps) == 96
        # Hours 0-5: steps 0-71
        for s in range(72):
            assert s in steps
        # Hours 22-23: steps 264-287
        for s in range(264, 288):
            assert s in steps
        # Nothing in between
        for s in range(72, 264):
            assert s not in steps


class TestDerivePriceBand:
    """Tests for derive_price_band function."""

    def test_derive_price_band_thresholds(self) -> None:
        """All band transitions at correct thresholds."""
        assert derive_price_band(-0.05) == "extremely_low"
        assert derive_price_band(0.0) == "very_low"
        assert derive_price_band(0.07) == "very_low"
        assert derive_price_band(0.08) == "low"
        assert derive_price_band(0.14) == "low"
        assert derive_price_band(0.15) == "neutral"
        assert derive_price_band(0.34) == "neutral"
        assert derive_price_band(0.35) == "high"
        assert derive_price_band(0.79) == "high"
        assert derive_price_band(0.80) == "spike"
        assert derive_price_band(1.50) == "spike"


class TestBuildDayJson:
    """Tests for build_day_json function."""

    def test_build_day_json_complete(self) -> None:
        """Mock hourly data produces a valid DayData-compatible dict."""
        solar_h = [0.0] * 6 + [1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 4.0, 3.0, 2.0, 1.0] + [0.0] * 8
        load_h = [1.0] * 24
        buy_h = [0.15] * 24
        sell_h = [0.03] * 24

        result = build_day_json("2026-03-15", solar_h, load_h, buy_h, sell_h)

        assert result["date"] == "2026-03-15"
        assert len(result["solar_kw_5min"]) == 288
        assert len(result["load_kw_5min"]) == 288
        assert len(result["buy_price_5min"]) == 288
        assert len(result["sell_price_5min"]) == 288
        assert 0 <= result["sunset_step"] <= 287
        assert len(result["overnight_steps"]) == 96
        assert result["start_soc_kwh"] == 7.1
        assert result["price_bands"] is not None
        assert len(result["price_bands"]) == 288

        # Verify it can be loaded as DayData
        day = DayData(**result)
        assert day.date == "2026-03-15"


# ──────────────────────────────────────────────────────────────────
# Import safety test
# ──────────────────────────────────────────────────────────────────


class TestNoHomeAssistantImports:
    """Verify modules have no homeassistant imports."""

    def test_no_homeassistant_imports(self) -> None:
        """data_loader.py and fetch_data.py must not import homeassistant."""
        for module_name in ("data_loader", "fetch_data"):
            path = Path(__file__).parent.parent / "autotune" / f"{module_name}.py"
            content = path.read_text()
            assert "homeassistant" not in content, (
                f"{module_name}.py contains 'homeassistant' import"
            )
            assert "custom_components" not in content, (
                f"{module_name}.py contains 'custom_components' import"
            )


# ──────────────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────────────


def _make_minimal_day(date: str) -> dict:
    """Create a minimal valid day JSON dict."""
    return {
        "date": date,
        "solar_kw_5min": [0.0] * 288,
        "load_kw_5min": [1.0] * 288,
        "buy_price_5min": [0.15] * 288,
        "sell_price_5min": [0.03] * 288,
        "sunset_step": 216,
        "overnight_steps": list(range(72)) + list(range(264, 288)),
        "start_soc_kwh": 7.1,
    }


def _make_day_data(**overrides) -> DayData:
    """Create a DayData with sensible defaults, overridden by kwargs."""
    defaults = {
        "date": "2026-03-15",
        "solar_kw_5min": [0.0] * 288,
        "load_kw_5min": [1.0] * 288,
        "buy_price_5min": [0.15] * 288,
        "sell_price_5min": [0.03] * 288,
        "sunset_step": 216,
        "overnight_steps": list(range(72)) + list(range(264, 288)),
        "start_soc_kwh": 7.1,
    }
    defaults.update(overrides)
    return DayData(**defaults)
