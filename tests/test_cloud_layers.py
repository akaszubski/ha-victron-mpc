"""Tests for Open-Meteo cloud layer calculation.

Based on working effective_cloud_pct() from forecasts.py and
the cloud layer weighting validated on 2026-03-18.
"""

from custom_components.victron_mpc.api.open_meteo import (
    DEFAULT_CLOUD_WEIGHTS,
    OpenMeteoClient,
)


def test_effective_cloud_clear_sky():
    """No cloud → 0% effective."""
    layers = {"low": 0, "mid": 0, "high": 0}
    assert OpenMeteoClient.effective_cloud_pct(layers) == 0.0


def test_effective_cloud_full_overcast():
    """100% all layers → near 100% effective."""
    layers = {"low": 100, "mid": 100, "high": 100}
    result = OpenMeteoClient.effective_cloud_pct(layers)
    assert result > 95  # Should be very high


def test_effective_cloud_high_cirrus_only():
    """100% high cirrus → low effective (barely blocks solar).

    This was the key insight on 2026-03-18: raw 100% cloud with only
    high cirrus produced 3.9kW solar. Old system would have derated heavily.
    Weighted average: 100*0.15 / (100*0.15 + 100*0.5 + 100*0.9) * 100 = 9.7%
    """
    layers = {"low": 0, "mid": 0, "high": 100}
    result = OpenMeteoClient.effective_cloud_pct(layers)
    assert result < 15  # Much less than raw 100%


def test_effective_cloud_low_stratus_only():
    """100% low stratus → high effective (major solar reduction).

    Weighted average: 100*0.9 / (100*1.55) * 100 = 58.1%
    """
    layers = {"low": 100, "mid": 0, "high": 0}
    result = OpenMeteoClient.effective_cloud_pct(layers)
    assert result > 50  # Dominant layer


def test_effective_cloud_mixed_layers():
    """Mixed layers — weighted average model.

    Real case from 2026-03-18 13:29 AEDT:
    low=26%, mid=71%, high=91% → effective cloud well below raw average
    """
    layers = {"low": 26, "mid": 71, "high": 91}
    result = OpenMeteoClient.effective_cloud_pct(layers)
    raw_average = (26 + 71 + 91) / 3  # ~62.7%
    # Weighted should be lower because high cirrus dominates but has low weight
    assert result < raw_average


def test_effective_cloud_custom_weights():
    """Custom weights override defaults."""
    layers = {"low": 50, "mid": 50, "high": 50}
    # Equal weights → exactly 50%
    equal_weights = {"low": 1.0, "mid": 1.0, "high": 1.0}
    result = OpenMeteoClient.effective_cloud_pct(layers, equal_weights)
    assert result == 50.0


def test_default_weights_match_config():
    """Default weights match the working config.py values."""
    assert DEFAULT_CLOUD_WEIGHTS["high"] == 0.15
    assert DEFAULT_CLOUD_WEIGHTS["mid"] == 0.5
    assert DEFAULT_CLOUD_WEIGHTS["low"] == 0.9
