"""API clients for external services.

Each client is async and uses HA's shared aiohttp session.
Caching is in hass.data[DOMAIN] with configurable TTLs.

Modules:
    vrm — Victron VRM API (historical solar, consumption)
    open_meteo — Cloud layer data (low/mid/high altitude)
    modbus — Register write helpers
    fuel_price — PetrolSpy diesel pricing (optional)
"""
