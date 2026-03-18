"""Victron VRM API client.

Async client for VRM historical solar data and consumption forecasts.
Uses HA's shared aiohttp session for connection pooling.

Ported from scripts/mpc/forecasts.py VRMClient — sync requests → async aiohttp.

API: https://vrmapi.victronenergy.com/v2
Auth: X-Authorization: Token {vrm_token}
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientSession

from ..const import LOGGER

# Cache TTLs (seconds)
_CLEARSKY_TTL = 86400  # 24h — historical data changes slowly
_MONTHLY_CONSUMPTION_TTL = 86400  # 24h
_HOURLY_FORECAST_TTL = 3600  # 1h — forecast updates
_HISTORICAL_STATS_TTL = 21600  # 6h
_MONTHLY_PEAK_TTL = 86400  # 24h


class VRMClient:
    """Async VRM API client with in-memory caching.

    Ported from scripts/mpc/forecasts.py VRMClient.
    All methods converted from sync requests to async aiohttp.
    Cache moved from /tmp files to in-memory dict with TTL.
    """

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

    async def get_hourly_forecasts(self, days_ahead: int = 2) -> dict:
        """Fetch hourly solar + consumption forecasts from VRM.

        Returns:
            {"solar_hourly": [(timestamp_ms, wh), ...],
             "consumption_hourly": [(timestamp_ms, wh), ...]}
        """
        empty = {"solar_hourly": [], "consumption_hourly": []}
        if not self.available:
            return empty

        cached = self._cache_get("hourly_forecast", _HOURLY_FORECAST_TTL)
        if cached is not None:
            return cached

        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=days_ahead + 1)

        try:
            async with self._session.get(
                f"{self._base_url}/installations/{self._installation_id}/stats",
                params={
                    "type": "forecast",
                    "interval": "hours",
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                },
                headers=self._headers,
                timeout=15,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if not data.get("success"):
                return empty

            records = data.get("records", {})
            solar = records.get("solar_yield_forecast", [])
            mppt = records.get("vrm_pv_charger_yield_fc", [])
            consumption = records.get("vrm_consumption_fc", [])

            # Merge solar + MPPT charger data
            solar_by_ts: dict[int, float] = {}
            for entry in solar + mppt:
                if isinstance(entry, list) and len(entry) >= 2:
                    ts, wh = entry[0], entry[1]
                    solar_by_ts[ts] = solar_by_ts.get(ts, 0) + wh

            result = {
                "solar_hourly": sorted(solar_by_ts.items()),
                "consumption_hourly": [
                    (e[0], e[1]) for e in consumption
                    if isinstance(e, list) and len(e) >= 2
                ],
            }
            self._cache_set("hourly_forecast", result)
            return result

        except Exception:
            LOGGER.debug("VRM hourly forecast fetch failed", exc_info=True)
            return empty

    async def get_monthly_consumption(self, months_back: int = 12) -> dict[int, float]:
        """Fetch monthly average daily consumption from VRM.

        Returns dict mapping month number (1-12) to average daily kWh.
        """
        if not self.available:
            return {}

        cached = self._cache_get("monthly_consumption", _MONTHLY_CONSUMPTION_TTL)
        if cached is not None:
            return cached

        now = datetime.now()
        days_back = min(months_back * 30, 365)
        start = now - timedelta(days=days_back)

        try:
            async with self._session.get(
                f"{self._base_url}/installations/{self._installation_id}/stats",
                params={
                    "type": "kwh",
                    "start": int(start.timestamp()),
                    "end": int(now.timestamp()),
                    "interval": "days",
                },
                headers=self._headers,
                timeout=30,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            records = data.get("records", {})
            if not isinstance(records, dict):
                return {}

            gc_data = records.get("Gc", [])
            pc_data = records.get("Pc", [])
            bc_data = records.get("Bc", [])

            if not (gc_data and pc_data and bc_data):
                return {}

            monthly_totals: dict[int, list[float]] = {m: [] for m in range(1, 13)}
            for gc, pc, bc in zip(gc_data, pc_data, bc_data):
                try:
                    ts = gc[0] / 1000 if gc[0] > 1e12 else gc[0]
                    dt_obj = datetime.fromtimestamp(ts)
                    daily_kwh = gc[1] + pc[1] + bc[1]
                    if 0 < daily_kwh < 200:
                        monthly_totals[dt_obj.month].append(daily_kwh)
                except (IndexError, ValueError, TypeError):
                    continue

            result = {}
            for month, values in monthly_totals.items():
                if values:
                    result[month] = sum(values) / len(values)

            self._cache_set("monthly_consumption", result)
            return result

        except Exception:
            LOGGER.debug("VRM monthly consumption fetch failed", exc_info=True)
            return {}

    async def get_clearsky_envelope(
        self, percentile: float = 0.90,
    ) -> dict[int, list[float]] | None:
        """Build production envelope from 180 days of hourly actuals.

        Returns the specified percentile of hourly production by month.
        P90 = clear-sky. P70 = partly cloudy. P40 = overcast. P15 = rain.

        Returns:
            {month: [kw_hour_0, ..., kw_hour_23]} or None
        """
        if not self.available:
            return None

        cache_key = f"clearsky_p{int(percentile * 100)}"
        cached = self._cache_get(cache_key, _CLEARSKY_TTL)
        if cached is not None:
            return cached

        now = datetime.now()
        start = now - timedelta(days=180)

        try:
            async with self._session.get(
                f"{self._base_url}/installations/{self._installation_id}/stats",
                params={
                    "type": "custom",
                    "interval": "hours",
                    "start": int(start.timestamp()),
                    "end": int(now.timestamp()),
                    "attributeCodes[]": ["solar_yield"],
                },
                headers=self._headers,
                timeout=60,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            records = data.get("records", {})
            all_records = []
            if isinstance(records, dict):
                all_records = records.get("solar_yield", [])

            if len(all_records) < 168:  # Need at least 1 week
                return None

            # Group by (month, hour) and collect values
            by_month_hour: dict[tuple[int, int], list[float]] = defaultdict(list)
            for entry in all_records:
                try:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    ts = entry[0] / 1000 if entry[0] > 1e12 else entry[0]
                    dt_obj = datetime.fromtimestamp(ts)
                    kw = max(0.0, float(entry[1]) / 1000.0)
                    by_month_hour[(dt_obj.month, dt_obj.hour)].append(kw)
                except (ValueError, TypeError, IndexError):
                    continue

            # Build envelope at requested percentile per month
            envelope: dict[int, list[float]] = {}
            for month in range(1, 13):
                hourly = []
                for hour in range(24):
                    vals = by_month_hour.get((month, hour), [])
                    if len(vals) >= 3:
                        vals_sorted = sorted(vals)
                        idx = min(int(len(vals_sorted) * percentile), len(vals_sorted) - 1)
                        hourly.append(vals_sorted[idx])
                    else:
                        hourly.append(0.0)
                if sum(hourly) > 0:
                    envelope[month] = hourly

            result = envelope if envelope else None
            self._cache_set(cache_key, result)
            return result

        except Exception:
            LOGGER.debug("VRM clearsky envelope fetch failed", exc_info=True)
            return None

    async def get_monthly_peak_kwh(self) -> dict[int, float] | None:
        """Get the best single-day solar yield per month from 365 days.

        Returns the TRUE production ceiling for each month.
        """
        if not self.available:
            return None

        cached = self._cache_get("monthly_peak", _MONTHLY_PEAK_TTL)
        if cached is not None:
            return cached

        now = datetime.now()
        end_ts = int(now.timestamp())
        start_ts = end_ts - 365 * 86400

        try:
            async with self._session.get(
                f"{self._base_url}/installations/{self._installation_id}/stats",
                params={
                    "type": "kwh",
                    "start": start_ts,
                    "end": end_ts,
                    "interval": "days",
                },
                headers=self._headers,
                timeout=30,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            records = data.get("records", {})
            pb = {int(e[0]): e[1] for e in records.get("Pb", [])}
            pc = {int(e[0]): e[1] for e in records.get("Pc", [])}

            all_ts = set(pb.keys()) | set(pc.keys())
            if not all_ts:
                return None

            by_month: dict[int, list[float]] = defaultdict(list)
            for ts in all_ts:
                solar = pb.get(ts, 0) + pc.get(ts, 0)
                dt_obj = datetime.fromtimestamp(ts / 1000)
                by_month[dt_obj.month].append(solar)

            result = {m: max(days) for m, days in by_month.items() if days} or None
            self._cache_set("monthly_peak", result)
            return result

        except Exception:
            LOGGER.debug("VRM monthly peak fetch failed", exc_info=True)
            return None

    async def get_historical_stats(self, days_back: int = 30) -> dict:
        """Fetch historical actual production/consumption from VRM."""
        empty = {"solar_hourly": [], "consumption_hourly": []}
        if not self.available:
            return empty

        cached = self._cache_get("historical_stats", _HISTORICAL_STATS_TTL)
        if cached is not None:
            return cached

        now = datetime.now()
        end = now.replace(hour=23, minute=59, second=59)
        start = end - timedelta(days=days_back)

        try:
            async with self._session.get(
                f"{self._base_url}/installations/{self._installation_id}/stats",
                params={
                    "type": "custom",
                    "interval": "hours",
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "attributeCodes[]": ["solar_yield", "consumption"],
                },
                headers=self._headers,
                timeout=30,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if not data.get("success"):
                return empty

            records = data.get("records", {})
            result = {
                "solar_hourly": records.get("solar_yield", []),
                "consumption_hourly": records.get("consumption", []),
            }
            self._cache_set("historical_stats", result)
            return result

        except Exception:
            LOGGER.debug("VRM historical stats fetch failed", exc_info=True)
            return empty
