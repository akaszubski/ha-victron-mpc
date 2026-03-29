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


# ──────────────────────────────────────────────────────────────────
# Regression: BatteryLife register (R2900) — bug discovered 2026-03-30
#
# BatteryLife was silently overwriting R2901 every ~15s, setting it
# above current SoC, causing 500W+ grid import while MPC reported
# 'discharge' mode. Fix: write R2900=12 every cycle to disable BL.
# ──────────────────────────────────────────────────────────────────


def test_batterylife_register_constant_exists():
    """REGISTER_BATTERYLIFE_STATE must be 2900."""
    from custom_components.victron_mpc.const import REGISTER_BATTERYLIFE_STATE

    assert REGISTER_BATTERYLIFE_STATE == 2900


def test_batterylife_register_imported_in_coordinator():
    """Coordinator must import REGISTER_BATTERYLIFE_STATE for R2900 writes."""
    import custom_components.victron_mpc.coordinator as coord

    assert hasattr(coord, "REGISTER_BATTERYLIFE_STATE")
    assert coord.REGISTER_BATTERYLIFE_STATE == 2900


def test_coordinator_has_write_batterylife_method():
    """Coordinator class must have _write_batterylife_register method."""
    from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

    assert hasattr(VictronMPCCoordinator, "_write_batterylife_register")
    assert callable(getattr(VictronMPCCoordinator, "_write_batterylife_register"))
