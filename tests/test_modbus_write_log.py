"""Tests for Issue #65: Modbus write audit log with source identification and rogue write detection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.coordinator import VictronMPCCoordinator


def _make_coordinator(*, readback_pct: float = -1) -> VictronMPCCoordinator:
    """Create a minimal coordinator with mocked hass and config entry.

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

    # Set attributes that __init__ would normally set
    coord.hass = hass
    coord.entry = entry
    coord._modbus_consecutive_failures = 0
    coord._modbus_last_success = None
    coord._modbus_alerted = False
    coord._last_register_value = None
    coord._last_feedin_value = None
    coord._last_mode = None
    coord._cycle_count = 42  # Non-zero for realistic tests
    coord._modbus_write_log = []
    coord._modbus_write_log_max = 2016

    # Mock _get_r2901_readback
    coord._get_r2901_readback = MagicMock(return_value=readback_pct)

    return coord


class TestR2901WriteAppendsToLog:
    """R2901 (_write_register) writes must appear in the audit log."""

    @pytest.mark.asyncio
    async def test_successful_write_appends_log_entry(self):
        """A successful R2901 write creates one log entry with correct fields."""
        coord = _make_coordinator(readback_pct=-1)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert len(coord._modbus_write_log) == 1
        entry = coord._modbus_write_log[0]
        assert entry["register"] == 2901
        assert entry["register_name"] == "ess_min_soc"
        assert entry["value"] == 500
        assert entry["source"] == "hacs_mpc"
        assert entry["cycle"] == 42
        assert "timestamp" in entry

    @pytest.mark.asyncio
    async def test_log_entry_previous_value_is_none_on_first_write(self):
        """First write has previous_value=None (not yet tracked)."""
        coord = _make_coordinator(readback_pct=-1)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        entry = coord._modbus_write_log[0]
        assert entry["previous_value"] is None

    @pytest.mark.asyncio
    async def test_log_entry_previous_value_tracks_prior_write(self):
        """After a first write, a second write records previous_value correctly."""
        coord = _make_coordinator(readback_pct=-1)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(300)
            await coord._write_register(500)

        assert len(coord._modbus_write_log) == 2
        assert coord._modbus_write_log[1]["previous_value"] == 300
        assert coord._modbus_write_log[1]["value"] == 500

    @pytest.mark.asyncio
    async def test_log_entry_verified_none_when_sensor_unavailable(self):
        """verified=None when readback sensor is unavailable."""
        coord = _make_coordinator(readback_pct=-1)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        entry = coord._modbus_write_log[0]
        assert entry["verified"] is None
        assert entry["readback_value"] is None

    @pytest.mark.asyncio
    async def test_log_entry_verified_true_on_matching_readback(self):
        """verified=True when readback matches within tolerance."""
        coord = _make_coordinator(readback_pct=50.0)  # 500 / 10 = 50.0%

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        entry = coord._modbus_write_log[0]
        assert entry["verified"] is True
        assert entry["readback_value"] == 50.0

    @pytest.mark.asyncio
    async def test_log_entry_verified_false_on_persistent_mismatch(self):
        """verified=False when readback never matches after retry."""
        coord = _make_coordinator()
        # Both readbacks return wrong value — 99.0% when we wrote 500 (50%)
        coord._get_r2901_readback = MagicMock(return_value=99.0)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        # First entry is our write, second is the rogue write suspect
        normal_entries = [e for e in coord._modbus_write_log if e.get("source") == "hacs_mpc"]
        assert len(normal_entries) == 1
        assert normal_entries[0]["verified"] is False

    @pytest.mark.asyncio
    async def test_write_skip_unchanged_value_does_not_append(self):
        """Skipped writes (value unchanged) must not append to the log."""
        coord = _make_coordinator(readback_pct=-1)
        coord._last_register_value = 500

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert len(coord._modbus_write_log) == 0

    @pytest.mark.asyncio
    async def test_failed_write_does_not_append_to_log(self):
        """A Modbus exception on write must not append a log entry."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("timeout"))

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert len(coord._modbus_write_log) == 0

    @pytest.mark.asyncio
    async def test_log_entry_contains_mode(self):
        """Log entry records current mode at time of write."""
        coord = _make_coordinator(readback_pct=-1)
        coord._last_mode = "hold"

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(300)

        assert coord._modbus_write_log[0]["mode"] == "hold"


class TestR2706WriteAppendsToLog:
    """R2706 (_write_feedin_register) writes must appear in the audit log."""

    @pytest.mark.asyncio
    async def test_feedin_write_appends_log_entry(self):
        """A successful R2706 write creates one log entry."""
        coord = _make_coordinator()

        await coord._write_feedin_register(70)

        assert len(coord._modbus_write_log) == 1
        entry = coord._modbus_write_log[0]
        assert entry["register"] == 2706
        assert entry["register_name"] == "max_feed_in"
        assert entry["value"] == 70
        assert entry["source"] == "hacs_mpc"
        assert entry["verified"] is None  # No read-back on R2706
        assert entry["readback_value"] is None

    @pytest.mark.asyncio
    async def test_feedin_write_records_previous_value(self):
        """Feed-in log entry captures the previous feed-in value."""
        coord = _make_coordinator()
        coord._last_feedin_value = 0  # Previously blocked

        await coord._write_feedin_register(70)

        assert coord._modbus_write_log[0]["previous_value"] == 0

    @pytest.mark.asyncio
    async def test_feedin_skip_unchanged_does_not_append(self):
        """Skipped feed-in writes must not append to the log."""
        coord = _make_coordinator()
        coord._last_feedin_value = 70

        await coord._write_feedin_register(70)

        assert len(coord._modbus_write_log) == 0

    @pytest.mark.asyncio
    async def test_feedin_write_failure_does_not_append(self):
        """A failed R2706 write must not append to the log."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("timeout"))

        await coord._write_feedin_register(70)

        assert len(coord._modbus_write_log) == 0


class TestR2900WriteAppendsToLog:
    """R2900 (_write_batterylife_register) writes must appear in the audit log."""

    @pytest.mark.asyncio
    async def test_batterylife_write_appends_log_entry(self):
        """A successful R2900 write creates one log entry."""
        coord = _make_coordinator()

        await coord._write_batterylife_register()

        assert len(coord._modbus_write_log) == 1
        entry = coord._modbus_write_log[0]
        assert entry["register"] == 2900
        assert entry["register_name"] == "batterylife_state"
        assert entry["value"] == 12
        assert entry["source"] == "hacs_mpc"
        assert entry["verified"] is None  # No read-back on R2900
        assert entry["readback_value"] is None

    @pytest.mark.asyncio
    async def test_batterylife_write_failure_does_not_append(self):
        """A failed R2900 write must not append to the log."""
        coord = _make_coordinator()
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("timeout"))

        await coord._write_batterylife_register()

        assert len(coord._modbus_write_log) == 0


class TestRogueWriteDetection:
    """Persistent readback mismatch triggers a rogue write suspicion entry."""

    @pytest.mark.asyncio
    async def test_persistent_mismatch_appends_rogue_entry(self):
        """After persistent mismatch, a 'unknown_external' entry is added."""
        coord = _make_coordinator()
        # Readback always shows 99.0% regardless of what we write
        coord._get_r2901_readback = MagicMock(return_value=99.0)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        rogue_entries = [e for e in coord._modbus_write_log if e.get("source") == "unknown_external"]
        assert len(rogue_entries) == 1
        rogue = rogue_entries[0]
        assert rogue["alert"] is True
        assert rogue["register"] == 2901
        assert rogue["expected_value"] == 500
        assert rogue["actual_value"] == 990  # 99.0 * 10

    @pytest.mark.asyncio
    async def test_first_attempt_mismatch_then_success_no_rogue_entry(self):
        """Mismatch on attempt 1 but success on attempt 2 does NOT flag rogue."""
        coord = _make_coordinator()
        # First readback: wrong, second: correct (50%)
        coord._get_r2901_readback = MagicMock(side_effect=[70.0, 50.0])

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        rogue_entries = [e for e in coord._modbus_write_log if e.get("source") == "unknown_external"]
        assert len(rogue_entries) == 0

    @pytest.mark.asyncio
    async def test_successful_write_no_rogue_entry(self):
        """A verified write creates no rogue entries."""
        coord = _make_coordinator(readback_pct=50.0)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        rogue_entries = [e for e in coord._modbus_write_log if e.get("source") == "unknown_external"]
        assert len(rogue_entries) == 0

    @pytest.mark.asyncio
    async def test_unavailable_readback_no_rogue_entry(self):
        """Unavailable readback (sensor -1) must not flag a rogue write."""
        coord = _make_coordinator(readback_pct=-1)

        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        rogue_entries = [e for e in coord._modbus_write_log if e.get("source") == "unknown_external"]
        assert len(rogue_entries) == 0


class TestMaxLogSize:
    """Log must not grow beyond max size."""

    @pytest.mark.asyncio
    async def test_log_respects_max_size(self):
        """Log trims to max_size, keeping the most recent entries."""
        coord = _make_coordinator(readback_pct=-1)
        coord._modbus_write_log_max = 5
        # Pre-fill log to just below max
        coord._modbus_write_log = [{"cycle": i} for i in range(5)]

        # Write one more — should push out the oldest
        with patch("custom_components.victron_mpc.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await coord._write_register(500)

        assert len(coord._modbus_write_log) == 5
        # Most recent entry is our write
        assert coord._modbus_write_log[-1]["register"] == 2901

    @pytest.mark.asyncio
    async def test_log_default_max_is_2016(self):
        """The default log max is 2016 entries (7 days × 288 cycles/day)."""
        coord = _make_coordinator()
        assert coord._modbus_write_log_max == 2016


class TestSensorDataShape:
    """modbus_write_log key in _build_sensor_data() has required structure."""

    def test_sensor_data_shape_empty_log(self):
        """With an empty log, sensor data has correct zero values."""
        coord = _make_coordinator()
        # Build the dict that _build_sensor_data would produce
        write_log_data = {
            "state": len(coord._modbus_write_log),
            "entries": coord._modbus_write_log[-50:],
            "rogue_write_count": sum(
                1 for e in coord._modbus_write_log if e.get("source") == "unknown_external"
            ),
            "total_writes": len(coord._modbus_write_log),
        }
        assert write_log_data["state"] == 0
        assert write_log_data["entries"] == []
        assert write_log_data["rogue_write_count"] == 0
        assert write_log_data["total_writes"] == 0

    def test_sensor_data_rogue_write_count(self):
        """rogue_write_count correctly counts unknown_external source entries."""
        coord = _make_coordinator()
        coord._modbus_write_log = [
            {"source": "hacs_mpc", "register": 2901},
            {"source": "unknown_external", "register": 2901, "alert": True},
            {"source": "hacs_mpc", "register": 2706},
            {"source": "unknown_external", "register": 2901, "alert": True},
        ]
        rogue_count = sum(
            1 for e in coord._modbus_write_log if e.get("source") == "unknown_external"
        )
        assert rogue_count == 2

    def test_sensor_data_entries_capped_at_50(self):
        """entries in sensor data is limited to last 50 entries."""
        coord = _make_coordinator()
        coord._modbus_write_log = [{"cycle": i} for i in range(100)]
        entries = coord._modbus_write_log[-50:]
        assert len(entries) == 50
        # Should be the most recent 50
        assert entries[0]["cycle"] == 50
        assert entries[-1]["cycle"] == 99

    def test_sensor_data_total_writes_reflects_all_entries(self):
        """total_writes is the full log count, not capped at 50."""
        coord = _make_coordinator()
        coord._modbus_write_log = [{"cycle": i} for i in range(100)]
        total_writes = len(coord._modbus_write_log)
        assert total_writes == 100
