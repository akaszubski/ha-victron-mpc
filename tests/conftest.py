"""Shared test fixtures for MPC optimizer tests."""

from __future__ import annotations

import math

from custom_components.victron_mpc.optimizer import OptInput


# ──────────────────────────────────────────────────────────────────
# System defaults
# ──────────────────────────────────────────────────────────────────

BATTERY_CAPACITY = 14.2  # kWh
DT_MINUTES = 5
DT_HOURS = DT_MINUTES / 60
STEPS_24H = 24 * 60 // DT_MINUTES  # 288


def make_opt_input(
    *,
    soc_pct: float = 50.0,
    solar_kw: list[float] | float = 0.0,
    load_kw: list[float] | float = 1.0,
    buy_price: list[float] | float = 0.30,
    sell_price: list[float] | float = 0.06,
    horizon_steps: int = STEPS_24H,
    soc_min_pct: float = 20.0,
    soc_max_pct: float = 100.0,
    max_charge_kw: float = 3.5,
    max_discharge_kw: float = 4.5,
    max_grid_import_kw: float = 10.0,
    max_grid_export_kw: float = 5.0,
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
    battery_wear_cost: float = 0.05,
    grid_import_penalty: float = 0.02,
    sunset_step: int | None = None,
    sunset_reward: float = 0.04,
    terminal_reward: float = 0.03,
    overnight_hold_reward: float = 0.0,
    overnight_steps: list[int] | None = None,
    force_full_charge: bool = False,
) -> OptInput:
    """Create OptInput with sensible defaults. Scalars are broadcast to arrays."""

    def _to_list(v, n):
        if isinstance(v, (list, tuple)):
            if len(v) < n:
                return list(v) + [v[-1]] * (n - len(v))
            return list(v[:n])
        return [v] * n

    return OptInput(
        horizon_steps=horizon_steps,
        dt_hours=DT_HOURS,
        battery_soc_kwh=soc_pct / 100 * BATTERY_CAPACITY,
        battery_capacity_kwh=BATTERY_CAPACITY,
        soc_min_kwh=soc_min_pct / 100 * BATTERY_CAPACITY,
        soc_max_kwh=soc_max_pct / 100 * BATTERY_CAPACITY,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
        max_grid_import_kw=max_grid_import_kw,
        max_grid_export_kw=max_grid_export_kw,
        solar_forecast_kw=_to_list(solar_kw, horizon_steps),
        load_forecast_kw=_to_list(load_kw, horizon_steps),
        buy_price=_to_list(buy_price, horizon_steps),
        sell_price=_to_list(sell_price, horizon_steps),
        battery_wear_cost=battery_wear_cost,
        grid_import_penalty=grid_import_penalty,
        sunset_step=sunset_step,
        sunset_reward=sunset_reward,
        terminal_reward=terminal_reward,
        overnight_hold_reward=overnight_hold_reward,
        overnight_steps=overnight_steps,
        force_full_charge=force_full_charge,
    )


# ──────────────────────────────────────────────────────────────────
# Price curve helpers
# ──────────────────────────────────────────────────────────────────

def flat_price(price: float, n: int = STEPS_24H) -> list[float]:
    """Constant price."""
    return [price] * n


def amber_typical_day(n: int = STEPS_24H) -> list[float]:
    """Typical Amber day: cheap overnight, moderate day, expensive evening."""
    steps_per_hour = 60 // DT_MINUTES
    prices = []
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


def spike_at_hour(
    base_price: float, spike_price: float, spike_hour: int,
    spike_duration_hours: float = 1.0, n: int = STEPS_24H,
) -> list[float]:
    """Price profile with a spike at a specific hour."""
    steps_per_hour = 60 // DT_MINUTES
    spike_start = spike_hour * steps_per_hour
    spike_end = spike_start + int(spike_duration_hours * steps_per_hour)
    prices = [base_price] * n
    for i in range(max(0, spike_start), min(n, spike_end)):
        prices[i] = spike_price
    return prices


# ──────────────────────────────────────────────────────────────────
# Solar curve helpers
# ──────────────────────────────────────────────────────────────────

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
    profile = []
    for i in range(n):
        hour = i / steps_per_hour
        if sunrise_hour <= hour <= sunset_hour:
            intensity = math.exp(-0.5 * ((hour - noon) / sigma) ** 2)
            profile.append(peak_kw * intensity)
        else:
            profile.append(0.0)
    return profile


def no_solar(n: int = STEPS_24H) -> list[float]:
    return [0.0] * n


def cloudy_solar(peak_kw: float = 2.0, n: int = STEPS_24H) -> list[float]:
    return solar_bell(peak_kw=peak_kw, n=n)
