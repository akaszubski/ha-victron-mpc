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
    REGISTER_ESS_MIN_SOC,
    REGISTER_FEEDIN_BLOCK,
    REGISTER_MAX_FEED_IN,
    UPDATE_INTERVAL_MINUTES,
)
from .forecasts import ForecastBuilder
from .optimizer import OptInput, OptOutput, optimize
from .utils import scale_overnight_hold_reward


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
        self._last_known_buy_price: float = 0.30

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
        self._fuel_price_client = FuelPriceClient(session=session)

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

            # Time-varying SoC floor
            daytime_min_kwh = (
                max(system.soc_min_pct, tunables.soc_floor_pct) / 100.0 * cap
            )
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
            )

            opt_input = OptInput(
                horizon_steps=tunables.horizon_steps,
                dt_hours=tunables.dt_hours,
                battery_soc_kwh=soc_pct / 100.0 * cap,
                battery_capacity_kwh=cap,
                soc_min_kwh=daytime_min_kwh,
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
                terminal_reward=tunables.terminal_reward,
                overnight_hold_reward=overnight_hold,
                overnight_steps=overnight_steps,
                force_full_charge=force_full_charge,
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
                if minutes_down > 5:
                    LOGGER.warning(
                        "Amber unavailable for %.0f min — defensive mode active "
                        "(using $%.2f/kWh)",
                        minutes_down,
                        buy_price_now,
                    )
                    # Send notification if we haven't already for this outage
                    if not self._api_health["amber"]["alerted"]:
                        try:
                            await self.hass.services.async_call(
                                "persistent_notification",
                                "create",
                                {
                                    "title": "MPC: Amber Pricing Unavailable",
                                    "message": (
                                        f"Amber API down for {minutes_down:.0f} min. "
                                        "MPC operating in defensive mode."
                                    ),
                                    "notification_id": "mpc_amber_down",
                                },
                            )
                            self._api_health["amber"]["alerted"] = True
                        except Exception:
                            pass

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
            elif is_spike or buy_price_now > 1.0:
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
            # Phase 8: Build data dict for sensor entities
            # ----------------------------------------------------------
            return self._build_sensor_data(
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

        When Amber unavailable >5min:
        - Evening peak (17:00-21:00): assume spike risk → return $2.00
        - Other times: hold conservatively → return $0.30

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

            if minutes_down < 5:
                # Brief blip — use last known price
                return (False, self._last_known_buy_price)

            # Extended outage — defensive pricing based on time of day
            hour = now.hour
            if 17 <= hour < 21:
                # Evening peak: assume spike risk
                return (False, 2.00)
            else:
                # Off-peak: conservative hold
                return (False, 0.30)

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

        if is_spike and sell_price > 0.10 and soc_pct > 30:
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
        return self._modbus_consecutive_failures < 3

    async def _modbus_write_success(self) -> None:
        """Handle a successful Modbus write — reset failure tracking."""
        if self._modbus_consecutive_failures > 2 and self._modbus_alerted:
            try:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "MPC: Modbus Communication Restored",
                        "message": (
                            "Victron Cerbo GX communication restored. "
                            "Registers updating normally."
                        ),
                        "notification_id": "mpc_modbus_down",
                    },
                )
            except Exception:
                pass
        self._modbus_consecutive_failures = 0
        self._modbus_alerted = False
        self._modbus_last_success = datetime.now()

    async def _modbus_write_failure(self) -> None:
        """Handle a failed Modbus write — increment counter, alert if needed."""
        self._modbus_consecutive_failures += 1
        if self._modbus_consecutive_failures >= 3 and not self._modbus_alerted:
            try:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "MPC: Modbus Communication Failed",
                        "message": (
                            f"Cannot write to Victron Cerbo GX. "
                            f"{self._modbus_consecutive_failures} consecutive "
                            f"failures. Registers are NOT being updated."
                        ),
                        "notification_id": "mpc_modbus_down",
                    },
                )
                self._modbus_alerted = True
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Cell balancing
    # ------------------------------------------------------------------

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

        # Solar forecast daily total (sum kW × dt_hours)
        solar_forecast_kwh = round(
            sum(solar_kw) * tunables.dt_hours, 2
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
                "schedule_30min": json.dumps(schedule_30min[:16]),
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
