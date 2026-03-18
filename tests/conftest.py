"""Test fixtures for Victron MPC Battery Optimizer."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.victron_mpc.const import DOMAIN


@pytest.fixture
def mock_config_entry_data() -> dict:
    """Return minimal config entry data matching our working HA setup."""
    return {
        "modbus_host": "192.168.0.197",
        "modbus_port": 502,
        "modbus_slave_system": 100,
        "modbus_slave_vebus": 227,
        "battery_capacity_kwh": 14.2,
        "max_charge_kw": 3.5,
        "max_discharge_kw": 4.5,
        "soc_floor_pct": 20,
        "amber_price": "sensor.amber_general_price",
        "amber_forecast": "sensor.amber_general_forecast",
        "amber_feedin": "sensor.amber_feed_in_price",
        "amber_feedin_forecast": "sensor.amber_feed_in_forecast",
        "amber_spike": "binary_sensor.amber_price_spike",
        "battery_soc": "sensor.victron_battery_state_of_charge",
        "solar_power": "sensor.solar_power",
        "ac_consumption": "sensor.victron_ac_consumption",
        "grid_power": "sensor.victron_grid_power",
        "weather_entity": "weather.home",
        "vrm_token": "",
        "vrm_installation_id": "",
    }


@pytest.fixture
def mock_config_entry_options() -> dict:
    """Return default options (tunables) matching MPCTunables defaults."""
    return {
        "battery_wear_cost": 0.05,
        "sunset_reward": 0.04,
        "overnight_hold_reward": 0.10,
        "soc_floor_pct": 20,
        "overnight_min_soc_pct": 30,
        "shadow_mode": True,
    }
