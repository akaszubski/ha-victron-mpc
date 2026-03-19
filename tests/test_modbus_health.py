"""Tests for Modbus health monitoring and alerting."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.coordinator import VictronMPCCoordinator


def _make_coordinator() -> VictronMPCCoordinator:
    """Create a coordinator with mocked hass and config entry."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test_entry_123"
    entry.data = {
        "modbus_hub": "cerbo",
        "modbus_slave_system": 100,
    }
    entry.options = {}

    with patch.object(VictronMPCCoordinator, "__init__", lambda self, *a, **kw: None):
        coord = VictronMPCCoordinator.__new__(VictronMPCCoordinator)

    # Set the attributes that __init__ would normally set
    coord.hass = hass
    coord.entry = entry
    coord._modbus_consecutive_failures = 0
    coord._modbus_last_success = None
    coord._modbus_alerted = False
    coord._last_register_value = None
    coord._last_feedin_value = None

    return coord


class TestModbusHealthProperty:
    """Tests for the modbus_healthy property."""

    def test_modbus_healthy_initially(self):
        """Zero failures means healthy."""
        coord = _make_coordinator()
        assert coord.modbus_healthy is True

    def test_modbus_healthy_with_1_failure(self):
        """One failure is still healthy."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 1
        assert coord.modbus_healthy is True

    def test_modbus_healthy_with_2_failures(self):
        """Two failures is still healthy (threshold is 3)."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 2
        assert coord.modbus_healthy is True

    def test_modbus_unhealthy_at_3_failures(self):
        """Three failures means unhealthy."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 3
        assert coord.modbus_healthy is False

    def test_modbus_unhealthy_above_3_failures(self):
        """More than three failures means unhealthy."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 10
        assert coord.modbus_healthy is False


class TestModbusFailureTracking:
    """Tests for failure counting and alerting."""

    @pytest.mark.asyncio
    async def test_modbus_failure_increments_counter(self):
        """Each failure increments the counter."""
        coord = _make_coordinator()
        assert coord._modbus_consecutive_failures == 0

        await coord._modbus_write_failure()
        assert coord._modbus_consecutive_failures == 1

        await coord._modbus_write_failure()
        assert coord._modbus_consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_modbus_no_alert_under_3_failures(self):
        """No notification sent for fewer than 3 failures."""
        coord = _make_coordinator()

        await coord._modbus_write_failure()
        await coord._modbus_write_failure()

        # Should not have called persistent_notification
        coord.hass.services.async_call.assert_not_called()
        assert coord._modbus_alerted is False

    @pytest.mark.asyncio
    async def test_modbus_3_failures_triggers_alert(self):
        """After 3 consecutive failures, a notification is sent."""
        coord = _make_coordinator()

        await coord._modbus_write_failure()
        await coord._modbus_write_failure()
        await coord._modbus_write_failure()

        assert coord._modbus_consecutive_failures == 3
        assert coord._modbus_alerted is True
        coord.hass.services.async_call.assert_called_once_with(
            "persistent_notification",
            "create",
            {
                "title": "MPC: Modbus Communication Failed",
                "message": (
                    "Cannot write to Victron Cerbo GX. "
                    "3 consecutive failures. "
                    "Registers are NOT being updated."
                ),
                "notification_id": "mpc_modbus_down",
            },
        )

    @pytest.mark.asyncio
    async def test_modbus_no_duplicate_alerts(self):
        """Once alerted, additional failures don't re-alert."""
        coord = _make_coordinator()

        for _ in range(5):
            await coord._modbus_write_failure()

        assert coord._modbus_consecutive_failures == 5
        # Only one notification call (at failure 3)
        assert coord.hass.services.async_call.call_count == 1

    @pytest.mark.asyncio
    async def test_modbus_alert_survives_notification_error(self):
        """If the notification service itself fails, we still track the alert."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("notification service down")
        )

        await coord._modbus_write_failure()
        await coord._modbus_write_failure()
        await coord._modbus_write_failure()

        # Counter incremented, but alerted stays False since the call failed
        assert coord._modbus_consecutive_failures == 3
        assert coord._modbus_alerted is False


class TestModbusRecovery:
    """Tests for recovery after failures."""

    @pytest.mark.asyncio
    async def test_modbus_recovery_clears_state(self):
        """Successful write after failures resets counter and alert flag."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 5
        coord._modbus_alerted = True

        await coord._modbus_write_success()

        assert coord._modbus_consecutive_failures == 0
        assert coord._modbus_alerted is False
        assert coord._modbus_last_success is not None

    @pytest.mark.asyncio
    async def test_modbus_recovery_sends_notification(self):
        """Recovery after alert sends a restored notification."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 5
        coord._modbus_alerted = True

        await coord._modbus_write_success()

        coord.hass.services.async_call.assert_called_once_with(
            "persistent_notification",
            "create",
            {
                "title": "MPC: Modbus Communication Restored",
                "message": (
                    "Victron Cerbo GX communication restored. "
                    "Registers updating normally."
                ),
                "notification_id": "mpc_modbus_down",
            },
        )

    @pytest.mark.asyncio
    async def test_modbus_recovery_no_notification_if_not_alerted(self):
        """Recovery without prior alert does not send notification."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 1
        coord._modbus_alerted = False

        await coord._modbus_write_success()

        coord.hass.services.async_call.assert_not_called()
        assert coord._modbus_consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_modbus_recovery_sets_last_success_time(self):
        """Successful write records the timestamp."""
        coord = _make_coordinator()
        assert coord._modbus_last_success is None

        before = datetime.now()
        await coord._modbus_write_success()
        after = datetime.now()

        assert coord._modbus_last_success is not None
        assert before <= coord._modbus_last_success <= after


class TestWriteRegisterIntegration:
    """Test that _write_register and _write_feedin_register call health tracking."""

    @pytest.mark.asyncio
    async def test_write_register_success_updates_health(self):
        """Successful R2901 write calls _modbus_write_success."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 2

        await coord._write_register(500)

        assert coord._modbus_consecutive_failures == 0
        assert coord._modbus_last_success is not None
        assert coord._last_register_value == 500

    @pytest.mark.asyncio
    async def test_write_register_failure_updates_health(self):
        """Failed R2901 write calls _modbus_write_failure."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("modbus timeout")
        )

        await coord._write_register(500)

        assert coord._modbus_consecutive_failures == 1
        assert coord._last_register_value is None  # Not updated on failure

    @pytest.mark.asyncio
    async def test_write_feedin_register_success_updates_health(self):
        """Successful R2706 write calls _modbus_write_success."""
        coord = _make_coordinator()
        coord._modbus_consecutive_failures = 2

        await coord._write_feedin_register(70)

        assert coord._modbus_consecutive_failures == 0
        assert coord._modbus_last_success is not None
        assert coord._last_feedin_value == 70

    @pytest.mark.asyncio
    async def test_write_feedin_register_failure_updates_health(self):
        """Failed R2706 write calls _modbus_write_failure."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("modbus timeout")
        )

        await coord._write_feedin_register(70)

        assert coord._modbus_consecutive_failures == 1
        assert coord._last_feedin_value is None  # Not updated on failure

    @pytest.mark.asyncio
    async def test_write_register_skips_unchanged(self):
        """No write if value unchanged — no health tracking call."""
        coord = _make_coordinator()
        coord._last_register_value = 500

        await coord._write_register(500)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_feedin_skips_unchanged(self):
        """No write if value unchanged — no health tracking call."""
        coord = _make_coordinator()
        coord._last_feedin_value = 70

        await coord._write_feedin_register(70)

        coord.hass.services.async_call.assert_not_called()
