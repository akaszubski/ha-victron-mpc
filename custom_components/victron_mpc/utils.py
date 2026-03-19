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
    price_low: float = 0.15,
    price_high: float = 0.25,
) -> float:
    """Scale overnight hold reward based on average overnight price.

    When grid is cheap (<price_low), full hold reward — preserve battery for
    morning spikes. When grid is moderate/expensive (>price_high), scale to
    zero — discharging is clearly more profitable than holding.

    Linear interpolation between price_low and price_high thresholds.

    Args:
        base_reward: Base overnight hold reward ($/kWh).
        buy_prices: Full price forecast array.
        overnight_steps: Step indices that fall within overnight hours.
        price_low: Full reward below this price (default $0.15).
        price_high: Zero reward above this price (default $0.25).
    """
    if not overnight_steps or base_reward <= 0:
        return base_reward

    overnight_prices = [buy_prices[i] for i in overnight_steps if i < len(buy_prices)]
    if not overnight_prices:
        return base_reward

    avg_price = sum(overnight_prices) / len(overnight_prices)

    if avg_price <= price_low:
        scale = 1.0
    elif avg_price >= price_high:
        scale = 0.0
    else:
        scale = (price_high - avg_price) / (price_high - price_low)

    scaled = round(base_reward * scale, 4)
    if abs(scaled - base_reward) > 0.001:
        log.info(
            "Overnight hold reward scaled: $%.3f → $%.3f "
            "(avg overnight price $%.2f)",
            base_reward, scaled, avg_price,
        )
    return scaled
