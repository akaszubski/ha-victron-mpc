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

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN as DOMAIN, LOGGER
from .coordinator import VictronMPCCoordinator

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
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_options(
    hass: HomeAssistant,
    entry: VictronMPCConfigEntry,
) -> None:
    """Handle options update — reload integration to pick up new tunables."""
    await hass.config_entries.async_reload(entry.entry_id)
