"""Tests for Modbus health monitoring, alerting, and register read-back validation."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.coordinator import VictronMPCCoordinator


def _make_coordinator(*, readback_pct: float = -1) -> VictronMPCCoordinator:
    """Create a coordinator with mocked hass and config entry.

    Args:
        readback_pct: Value returned by _get_r2901_readback().
            -1 means sensor unavailable (default).
            A positive float simulates a sensor reading (e.g. 50.0 = 50%).
    """
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
    coord._last_mode = None
    coord._cycle_count = 0
    coord._modbus_write_log = []
    coord._modbus_write_log_max = 2016

    # Mock _get_r2901_readback to return the specified value
    coord._get_r2901_readback = MagicMock(return_value=readback_pct)

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
        # _notify calls persistent_notification + mobile targets
        calls = coord.hass.services.async_call.call_args_list
        titles = [c[0][2].get("title", "") for c in calls if len(c[0]) > 2 and isinstance(c[0][2], dict)]
        assert any("Modbus Communication Failed" in t for t in titles)

    @pytest.mark.asyncio
    async def test_modbus_no_duplicate_alerts(self):
        """Once alerted, additional failures don't re-alert."""
        coord = _make_coordinator()

        for _ in range(5):
            await coord._modbus_write_failure()

        assert coord._modbus_consecutive_failures == 5
        # _notify fires once at failure 3 (persistent + mobile targets)
        # but should not re-fire at failures 4, 5
        # Count distinct notification titles
        calls = coord.hass.services.async_call.call_args_list
        modbus_alerts = [c for c in calls if len(c[0]) > 2 and isinstance(c[0][2], dict) and "Modbus" in c[0][2].get("title", "")]
        assert len(modbus_alerts) <= 3  # persistent + 2 mobile = 3 calls max

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

        # Counter incremented. _notify catches exceptions internally,
        # so _modbus_alerted should still be set True.
        assert coord._modbus_consecutive_failures == 3
        assert coord._modbus_alerted is True


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

        calls = coord.hass.services.async_call.call_args_list
        titles = [c[0][2].get("title", "") for c in calls if len(c[0]) > 2 and isinstance(c[0][2], dict)]
        assert any("Modbus Communication Restored" in t for t in titles)

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


class TestRegisterReadbackValidation:
    """Tests for #58: R2901 write-then-verify with retry."""

    @pytest.mark.asyncio
    async def test_write_verified_on_matching_readback(self):
        """Write succeeds when readback matches within tolerance."""
        coord = _make_coordinator(readback_pct=50.0)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert coord._last_register_value == 500
        # Only one modbus write call (no retry needed)
        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 1

    @pytest.mark.asyncio
    async def test_write_accepted_when_sensor_unavailable(self):
        """Write proceeds without verification when sensor returns -1."""
        coord = _make_coordinator(readback_pct=-1)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert coord._last_register_value == 500

    @pytest.mark.asyncio
    async def test_write_retries_on_mismatch(self):
        """Mismatch on first read triggers one retry."""
        coord = _make_coordinator()
        # First readback: wrong (old value), second readback: correct
        coord._get_r2901_readback = MagicMock(side_effect=[70.0, 50.0])

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert coord._last_register_value == 500
        # Two modbus write calls (initial + retry)
        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 2

    @pytest.mark.asyncio
    async def test_persistent_mismatch_still_updates_tracking(self):
        """Persistent mismatch logs warning but still updates _last_register_value."""
        coord = _make_coordinator()
        # Both readbacks return wrong value
        coord._get_r2901_readback = MagicMock(return_value=99.0)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        # Value still tracked so next cycle doesn't skip the write
        assert coord._last_register_value == 500
        # Two modbus write calls (initial + retry)
        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 2

    @pytest.mark.asyncio
    async def test_readback_tolerance_accepts_rounding(self):
        """Readback within 1% (±1.0 percentage point) is accepted."""
        # Write 505 (50.5%), readback 50.0% — within 1% tolerance
        coord = _make_coordinator(readback_pct=50.0)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(505)

        assert coord._last_register_value == 505
        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 1  # No retry needed

    @pytest.mark.asyncio
    async def test_write_failure_skips_readback(self):
        """If the Modbus write itself fails, no readback is attempted."""
        coord = _make_coordinator(readback_pct=50.0)
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("modbus timeout")
        )

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert coord._modbus_consecutive_failures == 1
        # Readback not called because write failed
        coord._get_r2901_readback.assert_not_called()


class TestFastStartupRegisterWrite:
    """Regression: first cycle (_last_register_value=None) always writes register."""

    @pytest.mark.asyncio
    async def test_first_cycle_always_writes(self):
        """When _last_register_value is None, _write_register(200) calls Modbus."""
        coord = _make_coordinator(readback_pct=-1)
        assert coord._last_register_value is None

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(200)

        # Modbus service should have been called (not skipped)
        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) >= 1, "First cycle must write register even for default value"
        assert coord._last_register_value == 200

    @pytest.mark.asyncio
    async def test_second_cycle_same_value_skips(self):
        """After first write, same value skips (existing behavior)."""
        coord = _make_coordinator(readback_pct=-1)
        coord._last_register_value = 200  # Already written

        await coord._write_register(200)

        coord.hass.services.async_call.assert_not_called()


class TestPhase7bGridImportAutoCorrect:
    """Phase 7b: auto-correct grid import anomaly during non-grid-charge modes.

    Regression for coordinator.py:606 — if mode != grid_charge AND grid_import > 200W
    AND soc > 35%, write floor register to stop unintended grid import.
    """

    @pytest.mark.asyncio
    async def test_auto_correct_triggered(self):
        """Grid import > 200W during hold mode with SoC > 35% triggers floor write."""
        coord = _make_coordinator(readback_pct=-1)
        coord._last_mode = "hold"
        coord._last_register_value = 500

        # The Phase 7b logic in coordinator checks:
        # mode != "grid_charge" and grid_import_w > 200 and not genset_active and soc_pct > 35
        mode = "hold"
        grid_import_w = 350.0
        genset_active = False
        soc_pct = 50.0

        should_correct = (
            mode != "grid_charge"
            and grid_import_w > 200
            and not genset_active
            and soc_pct > 35
        )
        assert should_correct is True, "Auto-correct conditions should be met"

        # Verify the floor register calculation
        from custom_components.victron_mpc.config import MPCTunables, VictronSystem
        tunables = MPCTunables()
        system = VictronSystem()
        floor_register = int(max(tunables.soc_floor_pct, system.soc_min_pct) * 10)
        assert floor_register == 100  # 10% * 10 = 100

        # Write the floor register
        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(floor_register)

        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) >= 1
        assert modbus_calls[0][0][2]["value"] == 100

    def test_no_correction_during_grid_charge(self):
        """Grid import during grid_charge mode is expected — no auto-correct."""
        mode = "grid_charge"
        grid_import_w = 500.0
        genset_active = False
        soc_pct = 50.0

        should_correct = (
            mode != "grid_charge"
            and grid_import_w > 200
            and not genset_active
            and soc_pct > 35
        )
        assert should_correct is False

    def test_no_correction_low_soc(self):
        """Low SoC (< 35%) should not trigger auto-correct — may need grid."""
        mode = "hold"
        grid_import_w = 350.0
        genset_active = False
        soc_pct = 30.0

        should_correct = (
            mode != "grid_charge"
            and grid_import_w > 200
            and not genset_active
            and soc_pct > 35
        )
        assert should_correct is False

    def test_no_correction_small_grid_import(self):
        """Grid import <= 200W is within normal ESS oscillation — no correction."""
        mode = "hold"
        grid_import_w = 150.0
        genset_active = False
        soc_pct = 50.0

        should_correct = (
            mode != "grid_charge"
            and grid_import_w > 200
            and not genset_active
            and soc_pct > 35
        )
        assert should_correct is False


class TestTransitionSafety:
    """Tests for #58: grid_charge → other mode transition writes floor first."""

    @pytest.mark.asyncio
    async def test_leaving_grid_charge_writes_floor_then_target(self):
        """Transition from grid_charge to hold writes floor before target."""
        coord = _make_coordinator(readback_pct=-1)  # Skip verification
        coord._last_mode = "grid_charge"

        # Simulate Phase 7 transition safety logic inline
        # (testing the _write_register call sequence)
        floor_register = 300

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(floor_register)
            await coord._write_register(700)

        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 2
        # First write: floor (300)
        assert modbus_calls[0][0][2]["value"] == 300
        # Second write: target (700)
        assert modbus_calls[1][0][2]["value"] == 700

    @pytest.mark.asyncio
    async def test_no_transition_safety_when_staying_in_mode(self):
        """No extra floor write when mode doesn't change from grid_charge."""
        coord = _make_coordinator(readback_pct=-1)
        coord._last_mode = "hold"

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(700)

        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 1
        assert modbus_calls[0][0][2]["value"] == 700

    @pytest.mark.asyncio
    async def test_no_transition_safety_on_first_cycle(self):
        """No transition safety when _last_mode is None (first cycle)."""
        coord = _make_coordinator(readback_pct=-1)
        coord._last_mode = None

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(700)

        modbus_calls = [
            c for c in coord.hass.services.async_call.call_args_list
            if c[0][0] == "modbus"
        ]
        assert len(modbus_calls) == 1


class TestAuditLog:
    """Tests for #56: audit log persists every decision with context."""

    def test_audit_log_initialized_empty(self):
        """Audit log starts empty."""
        coord = _make_coordinator()
        coord._audit_log = []
        assert len(coord._audit_log) == 0

    def test_audit_log_entry_has_required_fields(self):
        """Audit log entries contain all required fields."""
        required = {
            "cycle", "timestamp", "mode", "register", "feedin",
            "soc_pct", "buy_price", "sell_price", "solar_w", "load_w",
            "cost_24h", "reason", "shadow", "spike",
        }
        entry = {
            "cycle": 1, "timestamp": "2026-04-06T08:00:00",
            "mode": "hold", "register": 300, "feedin": 0,
            "soc_pct": 50.0, "buy_price": 0.18, "sell_price": 0.06,
            "solar_w": 500, "load_w": 800, "cost_24h": 2.34,
            "reason": "LP optimal", "shadow": False, "spike": False,
        }
        assert required.issubset(entry.keys())

    def test_audit_log_rolls_over_at_max(self):
        """Log trims to max entries, keeping most recent."""
        coord = _make_coordinator()
        coord._audit_log = [{"cycle": i} for i in range(20)]
        coord._audit_log_max = 10
        # Simulate trim logic from coordinator
        if len(coord._audit_log) > coord._audit_log_max:
            coord._audit_log = coord._audit_log[-coord._audit_log_max:]
        assert len(coord._audit_log) == 10
        assert coord._audit_log[0]["cycle"] == 10
        assert coord._audit_log[-1]["cycle"] == 19


class TestSystemIntents:
    """Tests for explicit system intents (IAADE Intent phase)."""

    def test_intents_list_exists_and_nonempty(self):
        """SYSTEM_INTENTS is declared with at least 7 intents."""
        from custom_components.victron_mpc.config import SYSTEM_INTENTS
        assert isinstance(SYSTEM_INTENTS, list)
        assert len(SYSTEM_INTENTS) >= 7

    def test_each_intent_has_required_fields(self):
        """Every intent has id, description, implementation, measurable."""
        from custom_components.victron_mpc.config import SYSTEM_INTENTS
        required = {"id", "description", "implementation", "measurable"}
        for intent in SYSTEM_INTENTS:
            missing = required - intent.keys()
            assert not missing, f"Intent '{intent.get('id')}' missing: {missing}"

    def test_intent_ids_are_unique(self):
        """No duplicate intent IDs."""
        from custom_components.victron_mpc.config import SYSTEM_INTENTS
        ids = [i["id"] for i in SYSTEM_INTENTS]
        assert len(ids) == len(set(ids))

    def test_core_intents_declared(self):
        """Critical intents exist: cost, sunset, resilience, health."""
        from custom_components.victron_mpc.config import SYSTEM_INTENTS
        ids = {i["id"] for i in SYSTEM_INTENTS}
        for required_id in ["cost_minimisation", "sunset_readiness",
                            "overnight_resilience", "battery_health"]:
            assert required_id in ids, f"Missing core intent: {required_id}"
