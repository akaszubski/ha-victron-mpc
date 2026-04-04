"""DataUpdateCoordinator for Victron MPC Battery Optimizer.

Replaces the Mac-based runner.py daemon. Runs the full optimization
cycle every 5 minutes natively inside Home Assistant:

    fetch state → build forecasts → LP optimize → apply overrides → write registers

The coordinator manages:
- External API caching (VRM, Open-Meteo, PetrolSpy)
- Override logic (spike, negative pricing, stale safety)
- Modbus register writes (R2901, R2706)
- Shadow mode (log without writing)
- Cell balancing tracking
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.fuel_price import FuelPriceClient
from .api.open_meteo import OpenMeteoClient
from .api.vrm import VRMClient
from .config import MPCTunables, VictronSystem
from .const import (
    CONF_VRM_INSTALLATION_ID,
    CONF_VRM_TOKEN,
    DOMAIN,
    LOGGER,
    REGISTER_BATTERYLIFE_STATE,
    REGISTER_ESS_MIN_SOC,
    REGISTER_FEEDIN_BLOCK,
    REGISTER_MAX_FEED_IN,
    UPDATE_INTERVAL_MINUTES,
)
from .forecast_accuracy import compute_forecast_accuracy
from .forecasts import ForecastBuilder
from .genai_monitor import (
    GENAI_CYCLE_INTERVAL,
    build_strategic_snapshot,
    run_deterministic_checks,
    run_genai_health_check,
)
from .optimizer import OptInput, OptOutput, compute_sunset_target, optimize
from .utils import scale_overnight_hold_reward

# Amber three-tier escalation thresholds
_AMBER_CAUTIOUS_MINUTES = 30.0
_AMBER_CAUTIOUS_PRICE = 0.50


def _build_soc_target_reward(
    now, horizon_steps, dt_hours, tunables, buy_prices, price_bands=None,
    solar_forecast_kw=None,
):
    """Build time-varying SoC target reward array."""
    rewards = []
    for i in range(horizon_steps):
        hour_offset = i * dt_hours
        hour_of_day = (now.hour + now.minute / 60 + hour_offset) % 24
        if 17 <= hour_of_day < 21:
            base = tunables.soc_profile_peak
        elif 11 <= hour_of_day < 17:
            base = tunables.soc_profile_pre_peak
        elif 6 <= hour_of_day < 9:
            base = tunables.soc_profile_morning
        elif 22 <= hour_of_day or hour_of_day < 6:
            base = tunables.soc_profile_overnight
        else:
            base = tunables.soc_profile_default
        # Solar-aware insurance: when forecast solar is low (<300W),
        # stored battery has insurance value as the only backup if load
        # or prices spike. Boost reward toward grid price.
        if solar_forecast_kw is not None and i < len(solar_forecast_kw):
            solar_w = solar_forecast_kw[i] * 1000
            if solar_w < 300:
                buy_at_step = buy_prices[i] if i < len(buy_prices) else 0.15
                solar_insurance = buy_at_step * 0.6
                base = max(base, solar_insurance)
        # Price bonus from Amber bands (seasonally adaptive)
        band = price_bands[i] if price_bands and i < len(price_bands) else "low"
        if band in ("extremely_low", "very_low"):
            base += tunables.grid_charge_boost
        elif band == "low":
            base += tunables.grid_charge_boost * 0.5
        rewards.append(round(base, 4))
    return rewards

def _compute_dynamic_terminal_reward(
    buy_prices: list[float],
    solar_forecast_kw: list[float],
    dt_hours: float,
    base_reward: float = 0.03,
    wear_cost: float = 0.02,
) -> float:
    """Terminal reward based on prices beyond horizon.

    Values stored energy at the cost of grid during no-solar hours
    at the tail of the forecast, not a fixed $0.03. Prevents LP from
    undervaluing battery when tomorrow morning is expensive + no solar.
    """
    if buy_prices is None or len(buy_prices) == 0:
        return base_reward
    N = len(buy_prices)
    tail_steps = min(72, N // 4)
    tail_start = N - tail_steps
    no_solar_prices = []
    for i in range(tail_start, N):
        solar = float(solar_forecast_kw[i]) if i < len(solar_forecast_kw) else 0.0
        if solar < 0.2:
            no_solar_prices.append(float(buy_prices[i]))
    if not no_solar_prices:
        return base_reward
    avg_no_solar_price = sum(no_solar_prices) / len(no_solar_prices)
    dynamic_reward = max(base_reward, avg_no_solar_price - wear_cost)
    return round(min(dynamic_reward, 0.08), 4)


class VictronMPCCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Victron MPC Battery Optimizer.

    Runs the optimization cycle every 5 minutes:
    1. Read current state from HA entities (battery, solar, load, prices)
    2. Fetch external APIs (VRM, Open-Meteo cloud layers) with caching
    3. Build forecasts (solar/load/price — 288 steps × 5-min)
    4. Run LP optimizer in executor thread (scipy, ~50ms)
    5. Apply overrides (spike → discharge, negative → charge)
    6. Write Modbus registers (R2901, R2706) if not shadow mode
    7. Return data dict consumed by sensor entities
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self._cycle_count = 0
        self._consecutive_failures = 0
        self._last_register_value: int | None = None
        self._last_feedin_value: int | None = None
        self._last_full_charge_check: datetime | None = None
        self._force_full_charge: bool = False

        # API clients — initialized in _async_setup
        self._vrm_client: VRMClient | None = None
        self._open_meteo_client: OpenMeteoClient | None = None
        self._fuel_price_client: FuelPriceClient | None = None
        self._forecast_builder: ForecastBuilder | None = None

        # API health tracking — for notifications
        self._api_health: dict[str, dict[str, Any]] = {
            "amber": {"down_since": None, "alerted": False},
            "vrm": {"down_since": None, "alerted": False},
            "open_meteo": {"down_since": None, "alerted": False},
        }

        # Modbus write health tracking
        self._modbus_consecutive_failures: int = 0
        self._modbus_last_success: datetime | None = None
        self._modbus_alerted: bool = False

        # Amber defensive discharge state
        self._amber_unavailable_since: datetime | None = None
        self._last_known_buy_price: float = 0.30  # Updated from tunables each cycle

        # Amber forecast accuracy tracking — rolling 7-day log
        # Each entry: {timestamp, forecast_prices: {+1h, +2h, +3h, +6h}, actual_price, spike_predicted, spike_actual}
        self._amber_forecast_log: list[dict[str, Any]] = []

        # GenAI health monitor state
        self._genai_cycle_count = 0
        self._last_genai_result: dict[str, str] = {}
        self._genai_consecutive_red = 0
        self._genai_history: list[dict[str, Any]] = []
        self._genai_history_max = 168  # 7 days x 24 hourly checks
        self._amber_forecast_log_max = 2016  # 7 days × 288 cycles/day
        self._forecast_accuracy_cache: dict = {}

        # Appliance monitoring (Phase 0 — data collection)
        self._appliance_log: list[dict[str, Any]] = []
        self._appliance_log_max = 2016

    async def _async_setup(self) -> None:
        """One-time setup called during first refresh (HA 2024.8+).

        Initialize API clients, validate Modbus connectivity.
        """
        session = async_get_clientsession(self.hass)
        data = self.entry.data
        options = self.entry.options

        # VRM client (optional — only if token configured)
        vrm_token = data.get(CONF_VRM_TOKEN) or options.get(CONF_VRM_TOKEN)
        vrm_install_id = data.get(CONF_VRM_INSTALLATION_ID) or options.get(
            CONF_VRM_INSTALLATION_ID
        )
        if vrm_token and vrm_install_id:
            self._vrm_client = VRMClient(
                session=session,
                access_token=vrm_token,
                installation_id=str(vrm_install_id),
            )
            LOGGER.info("VRM client initialized (installation %s)", vrm_install_id)

        # Open-Meteo client (free, uses HA's configured lat/lon)
        self._open_meteo_client = OpenMeteoClient(
            session=session,
            latitude=self.hass.config.latitude,
            longitude=self.hass.config.longitude,
        )

        # Fuel price client (PetrolSpy — free, no key)
        # Derive bounding box from HA location (~5km radius)
        ha_lat = self.hass.config.latitude
        ha_lng = self.hass.config.longitude
        self._fuel_price_client = FuelPriceClient(
            session=session,
            ne_lat=ha_lat + 0.05,
            ne_lng=ha_lng + 0.05,
            sw_lat=ha_lat - 0.05,
            sw_lng=ha_lng - 0.05,
        )

        # Fast startup: write safe register immediately to prevent
        # unintended grid charging during the 2-3 min until the first
        # full optimization cycle completes. The ESS holds the last
        # register value across HA restarts which could be high (90%+).
        shadow_mode = self.entry.options.get("shadow_mode", True)
        if not shadow_mode:
            try:
                await self._write_batterylife_register()
                soc_floor = int(self.entry.data.get("soc_floor_pct", 20))
                await self._write_register(soc_floor * 10)
                LOGGER.info(
                    "Fast startup: R2900=12 (BL disabled), R2901=%d (safe floor until first cycle)",
                    soc_floor * 10,
                )
            except Exception:
                LOGGER.warning("Fast startup register write failed")

        LOGGER.info("Victron MPC coordinator setup complete")

    async def _async_update_data(self) -> dict[str, Any]:
        """Run one MPC optimization cycle.

        Returns:
            Dict consumed by all sensor/number/switch entities.

        Raises:
            UpdateFailed: On recoverable errors (API timeout, bad data).
        """
        self._cycle_count += 1

        try:
            # ----------------------------------------------------------
            # Phase 1: Build tunables and system config from entry
            # ----------------------------------------------------------
            tunables = MPCTunables.from_config_entry(self.entry.options)
            self._tunables = tunables  # Make accessible to helper methods
            system = VictronSystem.from_config_entry(self.entry.data)

            # Update diesel price for genset cost calculation
            if self._fuel_price_client:
                try:
                    diesel_price = await self._fuel_price_client.get_diesel_price()
                    if diesel_price is not None:
                        system.genset_diesel_price_per_litre = diesel_price
                except Exception:
                    LOGGER.debug("Diesel price fetch failed, using default")

            # ----------------------------------------------------------
            # Phase 2: Build forecasts
            # ----------------------------------------------------------
            entities = self._get_entity_map()
            forecast_builder = ForecastBuilder(
                hass=self.hass,
                entities=entities,
                tunables=tunables,
                vrm=self._vrm_client,
                open_meteo=self._open_meteo_client,
            )
            self._forecast_builder = forecast_builder
            forecasts = await forecast_builder.build_all()

            now = datetime.now()
            cap = system.battery_capacity_kwh
            soc_pct: float = forecasts["battery_soc_pct"]

            # ----------------------------------------------------------
            # Phase 3: Build optimizer input
            # ----------------------------------------------------------
            overnight_steps = _compute_overnight_steps(
                now,
                tunables.overnight_start_hour,
                tunables.overnight_end_hour,
                tunables.horizon_steps,
                tunables.dt_hours,
            )

            # Cell balancing check
            force_full_charge = self._check_full_charge_needed(
                tunables.full_charge_interval_days
            )

            # SoC floor band: hard floor (20%) + soft floor (30%)
            hard_floor_kwh = (
                max(system.soc_min_pct, tunables.soc_floor_pct) / 100.0 * cap
            )
            soft_floor_kwh = tunables.soc_soft_floor_pct / 100.0 * cap
            daytime_min_kwh = hard_floor_kwh
            overnight_min_kwh = max(
                daytime_min_kwh, tunables.overnight_min_soc_pct / 100.0 * cap
            )
            overnight_set = set(overnight_steps)
            soc_min_schedule = [
                overnight_min_kwh if i in overnight_set else daytime_min_kwh
                for i in range(tunables.horizon_steps)
            ]

            # Scale overnight hold reward by price
            overnight_hold = scale_overnight_hold_reward(
                tunables.overnight_hold_reward,
                forecasts["buy_price"],
                overnight_steps,
                price_low=tunables.overnight_price_low,
                price_high=tunables.overnight_price_high,
            )

            opt_input = OptInput(
                horizon_steps=tunables.horizon_steps,
                dt_hours=tunables.dt_hours,
                battery_soc_kwh=soc_pct / 100.0 * cap,
                battery_capacity_kwh=cap,
                soc_min_kwh=hard_floor_kwh,
                soc_soft_floor_kwh=soft_floor_kwh,
                soft_floor_penalty=tunables.soft_floor_penalty,
                grid_charge_boost=tunables.grid_charge_boost,
                soc_target_reward=_build_soc_target_reward(
                    now, tunables.horizon_steps, tunables.dt_hours,
                    tunables, forecasts["buy_price"],
                    price_bands=forecasts.get("price_bands"),
                    solar_forecast_kw=forecasts.get("solar_forecast_kw"),
                ) if tunables.soc_profile_enabled else None,
                soc_min_schedule_kwh=soc_min_schedule,
                soc_max_kwh=system.soc_max_pct / 100.0 * cap,
                max_charge_kw=system.max_charge_kw,
                max_discharge_kw=system.max_discharge_kw,
                charge_efficiency=system.charge_efficiency,
                discharge_efficiency=system.discharge_efficiency,
                max_grid_import_kw=system.max_grid_import_kw,
                max_grid_export_kw=system.max_grid_export_kw,
                solar_forecast_kw=forecasts["solar_forecast_kw"],
                load_forecast_kw=forecasts["load_forecast_kw"],
                buy_price=forecasts["buy_price"],
                sell_price=forecasts["sell_price"],
                battery_wear_cost=tunables.battery_wear_cost,
                grid_import_penalty=tunables.grid_import_penalty,
                sunset_step=forecasts["sunset_step"],
                sunset_reward=tunables.sunset_reward,
                terminal_reward=_compute_dynamic_terminal_reward(
                    forecasts["buy_price"],
                    forecasts["solar_forecast_kw"],
                    tunables.dt_hours,
                    base_reward=tunables.terminal_reward,
                    wear_cost=tunables.battery_wear_cost,
                ),
                overnight_hold_reward=overnight_hold,
                overnight_steps=overnight_steps,
                force_full_charge=force_full_charge,
                sunset_soc_target_pct=95.0,  # Fixed 95% — dynamic target was too low on overcast days
            )

            # ----------------------------------------------------------
            # Phase 4: Run optimizer in executor thread
            # ----------------------------------------------------------
            result: OptOutput = await self.hass.async_add_executor_job(
                optimize, opt_input
            )

            LOGGER.info(
                "MPC cycle %d: %s → register=%d (%.0f%%), "
                "solve=%dms, cost=$%.2f",
                self._cycle_count,
                result.mode,
                result.target_register,
                result.target_soc_pct,
                result.solve_time_ms,
                result.total_cost,
            )

            # ----------------------------------------------------------
            # Phase 5: Check Amber health + apply overrides
            # ----------------------------------------------------------
            amber_available, buy_price_now = self._check_amber_health()
            if not amber_available and self._amber_unavailable_since:
                minutes_down = (
                    datetime.now() - self._amber_unavailable_since
                ).total_seconds() / 60
                if minutes_down > tunables.amber_blip_minutes:
                    LOGGER.warning(
                        "Amber unavailable for %.0f min — defensive mode active "
                        "(using $%.2f/kWh)",
                        minutes_down,
                        buy_price_now,
                    )
                    # Send notification if we haven't already for this outage
                    if not self._api_health["amber"]["alerted"]:
                        await self._notify(
                            "MPC: Amber Pricing Unavailable",
                            f"Amber API down for {minutes_down:.0f} min. "
                            f"MPC operating in defensive mode "
                            f"(assuming ${buy_price_now:.2f}/kWh).",
                            notification_id="mpc_amber_down",
                        )
                        self._api_health["amber"]["alerted"] = True

            if amber_available:
                buy_price_now = forecasts["buy_price"][0]

            sell_price_now = forecasts["sell_price"][0]
            is_spike = self._is_spike_active()
            override_reason: str | None = None

            if buy_price_now < 0:
                # Negative pricing — charge from grid (we're paid to consume)
                target_register = 1000
                mode = "grid_charge"
                override_reason = (
                    f"Negative pricing (${buy_price_now:.3f}/kWh) — charging"
                )
            elif is_spike or buy_price_now > tunables.spike_threshold:
                # Spike — discharge to minimise grid usage
                target_register = 100
                mode = "discharge"
                override_reason = (
                    f"Spike active (${buy_price_now:.3f}/kWh) — discharging"
                )
            else:
                # Normal — use optimizer decision
                target_register = result.target_register
                mode = result.mode
                override_reason = None

            if override_reason:
                LOGGER.info("Override: %s", override_reason)

            # ----------------------------------------------------------
            # Phase 6: Compute feed-in value (R2706)
            # ----------------------------------------------------------
            feedin_value = self._compute_feedin_value(
                buy_price=buy_price_now,
                sell_price=sell_price_now,
                mode=mode,
                soc_pct=soc_pct,
                is_spike=is_spike,
            )

            # ----------------------------------------------------------
            # Phase 7: Write Modbus registers (if not shadow mode)
            # ----------------------------------------------------------
            shadow_mode = self.entry.options.get("shadow_mode", True)
            if not shadow_mode:
                await self._write_batterylife_register()
                await self._write_register(target_register)
                await self._write_feedin_register(feedin_value)
            else:
                LOGGER.info(
                    "SHADOW: Would write R2901=%d, R2706=%d",
                    target_register,
                    feedin_value,
                )

            # Record full charge if SoC near 100%
            if soc_pct >= 95:
                self._last_full_charge_check = datetime.now()

            self._consecutive_failures = 0

            # ----------------------------------------------------------
            # Phase 7b: Reality check — verify grid matches intent
            # ----------------------------------------------------------
            grid_import_w = self._get_grid_import()
            genset_active = self._is_genset_active()

            if mode != "grid_charge" and grid_import_w > 200 and not genset_active and soc_pct > 35:
                LOGGER.warning(
                    "GRID IMPORT ANOMALY: mode=%s but grid importing %dW "
                    "(register=%d, SoC=%.0f%%). Auto-correcting to floor.",
                    mode, grid_import_w, target_register, soc_pct,
                )
                # Auto-correct: force register to hard floor to stop grid import
                floor_register = int(
                    max(tunables.soc_floor_pct, system.soc_min_pct) * 10
                )
                if not shadow_mode:
                    await self._write_batterylife_register()
                    await self._write_register(floor_register)
                    target_register = floor_register
                    LOGGER.info(
                        "Auto-corrected R2901 to %d (floor) to stop grid import",
                        floor_register,
                    )
                # Notify
                await self._notify(
                    "MPC: Grid Import Anomaly",
                    f"Grid importing {grid_import_w:.0f}W during {mode} mode. "
                    f"Auto-corrected register from {target_register} to {floor_register}. "
                    f"SoC={soc_pct:.0f}%, Solar={forecasts['current_solar_w']:.0f}W.",
                    notification_id="mpc_grid_anomaly",
                )

            if genset_active:
                LOGGER.info(
                    "Genset active — AC Input 2 running, grid unavailable"
                )

            # ----------------------------------------------------------
            # Phase 7c: Guard against YAML automation re-enablement
            # ----------------------------------------------------------
            await self._check_yaml_automations()

            # ----------------------------------------------------------
            # Phase 7d: Log Amber forecast for accuracy tracking
            # ----------------------------------------------------------
            self._log_amber_forecast(forecasts)

            # Compute forecast accuracy every 12 cycles (1 hour)
            if self._cycle_count % 12 == 0 and len(self._amber_forecast_log) >= 100:
                self._forecast_accuracy_cache = compute_forecast_accuracy(
                    self._amber_forecast_log
                )

            # Phase 0: Appliance data collection
            self._log_appliance_state()

            # ----------------------------------------------------------
            # Phase 7e+7f: Health monitoring (deterministic + GenAI)
            # ----------------------------------------------------------
            # Build extra dict OUTSIDE the cycle check so deterministic
            # checks can use it every cycle.
            extra = {
                "r2901_readback_pct": self._get_r2901_readback(),
                "r2900": self._get_r2900(),
                "r37_setpoint_w": self._get_r37_setpoint(),
                "grid_import_w": grid_import_w,
                "grid_export_w": self._get_grid_export(),
                "battery_power_w": self._get_battery_power(),
                "weather": forecasts.get("weather_condition", "unknown"),
                "solar_yield_kwh": self._get_solar_yield(),
            }

            # Cache sensor data for both deterministic checks and Phase 8 return
            sensor_data = self._build_sensor_data(
                result=result,
                forecasts=forecasts,
                tunables=tunables,
                target_register=target_register,
                feedin_value=feedin_value,
                mode=mode,
                override_reason=override_reason,
                is_spike=is_spike,
                shadow_mode=shadow_mode,
                buy_price_now=buy_price_now,
                sell_price_now=sell_price_now,
                forecast_builder=forecast_builder,
            )

            # Phase 7e: Deterministic health checks (every cycle)
            deterministic_results = run_deterministic_checks(
                coordinator_data=sensor_data,
                extra=extra,
            )

            if deterministic_results:
                # RED from deterministic checks
                first_red = deterministic_results[0]
                self._last_genai_result = {
                    "status": "RED",
                    "summary": first_red["reason"],
                    "details": "; ".join(r["reason"] for r in deterministic_results),
                    "source": "deterministic",
                }
                self._genai_consecutive_red += 1
                self._append_genai_history(
                    "deterministic", "RED", first_red["reason"],
                    soc_pct, mode, buy_price_now,
                    int(forecasts.get("current_solar_w", 0)),
                    int(forecasts.get("current_load_w", 0)),
                    extra.get("grid_import_w", 0),
                )
                LOGGER.warning("Deterministic RED: %s", first_red["reason"])
                if self._genai_consecutive_red >= 2:
                    await self._notify(
                        "MPC Alert: System Issue Detected",
                        f"RED for {self._genai_consecutive_red} consecutive checks.\n\n"
                        + "\n".join(f"- {r['reason']}" for r in deterministic_results),
                        notification_id="mpc_deterministic_alert",
                    )
            else:
                # All deterministic checks passed
                self._last_genai_result = {
                    "status": "GREEN",
                    "summary": "All operational checks passed",
                    "details": "",
                    "source": "deterministic",
                }
                if self._genai_consecutive_red > 0:
                    LOGGER.info(
                        "Deterministic cleared after %d consecutive RED",
                        self._genai_consecutive_red,
                    )
                self._genai_consecutive_red = 0

            # Phase 7f: GenAI strategic review (hourly, only when healthy)
            self._genai_cycle_count += 1
            is_first_cycle = (self._genai_cycle_count == 1)
            is_hourly_cycle = (self._genai_cycle_count >= GENAI_CYCLE_INTERVAL)
            if (is_first_cycle or is_hourly_cycle) and not deterministic_results:
                if is_hourly_cycle:
                    self._genai_cycle_count = 0
                api_key = self.entry.data.get(
                    "openrouter_api_key", ""
                ) or self.entry.options.get("openrouter_api_key", "")
                if api_key:
                    snapshot = build_strategic_snapshot(
                        coordinator_data=sensor_data,
                        extra=extra,
                    )
                    session = async_get_clientsession(self.hass)
                    genai_result = await run_genai_health_check(
                        session, api_key, snapshot,
                    )
                    # Merge GenAI result with deterministic GREEN
                    if genai_result.get("status") in ("YELLOW", "GREEN"):
                        self._last_genai_result = {
                            "status": genai_result["status"],
                            "summary": genai_result.get("summary", ""),
                            "details": genai_result.get("details", ""),
                            "source": "genai",
                        }
                    self._append_genai_history(
                        "genai", genai_result.get("status", "?"),
                        genai_result.get("summary", ""),
                        soc_pct, mode, buy_price_now,
                        int(forecasts.get("current_solar_w", 0)),
                        int(forecasts.get("current_load_w", 0)),
                        extra.get("grid_import_w", 0),
                    )
                    LOGGER.info(
                        "GenAI strategic: %s -- %s",
                        genai_result.get("status"),
                        genai_result.get("summary"),
                    )

            # ----------------------------------------------------------
            # Phase 8: Return cached sensor data
            # ----------------------------------------------------------
            return sensor_data

        except Exception as err:
            self._consecutive_failures += 1
            raise UpdateFailed(
                f"MPC cycle {self._cycle_count} failed "
                f"({self._consecutive_failures} consecutive): {err}"
            ) from err

    # ------------------------------------------------------------------
    # Entity map from config entry
    # ------------------------------------------------------------------

    def _get_entity_map(self) -> dict[str, str]:
        """Build entity ID map from config entry data."""
        data = self.entry.data
        options = self.entry.options
        merged = {**data, **options}
        return {
            "battery_soc": merged.get(
                "battery_soc", "sensor.victron_battery_state_of_charge"
            ),
            "solar_power": merged.get("solar_power", "sensor.solar_power"),
            "ac_consumption": merged.get(
                "ac_consumption", "sensor.victron_ac_consumption"
            ),
            "grid_power": merged.get("grid_power", "sensor.victron_grid_power"),
            "amber_price": merged.get("amber_price", "sensor.amber_general_price"),
            "amber_forecast": merged.get(
                "amber_forecast", "sensor.amber_general_forecast"
            ),
            "amber_feedin": merged.get("amber_feedin", "sensor.amber_feed_in_price"),
            "amber_feedin_forecast": merged.get(
                "amber_feedin_forecast", "sensor.amber_feed_in_forecast"
            ),
            "amber_spike": merged.get("amber_spike", "sensor.amber_general_price"),
            "weather_entity": merged.get("weather_entity", "weather.home"),
            "genset_power": merged.get(
                "genset_power", "sensor.victron_genset_power"
            ),
            "solcast_forecast": merged.get(
                "solcast_forecast",
                "sensor.solcast_pv_forecast_forecast_today",
            ),
        }

    # ------------------------------------------------------------------
    # Amber health / defensive discharge
    # ------------------------------------------------------------------

    def _check_amber_health(self) -> tuple[bool, float]:
        """Check Amber API health and return (is_available, price_to_use).

        When Amber unavailable >5min, ALWAYS assume spike risk and return
        a high price ($2.00) to force discharge. The cost of discharging
        unnecessarily ($0.05/kWh wear) is trivial compared to staying on
        grid during a $20/kWh spike.

        Spikes can happen at ANY time — morning demand events, grid
        failures, transmission constraints — not just evening peak.

        Returns:
            Tuple of (is_available, effective_buy_price).
        """
        entities = self._get_entity_map()
        amber_entity = entities.get("amber_price", "sensor.amber_general_price")
        state = self.hass.states.get(amber_entity)

        if state is None or state.state in ("unavailable", "unknown"):
            # Amber is down
            now = datetime.now()
            if self._amber_unavailable_since is None:
                self._amber_unavailable_since = now

            minutes_down = (
                now - self._amber_unavailable_since
            ).total_seconds() / 60

            blip_min = getattr(self, "_tunables", None)
            blip_threshold = blip_min.amber_blip_minutes if blip_min else 5.0
            defensive_price = blip_min.defensive_price if blip_min else 2.00

            if minutes_down < blip_threshold:
                # Tier 1: Brief blip — use last known price
                return (False, self._last_known_buy_price)

            if minutes_down < _AMBER_CAUTIOUS_MINUTES:
                # Tier 2: Moderate outage — cautious but not panic
                cautious_price = max(self._last_known_buy_price, _AMBER_CAUTIOUS_PRICE)
                return (False, cautious_price)

            # Tier 3: Extended outage — full defensive
            return (False, defensive_price)

        # Amber is available
        self._amber_unavailable_since = None
        self._api_health["amber"]["alerted"] = False
        try:
            price = float(state.state)
            self._last_known_buy_price = price
            return (True, price)
        except (ValueError, TypeError):
            return (True, self._last_known_buy_price)

    # ------------------------------------------------------------------
    # Override helpers
    # ------------------------------------------------------------------

    def _get_grid_import(self) -> float:
        """Read real-time grid import power (W) from Victron."""
        state = self.hass.states.get("sensor.victron_grid_import")
        if state and state.state not in ("unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        # Fallback to grid_power (positive = import)
        state = self.hass.states.get(
            self._get_entity_map().get("grid_power", "sensor.victron_grid_power")
        )
        if state and state.state not in ("unknown", "unavailable"):
            try:
                return max(0, float(state.state))
            except (ValueError, TypeError):
                pass
        return 0.0

    def _append_genai_history(
        self,
        source: str,
        status: str,
        summary: str,
        soc_pct: float,
        mode: str,
        buy_price: float,
        solar_w: int,
        load_w: int,
        grid_import_w: int,
    ) -> None:
        """Append health check result to rolling history buffer."""
        # Deduplicate: skip if same as last entry
        if self._genai_history:
            last = self._genai_history[-1]
            if last.get("source") == source and last.get("status") == status and last.get("summary") == summary:
                return

        self._genai_history.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "status": status,
            "summary": summary,
            "readings": {
                "soc_pct": round(soc_pct, 1),
                "mode": mode,
                "buy_price": round(buy_price, 4),
                "solar_w": solar_w,
                "load_w": load_w,
                "grid_import_w": grid_import_w,
            },
        })
        if len(self._genai_history) > self._genai_history_max:
            self._genai_history = self._genai_history[-self._genai_history_max:]

    def _get_r2901_readback(self) -> float:
        """Read R2901 from the Modbus sensor."""
        try:
            state = self.hass.states.get(
                "sensor.victron_ess_minimum_soc_unless_grid_fails"
            )
            if state and state.state not in ("unknown", "unavailable"):
                return float(state.state)
            return -1
        except (ValueError, AttributeError):
            return -1

    def _get_r2900(self) -> int:
        """Read R2900 (ESS mode) from the Modbus sensor."""
        try:
            state = self.hass.states.get("sensor.victron_ess_mode")
            if state and state.state not in ("unknown", "unavailable"):
                return int(float(state.state))
            return -1
        except (ValueError, AttributeError):
            return -1

    def _get_r37_setpoint(self) -> int:
        """Read R37 ESS power setpoint."""
        try:
            state = self.hass.states.get(
                "sensor.victron_ess_power_setpoint_phase_1"
            )
            if state and state.state not in ("unknown", "unavailable"):
                return int(float(state.state))
            return 0
        except (ValueError, AttributeError):
            return 0

    def _get_grid_export(self) -> int:
        """Read grid export power."""
        try:
            state = self.hass.states.get("sensor.victron_grid_export")
            if state and state.state not in ("unknown", "unavailable"):
                return int(float(state.state))
            return 0
        except (ValueError, AttributeError):
            return 0

    def _get_battery_power(self) -> int:
        """Read battery power from Modbus sensor."""
        try:
            state = self.hass.states.get("sensor.victron_battery_power")
            if state and state.state not in ("unknown", "unavailable"):
                return int(float(state.state))
            return 0
        except (ValueError, AttributeError):
            return 0

    def _get_solar_yield(self) -> float:
        """Read today's solar yield."""
        try:
            state = self.hass.states.get("sensor.solar_yield_today")
            if state and state.state not in ("unknown", "unavailable"):
                return float(state.state)
            return 0.0
        except (ValueError, AttributeError):
            return 0.0

    def _is_genset_active(self) -> bool:
        """Check if genset is running (AC Input 2).

        When genset is active, grid is unavailable — Amber prices are
        irrelevant and cost should use genset $/kWh instead.
        """
        entities = self._get_entity_map()
        # Check dedicated genset active sensor
        state = self.hass.states.get(
            entities.get("genset_active", "sensor.victron_genset_active")
        )
        if state and state.state == "1":
            return True
        # Also check active input source (1=Grid, 2=Genset)
        state = self.hass.states.get("sensor.victron_active_input_source")
        if state and state.state == "2":
            return True
        return False

    def _is_spike_active(self) -> bool:
        """Check if Amber spike is currently active."""
        entities = self._get_entity_map()
        spike_entity = entities.get("amber_spike", "sensor.amber_general_price")
        state = self.hass.states.get(spike_entity)
        if state is None:
            return False
        # Check spike attribute (Amber integration uses spike_status attr)
        spike_status = state.attributes.get("spike_status", "none")
        if spike_status and spike_status != "none":
            return True
        # Also check dedicated spike sensor if it exists
        spike_sensor = self.hass.states.get("binary_sensor.amber_price_spike")
        if spike_sensor and spike_sensor.state == "on":
            return True
        return False

    def _compute_feedin_value(
        self,
        *,
        buy_price: float,
        sell_price: float,
        mode: str,
        soc_pct: float,
        is_spike: bool,
    ) -> int:
        """Compute R2706 feed-in register value using 5-rule priority.

        Rules evaluated in order (first match wins):
        1. Negative buy price → 70 (export everything, we're paid to consume)
        2. MPC grid_charge mode → 70 (inverter needs grid pull)
        3. Spike + FIT > $0.10 + SoC > 30% → 70 (export for profit)
        4. SoC > 95% + FIT > 0 → 70 (battery full, export excess)
        5. Otherwise → 0 (block export, self-consume)
        """
        if buy_price < 0:
            LOGGER.debug("R2706: Rule 1 — negative buy price, export everything")
            return REGISTER_MAX_FEED_IN  # 70

        if mode == "grid_charge":
            LOGGER.debug("R2706: Rule 2 — grid_charge mode, allow import")
            return REGISTER_MAX_FEED_IN  # 70

        tunables = getattr(self, "_tunables", None)
        fit_threshold = tunables.feedin_export_threshold if tunables else 0.10
        soc_threshold = tunables.feedin_soc_threshold if tunables else 30.0

        if is_spike and sell_price > fit_threshold and soc_pct > soc_threshold:
            LOGGER.debug(
                "R2706: Rule 3 — spike + FIT $%.2f + SoC %.0f%%, export for profit",
                sell_price,
                soc_pct,
            )
            return REGISTER_MAX_FEED_IN  # 70

        if soc_pct > 95 and sell_price > 0:
            LOGGER.debug("R2706: Rule 4 — battery full + positive FIT, export excess")
            return REGISTER_MAX_FEED_IN  # 70

        LOGGER.debug("R2706: Rule 5 — block export, self-consume")
        return REGISTER_FEEDIN_BLOCK  # 0

    # ------------------------------------------------------------------
    # Modbus health
    # ------------------------------------------------------------------

    @property
    def modbus_healthy(self) -> bool:
        """Return True if Modbus communication is healthy."""
        threshold = 3
        tunables = getattr(self, "_tunables", None)
        if tunables and hasattr(tunables, "modbus_failure_threshold"):
            threshold = int(tunables.modbus_failure_threshold) if hasattr(tunables, "modbus_failure_threshold") else 3
        return self._modbus_consecutive_failures < threshold

    async def _modbus_write_success(self) -> None:
        """Handle a successful Modbus write — reset failure tracking."""
        if self._modbus_consecutive_failures > 2 and self._modbus_alerted:
            await self._notify(
                "MPC: Modbus Communication Restored",
                "Victron Cerbo GX communication restored. "
                "Registers updating normally.",
                notification_id="mpc_modbus_down",
            )
        self._modbus_consecutive_failures = 0
        self._modbus_alerted = False
        self._modbus_last_success = datetime.now()

    async def _modbus_write_failure(self) -> None:
        """Handle a failed Modbus write — increment counter, alert if needed."""
        self._modbus_consecutive_failures += 1
        if self._modbus_consecutive_failures >= 3 and not self._modbus_alerted:
            await self._notify(
                "MPC: Modbus Communication Failed",
                f"Cannot write to Victron Cerbo GX. "
                f"{self._modbus_consecutive_failures} consecutive failures. "
                f"Registers are NOT being updated. Check Cerbo network.",
                notification_id="mpc_modbus_down",
            )
            self._modbus_alerted = True

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def _notify(
        self, title: str, message: str, notification_id: str | None = None,
    ) -> None:
        """Send push notification to configured devices + persistent notification.

        Sends to both notify.mobile_app_ak_iphone and persistent_notification
        so the user sees it on phone AND in HA dashboard.
        """
        # Persistent notification (always visible in HA)
        try:
            data: dict[str, str] = {"title": title, "message": message}
            if notification_id:
                data["notification_id"] = notification_id
            await self.hass.services.async_call(
                "persistent_notification", "create", data,
            )
        except Exception:
            pass

        # Mobile push notification
        for target in ("notify.mobile_app_ak_iphone",):
            try:
                await self.hass.services.async_call(
                    target.split(".")[0],
                    target.split(".")[1],
                    {"title": title, "message": message},
                )
            except Exception:
                pass  # Service may not exist

    # ------------------------------------------------------------------
    # YAML automation guard
    # ------------------------------------------------------------------

    def _log_amber_forecast(self, forecasts: dict[str, Any]) -> None:
        """Log current Amber forecast vs actual for accuracy tracking.

        Stores what Amber predicts for +1h, +2h, +3h, +6h alongside
        the current actual price. After 7 days, this data reveals
        systematic forecast biases by time of day.
        """
        try:
            entities = self._get_entity_map()
            amber_state = self.hass.states.get(
                entities.get("amber_price", "sensor.amber_general_price")
            )
            if not amber_state or amber_state.state in ("unavailable", "unknown"):
                return

            actual_price = float(amber_state.state)
            spot_price = float(amber_state.attributes.get("spot_per_kwh", 0))
            spike_actual = amber_state.attributes.get("spike_status", "none")

            # Get forecast prices from the forecast entity
            forecast_state = self.hass.states.get(
                entities.get("amber_forecast", "sensor.amber_general_forecast")
            )
            forecast_prices = {}
            if forecast_state:
                fc_list = forecast_state.attributes.get("forecasts", [])
                # Extract prices at +1h, +2h, +3h, +6h offsets
                for offset_idx, label in [(2, "+1h"), (4, "+2h"), (6, "+3h"), (12, "+6h")]:
                    if offset_idx < len(fc_list):
                        forecast_prices[label] = fc_list[offset_idx].get("per_kwh", 0)
                        forecast_prices[f"{label}_spike"] = fc_list[offset_idx].get("spike_status", "none")

            now = datetime.now()
            entry = {
                "timestamp": now.isoformat(),
                "hour": now.hour,
                "actual_buy": actual_price,
                "actual_spot": spot_price,
                "margin": round(actual_price - spot_price, 4),
                "spike_actual": spike_actual,
                **forecast_prices,
            }

            self._amber_forecast_log.append(entry)

            # Trim to max size
            if len(self._amber_forecast_log) > self._amber_forecast_log_max:
                self._amber_forecast_log = self._amber_forecast_log[-self._amber_forecast_log_max:]

        except Exception:
            pass  # Never crash the coordinator for logging

    def _log_appliance_state(self) -> None:
        """Log appliance power readings for Phase 0 data collection."""
        try:
            from .const import (
                APPLIANCE_IDLE_W,
                APPLIANCE_STANDBY_W,
                DEFAULT_APPLIANCE_SENSORS,
            )

            now = datetime.now()
            readings: dict[str, dict[str, Any]] = {}
            running_count = 0

            for entity_id in DEFAULT_APPLIANCE_SENSORS:
                state = self.hass.states.get(entity_id)
                if state is None or state.state in ("unavailable", "unknown"):
                    readings[entity_id] = {"power_w": 0, "state": "unavailable"}
                    continue
                try:
                    power = float(state.state)
                except (ValueError, TypeError):
                    readings[entity_id] = {"power_w": 0, "state": "error"}
                    continue

                if power < APPLIANCE_IDLE_W:
                    appliance_state = "idle"
                elif power < APPLIANCE_STANDBY_W:
                    appliance_state = "standby"
                else:
                    appliance_state = "running"
                    running_count += 1

                readings[entity_id] = {
                    "power_w": round(power, 1),
                    "state": appliance_state,
                }

            entry = {
                "timestamp": now.isoformat(),
                "hour": now.hour,
                "readings": readings,
                "running_count": running_count,
                "total_w": sum(r["power_w"] for r in readings.values()),
            }

            self._appliance_log.append(entry)
            if len(self._appliance_log) > self._appliance_log_max:
                self._appliance_log = self._appliance_log[-self._appliance_log_max :]

        except Exception:
            pass  # Never crash coordinator for logging

    async def _check_yaml_automations(self) -> None:
        """Detect and disable any re-enabled MPC YAML automations.

        The old YAML automations (mpc_write_battery_register etc.) must
        stay OFF — if someone re-enables one in the HA UI, it will override
        HACS register writes and cause unintended grid charging.
        """
        mpc_automations = [
            "automation.mpc_write_battery_register",
            "automation.mpc_data_stale",
            "automation.mpc_solver_failure",
            "automation.mpc_shadow_delta_large",
            "automation.mpc_kill_switch",
            "automation.mpc_register_writer",
            "automation.mpc_grid_feed_in_control",
            "automation.mpc_stale_data_safety",
            "automation.mpc_amber_api_down",
            "automation.mpc_amber_api_recovered",
            "automation.mpc_vrm_api_down",
            "automation.mpc_vrm_api_recovered",
            "automation.mpc_solar_forecast_fallback_active",
        ]

        for auto_id in mpc_automations:
            state = self.hass.states.get(auto_id)
            if state and state.state == "on":
                LOGGER.warning(
                    "YAML automation %s is ON — disabling to prevent "
                    "register conflicts with HACS integration",
                    auto_id,
                )
                try:
                    await self.hass.services.async_call(
                        "automation", "turn_off",
                        {"entity_id": auto_id},
                    )
                except Exception:
                    pass
                await self._notify(
                    "MPC: YAML Automation Conflict",
                    f"{auto_id} was re-enabled and has been automatically "
                    f"disabled. These automations conflict with the HACS "
                    f"integration's register writes.",
                    notification_id="mpc_yaml_conflict",
                )

    # ------------------------------------------------------------------
    # Cell balancing
    # ------------------------------------------------------------------


    def _extract_amber_bands(self, buy_prices):
        """Extract Amber price band descriptors from forecast entity."""
        try:
            state = self._hass.states.get("sensor.amber_general_forecast")
            if state is None:
                return None
            forecasts = state.attributes.get("forecasts", [])
            if not forecasts:
                return None
            # Each forecast is 30min, expand to 5min steps
            bands_30min = ["low"]  # Current period
            for f in forecasts:
                bands_30min.append(f.get("descriptor", "low"))
            bands_5min = []
            for b in bands_30min:
                bands_5min.extend([b] * 6)
            # Pad to horizon length
            n = len(buy_prices)
            bands_5min = bands_5min[:n]
            while len(bands_5min) < n:
                bands_5min.append("low")
            return bands_5min
        except Exception:
            return None

    def _check_full_charge_needed(self, interval_days: int) -> bool:
        """Check if periodic full charge is due for cell balancing.

        Uses HA sensor or internal tracking to determine last full charge.
        """
        if interval_days <= 0:
            return False

        # Check HA sensor first
        state = self.hass.states.get("sensor.mpc_last_full_charge")
        if state and state.state not in ("unknown", "unavailable", ""):
            try:
                last_charge = datetime.fromisoformat(state.state)
                days_since = (datetime.now() - last_charge).total_seconds() / 86400
                return days_since >= interval_days
            except (ValueError, TypeError):
                pass

        # Fall back to internal tracking
        if self._last_full_charge_check is not None:
            days_since = (
                datetime.now() - self._last_full_charge_check
            ).total_seconds() / 86400
            return days_since >= interval_days

        # No record — assume full charge needed
        return True

    # ------------------------------------------------------------------
    # Sensor data builder
    # ------------------------------------------------------------------

    def _build_sensor_data(
        self,
        *,
        result: OptOutput,
        forecasts: dict[str, Any],
        tunables: MPCTunables,
        target_register: int,
        feedin_value: int,
        mode: str,
        override_reason: str | None,
        is_spike: bool,
        shadow_mode: bool,
        buy_price_now: float,
        sell_price_now: float,
        forecast_builder: ForecastBuilder,
    ) -> dict[str, Any]:
        """Build the data dict consumed by sensor entities.

        Keys match the attr_key values in sensor.py SENSOR_DESCRIPTIONS.
        Dict-valued entries use {"state": ..., ...attrs} pattern so sensor.py
        can split state from extra_state_attributes.
        """
        soc_pct: float = forecasts["battery_soc_pct"]
        breakdown = result.cost_breakdown or {}

        # SoC lookahead from optimizer trajectory
        traj = result.soc_trajectory_pct
        sph = tunables.steps_per_hour
        soc_lookahead = {
            f"soc_{h}h_pct": round(traj[min(h * sph, len(traj) - 1)], 1)
            for h in range(1, 5)
        }

        # 30-min schedule for dashboard
        schedule_30min = []
        now = datetime.now()
        for i in range(0, min(len(traj) - 1, tunables.horizon_steps), 6):
            t = now.replace(second=0, microsecond=0) + timedelta(minutes=5 * i)
            buy_idx = min(i, len(forecasts["buy_price"]) - 1)
            schedule_30min.append(
                {
                    "time": t.strftime("%H:%M"),
                    "soc_pct": traj[i],
                    "buy_price": round(forecasts["buy_price"][buy_idx], 3),
                }
            )

        # Weather/cloud conditions
        weather_state = self.hass.states.get(
            self._get_entity_map().get("weather_entity", "weather.home")
        )
        weather_condition = "unknown"
        cloud_coverage_total = 0
        temperature = 0
        humidity = 0
        if weather_state:
            weather_condition = weather_state.state
            cloud_coverage_total = weather_state.attributes.get("cloud_coverage", 0)
            temperature = weather_state.attributes.get("temperature", 0)
            humidity = weather_state.attributes.get("humidity", 0)

        # Cloud layer breakdown from forecast builder
        cloud_layers_cache = getattr(forecast_builder, "_cloud_layers_cache", None)
        cloud_attrs: dict[str, Any] = {
            "weather_condition": weather_condition,
            "temperature": temperature,
            "humidity": humidity,
        }
        if cloud_layers_cache:
            current_layers = cloud_layers_cache.get(0, {})
            cloud_attrs["cloud_low_pct"] = current_layers.get("low", 0)
            cloud_attrs["cloud_mid_pct"] = current_layers.get("mid", 0)
            cloud_attrs["cloud_high_pct"] = current_layers.get("high", 0)
            effective = round(forecast_builder._effective_cloud_pct(current_layers), 1)
            cloud_attrs["effective_cloud_pct"] = effective
            cloud_attrs["cloud_source"] = "open-meteo_layers"
        else:
            effective = cloud_coverage_total
            cloud_attrs["cloud_source"] = "met.no_total"

        # Solar forecast hourly lookahead
        solar_kw = forecasts.get("solar_forecast_kw", [])
        solar_steps_per_hour = 12  # 60 / 5
        solar_hourly = {}
        for h in range(1, 5):
            idx = min(h * solar_steps_per_hour, len(solar_kw) - 1) if solar_kw else 0
            solar_hourly[f"forecast_{h}h_w"] = (
                round(float(solar_kw[idx]) * 1000) if solar_kw else 0
            )

        # Solar forecast remaining TODAY (not full 24h horizon).
        # The forecast array is a rolling 24h window from now, so after
        # sunset it includes tomorrow's solar. Clamp to steps until midnight
        # so the "today" attribute reads ~0 after dark.
        now = datetime.now()
        minutes_until_midnight = (24 - now.hour) * 60 - now.minute
        steps_until_midnight = min(
            minutes_until_midnight // tunables.dt_minutes, len(solar_kw)
        )
        solar_forecast_kwh = round(
            sum(solar_kw[:steps_until_midnight]) * tunables.dt_hours, 2
        ) if solar_kw else 0.0

        reason = override_reason if override_reason else result.reason

        return {
            # battery_plan — core MPC recommendation
            "battery_plan": {
                "state": round(result.target_soc_pct, 1),
                "mode": mode,
                "reason": reason,
                "target_register": target_register,
                "feedin_register": feedin_value,
                "shadow_mode": shadow_mode,
                "last_push": round(time.time()),
                **soc_lookahead,
            },
            # decision — full context for dashboards/automations
            "decision": {
                "state": mode,
                "reason": reason,
                "target_soc_pct": round(result.target_soc_pct, 1),
                "target_register": target_register,
                "buy_price_actual": round(buy_price_now, 4),
                "sell_price_actual": round(sell_price_now, 4),
                "buy_price_forecast": round(forecasts["buy_price"][0], 4),
                "sell_price_forecast": round(forecasts["sell_price"][0], 4),
                "spike": is_spike,
                "shadow_mode": shadow_mode,
                "override_applied": override_reason is not None,
                "override_reason": override_reason or "",
                "cloud_coverage": cloud_coverage_total,
                "weather": weather_condition,
                "solar_forecast_source": forecasts.get(
                    "solar_forecast_source", "unknown"
                ),
                "solar_day_type": forecasts.get("solar_day_type", "unknown"),
                "battery_soc_pct": soc_pct,
                "current_solar_w": forecasts["current_solar_w"],
                "current_load_w": forecasts["current_load_w"],
                "grid_import_w": self._get_grid_import(),
                "genset_active": self._is_genset_active(),
                "input_source": "genset" if self._is_genset_active() else "grid",
                "schedule_30min": json.dumps(schedule_30min[:48]),
                **soc_lookahead,
            },
            # Scalar sensors
            "effective_price": round(result.effective_price, 4),
            "cost_24h": {
                "state": round(result.total_cost, 4),
                "grid_cost": round(breakdown.get("grid_cost", 0), 4),
                "export_revenue": round(breakdown.get("export_revenue", 0), 4),
                "wear_cost": round(breakdown.get("wear_cost", 0), 4),
            },
            "solar_input_w": round(forecasts["current_solar_w"]),
            "load_input_w": round(forecasts["current_load_w"]),
            "buy_price": {
                "state": round(buy_price_now, 4),
                "spike": is_spike,
                "mpc_forecast_price": round(forecasts["buy_price"][0], 4),
            },
            "sell_price": {
                "state": round(sell_price_now, 4),
                "mpc_forecast_price": round(forecasts["sell_price"][0], 4),
            },
            "cloud_coverage": {
                "state": effective,
                **cloud_attrs,
            },
            "solar_forecast_today": {
                "state": solar_forecast_kwh,
                "solar_derate": round(
                    forecasts.get("solar_derate", 1.0)
                    if "solar_derate" in forecasts
                    else 1.0,
                    3,
                ),
                "solar_forecast_source": forecasts.get(
                    "solar_forecast_source", "unknown"
                ),
                "solar_day_type": forecasts.get("solar_day_type", "unknown"),
                "solar_shading_ratios": [
                    round(r, 2) for r in forecasts.get("solar_shading_ratios", [])
                ],
                "load_forecast_source": forecasts.get(
                    "load_forecast_source", "unknown"
                ),
                "seasonal_load_factor": forecasts.get("seasonal_load_factor", 1.0),
                **solar_hourly,
            },
            "solve_time_ms": round(result.solve_time_ms, 1),
            "spike_active": is_spike,
            "modbus_healthy": self.modbus_healthy,
            "modbus_failures": self._modbus_consecutive_failures,
            "amber_forecast_log_entries": len(self._amber_forecast_log),
            "genai_health": {
                **self._last_genai_result,
                "history": self._genai_history,
            },
            "amber_forecast_accuracy": self._forecast_accuracy_cache,
            "appliance_monitor": {
                "state": (
                    self._appliance_log[-1]["running_count"]
                    if self._appliance_log
                    else 0
                ),
                "readings": (
                    self._appliance_log[-1]["readings"]
                    if self._appliance_log
                    else {}
                ),
                "log_entries": len(self._appliance_log),
                "recent_history": (
                    self._appliance_log[-12:]
                    if self._appliance_log
                    else []
                ),
            },
        }

    # ------------------------------------------------------------------
    # Modbus register writes
    # ------------------------------------------------------------------

    async def _write_register(self, value: int) -> None:
        """Write ESS min SoC register (R2901) via Modbus.

        Value = SoC% x 10, range 100-1000.
        Acts as floor AND target: ESS discharges down to this value.
        """
        # Don't re-write if unchanged
        if value == self._last_register_value:
            return

        value = max(100, min(1000, value))  # Clamp to valid range

        try:
            await self.hass.services.async_call(
                "modbus",
                "write_register",
                {
                    "hub": self.entry.data.get("modbus_hub", "cerbo"),
                    "unit": self.entry.data.get("modbus_slave_system", 100),
                    "address": REGISTER_ESS_MIN_SOC,
                    "value": value,
                },
            )
            LOGGER.info("R2901 written: %d (was %s)", value, self._last_register_value)
            self._last_register_value = value
            await self._modbus_write_success()
        except Exception:
            LOGGER.exception("Failed to write R2901=%d", value)
            await self._modbus_write_failure()

    async def _write_feedin_register(self, value: int) -> None:
        """Write max grid feed-in register (R2706) via Modbus.

        Units = 100W per value (70 = 7000W, 0 = block all export).
        """
        if value == self._last_feedin_value:
            return

        value = max(REGISTER_FEEDIN_BLOCK, min(REGISTER_MAX_FEED_IN, value))

        try:
            await self.hass.services.async_call(
                "modbus",
                "write_register",
                {
                    "hub": self.entry.data.get("modbus_hub", "cerbo"),
                    "unit": self.entry.data.get("modbus_slave_system", 100),
                    "address": REGISTER_MAX_FEED_IN,
                    "value": value,
                },
            )
            LOGGER.info("R2706 written: %d (was %s)", value, self._last_feedin_value)
            self._last_feedin_value = value
            await self._modbus_write_success()
        except Exception:
            LOGGER.exception("Failed to write R2706=%d", value)
            await self._modbus_write_failure()

    async def _write_batterylife_register(self) -> None:
        """Write ESS BatteryLife state register (R2900) to disable BatteryLife.

        Value 12 = BatteryLife disabled + discharging. This prevents the
        Cerbo GX BatteryLife algorithm from overwriting R2901 with its own
        calculated min SoC (which can be ABOVE current SoC, causing unwanted
        grid charging).

        Must be written every cycle -- Cerbo may reset R2900 after reboot.

        Bug discovered 2026-03-30: BatteryLife was silently overwriting R2901
        every ~15 seconds, setting it above SoC, causing 500W+ grid import
        while MPC reported 'discharge' mode.
        """
        try:
            await self.hass.services.async_call(
                "modbus",
                "write_register",
                {
                    "hub": self.entry.data.get("modbus_hub", "cerbo"),
                    "unit": self.entry.data.get("modbus_slave_system", 100),
                    "address": REGISTER_BATTERYLIFE_STATE,
                    "value": 12,
                },
            )
        except Exception:
            LOGGER.exception("Failed to write R2900=12 (BatteryLife disable)")


# ======================================================================
# Module-level helpers
# ======================================================================


def _compute_overnight_steps(
    now: datetime,
    start_hour: int,
    end_hour: int,
    horizon_steps: int,
    dt_hours: float,
) -> list[int]:
    """Compute optimizer step indices that fall within overnight hours.

    For example, if now is 20:00 and overnight is 22:00-06:00,
    steps 24-120 (at 5-min intervals) would be overnight.
    """
    steps: list[int] = []
    now_frac = now.hour + now.minute / 60.0
    for i in range(horizon_steps):
        hour_of_day = (now_frac + i * dt_hours) % 24
        if start_hour > end_hour:
            # Wraps midnight (e.g., 22:00-06:00)
            if hour_of_day >= start_hour or hour_of_day < end_hour:
                steps.append(i)
        elif start_hour <= hour_of_day < end_hour:
            steps.append(i)
    return steps
