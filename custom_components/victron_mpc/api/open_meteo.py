"""Open-Meteo cloud layer client.

Fetches cloud_cover_low/mid/high from Open-Meteo free API for accurate
solar derating. High cirrus barely blocks solar while low stratus
blocks heavily — the layer breakdown is critical for accurate forecasts.

Ported from scripts/mpc/forecasts.py _fetch_cloud_layers() + _effective_cloud_pct().

API: https://api.open-meteo.com/v1/forecast (free, no key required)
"""

from __future__ import annotations

import time
from datetime import datetime

from aiohttp import ClientSession

from ..const import LOGGER

_CACHE_TTL = 1800  # 30 min — weather changes

# Default cloud layer weights (from working config.py MPCTunables)
DEFAULT_CLOUD_WEIGHTS = {
    "high": 0.15,   # Cirrus — thin ice crystals, minimal solar impact
    "mid": 0.5,     # Altostratus/altocumulus — moderate blocking
    "low": 0.9,     # Stratus/cumulus — thick, major solar reduction
}


class OpenMeteoClient:
    """Async Open-Meteo cloud layer client with caching.

    Ported from ForecastBuilder._fetch_cloud_layers().
    """

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
        self._cache: dict[int, dict[str, float]] | None = None
        self._cache_time: float = 0

    async def fetch_cloud_layers(
        self,
        now: datetime | None = None,
        forecast_hours: int = 24,
    ) -> dict[int, dict[str, float]] | None:
        """Fetch hourly cloud_cover_low/mid/high for next 24h.

        Returns: {hour_offset: {"low": %, "mid": %, "high": %}} or None.
        """
        # Check cache
        if self._cache and (time.monotonic() - self._cache_time) < _CACHE_TTL:
            return self._cache

        if now is None:
            now = datetime.now()

        try:
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={self._lat}&longitude={self._lon}"
                f"&hourly=cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high"
                f"&timezone=auto&forecast_days=2"
            )
            async with self._session.get(url, timeout=10) as resp:
                resp.raise_for_status()
                data = await resp.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            cover_low = hourly.get("cloud_cover_low", [])
            cover_mid = hourly.get("cloud_cover_mid", [])
            cover_high = hourly.get("cloud_cover_high", [])

            if not times or not cover_low:
                return None

            now_aware = now.astimezone()
            layers: dict[int, dict[str, float]] = {}

            for i, t_str in enumerate(times):
                try:
                    dt = datetime.fromisoformat(t_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=now_aware.tzinfo)
                    delta_h = (dt - now_aware).total_seconds() / 3600
                    if -0.5 <= delta_h < forecast_hours:
                        idx = max(0, int(delta_h))
                        layers[idx] = {
                            "low": float(cover_low[i]) if i < len(cover_low) else 0,
                            "mid": float(cover_mid[i]) if i < len(cover_mid) else 0,
                            "high": float(cover_high[i]) if i < len(cover_high) else 0,
                        }
                except (ValueError, TypeError):
                    continue

            if len(layers) < 6:
                return None

            self._cache = layers
            self._cache_time = time.monotonic()
            return layers

        except Exception:
            LOGGER.debug("Open-Meteo cloud layers unavailable", exc_info=True)
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
