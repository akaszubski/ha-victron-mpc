"""Number entities for Victron MPC tunables.

Exposes key optimization parameters as adjustable sliders/boxes
in the HA UI. Changes take effect on the next coordinator cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VictronMPCCoordinator


@dataclass(frozen=True, kw_only=True)
class VictronMPCNumberDescription(NumberEntityDescription):
    """Describe a Victron MPC tunable number entity."""

    option_key: str  # Key in config entry options


NUMBER_DESCRIPTIONS: tuple[VictronMPCNumberDescription, ...] = (
    VictronMPCNumberDescription(
        key="mpc_battery_wear_cost",
        option_key="battery_wear_cost",
        translation_key="battery_wear_cost",
        native_min_value=0.01,
        native_max_value=0.10,
        native_step=0.01,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:battery-heart-variant",
    ),
    VictronMPCNumberDescription(
        key="mpc_sunset_reward",
        option_key="sunset_reward",
        translation_key="sunset_reward",
        native_min_value=0.01,
        native_max_value=0.10,
        native_step=0.01,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:weather-sunset",
    ),
    VictronMPCNumberDescription(
        key="mpc_overnight_hold_reward",
        option_key="overnight_hold_reward",
        translation_key="overnight_hold_reward",
        native_min_value=0.02,
        native_max_value=0.20,
        native_step=0.01,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:weather-night",
    ),
    VictronMPCNumberDescription(
        key="mpc_soc_floor",
        option_key="soc_floor_pct",
        translation_key="soc_floor",
        native_min_value=15,
        native_max_value=35,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        icon="mdi:battery-low",
    ),
    VictronMPCNumberDescription(
        key="mpc_overnight_min_soc",
        option_key="overnight_min_soc_pct",
        translation_key="overnight_min_soc",
        native_min_value=20,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        icon="mdi:battery-lock",
    ),
    VictronMPCNumberDescription(
        key="mpc_load_inflation",
        option_key="load_inflation_pct",
        translation_key="load_inflation",
        native_min_value=5,
        native_max_value=25,
        native_step=1,
        native_unit_of_measurement="%",
        mode=NumberMode.SLIDER,
        icon="mdi:trending-up",
    ),
    # Safety & override thresholds
    VictronMPCNumberDescription(
        key="mpc_spike_threshold",
        option_key="spike_threshold",
        translation_key="spike_threshold",
        native_min_value=0.50,
        native_max_value=5.00,
        native_step=0.10,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:flash-alert",
    ),
    VictronMPCNumberDescription(
        key="mpc_defensive_price",
        option_key="defensive_price",
        translation_key="defensive_price",
        native_min_value=0.50,
        native_max_value=5.00,
        native_step=0.10,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:shield-alert",
    ),
    VictronMPCNumberDescription(
        key="mpc_amber_blip_minutes",
        option_key="amber_blip_minutes",
        translation_key="amber_blip_minutes",
        native_min_value=1,
        native_max_value=15,
        native_step=1,
        native_unit_of_measurement="min",
        mode=NumberMode.SLIDER,
        icon="mdi:timer-sand",
    ),
    VictronMPCNumberDescription(
        key="mpc_feedin_export_threshold",
        option_key="feedin_export_threshold",
        translation_key="feedin_export_threshold",
        native_min_value=0.01,
        native_max_value=0.50,
        native_step=0.01,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:transmission-tower-export",
    ),
    # Overnight hold reward price thresholds
    VictronMPCNumberDescription(
        key="mpc_overnight_price_low",
        option_key="overnight_price_low",
        translation_key="overnight_price_low",
        native_min_value=0.05,
        native_max_value=0.30,
        native_step=0.01,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:cash-minus",
    ),
    VictronMPCNumberDescription(
        key="mpc_overnight_price_high",
        option_key="overnight_price_high",
        translation_key="overnight_price_high",
        native_min_value=0.15,
        native_max_value=0.50,
        native_step=0.01,
        native_unit_of_measurement="$/kWh",
        mode=NumberMode.BOX,
        icon="mdi:cash-plus",
    ),
)

# Defaults matching the working MPCTunables from config.py
_DEFAULTS: dict[str, float] = {
    "battery_wear_cost": 0.02,
    "sunset_reward": 0.04,
    "overnight_hold_reward": 0.10,
    "soc_floor_pct": 20,
    "overnight_min_soc_pct": 30,
    "load_inflation_pct": 10,
    "spike_threshold": 1.00,
    "defensive_price": 2.00,
    "amber_blip_minutes": 5,
    "feedin_export_threshold": 0.10,
    "overnight_price_low": 0.15,
    "overnight_price_high": 0.25,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Victron MPC number entities."""
    coordinator: VictronMPCCoordinator = entry.runtime_data

    async_add_entities(
        VictronMPCNumber(coordinator, entry, description)
        for description in NUMBER_DESCRIPTIONS
    )


class VictronMPCNumber(CoordinatorEntity[VictronMPCCoordinator], NumberEntity):
    """Tunable number entity — adjusts optimizer parameters from the UI."""

    entity_description: VictronMPCNumberDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VictronMPCCoordinator,
        entry: ConfigEntry,
        description: VictronMPCNumberDescription,
    ) -> None:
        """Initialize number entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
        }

    @property
    def native_value(self) -> float:
        """Return current value from config entry options."""
        return self._entry.options.get(
            self.entity_description.option_key,
            _DEFAULTS.get(self.entity_description.option_key, 0),
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update tunable — persists in config entry, takes effect next cycle."""
        new_options = dict(self._entry.options)
        new_options[self.entity_description.option_key] = value
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )
        self.async_write_ha_state()
