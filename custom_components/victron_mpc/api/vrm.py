"""Victron VRM API client.

Async client for VRM historical solar data and consumption forecasts.
Uses HA's shared aiohttp session for connection pooling.

API: https://vrmapi.victronenergy.com/v2
Auth: X-Authorization: Token {vrm_token}
"""

from __future__ import annotations

import time
from typing import Any

from aiohttp import ClientSession

from ..const import LOGGER

# Cache TTLs (seconds)
_CLEARSKY_TTL = 86400  # 24h — historical data changes slowly
_MONTHLY_CONSUMPTION_TTL = 86400  # 24h
_HOURLY_FORECAST_TTL = 3600  # 1h — forecast updates
_HISTORICAL_STATS_TTL = 21600  # 6h


class VRMClient:
    """Async VRM API client with in-memory caching."""

    def __init__(
        self,
        session: ClientSession,
        access_token: str,
        installation_id: str,
        base_url: str = "https://vrmapi.victronenergy.com/v2",
    ) -> None:
        """Initialize VRM client."""
        self._session = session
        self._token = access_token
        self._installation_id = installation_id
        self._base_url = base_url
        self._headers = {"X-Authorization": f"Token {access_token}"}
        self._cache: dict[str, dict[str, Any]] = {}

    def _cache_get(self, key: str, ttl: int) -> Any | None:
        """Return cached value if fresh, else None."""
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry["time"]) < ttl:
            return entry["data"]
        return None

    def _cache_set(self, key: str, data: Any) -> None:
        """Store value in cache."""
        self._cache[key] = {"data": data, "time": time.monotonic()}

    @property
    def available(self) -> bool:
        """Return True if VRM credentials are configured."""
        return bool(self._token and self._installation_id)

    async def get_clearsky_envelope(
        self, percentile: float = 0.90
    ) -> dict[int, list[float]] | None:
        """Fetch 180-day hourly solar actuals, grouped by (month, hour) at percentile.

        Returns: {month: [kw_h0, kw_h1, ..., kw_h23]} or None on failure.
        """
        # TODO: Port from scripts/mpc/forecasts.py VRMClient.get_clearsky_envelope
        cached = self._cache_get("clearsky", _CLEARSKY_TTL)
        if cached is not None:
            return cached

        LOGGER.debug("VRM clearsky envelope fetch not yet implemented")
        return None

    async def get_monthly_consumption(
        self, months_back: int = 12
    ) -> dict[int, float]:
        """Fetch 365 days daily stats, aggregate consumption by month.

        Returns: {1: avg_kwh_jan, 2: avg_kwh_feb, ...}
        """
        # TODO: Port from scripts/mpc/forecasts.py VRMClient.get_monthly_consumption
        cached = self._cache_get("monthly_consumption", _MONTHLY_CONSUMPTION_TTL)
        if cached is not None:
            return cached

        LOGGER.debug("VRM monthly consumption fetch not yet implemented")
        return {}

    async def get_hourly_forecasts(
        self, days_ahead: int = 2
    ) -> dict[str, list] | None:
        """Fetch VRM's own solar + consumption forecast (hourly).

        Returns: {"solar_hourly": [(ts_ms, wh), ...], "consumption_hourly": [...]}
        """
        # TODO: Port from scripts/mpc/forecasts.py VRMClient.get_hourly_forecasts
        cached = self._cache_get("hourly_forecast", _HOURLY_FORECAST_TTL)
        if cached is not None:
            return cached

        LOGGER.debug("VRM hourly forecast fetch not yet implemented")
        return None

    async def get_historical_stats(
        self, days_back: int = 30
    ) -> dict | None:
        """Fetch daily solar yield + consumption stats.

        Returns: dict with daily summary data.
        """
        # TODO: Port from scripts/mpc/forecasts.py VRMClient.get_historical_stats
        cached = self._cache_get("historical_stats", _HISTORICAL_STATS_TTL)
        if cached is not None:
            return cached

        LOGGER.debug("VRM historical stats fetch not yet implemented")
        return None
