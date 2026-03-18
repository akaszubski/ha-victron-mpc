"""Diagnostics for Victron MPC Battery Optimizer.

Provides sanitized config dump downloadable from HA Settings UI.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_VRM_TOKEN

REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    # Sanitize config data — redact secrets
    safe_data = dict(entry.data)
    if CONF_VRM_TOKEN in safe_data:
        safe_data[CONF_VRM_TOKEN] = REDACTED

    safe_options = dict(entry.options)

    diag: dict[str, Any] = {
        "config_entry": {
            "data": safe_data,
            "options": safe_options,
        },
        "coordinator": {
            "cycle_count": coordinator._cycle_count,
            "consecutive_failures": coordinator._consecutive_failures,
            "last_update_success": coordinator.last_update_success,
            "last_register_value": coordinator._last_register_value,
            "last_feedin_value": coordinator._last_feedin_value,
        },
        "api_health": coordinator._api_health,
    }

    if coordinator.data:
        # Include last decision (non-sensitive)
        diag["last_decision"] = {
            k: v for k, v in coordinator.data.items()
            if k in ("battery_plan", "decision", "solve_time_ms", "spike_active")
        }

    return diag
