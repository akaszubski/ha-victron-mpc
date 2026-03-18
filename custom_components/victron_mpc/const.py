"""Constants for Victron MPC Battery Optimizer."""

from __future__ import annotations

from logging import getLogger

LOGGER = getLogger(__package__)

DOMAIN = "victron_mpc"
ATTRIBUTION = "Optimized by Victron MPC Battery Optimizer"

# Platforms this integration provides
PLATFORMS = ["sensor", "number", "switch", "binary_sensor"]

# Config flow step IDs
CONF_MODBUS_HOST = "modbus_host"
CONF_MODBUS_PORT = "modbus_port"
CONF_MODBUS_SLAVE_SYSTEM = "modbus_slave_system"
CONF_MODBUS_SLAVE_VEBUS = "modbus_slave_vebus"

CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MAX_DISCHARGE_KW = "max_discharge_kw"
CONF_SOC_FLOOR_PCT = "soc_floor_pct"

CONF_AMBER_PRICE = "amber_price"
CONF_AMBER_FORECAST = "amber_forecast"
CONF_AMBER_FEEDIN = "amber_feedin"
CONF_AMBER_FEEDIN_FORECAST = "amber_feedin_forecast"
CONF_AMBER_SPIKE = "amber_spike"

CONF_BATTERY_SOC = "battery_soc"
CONF_SOLAR_POWER = "solar_power"
CONF_AC_CONSUMPTION = "ac_consumption"
CONF_GRID_POWER = "grid_power"
CONF_GENSET_POWER = "genset_power"
CONF_WEATHER_ENTITY = "weather_entity"

CONF_VRM_TOKEN = "vrm_token"
CONF_VRM_INSTALLATION_ID = "vrm_installation_id"

# Victron Modbus registers
REGISTER_ESS_MIN_SOC = 2901  # value = SoC% × 10, range 100-1000
REGISTER_MAX_FEED_IN = 2706  # units = 100W/value (70 = 7000W, 0 = block)

# Register ranges
REGISTER_ESS_MIN = 100  # 10% SoC
REGISTER_ESS_MAX = 1000  # 100% SoC
REGISTER_FEEDIN_MAX = 70  # 7000W
REGISTER_FEEDIN_BLOCK = 0  # No export

# Optimization cycle interval
UPDATE_INTERVAL_MINUTES = 5

# Stale data threshold
STALE_THRESHOLD_MINUTES = 10

# Default system parameters (from working VictronSystem config)
DEFAULT_BATTERY_CAPACITY_KWH = 14.2
DEFAULT_MAX_CHARGE_KW = 3.5
DEFAULT_MAX_DISCHARGE_KW = 4.5
DEFAULT_MAX_SOLAR_KW = 7.0
DEFAULT_MAX_GRID_IMPORT_KW = 10.0
DEFAULT_MAX_GRID_EXPORT_KW = 5.0
DEFAULT_INVERTER_MAX_KW = 5.0
DEFAULT_SOC_MIN_PCT = 10.0
DEFAULT_CHARGE_EFFICIENCY = 0.95
DEFAULT_DISCHARGE_EFFICIENCY = 0.95
