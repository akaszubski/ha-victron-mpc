"""Switch entities for Victron MPC Battery Optimizer."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VictronMPCCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Victron MPC switches."""
    coordinator: VictronMPCCoordinator = entry.runtime_data

    async_add_entities([
        VictronMPCShadowModeSwitch(coordinator, entry),
    ])


class VictronMPCShadowModeSwitch(
    CoordinatorEntity[VictronMPCCoordinator], SwitchEntity
):
    """Shadow mode — log decisions without writing Modbus registers."""

    _attr_has_entity_name = True
    _attr_translation_key = "shadow_mode"
    _attr_icon = "mdi:eye-outline"

    def __init__(
        self,
        coordinator: VictronMPCCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mpc_shadow_mode"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if shadow mode is active."""
        return self._entry.options.get("shadow_mode", True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable shadow mode — stop writing registers."""
        new_options = dict(self._entry.options)
        new_options["shadow_mode"] = True
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable shadow mode — start writing registers."""
        new_options = dict(self._entry.options)
        new_options["shadow_mode"] = False
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )
        self.async_write_ha_state()
