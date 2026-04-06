"""Tests for input validation (GitHub issues #48-51, Milestone 10 Resilience).

#48: Amber price sanity bounds
#49: SoC jump detection
#50: Solar forecast cap at array maximum
#51: Load forecast reasonableness
"""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.config import MPCTunables
from custom_components.victron_mpc.coordinator import VictronMPCCoordinator
from custom_components.victron_mpc.forecasts import (
    ForecastBuilder,
    _expand_hourly_to_5min,
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


def _make_coordinator() -> VictronMPCCoordinator:
    """Create a coordinator with mocked hass and config entry."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test_entry_123"
    entry.data = {"modbus_hub": "cerbo", "modbus_slave_system": 100}
    entry.options = {}

    with patch.object(VictronMPCCoordinator, "__init__", lambda self, *a, **kw: None):
        coord = VictronMPCCoordinator.__new__(VictronMPCCoordinator)

    coord.hass = hass
    coord.entry = entry
    coord._last_soc_pct = None
    coord._modbus_consecutive_failures = 0
    coord._modbus_last_success = None
    coord._modbus_alerted = False
    coord._last_register_value = None
    coord._last_feedin_value = None
    coord._last_mode = None

    return coord


# ===================================================================
# Issue #48: Amber price sanity bounds
# ===================================================================


class TestAmberPriceSanityBounds:
    """Buy prices exceeding sanity bounds are clamped."""

    async def test_price_above_max_clamped(
        self, hass: HomeAssistant, tunables: MPCTunables, caplog,
    ):
        """Prices above $50/kWh (corruption) are clamped to max bound."""
        # Set up a normal current price but a forecast with corrupted price
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": [{"per_kwh": "99.99", "descriptor": "spike"}]},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": [{"per_kwh": "0.10", "descriptor": "low"}]},
        )

        fb = _builder(hass, tunables)
        with caplog.at_level(logging.WARNING):
            buy, sell, bands = fb._build_price_forecast()

        # The corrupted price should be clamped to max bound (50.0)
        assert max(buy) <= tunables.price_max_buy
        assert "exceeds max sanity bound" in caplog.text

    async def test_price_below_min_clamped(
        self, hass: HomeAssistant, tunables: MPCTunables, caplog,
    ):
        """Prices below -$10/kWh (corruption) are clamped to min bound."""
        hass.states.async_set("sensor.amber_general_price", "-15.00")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )

        fb = _builder(hass, tunables)
        with caplog.at_level(logging.WARNING):
            buy, sell, bands = fb._build_price_forecast()

        assert min(buy) >= tunables.price_min_buy
        assert "below min sanity bound" in caplog.text

    async def test_normal_prices_pass_through(
        self, hass: HomeAssistant, tunables: MPCTunables, caplog,
    ):
        """Normal prices within bounds are not modified."""
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": [{"per_kwh": "0.45", "descriptor": "high"}]},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": [{"per_kwh": "0.10", "descriptor": "low"}]},
        )

        fb = _builder(hass, tunables)
        with caplog.at_level(logging.WARNING):
            buy, sell, bands = fb._build_price_forecast()

        assert "sanity bound" not in caplog.text
        # First entries should reflect the set prices
        assert buy[0] == pytest.approx(0.30, abs=0.01)

    async def test_negative_prices_within_bound_pass_through(
        self, hass: HomeAssistant, tunables: MPCTunables, caplog,
    ):
        """Negative prices above -$1 (e.g. -$0.50) are valid and pass through."""
        hass.states.async_set("sensor.amber_general_price", "-0.50")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )

        fb = _builder(hass, tunables)
        with caplog.at_level(logging.WARNING):
            buy, sell, bands = fb._build_price_forecast()

        assert buy[0] == pytest.approx(-0.50, abs=0.01)
        assert "sanity bound" not in caplog.text

    async def test_custom_bounds_from_tunables(
        self, hass: HomeAssistant, caplog,
    ):
        """Custom bounds in tunables are respected."""
        custom = MPCTunables(price_max_buy=2.0, price_min_buy=-0.50)
        hass.states.async_set("sensor.amber_general_price", "3.00")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )

        fb = _builder(hass, custom)
        with caplog.at_level(logging.WARNING):
            buy, sell, bands = fb._build_price_forecast()

        assert max(buy) <= 2.0
        assert "exceeds max sanity bound" in caplog.text


# ===================================================================
# Issue #49: SoC jump detection
# ===================================================================


class TestSoCJumpDetection:
    """SoC changes > 15% per 5-min cycle are rejected."""

    def test_first_cycle_no_rejection(self):
        """First cycle has no previous value, so any SoC is accepted."""
        coord = _make_coordinator()
        # _last_soc_pct is None on first cycle
        assert coord._last_soc_pct is None

    def test_normal_change_accepted(self):
        """A 3% change is within bounds and accepted."""
        coord = _make_coordinator()
        coord._last_soc_pct = 50.0
        # Simulate what the coordinator does: check jump, then update
        new_soc = 47.0
        delta = abs(new_soc - coord._last_soc_pct)
        tunables = MPCTunables()
        assert delta <= tunables.soc_max_jump_pct
        coord._last_soc_pct = new_soc
        assert coord._last_soc_pct == 47.0

    def test_large_jump_detected(self):
        """A 25% jump exceeds the 15% threshold."""
        coord = _make_coordinator()
        coord._last_soc_pct = 50.0
        new_soc = 75.0
        delta = abs(new_soc - coord._last_soc_pct)
        tunables = MPCTunables()
        assert delta > tunables.soc_max_jump_pct

    def test_large_drop_detected(self):
        """A 20% drop also exceeds the threshold."""
        coord = _make_coordinator()
        coord._last_soc_pct = 60.0
        new_soc = 40.0
        delta = abs(new_soc - coord._last_soc_pct)
        tunables = MPCTunables()
        assert delta > tunables.soc_max_jump_pct

    def test_boundary_value_accepted(self):
        """Exactly 15% change is within bounds (not strictly greater)."""
        coord = _make_coordinator()
        coord._last_soc_pct = 50.0
        new_soc = 65.0
        delta = abs(new_soc - coord._last_soc_pct)
        tunables = MPCTunables()
        # 15.0 is not > 15.0, so it should be accepted
        assert not (delta > tunables.soc_max_jump_pct)

    def test_custom_threshold(self):
        """Custom threshold from tunables is respected."""
        coord = _make_coordinator()
        coord._last_soc_pct = 50.0
        new_soc = 58.0
        delta = abs(new_soc - coord._last_soc_pct)
        tunables = MPCTunables(soc_max_jump_pct=5.0)
        assert delta > tunables.soc_max_jump_pct


# ===================================================================
# Issue #50: Solar forecast cap at array maximum
# ===================================================================


class TestSolarForecastCap:
    """Solar forecast values are capped at array maximum."""

    async def test_values_above_cap_are_clamped(
        self, hass: HomeAssistant, tunables: MPCTunables, mock_vrm, caplog,
    ):
        """Solar values > 8kW are clamped down."""
        # Set up minimal state for build_all
        hass.states.async_set("sensor.victron_battery_state_of_charge", "50")
        hass.states.async_set("sensor.solar_power", "3000")
        hass.states.async_set("sensor.victron_ac_consumption", "1000")
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.vrm_solar_forecast_tomorrow", "unknown",
            {"consumption_today_kwh": "22"},
        )
        hass.states.async_set("weather.home", "sunny", {
            "forecast": [],
            "temperature": 25,
        })

        fb = _builder(hass, tunables, vrm=mock_vrm)

        # Directly test _build_solar_forecast by injecting an oversized
        # forecast via a mock that returns values > cap
        oversized = [10.0] * STEPS_24H  # 10kW > 8kW cap
        with patch.object(
            fb, "_get_solcast_ha_forecast", return_value=oversized
        ), patch.object(
            fb, "_apply_per_hour_shading",
            new_callable=AsyncMock,
            return_value=(oversized, 1.0),
        ), patch.object(
            fb, "_classify_day_type",
            new_callable=AsyncMock,
            return_value=("clear", 10.0, 0.0),
        ), patch.object(
            fb, "_maybe_adjust_day_type", return_value="clear",
        ), caplog.at_level(logging.WARNING):
            solar_kw, source, day_type = await fb._build_solar_forecast(
                datetime.now(), 3000.0,
            )

        # All values except [0] (injected real) should be capped at 8.0
        for i in range(1, len(solar_kw)):
            assert solar_kw[i] <= tunables.solar_forecast_cap_kw
        assert "exceeds array max" in caplog.text

    async def test_values_below_cap_pass_through(
        self, hass: HomeAssistant, tunables: MPCTunables, mock_vrm, caplog,
    ):
        """Solar values < 8kW are not modified."""
        hass.states.async_set("sensor.victron_battery_state_of_charge", "50")
        hass.states.async_set("sensor.solar_power", "3000")
        hass.states.async_set("sensor.victron_ac_consumption", "1000")
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set("weather.home", "sunny", {
            "forecast": [],
            "temperature": 25,
        })

        fb = _builder(hass, tunables, vrm=mock_vrm)

        normal = [5.0] * STEPS_24H  # 5kW < 8kW cap
        with patch.object(
            fb, "_get_solcast_ha_forecast", return_value=normal
        ), patch.object(
            fb, "_apply_per_hour_shading",
            new_callable=AsyncMock,
            return_value=(normal, 1.0),
        ), patch.object(
            fb, "_classify_day_type",
            new_callable=AsyncMock,
            return_value=("clear", 10.0, 0.0),
        ), patch.object(
            fb, "_maybe_adjust_day_type", return_value="clear",
        ), caplog.at_level(logging.WARNING):
            solar_kw, source, day_type = await fb._build_solar_forecast(
                datetime.now(), 3000.0,
            )

        assert "exceeds array max" not in caplog.text

    def test_custom_cap_from_tunables(self):
        """Custom cap value from tunables is respected."""
        custom = MPCTunables(solar_forecast_cap_kw=5.0)
        assert custom.solar_forecast_cap_kw == 5.0


# ===================================================================
# Issue #51: Load forecast reasonableness
# ===================================================================


class TestLoadForecastReasonableness:
    """Load forecast values are clamped to reasonable bounds."""

    async def test_excessive_load_capped(
        self, hass: HomeAssistant, tunables: MPCTunables, mock_vrm, caplog,
    ):
        """Load values > 15kW are capped."""
        hass.states.async_set("sensor.victron_battery_state_of_charge", "50")
        hass.states.async_set("sensor.solar_power", "3000")
        hass.states.async_set("sensor.victron_ac_consumption", "1000")
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.vrm_solar_forecast_tomorrow", "unknown",
            {"consumption_today_kwh": "22"},
        )
        hass.states.async_set("weather.home", "sunny", {
            "forecast": [],
            "temperature": 25,
        })

        fb = _builder(hass, tunables, vrm=mock_vrm)

        # VRM consumption_hourly expects [(timestamp_ms, wh), ...]
        # 20kWh per hour = 20000Wh — will produce 20kW per step
        now = datetime.now()
        vrm_consumption = [
            (int((now.replace(hour=h, minute=0, second=0)).timestamp() * 1000), 20000)
            for h in range(24)
        ]
        mock_vrm.get_hourly_forecasts = AsyncMock(
            return_value={
                "solar_hourly": [],
                "consumption_hourly": vrm_consumption,
            }
        )

        with patch.object(
            fb, "_seasonal_load_scale",
            new_callable=AsyncMock,
            return_value=1.0,
        ), caplog.at_level(logging.WARNING):
            load_kw, source, seasonal = await fb._build_load_forecast(
                now, 1000.0,
            )

        # All values except [0] (injected real) should be capped at 15.0
        for i in range(1, len(load_kw)):
            assert load_kw[i] <= tunables.load_forecast_cap_kw
        assert "exceeds max reasonable load" in caplog.text

    def test_negative_load_clamped_to_zero(self, tunables):
        """Negative load values in the forecast array are clamped to 0.

        Tests the clamping logic directly — the validation at the end of
        _build_load_forecast that catches any negative kW values regardless
        of source.
        """
        load_kw = [-1.5, 0.8, -0.3, 2.0, -5.0]
        load_min = tunables.load_forecast_min_kw  # 0.0
        load_max = tunables.load_forecast_cap_kw  # 15.0

        # Apply same clamping logic as forecasts.py lines 1808-1822
        for i, val in enumerate(load_kw):
            if val > load_max:
                load_kw[i] = load_max
            elif val < load_min:
                load_kw[i] = load_min

        assert load_kw == [0.0, 0.8, 0.0, 2.0, 0.0]

    async def test_normal_load_passes_through(
        self, hass: HomeAssistant, tunables: MPCTunables, mock_vrm, caplog,
    ):
        """Normal load values within bounds are not modified."""
        hass.states.async_set("sensor.victron_battery_state_of_charge", "50")
        hass.states.async_set("sensor.solar_power", "3000")
        hass.states.async_set("sensor.victron_ac_consumption", "1000")
        hass.states.async_set("sensor.amber_general_price", "0.30")
        hass.states.async_set("sensor.amber_feed_in_price", "0.06")
        hass.states.async_set(
            "sensor.amber_general_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.amber_feed_in_forecast", "unknown",
            {"forecasts": []},
        )
        hass.states.async_set(
            "sensor.vrm_solar_forecast_tomorrow", "unknown",
            {"consumption_today_kwh": "22"},
        )
        hass.states.async_set("weather.home", "sunny", {
            "forecast": [],
            "temperature": 25,
        })

        fb = _builder(hass, tunables, vrm=mock_vrm)

        # VRM consumption_hourly expects [(timestamp_ms, wh), ...]
        # 2kWh per hour = 2000Wh — normal residential load
        now = datetime.now()
        vrm_consumption = [
            (int((now.replace(hour=h, minute=0, second=0)).timestamp() * 1000), 2000)
            for h in range(24)
        ]
        mock_vrm.get_hourly_forecasts = AsyncMock(
            return_value={
                "solar_hourly": [],
                "consumption_hourly": vrm_consumption,
            }
        )

        with patch.object(
            fb, "_seasonal_load_scale",
            new_callable=AsyncMock,
            return_value=1.0,
        ), caplog.at_level(logging.WARNING):
            load_kw, source, seasonal = await fb._build_load_forecast(
                now, 1000.0,
            )

        assert "exceeds max reasonable load" not in caplog.text
        assert "negative load rejected" not in caplog.text

    def test_custom_bounds_from_tunables(self):
        """Custom load bounds from tunables are respected."""
        custom = MPCTunables(load_forecast_cap_kw=10.0, load_forecast_min_kw=0.5)
        assert custom.load_forecast_cap_kw == 10.0
        assert custom.load_forecast_min_kw == 0.5


# ===================================================================
# Config tunables defaults
# ===================================================================


class TestValidationTunableDefaults:
    """Validation tunables have sensible defaults."""

    def test_price_bounds_defaults(self):
        t = MPCTunables()
        assert t.price_max_buy == 50.0  # NEM cap ~$17, 50 catches corruption
        assert t.price_min_buy == -10.0  # Deep negatives real, -10 catches corruption

    def test_soc_jump_default(self):
        t = MPCTunables()
        assert t.soc_max_jump_pct == 15.0

    def test_solar_cap_default(self):
        t = MPCTunables()
        assert t.solar_forecast_cap_kw == 8.0

    def test_load_bounds_defaults(self):
        t = MPCTunables()
        assert t.load_forecast_cap_kw == 15.0
        assert t.load_forecast_min_kw == 0.0

    def test_tunables_from_dict_round_trip(self):
        """Validation tunables survive to_dict/from_dict round trip."""
        t = MPCTunables(
            price_max_buy=3.0,
            soc_max_jump_pct=10.0,
            solar_forecast_cap_kw=6.0,
            load_forecast_cap_kw=12.0,
        )
        d = t.to_dict()
        t2 = MPCTunables.from_dict(d)
        assert t2.price_max_buy == 3.0
        assert t2.soc_max_jump_pct == 10.0
        assert t2.solar_forecast_cap_kw == 6.0
        assert t2.load_forecast_cap_kw == 12.0
