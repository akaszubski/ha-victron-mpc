"""Sensor entities for Victron MPC Battery Optimizer.

Replaces the REST API sensor pushing from runner.py with native HA
entities backed by the DataUpdateCoordinator. All sensors auto-update
when the coordinator refreshes (every 5 minutes).

Entity pattern matches the existing sensor.mpc_* namespace that
automations and dashboards already reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VictronMPCCoordinator


@dataclass(frozen=True, kw_only=True)
class VictronMPCSensorDescription(SensorEntityDescription):
    """Describe a Victron MPC sensor."""

    attr_key: str  # Key in coordinator.data dict
    attributes_fn: Any = None  # Callable to extract extra_state_attributes


# Sensor definitions matching existing sensor.mpc_* entities
SENSOR_DESCRIPTIONS: tuple[VictronMPCSensorDescription, ...] = (
    VictronMPCSensorDescription(
        key="mpc_battery_plan",
        attr_key="battery_plan",
        translation_key="battery_plan",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-charging",
    ),
    VictronMPCSensorDescription(
        key="mpc_decision",
        attr_key="decision",
        translation_key="decision",
        icon="mdi:brain",
    ),
    VictronMPCSensorDescription(
        key="mpc_effective_price",
        attr_key="effective_price",
        translation_key="effective_price",
        native_unit_of_measurement="$/kWh",
        icon="mdi:currency-usd",
    ),
    VictronMPCSensorDescription(
        key="mpc_cost_24h",
        attr_key="cost_24h",
        translation_key="cost_24h",
        native_unit_of_measurement="$",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:cash-multiple",
    ),
    VictronMPCSensorDescription(
        key="mpc_solar_input",
        attr_key="solar_input_w",
        translation_key="solar_input",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power",
    ),
    VictronMPCSensorDescription(
        key="mpc_load_input",
        attr_key="load_input_w",
        translation_key="load_input",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-lightning-bolt",
    ),
    VictronMPCSensorDescription(
        key="mpc_buy_price",
        attr_key="buy_price",
        translation_key="buy_price",
        native_unit_of_measurement="$/kWh",
        icon="mdi:cash-minus",
    ),
    VictronMPCSensorDescription(
        key="mpc_sell_price",
        attr_key="sell_price",
        translation_key="sell_price",
        native_unit_of_measurement="$/kWh",
        icon="mdi:cash-plus",
    ),
    VictronMPCSensorDescription(
        key="mpc_cloud_coverage",
        attr_key="cloud_coverage",
        translation_key="cloud_coverage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-cloudy",
    ),
    VictronMPCSensorDescription(
        key="mpc_solar_forecast_today",
        attr_key="solar_forecast_today",
        translation_key="solar_forecast_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:solar-power-variant",
    ),
    VictronMPCSensorDescription(
        key="mpc_solve_time",
        attr_key="solve_time_ms",
        translation_key="solve_time",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
    ),
    VictronMPCSensorDescription(
        key="mpc_genai_health",
        attr_key="genai_health",
        translation_key="genai_health",
        icon="mdi:robot",
        attributes_fn=lambda data: {
            "summary": data.get("summary", ""),
            "details": data.get("details", ""),
            "last_check": datetime.now().isoformat(),
            "history": data.get("history", []),
        }
        if isinstance(data, dict)
        else {},
    ),
    VictronMPCSensorDescription(
        key="mpc_amber_forecast_accuracy",
        attr_key="amber_forecast_accuracy",
        translation_key="amber_forecast_accuracy",
        icon="mdi:chart-line",
        attributes_fn=lambda data: {
            "bias_by_hour": data.get("bias_by_hour", {}),
            "mae_by_horizon": data.get("mae_by_horizon", {}),
            "bias_by_horizon": data.get("bias_by_horizon", {}),
            "spike_accuracy": data.get("spike_accuracy", {}),
            "matched_pairs": data.get("matched_pairs", 0),
            "coverage_hours": data.get("coverage_hours", 0),
            "last_updated": data.get("last_updated", ""),
        }
        if isinstance(data, dict)
        else {},
    ),
    VictronMPCSensorDescription(
        key="mpc_appliance_monitor",
        attr_key="appliance_monitor",
        translation_key="appliance_monitor",
        icon="mdi:washing-machine",
        attributes_fn=lambda data: {
            "readings": data.get("readings", {}),
            "log_entries": data.get("log_entries", 0),
            "recent_history": data.get("recent_history", []),
        }
        if isinstance(data, dict)
        else {},
    ),
    VictronMPCSensorDescription(
        key="mpc_genai_audit_log",
        attr_key="genai_audit_log",
        translation_key="genai_audit_log",
        icon="mdi:clipboard-text-clock",
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=lambda data: {
            "entries": data.get("entries", []),
            "false_positive_count": data.get("false_positive_count", 0),
            "prompt_version": data.get("prompt_version", "unknown"),
            "current_version": data.get("current_version", "unknown"),
            "version_stats": data.get("version_stats", {}),
        }
        if isinstance(data, dict)
        else {},
    ),
    VictronMPCSensorDescription(
        key="mpc_human_feedback",
        attr_key="human_feedback",
        translation_key="human_feedback",
        icon="mdi:account-voice",
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=lambda data: {
            "entries": data.get("entries", []),
            "disagreement_stats": data.get("disagreement_stats", {}),
            "total_entries": data.get("total_entries", 0),
        }
        if isinstance(data, dict)
        else {},
    ),
    VictronMPCSensorDescription(
        key="mpc_modbus_write_log",
        attr_key="modbus_write_log",
        translation_key="modbus_write_log",
        icon="mdi:database-clock",
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=lambda data: {
            "entries": data.get("entries", []),
            "rogue_write_count": data.get("rogue_write_count", 0),
            "total_writes": data.get("total_writes", 0),
        }
        if isinstance(data, dict)
        else {},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Victron MPC sensors from config entry."""
    coordinator: VictronMPCCoordinator = entry.runtime_data

    async_add_entities(
        VictronMPCSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class VictronMPCSensor(CoordinatorEntity[VictronMPCCoordinator], SensorEntity):
    """Victron MPC sensor entity.

    Reads state from coordinator.data dict, keyed by description.attr_key.
    Rich attributes (SoC trajectory, price curves, forecast details) are
    passed through extra_state_attributes to match the existing sensor
    attribute structure that dashboards rely on.
    """

    entity_description: VictronMPCSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VictronMPCCoordinator,
        entry: ConfigEntry,
        description: VictronMPCSensorDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Victron MPC Battery Optimizer",
            "manufacturer": "Victron Energy",
            "model": "MPC Optimizer",
            "sw_version": "0.1.0",
        }

    @property
    def native_value(self) -> float | str | None:
        """Return sensor state from coordinator data."""
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data.get(self.entity_description.attr_key)
        if isinstance(data, dict):
            # If attributes_fn is set, the dict IS the raw data and
            # the state is extracted by convention (e.g. "status" for genai_health).
            if self.entity_description.attributes_fn is not None:
                return data.get("status") or data.get("state")
            return data.get("state")
        return data

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return rich attributes (trajectory, prices, forecast details).

        Matches the existing sensor.mpc_* attribute structure so that
        dashboards, automations, and monitoring continue to work.
        """
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data.get(self.entity_description.attr_key)
        if isinstance(data, dict):
            if self.entity_description.attributes_fn is not None:
                return self.entity_description.attributes_fn(data)
            return {k: v for k, v in data.items() if k != "state"}
        return None
