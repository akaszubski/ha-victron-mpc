"""Victron MPC Battery Optimizer.

LP-optimized battery dispatch for Victron ESS with Amber Electric
wholesale pricing. Replaces manual tier-based automations with a
24-hour rolling optimization that minimizes electricity cost while
respecting battery health and physical constraints.

Architecture:
    DataUpdateCoordinator runs every 5 minutes:
    1. Read HA entities (SoC, solar, load, Amber prices, weather)
    2. Fetch external APIs (VRM historical, Open-Meteo cloud layers)
    3. Build forecasts (solar, load, price — 288 × 5-min steps)
    4. Run LP optimizer (scipy HiGHS, ~50ms)
    5. Apply overrides (spike, negative pricing, stale safety)
    6. Write Modbus registers (R2901 ESS min SoC, R2706 feed-in limit)
    7. Update sensor entities with decision + context
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN as DOMAIN, LOGGER
from .coordinator import VictronMPCCoordinator

SERVICE_EMERGENCY_STOP = "emergency_stop"
SERVICE_EMERGENCY_RESUME = "emergency_resume"
SERVICE_RECORD_FEEDBACK = "record_feedback"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
]

type VictronMPCConfigEntry = ConfigEntry[VictronMPCCoordinator]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VictronMPCConfigEntry,
) -> bool:
    """Set up Victron MPC from a config entry."""
    coordinator = VictronMPCCoordinator(hass, entry)

    # First refresh — validates connectivity and loads initial data.
    # Raises ConfigEntryNotReady if Modbus or APIs unreachable.
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator for entity platforms to access
    entry.runtime_data = coordinator

    # Forward setup to sensor, number, switch, binary_sensor platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload on options change (tunables adjusted from UI)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # Register services (emergency stop/resume)
    _register_services(hass, coordinator)

    LOGGER.info(
        "Victron MPC initialized — %s mode, %d-min cycle",
        "shadow" if entry.options.get("shadow_mode", True) else "active",
        coordinator.update_interval.total_seconds() / 60,
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: VictronMPCConfigEntry,
) -> bool:
    """Unload Victron MPC config entry."""
    # Remove services when the last entry is unloaded
    hass.services.async_remove(DOMAIN, SERVICE_EMERGENCY_STOP)
    hass.services.async_remove(DOMAIN, SERVICE_EMERGENCY_RESUME)
    hass.services.async_remove(DOMAIN, SERVICE_RECORD_FEEDBACK)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _register_services(
    hass: HomeAssistant, coordinator: VictronMPCCoordinator,
) -> None:
    """Register emergency stop/resume services.

    These allow one-button MPC disable via HA service call, developer
    tools, or automation. The services write safe register values and
    pause/resume the optimization loop.
    """

    async def _handle_emergency_stop(call: ServiceCall) -> None:
        """Handle emergency_stop service call."""
        await coordinator.async_emergency_stop()

    async def _handle_emergency_resume(call: ServiceCall) -> None:
        """Handle emergency_resume service call."""
        await coordinator.async_emergency_resume()

    if not hass.services.has_service(DOMAIN, SERVICE_EMERGENCY_STOP):
        hass.services.async_register(
            DOMAIN, SERVICE_EMERGENCY_STOP, _handle_emergency_stop, schema=vol.Schema({}),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_EMERGENCY_RESUME):
        hass.services.async_register(
            DOMAIN, SERVICE_EMERGENCY_RESUME, _handle_emergency_resume, schema=vol.Schema({}),
        )

    async def _handle_record_feedback(call: ServiceCall) -> None:
        """Handle record_feedback service call."""
        await coordinator.record_human_feedback(
            human_verdict=call.data["human_verdict"],
            human_reasoning=call.data["human_reasoning"],
            uat_verdict=call.data.get("uat_verdict", ""),
            genai_verdict=call.data.get("genai_verdict", ""),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RECORD_FEEDBACK):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RECORD_FEEDBACK,
            _handle_record_feedback,
            schema=vol.Schema({
                vol.Required("human_verdict"): vol.In(["agree", "disagree", "override"]),
                vol.Required("human_reasoning"): str,
                vol.Optional("uat_verdict", default=""): str,
                vol.Optional("genai_verdict", default=""): str,
            }),
        )
    LOGGER.info("Registered services: %s.%s, %s.%s, %s.%s", DOMAIN, SERVICE_EMERGENCY_STOP, DOMAIN, SERVICE_EMERGENCY_RESUME, DOMAIN, SERVICE_RECORD_FEEDBACK)


async def _async_update_options(
    hass: HomeAssistant,
    entry: VictronMPCConfigEntry,
) -> None:
    """Handle options update — reload integration to pick up new tunables."""
    await hass.config_entries.async_reload(entry.entry_id)
