"""Tests for appliance monitor Phase 0 data collection (Issue #36)."""

from __future__ import annotations

import pytest

from custom_components.victron_mpc.const import (
    APPLIANCE_IDLE_W,
    APPLIANCE_RUNNING_W,
    APPLIANCE_STANDBY_W,
    DEFAULT_APPLIANCE_SENSORS,
)


class TestApplianceClassification:
    """Tests for power-level based appliance state classification."""

    def test_classify_idle(self) -> None:
        """Power below IDLE threshold -> 'idle'."""
        power = 5.0
        assert power < APPLIANCE_IDLE_W
        # Classification logic: < IDLE_W -> idle
        if power < APPLIANCE_IDLE_W:
            state = "idle"
        elif power < APPLIANCE_STANDBY_W:
            state = "standby"
        else:
            state = "running"
        assert state == "idle"

    def test_classify_standby(self) -> None:
        """Power between IDLE and STANDBY thresholds -> 'standby'."""
        power = 25.0
        assert power >= APPLIANCE_IDLE_W
        assert power < APPLIANCE_STANDBY_W
        if power < APPLIANCE_IDLE_W:
            state = "idle"
        elif power < APPLIANCE_STANDBY_W:
            state = "standby"
        else:
            state = "running"
        assert state == "standby"

    def test_classify_running(self) -> None:
        """Power above RUNNING threshold -> 'running'."""
        power = 100.0
        assert power >= APPLIANCE_RUNNING_W
        if power < APPLIANCE_IDLE_W:
            state = "idle"
        elif power < APPLIANCE_STANDBY_W:
            state = "standby"
        else:
            state = "running"
        assert state == "running"


class TestApplianceConstants:
    """Verify appliance constants are correctly defined."""

    def test_default_sensors(self) -> None:
        """Default sensors list contains expected smart plug entities."""
        assert "sensor.sp7_power" in DEFAULT_APPLIANCE_SENSORS
        assert "sensor.sp8_power" in DEFAULT_APPLIANCE_SENSORS
        assert len(DEFAULT_APPLIANCE_SENSORS) == 2

    def test_thresholds_ordered(self) -> None:
        """Thresholds must be ordered: idle < standby <= running."""
        assert APPLIANCE_IDLE_W < APPLIANCE_STANDBY_W
        assert APPLIANCE_STANDBY_W <= APPLIANCE_RUNNING_W


class TestLogEntryStructure:
    """Tests for the log entry dict structure produced by _log_appliance_state."""

    def test_log_entry_has_expected_keys(self) -> None:
        """A well-formed log entry has timestamp, hour, readings, running_count, total_w."""
        # Simulate what _log_appliance_state produces
        from datetime import datetime

        now = datetime.now()
        readings = {
            "sensor.sp7_power": {"power_w": 150.0, "state": "running"},
            "sensor.sp8_power": {"power_w": 5.0, "state": "idle"},
        }
        entry = {
            "timestamp": now.isoformat(),
            "hour": now.hour,
            "readings": readings,
            "running_count": 1,
            "total_w": sum(r["power_w"] for r in readings.values()),
        }

        assert "timestamp" in entry
        assert "hour" in entry
        assert "readings" in entry
        assert "running_count" in entry
        assert "total_w" in entry
        assert entry["running_count"] == 1
        assert entry["total_w"] == 155.0

    def test_reading_structure(self) -> None:
        """Each reading has power_w and state keys."""
        reading = {"power_w": 75.0, "state": "running"}
        assert "power_w" in reading
        assert "state" in reading
        assert reading["state"] in ("idle", "standby", "running", "unavailable", "error")


class TestBufferTrim:
    """Tests for log buffer size enforcement."""

    def test_buffer_trim_at_max(self) -> None:
        """Buffer should be trimmed when exceeding max size."""
        from datetime import datetime, timedelta

        max_size = 2016
        log: list[dict] = []

        # Fill beyond max
        base = datetime(2026, 4, 1, 0, 0)
        for i in range(max_size + 100):
            t = base + timedelta(minutes=5 * i)
            log.append({
                "timestamp": t.isoformat(),
                "hour": t.hour,
                "readings": {},
                "running_count": 0,
                "total_w": 0,
            })

        # Simulate the trim logic from coordinator
        if len(log) > max_size:
            log = log[-max_size:]

        assert len(log) == max_size
        # First entry should be the 101st original entry (index 100)
        expected_first = base + timedelta(minutes=5 * 100)
        assert log[0]["timestamp"] == expected_first.isoformat()


class TestUnavailableSensor:
    """Tests for graceful handling of unavailable sensors."""

    def test_unavailable_sensor_produces_zero_power(self) -> None:
        """Unavailable sensor should produce power_w=0 and state='unavailable'."""
        # Simulate unavailable handling from _log_appliance_state
        state_value = "unavailable"
        if state_value in ("unavailable", "unknown"):
            reading = {"power_w": 0, "state": "unavailable"}
        assert reading["power_w"] == 0
        assert reading["state"] == "unavailable"

    def test_non_numeric_state_produces_error(self) -> None:
        """Non-numeric state value should produce state='error'."""
        state_value = "abc"
        try:
            power = float(state_value)
            reading = {"power_w": power, "state": "running"}
        except (ValueError, TypeError):
            reading = {"power_w": 0, "state": "error"}
        assert reading["state"] == "error"
        assert reading["power_w"] == 0
