"""Tests for R2706 feed-in register safety bounds (#53).

R2706 must only ever be written with values 0 (block export) or 70 (7kW).
The Quattro 48/8000 is 8kVA (~7.3kW) so 70 already saturates the hardware.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from custom_components.victron_mpc.const import (
    REGISTER_FEEDIN_MAX,
    REGISTER_FEEDIN_BLOCK,
)


class TestFeedinConstants:
    """Verify the allowed R2706 values are correct."""

    def test_feedin_max_is_70(self):
        assert REGISTER_FEEDIN_MAX == 70

    def test_feedin_block_is_0(self):
        assert REGISTER_FEEDIN_BLOCK == 0


class TestComputeFeedinValue:
    """Test _compute_feedin_value returns only {0, 70}."""

    @pytest.fixture
    def coordinator(self):
        """Minimal coordinator mock with _compute_feedin_value accessible."""
        from custom_components.victron_mpc.coordinator import VictronMPCCoordinator
        # Access the unbound method directly
        return VictronMPCCoordinator._compute_feedin_value

    def _make_coord_stub(self, *, feedin_threshold=0.10, soc_threshold=30.0):
        """Create a minimal object with tunables for _compute_feedin_value."""
        tunables = MagicMock()
        tunables.feedin_export_threshold = feedin_threshold
        tunables.feedin_soc_threshold = soc_threshold
        stub = MagicMock()
        stub._tunables = tunables
        return stub

    def test_negative_buy_returns_70(self, coordinator):
        stub = self._make_coord_stub()
        result = coordinator(stub, buy_price=-0.10, sell_price=0.05,
                             mode="hold", soc_pct=50, is_spike=False)
        assert result == 70

    def test_grid_charge_returns_70(self, coordinator):
        stub = self._make_coord_stub()
        result = coordinator(stub, buy_price=0.20, sell_price=0.05,
                             mode="grid_charge", soc_pct=50, is_spike=False)
        assert result == 70

    def test_spike_high_fit_returns_70(self, coordinator):
        stub = self._make_coord_stub()
        result = coordinator(stub, buy_price=1.00, sell_price=0.50,
                             mode="hold", soc_pct=60, is_spike=True)
        assert result == 70

    def test_battery_full_positive_fit_returns_70(self, coordinator):
        stub = self._make_coord_stub()
        result = coordinator(stub, buy_price=0.20, sell_price=0.05,
                             mode="hold", soc_pct=98, is_spike=False)
        assert result == 70

    def test_normal_conditions_returns_0(self, coordinator):
        stub = self._make_coord_stub()
        result = coordinator(stub, buy_price=0.20, sell_price=0.05,
                             mode="hold", soc_pct=50, is_spike=False)
        assert result == 0

    def test_all_paths_produce_allowed_values(self, coordinator):
        """Exhaustive check: every combination produces 0 or 70."""
        allowed = {REGISTER_FEEDIN_BLOCK, REGISTER_FEEDIN_MAX}
        cases = [
            dict(buy_price=0.20, sell_price=0.05, mode="hold", soc_pct=50, is_spike=False),
            dict(buy_price=0.20, sell_price=0.05, mode="grid_charge", soc_pct=50, is_spike=False),
            dict(buy_price=-0.10, sell_price=0.05, mode="hold", soc_pct=50, is_spike=False),
            dict(buy_price=0.20, sell_price=-0.05, mode="hold", soc_pct=50, is_spike=False),
            dict(buy_price=0.20, sell_price=0.50, mode="hold", soc_pct=60, is_spike=True),
            dict(buy_price=0.20, sell_price=0.05, mode="hold", soc_pct=98, is_spike=False),
            dict(buy_price=0.20, sell_price=0.00, mode="hold", soc_pct=98, is_spike=False),
            dict(buy_price=0.20, sell_price=0.05, mode="discharge", soc_pct=30, is_spike=False),
            dict(buy_price=0.20, sell_price=0.05, mode="solar_charge", soc_pct=50, is_spike=False),
        ]
        stub = self._make_coord_stub()
        for kwargs in cases:
            result = coordinator(stub, **kwargs)
            assert result in allowed, f"R2706={result} for {kwargs}"


class TestWriteFeedinValidation:
    """Test that _write_feedin_register rejects invalid values."""

    @pytest.mark.asyncio
    async def test_rejects_value_100(self):
        """Value 100 (old grid_charge value) must be rejected."""
        from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

        coord = MagicMock(spec=VictronMPCCoordinator)
        coord._last_feedin_value = None
        coord.hass = MagicMock()
        coord.hass.services.async_call = AsyncMock()
        coord.entry = MagicMock()
        coord.entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}
        coord._modbus_write_success = AsyncMock()
        coord._modbus_write_failure = AsyncMock()

        # Call the real method with invalid value
        await VictronMPCCoordinator._write_feedin_register(coord, 100)

        # Should NOT have written 100 — should default to 0
        if coord.hass.services.async_call.called:
            written = coord.hass.services.async_call.call_args[1].get(
                "service_data", coord.hass.services.async_call.call_args[0][2] if len(coord.hass.services.async_call.call_args[0]) > 2 else {}
            )
            assert written.get("value", 0) != 100

    @pytest.mark.asyncio
    async def test_accepts_value_70(self):
        """Value 70 must be accepted and written."""
        from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

        coord = MagicMock(spec=VictronMPCCoordinator)
        coord._last_feedin_value = None
        coord.hass = MagicMock()
        coord.hass.services.async_call = AsyncMock()
        coord.entry = MagicMock()
        coord.entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}
        coord._modbus_write_success = AsyncMock()

        await VictronMPCCoordinator._write_feedin_register(coord, 70)

        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_accepts_value_0(self):
        """Value 0 must be accepted and written."""
        from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

        coord = MagicMock(spec=VictronMPCCoordinator)
        coord._last_feedin_value = None
        coord.hass = MagicMock()
        coord.hass.services.async_call = AsyncMock()
        coord.entry = MagicMock()
        coord.entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}
        coord._modbus_write_success = AsyncMock()

        await VictronMPCCoordinator._write_feedin_register(coord, 0)

        coord.hass.services.async_call.assert_called_once()
