"""Tests for autotune optimization loop, report, and apply modules."""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

import pytest

from autotune.metric import BATTERY_CAPACITY_KWH
from autotune.optimize_loop import analyze_cost_drivers, propose_change
from autotune.report import compare_configs, daily_breakdown, generate_report
from autotune.apply import generate_options_flow_values, preview_changes
from autotune.types import DayResult, EvalResult


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def make_day_result(
    date: str = "2026-03-15",
    grid_cost: float = 2.0,
    export_revenue: float = 0.5,
    wear_cost_fixed: float = 0.10,
    floor_violations: int = 0,
    min_soc_pct: float = 35.0,
    sunset_soc_pct: float = 85.0,
    end_soc_kwh: float = 7.1,
    total_discharge_kwh: float = 5.0,
    solver_status: str = "optimal",
) -> DayResult:
    """Create a DayResult for testing."""
    return DayResult(
        date=date,
        grid_cost=grid_cost,
        export_revenue=export_revenue,
        wear_cost_fixed=wear_cost_fixed,
        floor_violations=floor_violations,
        min_soc_pct=min_soc_pct,
        sunset_soc_pct=sunset_soc_pct,
        end_soc_kwh=end_soc_kwh,
        total_discharge_kwh=total_discharge_kwh,
        solver_status=solver_status,
    )


def make_eval_result(
    day_results: list[DayResult] | None = None,
    composite: float = 5.0,
) -> EvalResult:
    """Create an EvalResult for testing."""
    if day_results is None:
        day_results = [make_day_result()]
    return EvalResult(
        composite_metric=composite,
        breakdown={
            "grid_cost": sum(r.grid_cost for r in day_results),
            "export_revenue": sum(r.export_revenue for r in day_results),
            "wear_cost_fixed": 0.10,
            "floor_violations": sum(r.floor_violations for r in day_results),
            "floor_penalty": 0.0,
            "sunset_penalty": 0.0,
            "continuity_penalty": 0.0,
            "cycling_penalty": 0.0,
            "composite": composite,
        },
        per_day=day_results,
    )


def make_train_config() -> dict:
    """Create a minimal train_config dict."""
    return {
        "parameters": {
            "battery_wear_cost": {"value": 0.02, "min": 0.01, "max": 0.10, "step": 0.005},
            "grid_import_penalty": {"value": 0.02, "min": 0.00, "max": 0.05, "step": 0.005},
            "sunset_reward": {"value": 0.04, "min": 0.01, "max": 0.10, "step": 0.01},
            "overnight_hold_reward": {"value": 0.05, "min": 0.02, "max": 0.20, "step": 0.01},
            "overnight_min_soc_pct": {"value": 31.0, "min": 20.0, "max": 50.0, "step": 1.0},
            "soc_profile_pre_peak": {"value": 0.20, "min": 0.10, "max": 0.40, "step": 0.01},
            "soft_floor_penalty": {"value": 0.10, "min": 0.05, "max": 0.30, "step": 0.01},
        },
        "evaluation": {
            "fixed_wear_cost_per_kwh": 0.02,
            "min_days": 14,
            "improvement_threshold": 0.001,
        },
    }


def write_temp_config(config: dict) -> Path:
    """Write a train_config dict to a temp file and return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(config, f)
    f.close()
    return Path(f.name)


# ──────────────────────────────────────────────────────────────────
# Tests: analyze_cost_drivers
# ──────────────────────────────────────────────────────────────────


class TestAnalyzeCostDrivers:
    """Tests for cost driver analysis."""

    def test_analyze_floor_violations(self):
        """DayResult with floor_violations=10 suggests overnight params."""
        day = make_day_result(floor_violations=10, sunset_soc_pct=85.0)
        result = make_eval_result([day])
        drivers = analyze_cost_drivers(result)

        param_names = [d["param"] for d in drivers]
        assert "overnight_hold_reward" in param_names
        assert "overnight_min_soc_pct" in param_names

    def test_analyze_low_sunset(self):
        """DayResult with sunset_soc_pct=65 suggests sunset_reward."""
        day = make_day_result(sunset_soc_pct=65.0, floor_violations=0)
        result = make_eval_result([day])
        drivers = analyze_cost_drivers(result)

        param_names = [d["param"] for d in drivers]
        assert "sunset_reward" in param_names

    def test_analyze_no_issues(self):
        """Clean DayResult with no violations -> empty or minimal drivers."""
        day = make_day_result(
            floor_violations=0,
            sunset_soc_pct=85.0,
            total_discharge_kwh=5.0,
            grid_cost=1.0,
        )
        # Make composite high enough that grid cost ratio is < 70%
        result = make_eval_result([day], composite=5.0)
        drivers = analyze_cost_drivers(result)

        # No floor violations, good sunset, moderate cycling, low grid ratio
        driver_types = [d["driver"] for d in drivers]
        assert "floor_violations" not in driver_types
        assert "low_sunset_soc" not in driver_types


# ──────────────────────────────────────────────────────────────────
# Tests: propose_change
# ──────────────────────────────────────────────────────────────────


class TestProposeChange:
    """Tests for parameter change proposals."""

    def test_propose_change_respects_bounds(self):
        """Proposed value should be clamped to [min, max]."""
        config = make_train_config()
        current = {"sunset_reward": 0.09}  # Near max of 0.10
        driver = {
            "driver": "low_sunset_soc",
            "param": "sunset_reward",
            "direction": "+",
            "magnitude": 0.05,  # Would push to 0.14, above max
        }

        new_tunables = propose_change(driver, current, config)
        assert new_tunables["sunset_reward"] <= 0.10  # Clamped to max


# ──────────────────────────────────────────────────────────────────
# Tests: report.py
# ──────────────────────────────────────────────────────────────────


class TestReport:
    """Tests for report generation."""

    def test_generate_report_nonempty(self):
        """Baseline and optimized EvalResults produce a non-empty report."""
        baseline = make_eval_result(composite=5.0)
        optimized = make_eval_result(composite=4.5)
        before = {"battery_wear_cost": 0.02, "sunset_reward": 0.04}
        after = {"battery_wear_cost": 0.03, "sunset_reward": 0.04}

        report = generate_report(baseline, optimized, before, after)

        assert len(report) > 0
        assert "AUTOTUNE OPTIMIZATION REPORT" in report
        assert "Baseline metric" in report
        assert "Optimized metric" in report

    def test_compare_configs_changed(self):
        """Before/after with differences shows changes."""
        before = {"battery_wear_cost": 0.02, "sunset_reward": 0.04}
        after = {"battery_wear_cost": 0.03, "sunset_reward": 0.06}

        text = compare_configs(before, after)

        assert "battery_wear_cost" in text
        assert "sunset_reward" in text
        assert "(no changes)" not in text

    def test_compare_configs_unchanged(self):
        """Identical params show '(no changes)'."""
        params = {"battery_wear_cost": 0.02, "sunset_reward": 0.04}

        text = compare_configs(params, params)

        assert "(no changes)" in text

    def test_daily_breakdown_rows(self):
        """EvalResult with 3 days produces 3 data rows."""
        days = [
            make_day_result(date=f"2026-03-{15+i}")
            for i in range(3)
        ]
        result = make_eval_result(days)

        text = daily_breakdown(result)

        assert "2026-03-15" in text
        assert "2026-03-16" in text
        assert "2026-03-17" in text
        assert "TOTAL" in text
        assert "Composite metric" in text


# ──────────────────────────────────────────────────────────────────
# Tests: apply.py
# ──────────────────────────────────────────────────────────────────


class TestApply:
    """Tests for production preview and options flow."""

    def test_preview_changes(self):
        """preview_changes with temp config produces non-empty output."""
        config = make_train_config()
        path = write_temp_config(config)
        try:
            production = {"battery_wear_cost": 0.02, "sunset_reward": 0.06}
            text = preview_changes(path, production)

            assert len(text) > 0
            assert "PRODUCTION CHANGE PREVIEW" in text
        finally:
            path.unlink()

    def test_generate_options_flow_values(self):
        """generate_options_flow_values returns dict with all params."""
        config = make_train_config()
        path = write_temp_config(config)
        try:
            values = generate_options_flow_values(path)

            assert isinstance(values, dict)
            assert "battery_wear_cost" in values
            assert "sunset_reward" in values
            assert values["battery_wear_cost"] == 0.02
        finally:
            path.unlink()

    def test_apply_no_network_imports(self):
        """apply.py must not import urllib, requests, socket, http.client."""
        apply_path = Path(__file__).parent.parent / "autotune" / "apply.py"
        source = apply_path.read_text()

        forbidden = ["urllib", "requests", "socket", "http.client"]
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for lib in forbidden:
                assert f"import {lib}" not in stripped, (
                    f"apply.py imports forbidden network library: {lib}"
                )
                assert f"from {lib}" not in stripped, (
                    f"apply.py imports forbidden network library: {lib}"
                )


# ──────────────────────────────────────────────────────────────────
# Tests: no homeassistant imports in new modules
# ──────────────────────────────────────────────────────────────────


class TestNoHAImports:
    """Verify zero homeassistant imports in all autotune modules."""

    def test_no_ha_imports(self):
        """All autotune source files must not contain homeassistant imports."""
        autotune_dir = Path(__file__).parent.parent / "autotune"
        for py_file in autotune_dir.glob("*.py"):
            source = py_file.read_text()
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert "from homeassistant" not in stripped, (
                    f"{py_file.name} imports homeassistant: {stripped}"
                )
                assert "import homeassistant" not in stripped, (
                    f"{py_file.name} imports homeassistant: {stripped}"
                )
