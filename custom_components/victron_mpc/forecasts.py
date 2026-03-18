"""Forecast data fetching and preparation for HACS integration.

Async version of scripts/mpc/forecasts.py ForecastBuilder. Reads from native
HA state machine and async API clients to build the 5-minute interval arrays
needed by the optimizer. Data source priority:

Solar forecast:
  0. ha-solcast-solar integration (satellite-based, most accurate if installed)
  1. Weather-classified VRM envelope (P90/P70/P40/P15 by day type)
  2. VRM actual hourly average (30d) + VRM daily scaling (fallback)
  3. HA history profile (fallback — requires recorder, left as TODO)
  4. Synthetic bell curve (last resort)

Load forecast:
  1. VRM hourly consumption forecast (ML, learns your patterns)
  2. HA history profile (fallback — TODO)
  3. Typical residential curve (last resort)

Price forecast:
  - Amber 30-min forecast interpolated to 5-min
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .config import MPCTunables

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api.open_meteo import OpenMeteoClient
    from .api.solcast import SolcastClient
    from .api.vrm import VRMClient

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helper functions (pure, no I/O)
# ---------------------------------------------------------------------------

def _vrm_hourly_to_5min(
    hourly_data: list[tuple[int, float]],
    now: datetime,
    target_length: int,
    dt_hours: float,
) -> list[float] | None:
    """Convert VRM hourly forecast [(timestamp_ms, wh), ...] to 5-min kW array.

    VRM gives energy in Wh per hour. We convert to average kW for each
    5-min interval within that hour.
    """
    if not hourly_data:
        return None

    # Build hour -> kW mapping
    hour_kw: dict[int, float] = {}
    for ts_ms, wh in hourly_data:
        try:
            dt_obj = datetime.fromtimestamp(ts_ms / 1000)
            # Wh per hour = average W over that hour = kW / 1000
            kw = float(wh) / 1000.0
            hour_kw[dt_obj.hour] = kw
        except (ValueError, TypeError):
            continue

    if not hour_kw:
        return None

    # Build 5-min array starting from current time
    result: list[float] = []
    for i in range(target_length):
        hour_offset = i * dt_hours
        hour = int((now.hour + now.minute / 60 + hour_offset) % 24)
        if hour in hour_kw:
            result.append(max(0, hour_kw[hour]))
        else:
            result.append(0.0)

    return result if len(result) == target_length else None


def _interpolate_stepwise(
    values_30min: list[float], steps_per_interval: int, target_length: int,
) -> list[float]:
    """Expand 30-min values to 5-min by repeating each value."""
    result: list[float] = []
    for v in values_30min:
        result.extend([v] * steps_per_interval)
    if len(result) < target_length:
        last = result[-1] if result else 0.30
        result.extend([last] * (target_length - len(result)))
    return result[:target_length]


def _vrm_actuals_to_hourly_profile(
    hourly_records: list[Any], now: datetime,
) -> list[float] | None:
    """Build 24-hour production profile from VRM actual hourly data.

    Groups VRM historical solar_yield records by hour-of-day and averages.
    This captures the REAL production curve including physical shading
    from trees/buildings, panel orientation, and seasonal sun angle.

    Returns 24-element list starting from current hour (reordered),
    or None if insufficient data.
    """
    hourly_sums: dict[int, list[float]] = defaultdict(list)

    for entry in hourly_records:
        try:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            ts = entry[0] / 1000 if entry[0] > 1e12 else entry[0]
            dt_obj = datetime.fromtimestamp(ts)
            kw = float(entry[1]) / 1000.0  # Wh per hour -> avg kW
            if 0 <= kw < 20:  # Sanity: 0 to 20kW
                hourly_sums[dt_obj.hour].append(kw)
        except (ValueError, TypeError, IndexError):
            continue

    if len(hourly_sums) < 8:  # Need at least 8 hours with data
        return None

    # Average each hour
    profile: list[float] = []
    for h in range(24):
        vals = hourly_sums.get(h, [])
        if vals:
            profile.append(sum(vals) / len(vals))
        else:
            profile.append(0.0)

    # Reorder starting from current hour
    current_hour = now.hour
    reordered = profile[current_hour:] + profile[:current_hour]
    return reordered


def _build_hourly_profile_from_history(
    history: list[dict[str, Any]], now: datetime, days_back: int,
) -> list[float]:
    """Build a 24-hour profile from HA history data.

    Groups historical readings by hour-of-day and averages them.
    This captures the actual patterns (shading, usage habits, etc.).
    """
    hourly_sums: dict[int, list[float]] = {h: [] for h in range(24)}

    for entry in history:
        try:
            val = float(entry.get("state", 0))
            if val < 0 or val > 50000:  # Sanity check (W)
                continue
            ts = entry.get("last_changed") or entry.get("last_updated", "")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts)
            hourly_sums[dt.hour].append(val / 1000.0)  # W to kW
        except (ValueError, TypeError):
            continue

    # Average each hour
    profile: list[float] = []
    for h in range(24):
        vals = hourly_sums[h]
        if vals:
            profile.append(sum(vals) / len(vals))
        else:
            profile.append(0.0)

    # Reorder starting from current hour
    current_hour = now.hour
    reordered = profile[current_hour:] + profile[:current_hour]
    return reordered


def _expand_hourly_to_5min(hourly: list[float], target_length: int) -> list[float]:
    """Expand hourly values to 5-min intervals."""
    result: list[float] = []
    for v in hourly:
        result.extend([v] * 12)  # 12 x 5min = 1 hour
    if len(result) < target_length:
        last = result[-1] if result else 0.0
        result.extend([last] * (target_length - len(result)))
    return result[:target_length]


def _solar_bell_curve(
    now: datetime, daily_kwh: float, steps: int, dt_hours: float,
) -> list[float]:
    """Generate bell-curve solar profile when no history available."""
    solar_noon_hour = 12.5
    sigma = 3.0

    profile: list[float] = []
    for i in range(steps):
        hour_offset = i * dt_hours
        hour_of_day = (now.hour + now.minute / 60 + hour_offset) % 24
        if 5 <= hour_of_day <= 20:
            intensity = math.exp(-0.5 * ((hour_of_day - solar_noon_hour) / sigma) ** 2)
        else:
            intensity = 0.0
        profile.append(intensity)

    total = sum(profile) * dt_hours
    if total > 0:
        scale = daily_kwh / total
        profile = [p * scale for p in profile]

    return profile


def _load_typical_profile(
    now: datetime, daily_kwh: float, steps: int, dt_hours: float,
) -> list[float]:
    """Typical residential load profile when no history available.

    Two peaks: morning 7-9am, evening 5-9pm. Baseline overnight.
    """
    profile: list[float] = []
    for i in range(steps):
        hour_offset = i * dt_hours
        hour = (now.hour + now.minute / 60 + hour_offset) % 24

        if 7 <= hour < 9:
            factor = 1.4  # Morning peak
        elif 17 <= hour < 21:
            factor = 1.6  # Evening peak
        elif 22 <= hour or hour < 6:
            factor = 0.6  # Overnight low
        else:
            factor = 1.0  # Baseline

        profile.append(factor)

    total = sum(profile) * dt_hours
    if total > 0:
        scale = daily_kwh / total
        profile = [p * scale for p in profile]

    return profile


# ---------------------------------------------------------------------------
# ForecastBuilder — async, native HA state access
# ---------------------------------------------------------------------------

class ForecastBuilder:
    """Builds forecast arrays from VRM and HA sensor data.

    Async port of scripts/mpc/forecasts.py ForecastBuilder for the HACS
    integration. Uses native HA state machine (hass.states.get) and async
    API clients instead of REST API calls.

    Data source priority:
        0. Solcast satellite-based forecast (most accurate, accounts for clouds)
        1. VRM hourly forecasts (ML-powered, best accuracy)
        2. HA sensor history (real data from your system)
        3. Synthetic profiles (last resort)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entities: dict[str, str],
        tunables: MPCTunables,
        vrm: VRMClient | None = None,
        open_meteo: OpenMeteoClient | None = None,
        solcast: SolcastClient | None = None,
    ) -> None:
        self._hass = hass
        self._entities = entities
        self._tunables = tunables
        self._vrm = vrm
        self._open_meteo = open_meteo
        self._solcast = solcast
        self.N = tunables.horizon_steps
        self.dt_hours = tunables.dt_hours
        # Cached cloud layers from _classify_day_type for reuse in _get_cloud_derate_factors
        self._cloud_layers_cache: dict[int, dict[str, float]] | None = None

    # ------------------------------------------------------------------
    # HA state helpers (replacing HAClient methods)
    # ------------------------------------------------------------------

    def _get_state(self, entity_id: str) -> Any:
        """Get the full state object for an HA entity.

        Returns a State object with .state and .attributes, or None.
        """
        return self._hass.states.get(entity_id)

    def _get_numeric(self, entity_id: str, default: float = 0.0) -> float:
        """Get a numeric state value from HA, with fallback."""
        try:
            state = self._hass.states.get(entity_id)
            if state is None:
                return default
            return float(state.state)
        except (ValueError, TypeError, AttributeError):
            return default

    def _get_state_value(self, entity_id: str, default: str = "") -> str:
        """Get the string state value for an entity."""
        state = self._hass.states.get(entity_id)
        if state is None:
            return default
        return state.state

    def _get_attribute(self, entity_id: str, attr: str, default: Any = None) -> Any:
        """Get a specific attribute from an HA entity."""
        state = self._hass.states.get(entity_id)
        if state is None:
            return default
        return state.attributes.get(attr, default)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def build_all(self) -> dict[str, Any]:
        """Fetch all data and return complete forecast dict.

        Returns dict with keys matching OptInput fields:
            solar_forecast_kw, load_forecast_kw, buy_price, sell_price,
            battery_soc_kwh, sunset_step
        """
        now = datetime.now()

        # Current state from HA entities
        battery_soc_pct = self._get_numeric(
            self._entities.get("battery_soc", "sensor.victron_battery_state_of_charge"),
            50.0,
        )
        current_solar_w = self._get_numeric(
            self._entities.get("solar_power", "sensor.solar_power"),
            0.0,
        )
        current_load_w = self._get_numeric(
            self._entities.get("ac_consumption", "sensor.victron_ac_consumption"),
            1000.0,
        )

        # Amber prices
        buy_prices, sell_prices = self._build_price_forecast()

        # Solar forecast (weather-classified)
        solar_kw, solar_source, day_type = await self._build_solar_forecast(
            now, current_solar_w,
        )

        # Load forecast
        load_kw, load_source, seasonal_factor = await self._build_load_forecast(
            now, current_load_w,
        )

        # Sunset step
        sunset_step = self._compute_sunset_step(now)

        return {
            "battery_soc_pct": battery_soc_pct,
            "solar_forecast_kw": solar_kw,
            "load_forecast_kw": load_kw,
            "buy_price": buy_prices,
            "sell_price": sell_prices,
            "sunset_step": sunset_step,
            "current_solar_w": current_solar_w,
            "current_load_w": current_load_w,
            "solar_forecast_source": solar_source,
            "solar_day_type": day_type,
            "load_forecast_source": load_source,
            "seasonal_load_factor": round(seasonal_factor, 3),
            "timestamp": now.isoformat(),
        }

    # ------------------------------------------------------------------
    # Price forecast
    # ------------------------------------------------------------------

    def _build_price_forecast(self) -> tuple[list[float], list[float]]:
        """Build 5-min price arrays from Amber 30-min forecasts."""
        amber_forecast_id = self._entities.get(
            "amber_forecast", "sensor.amber_general_forecast",
        )
        amber_feedin_forecast_id = self._entities.get(
            "amber_feedin_forecast", "sensor.amber_feed_in_forecast",
        )
        amber_price_id = self._entities.get(
            "amber_price", "sensor.amber_general_price",
        )
        amber_feedin_id = self._entities.get(
            "amber_feedin", "sensor.amber_feed_in_price",
        )

        buy_forecasts = self._get_attribute(amber_forecast_id, "forecasts", [])
        sell_forecasts = self._get_attribute(amber_feedin_forecast_id, "forecasts", [])

        try:
            current_buy = float(self._get_state_value(amber_price_id, "0.30"))
        except (ValueError, TypeError):
            current_buy = 0.30

        try:
            current_sell = float(self._get_state_value(amber_feedin_id, "0.06"))
        except (ValueError, TypeError):
            current_sell = 0.06

        buy_30min: list[float] = [current_buy]
        sell_30min: list[float] = [current_sell]

        for f in buy_forecasts:
            try:
                buy_30min.append(float(f.get("per_kwh", current_buy)))
            except (ValueError, TypeError):
                buy_30min.append(current_buy)

        for f in sell_forecasts:
            try:
                sell_30min.append(float(f.get("per_kwh", current_sell)))
            except (ValueError, TypeError):
                sell_30min.append(current_sell)

        # Interpolate 30-min -> 5-min
        steps_per_interval = 6  # 30min / 5min
        buy_5min = _interpolate_stepwise(buy_30min, steps_per_interval, self.N)
        sell_5min = _interpolate_stepwise(sell_30min, steps_per_interval, self.N)

        return buy_5min, sell_5min

    # ------------------------------------------------------------------
    # Cloud layers (delegated to OpenMeteoClient)
    # ------------------------------------------------------------------

    async def _fetch_cloud_layers(
        self, now: datetime,
    ) -> dict[int, dict[str, float]] | None:
        """Fetch hourly cloud layer data via the async OpenMeteoClient.

        Returns a mapping of hour_offset -> {"low": pct, "mid": pct, "high": pct}
        for each hour in the forecast horizon. Returns None if unavailable.
        """
        if self._open_meteo is None:
            return None

        return await self._open_meteo.fetch_cloud_layers(
            now=now,
            forecast_hours=self._tunables.forecast_hours,
        )

    def _effective_cloud_pct(self, layers: dict[str, float]) -> float:
        """Compute effective cloud percentage from layer breakdown.

        Weights each layer by its solar impact:
          - high (cirrus): 0.15 weight -- barely blocks solar
          - mid (altostratus): 0.5 weight -- moderate blocking
          - low (stratus): 0.9 weight -- heavy blocking

        Returns weighted effective cloud %, capped at 100.
        """
        weights = self._tunables.solar_cloud_layer_weights
        weighted_sum = 0.0
        total_weight = 0.0
        for layer_name, weight in weights.items():
            pct = layers.get(layer_name, 0.0)
            weighted_sum += pct * weight
            total_weight += 100.0 * weight  # Max possible contribution

        if total_weight == 0:
            return 0.0
        return min(100.0, weighted_sum / total_weight * 100.0)

    # ------------------------------------------------------------------
    # Day type classification
    # ------------------------------------------------------------------

    async def _classify_day_type(
        self, now: datetime,
    ) -> tuple[str, float, float]:
        """Classify today's weather from met.no hourly forecast.

        Looks at daylight hours (6am-6pm local) in the forecast to compute
        mean cloud coverage and total precipitation. Classifies into one of:
        clear, partly_cloudy, overcast, rain.

        Returns:
            Tuple of (day_type, mean_cloud_pct, total_precip_mm).
            Falls back to ("partly_cloudy", 50.0, 0.0) if weather unavailable.
        """
        default: tuple[str, float, float] = ("partly_cloudy", 50.0, 0.0)
        weather_entity = self._entities.get("weather_entity", "weather.home")

        try:
            # Call weather.get_forecasts service natively
            result = await self._hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )

            if not result:
                return default

            forecasts = (
                result.get(weather_entity, {}).get("forecast", [])
            )
            if not forecasts or len(forecasts) < 6:
                return default

            now_aware = now.astimezone()

            # Collect cloud + precip for remaining daylight hours (now-6pm).
            cloud_vals: list[float] = []
            precip_total: float = 0.0
            for f in forecasts:
                try:
                    dt_str = f.get("datetime", "")
                    dt_parsed = datetime.fromisoformat(dt_str)
                    if dt_parsed.tzinfo is None:
                        dt_parsed = dt_parsed.astimezone()
                    dt_local = dt_parsed.astimezone(now_aware.tzinfo)

                    # Only consider remaining daylight hours
                    if dt_local.hour < 6 or dt_local.hour >= 18:
                        continue
                    # Skip hours that have already passed
                    if dt_local < now_aware:
                        continue
                    # Only consider today and tomorrow
                    if (dt_local.date() - now_aware.date()).days > 1:
                        continue

                    cloud_vals.append(float(f.get("cloud_coverage", 0)))
                    precip_total += float(f.get("precipitation", 0))
                except (ValueError, TypeError):
                    continue

            if not cloud_vals:
                return default

            mean_cloud_raw = sum(cloud_vals) / len(cloud_vals)

            # Try layer-weighted cloud from Open-Meteo for better classification.
            mean_cloud = mean_cloud_raw
            cloud_layers = await self._fetch_cloud_layers(now)
            if cloud_layers:
                effective_vals = []
                for _offset, layer_data in cloud_layers.items():
                    effective_vals.append(self._effective_cloud_pct(layer_data))
                if effective_vals:
                    mean_cloud = sum(effective_vals) / len(effective_vals)
                    _LOGGER.info(
                        "Cloud layers: raw=%.0f%% -> effective=%.0f%% "
                        "(high cirrus has minimal solar impact)",
                        mean_cloud_raw, mean_cloud,
                    )
                    # Store layer data for use by cloud derate factors
                    self._cloud_layers_cache = cloud_layers

            # Classify using effective cloud
            cloud_clear = self._tunables.solar_day_type_cloud_clear
            cloud_overcast = self._tunables.solar_day_type_cloud_overcast
            precip_heavy = self._tunables.solar_day_type_precip_heavy
            precip_light = self._tunables.solar_day_type_precip_light

            if precip_total >= precip_heavy:
                day_type = "rain"
            elif mean_cloud > cloud_overcast and precip_total < precip_heavy:
                day_type = "overcast"
            elif mean_cloud < cloud_clear and precip_total < precip_light:
                day_type = "clear"
            else:
                day_type = "partly_cloudy"

            return day_type, round(mean_cloud, 1), round(precip_total, 1)

        except Exception:
            _LOGGER.debug("Day type classification failed", exc_info=True)
            return default

    def _maybe_adjust_day_type(self, now: datetime, day_type: str) -> str:
        """Adjust day type if actual solar yield diverges from expected.

        After 10am, compares actual yield to VRM forecast using a sin^2 curve
        to estimate what fraction should have been produced by now.
        - If actual < 60% of expected, downgrades one level
        - If actual > 150% of expected, upgrades one level
        """
        current_hour_f = now.hour + now.minute / 60.0
        if current_hour_f < 10.0:
            return day_type  # Too early to judge

        try:
            solar_yield_entity = self._entities.get(
                "solar_yield_today", "sensor.solar_yield_today",
            )
            actual_yield = self._get_numeric(solar_yield_entity, default=-1.0)
            if actual_yield < 0:
                return day_type

            vrm_forecast_entity = self._entities.get(
                "vrm_forecast", "sensor.vrm_solar_forecast_tomorrow",
            )
            forecast_daily_kwh = float(
                self._get_attribute(vrm_forecast_entity, "forecast_today_kwh", 0)
            )
            if forecast_daily_kwh < 1.0:
                return day_type

            # Estimate expected yield by now using sin^2 cumulative model
            solar_start = 8.0
            solar_end = 18.5
            solar_length = solar_end - solar_start
            elapsed = min(current_hour_f - solar_start, solar_length)
            progress = elapsed / solar_length
            cum_fraction = progress - math.sin(2 * math.pi * progress) / (2 * math.pi)
            expected_by_now = forecast_daily_kwh * cum_fraction

            if expected_by_now < 1.0:
                return day_type

            yield_ratio = actual_yield / expected_by_now

            # Downgrade: actual far below expected
            if yield_ratio < 0.60 and day_type != "rain":
                downgrade_map = {
                    "clear": "partly_cloudy",
                    "partly_cloudy": "overcast",
                    "overcast": "rain",
                }
                new_type = downgrade_map.get(day_type, day_type)
                _LOGGER.info(
                    "Day type downgrade: %s -> %s "
                    "(yield %.1f kWh vs %.1f expected = %.0f%%)",
                    day_type, new_type, actual_yield, expected_by_now,
                    yield_ratio * 100,
                )
                return new_type

            # Upgrade: actual well above expected
            if yield_ratio > 1.50 and day_type != "clear":
                upgrade_map = {
                    "rain": "overcast",
                    "overcast": "partly_cloudy",
                    "partly_cloudy": "clear",
                }
                new_type = upgrade_map.get(day_type, day_type)
                _LOGGER.info(
                    "Day type upgrade: %s -> %s "
                    "(yield %.1f kWh vs %.1f expected = %.0f%%)",
                    day_type, new_type, actual_yield, expected_by_now,
                    yield_ratio * 100,
                )
                return new_type

        except Exception:
            pass

        return day_type

    # ------------------------------------------------------------------
    # Solar forecast
    # ------------------------------------------------------------------

    def _get_solcast_ha_forecast(
        self, now: datetime,
    ) -> list[float] | None:
        """Read solar forecast from ha-solcast-solar integration if installed.

        Reads the detailedForecast attribute from sensor.solcast_pv_forecast_forecast_today
        which provides 30-min resolution power forecasts (kW) with pv_estimate,
        pv_estimate10, and pv_estimate90 fields.

        Returns 5-min solar_kw array or None if unavailable.
        """
        # Check for ha-solcast-solar entity
        solcast_entity = self._entities.get(
            "solcast_forecast", "sensor.solcast_pv_forecast_forecast_today"
        )
        state = self._get_state(solcast_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return None

        detailed = state.attributes.get("detailedForecast")
        if not detailed or not isinstance(detailed, list):
            # Try alternate attribute names
            detailed = state.attributes.get("detailed_forecast")
            if not detailed:
                return None

        # Extract 30-min power values (kW) starting from now
        now_aware = now.astimezone()
        forecast_30min: list[float] = []

        for entry in detailed:
            try:
                period_start = entry.get("period_start", "")
                if isinstance(period_start, str):
                    dt = datetime.fromisoformat(period_start)
                else:
                    dt = period_start

                # Only include future periods (or current)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now_aware.tzinfo)
                delta_h = (dt - now_aware).total_seconds() / 3600
                if delta_h < -0.5:
                    continue
                if delta_h >= self._tunables.forecast_hours:
                    break

                # Use central estimate (pv_estimate), fallback to estimate
                kw = float(
                    entry.get("pv_estimate")
                    or entry.get("estimate")
                    or 0
                )
                forecast_30min.append(max(0.0, kw))
            except (ValueError, TypeError, KeyError):
                continue

        if len(forecast_30min) < 6:
            return None

        # Interpolate 30-min → 5-min (each 30-min value → 6 × 5-min steps)
        solar_kw = _interpolate_stepwise(forecast_30min, 6, self.N)
        solcast_total = sum(solar_kw) * self.dt_hours

        _LOGGER.info(
            "Solar forecast: ha-solcast-solar raw (%d periods, "
            "peak=%.1fkW, total=%.1fkWh)",
            len(forecast_30min),
            max(forecast_30min) if forecast_30min else 0,
            solcast_total,
        )
        return solar_kw

    async def _cap_solcast_with_vrm(
        self, solar_kw: list[float], now: datetime,
    ) -> tuple[list[float], float]:
        """Apply VRM hourly production envelope as a shading mask to Solcast.

        Solcast doesn't know about site-specific shading — trees, buildings,
        roof obstructions that vary by hour and season. VRM has years of
        actual production data that captures these patterns.

        Uses the VRM P90 envelope (best-case per hour per month) as a
        per-hour ceiling. Each hour of Solcast is capped at the VRM P90
        value for that hour. This preserves Solcast's cloud-awareness
        while preventing physically impossible predictions.

        The envelope updates naturally as seasons change — VRM data is
        fetched with 24h cache TTL, so shading patterns stay current.

        Returns (shaped_solar_kw, scale_factor).
        Scale factor < 1.0 means Solcast was reduced.
        """
        if not self._vrm or not self._vrm.available:
            _LOGGER.warning(
                "Solcast used WITHOUT VRM shading correction — forecasts "
                "may be significantly over-estimated. Configure VRM API "
                "credentials for accurate solar predictions."
            )
            return solar_kw, 1.0

        # Get P90 envelope — the maximum realistic production per hour
        envelope = await self._vrm.get_clearsky_envelope(percentile=0.90)
        if not envelope:
            _LOGGER.warning(
                "VRM P90 envelope unavailable — Solcast used without "
                "shading correction this cycle"
            )
            return solar_kw, 1.0

        month = now.month
        hourly_ceiling = envelope.get(month)
        if not hourly_ceiling:
            for offset in [1, -1, 2, -2]:
                m = ((month - 1 + offset) % 12) + 1
                hourly_ceiling = envelope.get(m)
                if hourly_ceiling:
                    break

        if not hourly_ceiling or sum(hourly_ceiling) <= 0:
            return solar_kw, 1.0

        # Reorder ceiling to start from current hour
        current_hour = now.hour
        ceiling = hourly_ceiling[current_hour:] + hourly_ceiling[:current_hour]

        # Apply per-hour cap: each 5-min step capped at its hour's VRM P90
        steps_per_hour = self._tunables.steps_per_hour
        solcast_total = 0.0
        capped_total = 0.0
        capped = []

        for i, kw in enumerate(solar_kw):
            hour_idx = min(i // steps_per_hour, len(ceiling) - 1)
            vrm_max = ceiling[hour_idx]
            capped_kw = min(kw, vrm_max)
            capped.append(capped_kw)
            solcast_total += kw
            capped_total += capped_kw

        scale = capped_total / solcast_total if solcast_total > 0 else 1.0

        if scale < 0.99:
            _LOGGER.info(
                "Solcast shaped by VRM P90 envelope: %.1fkWh -> %.1fkWh "
                "(scale=%.2f, month=%d, shading mask applied per-hour)",
                solcast_total * self.dt_hours,
                capped_total * self.dt_hours,
                scale, month,
            )

        return capped, scale

    async def _build_solar_forecast(
        self, now: datetime, current_solar_w: float,
    ) -> tuple[list[float], str, str]:
        """Build 5-min solar forecast.

        Returns:
            Tuple of (solar_kw list, source name, day_type).

        Priority:
            0. ha-solcast-solar integration (satellite-based, most accurate)
            1. Weather-classified VRM envelope
            2. VRM actual hourly average (30d) + daily scaling
            3. HA sensor history + VRM daily scaling (TODO)
            4. Synthetic bell curve
        """
        solar_kw: list[float] | None = None
        source = "unknown"

        # Classify day type from weather forecast
        day_type, mean_cloud, total_precip = await self._classify_day_type(now)

        # Reality check: if actual yield is far below forecast by midday,
        # downgrade day type
        day_type = self._maybe_adjust_day_type(now, day_type)

        # Select VRM percentile based on day type
        percentile = self._tunables.solar_day_type_percentiles.get(day_type, 0.90)

        # Priority 0: ha-solcast-solar integration (satellite-based)
        # Solcast doesn't account for site-specific shading, so we cap
        # against VRM's actual best-ever production for this month.
        if solar_kw is None:
            solcast_forecast = self._get_solcast_ha_forecast(now)
            if solcast_forecast is not None:
                solcast_forecast, vrm_scale = await self._cap_solcast_with_vrm(
                    solcast_forecast, now,
                )
                solar_kw = solcast_forecast
                source = "solcast_ha"
                if vrm_scale < 1.0:
                    source = f"solcast_ha_capped_{vrm_scale:.0%}"

        # Priority 1: Weather-classified VRM envelope
        if solar_kw is None and self._vrm and self._vrm.available:
            envelope = await self._vrm.get_clearsky_envelope(percentile=percentile)
            if envelope:
                month = now.month
                hourly_profile = envelope.get(month)
                if not hourly_profile:
                    for offset in [1, -1, 2, -2]:
                        m = ((month - 1 + offset) % 12) + 1
                        hourly_profile = envelope.get(m)
                        if hourly_profile:
                            break

                if hourly_profile and sum(hourly_profile) > 0:
                    envelope_total = sum(hourly_profile)

                    # Cap envelope at historical monthly peak
                    monthly_peaks = await self._vrm.get_monthly_peak_kwh()
                    if monthly_peaks and month in monthly_peaks:
                        peak_kwh = monthly_peaks[month]
                        if envelope_total > peak_kwh:
                            scale = peak_kwh / envelope_total
                            hourly_profile = [h * scale for h in hourly_profile]
                            _LOGGER.info(
                                "P%d envelope capped: %.1fkWh -> %.1fkWh "
                                "(monthly peak, scale=%.2f)",
                                int(percentile * 100), envelope_total, peak_kwh, scale,
                            )
                            envelope_total = peak_kwh

                    # Reorder starting from current hour
                    current_hour = now.hour
                    profile = hourly_profile[current_hour:] + hourly_profile[:current_hour]
                    solar_kw = _expand_hourly_to_5min(profile, self.N)
                    pct_label = int(percentile * 100)
                    source = f"clearsky_p{pct_label}"

                    _LOGGER.info(
                        "Solar forecast: %s day -> P%d envelope "
                        "(%.1fkWh, peak=%.1fkW, cloud=%.0f%%, precip=%.1fmm, month=%d)",
                        day_type, pct_label, envelope_total,
                        max(hourly_profile), mean_cloud, total_precip, month,
                    )

        # Priority 2: VRM actual hourly average (30d) + daily scaling
        if solar_kw is None and self._vrm and self._vrm.available:
            historical = await self._vrm.get_historical_stats(days_back=30)
            if historical.get("solar_hourly") and len(historical["solar_hourly"]) > 48:
                profile_kw = _vrm_actuals_to_hourly_profile(
                    historical["solar_hourly"], now,
                )
                if profile_kw:
                    scale = self._get_vrm_daily_scale(profile_kw)
                    profile_kw = [p * scale for p in profile_kw]
                    solar_kw = _expand_hourly_to_5min(profile_kw, self.N)
                    source = "vrm_30d_avg"
                    _LOGGER.info(
                        "Solar forecast: VRM 30d avg x daily scale (%.2f)",
                        scale,
                    )

        # Priority 3: HA sensor history + VRM daily scaling
        # TODO: HA recorder queries require async recorder API access
        # (homeassistant.components.recorder.get_instance().async_add_executor_job)
        # which is complex. For now, skip to priority 4. To implement:
        #   from homeassistant.components.recorder import get_instance
        #   instance = get_instance(self._hass)
        #   history = await instance.async_add_executor_job(...)

        # Priority 4: Synthetic bell curve
        if solar_kw is None:
            vrm_forecast_entity = self._entities.get(
                "vrm_forecast", "sensor.vrm_solar_forecast_tomorrow",
            )
            daily_kwh = float(
                self._get_attribute(vrm_forecast_entity, "forecast_today_kwh", 25)
            )
            solar_kw = _solar_bell_curve(now, daily_kwh, self.N, self.dt_hours)
            source = "bell_curve"

        # Apply solar derating: weather-aware per-hour adjustment
        if solar_kw and self._tunables.solar_derating:
            # Layer 1: Solcast satellite weather signal
            solcast_factors = await self._get_solcast_derate_factors(now)
            if solcast_factors:
                avg_solcast = sum(solcast_factors) / len(solcast_factors)
                _LOGGER.info(
                    "Cloud derating: avg %.0f%% over %dh horizon (Solcast satellite)",
                    avg_solcast * 100, len(solcast_factors),
                )
                solar_kw = [
                    s * solcast_factors[min(i, len(solcast_factors) - 1)]
                    for i, s in enumerate(solar_kw)
                ]
            else:
                # Layer 2: met.no hourly cloud forecast (fallback)
                cloud_factors = await self._get_cloud_derate_factors(now)
                if cloud_factors:
                    avg_cloud_derate = sum(cloud_factors) / len(cloud_factors)
                    _LOGGER.info(
                        "Cloud derating: avg %.0f%% over %dh horizon (met.no fallback)",
                        avg_cloud_derate * 100, len(cloud_factors),
                    )
                    solar_kw = [
                        s * cloud_factors[min(i, len(cloud_factors) - 1)]
                        for i, s in enumerate(solar_kw)
                    ]
                else:
                    # Layer 3: Rolling accuracy ratio (last resort)
                    derate = await self._compute_solar_derate()
                    if derate < 1.0:
                        _LOGGER.info(
                            "Solar derating: %.0f%% (rolling accuracy, no weather data)",
                            derate * 100,
                        )
                        solar_kw = [s * derate for s in solar_kw]

        # Intra-day solar correction
        if solar_kw and now.hour >= 10 and self._is_daytime():
            correction = self._intraday_solar_correction(now, solar_kw)
            if correction is not None and correction < 0.95:
                _LOGGER.info(
                    "Intra-day solar correction: %.0f%% (actual yield tracking "
                    "below forecast)",
                    correction * 100,
                )
                solar_kw = [s * correction for s in solar_kw]

        # Inject current real value for first interval
        if solar_kw:
            solar_kw[0] = current_solar_w / 1000.0  # W to kW

        return solar_kw, source, day_type

    # ------------------------------------------------------------------
    # Solcast derate
    # ------------------------------------------------------------------

    async def _get_solcast_derate_factors(
        self, now: datetime,
    ) -> list[float] | None:
        """Get per-step cloud derating from Solcast satellite P50/P90 ratio.

        Solcast returns P50 (expected) and P90 (optimistic/clear-sky) for
        each 30-min period. The ratio P50/P90 tells us how much cloud
        Solcast's satellite imagery expects.

        Returns None if Solcast unavailable (falls back to met.no).
        """
        if not self._solcast or not self._solcast.available:
            return None

        if not self._is_daytime():
            return None

        solcast_data = await self._solcast.get_forecasts()
        if not solcast_data:
            return None

        local_tz = now.astimezone().tzinfo
        now_aware = now.astimezone(local_tz)

        # Build hour -> P50/P90 ratio mapping
        hour_factors: dict[int, float] = {}
        for entry in solcast_data:
            try:
                period_end_str = entry.get("period_end", "")
                p50 = float(entry.get("pv_estimate", 0))
                p90 = float(entry.get("pv_estimate90", 0))

                # Parse UTC time
                clean = period_end_str.rstrip("Z").split(".")[0]
                dt_utc = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone(local_tz)
                dt_mid = dt_local - timedelta(minutes=15)
                offset_h = (dt_mid - now_aware).total_seconds() / 3600

                if 0 <= offset_h < self._tunables.forecast_hours:
                    hour_idx = int(offset_h)
                    if p90 > 0.05:
                        ratio = min(1.0, p50 / p90)
                    else:
                        ratio = 1.0  # Nighttime or negligible
                    if hour_idx not in hour_factors or ratio < hour_factors[hour_idx]:
                        hour_factors[hour_idx] = ratio
            except (ValueError, TypeError, KeyError):
                continue

        if len(hour_factors) < 4:
            return None

        # Expand to per-step factors
        factors: list[float] = []
        for i in range(self.N):
            hour_offset = int(i * self.dt_hours)
            factor = hour_factors.get(hour_offset, 1.0)
            factors.append(max(0.2, factor))  # Floor at 20%

        return factors

    # ------------------------------------------------------------------
    # Cloud derate (met.no / Open-Meteo fallback)
    # ------------------------------------------------------------------

    async def _get_cloud_derate_factors(
        self, now: datetime,
    ) -> list[float] | None:
        """Get per-step cloud derating factors using layer-weighted cloud data.

        Uses Open-Meteo cloud layer data (low/mid/high) when available.
        Falls back to met.no total cloud if layers unavailable.

        Returns None if weather data unavailable.
        """
        try:
            # Try layer-weighted cloud first (from cache or fresh fetch)
            cloud_layers = self._cloud_layers_cache
            if cloud_layers is None:
                cloud_layers = await self._fetch_cloud_layers(now)

            if cloud_layers:
                impact = self._tunables.solar_cloud_impact
                factors: list[float] = []
                for i in range(self.N):
                    hour_offset = int(i * self.dt_hours)
                    layer_data = cloud_layers.get(hour_offset, {})
                    if layer_data:
                        cloud_pct = self._effective_cloud_pct(layer_data)
                    else:
                        cloud_pct = 0.0

                    raw = 1.0 - (cloud_pct / 100.0 * impact)
                    factor = max(raw ** 0.5, 1.0 - impact)
                    factors.append(factor)

                avg = sum(factors) / len(factors) if factors else 1.0
                _LOGGER.info(
                    "Cloud derate: layer-weighted avg %.0f%% (Open-Meteo layers)",
                    avg * 100,
                )
                return factors

            # Fallback: met.no total cloud coverage via weather service
            weather_entity = self._entities.get("weather_entity", "weather.home")
            result = await self._hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )

            if not result:
                return None

            forecasts = (
                result.get(weather_entity, {}).get("forecast", [])
            )

            if not forecasts or len(forecasts) < 6:
                return None

            now_aware = now.astimezone()

            hour_cloud: dict[int, float] = {}
            for f in forecasts:
                dt_str = f.get("datetime", "")
                cloud_pct_val = f.get("cloud_coverage", 0)
                try:
                    dt_parsed = datetime.fromisoformat(dt_str)
                    if dt_parsed.tzinfo is None:
                        dt_parsed = dt_parsed.astimezone()
                    dt_local = dt_parsed.astimezone(now_aware.tzinfo)
                    delta_h = (dt_local - now_aware).total_seconds() / 3600
                    if 0 <= delta_h < self._tunables.forecast_hours:
                        step_idx = int(delta_h)
                        hour_cloud[step_idx] = float(cloud_pct_val)
                except (ValueError, TypeError):
                    continue

            if len(hour_cloud) < 6:
                return None

            # Build per-step factors
            impact = self._tunables.solar_cloud_impact
            factors = []
            for i in range(self.N):
                hour_offset = int(i * self.dt_hours)
                cloud_pct_val2 = hour_cloud.get(hour_offset, 0)

                raw = 1.0 - (cloud_pct_val2 / 100.0 * impact)
                factor = max(raw ** 0.5, 1.0 - impact)
                factors.append(factor)

            return factors

        except Exception:
            _LOGGER.debug("Cloud derate factors unavailable", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Solar derate (rolling accuracy)
    # ------------------------------------------------------------------

    async def _compute_solar_derate(self) -> float:
        """Compute solar derating factor from recent forecast-vs-actual accuracy.

        Compares VRM's daily solar forecast against actual production over
        the last N days using VRM's own historical data.

        Only derates (ratio <= 1.0), never inflates.
        """
        if not self._vrm or not self._vrm.available:
            return 1.0

        try:
            days = self._tunables.solar_derating_days

            # Get actual daily production from VRM historical stats
            # The VRM client handles the API call asynchronously
            historical = await self._vrm.get_historical_stats(days_back=days + 1)
            solar_hourly = historical.get("solar_hourly", [])

            if not solar_hourly or len(solar_hourly) < 24:
                return 1.0

            # Sum actual daily production from hourly data
            daily_sums: dict[str, float] = defaultdict(float)
            for entry in solar_hourly:
                try:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    ts = entry[0] / 1000 if entry[0] > 1e12 else entry[0]
                    dt_obj = datetime.fromtimestamp(ts)
                    kwh = float(entry[1]) / 1000.0  # Wh -> kWh
                    if kwh > 0:
                        daily_sums[dt_obj.strftime("%Y-%m-%d")] += kwh
                except (ValueError, TypeError, IndexError):
                    continue

            actuals = [v for v in daily_sums.values() if v > 0]
            if len(actuals) < 3:
                return 1.0

            actual_avg = sum(actuals) / len(actuals)

            # Get VRM's forecast for today as proxy
            vrm_forecast_entity = self._entities.get(
                "vrm_forecast", "sensor.vrm_solar_forecast_tomorrow",
            )
            forecast_today = float(
                self._get_attribute(vrm_forecast_entity, "forecast_today_kwh", 0)
            )
            forecast_tomorrow = float(
                self._get_attribute(vrm_forecast_entity, "forecast_tomorrow_kwh", 0)
            )

            if forecast_today <= 0:
                return 1.0

            forecast_avg = (
                (forecast_today + forecast_tomorrow) / 2
                if forecast_tomorrow > 0
                else forecast_today
            )

            ratio = actual_avg / forecast_avg
            ratio = max(
                self._tunables.solar_derating_min,
                min(self._tunables.solar_derating_max, ratio),
            )

            return ratio

        except Exception:
            return 1.0

    # ------------------------------------------------------------------
    # Intra-day solar correction
    # ------------------------------------------------------------------

    def _intraday_solar_correction(
        self, now: datetime, solar_kw: list[float],
    ) -> float | None:
        """Compare actual solar yield vs forecast-to-now and return correction.

        Only meaningful after enough solar hours (caller checks hour >= 10).
        Returns None if data unavailable or correction not needed.
        """
        try:
            solar_yield_entity = self._entities.get(
                "solar_yield_today", "sensor.solar_yield_today",
            )
            actual_yield = self._get_numeric(solar_yield_entity, default=-1.0)
            if actual_yield < 0:
                return None

            vrm_forecast_entity = self._entities.get(
                "vrm_forecast", "sensor.vrm_solar_forecast_tomorrow",
            )
            forecast_daily_kwh = float(
                self._get_attribute(vrm_forecast_entity, "forecast_today_kwh", 0)
            )

            if forecast_daily_kwh < 1.0:
                return None

            # Check sun is up
            sun_state = self._hass.states.get("sun.sun")
            if sun_state is None:
                return None
            elevation = float(sun_state.attributes.get("elevation", 0))
            if elevation <= 0:
                return None

            # Estimate expected fraction using sin^2 model
            solar_start_hour = 8.0
            effective_start = 11.5  # Heavy morning shading
            solar_end_hour = 18.5
            current_hour_f = now.hour + now.minute / 60.0

            if current_hour_f <= solar_start_hour:
                return None

            solar_day_length = solar_end_hour - solar_start_hour
            elapsed = min(current_hour_f - solar_start_hour, solar_day_length)

            if elapsed / solar_day_length >= 0.95:
                return None  # Day nearly over

            progress = elapsed / solar_day_length
            cum_fraction = progress - math.sin(2 * math.pi * progress) / (2 * math.pi)

            expected_by_now = forecast_daily_kwh * cum_fraction

            if expected_by_now < 0.5:
                return None

            ratio = actual_yield / expected_by_now

            if ratio >= 0.70:
                return None  # Within 30%, no correction

            # Confidence weight: increases with more solar hours
            hours_of_data = max(0, current_hour_f - effective_start)
            confidence = min(0.95, 0.3 + hours_of_data * 0.1)

            correction = confidence * ratio + (1.0 - confidence) * 1.0
            correction = max(0.3, min(1.0, correction))

            return correction

        except Exception:
            return None

    # ------------------------------------------------------------------
    # Daytime check
    # ------------------------------------------------------------------

    def _is_daytime(self) -> bool:
        """Check if sun is up using HA sun.sun entity."""
        try:
            sun = self._hass.states.get("sun.sun")
            if sun is None:
                return True  # Assume daytime (conservative)
            return sun.state == "above_horizon"
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Load forecast
    # ------------------------------------------------------------------

    async def _build_load_forecast(
        self, now: datetime, current_load_w: float,
    ) -> tuple[list[float], str, float]:
        """Build 5-min load forecast.

        Priority:
            1. VRM hourly consumption forecast
            2. HA history (TODO)
            3. Typical residential curve

        Returns:
            Tuple of (load_kw list, source name, seasonal_factor).
        """
        load_kw: list[float] | None = None
        source = "unknown"
        inflation = 1.0 + self._tunables.load_inflation_pct / 100.0

        # Priority 1: VRM hourly consumption forecast
        if self._vrm and self._vrm.available:
            vrm_data = await self._vrm.get_hourly_forecasts()
            if vrm_data["consumption_hourly"]:
                load_kw = _vrm_hourly_to_5min(
                    vrm_data["consumption_hourly"], now, self.N, self.dt_hours,
                )
                source = "vrm_hourly"

        # Priority 2: HA history
        # TODO: HA recorder queries require async recorder API access
        # (homeassistant.components.recorder.get_instance().async_add_executor_job)
        # which is complex. For now, skip to priority 3.

        # Priority 3: Typical residential profile
        if load_kw is None:
            vrm_forecast_entity = self._entities.get(
                "vrm_forecast", "sensor.vrm_solar_forecast_tomorrow",
            )
            daily_kwh = float(
                self._get_attribute(vrm_forecast_entity, "consumption_today_kwh", 22)
            )
            load_kw = _load_typical_profile(now, daily_kwh, self.N, self.dt_hours)
            source = "typical_profile"

        # Seasonal adjustment
        seasonal = await self._seasonal_load_scale(now)
        load_kw = [val * seasonal for val in load_kw]

        # Inflate by safety margin
        load_kw = [val * inflation for val in load_kw]

        # Real-time indoor temperature AC boost
        ac_boost_kw = self._indoor_temp_ac_boost()
        if ac_boost_kw > 0:
            _LOGGER.info(
                "Indoor AC boost: +%.1fkW for next %dh (2h full, then taper)",
                ac_boost_kw, self._tunables.indoor_ac_boost_hours,
            )
        if ac_boost_kw > 0:
            boost_steps = self._tunables.indoor_ac_boost_hours * self._tunables.steps_per_hour
            for i in range(min(boost_steps, len(load_kw))):
                taper_start = 2 * self._tunables.steps_per_hour
                if i < taper_start:
                    load_kw[i] += ac_boost_kw
                else:
                    frac = 1.0 - (i - taper_start) / (boost_steps - taper_start)
                    load_kw[i] += ac_boost_kw * max(0, frac)

        # Inject current real value
        if load_kw:
            load_kw[0] = current_load_w / 1000.0

        return load_kw, source, seasonal

    # ------------------------------------------------------------------
    # Indoor temp AC boost
    # ------------------------------------------------------------------

    def _indoor_temp_ac_boost(self) -> float:
        """Check indoor temps and AC status to estimate extra cooling load.

        Two signals, uses the HIGHER of the two:
        1. AC confirmed running (climate entity not 'off') -> flat kW per unit
        2. Room temp above threshold -> scaled kW per degree per zone
        """
        try:
            # Entity lists from config entry data
            indoor_temp_entities = self._entities.get(
                "indoor_temp_entities", "",
            )
            indoor_ac_climate_entities = self._entities.get(
                "indoor_ac_climate_entities", "",
            )

            # Parse comma-separated entity lists if stored as strings
            if isinstance(indoor_temp_entities, str):
                temp_entities = [
                    e.strip() for e in indoor_temp_entities.split(",") if e.strip()
                ]
            else:
                temp_entities = list(indoor_temp_entities) if indoor_temp_entities else []

            if isinstance(indoor_ac_climate_entities, str):
                ac_entities = [
                    e.strip() for e in indoor_ac_climate_entities.split(",") if e.strip()
                ]
            else:
                ac_entities = list(indoor_ac_climate_entities) if indoor_ac_climate_entities else []

            # Signal 1: check if AC units are actually running
            ac_running_kw = 0.0
            for entity in ac_entities:
                state = self._hass.states.get(entity)
                if state is None:
                    continue
                hvac_state = state.state
                if hvac_state not in ("off", "unavailable", "unknown"):
                    ac_running_kw += self._tunables.indoor_ac_running_kw

            # Signal 2: indoor temp above threshold
            temp_boost_kw = 0.0
            max_excess = 0.0
            zones_hot = 0
            for entity in temp_entities:
                state = self._hass.states.get(entity)
                if state is None:
                    continue
                try:
                    temp = float(state.state)
                except (ValueError, TypeError):
                    continue
                excess = temp - self._tunables.indoor_temp_ac_threshold
                if excess > 0:
                    max_excess = max(max_excess, excess)
                    zones_hot += 1
            if zones_hot > 0:
                temp_boost_kw = zones_hot * max_excess * self._tunables.indoor_ac_kw_per_degree

            # Use the higher of the two signals
            boost = max(ac_running_kw, temp_boost_kw)
            return min(boost, 5.0)  # Cap at 5kW
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Seasonal load scaling
    # ------------------------------------------------------------------

    async def _seasonal_load_scale(self, now: datetime) -> float:
        """Compute seasonal load multiplier from VRM monthly history + temperature.

        Uses two signals:
        1. VRM historical consumption: what's typical for this month vs annual average?
        2. Current temperature: adjust for heating/cooling demand vs comfort band.

        Returns a multiplier clamped to [0.7, 1.5].
        """
        if not self._tunables.seasonal_load_adjustment:
            return 1.0

        scale = 1.0

        # Signal 1: VRM monthly consumption profile
        if self._vrm and self._vrm.available:
            monthly = await self._vrm.get_monthly_consumption(months_back=12)
            if len(monthly) >= 6:
                annual_avg = sum(monthly.values()) / len(monthly)
                this_month = monthly.get(now.month)
                if this_month and annual_avg > 0:
                    ratio = this_month / annual_avg
                    scale = 0.3 + 0.7 * ratio

        # Signal 2: Temperature adjustment
        try:
            weather_entity = self._entities.get("weather_entity", "weather.home")
            weather_state = self._hass.states.get(weather_entity)
            if weather_state is not None:
                temp_c = float(weather_state.attributes.get("temperature", 20))

                if temp_c < self._tunables.temp_base_cool:
                    delta = self._tunables.temp_base_cool - temp_c
                    scale *= 1.0 + delta * self._tunables.temp_cool_pct_per_degree / 100.0
                elif temp_c > self._tunables.temp_base_heat:
                    delta = temp_c - self._tunables.temp_base_heat
                    scale *= 1.0 + delta * self._tunables.temp_heat_pct_per_degree / 100.0
        except Exception:
            pass  # No temp data -- seasonal signal alone is fine

        return max(0.7, min(1.5, scale))

    # ------------------------------------------------------------------
    # VRM daily scale
    # ------------------------------------------------------------------

    def _get_vrm_daily_scale(self, profile_kw: list[float]) -> float:
        """Scale HA history profile to match VRM's weather-adjusted daily forecast.

        Preserves the shaded shape but adjusts the amplitude for weather.
        """
        try:
            vrm_forecast_entity = self._entities.get(
                "vrm_forecast", "sensor.vrm_solar_forecast_tomorrow",
            )
            vrm_today = float(
                self._get_attribute(vrm_forecast_entity, "forecast_today_kwh", 0)
            )
            if vrm_today <= 0:
                return 1.0

            profile_daily_kwh = sum(profile_kw)
            if profile_daily_kwh <= 0:
                return 1.0

            scale = vrm_today / profile_daily_kwh
            return max(0.3, min(2.0, scale))
        except Exception:
            return 1.0

    # ------------------------------------------------------------------
    # Sunset step
    # ------------------------------------------------------------------

    def _compute_sunset_step(self, now: datetime) -> int | None:
        """Find the horizon step index corresponding to sunset."""
        try:
            sun = self._hass.states.get("sun.sun")
            if sun is None:
                return None
            next_setting = sun.attributes.get("next_setting")
            if not next_setting:
                return None
            sunset_dt = datetime.fromisoformat(
                next_setting.replace("Z", "+00:00"),
            )
            sunset_local = sunset_dt.astimezone(now.astimezone().tzinfo)
            delta_hours = (sunset_local - now).total_seconds() / 3600
            if delta_hours < 0 or delta_hours > self._tunables.forecast_hours:
                return None
            return int(delta_hours / self.dt_hours)
        except Exception:
            return None
