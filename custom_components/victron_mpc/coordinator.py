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

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    LOGGER,
    REGISTER_ESS_MIN_SOC,
    REGISTER_FEEDIN_BLOCK,
    REGISTER_MAX_FEED_IN,
    UPDATE_INTERVAL_MINUTES,
)


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

        # API health tracking — for notifications
        self._api_health: dict[str, dict[str, Any]] = {
            "amber": {"down_since": None, "alerted": False},
            "vrm": {"down_since": None, "alerted": False},
            "open_meteo": {"down_since": None, "alerted": False},
        }

    async def _async_setup(self) -> None:
        """One-time setup called during first refresh (HA 2024.8+).

        Initialize API clients, validate Modbus connectivity.
        """
        # TODO: Initialize VRM client, Open-Meteo client, FuelPrice client
        # TODO: Validate Modbus connection by reading a known register
        # TODO: Load full charge tracking state from storage
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
            # TODO Phase 1: Read HA entity states
            # soc = float(self.hass.states.get(self.entry.data["battery_soc"]).state)
            # solar_w = float(self.hass.states.get(self.entry.data["solar_power"]).state)
            # ...

            # TODO Phase 2: Fetch external APIs (VRM, Open-Meteo) — cached
            # vrm_data = await self._vrm_client.get_clearsky_envelope()
            # cloud_layers = await self._open_meteo_client.fetch_cloud_layers()

            # TODO Phase 3: Build forecasts
            # forecasts = await self._forecast_builder.build_all()

            # TODO Phase 4: Build OptInput and run optimizer in executor
            # opt_input = self._build_opt_input(forecasts)
            # result = await self.hass.async_add_executor_job(optimize, opt_input)

            # TODO Phase 5: Apply overrides (spike, negative pricing)
            # register = self._apply_overrides(result, amber_data)

            # TODO Phase 6: Write Modbus registers (if not shadow mode)
            # if not self.entry.options.get("shadow_mode", True):
            #     await self._write_register(register)
            #     await self._write_feedin_register(feedin_value)

            self._consecutive_failures = 0

            # Placeholder return until optimizer is ported
            return {
                "status": "placeholder",
                "cycle": self._cycle_count,
            }

        except Exception as err:
            self._consecutive_failures += 1
            raise UpdateFailed(
                f"MPC cycle {self._cycle_count} failed "
                f"({self._consecutive_failures} consecutive): {err}"
            ) from err

    async def _write_register(self, value: int) -> None:
        """Write ESS min SoC register (R2901) via Modbus.

        Value = SoC% × 10, range 100-1000.
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
        except Exception:
            LOGGER.exception("Failed to write R2901=%d", value)

    async def _write_feedin_register(self, value: int) -> None:
        """Write max grid feed-in register (R2706) via Modbus.

        Units = 100W per value (70 = 7000W, 0 = block all export).
        Feed-in rules (first match wins):
        1. Negative buy price → 70 (export everything)
        2. MPC grid_charge mode → 70 (need grid pull)
        3. Spike + FIT > $0.10 + SoC > 30% → 70 (export for profit)
        4. SoC > 95% + FIT > 0 → 70 (battery full, export excess)
        5. Otherwise → 0 (block export, self-consume)
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
        except Exception:
            LOGGER.exception("Failed to write R2706=%d", value)
