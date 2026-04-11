"""Utility functions for Victron MPC Battery Optimizer.

Functions that support the optimization cycle but aren't part of the
core LP solver. Ported from runner.py.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def scale_overnight_hold_reward(
    base_reward: float,
    buy_prices: list[float],
    overnight_steps: list[int],
    morning_steps: list[int],
    arbitrage_threshold: float = 0.10,
    battery_wear_cost: float = 0.02,
    discharge_penalty: float = 0.03,
) -> float:
    """Scale overnight hold reward by overnight-vs-morning price spread.

    Only scales the reward down when morning refill is *meaningfully cheaper*
    than discharging overnight — i.e. when real arbitrage is available. When
    overnight and morning prices are similar, the reward is preserved to
    prevent uneconomic discharge that burns battery wear cost without
    producing real savings.

    Economic reasoning:
        - Discharging saves overnight_price per kWh now
        - Refilling costs morning_price per kWh later
        - Battery wear costs ~$0.02/kWh
        - Round-trip efficiency ~5% loss
        - Break-even spread ≈ $0.035/kWh
        - Default threshold $0.10 gives ~2.8x safety margin over break-even
          to absorb Amber price-forecast noise (MAE ~$0.007/kWh)

    Fix for GitHub issue #80: previously scaled to zero when overnight price
    was absolutely expensive (>$0.25), which was inverted for sites where
    morning replacement ≈ overnight price. Battery drained to floor overnight
    and was still refilled at same-priced morning grid, losing $0.02/kWh wear
    cost for nothing.

    Args:
        base_reward: Base overnight hold reward ($/kWh).
        buy_prices: Full price forecast array.
        overnight_steps: Step indices that fall within overnight hours.
        morning_steps: Step indices that fall within morning refill window
            (e.g. 06:00-09:00 local time).
        arbitrage_threshold: Overnight-minus-morning spread ($/kWh) at which
            the reward is fully scaled to zero. Linear taper from 0 to
            this value. Default $0.10.
        battery_wear_cost: Battery cycle wear cost ($/kWh). Used to compute
            adaptive floor. Default $0.02.
        discharge_penalty: Combined soc_profile + grid_import penalty ($/kWh).
            Used to compute adaptive floor. Default $0.03.

    Returns:
        base_reward when spread ≤ 0 or morning data is missing,
        0 when spread ≥ arbitrage_threshold,
        linear interpolation in between.
    """
    if not overnight_steps or base_reward <= 0:
        return base_reward

    overnight_prices = [buy_prices[i] for i in overnight_steps if i < len(buy_prices)]
    morning_prices = [buy_prices[i] for i in morning_steps if i < len(buy_prices)]
    if not overnight_prices or not morning_prices:
        # Missing data — default to preservation (safer)
        return base_reward

    overnight_avg = sum(overnight_prices) / len(overnight_prices)
    morning_avg = sum(morning_prices) / len(morning_prices)
    spread = overnight_avg - morning_avg

    if spread <= 0:
        scale = 1.0
    elif spread >= arbitrage_threshold:
        scale = 0.0
    else:
        scale = 1.0 - (spread / arbitrage_threshold)

    scaled = round(base_reward * scale, 4)

    # Price-adaptive floor: ensure hold penalty + wear + soc_profile makes
    # overnight discharge unprofitable. Without this, LP rationally drains
    # battery when grid price > total penalties. GitHub issue #80.
    # Only apply when spread is below arbitrage threshold — when genuine
    # arbitrage exists (morning much cheaper), LP should be free to discharge.
    adaptive_floor = max(0.0, overnight_avg - battery_wear_cost - discharge_penalty)
    if spread < arbitrage_threshold and scaled < adaptive_floor:
        log.info(
            "Overnight hold reward floored: $%.3f → $%.3f "
            "(overnight avg $%.3f, adaptive floor from price)",
            scaled, adaptive_floor, overnight_avg,
        )
        scaled = adaptive_floor

    if abs(scaled - base_reward) > 0.001:
        log.info(
            "Overnight hold reward scaled: $%.3f → $%.3f "
            "(overnight avg $%.3f, morning avg $%.3f, spread $%.3f)",
            base_reward, scaled, overnight_avg, morning_avg, spread,
        )
    return scaled
