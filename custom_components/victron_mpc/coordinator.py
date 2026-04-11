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

import asyncio
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
    REGISTER_FEEDIN_MAX,
    REGISTER_MAX_FEED_IN,
    REGISTER_SAFE_PARK,
    SAFE_PARK_CONSECUTIVE_FAILURES,
    UPDATE_INTERVAL_MINUTES,
)
from .forecast_accuracy import compute_forecast_accuracy
from .forecasts import ForecastBuilder
from .genai_monitor import (
    GENAI_CYCLE_INTERVAL,
    PROMPT_VERSION,
    build_strategic_snapshot,
    run_deterministic_checks,
    run_genai_health_check,
)
from .optimizer import OptInput, OptOutput, compute_sunset_target, optimize
from .utils import scale_overnight_hold_reward

# Amber three-tier escalation thresholds
_AMBER_CAUTIOUS_MINUTES = 15.0  # Default; overridden by tunables.amber_cautious_minutes
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
        self._last_mode: str | None = None
        self._previous_mode: str | None = None  # Mode persistence for thrashing prevention (#78)
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

        # SoC jump detection — track previous cycle's SoC
        self._last_soc_pct: float | None = None

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

        # Emergency stop — one-button MPC disable (Issue #54)
        self._emergency_stopped: bool = False

        # Safe-park — auto-recoverable register parking on consecutive failures (#71)
        self._safe_parked: bool = False

        # Audit log — every register write with full decision context (#56)
        self._audit_log: list[dict[str, Any]] = []
        self._audit_log_max = 2016  # 7 days × 288 cycles/day

        # Human feedback log — captures human verdicts vs UAT/GenAI (#60)
        self._human_feedback_log: list[dict[str, Any]] = []
        self._human_feedback_log_max = 200

        # Modbus write audit log — every register write with source tracking (#65)
        self._modbus_write_log: list[dict[str, Any]] = []
        self._modbus_write_log_max = 2016  # 7 days × 288 cycles/day

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

        # Emergency stop — skip all optimization while active
        if self._emergency_stopped:
            LOGGER.warning(
                "EMERGENCY STOP active — skipping optimization cycle %d",
                self._cycle_count,
            )
            return self.data or {"mode": "emergency_stop", "emergency_stopped": True}

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

            # SoC jump detection — reject physically impossible changes
            if self._last_soc_pct is not None:
                soc_delta = abs(soc_pct - self._last_soc_pct)
                if soc_delta > tunables.soc_max_jump_pct:
                    LOGGER.warning(
                        "SoC jump rejected: %.1f%% -> %.1f%% "
                        "(delta=%.1f%%, max=%.1f%%) — using previous value",
                        self._last_soc_pct, soc_pct,
                        soc_delta, tunables.soc_max_jump_pct,
                    )
                    soc_pct = self._last_soc_pct
            self._last_soc_pct = soc_pct

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
            morning_steps = _compute_overnight_steps(
                now,
                tunables.morning_start_hour,
                tunables.morning_end_hour,
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

            # Scale overnight hold reward by overnight-vs-morning price spread.
            # See scale_overnight_hold_reward docstring and GitHub issue #80.
            overnight_hold = scale_overnight_hold_reward(
                tunables.overnight_hold_reward,
                forecasts["buy_price"],
                overnight_steps,
                morning_steps,
                arbitrage_threshold=tunables.overnight_arbitrage_threshold,
                battery_wear_cost=tunables.battery_wear_cost,
                discharge_penalty=tunables.soc_profile_overnight + tunables.grid_import_penalty,
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
                previous_mode=self._previous_mode,
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
                if minutes_down > max(tunables.amber_blip_minutes, 10.0):
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

            # Price data quality: warn on extreme (but real) values,
            # only clamp on truly impossible values (API corruption).
            # NEM prices can legitimately be $17+ or deeply negative.
            if buy_price_now > tunables.price_max_buy:
                LOGGER.warning(
                    "PRICE DATA: $%.2f exceeds sanity bound ($%.2f) "
                    "— likely API corruption, clamping",
                    buy_price_now, tunables.price_max_buy,
                )
                buy_price_now = tunables.price_max_buy
            elif buy_price_now < tunables.price_min_buy:
                LOGGER.warning(
                    "PRICE DATA: $%.2f below sanity bound ($%.2f) "
                    "— likely API corruption, clamping",
                    buy_price_now, tunables.price_min_buy,
                )
                buy_price_now = tunables.price_min_buy
            elif buy_price_now > 5.0 or buy_price_now < -1.0:
                # Unusual but real — log for awareness, don't clamp
                LOGGER.info(
                    "Extreme price: $%.2f/kWh — real market event, "
                    "override logic will handle",
                    buy_price_now,
                )

            sell_price_now = forecasts["sell_price"][0]
            is_spike = self._is_spike_active()
            override_reason: str | None = None

            override_principle: str | None = None
            if buy_price_now < 0:
                # Negative pricing — charge from grid (we're paid to consume)
                target_register = 1000
                mode = "grid_charge"
                override_reason = (
                    f"Negative pricing (${buy_price_now:.3f}/kWh) — charging"
                )
                override_principle = "negative_capture"
            elif is_spike or buy_price_now > tunables.spike_threshold:
                # Spike — discharge to minimise grid usage
                target_register = 100
                mode = "discharge"
                override_reason = (
                    f"Spike active (${buy_price_now:.3f}/kWh) — discharging"
                )
                override_principle = "spike_protection"
            else:
                # Normal — use optimizer decision
                target_register = result.target_register
                mode = result.mode
                override_reason = None

            # Annotate intent with override information
            if override_reason and result.intent:
                result.intent["override_applied"] = True
                result.intent["override_principle"] = override_principle
                result.intent["override_note"] = (
                    "Post-LP override active — LP decision replaced"
                )

            # #37 hysteresis monitoring — log when BMS rounding triggers hold
            if mode == "hold" and "BMS rounding" in (result.reason or ""):
                LOGGER.info(
                    "HYSTERESIS: SoC=%.1f%% near floor, holding instead of grid_charge",
                    soc_pct,
                )

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

                # Transition safety: when leaving grid_charge, the old
                # high register value may still be active on the Cerbo
                # (propagation lag).  Write the hard floor first to stop
                # any unintended grid import, then write the real target.
                leaving_grid_charge = (
                    self._last_mode == "grid_charge"
                    and mode != "grid_charge"
                )
                if leaving_grid_charge:
                    floor_register = int(
                        max(tunables.soc_floor_pct, system.soc_min_pct) * 10
                    )
                    LOGGER.info(
                        "TRANSITION SAFETY: grid_charge → %s, "
                        "writing floor %d before target %d",
                        mode, floor_register, target_register,
                    )
                    await self._write_register(floor_register)
                    await asyncio.sleep(2)

                await self._write_register(target_register)
                await self._write_feedin_register(feedin_value)
            else:
                LOGGER.info(
                    "SHADOW: Would write R2901=%d, R2706=%d",
                    target_register,
                    feedin_value,
                )

            self._last_mode = mode
            self._previous_mode = mode  # Mode persistence for next cycle (#78)

            # ----------------------------------------------------------
            # Phase 7a: Audit log — persist every decision (#56)
            # ----------------------------------------------------------
            self._audit_log.append({
                "cycle": self._cycle_count,
                "timestamp": datetime.now().isoformat(),
                "mode": mode,
                "register": target_register,
                "feedin": feedin_value,
                "soc_pct": round(soc_pct, 1),
                "buy_price": round(buy_price_now, 4),
                "sell_price": round(sell_price_now, 4),
                "solar_w": round(forecasts.get("current_solar_w", 0)),
                "load_w": round(forecasts.get("current_load_w", 0)),
                "cost_24h": round(result.total_cost, 4),
                "reason": override_reason or result.reason,
                "shadow": shadow_mode,
                "spike": is_spike,
            })
            if len(self._audit_log) > self._audit_log_max:
                self._audit_log = self._audit_log[-self._audit_log_max:]

            # Record full charge if SoC near 100%
            if soc_pct >= 95:
                self._last_full_charge_check = datetime.now()

            self._consecutive_failures = 0
            if self._safe_parked:
                LOGGER.info(
                    "SAFE-PARK recovered — disabling fallback automations"
                )
                self._safe_parked = False
                try:
                    await self.hass.services.async_call(
                        "automation", "turn_off",
                        {"entity_id": [
                            "automation.fallback_battery_management",
                            "automation.fallback_feed_in_control",
                        ]},
                    )
                except Exception:
                    LOGGER.exception(
                        "Safe-park recovery: failed to disable fallback automations"
                    )
                await self._notify(
                    "MPC: Recovered",
                    "MPC optimizer recovered. Fallback automations deactivated. "
                    "MPC is back in control.",
                    notification_id="mpc_safe_park",
                )

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
                    details="; ".join(r["reason"] for r in deterministic_results),
                    deterministic_checks=[r["check"] for r in deterministic_results],
                    amber_band=(forecasts.get("price_bands") or ["unknown"])[0],
                    weather=extra.get("weather", "unknown"),
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
                # All deterministic checks passed.
                # Preserve GenAI YELLOW verdict until next GenAI cycle — don't let
                # deterministic GREEN overwrite it. GenAI runs hourly and should
                # retain its verdict for that full hour. GitHub issue #79.
                if (self._last_genai_result.get("source") == "genai"
                        and self._last_genai_result.get("status") == "YELLOW"):
                    # Keep GenAI YELLOW — deterministic GREEN doesn't override
                    pass
                else:
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
                        details=genai_result.get("details", ""),
                        deterministic_checks=[],
                        amber_band=(forecasts.get("price_bands") or ["unknown"])[0],
                        weather=extra.get("weather", "unknown"),
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

            # Safe-park: after N consecutive failures, write safe registers
            # and notify. This is auto-recoverable — next successful cycle
            # clears the flag. Does NOT set _emergency_stopped.
            if (
                self._consecutive_failures >= SAFE_PARK_CONSECUTIVE_FAILURES
                and not self._safe_parked
            ):
                self._safe_parked = True
                LOGGER.warning(
                    "SAFE-PARK: %d consecutive failures — activating "
                    "fallback automations",
                    self._consecutive_failures,
                )
                shadow_mode = self.entry.options.get("shadow_mode", True)
                if not shadow_mode:
                    # Write immediate safe registers
                    try:
                        self._last_register_value = None
                        await self._write_register(REGISTER_SAFE_PARK)
                    except Exception:
                        LOGGER.exception(
                            "Safe-park: failed to write R2901=%d",
                            REGISTER_SAFE_PARK,
                        )
                    try:
                        self._last_feedin_value = None
                        await self._write_feedin_register(REGISTER_FEEDIN_BLOCK)
                    except Exception:
                        LOGGER.exception(
                            "Safe-park: failed to write R2706=%d",
                            REGISTER_FEEDIN_BLOCK,
                        )
                # Enable fallback rate-based automations
                try:
                    await self.hass.services.async_call(
                        "automation", "turn_on",
                        {"entity_id": [
                            "automation.fallback_battery_management",
                            "automation.fallback_feed_in_control",
                        ]},
                    )
                except Exception:
                    LOGGER.exception(
                        "Safe-park: failed to enable fallback automations"
                    )
                await self._notify(
                    "MPC: Fallback Mode Active",
                    f"MPC optimizer failed {self._consecutive_failures} "
                    f"consecutive cycles (~{self._consecutive_failures * UPDATE_INTERVAL_MINUTES} min). "
                    "Fallback rate-based automations are now active "
                    "(charge when cheap, ride out spikes). "
                    "Will auto-recover on next successful cycle.",
                    notification_id="mpc_safe_park",
                )

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
            blip_threshold = max(blip_threshold, 10.0)
            defensive_price = blip_min.defensive_price if blip_min else 2.00

            if blip_min and blip_min.amber_blip_minutes < 10.0:
                LOGGER.info(
                    "Amber blip threshold floored: config=%.0f min, effective=10 min",
                    blip_min.amber_blip_minutes,
                )

            if minutes_down < blip_threshold:
                # Tier 1: Brief blip — use last known price
                return (False, self._last_known_buy_price)

            cautious_minutes = (
                blip_min.amber_cautious_minutes
                if blip_min and hasattr(blip_min, "amber_cautious_minutes")
                else _AMBER_CAUTIOUS_MINUTES
            )
            if minutes_down < cautious_minutes:
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
        *,
        details: str = "",
        deterministic_checks: list[str] | None = None,
        amber_band: str = "unknown",
        weather: str = "unknown",
    ) -> None:
        """Append health check result to rolling history buffer.

        Args:
            source: Origin of the check ("deterministic" or "genai").
            status: Overall status string ("GREEN", "YELLOW", "RED", or "?").
            summary: Short human-readable summary of the result.
            soc_pct: Battery state-of-charge at time of check.
            mode: Current MPC operating mode (e.g. "discharge", "grid_charge").
            buy_price: Current Amber buy price in $/kWh.
            solar_w: Current solar generation in watts.
            load_w: Current house load in watts.
            grid_import_w: Current grid import in watts.
            details: Full reasoning text from GenAI or deterministic details.
            deterministic_checks: Names of checks that returned RED; empty if all GREEN.
            amber_band: Current Amber price band descriptor (e.g. "low", "spike").
            weather: Current weather condition string.
        """
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
            "details": details,
            "prompt_version": PROMPT_VERSION,
            "deterministic_checks": deterministic_checks if deterministic_checks is not None else [],
            "amber_band": amber_band,
            "weather": weather,
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

    async def record_human_feedback(
        self,
        human_verdict: str,
        human_reasoning: str,
        uat_verdict: str = "",
        genai_verdict: str = "",
    ) -> None:
        """Record a human feedback entry capturing human vs system verdicts.

        Called via the ``record_feedback`` HA service when the human agrees,
        disagrees, or overrides the UAT or GenAI assessment.

        Args:
            human_verdict: One of "agree", "disagree", or "override".
            human_reasoning: Free-text explanation of the human's decision.
            uat_verdict: The UAT assessment verdict at time of feedback (optional).
            genai_verdict: The GenAI assessment verdict at time of feedback (optional).
        """
        # Build a resilient system snapshot — each state read uses .get() with
        # safe defaults so the method works even in tests without HA.
        soc_pct: float = 0.0
        mode: str = "unknown"
        buy_price: float = 0.0
        solar_w: int = 0
        load_w: int = 0
        grid_import_w: int = 0
        amber_band: str = "unknown"
        weather: str = "unknown"

        try:
            data = self.data or {}
            soc_pct = float(data.get("battery_plan") or 0.0)
            mode = str(data.get("decision") or "unknown")
            if isinstance(data.get("decision"), dict):
                mode = data["decision"].get("state", "unknown")
            buy_price = float(data.get("buy_price") or 0.0)
            solar_w = int(data.get("solar_input_w") or 0)
            load_w = int(data.get("load_input_w") or 0)
        except (TypeError, ValueError):
            pass

        try:
            forecasts = getattr(self, "_last_forecasts", {}) or {}
            bands = forecasts.get("price_bands") or []
            if bands:
                amber_band = str(bands[0])
            weather = str(forecasts.get("weather_condition", "unknown"))
        except (TypeError, AttributeError):
            pass

        try:
            grid_state = self.hass.states.get("sensor.victron_grid_power")
            if grid_state and grid_state.state not in ("unknown", "unavailable"):
                grid_import_w = max(0, int(float(grid_state.state)))
        except (TypeError, ValueError, AttributeError):
            pass

        # Current GenAI status from last result
        genai_status = self._last_genai_result.get("status", "") if self._last_genai_result else ""

        entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "human_verdict": human_verdict,
            "human_reasoning": human_reasoning,
            "uat_verdict": uat_verdict,
            "genai_verdict": genai_verdict,
            "genai_status": genai_status,
            "prompt_version": PROMPT_VERSION,
            "readings": {
                "soc_pct": round(soc_pct, 1),
                "mode": mode,
                "buy_price": round(buy_price, 4),
                "solar_w": solar_w,
                "load_w": load_w,
                "grid_import_w": grid_import_w,
                "amber_band": amber_band,
                "weather": weather,
            },
        }

        self._human_feedback_log.append(entry)
        if len(self._human_feedback_log) > self._human_feedback_log_max:
            self._human_feedback_log = self._human_feedback_log[-self._human_feedback_log_max:]

        LOGGER.info(
            "Human feedback recorded: verdict=%s, uat=%s, genai=%s — %s",
            human_verdict, uat_verdict, genai_verdict, human_reasoning,
        )

    def _compute_disagreement_stats(self) -> dict[str, Any]:
        """Compute disagreement rate statistics from the human feedback log.

        Returns:
            Dict with total_feedback, disagree_count, agree_count,
            disagreement_rate (0.0-1.0), and recent_7d sub-dict with the
            same fields filtered to the last 7 days.
        """
        log = self._human_feedback_log
        total = len(log)
        disagree_count = sum(1 for e in log if e.get("human_verdict") == "disagree")
        agree_count = sum(1 for e in log if e.get("human_verdict") == "agree")
        rate = round(disagree_count / total, 4) if total > 0 else 0.0

        # Recent 7-day window
        cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
        recent = [e for e in log if e.get("timestamp", "") >= cutoff]
        recent_total = len(recent)
        recent_disagree = sum(1 for e in recent if e.get("human_verdict") == "disagree")
        recent_agree = sum(1 for e in recent if e.get("human_verdict") == "agree")
        recent_rate = round(recent_disagree / recent_total, 4) if recent_total > 0 else 0.0

        return {
            "total_feedback": total,
            "disagree_count": disagree_count,
            "agree_count": agree_count,
            "disagreement_rate": rate,
            "recent_7d": {
                "total_feedback": recent_total,
                "disagree_count": recent_disagree,
                "agree_count": recent_agree,
                "disagreement_rate": recent_rate,
            },
        }

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
            return REGISTER_FEEDIN_MAX  # 70

        if mode == "grid_charge":
            LOGGER.debug("R2706: Rule 2 — grid_charge mode, allow import")
            return REGISTER_FEEDIN_MAX  # 70

        tunables = getattr(self, "_tunables", None)
        fit_threshold = tunables.feedin_export_threshold if tunables else 0.10
        soc_threshold = tunables.feedin_soc_threshold if tunables else 30.0

        if is_spike and sell_price > fit_threshold and soc_pct > soc_threshold:
            LOGGER.debug(
                "R2706: Rule 3 — spike + FIT $%.2f + SoC %.0f%%, export for profit",
                sell_price,
                soc_pct,
            )
            return REGISTER_FEEDIN_MAX  # 70

        if soc_pct > 95 and sell_price > 0:
            LOGGER.debug("R2706: Rule 4 — battery full + positive FIT, export excess")
            return REGISTER_FEEDIN_MAX  # 70

        LOGGER.debug("R2706: Rule 5 — block export, self-consume")
        return REGISTER_FEEDIN_BLOCK  # 0

    # ------------------------------------------------------------------
    # Emergency stop (Issue #54)
    # ------------------------------------------------------------------

    async def async_emergency_stop(self) -> None:
        """Activate emergency stop — safe registers + skip future cycles.

        Sets R2901 to hard floor (300 = 30%), R2706 to 0 (block export),
        and prevents all future optimization cycles until resumed.
        """
        LOGGER.warning("EMERGENCY STOP activated — writing safe registers")
        self._emergency_stopped = True

        # Write safe registers immediately
        shadow_mode = self.entry.options.get("shadow_mode", True)
        if not shadow_mode:
            try:
                # R2901 = 300 (30% hard floor)
                self._last_register_value = None  # Force write
                await self._write_register(300)
            except Exception:
                LOGGER.exception("Emergency stop: failed to write R2901=300")
            try:
                # R2706 = 0 (block all export)
                self._last_feedin_value = None  # Force write
                await self._write_feedin_register(REGISTER_FEEDIN_BLOCK)
            except Exception:
                LOGGER.exception("Emergency stop: failed to write R2706=0")

        await self._notify(
            "MPC: EMERGENCY STOP",
            "MPC optimizer has been stopped. Registers set to safe defaults "
            "(R2901=300, R2706=0). All optimization cycles are paused. "
            "Call victron_mpc_battery_optimizer.emergency_resume to restart.",
            notification_id="mpc_emergency_stop",
        )
        LOGGER.warning("EMERGENCY STOP complete — optimization paused")

    async def async_emergency_resume(self) -> None:
        """Clear emergency stop — resume normal optimization cycles."""
        if not self._emergency_stopped:
            LOGGER.info("Emergency resume called but emergency stop is not active")
            return

        LOGGER.warning("EMERGENCY STOP cleared — resuming optimization")
        self._emergency_stopped = False

        await self._notify(
            "MPC: Emergency Stop Cleared",
            "MPC optimizer resumed. Normal optimization cycles will begin "
            "on the next 5-minute interval.",
            notification_id="mpc_emergency_stop",
        )

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

    def _compute_prompt_version_stats(self) -> dict[str, dict[str, Any]]:
        """Compute per-prompt-version statistics from the GenAI history buffer.

        Groups genai_history entries by their ``prompt_version`` field and
        produces aggregate statistics for each version.  This enables
        data-driven prompt calibration — callers can compare yellow_rate
        across versions to measure whether a new prompt reduces false
        positives.

        Returns:
            Dict keyed by version string (e.g. ``"v4"``).  Each value is a
            dict with the following fields:

            - ``total_assessments`` (int): Number of history entries for the
              version.
            - ``yellow_count`` (int): Entries whose ``status`` is ``"YELLOW"``.
            - ``green_count`` (int): Entries whose ``status`` is ``"GREEN"``.
            - ``error_count`` (int): Entries with status ``"ERROR"``,
              ``"SKIP"``, or ``"?"`` (non-actionable results).
            - ``yellow_rate`` (float): ``yellow_count / total_assessments``,
              or 0.0 when there are no entries.
            - ``first_seen`` (str): ISO 8601 timestamp of the earliest entry
              for this version.
            - ``last_seen`` (str): ISO 8601 timestamp of the most recent entry
              for this version.
        """
        stats: dict[str, dict[str, Any]] = {}
        for entry in self._genai_history:
            version = entry.get("prompt_version", "unknown")
            if version not in stats:
                stats[version] = {
                    "total_assessments": 0,
                    "yellow_count": 0,
                    "green_count": 0,
                    "error_count": 0,
                    "yellow_rate": 0.0,
                    "first_seen": entry.get("timestamp", ""),
                    "last_seen": entry.get("timestamp", ""),
                }
            bucket = stats[version]
            bucket["total_assessments"] += 1
            status = entry.get("status", "")
            if status == "YELLOW":
                bucket["yellow_count"] += 1
            elif status == "GREEN":
                bucket["green_count"] += 1
            elif status in ("ERROR", "SKIP", "?"):
                bucket["error_count"] += 1
            ts = entry.get("timestamp", "")
            if ts:
                if not bucket["first_seen"] or ts < bucket["first_seen"]:
                    bucket["first_seen"] = ts
                if not bucket["last_seen"] or ts > bucket["last_seen"]:
                    bucket["last_seen"] = ts

        # Compute yellow_rate as a final pass to avoid repeated division
        for bucket in stats.values():
            total = bucket["total_assessments"]
            bucket["yellow_rate"] = round(bucket["yellow_count"] / total, 4) if total > 0 else 0.0

        return stats

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
            # Enriched with LP explainability (#59): exposes WHY the LP
            # chose this mode, what the key cost drivers are, and how
            # confident the forecasts are.
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
                # LP explainability (#59) — expose reasoning
                "solve_time_ms": round(result.solve_time_ms),
                "solver_status": result.solver_status,
                "cost_grid": round(breakdown.get("grid_cost", 0), 4),
                "cost_wear": round(breakdown.get("wear_cost", 0), 4),
                "revenue_export": round(breakdown.get("export_revenue", 0), 4),
                "charge_next_kw": round(result.charge_schedule_kw[0], 2) if result.charge_schedule_kw else 0,
                "discharge_next_kw": round(result.discharge_schedule_kw[0], 2) if result.discharge_schedule_kw else 0,
                "grid_import_next_kw": round(result.grid_import_schedule_kw[0], 2) if result.grid_import_schedule_kw else 0,
                "solar_used_next_kw": round(result.solar_used_schedule_kw[0], 2) if result.solar_used_schedule_kw else 0,
                "solar_forecast_kwh_today": solar_forecast_kwh,
                "weather_confidence": forecasts.get("weather_confidence", 1.0),
                "intent": result.intent,
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
            "safe_parked": self._safe_parked,
            "amber_forecast_log_entries": len(self._amber_forecast_log),
            "genai_health": {
                **self._last_genai_result,
                "history": self._genai_history,
            },
            "amber_forecast_accuracy": {
                "state": self._forecast_accuracy_cache.get("entry_count", 0),
                **self._forecast_accuracy_cache,
            },
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
            # Audit log — last 50 register writes with context (#56)
            "audit_log": {
                "state": len(self._audit_log),
                "entries": self._audit_log[-50:],
            },
            # GenAI audit log — full history for analysis (#57)
            # version_stats and current_version added for #61 prompt calibration
            "genai_audit_log": {
                "state": len(self._genai_history),
                "entries": self._genai_history,
                "false_positive_count": sum(
                    1 for e in self._genai_history if e.get("status") == "YELLOW"
                ),
                "prompt_version": PROMPT_VERSION,
                "current_version": PROMPT_VERSION,
                "version_stats": self._compute_prompt_version_stats(),
            },
            # Human feedback log — captures human vs UAT/GenAI verdicts (#60)
            "human_feedback": {
                "state": len(self._human_feedback_log),
                "entries": self._human_feedback_log[-50:],
                "disagreement_stats": self._compute_disagreement_stats(),
                "total_entries": len(self._human_feedback_log),
            },
            # Modbus write audit log — every register write with source tracking (#65)
            "modbus_write_log": {
                "state": len(self._modbus_write_log),
                "entries": self._modbus_write_log[-50:],
                "rogue_write_count": sum(
                    1 for e in self._modbus_write_log if e.get("source") == "unknown_external"
                ),
                "total_writes": len(self._modbus_write_log),
            },
        }

    # ------------------------------------------------------------------
    # Modbus register writes
    # ------------------------------------------------------------------

    async def _write_register(self, value: int) -> None:
        """Write ESS min SoC register (R2901) via Modbus with read-back verification.

        Value = SoC% x 10, range 100-1000.
        Acts as floor AND target: ESS discharges down to this value.

        After writing, waits briefly and reads back the register via the HA
        sensor.  If the readback doesn't match (within tolerance), retries
        once.  A persistent mismatch is logged as a warning — something
        else (BatteryLife, another automation) may be overwriting the register.
        """
        # Don't re-write if unchanged
        if value == self._last_register_value:
            return

        value = max(100, min(1000, value))  # Clamp to valid range
        previous = self._last_register_value

        # Audit tracking — populated during write/verify loop
        verified: bool | None = None
        final_readback: float | None = None
        persistent_mismatch = False

        for attempt in range(2):  # write + 1 retry
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
            except Exception:
                LOGGER.exception("Failed to write R2901=%d", value)
                await self._modbus_write_failure()
                return

            # Read-back verification: wait for Modbus sensor to update,
            # then compare.  The HA Modbus integration polls on its own
            # schedule so the sensor may still show the old value.
            await asyncio.sleep(2)
            readback = self._get_r2901_readback()

            if readback < 0:
                # Sensor unavailable — can't verify, accept the write
                LOGGER.info(
                    "R2901 written: %d (was %s), readback unavailable",
                    value, previous,
                )
                verified = None
                final_readback = None
                break

            final_readback = readback
            # Tolerance: register is SoC% × 10, sensor is SoC%.
            # Accept if within 1% (10 register units).
            expected_pct = value / 10.0
            if abs(readback - expected_pct) <= 1.0:
                LOGGER.info(
                    "R2901 written: %d (was %s), readback %.1f%% — verified",
                    value, previous, readback,
                )
                verified = True
                break

            if attempt == 0:
                LOGGER.warning(
                    "R2901 READBACK MISMATCH: wrote %d (%.1f%%) but read "
                    "%.1f%%. Retrying in 2s.",
                    value, value / 10.0, readback,
                )
            else:
                verified = False
                persistent_mismatch = True
                LOGGER.warning(
                    "R2901 READBACK MISMATCH PERSISTENT: wrote %d (%.1f%%) "
                    "but read %.1f%% after retry. Possible BatteryLife "
                    "override or Modbus contention.",
                    value, value / 10.0, readback,
                )
        else:
            # Both attempts had mismatches — still update our tracking
            # so we don't skip the next write.
            pass

        self._last_register_value = value
        await self._modbus_write_success()

        # Append to Modbus write audit log (#65)
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "register": REGISTER_ESS_MIN_SOC,
            "register_name": "ess_min_soc",
            "value": value,
            "previous_value": previous,
            "source": "hacs_mpc",
            "verified": verified,
            "readback_value": final_readback,
            "cycle": getattr(self, "_cycle_count", None),
            "mode": getattr(self, "_last_mode", None),
            "reason": "R2901 write — ESS min SoC floor",
        }
        self._modbus_write_log.append(log_entry)
        if len(self._modbus_write_log) > self._modbus_write_log_max:
            self._modbus_write_log = self._modbus_write_log[-self._modbus_write_log_max:]

        # If persistent mismatch detected, also log a rogue write suspicion entry
        if persistent_mismatch and final_readback is not None:
            rogue_entry: dict[str, Any] = {
                "timestamp": datetime.now().isoformat(),
                "register": REGISTER_ESS_MIN_SOC,
                "register_name": "ess_min_soc",
                "source": "unknown_external",
                "expected_value": value,
                "actual_value": int(final_readback * 10),
                "alert": True,
                "cycle": getattr(self, "_cycle_count", None),
                "mode": getattr(self, "_last_mode", None),
            }
            self._modbus_write_log.append(rogue_entry)
            if len(self._modbus_write_log) > self._modbus_write_log_max:
                self._modbus_write_log = self._modbus_write_log[-self._modbus_write_log_max:]

    async def _write_feedin_register(self, value: int) -> None:
        """Write max grid feed-in register (R2706) via Modbus.

        Units = 100W per value (70 = 7000W, 0 = block all export).
        Only two values are allowed: 0 (block) or 70 (full inverter capacity).
        """
        if value == self._last_feedin_value:
            return

        if value not in (REGISTER_FEEDIN_BLOCK, REGISTER_FEEDIN_MAX):
            LOGGER.error(
                "R2706 safety: value %d not in {%d, %d} — defaulting to %d",
                value, REGISTER_FEEDIN_BLOCK, REGISTER_FEEDIN_MAX,
                REGISTER_FEEDIN_BLOCK,
            )
            value = REGISTER_FEEDIN_BLOCK

        previous_feedin = self._last_feedin_value
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
            # Append to Modbus write audit log (#65)
            self._modbus_write_log.append({
                "timestamp": datetime.now().isoformat(),
                "register": REGISTER_MAX_FEED_IN,
                "register_name": "max_feed_in",
                "value": value,
                "previous_value": previous_feedin,
                "source": "hacs_mpc",
                "verified": None,  # No read-back on R2706
                "readback_value": None,
                "cycle": getattr(self, "_cycle_count", None),
                "mode": getattr(self, "_last_mode", None),
                "reason": "R2706 write — max grid feed-in",
            })
            if len(self._modbus_write_log) > self._modbus_write_log_max:
                self._modbus_write_log = self._modbus_write_log[-self._modbus_write_log_max:]
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
            # Append to Modbus write audit log (#65)
            self._modbus_write_log.append({
                "timestamp": datetime.now().isoformat(),
                "register": REGISTER_BATTERYLIFE_STATE,
                "register_name": "batterylife_state",
                "value": 12,
                "previous_value": None,  # Not tracked separately
                "source": "hacs_mpc",
                "verified": None,  # No read-back on R2900
                "readback_value": None,
                "cycle": getattr(self, "_cycle_count", None),
                "mode": getattr(self, "_last_mode", None),
                "reason": "R2900 write — disable BatteryLife",
            })
            if len(self._modbus_write_log) > self._modbus_write_log_max:
                self._modbus_write_log = self._modbus_write_log[-self._modbus_write_log_max:]
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
