"""Solcast API client for satellite-based solar forecasting.

Async client using HA's shared aiohttp session. Returns P50/P90 kW estimates
at 30-min resolution that already account for clouds, shading, and panel config.

Free tier: 10 API calls/day. Cache for ~70 min = ~10 calls during daylight.

Ported from scripts/mpc/forecasts.py SolcastClient — sync requests to async aiohttp,
file-based cache to in-memory TTL cache.

API: https://api.solcast.com.au
Auth: Bearer {api_key}
"""

from __future__ import annotations

import time
from typing import Any

from aiohttp import ClientSession

from ..const import LOGGER

_CACHE_TTL = 4200  # 70 min — ~10 API calls per day during daylight


class SolcastClient:
    """Async Solcast API client with in-memory caching.

    Solcast provides satellite-based solar forecasts calibrated to your
    specific rooftop site. The P50/P90 ratio per period tells us how much
    cloud Solcast's satellite imagery expects — used as a weather derate
    signal that replaces met.no's regional forecast.
    """

    def __init__(
        self,
        session: ClientSession,
        api_key: str,
        site_id: str,
        base_url: str = "https://api.solcast.com.au",
        cache_max_age_seconds: int = _CACHE_TTL,
    ) -> None:
        """Initialize Solcast client."""
        self._session = session
        self._api_key = api_key
        self._site_id = site_id
        self._base_url = base_url
        self._cache_ttl = cache_max_age_seconds
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._cache: list[dict[str, Any]] | None = None
        self._cache_time: float = 0

    @property
    def available(self) -> bool:
        """Return True if Solcast credentials are configured."""
        return bool(self._api_key and self._site_id)

    async def get_forecasts(self) -> list[dict[str, Any]] | None:
        """Fetch 24h solar forecast, using cache if fresh enough.

        Returns list of forecast periods with pv_estimate, pv_estimate90,
        period_end, etc. Returns None on failure.
        """
        if not self.available:
            return None

        # Check cache
        if self._cache and (time.monotonic() - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            async with self._session.get(
                f"{self._base_url}/rooftop_sites/{self._site_id}/forecasts",
                params={"format": "json", "hours": 24},
                headers=self._headers,
                timeout=15,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            forecasts = data.get("forecasts", [])
            if forecasts:
                self._cache = forecasts
                self._cache_time = time.monotonic()
                return forecasts
            return None

        except Exception:
            LOGGER.debug("Solcast forecast fetch failed", exc_info=True)
            # API failed — return stale cache as last resort
            if self._cache:
                LOGGER.debug("Using stale Solcast cache as fallback")
                return self._cache
            return None
