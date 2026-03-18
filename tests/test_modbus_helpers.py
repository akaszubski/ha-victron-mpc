"""Tests for Modbus register helpers.

Based on working register logic from optimizer.py and
automations_mpc_control.yaml.
"""

from custom_components.victron_mpc.api.modbus import (
    register_to_soc,
    soc_to_register,
)


def test_soc_to_register_normal():
    """75% SoC → register 750."""
    assert soc_to_register(75.0) == 750


def test_soc_to_register_full():
    """100% SoC → register 1000."""
    assert soc_to_register(100.0) == 1000


def test_soc_to_register_minimum():
    """10% SoC → register 100 (hardware floor)."""
    assert soc_to_register(10.0) == 100


def test_soc_to_register_clamps_low():
    """Below 10% clamps to 100."""
    assert soc_to_register(5.0) == 100


def test_soc_to_register_clamps_high():
    """Above 100% clamps to 1000."""
    assert soc_to_register(110.0) == 1000


def test_soc_to_register_rounding():
    """75.3% → 753 (rounds correctly)."""
    assert soc_to_register(75.3) == 753


def test_register_to_soc_roundtrip():
    """Register roundtrip: 60% → 600 → 60%."""
    assert register_to_soc(soc_to_register(60.0)) == 60.0
