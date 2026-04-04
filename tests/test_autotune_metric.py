"""Tests for autotune anti-gaming composite metric."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from autotune.metric import (
    BATTERY_CAPACITY_KWH,
    EVAL_EXCESSIVE_CYCLING_PENALTY,
    EVAL_FLOOR_VIOLATION_PENALTY,
    EVAL_SOC_CONTINUITY_PENALTY,
    EVAL_SUNSET_PENALTY,
    EVAL_WEAR_COST,
    score_day,
    score_period,
)
from autotune.types import DayData, DayResult, EvalResult

# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────

STEPS_24H = 288
DT_MINUTES = 5
DT_HOURS = DT_MINUTES / 60.0


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


def solar_bell(
    peak_kw: float = 5.0,
    sunrise_hour: int = 6,
    sunset_hour: int = 18,
    n: int = STEPS_24H,
) -> list[float]:
    """Bell-curve solar profile."""
    steps_per_hour = 60 // DT_MINUTES
    noon = (sunrise_hour + sunset_hour) / 2
    sigma = (sunset_hour - sunrise_hour) / 4
    profile: list[float] = []
    for i in range(n):
        hour = i / steps_per_hour
        if sunrise_hour <= hour <= sunset_hour:
            intensity = math.exp(-0.5 * ((hour - noon) / sigma) ** 2)
            profile.append(peak_kw * intensity)
        else:
            profile.append(0.0)
    return profile


def amber_typical_day(n: int = STEPS_24H) -> list[float]:
    """Typical Amber pricing: cheap overnight, expensive evening."""
    steps_per_hour = 60 // DT_MINUTES
    prices: list[float] = []
    for i in range(n):
        hour = (i // steps_per_hour) % 24
        if hour < 5:
            prices.append(0.15)
        elif hour < 7:
            prices.append(0.22)
        elif hour < 9:
            prices.append(0.30)
        elif hour < 15:
            prices.append(0.25)
        elif hour < 17:
            prices.append(0.30)
        elif hour < 21:
            prices.append(0.45)
        else:
            prices.append(0.20)
    return prices


def make_day_data(
    date: str = "2026-03-15",
    solar_kw: list[float] | None = None,
    load_kw: float = 1.0,
    buy_price: list[float] | None = None,
    sell_ratio: float = 0.2,
    sunset_step: int = 216,
    start_soc_kwh: float = 7.1,
    price_bands: list[str] | None = None,
) -> DayData:
    """Create synthetic DayData for testing."""
    if solar_kw is None:
        solar_kw = solar_bell()
    if buy_price is None:
        buy_price = amber_typical_day()

    sell_price = [p * sell_ratio for p in buy_price]

    steps_per_hour = 60 // DT_MINUTES
    overnight: list[int] = []
    for i in range(STEPS_24H):
        hour = i // steps_per_hour
        if hour >= 22 or hour < 6:
            overnight.append(i)

    return DayData(
        date=date,
        solar_kw_5min=solar_kw,
        load_kw_5min=[load_kw] * STEPS_24H,
        buy_price_5min=buy_price,
        sell_price_5min=sell_price,
        sunset_step=sunset_step,
        overnight_steps=overnight,
        start_soc_kwh=start_soc_kwh,
        price_bands=price_bands,
    )


def default_tunables() -> dict[str, float]:
    """Default tunable parameters matching train_config.json."""
    return {
        "battery_wear_cost": 0.02,
        "grid_import_penalty": 0.02,
        "sunset_reward": 0.04,
        "terminal_reward": 0.03,
        "overnight_hold_reward": 0.05,
        "soc_floor_pct": 10.0,
        "overnight_min_soc_pct": 31.0,
        "load_inflation_pct": 10.0,
        "solar_cloud_impact": 0.75,
        "solar_derating_min": 0.50,
        "soc_profile_peak": 0.15,
        "soc_profile_pre_peak": 0.20,
        "soc_profile_morning": 0.10,
        "soc_profile_overnight": 0.03,
        "soc_profile_default": 0.05,
        "grid_charge_boost": 0.15,
        "soft_floor_penalty": 0.10,
    }


# ──────────────────────────────────────────────────────────────────
# Tests: score_day
# ──────────────────────────────────────────────────────────────────


class TestScoreDay:
    """Tests for single-day scoring."""

    def test_score_day_basic(self):
        """Basic cost = grid_cost - export + wear, no penalties."""
        day = make_day_result(
            grid_cost=2.0,
            export_revenue=0.5,
            total_discharge_kwh=5.0,
            floor_violations=0,
            sunset_soc_pct=85.0,
        )
        score = score_day(day)
        # 2.0 - 0.5 + 5.0 * 0.02 = 1.6
        assert score == pytest.approx(1.6, abs=0.001)

    def test_score_day_floor_violation_penalty(self):
        """12 violations = 12 * $0.50 = $6.00 extra."""
        day = make_day_result(
            grid_cost=2.0,
            export_revenue=0.5,
            total_discharge_kwh=5.0,
            floor_violations=12,
            sunset_soc_pct=85.0,
        )
        score = score_day(day)
        # 2.0 - 0.5 + 0.10 + 12*0.50 = 1.5 + 0.10 + 6.0 = 7.6
        expected = 2.0 - 0.5 + 5.0 * EVAL_WEAR_COST + 12 * EVAL_FLOOR_VIOLATION_PENALTY
        assert score == pytest.approx(expected, abs=0.001)

    def test_score_day_sunset_penalty(self):
        """Sunset at 60% = (80-60)*0.10 = $2.00 penalty."""
        day = make_day_result(
            grid_cost=2.0,
            export_revenue=0.5,
            total_discharge_kwh=5.0,
            floor_violations=0,
            sunset_soc_pct=60.0,
        )
        score = score_day(day)
        expected = 2.0 - 0.5 + 5.0 * EVAL_WEAR_COST + (80.0 - 60.0) * EVAL_SUNSET_PENALTY
        assert score == pytest.approx(expected, abs=0.001)

    def test_score_day_no_sunset_penalty_above_80(self):
        """Sunset at 85% = $0 sunset penalty."""
        day = make_day_result(
            grid_cost=2.0,
            export_revenue=0.5,
            total_discharge_kwh=5.0,
            floor_violations=0,
            sunset_soc_pct=85.0,
        )
        score = score_day(day)
        # No sunset penalty
        expected = 2.0 - 0.5 + 5.0 * EVAL_WEAR_COST
        assert score == pytest.approx(expected, abs=0.001)


# ──────────────────────────────────────────────────────────────────
# Tests: score_period
# ──────────────────────────────────────────────────────────────────


class TestScorePeriod:
    """Tests for multi-day period scoring."""

    def test_score_period_continuity_penalty(self):
        """Start 50%, end 30% = 20 * $0.05 = $1.00 penalty."""
        day = make_day_result(
            grid_cost=0.0,
            export_revenue=0.0,
            total_discharge_kwh=0.0,
            floor_violations=0,
            sunset_soc_pct=85.0,
            end_soc_kwh=7.1 * 0.6,  # ~30% of 14.2
        )
        start_soc_kwh = 7.1  # 50%
        end_soc_kwh = day.end_soc_kwh  # ~30%

        result = score_period([day], start_soc_kwh, end_soc_kwh, 1)

        start_pct = start_soc_kwh / BATTERY_CAPACITY_KWH * 100
        end_pct = end_soc_kwh / BATTERY_CAPACITY_KWH * 100
        expected_continuity = abs(end_pct - start_pct) * EVAL_SOC_CONTINUITY_PENALTY

        assert result.breakdown["continuity_penalty"] == pytest.approx(
            expected_continuity, abs=0.01
        )

    def test_score_period_cycling_penalty(self):
        """3 days, 50kWh discharge, cap 14.2*3=42.6, excess 7.4 -> $0.074."""
        days = [
            make_day_result(
                date=f"2026-03-{15+i}",
                grid_cost=0.0,
                export_revenue=0.0,
                total_discharge_kwh=50.0 / 3,
                floor_violations=0,
                sunset_soc_pct=85.0,
                end_soc_kwh=7.1,
            )
            for i in range(3)
        ]
        result = score_period(days, 7.1, 7.1, 3)

        # Total discharge = 50, cap = 14.2*3 = 42.6, excess = 7.4
        expected_cycling = (50.0 - 42.6) * EVAL_EXCESSIVE_CYCLING_PENALTY
        assert result.breakdown["cycling_penalty"] == pytest.approx(
            expected_cycling, abs=0.01
        )

    def test_score_period_no_cycling_penalty(self):
        """Discharge within 1 cycle/day should have no cycling penalty."""
        days = [
            make_day_result(
                date=f"2026-03-{15+i}",
                total_discharge_kwh=10.0,  # Below 14.2 per day
                floor_violations=0,
                sunset_soc_pct=85.0,
                end_soc_kwh=7.1,
            )
            for i in range(3)
        ]
        result = score_period(days, 7.1, 7.1, 3)

        # Total = 30, cap = 42.6 -> no excess
        assert result.breakdown["cycling_penalty"] == pytest.approx(0.0, abs=0.001)

    def test_score_period_returns_eval_result(self):
        """score_period returns a valid EvalResult."""
        day = make_day_result()
        result = score_period([day], 7.1, day.end_soc_kwh, 1)

        assert isinstance(result, EvalResult)
        assert result.composite_metric > 0
        assert "grid_cost" in result.breakdown
        assert "cycling_penalty" in result.breakdown
        assert len(result.per_day) == 1


# ──────────────────────────────────────────────────────────────────
# Tests: anti-gaming
# ──────────────────────────────────────────────────────────────────


class TestAntiGaming:
    """Tests proving the metric cannot be gamed by zeroing tunables."""

    def test_anti_gaming_wear_cost_zero(self):
        """Even with battery_wear_cost=0.0 in tunables, metric still penalizes wear."""
        from autotune.evaluate import evaluate_day

        day = make_day_data(load_kw=2.0)
        tunables = default_tunables()
        tunables["battery_wear_cost"] = 0.0  # Zero out the tunable

        result = evaluate_day(day, tunables)

        # The LP may discharge more with zero wear cost
        # But the metric still uses FIXED wear cost
        assert result.total_discharge_kwh >= 0.0
        if result.total_discharge_kwh > 0:
            score = score_day(result)
            # Score must include wear penalty even though tunable is zero
            base_cost = result.grid_cost - result.export_revenue
            assert score > base_cost  # Wear cost adds to score

    def test_anti_gaming_floor_penalty_zero(self):
        """Even with soft_floor_penalty=0.0 in tunables, metric still penalizes violations."""
        # Create a DayResult that has floor violations
        day = make_day_result(floor_violations=5, sunset_soc_pct=85.0)

        score_with_violations = score_day(day)
        day_clean = make_day_result(floor_violations=0, sunset_soc_pct=85.0)
        score_clean = score_day(day_clean)

        # Even if the tunable soft_floor_penalty was 0, our FIXED penalty still applies
        assert score_with_violations > score_clean
        assert score_with_violations - score_clean == pytest.approx(
            5 * EVAL_FLOOR_VIOLATION_PENALTY, abs=0.001
        )


# ──────────────────────────────────────────────────────────────────
# Tests: determinism
# ──────────────────────────────────────────────────────────────────


class TestDeterminism:
    """Tests for reproducibility."""

    def test_determinism(self):
        """Same inputs produce same score across 10 iterations."""
        day = make_day_result()
        scores = [score_day(day) for _ in range(10)]
        assert all(s == scores[0] for s in scores)
