"""Config flow for Victron MPC Battery Optimizer.

Multi-step wizard:
1. Victron Modbus connection (host, port, unit IDs)
2. Battery system specs (capacity, charge/discharge rates)
3. Amber Electric entity pickers
4. Victron sensor entity pickers
5. VRM API credentials (optional)
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AC_CONSUMPTION,
    CONF_AMBER_FEEDIN,
    CONF_AMBER_FEEDIN_FORECAST,
    CONF_AMBER_FORECAST,
    CONF_AMBER_PRICE,
    CONF_AMBER_SPIKE,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC,
    CONF_GENSET_POWER,
    CONF_GRID_POWER,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODBUS_SLAVE_SYSTEM,
    CONF_MODBUS_SLAVE_VEBUS,
    CONF_SOC_FLOOR_PCT,
    CONF_SOLAR_POWER,
    CONF_VRM_INSTALLATION_ID,
    CONF_VRM_TOKEN,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_MAX_CHARGE_KW,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_SOC_MIN_PCT,
    DOMAIN,
)

# Reusable entity selectors matching our working HA entity patterns
_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)
_POWER_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        domain="sensor", device_class="power"
    )
)
_BATTERY_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        domain="sensor", device_class="battery"
    )
)
_BINARY_SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="binary_sensor")
)
_WEATHER_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="weather")
)


class VictronMPCConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Victron MPC Battery Optimizer."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Victron Modbus connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Prevent duplicate entries for same Modbus host
            self._async_abort_entries_match(
                {CONF_MODBUS_HOST: user_input[CONF_MODBUS_HOST]}
            )

            # TODO: Validate Modbus connection
            # try:
            #     client = AsyncModbusTcpClient(
            #         user_input[CONF_MODBUS_HOST],
            #         port=user_input[CONF_MODBUS_PORT],
            #     )
            #     await client.connect()
            #     result = await client.read_holding_registers(
            #         address=2901, count=1, slave=user_input[CONF_MODBUS_SLAVE_SYSTEM]
            #     )
            #     if result.isError():
            #         errors["base"] = "modbus_read_failed"
            #     await client.close()
            # except Exception:
            #     errors["base"] = "cannot_connect"

            if not errors:
                self._data.update(user_input)
                return await self.async_step_battery()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default="192.168.0.197"): str,
                    vol.Required(CONF_MODBUS_PORT, default=502): int,
                    vol.Required(CONF_MODBUS_SLAVE_SYSTEM, default=100): int,
                    vol.Required(CONF_MODBUS_SLAVE_VEBUS, default=227): int,
                }
            ),
            errors=errors,
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Battery system specifications."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_amber()

        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BATTERY_CAPACITY_KWH,
                        default=DEFAULT_BATTERY_CAPACITY_KWH,
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_MAX_CHARGE_KW, default=DEFAULT_MAX_CHARGE_KW
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_MAX_DISCHARGE_KW, default=DEFAULT_MAX_DISCHARGE_KW
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_SOC_FLOOR_PCT, default=int(DEFAULT_SOC_MIN_PCT)
                    ): vol.All(int, vol.Range(min=10, max=50)),
                }
            ),
        )

    async def async_step_amber(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Amber Electric entity pickers."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_victron_sensors()

        return self.async_show_form(
            step_id="amber",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AMBER_PRICE,
                        default="sensor.amber_general_price",
                    ): _SENSOR_SELECTOR,
                    vol.Required(
                        CONF_AMBER_FORECAST,
                        default="sensor.amber_general_forecast",
                    ): _SENSOR_SELECTOR,
                    vol.Required(
                        CONF_AMBER_FEEDIN,
                        default="sensor.amber_feed_in_price",
                    ): _SENSOR_SELECTOR,
                    vol.Required(
                        CONF_AMBER_FEEDIN_FORECAST,
                        default="sensor.amber_feed_in_forecast",
                    ): _SENSOR_SELECTOR,
                    vol.Required(
                        CONF_AMBER_SPIKE,
                        default="binary_sensor.amber_price_spike",
                    ): _BINARY_SENSOR_SELECTOR,
                }
            ),
        )

    async def async_step_victron_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: Victron sensor entity pickers."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_vrm()

        return self.async_show_form(
            step_id="victron_sensors",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BATTERY_SOC,
                        default="sensor.victron_battery_state_of_charge",
                    ): _BATTERY_SENSOR_SELECTOR,
                    vol.Required(
                        CONF_SOLAR_POWER,
                        default="sensor.solar_power",
                    ): _POWER_SENSOR_SELECTOR,
                    vol.Required(
                        CONF_AC_CONSUMPTION,
                        default="sensor.victron_ac_consumption",
                    ): _POWER_SENSOR_SELECTOR,
                    vol.Required(
                        CONF_GRID_POWER,
                        default="sensor.victron_grid_power",
                    ): _POWER_SENSOR_SELECTOR,
                    vol.Optional(
                        CONF_GENSET_POWER,
                    ): _POWER_SENSOR_SELECTOR,
                    vol.Required(
                        CONF_WEATHER_ENTITY,
                        default="weather.home",
                    ): _WEATHER_SELECTOR,
                }
            ),
        )

    async def async_step_vrm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: VRM API credentials (optional)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            vrm_token = user_input.get(CONF_VRM_TOKEN, "")
            vrm_id = user_input.get(CONF_VRM_INSTALLATION_ID, "")

            if vrm_token and vrm_id:
                # TODO: Validate VRM API connection
                # try:
                #     session = async_get_clientsession(self.hass)
                #     resp = await session.get(
                #         f"https://vrmapi.victronenergy.com/v2/installations/{vrm_id}/summary",
                #         headers={"X-Authorization": f"Token {vrm_token}"},
                #     )
                #     if resp.status != 200:
                #         errors["base"] = "vrm_auth_failed"
                # except Exception:
                #     errors["base"] = "vrm_connect_failed"
                pass

            if not errors:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=f"Victron MPC ({self._data[CONF_MODBUS_HOST]})",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="vrm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_VRM_TOKEN, default=""): str,
                    vol.Optional(CONF_VRM_INSTALLATION_ID, default=""): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> VictronMPCOptionsFlow:
        """Get the options flow handler."""
        return VictronMPCOptionsFlow(config_entry)


class VictronMPCOptionsFlow(OptionsFlow):
    """Handle options flow for tunables."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options — battery economics and optimization tunables."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    # Battery economics
                    vol.Optional(
                        "battery_wear_cost",
                        default=current.get("battery_wear_cost", 0.05),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.01, max=0.10, step=0.01, mode="box"
                        )
                    ),
                    vol.Optional(
                        "sunset_reward",
                        default=current.get("sunset_reward", 0.04),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.01, max=0.10, step=0.01, mode="box"
                        )
                    ),
                    vol.Optional(
                        "overnight_hold_reward",
                        default=current.get("overnight_hold_reward", 0.10),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.02, max=0.20, step=0.01, mode="box"
                        )
                    ),
                    vol.Optional(
                        "soc_floor_pct",
                        default=current.get("soc_floor_pct", 20),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=15, max=30, step=1, mode="slider"
                        )
                    ),
                    vol.Optional(
                        "overnight_min_soc_pct",
                        default=current.get("overnight_min_soc_pct", 30),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=20, max=45, step=1, mode="slider"
                        )
                    ),
                    # Operating mode
                    vol.Optional(
                        "shadow_mode",
                        default=current.get("shadow_mode", True),
                    ): selector.BooleanSelector(),
                }
            ),
        )
