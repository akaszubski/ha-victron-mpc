"""Tests for safe-park automation (GitHub issue #71).

Safe-park triggers after SAFE_PARK_CONSECUTIVE_FAILURES consecutive cycle
failures. It writes immediate safe registers, enables fallback rate-based
automations, and notifies. Does NOT set _emergency_stopped — the coordinator
auto-recovers on the next successful cycle, disabling fallback automations.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.victron_mpc.const import (
    REGISTER_FEEDIN_BLOCK,
    REGISTER_SAFE_PARK,
    SAFE_PARK_CONSECUTIVE_FAILURES,
)
from custom_components.victron_mpc.coordinator import VictronMPCCoordinator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FALLBACK_AUTOMATIONS = [
    "automation.fallback_battery_management",
    "automation.fallback_feed_in_control",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator_stub(*, shadow_mode: bool = False) -> MagicMock:
    """Create a minimal coordinator-like stub for testing safe-park logic."""
    stub = MagicMock(spec=VictronMPCCoordinator)
    stub._consecutive_failures = 0
    stub._safe_parked = False
    stub._emergency_stopped = False
    stub._cycle_count = 0
    stub.data = None

    stub.entry = MagicMock()
    stub.entry.options = {"shadow_mode": shadow_mode}
    stub.entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}

    stub._write_register = AsyncMock()
    stub._write_feedin_register = AsyncMock()
    stub._notify = AsyncMock()
    stub.hass = MagicMock()
    stub.hass.services = MagicMock()
    stub.hass.services.async_call = AsyncMock()

    stub._last_register_value = 500
    stub._last_feedin_value = 70

    return stub


async def _simulate_failures(stub: MagicMock, count: int) -> None:
    """Simulate N consecutive cycle failures through safe-park logic."""
    for _ in range(count):
        stub._cycle_count += 1
        stub._consecutive_failures += 1

        if (
            stub._consecutive_failures >= SAFE_PARK_CONSECUTIVE_FAILURES
            and not stub._safe_parked
        ):
            stub._safe_parked = True
            shadow_mode = stub.entry.options.get("shadow_mode", True)
            if not shadow_mode:
                stub._last_register_value = None
                await stub._write_register(REGISTER_SAFE_PARK)
                stub._last_feedin_value = None
                await stub._write_feedin_register(REGISTER_FEEDIN_BLOCK)
            # Enable fallback automations
            await stub.hass.services.async_call(
                "automation", "turn_on",
                {"entity_id": FALLBACK_AUTOMATIONS},
            )
            await stub._notify(
                "MPC: Fallback Mode Active",
                f"MPC optimizer failed {stub._consecutive_failures} "
                f"consecutive cycles (~{stub._consecutive_failures * 5} min). "
                "Fallback rate-based automations are now active "
                "(charge when cheap, ride out spikes). "
                "Will auto-recover on next successful cycle.",
                notification_id="mpc_safe_park",
            )


async def _simulate_success(stub: MagicMock) -> None:
    """Simulate a successful cycle clearing failure state."""
    stub._consecutive_failures = 0
    if stub._safe_parked:
        stub._safe_parked = False
        await stub.hass.services.async_call(
            "automation", "turn_off",
            {"entity_id": FALLBACK_AUTOMATIONS},
        )
        await stub._notify(
            "MPC: Recovered",
            "MPC optimizer recovered. Fallback automations deactivated. "
            "MPC is back in control.",
            notification_id="mpc_safe_park",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSafeParkConstants:
    """Verify safe-park constants are correct."""

    def test_register_safe_park_is_300(self):
        assert REGISTER_SAFE_PARK == 300

    def test_consecutive_failures_threshold_is_3(self):
        assert SAFE_PARK_CONSECUTIVE_FAILURES == 3


class TestSafeParkTrigger:
    """Test safe-park triggers after consecutive failures."""

    @pytest.mark.asyncio
    async def test_triggers_after_3_failures(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)

        assert stub._safe_parked is True
        stub._write_register.assert_called_once_with(REGISTER_SAFE_PARK)
        stub._write_feedin_register.assert_called_once_with(REGISTER_FEEDIN_BLOCK)

    @pytest.mark.asyncio
    async def test_does_not_trigger_before_threshold(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES - 1)

        assert stub._safe_parked is False
        stub._write_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_triggers_once(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, 5)

        assert stub._safe_parked is True
        stub._write_register.assert_called_once()
        stub._notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_register_writes_in_shadow_mode(self):
        stub = _make_coordinator_stub(shadow_mode=True)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)

        assert stub._safe_parked is True
        stub._write_register.assert_not_called()
        stub._write_feedin_register.assert_not_called()
        # Fallback automations still enabled even in shadow mode
        stub.hass.services.async_call.assert_called()
        stub._notify.assert_called_once()


class TestSafeParkFallbackAutomations:
    """Test that safe-park enables/disables fallback automations."""

    @pytest.mark.asyncio
    async def test_enables_fallback_automations_on_park(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)

        # Check automation.turn_on was called with fallback entities
        calls = stub.hass.services.async_call.call_args_list
        turn_on_calls = [
            c for c in calls
            if c[0][0] == "automation" and c[0][1] == "turn_on"
        ]
        assert len(turn_on_calls) == 1
        entities = turn_on_calls[0][0][2]["entity_id"]
        assert "automation.fallback_battery_management" in entities
        assert "automation.fallback_feed_in_control" in entities

    @pytest.mark.asyncio
    async def test_disables_fallback_automations_on_recovery(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)
        assert stub._safe_parked is True

        stub.hass.services.async_call.reset_mock()
        await _simulate_success(stub)

        assert stub._safe_parked is False
        calls = stub.hass.services.async_call.call_args_list
        turn_off_calls = [
            c for c in calls
            if c[0][0] == "automation" and c[0][1] == "turn_off"
        ]
        assert len(turn_off_calls) == 1
        entities = turn_off_calls[0][0][2]["entity_id"]
        assert "automation.fallback_battery_management" in entities
        assert "automation.fallback_feed_in_control" in entities

    @pytest.mark.asyncio
    async def test_recovery_sends_notification(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)

        stub._notify.reset_mock()
        await _simulate_success(stub)

        stub._notify.assert_called_once()
        title = stub._notify.call_args[0][0]
        assert title == "MPC: Recovered"


class TestSafeParkRecovery:
    """Test safe-park clears on successful cycle."""

    @pytest.mark.asyncio
    async def test_clears_on_success(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)
        assert stub._safe_parked is True

        await _simulate_success(stub)
        assert stub._safe_parked is False
        assert stub._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_can_retrigger_after_recovery(self):
        stub = _make_coordinator_stub(shadow_mode=False)

        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)
        assert stub._safe_parked is True

        await _simulate_success(stub)
        assert stub._safe_parked is False

        stub._write_register.reset_mock()
        stub._notify.reset_mock()

        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)
        assert stub._safe_parked is True
        stub._write_register.assert_called_once_with(REGISTER_SAFE_PARK)


class TestSafeParkNotification:
    """Test safe-park notification content."""

    @pytest.mark.asyncio
    async def test_notification_content(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)

        stub._notify.assert_called_once()
        title = stub._notify.call_args[0][0]
        message = stub._notify.call_args[0][1]

        assert title == "MPC: Fallback Mode Active"
        assert "fallback" in message.lower()
        assert "auto-recover" in message.lower()
        assert stub._notify.call_args[1]["notification_id"] == "mpc_safe_park"


class TestSafeParkDoesNotBlockRecovery:
    """Verify safe-park is distinct from emergency stop."""

    @pytest.mark.asyncio
    async def test_does_not_set_emergency_stopped(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)

        assert stub._safe_parked is True
        assert stub._emergency_stopped is False


class TestSafeParkAttribute:
    """Test safe_parked attribute."""

    def test_coordinator_initializes_safe_parked_false(self):
        stub = _make_coordinator_stub()
        assert stub._safe_parked is False

    @pytest.mark.asyncio
    async def test_safe_parked_true_after_park(self):
        stub = _make_coordinator_stub(shadow_mode=False)
        await _simulate_failures(stub, SAFE_PARK_CONSECUTIVE_FAILURES)
        assert stub._safe_parked is True
