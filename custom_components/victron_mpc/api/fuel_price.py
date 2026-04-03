"""PetrolSpy fuel price client.

Fetches local diesel prices for genset cost calculation.
Ported from scripts/mpc/forecasts.py FuelPriceClient.

API: PetrolSpy (free, no key required, Australian fuel stations)
"""

from __future__ import annotations

import time
from typing import Any

from aiohttp import ClientSession

from ..const import LOGGER

_CACHE_TTL = 86400  # 24h — diesel prices don't change often
_BASE_URL = "https://petrolspy.com.au/webservice-1/station/box"
_DIESEL_KEYS = ("DIESEL", "Diesel", "PremDSL")


class FuelPriceClient:
    """Async PetrolSpy diesel price client with in-memory caching.

    Ported from scripts/mpc/forecasts.py FuelPriceClient.
    Queries PetrolSpy for nearby stations, returns median diesel price.
    """

    def __init__(
        self,
        session: ClientSession,
        *,
        ne_lat: float = -37.75,
        ne_lng: float = 145.15,
        sw_lat: float = -37.85,
        sw_lng: float = 145.05,
    ) -> None:
        """Initialize with bounding box for nearby fuel station search."""
        self._session = session
        self._params = {
            "neLat": ne_lat,
            "neLng": ne_lng,
            "swLat": sw_lat,
            "swLng": sw_lng,
            "fuelType": "Diesel",
        }
        self._cache: float | None = None
        self._cache_time: float = 0

    async def get_diesel_price(self) -> float | None:
        """Get median local diesel price in $/litre.

        Returns cached value if <24h old, otherwise fetches fresh.
        Returns None on failure (caller should keep existing default).
        """
        if self._cache and (time.monotonic() - self._cache_time) < _CACHE_TTL:
            return self._cache

        try:
            async with self._session.get(
                _BASE_URL,
                params=self._params,
                headers={"User-Agent": "Mozilla/5.0 (MPC-Optimizer)"},
                timeout=15,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            price = self._extract_median_diesel(data)
            if price is not None:
                self._cache = price
                self._cache_time = time.monotonic()
            return price

        except Exception:
            LOGGER.debug("PetrolSpy diesel price fetch failed", exc_info=True)
            return self._cache  # Return stale cache on failure

    @staticmethod
    def _extract_median_diesel(data: dict[str, Any]) -> float | None:
        """Extract median diesel price from PetrolSpy response.

        PetrolSpy returns prices in cents/litre (e.g. 225.9).
        We convert to $/litre and take the median for robustness.
        """
        stations = data.get("message", {}).get("list", [])
        amounts: list[float] = []
        for station in stations:
            prices = station.get("prices", {})
            for key in _DIESEL_KEYS:
                diesel = prices.get(key)
                if diesel and diesel.get("amount"):
                    amounts.append(float(diesel["amount"]))
                    break

        if not amounts:
            return None

        amounts.sort()
        median_cents = amounts[len(amounts) // 2]
        return median_cents / 100  # cents → dollars
