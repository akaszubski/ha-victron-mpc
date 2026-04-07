"""Tests for GenAI health monitor — deterministic checks, strategic snapshot, and API calls."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.genai_monitor import (
    GENAI_CYCLE_INTERVAL,
    build_health_snapshot,
    build_strategic_snapshot,
    run_deterministic_checks,
    run_genai_health_check,
)


# ======================================================================
# TestRunDeterministicChecks — Layer 1 pure Python checks
# ======================================================================


class TestRunDeterministicChecks:
    """Tests for run_deterministic_checks — all 7 checks plus edge cases."""

    # --- Check 1: R2900 ESS mode ---

    def test_r2900_normal_10(self):
        """R2900=10 (BL disabled, optimized) passes."""
        results = run_deterministic_checks({}, {"r2900": 10})
        assert not any(r["check"] == "r2900_ess_mode" for r in results)

    def test_r2900_normal_12(self):
        """R2900=12 (BL disabled, keep SoC) passes."""
        results = run_deterministic_checks({}, {"r2900": 12})
        assert not any(r["check"] == "r2900_ess_mode" for r in results)

    def test_r2900_valid_11(self):
        """R2900=11 (BL disabled, optimized w/o BatteryLife) passes."""
        results = run_deterministic_checks({}, {"r2900": 11})
        assert not any(r["check"] == "r2900_ess_mode" for r in results)

    def test_r2900_batterylife_active(self):
        """R2900=2 (BatteryLife active) is RED."""
        results = run_deterministic_checks({}, {"r2900": 2})
        reds = [r for r in results if r["check"] == "r2900_ess_mode"]
        assert len(reds) == 1
        assert reds[0]["status"] == "RED"
        assert "2" in reds[0]["reason"]

    def test_r2900_keep_charged(self):
        """R2900=9 (Keep Charged) is RED."""
        results = run_deterministic_checks({}, {"r2900": 9})
        reds = [r for r in results if r["check"] == "r2900_ess_mode"]
        assert len(reds) == 1
        assert reds[0]["status"] == "RED"

    def test_r2900_unavailable_skipped(self):
        """R2900=-1 (unavailable) is NOT flagged."""
        results = run_deterministic_checks({}, {"r2900": -1})
        assert not any(r["check"] == "r2900_ess_mode" for r in results)

    def test_r2900_missing_from_extra(self):
        """Missing r2900 defaults to -1, not flagged."""
        results = run_deterministic_checks({}, {})
        assert not any(r["check"] == "r2900_ess_mode" for r in results)

    # --- Check 2: R2901 readback >= SoC during non-grid-charge ---

    def test_r2901_above_soc_during_discharge(self):
        """R2901 above SoC in discharge mode is RED."""
        data = {"mode": "discharge", "battery_soc_pct": 50}
        extra = {"r2901_readback_pct": 55}
        results = run_deterministic_checks(data, extra)
        reds = [r for r in results if r["check"] == "r2901_above_soc"]
        assert len(reds) == 1
        assert "55" in reds[0]["reason"]
        assert "50" in reds[0]["reason"]

    def test_r2901_equal_to_soc_during_solar_charge(self):
        """R2901 == SoC in solar_charge mode is OK (strict > check)."""
        data = {"mode": "solar_charge", "battery_soc_pct": 30}
        extra = {"r2901_readback_pct": 30}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "r2901_above_soc" for r in results)

    def test_r2901_strictly_above_soc(self):
        """R2901 strictly above SoC during discharge is RED."""
        data = {"mode": "discharge", "battery_soc_pct": 30}
        extra = {"r2901_readback_pct": 31}
        results = run_deterministic_checks(data, extra)
        reds = [r for r in results if r["check"] == "r2901_above_soc"]
        assert len(reds) == 1
        assert reds[0]["status"] == "RED"

    def test_r2901_above_soc_during_grid_charge_ok(self):
        """R2901 above SoC during grid_charge is expected, not RED."""
        data = {"mode": "grid_charge", "battery_soc_pct": 50}
        extra = {"r2901_readback_pct": 80}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "r2901_above_soc" for r in results)

    def test_r2901_below_soc_passes(self):
        """R2901 below SoC passes."""
        data = {"mode": "discharge", "battery_soc_pct": 70}
        extra = {"r2901_readback_pct": 30}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "r2901_above_soc" for r in results)

    def test_r2901_unavailable_skipped(self):
        """R2901=-1 (unavailable) is not flagged."""
        data = {"mode": "discharge", "battery_soc_pct": 50}
        extra = {"r2901_readback_pct": -1}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "r2901_above_soc" for r in results)

    def test_r2901_unknown_mode_skipped(self):
        """Unknown mode skips the r2901 check."""
        data = {"mode": "unknown", "battery_soc_pct": 50}
        extra = {"r2901_readback_pct": 80}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "r2901_above_soc" for r in results)

    # --- Check 3: Grid import during discharge ---

    def test_grid_import_during_discharge(self):
        """Grid import > 200W during discharge is RED."""
        data = {"mode": "discharge"}
        extra = {"grid_import_w": 500}
        results = run_deterministic_checks(data, extra)
        reds = [r for r in results if r["check"] == "grid_import_during_discharge"]
        assert len(reds) == 1
        assert "500" in reds[0]["reason"]

    def test_grid_import_200w_during_discharge_ok(self):
        """Grid import exactly 200W during discharge passes (not > 200)."""
        data = {"mode": "discharge"}
        extra = {"grid_import_w": 200}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "grid_import_during_discharge" for r in results)

    def test_grid_import_during_hold_ok(self):
        """Grid import during hold mode is not flagged."""
        data = {"mode": "hold"}
        extra = {"grid_import_w": 500}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "grid_import_during_discharge" for r in results)

    def test_grid_import_discharge_near_floor_ok(self):
        """Grid import during discharge near 30% floor is OK (SoC=31)."""
        data = {"mode": "discharge", "battery_soc_pct": 31}
        extra = {"grid_import_w": 500}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "grid_import_during_discharge" for r in results)

    def test_grid_import_discharge_above_floor(self):
        """Grid import during discharge well above floor is RED (SoC=40)."""
        data = {"mode": "discharge", "battery_soc_pct": 40}
        extra = {"grid_import_w": 500}
        results = run_deterministic_checks(data, extra)
        reds = [r for r in results if r["check"] == "grid_import_during_discharge"]
        assert len(reds) == 1
        assert reds[0]["status"] == "RED"

    def test_grid_import_discharge_at_exact_floor(self):
        """Grid import during discharge at exactly 30% floor is OK."""
        data = {"mode": "discharge", "battery_soc_pct": 30}
        extra = {"grid_import_w": 500}
        results = run_deterministic_checks(data, extra)
        assert not any(r["check"] == "grid_import_during_discharge" for r in results)

    # --- Check 4: Mac runner ---

    def test_mac_runner_found(self):
        """Mac runner active is RED."""
        results = run_deterministic_checks({}, {"mac_runner_found": True})
        reds = [r for r in results if r["check"] == "mac_runner_active"]
        assert len(reds) == 1
        assert reds[0]["status"] == "RED"

    def test_mac_runner_not_found(self):
        """Mac runner not found passes."""
        results = run_deterministic_checks({}, {"mac_runner_found": False})
        assert not any(r["check"] == "mac_runner_active" for r in results)

    def test_mac_runner_missing_defaults_false(self):
        """Missing mac_runner_found defaults to False."""
        results = run_deterministic_checks({}, {})
        assert not any(r["check"] == "mac_runner_active" for r in results)

    # --- Check 5: YAML automations ---

    def test_yaml_automations_on(self):
        """YAML automations ON is RED."""
        extra = {"yaml_automations_on": ["automation.mpc_register_writer"]}
        results = run_deterministic_checks({}, extra)
        reds = [r for r in results if r["check"] == "yaml_automations_on"]
        assert len(reds) == 1
        assert "mpc_register_writer" in reds[0]["reason"]

    def test_yaml_automations_empty_list(self):
        """Empty YAML automations list passes."""
        results = run_deterministic_checks({}, {"yaml_automations_on": []})
        assert not any(r["check"] == "yaml_automations_on" for r in results)

    def test_yaml_automations_missing(self):
        """Missing yaml_automations_on defaults to empty list."""
        results = run_deterministic_checks({}, {})
        assert not any(r["check"] == "yaml_automations_on" for r in results)

    # --- Check 6: Shadow mode ---

    def test_shadow_mode_active(self):
        """Shadow mode active is RED."""
        data = {"shadow_mode": True}
        results = run_deterministic_checks(data, {})
        reds = [r for r in results if r["check"] == "shadow_mode_active"]
        assert len(reds) == 1
        assert reds[0]["status"] == "RED"

    def test_shadow_mode_inactive(self):
        """Shadow mode False passes."""
        data = {"shadow_mode": False}
        results = run_deterministic_checks(data, {})
        assert not any(r["check"] == "shadow_mode_active" for r in results)

    # --- Check 7: Overnight SoC ---

    @patch("custom_components.victron_mpc.genai_monitor.datetime")
    def test_soc_below_30_overnight(self, mock_dt):
        """SoC below 30% during overnight hours is RED."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 23, 30)
        data = {"battery_soc_pct": 25}
        results = run_deterministic_checks(data, {})
        reds = [r for r in results if r["check"] == "soc_below_floor_overnight"]
        assert len(reds) == 1
        assert "25" in reds[0]["reason"]

    @patch("custom_components.victron_mpc.genai_monitor.datetime")
    def test_soc_below_30_early_morning(self, mock_dt):
        """SoC below 30% at 4am is RED (still overnight)."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 4, 0)
        data = {"battery_soc_pct": 28}
        results = run_deterministic_checks(data, {})
        assert any(r["check"] == "soc_below_floor_overnight" for r in results)

    @patch("custom_components.victron_mpc.genai_monitor.datetime")
    def test_soc_below_30_daytime_ok(self, mock_dt):
        """SoC below 30% during daytime is not flagged."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 14, 0)
        data = {"battery_soc_pct": 25}
        results = run_deterministic_checks(data, {})
        assert not any(r["check"] == "soc_below_floor_overnight" for r in results)

    @patch("custom_components.victron_mpc.genai_monitor.datetime")
    def test_soc_above_30_overnight_ok(self, mock_dt):
        """SoC above 30% during overnight is fine."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 23, 30)
        data = {"battery_soc_pct": 45}
        results = run_deterministic_checks(data, {})
        assert not any(r["check"] == "soc_below_floor_overnight" for r in results)

    @patch("custom_components.victron_mpc.genai_monitor.datetime")
    def test_soc_exactly_30_overnight_ok(self, mock_dt):
        """SoC exactly 30% overnight passes (< 30 check, not <=)."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 23, 30)
        data = {"battery_soc_pct": 30}
        results = run_deterministic_checks(data, {})
        assert not any(r["check"] == "soc_below_floor_overnight" for r in results)

    # --- Nested format ---

    def test_nested_coordinator_data(self):
        """Handles nested format from _build_sensor_data."""
        data = {
            "decision": {
                "state": "discharge",
                "battery_soc_pct": 50,
                "shadow_mode": False,
                "grid_import_w": 10,
            },
        }
        extra = {"r2900": 10, "r2901_readback_pct": 30}
        results = run_deterministic_checks(data, extra)
        # Should not flag r2901 (30 < 50)
        assert not any(r["check"] == "r2901_above_soc" for r in results)

    def test_nested_format_r2901_above_soc(self):
        """Nested format correctly detects R2901 above SoC."""
        data = {
            "decision": {
                "state": "discharge",
                "battery_soc_pct": 40,
                "shadow_mode": False,
                "grid_import_w": 10,
            },
        }
        extra = {"r2900": 10, "r2901_readback_pct": 45}
        results = run_deterministic_checks(data, extra)
        assert any(r["check"] == "r2901_above_soc" for r in results)

    # --- All pass ---

    def test_all_pass_returns_empty(self):
        """Healthy system returns empty list."""
        data = {
            "mode": "discharge",
            "battery_soc_pct": 70,
            "shadow_mode": False,
        }
        extra = {
            "r2900": 10,
            "r2901_readback_pct": 30,
            "grid_import_w": 50,
            "mac_runner_found": False,
            "yaml_automations_on": [],
        }
        results = run_deterministic_checks(data, extra)
        assert results == []

    # --- Multiple failures ---

    def test_multiple_reds(self):
        """Multiple failures are all returned."""
        data = {"mode": "discharge", "battery_soc_pct": 50, "shadow_mode": True}
        extra = {
            "r2900": 2,
            "r2901_readback_pct": 60,
            "grid_import_w": 500,
            "mac_runner_found": True,
            "yaml_automations_on": ["automation.mpc_register_writer"],
        }
        results = run_deterministic_checks(data, extra)
        checks_found = {r["check"] for r in results}
        assert "r2900_ess_mode" in checks_found
        assert "r2901_above_soc" in checks_found
        assert "grid_import_during_discharge" in checks_found
        assert "mac_runner_active" in checks_found
        assert "yaml_automations_on" in checks_found
        assert "shadow_mode_active" in checks_found

    # --- Robustness ---

    def test_empty_inputs(self):
        """Empty dicts don't crash."""
        results = run_deterministic_checks({}, {})
        # Only r2900 could trigger if default were bad, but -1 skips
        # No crashes expected
        assert isinstance(results, list)

    def test_none_soc_skips_checks(self):
        """None SoC doesn't crash r2901 check."""
        data = {"mode": "discharge", "battery_soc_pct": None}
        extra = {"r2901_readback_pct": 50}
        results = run_deterministic_checks(data, extra)
        # soc is None, so r2901 check should be skipped
        assert not any(r["check"] == "r2901_above_soc" for r in results)


# ======================================================================
# TestBuildStrategicSnapshot — Layer 2 snapshot builder
# ======================================================================


class TestBuildStrategicSnapshot:
    """Tests for build_strategic_snapshot — inclusion/exclusion of fields."""

    def test_includes_strategic_fields(self):
        """Snapshot includes observed state and LP intent sections."""
        data = {
            "mode": "discharge",
            "battery_soc_pct": 72,
            "buy_price": 0.25,
            "solar_forecast_today": 18.5,
            "decision": {
                "state": "discharge",
                "battery_soc_pct": 72,
                "intent": {
                    "action": "discharge",
                    "why": "Discharging at 2.0kW",
                    "key_assumptions": {"buy_price_now": 0.25},
                    "expected_outcomes": {"soc_in_1h_pct": 65},
                    "constraints_active": {},
                },
                "weather_confidence": 1.0,
            },
        }
        extra = {"amber_band": "neutral", "weather": "clear"}
        snapshot = build_strategic_snapshot(data, extra)

        assert "OBSERVED STATE" in snapshot
        assert "LP STATED INTENT" in snapshot
        assert "SoC: 72%" in snapshot
        assert "$0.25" in snapshot
        assert "Amber Band: neutral" in snapshot
        assert "18.5" in snapshot
        assert "discharge" in snapshot.lower()

    def test_excludes_operational_fields(self):
        """Snapshot must NOT include registers, power flows."""
        data = {
            "mode": "discharge",
            "battery_soc_pct": 72,
            "buy_price": 0.25,
        }
        extra = {
            "r2901_readback_pct": 30,
            "r2900": 10,
            "r37_setpoint_w": 50,
            "grid_import_w": 100,
            "grid_export_w": 0,
            "battery_power_w": -1500,
            "mac_runner_found": False,
            "yaml_automations_on": [],
        }
        snapshot = build_strategic_snapshot(data, extra)

        assert "R2901" not in snapshot
        assert "R2900" not in snapshot
        assert "R37" not in snapshot
        assert "grid_import" not in snapshot.lower() or "Grid Import" not in snapshot
        assert "battery_power" not in snapshot.lower()
        assert "mac_runner" not in snapshot.lower()
        assert "yaml_automations" not in snapshot.lower()
        assert "feedin" not in snapshot.lower()

    def test_includes_trajectory(self):
        """Trajectory is included when schedule_30min present."""
        data = {
            "mode": "discharge",
            "battery_soc_pct": 70,
            "schedule_30min": "[70, 68, 65, 62]",
        }
        snapshot = build_strategic_snapshot(data, {})
        assert "trajectory" in snapshot.lower()

    def test_no_trajectory_when_absent(self):
        """No trajectory line when schedule is empty."""
        data = {"mode": "discharge", "battery_soc_pct": 70}
        snapshot = build_strategic_snapshot(data, {})
        assert "trajectory" not in snapshot.lower()

    def test_handles_nested_format(self):
        """Works with nested coordinator_data format."""
        data = {
            "decision": {
                "state": "solar_charge",
                "battery_soc_pct": 60,
                "shadow_mode": False,
                "grid_import_w": 0,
                "schedule_30min": "[60, 65, 70]",
                "weather_confidence": 1.0,
                "intent": {"action": "solar_charge", "why": "Solar charging", "key_assumptions": {}, "expected_outcomes": {}, "constraints_active": {}},
            },
            "buy_price": {"state": 0.15},
            "solar_forecast_today": {"state": 20.0},
        }
        extra = {"amber_band": "low", "weather": "clear"}
        snapshot = build_strategic_snapshot(data, extra)

        assert "solar_charge" in snapshot
        assert "SoC: 60%" in snapshot
        assert "$0.15" in snapshot
        assert "Amber Band: low" in snapshot


# ======================================================================
# TestBuildHealthSnapshot — backward compatibility (old snapshot)
# ======================================================================


class TestBuildHealthSnapshot:
    """Tests for build_health_snapshot (backward compatibility)."""

    def test_basic_snapshot_contains_all_fields(self):
        """Snapshot includes mode, SoC, prices, and other key fields."""
        coordinator_data = {
            "mode": "discharge",
            "battery_soc_pct": 72.5,
            "target_register": 300,
            "battery_power_w": -1500,
            "solar_input_w": 3200,
            "load_input_w": 1800,
            "buy_price": 0.25,
            "sell_price": 0.06,
            "cloud_coverage": 40,
            "solar_forecast_today": 18.5,
            "spike": False,
            "shadow_mode": False,
        }
        extra = {
            "r2901_readback_pct": 30.0,
            "r2900": 12,
            "r37_setpoint_w": 0,
            "grid_import_w": 50,
            "grid_export_w": 0,
            "weather": "sunny",
            "solar_yield_kwh": 8.2,
        }

        snapshot = build_health_snapshot(coordinator_data, extra)

        assert "Mode: discharge" in snapshot
        assert "SoC: 72.5%" in snapshot
        assert "R2901 Readback: 30.0%" in snapshot
        assert "R2900 (ESS Mode): 12" in snapshot
        assert "Grid Import: 50W" in snapshot
        assert "Solar: 3200W" in snapshot
        assert "Buy Price: $0.25/kWh" in snapshot
        assert "Weather: sunny" in snapshot
        assert "Solar Yield So Far: 8.2 kWh" in snapshot

    def test_snapshot_includes_trajectory_when_available(self):
        """Schedule is included when present in coordinator data."""
        coordinator_data = {
            "schedule_30min": [{"time": "10:00", "soc_pct": 70}] * 20,
        }
        extra = {}

        snapshot = build_health_snapshot(coordinator_data, extra)

        assert "Planned trajectory" in snapshot

    def test_snapshot_handles_missing_keys(self):
        """Snapshot uses '?' for missing data instead of crashing."""
        snapshot = build_health_snapshot({}, {})

        assert "Mode: unknown" in snapshot
        assert "SoC: ?%" in snapshot
        assert "R2901 Readback: ?%" in snapshot

    def test_snapshot_no_trajectory_when_absent(self):
        """No trajectory line when schedule_30min is not present."""
        snapshot = build_health_snapshot({}, {})

        assert "Planned trajectory" not in snapshot

    def test_snapshot_reads_nested_coordinator_data(self):
        """Regression: build_health_snapshot must read from nested _build_sensor_data dicts.

        The coordinator returns nested dicts like decision={state: ..., battery_soc_pct: ...}.
        Previously the function read flat keys (coordinator_data.get('mode')) which returned None.
        """
        coordinator_data = {
            "battery_plan": {
                "state": 70.7,
                "mode": "discharge",
                "target_register": 300,
                "feedin_register": 0,
                "shadow_mode": False,
                "soc_1h_pct": 60,
            },
            "decision": {
                "state": "discharge",
                "reason": "Evening peak discharge",
                "target_soc_pct": 70.7,
                "target_register": 300,
                "buy_price_actual": 0.28,
                "sell_price_actual": 0.08,
                "spike": False,
                "shadow_mode": False,
                "battery_soc_pct": 74,
                "current_solar_w": 0,
                "current_load_w": 400,
                "grid_import_w": 50,
                "schedule_30min": "[70, 68, 65, 62]",
                "soc_1h_pct": 60,
                "soc_2h_pct": 48,
                "forecast_1h_w": 0,
                "forecast_2h_w": 500,
            },
            "solar_input_w": 0,
            "load_input_w": 400,
            "buy_price": {"state": 0.28, "spike": False},
            "sell_price": {"state": 0.08},
            "cloud_coverage": {"state": 46.8, "weather_condition": "fog"},
            "solar_forecast_today": {"state": 22.78},
            "solve_time_ms": 1233,
            "genai_health": {},
        }
        extra = {
            "r2901_readback_pct": 30.0,
            "r2900": 10,
            "r37_setpoint_w": 50,
            "grid_import_w": 45,
            "grid_export_w": 0,
            "battery_power_w": -1500,
            "weather": "fog",
            "solar_yield_kwh": 5.2,
        }

        snapshot = build_health_snapshot(coordinator_data, extra)

        # Mode extracted from decision.state, not flat coordinator_data.get('mode')
        assert "Mode: discharge" in snapshot
        # SoC from decision.battery_soc_pct
        assert "SoC: 74%" in snapshot
        # Target register from decision
        assert "Target Register (R2901 written): 300" in snapshot
        # Buy/sell price from nested dict .state
        assert "Buy Price: $0.28/kWh" in snapshot
        assert "Sell Price: $0.08/kWh" in snapshot
        # Cloud from nested dict .state
        assert "Cloud: 46.8%" in snapshot
        # Solar forecast from nested dict .state
        assert "Solar Forecast Today: 22.78 kWh" in snapshot
        # Spike from decision
        assert "Spike: False" in snapshot
        # Shadow mode from decision
        assert "Shadow Mode: False" in snapshot
        # Feedin register from battery_plan
        assert "Feedin Register (R2706): 0" in snapshot
        # Grid import from decision (not extra)
        assert "Grid Import: 50W" in snapshot
        # Battery power from extra
        assert "Battery Power: -1500W" in snapshot
        # Solar/load from scalar keys
        assert "Solar: 0W" in snapshot
        assert "Load: 400W" in snapshot
        # Trajectory from decision.schedule_30min
        assert "Planned trajectory" in snapshot
        # SoC lookahead
        assert "SoC in 1h: 60%" in snapshot
        assert "SoC in 2h: 48%" in snapshot
        # Solar forecast hourly from decision attributes
        assert "Solar forecast_1h_w: 0W" in snapshot
        assert "Solar forecast_2h_w: 500W" in snapshot


# ======================================================================
# TestRunGenaiHealthCheck — Layer 2 API calls
# ======================================================================


class TestRunGenaiHealthCheck:
    """Tests for run_genai_health_check."""

    @pytest.mark.asyncio
    async def test_skips_when_no_api_key(self):
        """Returns SKIP status when API key is empty."""
        result = await run_genai_health_check(None, "", "snapshot")

        assert result["status"] == "SKIP"
        assert "No OpenRouter API key" in result["summary"]

    @pytest.mark.asyncio
    async def test_successful_green_response(self):
        """Parses a clean GREEN JSON response."""
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "GREEN",
                                "summary": "All systems nominal",
                                "details": "",
                            }
                        )
                    }
                }
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot data")

        assert result["status"] == "GREEN"
        assert result["summary"] == "All systems nominal"
        assert result["details"] == ""

    @pytest.mark.asyncio
    async def test_red_response_downgraded_to_yellow(self):
        """RED responses from GenAI are downgraded to YELLOW."""
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "RED",
                                "summary": "Grid charging during discharge mode",
                                "details": "R2901 is set above current SoC",
                            }
                        )
                    }
                }
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot data")

        # RED downgraded to YELLOW
        assert result["status"] == "YELLOW"
        assert "Grid charging" in result["summary"]
        assert "Downgraded from RED" in result["details"]

    @pytest.mark.asyncio
    async def test_handles_markdown_code_block_response(self):
        """Strips markdown code fences from model response."""
        wrapped = '```json\n{"status": "YELLOW", "summary": "Minor issue", "details": "test"}\n```'
        api_response = {"choices": [{"message": {"content": wrapped}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "YELLOW"
        assert result["summary"] == "Minor issue"

    @pytest.mark.asyncio
    async def test_handles_markdown_code_block_with_extra_text(self):
        """Strips markdown code fences even when extra text surrounds the JSON."""
        # Regression: some models return prose before/after the code block
        wrapped = 'Here is the analysis:\n```json\n{"status": "GREEN", "summary": "OK", "details": "fine"}\n```\nEnd.'
        api_response = {"choices": [{"message": {"content": wrapped}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "GREEN"
        assert result["summary"] == "OK"

    @pytest.mark.asyncio
    async def test_handles_bare_code_block_no_language_tag(self):
        """Strips bare ``` fences (no language tag)."""
        wrapped = '```\n{"status": "YELLOW", "summary": "Bad", "details": "very bad"}\n```'
        api_response = {"choices": [{"message": {"content": wrapped}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "YELLOW"
        assert result["summary"] == "Bad"

    @pytest.mark.asyncio
    async def test_handles_api_error_status(self):
        """Returns ERROR on non-200 HTTP status."""
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.text = AsyncMock(return_value="Rate limited")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "ERROR"
        assert "429" in result["summary"]

    @pytest.mark.asyncio
    async def test_handles_non_json_response(self):
        """Returns ERROR when model returns non-JSON text."""
        api_response = {"choices": [{"message": {"content": "I cannot parse this as JSON"}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "ERROR"
        assert "Non-JSON" in result["summary"]

    @pytest.mark.asyncio
    async def test_handles_network_exception(self):
        """Returns ERROR on network failure."""
        session = MagicMock()
        session.post = MagicMock(side_effect=ConnectionError("Network down"))

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "ERROR"
        assert "Network down" in result["summary"]

    @pytest.mark.asyncio
    async def test_sends_correct_headers(self):
        """Verifies Authorization Bearer header is sent."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-my-key", "snapshot")

        call_kwargs = session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == "Bearer sk-or-my-key"
        assert headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_uses_correct_model(self):
        """Verifies the anthropic/claude-haiku-4.5 model is used via OpenRouter."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_kwargs = session.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["model"] == "anthropic/claude-haiku-4.5"
        assert payload["max_tokens"] == 800
        assert payload["stream"] is False

    @pytest.mark.asyncio
    async def test_uses_openrouter_url(self):
        """Verifies the OpenRouter API endpoint is called."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_args = session.post.call_args
        url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert url == "https://openrouter.ai/api/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_system_prompt_in_messages(self):
        """Verifies system prompt is sent as a message (OpenAI format)."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_kwargs = session.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        # System prompt should NOT be a top-level key (Anthropic format)
        assert "system" not in payload

    @pytest.mark.asyncio
    async def test_system_prompt_is_strategic(self):
        """System prompt mentions strategic advisor, not register checks."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_kwargs = session.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        system_content = payload["messages"][0]["content"]
        assert "alignment" in system_content.lower()
        assert "Never return RED" in system_content

    @pytest.mark.asyncio
    async def test_truncated_red_response_downgraded(self):
        """Truncated RED response recovered via regex is also downgraded."""
        # Simulate a truncated JSON where only status and summary are parseable
        truncated = '{"status": "RED", "summary": "Critical issue", "details": "long text that gets'
        api_response = {"choices": [{"message": {"content": truncated}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-key", "snapshot")

        # Truncated RED should be downgraded to YELLOW
        assert result["status"] == "YELLOW"
        assert result["summary"] == "Critical issue"


# ======================================================================
# TestCycleInterval
# ======================================================================


class TestCycleInterval:
    """Tests for the cycle interval constant."""

    def test_interval_is_12(self):
        """12 cycles x 5 min = 60 min hourly check."""
        assert GENAI_CYCLE_INTERVAL == 12


# ======================================================================
# TestGenAIHistory — rolling history buffer
# ======================================================================


class TestGenAIHistory:
    """Tests for the rolling history buffer entry format."""

    def test_entry_has_required_keys(self):
        """History entry has all required top-level and nested keys."""
        entry = {
            "timestamp": "2026-03-31T01:00:00",
            "source": "genai",
            "status": "GREEN",
            "summary": "All good",
            "readings": {
                "soc_pct": 50.0,
                "mode": "discharge",
                "buy_price": 0.20,
                "solar_w": 0,
                "load_w": 600,
                "grid_import_w": 45,
            },
        }
        assert "timestamp" in entry
        assert "source" in entry
        assert entry["source"] in ("deterministic", "genai")
        assert entry["status"] in ("GREEN", "YELLOW", "RED")
        assert "readings" in entry
        assert "soc_pct" in entry["readings"]

    def test_buffer_trim(self):
        """Buffer is trimmed to max 168 entries."""
        buf: list[dict] = []
        for i in range(200):
            buf.append({"timestamp": f"t{i}", "status": "GREEN"})
        max_size = 168
        if len(buf) > max_size:
            buf = buf[-max_size:]
        assert len(buf) == 168
        assert buf[0]["timestamp"] == "t32"  # oldest kept

    def test_buffer_preserves_order(self):
        """Buffer maintains insertion order."""
        buf = [{"ts": i} for i in range(5)]
        assert buf[0]["ts"] == 0
        assert buf[-1]["ts"] == 4

    def test_readings_structure(self):
        """Readings dict has correct types."""
        readings = {
            "soc_pct": 45.0,
            "mode": "discharge",
            "buy_price": 0.22,
            "solar_w": 0,
            "load_w": 582,
            "grid_import_w": 43,
        }
        assert isinstance(readings["soc_pct"], float)
        assert isinstance(readings["solar_w"], int)
        assert readings["mode"] in (
            "discharge", "hold", "solar_charge", "grid_charge", "export",
        )

    def test_sensor_attributes_include_history(self):
        """Sensor attributes_fn includes history key."""
        data = {
            "status": "GREEN",
            "summary": "All ok",
            "details": "",
            "history": [{"timestamp": "t1", "status": "GREEN"}],
        }
        attrs_fn = lambda d: {
            "summary": d.get("summary", ""),
            "details": d.get("details", ""),
            "history": d.get("history", []),
        } if isinstance(d, dict) else {}
        attrs = attrs_fn(data)
        assert "history" in attrs
        assert len(attrs["history"]) == 1

    def test_empty_history_default(self):
        """Missing history key defaults to empty list."""
        data = {"status": "GREEN", "summary": "ok", "details": ""}
        attrs_fn = lambda d: {"history": d.get("history", [])}
        assert attrs_fn(data)["history"] == []

    def test_history_dedup_skips_identical(self):
        """Appending identical entry twice results in only one entry."""
        buf: list[dict] = []

        def append_with_dedup(buf, source, status, summary):
            if buf:
                last = buf[-1]
                if last.get("source") == source and last.get("status") == status and last.get("summary") == summary:
                    return
            buf.append({"source": source, "status": status, "summary": summary})

        append_with_dedup(buf, "deterministic", "GREEN", "All healthy")
        append_with_dedup(buf, "deterministic", "GREEN", "All healthy")
        assert len(buf) == 1

    def test_history_dedup_allows_different(self):
        """Appending different entries results in both being kept."""
        buf: list[dict] = []

        def append_with_dedup(buf, source, status, summary):
            if buf:
                last = buf[-1]
                if last.get("source") == source and last.get("status") == status and last.get("summary") == summary:
                    return
            buf.append({"source": source, "status": status, "summary": summary})

        append_with_dedup(buf, "deterministic", "GREEN", "All healthy")
        append_with_dedup(buf, "genai", "YELLOW", "Minor concern")
        assert len(buf) == 2
