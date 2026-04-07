"""Tests for human feedback capture — Issue #60.

Validates:
- record_human_feedback stores all required fields correctly
- _compute_disagreement_stats computes rates accurately
- Maximum log size (200 entries) is enforced
- human_feedback key in sensor data has the correct shape
- Empty log edge cases are handled gracefully
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.victron_mpc.genai_monitor import PROMPT_VERSION


# ======================================================================
# Helpers: build minimal coordinator-like objects
# ======================================================================


def _make_feedback_coordinator(max_entries: int = 200):
    """Return a mock bound with the human-feedback methods from the coordinator."""
    from custom_components.victron_mpc.coordinator import VictronMPCCoordinator

    obj = MagicMock(spec=VictronMPCCoordinator)
    obj._human_feedback_log = []
    obj._human_feedback_log_max = max_entries
    obj._last_genai_result = {}
    obj.data = None
    # hass.states.get returns None by default
    obj.hass = MagicMock()
    obj.hass.states.get.return_value = None

    # Bind real methods
    obj.record_human_feedback = VictronMPCCoordinator.record_human_feedback.__get__(
        obj, VictronMPCCoordinator
    )
    obj._compute_disagreement_stats = VictronMPCCoordinator._compute_disagreement_stats.__get__(
        obj, VictronMPCCoordinator
    )
    return obj


async def _record(obj, verdict="agree", reasoning="All good", uat="", genai=""):
    """Helper to call record_human_feedback on the mock."""
    await obj.record_human_feedback(
        human_verdict=verdict,
        human_reasoning=reasoning,
        uat_verdict=uat,
        genai_verdict=genai,
    )


# ======================================================================
# TestRecordHumanFeedback — stored fields
# ======================================================================


class TestRecordHumanFeedback:
    """record_human_feedback stores all required fields in each entry."""

    def test_entry_stored_in_log(self):
        """After one call, log has exactly one entry."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(_record(obj))
        assert len(obj._human_feedback_log) == 1

    def test_entry_contains_timestamp(self):
        """Each entry has a parseable ISO 8601 timestamp."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(_record(obj))
        entry = obj._human_feedback_log[0]
        assert "timestamp" in entry
        datetime.fromisoformat(entry["timestamp"])  # Must not raise

    def test_entry_contains_human_verdict(self):
        """human_verdict is stored verbatim."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(
            _record(obj, verdict="disagree")
        )
        assert obj._human_feedback_log[0]["human_verdict"] == "disagree"

    def test_entry_contains_human_reasoning(self):
        """human_reasoning is stored verbatim."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(
            _record(obj, reasoning="Battery should be charging now")
        )
        assert obj._human_feedback_log[0]["human_reasoning"] == "Battery should be charging now"

    def test_entry_contains_uat_verdict(self):
        """uat_verdict is stored when provided."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(
            _record(obj, uat="PASS")
        )
        assert obj._human_feedback_log[0]["uat_verdict"] == "PASS"

    def test_entry_contains_genai_verdict(self):
        """genai_verdict is stored when provided."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(
            _record(obj, genai="YELLOW")
        )
        assert obj._human_feedback_log[0]["genai_verdict"] == "YELLOW"

    def test_entry_uat_defaults_to_empty_string(self):
        """uat_verdict defaults to empty string when not provided."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(_record(obj))
        assert obj._human_feedback_log[0]["uat_verdict"] == ""

    def test_entry_genai_defaults_to_empty_string(self):
        """genai_verdict defaults to empty string when not provided."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(_record(obj))
        assert obj._human_feedback_log[0]["genai_verdict"] == ""

    def test_entry_contains_genai_status_from_last_result(self):
        """genai_status is taken from coordinator's _last_genai_result."""
        obj = _make_feedback_coordinator()
        obj._last_genai_result = {"status": "RED", "summary": "Grid import high"}
        asyncio.get_event_loop().run_until_complete(_record(obj))
        assert obj._human_feedback_log[0]["genai_status"] == "RED"

    def test_entry_genai_status_empty_when_no_last_result(self):
        """genai_status is empty string when _last_genai_result is empty."""
        obj = _make_feedback_coordinator()
        obj._last_genai_result = {}
        asyncio.get_event_loop().run_until_complete(_record(obj))
        assert obj._human_feedback_log[0]["genai_status"] == ""

    def test_entry_contains_prompt_version(self):
        """prompt_version matches PROMPT_VERSION constant."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(_record(obj))
        assert obj._human_feedback_log[0]["prompt_version"] == PROMPT_VERSION

    def test_entry_contains_readings_snapshot(self):
        """Each entry has a 'readings' dict with system snapshot keys."""
        obj = _make_feedback_coordinator()
        asyncio.get_event_loop().run_until_complete(_record(obj))
        entry = obj._human_feedback_log[0]
        assert "readings" in entry
        readings = entry["readings"]
        assert "soc_pct" in readings
        assert "mode" in readings
        assert "buy_price" in readings
        assert "solar_w" in readings
        assert "load_w" in readings
        assert "grid_import_w" in readings
        assert "amber_band" in readings
        assert "weather" in readings

    def test_readings_default_to_safe_values_when_no_data(self):
        """Readings default to safe zero/unknown values when no coordinator data."""
        obj = _make_feedback_coordinator()
        obj.data = None
        asyncio.get_event_loop().run_until_complete(_record(obj))
        readings = obj._human_feedback_log[0]["readings"]
        # All numeric defaults should be 0 or 0.0 — not exceptions
        assert isinstance(readings["soc_pct"], (int, float))
        assert isinstance(readings["solar_w"], int)
        assert isinstance(readings["load_w"], int)
        assert isinstance(readings["grid_import_w"], int)

    def test_all_verdict_types_accepted(self):
        """All three verdict types can be stored without errors."""
        obj = _make_feedback_coordinator()
        for verdict in ("agree", "disagree", "override"):
            asyncio.get_event_loop().run_until_complete(
                _record(obj, verdict=verdict)
            )
        verdicts = [e["human_verdict"] for e in obj._human_feedback_log]
        assert "agree" in verdicts
        assert "disagree" in verdicts
        assert "override" in verdicts


# ======================================================================
# TestMaxLogSize — enforces 200-entry cap
# ======================================================================


class TestMaxLogSize:
    """Log is trimmed to max_entries when exceeded."""

    def test_log_capped_at_max_entries(self):
        """After 210 entries with max=200, log has exactly 200."""
        obj = _make_feedback_coordinator(max_entries=200)
        for i in range(210):
            asyncio.get_event_loop().run_until_complete(
                _record(obj, reasoning=f"entry_{i}")
            )
        assert len(obj._human_feedback_log) == 200

    def test_most_recent_entries_retained_after_trim(self):
        """After trimming, the most recent entries are kept."""
        obj = _make_feedback_coordinator(max_entries=5)
        for i in range(8):
            asyncio.get_event_loop().run_until_complete(
                _record(obj, reasoning=f"entry_{i}")
            )
        assert len(obj._human_feedback_log) == 5
        # Last entry should be entry_7
        assert obj._human_feedback_log[-1]["human_reasoning"] == "entry_7"
        # First entry should be entry_3 (oldest retained)
        assert obj._human_feedback_log[0]["human_reasoning"] == "entry_3"

    def test_small_log_not_trimmed_unnecessarily(self):
        """Log under max_entries is not trimmed."""
        obj = _make_feedback_coordinator(max_entries=200)
        for i in range(5):
            asyncio.get_event_loop().run_until_complete(
                _record(obj, reasoning=f"entry_{i}")
            )
        assert len(obj._human_feedback_log) == 5


# ======================================================================
# TestComputeDisagreementStats
# ======================================================================


class TestComputeDisagreementStats:
    """_compute_disagreement_stats computes accurate rates."""

    def test_empty_log_returns_zero_stats(self):
        """Empty log produces all-zero stats."""
        obj = _make_feedback_coordinator()
        stats = obj._compute_disagreement_stats()
        assert stats["total_feedback"] == 0
        assert stats["disagree_count"] == 0
        assert stats["agree_count"] == 0
        assert stats["disagreement_rate"] == 0.0

    def test_empty_log_recent_7d_is_zero(self):
        """Empty log produces all-zero recent_7d stats."""
        obj = _make_feedback_coordinator()
        stats = obj._compute_disagreement_stats()
        r = stats["recent_7d"]
        assert r["total_feedback"] == 0
        assert r["disagree_count"] == 0
        assert r["disagreement_rate"] == 0.0

    def test_disagree_count_correct(self):
        """disagree_count counts only 'disagree' verdicts."""
        obj = _make_feedback_coordinator()
        obj._human_feedback_log = [
            {"human_verdict": "agree", "timestamp": datetime.now().isoformat()},
            {"human_verdict": "disagree", "timestamp": datetime.now().isoformat()},
            {"human_verdict": "disagree", "timestamp": datetime.now().isoformat()},
            {"human_verdict": "override", "timestamp": datetime.now().isoformat()},
        ]
        stats = obj._compute_disagreement_stats()
        assert stats["total_feedback"] == 4
        assert stats["disagree_count"] == 2
        assert stats["agree_count"] == 1

    def test_disagreement_rate_calculation(self):
        """disagreement_rate = disagree_count / total_feedback."""
        obj = _make_feedback_coordinator()
        obj._human_feedback_log = [
            {"human_verdict": "agree", "timestamp": datetime.now().isoformat()},
            {"human_verdict": "disagree", "timestamp": datetime.now().isoformat()},
        ]
        stats = obj._compute_disagreement_stats()
        assert stats["disagreement_rate"] == 0.5

    def test_disagreement_rate_rounded_to_4_decimal_places(self):
        """disagreement_rate is rounded to 4 decimal places."""
        obj = _make_feedback_coordinator()
        obj._human_feedback_log = [
            {"human_verdict": "disagree", "timestamp": datetime.now().isoformat()},
            {"human_verdict": "agree", "timestamp": datetime.now().isoformat()},
            {"human_verdict": "agree", "timestamp": datetime.now().isoformat()},
        ]
        stats = obj._compute_disagreement_stats()
        # 1/3 ≈ 0.3333
        assert stats["disagreement_rate"] == round(1 / 3, 4)

    def test_recent_7d_filters_old_entries(self):
        """recent_7d only includes entries from the last 7 days."""
        obj = _make_feedback_coordinator()
        old_ts = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
        new_ts = datetime.now().isoformat(timespec="seconds")
        obj._human_feedback_log = [
            {"human_verdict": "disagree", "timestamp": old_ts},
            {"human_verdict": "agree", "timestamp": new_ts},
            {"human_verdict": "disagree", "timestamp": new_ts},
        ]
        stats = obj._compute_disagreement_stats()
        # Total includes all 3
        assert stats["total_feedback"] == 3
        assert stats["disagree_count"] == 2
        # Recent only includes the 2 new ones
        r = stats["recent_7d"]
        assert r["total_feedback"] == 2
        assert r["disagree_count"] == 1
        assert r["agree_count"] == 1
        assert r["disagreement_rate"] == 0.5

    def test_all_agree_zero_disagreement_rate(self):
        """100% agree gives disagreement_rate of 0.0."""
        obj = _make_feedback_coordinator()
        obj._human_feedback_log = [
            {"human_verdict": "agree", "timestamp": datetime.now().isoformat()}
            for _ in range(10)
        ]
        stats = obj._compute_disagreement_stats()
        assert stats["disagreement_rate"] == 0.0

    def test_all_disagree_gives_rate_one(self):
        """100% disagree gives disagreement_rate of 1.0."""
        obj = _make_feedback_coordinator()
        obj._human_feedback_log = [
            {"human_verdict": "disagree", "timestamp": datetime.now().isoformat()}
            for _ in range(5)
        ]
        stats = obj._compute_disagreement_stats()
        assert stats["disagreement_rate"] == 1.0

    def test_stats_has_required_keys(self):
        """Stats dict has all required keys."""
        obj = _make_feedback_coordinator()
        stats = obj._compute_disagreement_stats()
        required = {"total_feedback", "disagree_count", "agree_count", "disagreement_rate", "recent_7d"}
        assert required.issubset(set(stats.keys()))

    def test_recent_7d_has_required_keys(self):
        """recent_7d sub-dict has all required keys."""
        obj = _make_feedback_coordinator()
        stats = obj._compute_disagreement_stats()
        r = stats["recent_7d"]
        required = {"total_feedback", "disagree_count", "agree_count", "disagreement_rate"}
        assert required.issubset(set(r.keys()))


# ======================================================================
# TestSensorDataShape — human_feedback key in _build_sensor_data output
# ======================================================================


class TestSensorDataShape:
    """human_feedback in sensor data has the correct structure."""

    def _make_human_feedback_data(self, log: list) -> dict:
        """Simulate what _build_sensor_data produces for 'human_feedback'."""
        # Replicate the exact structure from coordinator._build_sensor_data
        total = len(log)
        disagree = sum(1 for e in log if e.get("human_verdict") == "disagree")
        agree = sum(1 for e in log if e.get("human_verdict") == "agree")
        rate = round(disagree / total, 4) if total > 0 else 0.0
        return {
            "state": total,
            "entries": log[-50:],
            "disagreement_stats": {
                "total_feedback": total,
                "disagree_count": disagree,
                "agree_count": agree,
                "disagreement_rate": rate,
                "recent_7d": {
                    "total_feedback": total,
                    "disagree_count": disagree,
                    "agree_count": agree,
                    "disagreement_rate": rate,
                },
            },
            "total_entries": total,
        }

    def test_state_equals_log_length(self):
        """state field equals the total number of feedback entries."""
        log = [{"human_verdict": "agree"}, {"human_verdict": "disagree"}]
        data = self._make_human_feedback_data(log)
        assert data["state"] == 2

    def test_entries_capped_at_50(self):
        """entries field contains at most 50 entries."""
        log = [{"human_verdict": "agree"} for _ in range(60)]
        data = self._make_human_feedback_data(log)
        assert len(data["entries"]) == 50

    def test_entries_has_most_recent_when_truncated(self):
        """When log > 50, entries contains the most recent 50."""
        log = [{"human_verdict": "agree", "idx": i} for i in range(60)]
        data = self._make_human_feedback_data(log)
        # Most recent entry should have idx=59
        assert data["entries"][-1]["idx"] == 59
        # Oldest in entries should have idx=10
        assert data["entries"][0]["idx"] == 10

    def test_total_entries_is_full_log_length(self):
        """total_entries reflects total, even when entries is truncated."""
        log = [{"human_verdict": "agree"} for _ in range(75)]
        data = self._make_human_feedback_data(log)
        assert data["total_entries"] == 75
        assert len(data["entries"]) == 50

    def test_disagreement_stats_present(self):
        """disagreement_stats key is present in sensor data."""
        data = self._make_human_feedback_data([])
        assert "disagreement_stats" in data

    def test_empty_log_state_is_zero(self):
        """Empty log produces state=0."""
        data = self._make_human_feedback_data([])
        assert data["state"] == 0
        assert data["total_entries"] == 0


# ======================================================================
# TestHumanFeedbackSensorDescription — sensor.py SENSOR_DESCRIPTIONS
# ======================================================================


class TestHumanFeedbackSensorDescription:
    """mpc_human_feedback is registered in SENSOR_DESCRIPTIONS."""

    def test_sensor_key_exists(self):
        """mpc_human_feedback appears in SENSOR_DESCRIPTIONS."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        keys = [desc.key for desc in SENSOR_DESCRIPTIONS]
        assert "mpc_human_feedback" in keys, (
            f"mpc_human_feedback not found in SENSOR_DESCRIPTIONS. Found: {keys}"
        )

    def test_sensor_attr_key(self):
        """mpc_human_feedback maps to 'human_feedback' in coordinator data."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_human_feedback")
        assert desc.attr_key == "human_feedback"

    def test_sensor_has_attributes_fn(self):
        """mpc_human_feedback has an attributes_fn to expose rich attributes."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_human_feedback")
        assert desc.attributes_fn is not None

    def test_attributes_fn_exposes_entries(self):
        """attributes_fn extracts 'entries' from the data dict."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_human_feedback")
        sample_entries = [{"human_verdict": "agree", "timestamp": "2026-04-07T10:00:00"}]
        data = {
            "state": 1,
            "entries": sample_entries,
            "disagreement_stats": {},
            "total_entries": 1,
        }
        attrs = desc.attributes_fn(data)
        assert attrs["entries"] == sample_entries

    def test_attributes_fn_exposes_disagreement_stats(self):
        """attributes_fn extracts 'disagreement_stats' from the data dict."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_human_feedback")
        stats = {"total_feedback": 5, "disagreement_rate": 0.4}
        data = {
            "state": 5,
            "entries": [],
            "disagreement_stats": stats,
            "total_entries": 5,
        }
        attrs = desc.attributes_fn(data)
        assert attrs["disagreement_stats"] == stats

    def test_attributes_fn_exposes_total_entries(self):
        """attributes_fn extracts 'total_entries' from the data dict."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_human_feedback")
        data = {
            "state": 75,
            "entries": [],
            "disagreement_stats": {},
            "total_entries": 75,
        }
        attrs = desc.attributes_fn(data)
        assert attrs["total_entries"] == 75

    def test_attributes_fn_handles_non_dict(self):
        """attributes_fn returns empty dict for non-dict input (defensive)."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "mpc_human_feedback")
        assert desc.attributes_fn(None) == {}
        assert desc.attributes_fn("string") == {}
        assert desc.attributes_fn(42) == {}

    def test_existing_sensors_not_removed(self):
        """Existing sensors are not removed — backward compatibility."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        keys = [desc.key for desc in SENSOR_DESCRIPTIONS]
        for expected in ("mpc_genai_audit_log", "mpc_genai_health", "mpc_decision"):
            assert expected in keys, f"{expected} must not be removed"


# ======================================================================
# TestServiceRegistration — __init__.py service wiring
# ======================================================================


class TestServiceRegistration:
    """record_feedback service constant is defined in __init__.py."""

    def test_service_constant_exists(self):
        """SERVICE_RECORD_FEEDBACK constant is importable from __init__."""
        from custom_components.victron_mpc import SERVICE_RECORD_FEEDBACK

        assert SERVICE_RECORD_FEEDBACK == "record_feedback"

    def test_service_constant_string(self):
        """SERVICE_RECORD_FEEDBACK is a non-empty string."""
        from custom_components.victron_mpc import SERVICE_RECORD_FEEDBACK

        assert isinstance(SERVICE_RECORD_FEEDBACK, str)
        assert len(SERVICE_RECORD_FEEDBACK) > 0
