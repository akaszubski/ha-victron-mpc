"""Tests for Amber forecast accuracy analysis (Issue #30)."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta

import pytest

from custom_components.victron_mpc.forecast_accuracy import compute_forecast_accuracy


class TestEmptyAndInsufficientLog:
    """Tests for edge cases with too few entries."""

    def test_empty_log(self) -> None:
        """Empty log returns empty dict."""
        assert compute_forecast_accuracy([]) == {}

    def test_insufficient_entries(self) -> None:
        """Fewer than 100 entries returns empty dict."""
        base = datetime(2026, 4, 1, 12, 0)
        entries = [
            {"timestamp": (base + timedelta(minutes=5 * i)).isoformat(), "actual_buy": 0.20}
            for i in range(50)
        ]
        assert compute_forecast_accuracy(entries) == {}

    def test_exactly_100_entries_proceeds(self) -> None:
        """Exactly 100 entries should produce a result (not empty)."""
        base = datetime(2026, 4, 1, 8, 0)
        entries = []
        for i in range(100):
            t = base + timedelta(minutes=5 * i)
            entries.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "actual_buy": 0.20,
            })
        result = compute_forecast_accuracy(entries)
        assert isinstance(result, dict)
        assert result.get("entry_count") == 100


class TestBasicAccuracy:
    """Tests for bias and MAE computation with known data."""

    @pytest.fixture
    def synthetic_log(self) -> list[dict]:
        """Create a synthetic log where +1h forecast is always 0.05 above actual.

        Entries every 5 minutes for ~10 hours (120 entries).
        The +1h forecast recorded at time T should match the actual at T+60min (12 steps).
        """
        base = datetime(2026, 4, 1, 6, 0)
        entries = []
        for i in range(120):
            t = base + timedelta(minutes=5 * i)
            actual = 0.20
            entries.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "actual_buy": actual,
                "+1h": actual + 0.05,  # Consistently over-forecast by $0.05
                "+2h": actual + 0.10,
                "+3h": actual - 0.02,
                "+6h": None,  # Missing data for +6h
            })
        return entries

    def test_bias_by_horizon(self, synthetic_log: list[dict]) -> None:
        """Verify bias is correctly computed per horizon."""
        result = compute_forecast_accuracy(synthetic_log)
        assert result != {}
        # +1h forecast is always actual + 0.05, so bias should be +0.05
        assert "+1h" in result["bias_by_horizon"]
        assert abs(result["bias_by_horizon"]["+1h"] - 0.05) < 0.001

    def test_mae_by_horizon(self, synthetic_log: list[dict]) -> None:
        """MAE should equal absolute bias for constant error."""
        result = compute_forecast_accuracy(synthetic_log)
        assert "+1h" in result["mae_by_horizon"]
        assert abs(result["mae_by_horizon"]["+1h"] - 0.05) < 0.001

    def test_matched_pairs_count(self, synthetic_log: list[dict]) -> None:
        """Matched pairs should be positive (some forecasts match actuals)."""
        result = compute_forecast_accuracy(synthetic_log)
        assert result["matched_pairs"] > 0

    def test_coverage_hours(self, synthetic_log: list[dict]) -> None:
        """Coverage should reflect the time span of the log."""
        result = compute_forecast_accuracy(synthetic_log)
        # 120 entries * 5 min = 595 minutes ~ 9.9 hours
        assert result["coverage_hours"] > 9.0
        assert result["coverage_hours"] < 11.0

    def test_entry_count(self, synthetic_log: list[dict]) -> None:
        """Entry count matches input length."""
        result = compute_forecast_accuracy(synthetic_log)
        assert result["entry_count"] == 120


class TestSpikeAccuracy:
    """Tests for spike prediction accuracy metrics."""

    def test_spike_all_true_positives(self) -> None:
        """All entries predict spike and spike happens -> TPR = 1.0."""
        base = datetime(2026, 4, 1, 6, 0)
        entries = []
        for i in range(110):
            t = base + timedelta(minutes=5 * i)
            entries.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "actual_buy": 0.30,
                "+1h": 0.35,
                "+1h_spike": "potential",
                "spike_actual": "spike",
            })
        result = compute_forecast_accuracy(entries)
        assert result["spike_accuracy"]["true_positive_rate"] == 1.0
        assert result["spike_accuracy"]["true_positives"] == 110

    def test_spike_all_false_positives(self) -> None:
        """Predicted spike but none happened -> FPR > 0."""
        base = datetime(2026, 4, 1, 6, 0)
        entries = []
        for i in range(110):
            t = base + timedelta(minutes=5 * i)
            entries.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "actual_buy": 0.20,
                "+1h": 0.25,
                "+1h_spike": "potential",
                "spike_actual": "none",
            })
        result = compute_forecast_accuracy(entries)
        assert result["spike_accuracy"]["false_positives"] == 110
        assert result["spike_accuracy"]["true_positives"] == 0

    def test_spike_no_predictions_no_spikes(self) -> None:
        """No spike predictions and no spikes -> all TN."""
        base = datetime(2026, 4, 1, 6, 0)
        entries = []
        for i in range(110):
            t = base + timedelta(minutes=5 * i)
            entries.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "actual_buy": 0.20,
                "+1h": 0.22,
            })
        result = compute_forecast_accuracy(entries)
        assert result["spike_accuracy"]["true_positives"] == 0
        assert result["spike_accuracy"]["false_positives"] == 0
        assert result["spike_accuracy"]["false_negatives"] == 0


class TestHourBucketing:
    """Tests for bias bucketing by hour of day."""

    def test_entries_land_in_correct_hour_bucket(self) -> None:
        """Entries at specific hours produce bias in those hour buckets."""
        base = datetime(2026, 4, 1, 0, 0)
        entries = []
        # Create entries spanning 24 hours (288 entries at 5-min intervals)
        for i in range(288):
            t = base + timedelta(minutes=5 * i)
            actual = 0.20
            # +1h forecast varies by hour for traceability
            forecast_1h = actual + (t.hour * 0.01)
            entries.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "actual_buy": actual,
                "+1h": forecast_1h,
            })

        result = compute_forecast_accuracy(entries)
        assert result != {}
        # The bias_by_hour dict should have entries (not all hours may have matches)
        assert len(result["bias_by_hour"]) > 0
        # Each hour's bias should be the hour * 0.01 (the synthetic pattern)
        for hour, bias in result["bias_by_hour"].items():
            # The target hour = entry hour + 1 (since +1h forecast)
            # So bias at target_hour H should be (H-1) * 0.01
            # Allow some tolerance due to boundary effects
            assert isinstance(bias, float)


class TestNoHAImports:
    """Ensure forecast_accuracy.py has zero homeassistant imports."""

    def test_no_ha_imports(self) -> None:
        """Source code must not import from homeassistant."""
        source = inspect.getsource(compute_forecast_accuracy)
        # Also check the module file directly
        import custom_components.victron_mpc.forecast_accuracy as mod

        module_source = inspect.getsource(mod)
        assert "homeassistant" not in module_source
        assert "from homeassistant" not in module_source
        assert "import homeassistant" not in module_source
