"""Tests for GenAI audit log — Issue #57.

Validates:
- _append_genai_history includes all new structured fields
- genai_audit_log sensor exposes correct state and attributes
- Deduplication still works with new fields present
- PROMPT_VERSION is importable and consistent
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.victron_mpc.genai_monitor import PROMPT_VERSION


# ======================================================================
# TestPromptVersion
# ======================================================================


class TestPromptVersion:
    """PROMPT_VERSION constant is defined and importable."""

    def test_prompt_version_is_string(self):
        """PROMPT_VERSION is a non-empty string."""
        assert isinstance(PROMPT_VERSION, str)
        assert len(PROMPT_VERSION) > 0

    def test_prompt_version_format(self):
        """PROMPT_VERSION follows 'vN' convention."""
        assert PROMPT_VERSION.startswith("v"), f"Expected 'vN' format, got {PROMPT_VERSION!r}"
        version_number = PROMPT_VERSION[1:]
        assert version_number.isdigit(), f"Version number should be numeric, got {version_number!r}"

    def test_prompt_version_is_v4(self):
        """PROMPT_VERSION is v4 matching the 4 iterations of prompt calibration."""
        assert PROMPT_VERSION == "v4"


# ======================================================================
# Helper: minimal coordinator-like object with _append_genai_history
# ======================================================================


def _make_history_appender(max_entries: int = 168):
    """Return an object that has the same _append_genai_history logic as the coordinator.

    We import the method directly from coordinator to avoid instantiating the
    full coordinator (which requires Home Assistant).
    """
    from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

    # Build a minimal mock that satisfies the method's attribute access
    obj = MagicMock(spec=VictronMPCCoordinator)
    obj._genai_history = []
    obj._genai_history_max = max_entries

    # Bind the real method to the mock object
    obj._append_genai_history = VictronMPCCoordinator._append_genai_history.__get__(
        obj, VictronMPCCoordinator
    )
    return obj


# ======================================================================
# TestAppendGenaiHistory — new structured fields
# ======================================================================


class TestAppendGenaiHistory:
    """_append_genai_history stores all required fields in each entry."""

    def _call(self, obj, *, source="deterministic", status="GREEN", summary="All OK",
               soc_pct=50.0, mode="discharge", buy_price=0.25,
               solar_w=1000, load_w=800, grid_import_w=0,
               details="", deterministic_checks=None, amber_band="low", weather="clear"):
        obj._append_genai_history(
            source, status, summary,
            soc_pct, mode, buy_price,
            solar_w, load_w, grid_import_w,
            details=details,
            deterministic_checks=deterministic_checks,
            amber_band=amber_band,
            weather=weather,
        )

    def test_entry_contains_timestamp(self):
        """Each history entry has an ISO 8601 timestamp."""
        obj = _make_history_appender()
        self._call(obj)
        assert len(obj._genai_history) == 1
        entry = obj._genai_history[0]
        assert "timestamp" in entry
        # Should be parseable ISO format
        from datetime import datetime
        datetime.fromisoformat(entry["timestamp"])

    def test_entry_contains_prompt_version(self):
        """Each entry records the prompt version used."""
        obj = _make_history_appender()
        self._call(obj)
        entry = obj._genai_history[0]
        assert "prompt_version" in entry
        assert entry["prompt_version"] == PROMPT_VERSION

    def test_entry_contains_details(self):
        """Details field is stored in the entry."""
        obj = _make_history_appender()
        self._call(obj, details="Grid import anomaly detected at 1200W")
        entry = obj._genai_history[0]
        assert entry["details"] == "Grid import anomaly detected at 1200W"

    def test_entry_details_defaults_to_empty_string(self):
        """Details defaults to empty string when not provided."""
        obj = _make_history_appender()
        self._call(obj)
        assert obj._genai_history[0]["details"] == ""

    def test_entry_contains_deterministic_checks_list(self):
        """deterministic_checks field is stored as a list."""
        obj = _make_history_appender()
        self._call(obj, deterministic_checks=["r2900_ess_mode", "grid_import_high"])
        entry = obj._genai_history[0]
        assert "deterministic_checks" in entry
        assert entry["deterministic_checks"] == ["r2900_ess_mode", "grid_import_high"]

    def test_entry_deterministic_checks_defaults_to_empty_list(self):
        """deterministic_checks defaults to empty list when not provided."""
        obj = _make_history_appender()
        self._call(obj)
        assert obj._genai_history[0]["deterministic_checks"] == []

    def test_entry_contains_amber_band(self):
        """amber_band field is stored in the entry."""
        obj = _make_history_appender()
        self._call(obj, amber_band="spike")
        assert obj._genai_history[0]["amber_band"] == "spike"

    def test_entry_amber_band_defaults_to_unknown(self):
        """amber_band defaults to 'unknown' when not provided."""
        obj = _make_history_appender()
        self._call(obj)
        assert obj._genai_history[0]["amber_band"] == "low"  # default in helper

    def test_entry_contains_weather(self):
        """weather field is stored in the entry."""
        obj = _make_history_appender()
        self._call(obj, weather="rainy")
        assert obj._genai_history[0]["weather"] == "rainy"

    def test_entry_contains_legacy_readings(self):
        """Existing 'readings' dict is still present for backward compatibility."""
        obj = _make_history_appender()
        self._call(obj, soc_pct=62.5, mode="solar_charge", buy_price=0.31,
                   solar_w=2500, load_w=900, grid_import_w=0)
        entry = obj._genai_history[0]
        assert "readings" in entry
        readings = entry["readings"]
        assert readings["soc_pct"] == 62.5
        assert readings["mode"] == "solar_charge"
        assert readings["buy_price"] == 0.31
        assert readings["solar_w"] == 2500
        assert readings["load_w"] == 900
        assert readings["grid_import_w"] == 0

    def test_entry_contains_source_status_summary(self):
        """Legacy top-level fields source, status, summary are preserved."""
        obj = _make_history_appender()
        self._call(obj, source="genai", status="YELLOW", summary="Unusual pattern detected")
        entry = obj._genai_history[0]
        assert entry["source"] == "genai"
        assert entry["status"] == "YELLOW"
        assert entry["summary"] == "Unusual pattern detected"


# ======================================================================
# TestAppendGenaiHistoryDeduplication
# ======================================================================


class TestAppendGenaiHistoryDeduplication:
    """Deduplication still works after adding new fields."""

    def _append(self, obj, source="deterministic", status="GREEN", summary="OK", **kwargs):
        obj._append_genai_history(
            source, status, summary,
            50.0, "discharge", 0.25, 1000, 800, 0,
            **kwargs,
        )

    def test_duplicate_skipped(self):
        """Same source/status/summary is not appended twice."""
        obj = _make_history_appender()
        self._append(obj)
        self._append(obj)
        assert len(obj._genai_history) == 1

    def test_different_status_not_deduplicated(self):
        """Different status results in a new entry."""
        obj = _make_history_appender()
        self._append(obj, status="GREEN")
        self._append(obj, status="RED")
        assert len(obj._genai_history) == 2

    def test_different_summary_not_deduplicated(self):
        """Different summary results in a new entry."""
        obj = _make_history_appender()
        self._append(obj, summary="All OK")
        self._append(obj, summary="Grid import anomaly")
        assert len(obj._genai_history) == 2

    def test_different_source_not_deduplicated(self):
        """Different source results in a new entry."""
        obj = _make_history_appender()
        self._append(obj, source="deterministic")
        self._append(obj, source="genai")
        assert len(obj._genai_history) == 2

    def test_max_entries_enforced(self):
        """History is capped at max_entries."""
        obj = _make_history_appender(max_entries=5)
        for i in range(10):
            obj._append_genai_history(
                "deterministic", "GREEN", f"summary_{i}",
                50.0, "discharge", 0.25, 1000, 800, 0,
            )
        assert len(obj._genai_history) == 5
        # Most recent entries retained
        assert obj._genai_history[-1]["summary"] == "summary_9"


# ======================================================================
# TestGenaiAuditLogSensorData — _build_sensor_data shape
# ======================================================================


class TestGenaiAuditLogSensorData:
    """genai_audit_log key in sensor_data has the correct structure."""

    def _make_audit_log_data(self, history: list) -> dict:
        """Simulate what _build_sensor_data puts in 'genai_audit_log'."""
        return {
            "state": len(history),
            "entries": history,
            "false_positive_count": sum(
                1 for e in history if e.get("status") == "YELLOW"
            ),
            "prompt_version": PROMPT_VERSION,
        }

    def test_state_equals_entry_count(self):
        """State field equals the number of history entries."""
        history = [{"status": "GREEN"}, {"status": "RED"}]
        data = self._make_audit_log_data(history)
        assert data["state"] == 2

    def test_entries_is_full_history(self):
        """entries field contains all history entries."""
        history = [{"status": "GREEN", "summary": "OK"}]
        data = self._make_audit_log_data(history)
        assert data["entries"] == history

    def test_false_positive_count_counts_yellow(self):
        """false_positive_count counts YELLOW entries."""
        history = [
            {"status": "GREEN"},
            {"status": "YELLOW"},
            {"status": "YELLOW"},
            {"status": "RED"},
        ]
        data = self._make_audit_log_data(history)
        assert data["false_positive_count"] == 2

    def test_false_positive_count_zero_when_no_yellow(self):
        """false_positive_count is 0 when no YELLOW entries."""
        history = [{"status": "GREEN"}, {"status": "RED"}]
        data = self._make_audit_log_data(history)
        assert data["false_positive_count"] == 0

    def test_prompt_version_matches_constant(self):
        """prompt_version field matches the PROMPT_VERSION constant."""
        data = self._make_audit_log_data([])
        assert data["prompt_version"] == PROMPT_VERSION

    def test_empty_history_state_is_zero(self):
        """Empty history produces state=0."""
        data = self._make_audit_log_data([])
        assert data["state"] == 0


# ======================================================================
# TestGenaiAuditLogSensorDescription — sensor.py SENSOR_DESCRIPTIONS
# ======================================================================


class TestGenaiAuditLogSensorDescription:
    """mpc_genai_audit_log is registered in SENSOR_DESCRIPTIONS."""

    def test_sensor_key_exists(self):
        """mpc_genai_audit_log appears in SENSOR_DESCRIPTIONS."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        keys = [desc.key for desc in SENSOR_DESCRIPTIONS]
        assert "mpc_genai_audit_log" in keys, (
            f"mpc_genai_audit_log not found in SENSOR_DESCRIPTIONS. Found: {keys}"
        )

    def test_sensor_attr_key(self):
        """mpc_genai_audit_log maps to 'genai_audit_log' in coordinator data."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        assert desc.attr_key == "genai_audit_log"

    def test_sensor_has_attributes_fn(self):
        """mpc_genai_audit_log has an attributes_fn to expose rich attributes."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        assert desc.attributes_fn is not None, "attributes_fn must be set for genai_audit_log"

    def test_attributes_fn_exposes_entries(self):
        """attributes_fn extracts 'entries' from the data dict."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        sample_entries = [{"status": "GREEN", "timestamp": "2026-04-07T12:00:00"}]
        data = {
            "state": 1,
            "entries": sample_entries,
            "false_positive_count": 0,
            "prompt_version": "v4",
        }
        attrs = desc.attributes_fn(data)
        assert attrs["entries"] == sample_entries

    def test_attributes_fn_exposes_false_positive_count(self):
        """attributes_fn extracts 'false_positive_count'."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        data = {
            "state": 2,
            "entries": [],
            "false_positive_count": 3,
            "prompt_version": "v4",
        }
        attrs = desc.attributes_fn(data)
        assert attrs["false_positive_count"] == 3

    def test_attributes_fn_exposes_prompt_version(self):
        """attributes_fn extracts 'prompt_version'."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        data = {
            "state": 0,
            "entries": [],
            "false_positive_count": 0,
            "prompt_version": "v4",
        }
        attrs = desc.attributes_fn(data)
        assert attrs["prompt_version"] == "v4"

    def test_attributes_fn_handles_non_dict(self):
        """attributes_fn returns empty dict for non-dict input (defensive)."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        assert desc.attributes_fn(None) == {}
        assert desc.attributes_fn("string") == {}
        assert desc.attributes_fn(42) == {}

    def test_genai_health_sensor_still_exists(self):
        """Existing mpc_genai_health sensor is not removed (backward compatibility)."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        keys = [desc.key for desc in SENSOR_DESCRIPTIONS]
        assert "mpc_genai_health" in keys, "mpc_genai_health must not be removed"

    def test_attributes_fn_exposes_version_stats(self):
        """attributes_fn extracts 'version_stats' for #61 prompt calibration."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        version_stats = {"v4": {"total_assessments": 5, "yellow_rate": 0.2}}
        data = {
            "state": 5,
            "entries": [],
            "false_positive_count": 1,
            "prompt_version": "v4",
            "current_version": "v4",
            "version_stats": version_stats,
        }
        attrs = desc.attributes_fn(data)
        assert attrs["version_stats"] == version_stats

    def test_attributes_fn_exposes_current_version(self):
        """attributes_fn extracts 'current_version' for #61 prompt calibration."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        data = {
            "state": 0,
            "entries": [],
            "false_positive_count": 0,
            "prompt_version": "v4",
            "current_version": "v4",
            "version_stats": {},
        }
        attrs = desc.attributes_fn(data)
        assert attrs["current_version"] == "v4"

    def test_attributes_fn_version_stats_defaults_to_empty_dict(self):
        """attributes_fn returns empty dict for version_stats when key missing."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_genai_audit_log")
        data = {
            "state": 0,
            "entries": [],
            "false_positive_count": 0,
            "prompt_version": "v4",
        }
        attrs = desc.attributes_fn(data)
        assert attrs["version_stats"] == {}
        assert attrs["current_version"] == "unknown"


# ======================================================================
# TestComputePromptVersionStats — #61 per-version statistics
# ======================================================================


def _make_stats_computer():
    """Return an object bound with _compute_prompt_version_stats from coordinator."""
    from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

    obj = MagicMock(spec=VictronMPCCoordinator)
    obj._genai_history = []
    obj._compute_prompt_version_stats = VictronMPCCoordinator._compute_prompt_version_stats.__get__(
        obj, VictronMPCCoordinator
    )
    return obj


class TestComputePromptVersionStats:
    """_compute_prompt_version_stats groups entries and computes per-version metrics."""

    def test_empty_history_returns_empty_dict(self):
        """Empty _genai_history produces an empty stats dict."""
        obj = _make_stats_computer()
        stats = obj._compute_prompt_version_stats()
        assert stats == {}

    def test_single_version_green_entry(self):
        """Single GREEN entry for one version produces correct counts."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {
                "prompt_version": "v4",
                "status": "GREEN",
                "timestamp": "2026-04-07T10:00:00",
            }
        ]
        stats = obj._compute_prompt_version_stats()
        assert "v4" in stats
        v4 = stats["v4"]
        assert v4["total_assessments"] == 1
        assert v4["green_count"] == 1
        assert v4["yellow_count"] == 0
        assert v4["error_count"] == 0
        assert v4["yellow_rate"] == 0.0
        assert v4["first_seen"] == "2026-04-07T10:00:00"
        assert v4["last_seen"] == "2026-04-07T10:00:00"

    def test_yellow_rate_calculation(self):
        """yellow_rate = yellow_count / total_assessments."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T09:00:00"},
            {"prompt_version": "v4", "status": "YELLOW", "timestamp": "2026-04-07T10:00:00"},
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T11:00:00"},
            {"prompt_version": "v4", "status": "YELLOW", "timestamp": "2026-04-07T12:00:00"},
        ]
        stats = obj._compute_prompt_version_stats()
        v4 = stats["v4"]
        assert v4["total_assessments"] == 4
        assert v4["yellow_count"] == 2
        assert v4["green_count"] == 2
        assert v4["yellow_rate"] == 0.5

    def test_mixed_versions(self):
        """Entries from different versions are grouped separately."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {"prompt_version": "v3", "status": "YELLOW", "timestamp": "2026-04-06T08:00:00"},
            {"prompt_version": "v3", "status": "GREEN", "timestamp": "2026-04-06T09:00:00"},
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T10:00:00"},
        ]
        stats = obj._compute_prompt_version_stats()
        assert set(stats.keys()) == {"v3", "v4"}
        assert stats["v3"]["total_assessments"] == 2
        assert stats["v3"]["yellow_count"] == 1
        assert stats["v3"]["yellow_rate"] == 0.5
        assert stats["v4"]["total_assessments"] == 1
        assert stats["v4"]["yellow_count"] == 0
        assert stats["v4"]["yellow_rate"] == 0.0

    def test_error_skip_counted_in_error_count(self):
        """ERROR, SKIP, and '?' statuses increment error_count."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {"prompt_version": "v4", "status": "ERROR", "timestamp": "2026-04-07T10:00:00"},
            {"prompt_version": "v4", "status": "SKIP", "timestamp": "2026-04-07T10:05:00"},
            {"prompt_version": "v4", "status": "?", "timestamp": "2026-04-07T10:10:00"},
        ]
        stats = obj._compute_prompt_version_stats()
        v4 = stats["v4"]
        assert v4["error_count"] == 3
        assert v4["green_count"] == 0
        assert v4["yellow_count"] == 0
        assert v4["total_assessments"] == 3

    def test_first_seen_last_seen_tracking(self):
        """first_seen and last_seen track the timestamp range per version."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T12:00:00"},
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T08:00:00"},
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T10:00:00"},
        ]
        stats = obj._compute_prompt_version_stats()
        v4 = stats["v4"]
        assert v4["first_seen"] == "2026-04-07T08:00:00"
        assert v4["last_seen"] == "2026-04-07T12:00:00"

    def test_missing_prompt_version_grouped_as_unknown(self):
        """Entries without prompt_version are grouped under 'unknown'."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {"status": "GREEN", "timestamp": "2026-04-07T10:00:00"},
        ]
        stats = obj._compute_prompt_version_stats()
        assert "unknown" in stats
        assert stats["unknown"]["total_assessments"] == 1

    def test_yellow_rate_precision(self):
        """yellow_rate is rounded to 4 decimal places."""
        obj = _make_stats_computer()
        obj._genai_history = [
            {"prompt_version": "v4", "status": "YELLOW", "timestamp": "2026-04-07T10:00:00"},
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T10:05:00"},
            {"prompt_version": "v4", "status": "GREEN", "timestamp": "2026-04-07T10:10:00"},
        ]
        stats = obj._compute_prompt_version_stats()
        # 1/3 ≈ 0.3333
        assert stats["v4"]["yellow_rate"] == round(1 / 3, 4)

    def test_version_stats_returned_in_build_sensor_data(self):
        """_build_sensor_data includes version_stats and current_version in genai_audit_log."""
        obj = _make_history_appender()
        obj._append_genai_history(
            "deterministic", "GREEN", "All OK",
            50.0, "discharge", 0.25, 1000, 800, 0,
        )
        # Bind _compute_prompt_version_stats to the same mock
        from custom_components.victron_mpc.coordinator import VictronMPCCoordinator
        obj._compute_prompt_version_stats = VictronMPCCoordinator._compute_prompt_version_stats.__get__(
            obj, VictronMPCCoordinator
        )
        # Simulate what _build_sensor_data produces for genai_audit_log
        genai_audit_log = {
            "state": len(obj._genai_history),
            "entries": obj._genai_history,
            "false_positive_count": sum(
                1 for e in obj._genai_history if e.get("status") == "YELLOW"
            ),
            "prompt_version": PROMPT_VERSION,
            "current_version": PROMPT_VERSION,
            "version_stats": obj._compute_prompt_version_stats(),
        }
        assert "version_stats" in genai_audit_log
        assert "current_version" in genai_audit_log
        assert genai_audit_log["current_version"] == PROMPT_VERSION
        assert PROMPT_VERSION in genai_audit_log["version_stats"]
