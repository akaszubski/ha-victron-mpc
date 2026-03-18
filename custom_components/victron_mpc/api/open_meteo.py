"""Open-Meteo cloud layer client.

Fetches cloud_cover_low/mid/high from Open-Meteo free API for accurate
solar derating. High cirrus barely blocks solar while low stratus
blocks heavily — the layer breakdown is critical for accurate forecasts.

API: https://api.open-meteo.com/v1/forecast (free, no key required)
"""

from __future__ import annotations

import time
from typing import Any

from aiohttp import ClientSession

from ..const import LOGGER

_CACHE_TTL = 1800  # 30 min — weather changes

# Default cloud layer weights (from working config.py)
DEFAULT_CLOUD_WEIGHTS = {
    "high": 0.15,   # Cirrus — thin ice crystals, minimal solar impact
    "mid": 0.5,     # Altostratus/altocumulus — moderate blocking
    "low": 0.9,     # Stratus/cumulus — thick, major solar reduction
}


class OpenMeteoClient:
    """Async Open-Meteo cloud layer client with caching."""

    def __init__(
        self,
        session: ClientSession,
        latitude: float,
        longitude: float,
    ) -> None:
        """Initialize client."""
        self._session = session
        self._lat = latitude
        self._lon = longitude
        self._cache: dict[str, Any] | None = None
        self._cache_time: float = 0

    async def fetch_cloud_layers(
        self,
    ) -> dict[int, dict[str, float]] | None:
        """Fetch hourly cloud_cover_low/mid/high for next 24h.

        Returns: {hour: {"low": %, "mid": %, "high": %}} or None on failure.
        """
        # Check cache
        if self._cache and (time.monotonic() - self._cache_time) < _CACHE_TTL:
            return self._cache

        # TODO: Port from scripts/mpc/forecasts.py _fetch_cloud_layers()
        # url = (
        #     f"https://api.open-meteo.com/v1/forecast"
        #     f"?latitude={self._lat}&longitude={self._lon}"
        #     f"&hourly=cloud_cover_low,cloud_cover_mid,cloud_cover_high"
        #     f"&forecast_days=2&timezone=auto"
        # )
        # async with self._session.get(url) as resp:
        #     data = await resp.json()
        #     ...

        LOGGER.debug("Open-Meteo cloud layer fetch not yet implemented")
        return None

    @staticmethod
    def effective_cloud_pct(
        layers: dict[str, float],
        weights: dict[str, float] | None = None,
    ) -> float:
        """Compute effective cloud percentage from layer breakdown.

        Uses weighted average model matching forecasts.py _effective_cloud_pct():
        effective = Σ(cloud_i × weight_i) / Σ(100 × weight_i) × 100

        Weights each layer by its solar impact:
          - high (cirrus): 0.15 — barely blocks solar
          - mid (altostratus): 0.5 — moderate blocking
          - low (stratus): 0.9 — heavy blocking
        """
        if weights is None:
            weights = DEFAULT_CLOUD_WEIGHTS

        weighted_sum = 0.0
        total_weight = 0.0
        for layer, weight in weights.items():
            pct = layers.get(layer, 0.0)
            weighted_sum += pct * weight
            total_weight += 100.0 * weight

        if total_weight == 0:
            return 0.0
        return min(100.0, round(weighted_sum / total_weight * 100.0, 1))
