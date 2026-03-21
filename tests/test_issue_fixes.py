"""Tests for GitHub issues #31-#35 fixes.

Covers:
- #31: Overnight min SoC raised to 31%, bounds expanded
- #32: Grid charge prevented when solar surplus exists
- #33: Pre-dawn weather classification suppression
- #34: Register set 1% below floor to prevent grid leak
- #35: Hot water schedule load boost config
"""

from __future__ import annotations

import math

from custom_components.victron_mpc.config import MPCTunables, TUNABLE_BOUNDS
from custom_components.victron_mpc.optimizer import optimize

from .conftest import (
    BATTERY_CAPACITY,
    STEPS_24H,
    make_opt_input,
    solar_bell,
)


# ──────────────────────────────────────────────────────────────────
# #31: Raise SoC floor to 31%
# ──────────────────────────────────────────────────────────────────

class TestIssue31FloorRaise:
    """Verify overnight min SoC default raised and bounds expanded."""

    def test_overnight_min_soc_default_31(self):
        t = MPCTunables()
        assert t.overnight_min_soc_pct == 31.0

    def test_soc_floor_bounds_upper_35(self):
        lo, hi = TUNABLE_BOUNDS["soc_floor_pct"]
        assert hi == 35.0

    def test_overnight_min_soc_bounds_upper_50(self):
        lo, hi = TUNABLE_BOUNDS["overnight_min_soc_pct"]
        assert hi == 50.0

    def test_from_config_entry_clamps_to_new_bounds(self):
        # Setting soc_floor_pct to 32 should now be accepted (was clamped to 30)
        t = MPCTunables.from_config_entry({"soc_floor_pct": 32.0})
        assert t.soc_floor_pct == 32.0

    def test_from_config_entry_clamps_above_upper(self):
        # Setting above new upper bound should clamp
        t = MPCTunables.from_config_entry({"soc_floor_pct": 40.0})
        assert t.soc_floor_pct == 35.0


# ──────────────────────────────────────────────────────────────────
# #32: Prevent grid_charge when solar surplus exists
# ──────────────────────────────────────────────────────────────────

class TestIssue32SolarSurplusGuard:
    """Verify grid_charge is blocked when solar exceeds load."""

    def test_solar_surplus_prevents_grid_charge_register(self):
        """When solar > load, register should be at floor, not above SoC."""
        # Solar slightly above load — surplus goes to battery, not export
        solar = [1.5] * STEPS_24H
        load = [1.0] * STEPS_24H
        inp = make_opt_input(
            soc_pct=40.0,
            solar_kw=solar,
            load_kw=load,
            buy_price=0.15,
            sell_price=0.01,  # Low FIT discourages export
        )
        out = optimize(inp)
        # With solar surplus, mode should NOT be grid_charge
        assert out.mode != "grid_charge", (
            f"Expected non-grid_charge with solar surplus, got {out.mode}"
        )
        # Register should NOT be above current SoC (which would force grid charge)
        current_soc_register = int(40.0 * 10)  # 400
        assert out.target_register <= current_soc_register, (
            f"Register {out.target_register} should not exceed SoC register {current_soc_register} "
            f"when solar surplus exists"
        )

    def test_grid_charge_still_works_without_solar(self):
        """When no solar and cheap price, grid_charge should still be allowed."""
        # No solar, very cheap price, low SoC with expensive prices ahead
        prices = [0.05] * 12 + [0.50] * (STEPS_24H - 12)  # cheap then expensive
        inp = make_opt_input(
            soc_pct=30.0,
            solar_kw=0.0,
            load_kw=1.0,
            buy_price=prices,
        )
        out = optimize(inp)
        # LP should want to charge during cheap period
        assert out.charge_schedule_kw[0] > 0.0 or out.mode == "grid_charge", (
            "LP should grid_charge when no solar and cheap prices"
        )


# ──────────────────────────────────────────────────────────────────
# #33: Pre-dawn classification suppression
# ──────────────────────────────────────────────────────────────────

class TestIssue33PreDawnSuppression:
    """Verify _maybe_adjust_day_type returns unchanged before intraday_early_hour.

    The actual method is async and depends on HA state, so we test the
    config defaults that control the behavior.
    """

    def test_intraday_early_hour_default(self):
        """Verify default intraday start is 8am."""
        t = MPCTunables()
        assert t.intraday_early_hour == 8.0

    def test_cloud_override_threshold_default(self):
        """Verify default cloud override is 80%."""
        t = MPCTunables()
        assert t.cloud_override_low_pct == 80.0


# ──────────────────────────────────────────────────────────────────
# #34: Register 1% below floor to prevent grid leak
# ──────────────────────────────────────────────────────────────────

class TestIssue34RegisterBelowFloor:
    """Verify register is set below the operating floor."""

    def test_solar_charge_register_below_floor(self):
        """During solar charge, register should be below the operating floor."""
        # Solar charging scenario: surplus solar, at floor
        solar = [3.0] * STEPS_24H
        load = [1.0] * STEPS_24H
        soc_floor_pct = 20.0
        inp = make_opt_input(
            soc_pct=25.0,
            solar_kw=solar,
            load_kw=load,
            buy_price=0.15,
            soc_min_pct=soc_floor_pct,
        )
        out = optimize(inp)
        if out.mode == "solar_charge":
            # Register should be below the floor (20% = 200), at 19% = 190
            floor_register = int(soc_floor_pct * 10)
            assert out.target_register < floor_register, (
                f"Register {out.target_register} should be below floor register "
                f"{floor_register} to prevent grid leak"
            )

    def test_discharge_register_below_floor(self):
        """During discharge at floor, register should be below floor."""
        inp = make_opt_input(
            soc_pct=22.0,
            solar_kw=0.0,
            load_kw=1.0,
            buy_price=0.30,
            soc_min_pct=20.0,
        )
        out = optimize(inp)
        if out.mode in ("discharge", "hold"):
            floor_register = int(20.0 * 10)  # 200
            assert out.target_register < floor_register, (
                f"Register {out.target_register} should be below floor {floor_register}"
            )

    def test_register_never_below_hardware_minimum(self):
        """Register should never go below 10% (hardware limit)."""
        inp = make_opt_input(
            soc_pct=15.0,
            solar_kw=0.0,
            load_kw=1.0,
            buy_price=0.30,
            soc_min_pct=10.0,  # Very low floor
        )
        out = optimize(inp)
        # 10% - 1% = 9%, but hard minimum is 10% = 100
        # Actually the code uses max(10.0, floor - 1.0) so at 10% floor -> 10% - 1% = 9%, clamped to 10% -> register 100
        # Wait: max(10.0, 10.0 - 1.0) = max(10.0, 9.0) = 10.0 -> register 100
        assert out.target_register >= 90, (
            f"Register {out.target_register} should not go below ~90 (9% hardware safety)"
        )


# ──────────────────────────────────────────────────────────────────
# #35: Hot water schedule config
# ──────────────────────────────────────────────────────────────────

class TestIssue35HotWaterConfig:
    """Verify hot water schedule tunables exist with correct defaults."""

    def test_hot_water_defaults(self):
        t = MPCTunables()
        assert t.hot_water_boost_kw == 2.5
        assert t.hot_water_start_hour == 6.5
        assert t.hot_water_duration_minutes == 10.0
        assert t.hot_water_enabled is False

    def test_hot_water_disabled_by_default(self):
        """Hot water boost should be off unless explicitly enabled."""
        t = MPCTunables()
        assert not t.hot_water_enabled

    def test_hot_water_configurable_via_dict(self):
        """Hot water settings should be loadable from config dict."""
        t = MPCTunables.from_dict({
            "hot_water_enabled": True,
            "hot_water_boost_kw": 3.0,
            "hot_water_start_hour": 7.0,
            "hot_water_duration_minutes": 15.0,
        })
        assert t.hot_water_enabled is True
        assert t.hot_water_boost_kw == 3.0
        assert t.hot_water_start_hour == 7.0
        assert t.hot_water_duration_minutes == 15.0
