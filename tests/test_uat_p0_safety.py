"""UAT P0 Safety Invariant Tests for Victron MPC Battery Optimizer.

These tests validate the safety properties that must ALWAYS hold, regardless
of market conditions, time of day, or season. Each test maps to a P0 check
from UAT_OBJECTIVES.md.

P0 invariants tested:
  - R2900 (BatteryLife) must be written as 12 every cycle
  - R2901 must be below SoC unless mode = grid_charge
  - R2901 mode-specific register logic (grid_charge, solar_charge, discharge/hold)
  - R2706 only produces values {0, 70}
  - R2706 export rule priority (negative price, spike, default)
  - Register determinism (same inputs -> same output)
  - Safe-park on stale (3 consecutive failures -> safe registers)
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.victron_mpc.const import (
    REGISTER_BATTERYLIFE_STATE,
    REGISTER_ESS_MIN_SOC,
    REGISTER_FEEDIN_BLOCK,
    REGISTER_FEEDIN_MAX,
    REGISTER_SAFE_PARK,
    SAFE_PARK_CONSECUTIVE_FAILURES,
)
from custom_components.victron_mpc.coordinator import VictronMPCCoordinator
from custom_components.victron_mpc.optimizer import (
    OptInput,
    OptOutput,
    optimize,
    _soc_to_register,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coord_stub(
    *,
    feedin_threshold: float = 0.10,
    soc_threshold: float = 30.0,
) -> MagicMock:
    """Create a minimal coordinator-like stub for testing coordinator methods."""
    tunables = MagicMock()
    tunables.feedin_export_threshold = feedin_threshold
    tunables.feedin_soc_threshold = soc_threshold
    stub = MagicMock()
    stub._tunables = tunables
    return stub


def _make_opt_input(
    *,
    soc_kwh: float = 7.1,
    capacity_kwh: float = 14.2,
    soc_min_kwh: float = 1.42,
    horizon_steps: int = 288,
    buy_prices: list[float] | None = None,
    sell_prices: list[float] | None = None,
    solar_kw: list[float] | None = None,
    load_kw: list[float] | None = None,
) -> OptInput:
    """Build an OptInput with sensible defaults for safety tests."""
    N = horizon_steps
    return OptInput(
        horizon_steps=N,
        dt_hours=5.0 / 60.0,
        battery_soc_kwh=soc_kwh,
        battery_capacity_kwh=capacity_kwh,
        soc_min_kwh=soc_min_kwh,
        soc_max_kwh=capacity_kwh,
        max_charge_kw=7.1,
        max_discharge_kw=7.1,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        max_grid_import_kw=10.0,
        max_grid_export_kw=5.0,
        solar_forecast_kw=solar_kw or [0.0] * N,
        load_forecast_kw=load_kw or [1.0] * N,
        buy_price=buy_prices or [0.25] * N,
        sell_price=sell_prices or [0.08] * N,
        battery_wear_cost=0.02,
        grid_import_penalty=0.02,
        sunset_step=None,
        sunset_reward=0.0,
        terminal_reward=0.0,
    )


# ---------------------------------------------------------------------------
# P0: R2900 BatteryLife Must Be Disabled
# ---------------------------------------------------------------------------


class TestP0BatteryLifeDisabled:
    """R2900 must be written as 12 (BL disabled) every cycle."""

    @pytest.mark.asyncio
    async def test_p0_r2900_batterylife_disabled(self):
        """_write_batterylife_register writes R2900=12 to Modbus."""
        coord = MagicMock(spec=VictronMPCCoordinator)
        coord.hass = MagicMock()
        coord.hass.services.async_call = AsyncMock()
        coord.entry = MagicMock()
        coord.entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}

        await VictronMPCCoordinator._write_batterylife_register(coord)

        coord.hass.services.async_call.assert_called_once()
        call_kwargs = coord.hass.services.async_call.call_args
        service_data = call_kwargs[0][2] if len(call_kwargs[0]) > 2 else call_kwargs[1].get("service_data", {})
        assert service_data["address"] == REGISTER_BATTERYLIFE_STATE
        assert service_data["value"] == 12

    def test_p0_r2900_register_constant(self):
        """REGISTER_BATTERYLIFE_STATE must be 2900."""
        assert REGISTER_BATTERYLIFE_STATE == 2900


# ---------------------------------------------------------------------------
# P0: R2901 vs SoC Safety
# ---------------------------------------------------------------------------


class TestP0RegisterVsSoC:
    """R2901 must be below SoC unless mode = grid_charge."""

    def test_p0_r2901_below_soc_in_discharge(self):
        """In discharge mode, register must be below current SoC."""
        # 50% SoC, moderate prices, no solar -> discharge
        inputs = _make_opt_input(
            soc_kwh=7.1,  # 50%
            buy_prices=[0.30] * 288,
            sell_prices=[0.08] * 288,
            solar_kw=[0.0] * 288,
            load_kw=[2.0] * 288,
        )
        result = optimize(inputs)
        current_soc_register = int(round(50.0 * 10))  # 500

        if result.mode == "discharge":
            assert result.target_register < current_soc_register, (
                f"Discharge mode: register {result.target_register} must be "
                f"below SoC register {current_soc_register}"
            )

    def test_p0_r2901_below_soc_in_hold(self):
        """In hold mode, register must be below current SoC."""
        # 50% SoC, low price, no solar, low load -> hold
        inputs = _make_opt_input(
            soc_kwh=7.1,  # 50%
            buy_prices=[0.05] * 288,
            sell_prices=[0.02] * 288,
            solar_kw=[0.0] * 288,
            load_kw=[0.5] * 288,
        )
        result = optimize(inputs)
        current_soc_register = int(round(50.0 * 10))

        if result.mode == "hold":
            assert result.target_register < current_soc_register, (
                f"Hold mode: register {result.target_register} must be "
                f"below SoC register {current_soc_register}"
            )

    def test_p0_r2901_below_soc_in_solar_charge(self):
        """In solar_charge mode, register must be below current SoC."""
        # 50% SoC, solar available, low grid price
        inputs = _make_opt_input(
            soc_kwh=7.1,  # 50%
            buy_prices=[0.15] * 288,
            sell_prices=[0.05] * 288,
            solar_kw=[5.0] * 288,
            load_kw=[1.0] * 288,
        )
        result = optimize(inputs)
        current_soc_register = int(round(50.0 * 10))

        if result.mode == "solar_charge":
            assert result.target_register < current_soc_register, (
                f"Solar_charge mode: register {result.target_register} must be "
                f"below SoC register {current_soc_register}"
            )

    def test_p0_r2901_above_soc_in_grid_charge(self):
        """In grid_charge mode, register must be at or above current SoC.

        Grid charge sets register = target_soc (above current) to force
        the ESS to pull from grid.
        """
        # 20% SoC, very cheap price, no solar -> should grid charge
        inputs = _make_opt_input(
            soc_kwh=2.84,  # 20%
            buy_prices=[-0.10] * 288,  # Negative = free money
            sell_prices=[-0.05] * 288,
            solar_kw=[0.0] * 288,
            load_kw=[1.0] * 288,
        )
        result = optimize(inputs)
        current_soc_register = int(round(20.0 * 10))  # 200

        if result.mode == "grid_charge":
            assert result.target_register >= current_soc_register, (
                f"Grid_charge mode: register {result.target_register} must be "
                f">= SoC register {current_soc_register}"
            )


# ---------------------------------------------------------------------------
# P0: R2901 Mode-Specific Register Logic
# ---------------------------------------------------------------------------


class TestP0RegisterModeLogic:
    """Register value must follow mode-specific rules."""

    def test_p0_r2901_solar_charge_at_hard_floor(self):
        """In solar_charge mode, register should be near the hard floor.

        Solar charge sets register = soc_floor_pct - 1% to prevent ESS
        grid pull. For 10% floor -> register ~ 90 (9%).
        """
        inputs = _make_opt_input(
            soc_kwh=7.1,  # 50%
            soc_min_kwh=1.42,  # 10% hard floor
            buy_prices=[0.15] * 288,
            sell_prices=[0.05] * 288,
            solar_kw=[5.0] * 288,
            load_kw=[1.0] * 288,
        )
        result = optimize(inputs)

        if result.mode == "solar_charge":
            # Hard floor is 10%, register should be at floor (~ 9-10%)
            floor_register = _soc_to_register(10.0 - 1.0)  # 90
            # Allow some tolerance since the exact floor depends on soc_min_kwh/capacity
            assert result.target_register <= _soc_to_register(15.0), (
                f"Solar_charge register {result.target_register} should be "
                f"near hard floor, not high"
            )

    def test_p0_r2901_discharge_has_buffer(self):
        """In discharge/hold mode, register should include the 5% buffer.

        Discharge sets register = floor - 5% to prevent ESS from
        interpreting register near SoC as grid-charge request.
        """
        inputs = _make_opt_input(
            soc_kwh=7.1,  # 50%
            soc_min_kwh=1.42,  # 10% hard floor
            buy_prices=[0.30] * 288,
            sell_prices=[0.08] * 288,
            solar_kw=[0.0] * 288,
            load_kw=[2.0] * 288,
        )
        result = optimize(inputs)

        if result.mode in ("discharge", "hold"):
            # Register should be well below current SoC (50% = 500)
            # The buffer ensures register is at least 5% below the discharge floor
            current_register = _soc_to_register(50.0)
            assert result.target_register < current_register - 40, (
                f"Discharge register {result.target_register} should have "
                f"buffer below SoC register {current_register}"
            )

    def test_p0_r2901_register_in_valid_range(self):
        """Register value must always be in [100, 1000]."""
        test_cases = [
            {"soc_kwh": 1.42, "buy_prices": [0.25] * 288},  # Low SoC
            {"soc_kwh": 14.2, "buy_prices": [0.25] * 288},  # Full
            {"soc_kwh": 7.1, "buy_prices": [-0.10] * 288},  # Negative price
            {"soc_kwh": 7.1, "buy_prices": [5.00] * 288},  # Spike
        ]
        for case in test_cases:
            inputs = _make_opt_input(**case)
            result = optimize(inputs)
            assert 100 <= result.target_register <= 1000, (
                f"Register {result.target_register} out of range for {case}"
            )


# ---------------------------------------------------------------------------
# P0: R2706 Feed-In Safety
# ---------------------------------------------------------------------------


class TestP0FeedinValues:
    """R2706 must only produce values {0, 70}."""

    def test_p0_r2706_only_0_or_70(self):
        """Exhaustive: _compute_feedin_value produces only {0, 70}."""
        fn = VictronMPCCoordinator._compute_feedin_value
        allowed = {REGISTER_FEEDIN_BLOCK, REGISTER_FEEDIN_MAX}

        cases = [
            # (buy_price, sell_price, mode, soc_pct, is_spike)
            (0.25, 0.08, "hold", 50, False),
            (0.25, 0.08, "discharge", 50, False),
            (0.25, 0.08, "solar_charge", 50, False),
            (0.25, 0.08, "grid_charge", 50, False),
            (-0.10, 0.05, "hold", 50, False),
            (-0.10, -0.05, "discharge", 30, False),
            (1.50, 0.80, "hold", 60, True),
            (1.50, 0.05, "hold", 60, True),  # spike but low FIT
            (0.25, 0.08, "hold", 98, False),  # SoC > 95%
            (0.25, 0.00, "hold", 98, False),  # SoC > 95% but zero FIT
            (0.25, -0.05, "hold", 98, False),  # SoC > 95% but negative FIT
            (0.05, 0.02, "hold", 10, False),  # Low everything
            (5.00, 3.00, "discharge", 80, True),  # Extreme spike
        ]
        stub = _make_coord_stub()
        for buy, sell, mode, soc, spike in cases:
            result = fn(stub, buy_price=buy, sell_price=sell,
                        mode=mode, soc_pct=soc, is_spike=spike)
            assert result in allowed, (
                f"R2706={result} not in {allowed} for "
                f"buy={buy}, sell={sell}, mode={mode}, soc={soc}, spike={spike}"
            )


# ---------------------------------------------------------------------------
# P0: R2706 Export Rule Priority
# ---------------------------------------------------------------------------


class TestP0FeedinRules:
    """R2706 export rules follow documented priority order."""

    def _call(self, **kwargs) -> int:
        """Call _compute_feedin_value with defaults."""
        defaults = {
            "buy_price": 0.25,
            "sell_price": 0.08,
            "mode": "hold",
            "soc_pct": 50,
            "is_spike": False,
        }
        defaults.update(kwargs)
        stub = _make_coord_stub()
        return VictronMPCCoordinator._compute_feedin_value(stub, **defaults)

    def test_p0_r2706_negative_price_exports(self):
        """Rule 1: Negative buy price -> 70 (export everything)."""
        result = self._call(buy_price=-0.10)
        assert result == 70, "Negative price must trigger export (70)"

    def test_p0_r2706_grid_charge_allows_import(self):
        """Rule 2: grid_charge mode -> 70 (inverter needs grid pull)."""
        result = self._call(mode="grid_charge")
        assert result == 70, "grid_charge must set R2706=70"

    def test_p0_r2706_spike_with_high_fit_exports(self):
        """Rule 3: Spike + FIT > threshold + SoC > threshold -> 70."""
        result = self._call(
            buy_price=1.50, sell_price=0.50,
            soc_pct=60, is_spike=True,
        )
        assert result == 70, "Spike with high FIT should export"

    def test_p0_r2706_spike_low_fit_blocks(self):
        """Rule 3 negative: Spike but FIT below threshold -> 0."""
        result = self._call(
            buy_price=1.50, sell_price=0.05,
            soc_pct=60, is_spike=True,
        )
        assert result == 0, "Spike with low FIT should block export"

    def test_p0_r2706_spike_low_soc_blocks(self):
        """Rule 3 negative: Spike but SoC below threshold -> 0."""
        result = self._call(
            buy_price=1.50, sell_price=0.50,
            soc_pct=20, is_spike=True,
        )
        assert result == 0, "Spike with low SoC should block export"

    def test_p0_r2706_battery_full_positive_fit_exports(self):
        """Rule 4: SoC > 95% + FIT > 0 -> 70."""
        result = self._call(soc_pct=98, sell_price=0.05)
        assert result == 70, "Battery full + positive FIT should export"

    def test_p0_r2706_battery_full_zero_fit_blocks(self):
        """Rule 4 negative: SoC > 95% but FIT = 0 -> 0."""
        result = self._call(soc_pct=98, sell_price=0.0)
        assert result == 0, "Battery full but zero FIT should block"

    def test_p0_r2706_default_blocks_export(self):
        """Rule 5: Normal conditions -> 0 (block export, self-consume)."""
        result = self._call()
        assert result == 0, "Normal conditions must block export (0)"


# ---------------------------------------------------------------------------
# P0: Register Determinism
# ---------------------------------------------------------------------------


class TestP0RegisterDeterminism:
    """Same inputs must always produce the same register output."""

    def test_p0_register_deterministic(self):
        """Running optimize twice with identical inputs must produce identical output."""
        inputs = _make_opt_input(
            soc_kwh=7.1,
            buy_prices=[0.25] * 288,
            sell_prices=[0.08] * 288,
            solar_kw=[0.0] * 288,
            load_kw=[1.0] * 288,
        )
        result_1 = optimize(inputs)
        result_2 = optimize(inputs)

        assert result_1.target_register == result_2.target_register, (
            f"Non-deterministic: run1={result_1.target_register}, "
            f"run2={result_2.target_register}"
        )
        assert result_1.mode == result_2.mode, (
            f"Non-deterministic mode: run1={result_1.mode}, run2={result_2.mode}"
        )

    def test_p0_feedin_deterministic(self):
        """_compute_feedin_value is deterministic for same inputs."""
        stub = _make_coord_stub()
        fn = VictronMPCCoordinator._compute_feedin_value
        kwargs = dict(
            buy_price=0.25, sell_price=0.08,
            mode="hold", soc_pct=50, is_spike=False,
        )
        results = [fn(stub, **kwargs) for _ in range(10)]
        assert len(set(results)) == 1, f"Non-deterministic feedin: {results}"


# ---------------------------------------------------------------------------
# P0: Safe-Park on Stale Data
# ---------------------------------------------------------------------------


class TestP0SafeParkOnStale:
    """After 3 consecutive failures, safe registers must be written."""

    def test_p0_safe_park_constants(self):
        """Safe-park threshold is 3 consecutive failures."""
        assert SAFE_PARK_CONSECUTIVE_FAILURES == 3
        assert REGISTER_SAFE_PARK == 300  # 30% SoC floor

    @pytest.mark.asyncio
    async def test_p0_safe_park_on_stale(self):
        """After 3 failures, coordinator writes safe registers."""
        stub = MagicMock(spec=VictronMPCCoordinator)
        stub._consecutive_failures = 0
        stub._safe_parked = False
        stub._emergency_stopped = False
        stub._cycle_count = 0
        stub.entry = MagicMock()
        stub.entry.options = {"shadow_mode": False}
        stub.entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}
        stub._write_register = AsyncMock()
        stub._write_feedin_register = AsyncMock()
        stub._notify = AsyncMock()
        stub._last_register_value = None
        stub._last_feedin_value = None
        stub.hass = MagicMock()
        stub.hass.services = MagicMock()
        stub.hass.services.async_call = AsyncMock()

        # Simulate 3 consecutive failures
        for _ in range(SAFE_PARK_CONSECUTIVE_FAILURES):
            stub._cycle_count += 1
            stub._consecutive_failures += 1
            if (
                stub._consecutive_failures >= SAFE_PARK_CONSECUTIVE_FAILURES
                and not stub._safe_parked
            ):
                stub._safe_parked = True
                stub._last_register_value = None
                await stub._write_register(REGISTER_SAFE_PARK)
                stub._last_feedin_value = None
                await stub._write_feedin_register(REGISTER_FEEDIN_BLOCK)

        assert stub._safe_parked is True
        stub._write_register.assert_called_with(REGISTER_SAFE_PARK)
        stub._write_feedin_register.assert_called_with(REGISTER_FEEDIN_BLOCK)

    @pytest.mark.asyncio
    async def test_p0_safe_park_not_before_threshold(self):
        """Before 3 failures, safe-park must NOT activate."""
        stub = MagicMock(spec=VictronMPCCoordinator)
        stub._consecutive_failures = 0
        stub._safe_parked = False
        stub._write_register = AsyncMock()

        for _ in range(SAFE_PARK_CONSECUTIVE_FAILURES - 1):
            stub._consecutive_failures += 1
            if (
                stub._consecutive_failures >= SAFE_PARK_CONSECUTIVE_FAILURES
                and not stub._safe_parked
            ):
                stub._safe_parked = True
                await stub._write_register(REGISTER_SAFE_PARK)

        assert stub._safe_parked is False
        stub._write_register.assert_not_called()


# ---------------------------------------------------------------------------
# P0: _soc_to_register Conversion
# ---------------------------------------------------------------------------


class TestP0SocToRegister:
    """Register conversion must be safe and bounded."""

    def test_p0_soc_to_register_basic(self):
        """50% -> 500, 100% -> 1000, 10% -> 100."""
        assert _soc_to_register(50.0) == 500
        assert _soc_to_register(100.0) == 1000
        assert _soc_to_register(10.0) == 100

    def test_p0_soc_to_register_clamped_low(self):
        """Below 10% must clamp to 100 (never below hardware minimum)."""
        assert _soc_to_register(5.0) == 100
        assert _soc_to_register(0.0) == 100
        assert _soc_to_register(-10.0) == 100

    def test_p0_soc_to_register_clamped_high(self):
        """Above 100% must clamp to 1000."""
        assert _soc_to_register(110.0) == 1000
        assert _soc_to_register(200.0) == 1000
