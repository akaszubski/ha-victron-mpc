"""Binary sensor entities for Victron MPC Battery Optimizer."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up Victron MPC binary sensors."""
    coordinator: VictronMPCCoordinator = entry.runtime_data

    async_add_entities([
        VictronMPCStaleDataSensor(coordinator, entry),
        VictronMPCSpikeActiveSensor(coordinator, entry),
        VictronMPCModbusHealthSensor(coordinator, entry),
    ])


class VictronMPCStaleDataSensor(
    CoordinatorEntity[VictronMPCCoordinator], BinarySensorEntity
):
    """Stale data — no coordinator update in >10 minutes."""

    _attr_has_entity_name = True
    _attr_translation_key = "stale_data"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:clock-alert-outline"

    def __init__(
        self,
        coordinator: VictronMPCCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mpc_stale_data"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if data is stale (problem)."""
        return not self.coordinator.last_update_success


class VictronMPCSpikeActiveSensor(
    CoordinatorEntity[VictronMPCCoordinator], BinarySensorEntity
):
    """Price spike — override is currently active."""

    _attr_has_entity_name = True
    _attr_translation_key = "spike_active"
    _attr_icon = "mdi:flash-alert"

    def __init__(
        self,
        coordinator: VictronMPCCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mpc_spike_active"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if spike override is active."""
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.get("spike_active", False)


class VictronMPCModbusHealthSensor(
    CoordinatorEntity[VictronMPCCoordinator], BinarySensorEntity
):
    """Modbus communication health."""

    _attr_has_entity_name = True
    _attr_translation_key = "modbus_healthy"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:lan-connect"

    def __init__(
        self,
        coordinator: VictronMPCCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mpc_modbus_healthy"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if Modbus is healthy (connected)."""
        return self.coordinator.modbus_healthy
