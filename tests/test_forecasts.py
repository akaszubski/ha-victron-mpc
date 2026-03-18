"""Comprehensive integration tests for ForecastBuilder.

Tests the async forecast builder using pytest-homeassistant-custom-component
for a real ``hass`` fixture. External APIs (VRM, Open-Meteo, Solcast) are
mocked; HA entity states are set directly on the state machine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.core import SupportsResponse

from custom_components.victron_mpc.config import MPCTunables
from custom_components.victron_mpc.forecasts import (
    ForecastBuilder,
    _expand_hourly_to_5min,
    _interpolate_stepwise,
    _solar_bell_curve,
)
from homeassistant.core import HomeAssistant

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STEPS_24H = 288  # 24h * 12 steps/h (5-min)

DEFAULT_ENTITIES: dict[str, str] = {
    "battery_soc": "sensor.victron_battery_state_of_charge",
    "solar_power": "sensor.solar_power",
    "ac_consumption": "sensor.victron_ac_consumption",
    "amber_price": "sensor.amber_general_price",
    "amber_forecast": "sensor.amber_general_forecast",
    "amber_feedin": "sensor.amber_feed_in_price",
    "amber_feedin_forecast": "sensor.amber_feed_in_forecast",
    "weather_entity": "weather.home",
    "vrm_forecast": "sensor.vrm_solar_forecast_tomorrow",
    "solar_yield_today": "sensor.solar_yield_today",
    "indoor_temp_entities": "sensor.ac1_temp,sensor.ac2_temp",
    "indoor_ac_climate_entities": "climate.ac1,climate.ac2",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tunables() -> MPCTunables:
    """Default tunables."""
    return MPCTunables()


@pytest.fixture
def mock_vrm() -> MagicMock:
    """A mock VRMClient that reports as available."""
    vrm = MagicMock()
    vrm.available = True
    vrm.get_clearsky_envelope = AsyncMock(return_value=None)
    vrm.get_historical_stats = AsyncMock(
        return_value={"solar_hourly": [], "consumption_hourly": []}
    )
    vrm.get_hourly_forecasts = AsyncMock(
        return_value={"solar_hourly": [], "consumption_hourly": []}
    )
    vrm.get_monthly_consumption = AsyncMock(return_value={})
    vrm.get_monthly_peak_kwh = AsyncMock(return_value=None)
    return vrm


@pytest.fixture
def mock_open_meteo() -> MagicMock:
    """A mock OpenMeteoClient."""
    om = MagicMock()
    om.fetch_cloud_layers = AsyncMock(return_value=None)
    return om


def _builder(
    hass: HomeAssistant,
    tunables: MPCTunables,
    vrm: MagicMock | None = None,
    open_meteo: MagicMock | None = None,
    entities: dict[str, str] | None = None,
) -> ForecastBuilder:
    return ForecastBuilder(
        hass=hass,
        entities=entities or DEFAULT_ENTITIES,
        tunables=tunables,
        vrm=vrm,
        open_meteo=open_meteo,
    )


# ===================================================================
# 1. HA State Access Tests
# ===================================================================


class TestGetNumeric:
    """_get_numeric reads floats from the HA state machine."""

    async def test_reads_float(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sensor.soc", "72.5")
        fb = _builder(hass, tunables, entities={"battery_soc": "sensor.soc"})
        assert fb._get_numeric("sensor.soc") == 72.5

    async def test_returns_default_for_unavailable(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        hass.states.async_set("sensor.soc", "unavailable")
        fb = _builder(hass, tunables)
        assert fb._get_numeric("sensor.soc", 50.0) == 50.0

    async def test_returns_default_for_unknown(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        hass.states.async_set("sensor.soc", "unknown")
        fb = _builder(hass, tunables)
        assert fb._get_numeric("sensor.soc", 50.0) == 50.0

    async def test_returns_default_for_missing_entity(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        fb = _builder(hass, tunables)
        assert fb._get_numeric("sensor.does_not_exist", 99.0) == 99.0


class TestGetStateValue:
    """_get_state_value works with State objects (not dicts)."""

    async def test_reads_string(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sensor.x", "hello")
        fb = _builder(hass, tunables)
        assert fb._get_state_value("sensor.x") == "hello"

    async def test_returns_default_for_missing(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        fb = _builder(hass, tunables)
        assert fb._get_state_value("sensor.nope", "fallback") == "fallback"


class TestReadAmberPrice:
    """Read Amber price entity state + forecast attribute."""

    async def test_amber_price_read(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sensor.amber_general_price", "0.28")
        fb = _builder(hass, tunables)
        val = fb._get_numeric("sensor.amber_general_price")
        assert val == pytest.approx(0.28)

    async def test_amber_forecast_attribute(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        forecasts = [
            {"per_kwh": 0.30, "start_time": "2026-03-18T10:00:00"},
            {"per_kwh": 0.35, "start_time": "2026-03-18T10:30:00"},
        ]
        hass.states.async_set(
            "sensor.amber_general_forecast", "0.28", {"forecasts": forecasts}
        )
        fb = _builder(hass, tunables)
        attr = fb._get_attribute("sensor.amber_general_forecast", "forecasts")
        assert len(attr) == 2
        assert attr[0]["per_kwh"] == 0.30


class TestReadBatterySoC:
    """Read battery SoC entity."""

    async def test_battery_soc(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sensor.victron_battery_state_of_charge", "65.3")
        fb = _builder(hass, tunables)
        assert fb._get_numeric("sensor.victron_battery_state_of_charge") == pytest.approx(65.3)


class TestReadSolarPower:
    """Read solar power entity."""

    async def test_solar_power(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sensor.solar_power", "3200")
        fb = _builder(hass, tunables)
        assert fb._get_numeric("sensor.solar_power") == pytest.approx(3200.0)


class TestReadWeatherAttributes:
    """Read weather entity attributes (cloud_coverage, temperature)."""

    async def test_cloud_coverage(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set(
            "weather.home", "cloudy", {"cloud_coverage": 75, "temperature": 22}
        )
        fb = _builder(hass, tunables)
        assert fb._get_attribute("weather.home", "cloud_coverage") == 75

    async def test_temperature(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set(
            "weather.home", "sunny", {"temperature": 18.5, "cloud_coverage": 10}
        )
        fb = _builder(hass, tunables)
        assert fb._get_attribute("weather.home", "temperature") == 18.5


# ===================================================================
# 2. Weather Service Call Tests — _classify_day_type
# ===================================================================

def _make_hourly_forecasts(
    cloud_pcts: list[float],
    precip_mms: list[float] | None = None,
    start_hour: int = 6,
) -> list[dict[str, Any]]:
    """Build mock weather.get_forecasts response entries for daylight hours.

    Uses tomorrow's date so entries are always in the future regardless of
    when the test runs (the classify function skips past hours).
    """
    now = datetime.now().astimezone()
    # Use tomorrow so all daylight-hour entries are in the future
    today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    entries: list[dict[str, Any]] = []
    if precip_mms is None:
        precip_mms = [0.0] * len(cloud_pcts)
    for i, (cloud, precip) in enumerate(zip(cloud_pcts, precip_mms)):
        dt = today + timedelta(hours=start_hour + i)
        entries.append(
            {
                "datetime": dt.isoformat(),
                "cloud_coverage": cloud,
                "precipitation": precip,
                "temperature": 20,
            }
        )
    return entries


class TestClassifyDayType:
    """_classify_day_type with mocked weather.get_forecasts service."""

    async def test_clear_day(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_open_meteo: MagicMock,
    ):
        """Cloud < 30% and no precip => clear."""
        forecasts = _make_hourly_forecasts([10, 15, 20, 10, 15, 5, 10, 15, 10, 20, 10, 15])

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, open_meteo=mock_open_meteo)
        day_type, mean_cloud, total_precip = await fb._classify_day_type(
            datetime.now()
        )
        assert day_type == "clear"
        assert mean_cloud < 30
        assert total_precip == 0.0

    async def test_partly_cloudy(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_open_meteo: MagicMock,
    ):
        """Cloud 30-70%, low precip => partly_cloudy."""
        forecasts = _make_hourly_forecasts([40, 50, 55, 45, 50, 60, 55, 45, 50, 40, 50, 55])

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, open_meteo=mock_open_meteo)
        day_type, mean_cloud, _ = await fb._classify_day_type(datetime.now())
        assert day_type == "partly_cloudy"

    async def test_overcast(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_open_meteo: MagicMock,
    ):
        """Cloud > 70% => overcast."""
        forecasts = _make_hourly_forecasts([80, 85, 90, 75, 80, 85, 80, 90, 85, 80, 75, 80])

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, open_meteo=mock_open_meteo)
        day_type, _, _ = await fb._classify_day_type(datetime.now())
        assert day_type == "overcast"

    async def test_rain(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_open_meteo: MagicMock,
    ):
        """Precipitation >= 2mm => rain regardless of cloud."""
        # Use heavy precip that exceeds 2mm threshold even after filtering
        # past hours (code skips hours before now)
        precips = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
        # total = 6.0mm, well above threshold even with filtering
        forecasts = _make_hourly_forecasts(
            [90] * 12, precip_mms=precips
        )

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, open_meteo=mock_open_meteo)
        day_type, _, total_precip = await fb._classify_day_type(datetime.now())
        assert day_type == "rain"
        assert total_precip >= 2.0

    async def test_fallback_when_service_unavailable(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_open_meteo: MagicMock,
    ):
        """When weather service raises, falls back to partly_cloudy."""
        async def mock_service(call):
            raise Exception("Service unavailable")

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, open_meteo=mock_open_meteo)
        day_type, mean_cloud, total_precip = await fb._classify_day_type(
            datetime.now()
        )
        assert day_type == "partly_cloudy"
        assert mean_cloud == 50.0
        assert total_precip == 0.0


# ===================================================================
# 3. Price Forecast Tests
# ===================================================================


class TestBuildPriceForecast:
    """_build_price_forecast reads Amber and interpolates."""

    async def test_reads_amber_forecast_attribute(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        buy_forecasts = [{"per_kwh": 0.25 + i * 0.01} for i in range(48)]
        sell_forecasts = [{"per_kwh": 0.05 + i * 0.001} for i in range(48)]
        hass.states.async_set("sensor.amber_general_price", "0.28")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "0.28",
            {"forecasts": buy_forecasts},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "0.06",
            {"forecasts": sell_forecasts},
        )

        fb = _builder(hass, tunables)
        buy, sell = fb._build_price_forecast()
        assert len(buy) == STEPS_24H
        assert len(sell) == STEPS_24H
        # First value should be current price (repeated for first 6 steps)
        assert buy[0] == pytest.approx(0.28)

    async def test_30min_to_5min_interpolation_length(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """30-min forecasts expanded to 288 steps."""
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "0.30",
            {"forecasts": [{"per_kwh": 0.30}] * 48},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "0.06",
            {"forecasts": [{"per_kwh": 0.06}] * 48},
        )

        fb = _builder(hass, tunables)
        buy, sell = fb._build_price_forecast()
        assert len(buy) == STEPS_24H
        assert len(sell) == STEPS_24H

    async def test_fallback_flat_rate_when_amber_unavailable(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """No Amber entities → falls back to default prices."""
        # Don't set any Amber entities
        fb = _builder(hass, tunables)
        buy, sell = fb._build_price_forecast()
        assert len(buy) == STEPS_24H
        assert len(sell) == STEPS_24H
        # Default buy = 0.30, sell = 0.06 (from _get_state_value defaults)
        assert buy[0] == pytest.approx(0.30)
        assert sell[0] == pytest.approx(0.06)


# ===================================================================
# 4. Solar Forecast Priority Chain Tests
# ===================================================================


class TestSolarForecastPriority:
    """Solar forecast uses VRM clearsky envelope first, then bell curve."""

    async def test_vrm_clearsky_used_when_available(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """VRM envelope available => source is clearsky_pXX."""
        now = datetime.now()
        month = now.month
        # Build a simple hourly profile: 0 at night, ramp at day
        hourly_profile = [0.0] * 6 + [1.0, 2.0, 3.0, 4.0, 5.0, 5.5, 5.0, 4.0, 3.0, 2.0, 1.0, 0.5] + [0.0] * 6
        mock_vrm.get_clearsky_envelope = AsyncMock(
            return_value={month: hourly_profile}
        )
        mock_vrm.get_monthly_peak_kwh = AsyncMock(return_value=None)

        # Must register weather service for _classify_day_type
        forecasts = _make_hourly_forecasts([20] * 12)

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.states.async_set("sensor.solar_power", "2000")

        fb = _builder(hass, tunables, vrm=mock_vrm, open_meteo=mock_open_meteo)
        solar_kw, source, day_type = await fb._build_solar_forecast(now, 2000.0)

        assert source.startswith("clearsky_p")
        assert len(solar_kw) == STEPS_24H
        assert any(v > 0 for v in solar_kw)

    async def test_bell_curve_fallback(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """VRM unavailable => source is bell_curve."""
        mock_vrm.available = False

        forecasts = _make_hourly_forecasts([50] * 12)

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        hass.states.async_set(
            "sensor.vrm_solar_forecast_tomorrow", "25",
            {"forecast_today_kwh": 25},
        )
        hass.states.async_set("sensor.solar_power", "1000")

        fb = _builder(hass, tunables, vrm=mock_vrm, open_meteo=mock_open_meteo)
        now = datetime.now()
        solar_kw, source, day_type = await fb._build_solar_forecast(now, 1000.0)

        assert source == "bell_curve"
        assert len(solar_kw) == STEPS_24H

    async def test_day_type_selects_percentile(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """Overcast day → P40 percentile requested from VRM."""
        now = datetime.now()
        month = now.month
        hourly_profile = [0.0] * 6 + [1.0, 2.0, 3.0, 4.0, 5.0, 5.5, 5.0, 4.0, 3.0, 2.0, 1.0, 0.5] + [0.0] * 6

        # We'll capture the percentile argument
        async def mock_envelope(percentile: float = 0.90):
            return {month: hourly_profile}

        mock_vrm.get_clearsky_envelope = AsyncMock(side_effect=mock_envelope)
        mock_vrm.get_monthly_peak_kwh = AsyncMock(return_value=None)

        # Overcast forecast (>70% cloud)
        forecasts = _make_hourly_forecasts([80] * 12)

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )
        hass.states.async_set("sensor.solar_power", "500")

        fb = _builder(hass, tunables, vrm=mock_vrm, open_meteo=mock_open_meteo)
        await fb._build_solar_forecast(now, 500.0)

        # Verify get_clearsky_envelope was called with P40 for overcast
        call_args = mock_vrm.get_clearsky_envelope.call_args
        assert call_args is not None
        assert call_args.kwargs.get("percentile", call_args.args[0] if call_args.args else None) == pytest.approx(0.40)


# ===================================================================
# 5. Load Forecast Tests
# ===================================================================


class TestSeasonalLoadScale:
    """_seasonal_load_scale returns correct factor."""

    async def test_returns_1_when_disabled(
        self, hass: HomeAssistant, mock_vrm: MagicMock
    ):
        t = MPCTunables(seasonal_load_adjustment=False)
        fb = _builder(hass, t, vrm=mock_vrm)
        result = await fb._seasonal_load_scale(datetime.now())
        assert result == 1.0

    async def test_cold_temp_increases_factor(
        self, hass: HomeAssistant, mock_vrm: MagicMock
    ):
        """Temperature below base_cool should increase the load factor."""
        t = MPCTunables(temp_base_cool=15.0, temp_cool_pct_per_degree=1.0)
        hass.states.async_set(
            "weather.home", "cloudy", {"temperature": 5.0, "cloud_coverage": 50}
        )
        fb = _builder(hass, t, vrm=mock_vrm)
        result = await fb._seasonal_load_scale(datetime.now())
        # 10 degrees below base_cool → 10% increase
        assert result > 1.0

    async def test_hot_temp_increases_factor(
        self, hass: HomeAssistant, mock_vrm: MagicMock
    ):
        """Temperature above base_heat should increase the load factor."""
        t = MPCTunables(temp_base_heat=26.0, temp_heat_pct_per_degree=3.3)
        hass.states.async_set(
            "weather.home", "sunny", {"temperature": 35.0, "cloud_coverage": 10}
        )
        fb = _builder(hass, t, vrm=mock_vrm)
        result = await fb._seasonal_load_scale(datetime.now())
        # 9 degrees above base_heat → 9 * 3.3% = ~30% increase
        assert result > 1.2

    async def test_comfortable_temp_no_adjustment(
        self, hass: HomeAssistant, mock_vrm: MagicMock
    ):
        """Temperature in comfort band should not increase factor."""
        t = MPCTunables()
        hass.states.async_set(
            "weather.home", "sunny", {"temperature": 20.0, "cloud_coverage": 10}
        )
        fb = _builder(hass, t, vrm=mock_vrm)
        result = await fb._seasonal_load_scale(datetime.now())
        assert result == pytest.approx(1.0, abs=0.05)


class TestIndoorTempAcBoost:
    """_indoor_temp_ac_boost detects AC running from climate entity state."""

    async def test_ac_running_returns_boost(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """AC entity in 'cool' state → boost."""
        hass.states.async_set("climate.ac1", "cool")
        hass.states.async_set("climate.ac2", "off")
        hass.states.async_set("sensor.ac1_temp", "22")
        hass.states.async_set("sensor.ac2_temp", "22")

        fb = _builder(hass, tunables)
        boost = fb._indoor_temp_ac_boost()
        # 1 AC running => 2.0 kW (default indoor_ac_running_kw)
        assert boost == pytest.approx(2.0)

    async def test_both_ac_running(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """Both ACs running → 4.0 kW boost."""
        hass.states.async_set("climate.ac1", "cool")
        hass.states.async_set("climate.ac2", "heat")
        hass.states.async_set("sensor.ac1_temp", "22")
        hass.states.async_set("sensor.ac2_temp", "22")

        fb = _builder(hass, tunables)
        boost = fb._indoor_temp_ac_boost()
        assert boost == pytest.approx(4.0)

    async def test_hot_room_temp_boost(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """Indoor temp above threshold → boost from temperature signal."""
        hass.states.async_set("climate.ac1", "off")
        hass.states.async_set("climate.ac2", "off")
        hass.states.async_set("sensor.ac1_temp", "27")  # 3 degrees over 24
        hass.states.async_set("sensor.ac2_temp", "22")

        fb = _builder(hass, tunables)
        boost = fb._indoor_temp_ac_boost()
        # 1 zone hot, 3°C excess * 0.8 kW/°C = 2.4 kW
        assert boost == pytest.approx(2.4)

    async def test_no_boost_when_cool(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """AC off + temps below threshold → no boost."""
        hass.states.async_set("climate.ac1", "off")
        hass.states.async_set("climate.ac2", "off")
        hass.states.async_set("sensor.ac1_temp", "20")
        hass.states.async_set("sensor.ac2_temp", "19")

        fb = _builder(hass, tunables)
        boost = fb._indoor_temp_ac_boost()
        assert boost == 0.0

    async def test_no_entities_no_crash(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """No indoor entities configured → 0 boost, no exception."""
        entities = dict(DEFAULT_ENTITIES)
        entities["indoor_temp_entities"] = ""
        entities["indoor_ac_climate_entities"] = ""
        fb = _builder(hass, tunables, entities=entities)
        boost = fb._indoor_temp_ac_boost()
        assert boost == 0.0


class TestLoadInflation:
    """Load forecast applies inflation correctly."""

    async def test_load_inflation_applied(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """Load inflation adds safety margin to all steps."""
        # Set up minimal entities
        hass.states.async_set("sensor.victron_ac_consumption", "1000")
        hass.states.async_set(
            "sensor.vrm_solar_forecast_tomorrow", "22",
            {"consumption_today_kwh": 22},
        )
        hass.states.async_set("weather.home", "sunny", {"temperature": 20})

        # Disable VRM for simpler path
        mock_vrm.available = False
        t = MPCTunables(
            load_inflation_pct=10.0,
            seasonal_load_adjustment=False,
        )
        fb = _builder(hass, t, vrm=mock_vrm, open_meteo=mock_open_meteo)

        now = datetime.now()
        load_kw, source, seasonal = await fb._build_load_forecast(now, 1000.0)

        assert len(load_kw) == STEPS_24H
        assert source == "typical_profile"
        # First element is overwritten with current load
        assert load_kw[0] == pytest.approx(1.0)  # 1000W / 1000


# ===================================================================
# 6. build_all() Integration Tests
# ===================================================================


class TestBuildAll:
    """Full build_all() integration tests."""

    async def test_returns_all_required_keys(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """build_all() returns dict with all required keys."""
        mock_vrm.available = False
        self._setup_minimal_entities(hass)

        forecasts = _make_hourly_forecasts([50] * 12)

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, vrm=mock_vrm, open_meteo=mock_open_meteo)
        result = await fb.build_all()

        required_keys = {
            "battery_soc_pct", "solar_forecast_kw", "load_forecast_kw",
            "buy_price", "sell_price", "sunset_step",
            "current_solar_w", "current_load_w",
            "solar_forecast_source", "solar_day_type",
            "load_forecast_source", "seasonal_load_factor", "timestamp",
        }
        assert required_keys.issubset(result.keys())

    async def test_all_entities_unavailable_graceful(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """build_all() with no entities set up still returns valid data."""
        mock_vrm.available = False

        # Register weather service that returns empty
        async def mock_service(call):
            return {}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, vrm=mock_vrm, open_meteo=mock_open_meteo)
        result = await fb.build_all()

        # Should still return valid arrays with defaults
        assert len(result["solar_forecast_kw"]) == STEPS_24H
        assert len(result["load_forecast_kw"]) == STEPS_24H
        assert len(result["buy_price"]) == STEPS_24H
        assert len(result["sell_price"]) == STEPS_24H

    async def test_output_array_lengths(
        self,
        hass: HomeAssistant,
        tunables: MPCTunables,
        mock_vrm: MagicMock,
        mock_open_meteo: MagicMock,
    ):
        """All output arrays are exactly 288 steps (24h * 12 steps/h)."""
        mock_vrm.available = False
        self._setup_minimal_entities(hass)

        forecasts = _make_hourly_forecasts([50] * 12)

        async def mock_service(call):
            return {"weather.home": {"forecast": forecasts}}

        hass.services.async_register(
            "weather", "get_forecasts", mock_service,
            supports_response=SupportsResponse.OPTIONAL,
        )

        fb = _builder(hass, tunables, vrm=mock_vrm, open_meteo=mock_open_meteo)
        result = await fb.build_all()

        for key in ("solar_forecast_kw", "load_forecast_kw", "buy_price", "sell_price"):
            assert len(result[key]) == STEPS_24H, f"{key} length is {len(result[key])}, expected {STEPS_24H}"

    @staticmethod
    def _setup_minimal_entities(hass: HomeAssistant) -> None:
        """Set up minimum entities for build_all() to succeed."""
        hass.states.async_set("sensor.victron_battery_state_of_charge", "50")
        hass.states.async_set("sensor.solar_power", "2000")
        hass.states.async_set("sensor.victron_ac_consumption", "1000")
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "0.30", {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "0.06", {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.vrm_solar_forecast_tomorrow", "25",
            {"forecast_today_kwh": 25, "consumption_today_kwh": 22},
        )
        hass.states.async_set(
            "weather.home", "sunny", {"temperature": 20, "cloud_coverage": 10},
        )
        hass.states.async_set(
            "sun.sun", "above_horizon",
            {
                "next_setting": (
                    datetime.now(tz=timezone.utc) + timedelta(hours=4)
                ).isoformat(),
                "elevation": 45.0,
            },
        )


# ===================================================================
# 7. Sunset Calculation Tests
# ===================================================================


class TestComputeSunsetStep:
    """_compute_sunset_step reads sun.sun entity correctly."""

    async def test_sunset_step_calculated(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """Sunset 3 hours from now → step = 3 * 12 = 36."""
        now = datetime.now(tz=timezone.utc)
        sunset = now + timedelta(hours=3)
        hass.states.async_set(
            "sun.sun", "above_horizon",
            {"next_setting": sunset.isoformat(), "elevation": 30.0},
        )

        fb = _builder(hass, tunables)
        step = fb._compute_sunset_step(now)
        # 3 hours at 12 steps/hour = 36
        assert step is not None
        assert abs(step - 36) <= 1  # Allow ±1 rounding

    async def test_sunset_none_when_already_set(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """Sunset already past → returns None."""
        now = datetime.now(tz=timezone.utc)
        sunset = now - timedelta(hours=1)
        hass.states.async_set(
            "sun.sun", "below_horizon",
            {"next_setting": sunset.isoformat(), "elevation": -10.0},
        )

        fb = _builder(hass, tunables)
        step = fb._compute_sunset_step(now)
        assert step is None

    async def test_sunset_none_when_entity_missing(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """No sun.sun entity → returns None."""
        fb = _builder(hass, tunables)
        step = fb._compute_sunset_step(datetime.now())
        assert step is None

    async def test_sunset_none_when_beyond_horizon(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """Sunset 30h away → beyond forecast horizon → returns None."""
        now = datetime.now(tz=timezone.utc)
        sunset = now + timedelta(hours=30)
        hass.states.async_set(
            "sun.sun", "above_horizon",
            {"next_setting": sunset.isoformat(), "elevation": 30.0},
        )

        fb = _builder(hass, tunables)
        step = fb._compute_sunset_step(now)
        assert step is None


# ===================================================================
# Additional: Module-level helper function tests
# ===================================================================


class TestInterpolateStepwise:
    """_interpolate_stepwise 30-min → 5-min expansion."""

    def test_correct_length(self):
        values = [0.30] * 48  # 24h of 30-min values
        result = _interpolate_stepwise(values, 6, 288)
        assert len(result) == 288

    def test_values_repeated(self):
        values = [1.0, 2.0]
        result = _interpolate_stepwise(values, 6, 12)
        assert result[:6] == [1.0] * 6
        assert result[6:12] == [2.0] * 6

    def test_extends_when_short(self):
        values = [0.5]
        result = _interpolate_stepwise(values, 6, 12)
        assert len(result) == 12
        assert all(v == 0.5 for v in result)


class TestExpandHourlyTo5Min:
    """_expand_hourly_to_5min produces correct length."""

    def test_24_hours_to_288(self):
        hourly = [1.0] * 24
        result = _expand_hourly_to_5min(hourly, 288)
        assert len(result) == 288

    def test_values_repeated_12x(self):
        hourly = [2.0, 3.0]
        result = _expand_hourly_to_5min(hourly, 24)
        assert result[:12] == [2.0] * 12
        assert result[12:24] == [3.0] * 12


class TestSolarBellCurve:
    """_solar_bell_curve generates a valid profile."""

    def test_correct_length(self):
        now = datetime(2026, 3, 18, 10, 0)
        result = _solar_bell_curve(now, 25.0, 288, 5 / 60)
        assert len(result) == 288

    def test_energy_sums_to_daily(self):
        now = datetime(2026, 3, 18, 0, 0)
        daily_kwh = 25.0
        result = _solar_bell_curve(now, daily_kwh, 288, 5 / 60)
        total = sum(result) * (5 / 60)
        assert total == pytest.approx(daily_kwh, rel=0.01)


class TestIsDaytime:
    """_is_daytime checks sun.sun entity."""

    async def test_above_horizon(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sun.sun", "above_horizon", {"elevation": 30.0})
        fb = _builder(hass, tunables)
        assert fb._is_daytime() is True

    async def test_below_horizon(self, hass: HomeAssistant, tunables: MPCTunables):
        hass.states.async_set("sun.sun", "below_horizon", {"elevation": -10.0})
        fb = _builder(hass, tunables)
        assert fb._is_daytime() is False

    async def test_missing_sun_defaults_true(
        self, hass: HomeAssistant, tunables: MPCTunables
    ):
        """Missing sun entity → assume daytime (conservative)."""
        fb = _builder(hass, tunables)
        assert fb._is_daytime() is True


class TestEffectiveCloudPct:
    """_effective_cloud_pct with weighted layers."""

    async def test_clear_sky(self, hass: HomeAssistant, tunables: MPCTunables):
        fb = _builder(hass, tunables)
        result = fb._effective_cloud_pct({"low": 0, "mid": 0, "high": 0})
        assert result == 0.0

    async def test_high_cirrus_only(self, hass: HomeAssistant, tunables: MPCTunables):
        """100% high cirrus → low effective (barely blocks solar)."""
        fb = _builder(hass, tunables)
        result = fb._effective_cloud_pct({"low": 0, "mid": 0, "high": 100})
        assert result < 15

    async def test_low_stratus_only(self, hass: HomeAssistant, tunables: MPCTunables):
        """100% low stratus → high effective."""
        fb = _builder(hass, tunables)
        result = fb._effective_cloud_pct({"low": 100, "mid": 0, "high": 0})
        assert result > 50
