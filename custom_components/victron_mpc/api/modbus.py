"""Modbus register helpers for Victron ESS.

Register 2901 (ESS min SoC): value = SoC% × 10, range 100-1000
  - Acts as FLOOR + TARGET: ESS discharges down to this, charges up from grid
  - To discharge: set LOW (e.g., 200 = 20%)
  - To charge from grid: set HIGH (above current SoC)
  - CRITICAL: Must set to planned discharge floor, not next-5-min target

Register 2706 (max grid feed-in): units = 100W/value (70 = 7000W, 0 = block)
"""

from __future__ import annotations

from ..const import REGISTER_ESS_MAX, REGISTER_ESS_MIN


def soc_to_register(soc_pct: float) -> int:
    """Convert SoC percentage (0-100) to Register 2901 value (100-1000).

    Matches _soc_to_register() from optimizer.py.
    """
    register = int(round(soc_pct * 10))
    return max(REGISTER_ESS_MIN, min(REGISTER_ESS_MAX, register))


def register_to_soc(register: int) -> float:
    """Convert Register 2901 value (100-1000) to SoC percentage (0-100)."""
    return register / 10.0
