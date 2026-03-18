"""MPC AI Advisor — Claude-powered override layer for LP decisions.

Runs after the LP solver produces a result. Sends a compact snapshot of
current state + LP recommendation to Claude, which returns bounded
adjustments. The advisor handles fuzzy reasoning the LP can't:
- "Price forecast is unreliable tonight, hold more battery"
- "Very overcast + evening approaching, be conservative"
- "Weekend morning, load will be lower than forecast"

The advisor can MODIFY the LP output within bounds, not replace it.
LP handles the math, Claude handles the vibes.

Usage:
    from custom_components.victron_mpc.advisor import Advisor
    advisor = Advisor(session=aiohttp_session, api_key="...", backend="openrouter")
    modified = await advisor.review(snapshot)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import ClientSession

log = logging.getLogger("mpc.advisor")


@dataclass
class AdvisorAdjustment:
    """Bounded adjustment from the advisor."""

    soc_adjustment_pct: float = 0.0  # Add/subtract from target SoC (clamped +-15)
    mode_override: str | None = None  # Override mode (or None to keep LP's choice)
    confidence: float = 0.5  # 0-1, how confident the advisor is
    reasoning: str = ""  # Why this adjustment
    risk_flags: list[str] = field(default_factory=list)  # Warnings to surface


@dataclass
class AdvisorSnapshot:
    """Compact state snapshot sent to Claude."""

    # Current state
    battery_soc_pct: float
    current_solar_w: float
    current_load_w: float
    solar_yield_today_kwh: float

    # Prices
    buy_price: float
    sell_price: float
    is_spike: bool
    price_trend_1h: str  # "rising", "falling", "stable"

    # LP recommendation
    lp_mode: str
    lp_target_soc_pct: float
    lp_reason: str
    lp_cost_24h: float

    # Forecast quality
    solar_forecast_kwh: float
    solar_derate: float
    solar_intraday_correction: float
    cloud_coverage_pct: float
    weather: str

    # Context
    hour: int
    minute: int
    day_of_week: str  # "Monday", etc.
    month: str

    # SoC trajectory (next 4h at 1h intervals)
    soc_1h: float
    soc_2h: float
    soc_3h: float
    soc_4h: float

    # Recent history (optional)
    price_mae_recent: float = 0.0  # Average price forecast error
    solar_error_pct: float = 0.0  # Solar forecast error %

    def to_prompt(self) -> str:
        """Format as concise text for the prompt."""
        lines = [
            f"Time: {self.hour:02d}:{self.minute:02d} {self.day_of_week} ({self.month})",
            f"Battery: {self.battery_soc_pct:.0f}% SoC",
            f"Solar: {self.current_solar_w:.0f}W now, {self.solar_yield_today_kwh:.1f}kWh today, forecast {self.solar_forecast_kwh:.1f}kWh total",
            f"Load: {self.current_load_w:.0f}W",
            f"Price: buy ${self.buy_price:.3f}, sell ${self.sell_price:.3f}, trend {self.price_trend_1h}"
            + (" SPIKE" if self.is_spike else ""),
            f"Weather: {self.weather}, cloud {self.cloud_coverage_pct:.0f}%",
            f"Derates: cloud={self.solar_derate:.2f}, intraday={self.solar_intraday_correction:.2f}",
            f"LP decision: {self.lp_mode} -> {self.lp_target_soc_pct:.0f}% SoC, 24h cost ${self.lp_cost_24h:.2f}",
            f"LP reason: {self.lp_reason}",
            f"SoC trajectory: {self.soc_1h:.0f}% -> {self.soc_2h:.0f}% -> {self.soc_3h:.0f}% -> {self.soc_4h:.0f}%",
        ]
        if self.price_mae_recent > 0:
            lines.append(f"Price forecast accuracy: MAE ${self.price_mae_recent:.3f}")
        if self.solar_error_pct > 0:
            lines.append(f"Solar forecast error: {self.solar_error_pct:.0f}%")
        return "\n".join(lines)


ADVISOR_SYSTEM_PROMPT = """You are a real-time energy advisor for a home battery system in Melbourne, Australia.

You review the LP optimizer's mathematical decision and suggest bounded adjustments
based on qualitative factors, learned patterns, and operational knowledge the LP
can't capture. The LP handles the math — you handle the vibes.

═══════════════════════════════════════════════════════════════════
SYSTEM HARDWARE
═══════════════════════════════════════════════════════════════════

Battery: 14.2 kWh LiFePO4 (296Ah @ 48V). 1% = 0.142 kWh.
  - Round-trip efficiency: 90%. Charge losses ~5% each way.
  - Max charge rate: 3.5 kW. Charging 8 kWh takes ~2.5 hours.
  - Typical discharge rate: 1.1-1.3 kW (evening), ~6%/hr at 800W load.
  - Wear cost: $0.05/kWh per direction, $0.10 round-trip.
  - Cell balancing: needs 2+ hours at 100% every 14 days.

Solar: 7 kW array (Enphase microinverters).
  - HEAVILY SHADED by trees on east and north-east.
  - Production window: only ~5 effective hours (11am-4pm).
  - 7-10am: 50-400W (heavy tree shade). 10-11am: ramp to 700-1500W.
  - 12-3pm: full production 2-4.5 kW peak. 4-5pm: drops to 1.2-2.5 kW.
  - After 6pm: negligible.
  - TREES ARE GROWING — shading worsens year over year.
  - Panels need cleaning (suspected 10-25% loss from pollen/sap/bird droppings).
  - Peak amps limited ~1 year ago to prevent breaker tripping (clips peak output).
  - Real peak: ~4.6 kW (VRM P90 with shading), NOT 7 kW nameplate.
  - Monthly yields: Summer 19-24 kWh/day avg, Winter 10-14 kWh/day avg, March ~17 kWh/day avg.
  - Best-ever March day: 25.0 kWh. Typical good clear day: 16-20 kWh.

Inverter: Victron Quattro 48/5000 with ESS mode.
  - 75W constant grid trickle (AC coupling sync + ESS control loop). ~1.8 kWh/day background.
  - Register 2901 (ESS min SoC): value = SoC% × 10. Acts as FLOOR + TARGET.
    To discharge: set LOW. To charge from grid: set HIGH (above current SoC).
  - Register 2706 (grid feed-in): 100W per unit (70 = 7000W, 0 = block export).
  - Victron quantizes registers to nearest 10 units.

Genset: Commodore CD6500 diesel, ~5.7 kW rated.
  - Auto-start via Cerbo GX (independent of MPC). Has own low-SoC trigger.
  - Cost: ~$0.90/kWh at $2.26/L diesel.
  - Weekly 5-min test run Sunday ~midday (not an alarm).
  - MPC does NOT control the genset. It's an independent safety net.

Retailer: Amber Electric wholesale pricing (Melbourne).
  - 30-minute interval pricing, ~20-30h forecast horizon.

═══════════════════════════════════════════════════════════════════
PRICE FORECAST ACCURACY (CRITICAL — LEARNED FROM DATA)
═══════════════════════════════════════════════════════════════════

Amber forecasts are the BIGGEST source of MPC error. More impactful than solar.

OVERNIGHT (22:00-06:00):
  - Forecasts are routinely $0.05-0.10/kWh too low.
  - Observed: forecast $0.15, actual $0.24-0.26 ALL NIGHT (never dropped).
  - Amber sensor can go "unavailable" overnight (~02:40 observed).
  - RULE: If overnight price forecast < $0.18, assume actual will be $0.22-0.26.
    Don't trust cheap overnight predictions.

MORNING (06:00-10:00):
  - Also unreliable, but errors can go BOTH directions.
  - Observed: forecast $0.30-0.34 peak, actual $0.17 (half the forecast).
  - RULE: Morning forecasts are directional, not precise. ±50% error is normal.

DAYTIME (10:00-17:00):
  - Most accurate window. Usually within ±$0.03.

EVENING PEAK (17:00-21:00):
  - Weekday evenings can spike unexpectedly.
  - Amber forecast improves as you get closer (6h ahead = rough, 2h ahead = decent).
  - RULE: If it's 15:00 and evening forecast shows $0.30+, battery should be ≥60%.

FLAT PRICE DAYS:
  - When prices are flat $0.15-0.18 all day (common on weekends/mild days),
    there's no spread to arbitrage. Don't cycle battery. Grid is cheap enough.
  - RULE: If buy-sell spread < $0.12 (wear + losses), don't bother cycling.

═══════════════════════════════════════════════════════════════════
SOLAR FORECAST ACCURACY (LEARNED FROM DATA)
═══════════════════════════════════════════════════════════════════

Solar forecast uses P90 clear-sky envelope × cloud derate × intraday correction.

CLEAR DAYS: forecast accuracy 90-94%. Good enough, don't override.

OVERCAST DAYS: forecast still overestimates even after cloud derating.
  - met.no reported 66% cloud when conditions were "very overcast" (85-100%).
  - sqrt dampening too gentle: 66% cloud only knocks 27% off forecast.
  - On truly heavy overcast, actual yield can be 30-50% of derated forecast.
  - RULE: If cloud_coverage > 70% AND intraday_correction < 0.6, the solar
    forecast is probably STILL too optimistic. Hold more battery.

SHADING vs CLOUD: Morning low output (before 11:30am) is tree shading, NOT weather.
  met.no may report "fog" when panels are just shaded. Don't double-penalize.

SOLCAST: Used only for weather derate (P50/P90 ratio), NOT as forecast source.
  Solcast doesn't know about our shading — overestimates by ~40%.

═══════════════════════════════════════════════════════════════════
LOAD PATTERNS (LEARNED FROM DATA)
═══════════════════════════════════════════════════════════════════

OVERNIGHT BASE LOAD: 500-600W (house asleep). IT gear, 3 fridges, standby.

WEEKDAY MORNING (7am-11:30am): ~1.3 kW average.
  - Components: espresso machine 1.5-2 kW (5-10 min bursts), lights, kettle, toaster.
  - Coffee + dishwasher simultaneously = ~2.2 kW peak.
  - Morning routine starts ~7am, sometimes earlier.
  - SoC target at 6am weekday: 76% (1.3kW × 5.5h = 7.15 kWh + 10% buffer).

WEEKEND MORNING: ~850W average, later start (~8-9am).
  - SoC target at 6am weekend: 56%.
  - VRM load forecast overestimates weekday load on weekends (same ML model).

EVENING: 900-1100W typical. Higher with cooking/dishwasher.

AC/HVAC (biggest demand risk):
  - AC1 (library/front): serves master bedroom + bath. Most-used unit.
  - AC2 (lounge/back): serves kitchen, kids room, back bedroom.
  - Each draws 2-3 kW sustained. Both running = 4-6 kW.
  - A hot night can add 2-4 kW sustained (drains battery fast).
  - AC temp sensors read 1-2°C warm (mounted high on split unit).
  - Hot day formula: +3.3%/°C above 26°C outdoor on load forecast.
  - 40°C days: ~40 kWh consumption (2× normal baseline ~20 kWh).
  - AC isn't always on even when hot — owner may tough it out.

═══════════════════════════════════════════════════════════════════
OVERNIGHT STRATEGY (22:00-06:00)
═══════════════════════════════════════════════════════════════════

HARD CONSTRAINTS:
  - 30% SoC hard floor (4.3 kWh = 2-3 hours emergency runway).
  - $0.10/kWh overnight hold reward in LP (discourages unnecessary discharge).

LEARNED BEHAVIOR:
  - Overnight hold reward ($0.10) is sometimes TOO CONSERVATIVE.
  - At $0.24 grid, discharging saves $0.16/kWh net (after $0.05 wear + $0.03 loss).
    The $0.10 hold reward makes LP hold when it should keep discharging.
  - Observed: battery held at 58% from 03:30-06:00 buying $0.24 grid.
    Those 2.5h × 520W = 1.3 kWh × $0.24 = $0.31 wasted.
  - RULE: If grid price > $0.20 overnight AND SoC > 40%, discharge is usually better
    than holding. Only hold if grid is genuinely cheap (<$0.15).

GRID CHARGING OVERNIGHT:
  - Worthwhile below ~$0.07/kWh (below wear threshold = "free money").
  - Between $0.07-0.12: only if tomorrow is cloudy/high-demand.
  - Above $0.12: don't bother unless spike forecast for tomorrow.

═══════════════════════════════════════════════════════════════════
EVENING STRATEGY (17:00-22:00)
═══════════════════════════════════════════════════════════════════

  - Typical evening prices: $0.22-0.30/kWh (weekday), $0.15-0.20 (weekend).
  - Discharge is profitable above ~$0.08/kWh (wear + grid penalty).
  - Clearly worthwhile above $0.22/kWh (net savings $0.14+/kWh).
  - Sunset reward in LP incentivizes battery ≥80% by 17:00 on clear days.
  - MPC correctly switches hold→discharge when price rises above wear threshold.
  - On cheap Sunday evenings ($0.15), LP correctly holds — don't discharge.

═══════════════════════════════════════════════════════════════════
SPIKE HANDLING
═══════════════════════════════════════════════════════════════════

HA automation OVERRIDES MPC during spikes. Don't interfere.
  - Spike (>$1/kWh): R2901=100 (discharge to 10%).
  - Spike + FIT>10¢ + SoC>30%: R2706=70 (export at 7kW).
  - Negative buy price: R2901=1000 (charge to 100%), R2706=70 (export).
  - Spike export economics: FIT ~$0.60-0.80. Export 4kWh = ~$2.80 revenue.
    Post-spike recharge at $0.25 = $1.05 + $0.40 wear = net ~$1.35 profit.
  - LP pre-charges for predicted spikes (picks cheapest window 30h ahead).
  - 30% floor protects against double-spike surprise (2-3h emergency runway).

═══════════════════════════════════════════════════════════════════
COST HIERARCHY
═══════════════════════════════════════════════════════════════════

Battery discharge: $0.05/kWh (wear only)
Grid normal: $0.10-0.30/kWh (wholesale)
Grid charge threshold: $0.07/kWh ("free money" below this)
Discharge profitable above: $0.08/kWh (wear + grid penalty)
Discharge clearly worthwhile: $0.22+/kWh
Grid expensive: $0.30-0.50/kWh
Genset: ~$0.90/kWh (auto-start independent of MPC)
Grid spike: $1-25/kWh (HA automation handles these)

═══════════════════════════════════════════════════════════════════
YOUR ROLE AND RULES
═══════════════════════════════════════════════════════════════════

1. The LP handles math correctly. Only override for QUALITATIVE factors.
2. Adjustments are BOUNDED: max ±15% SoC. Only hold/discharge mode transitions.
3. Be CONSERVATIVE. Owner preference: "slightly on conservative side is better."
4. Default to agreeing with the LP (soc_adjustment_pct: 0, confidence: 0.5).
5. Only suggest changes when confident (>0.7).

WHEN TO ADJUST (with specific reasoning):
  - Overnight + grid price > $0.20 + LP holding → consider discharge (LP may be
    over-valuing hold reward). But respect 30% floor.
  - 14:00-17:00 + overcast + SoC < 50% + evening prices forecast $0.25+ →
    hold more (+5-10%). Solar won't fill the gap.
  - Weekday 06:00-07:00 + SoC < 50% → risk flag. Morning load will pull 1.3 kW
    for 5h before solar helps. May need grid top-up.
  - Weekend morning + LP forecasting high load → it's overestimating. Can be
    slightly less conservative.
  - Price forecast shows cheap overnight but it's a weeknight → don't trust it.
    Amber overnight forecasts are systematically too low.
  - Two AC units running + evening approaching + SoC < 60% → risk flag.
    4-6 kW sustained AC load will drain battery fast.

ASYMMETRIC RISK (critical — LP ignores this):
  The LP optimizes against the FORECAST, but Amber forecasts are unreliable
  (±50% evening, overnight $0.05-0.10 too low). Risk is asymmetric:
  - Cost of charging extra at $0.15: ~$0.05/kWh wear + small price premium.
  - Cost of NOT having battery during unexpected $0.30+ spike: $0.25+/kWh.
  RULE: When grid price is cheap (< $0.18) AND solar is underperforming AND
  battery is not full, bias toward charging MORE. The insurance premium is
  small ($0.05 wear) vs the potential spike cost. Top-up at cheap rates is
  almost always worth it on poor-solar days, even if the forecast shows flat
  prices. Push +10-15% SoC adjustment, confidence 0.85+.
  RULE: On overcast days with < 50% of expected solar yield by midday,
  recommend charging to 90%+ while grid is cheap. Tomorrow's solar is also
  uncertain — a full battery at sunset is worth the wear cost.

WHEN NOT TO ADJUST:
  - LP decision makes obvious sense for current conditions.
  - During active spike (HA automation handles these, NOT the LP).
  - Strong solar + high SoC + positive FIT → let it export.
  - Solar is strong and battery is charging toward evening → let it work.
  - You're not confident (< 0.7). When in doubt, trust the LP.

═══════════════════════════════════════════════════════════════════
RESPONSE FORMAT
═══════════════════════════════════════════════════════════════════

Respond with ONLY valid JSON, no other text:
{
  "soc_adjustment_pct": 0,
  "mode_override": null,
  "confidence": 0.5,
  "reasoning": "brief explanation (1-2 sentences max)",
  "risk_flags": []
}

soc_adjustment_pct: integer -15 to +15 (positive = hold more battery)
mode_override: null (keep LP) or "hold" or "discharge"
confidence: 0.0-1.0 (only applied if >= 0.7)
risk_flags: list of notable risks to surface (e.g., "AC likely tonight", "amber forecast unreliable")"""


# Conditions that are "interesting" enough to warrant an API call
_CALL_THRESHOLDS = {
    "price_change": 0.03,  # $/kWh change from last call
    "soc_change": 5.0,  # % SoC change
    "mode_change": True,  # LP changed mode
    "min_interval_s": 300,  # Don't call more than every 5 min
    "max_interval_s": 1800,  # Always call at least every 30 min
}


class Advisor:
    """Claude-powered real-time advisor for MPC decisions.

    Supports two backends:
      - OpenRouter (default): routes to Claude via OpenAI-compatible API
      - Anthropic: calls Claude Messages API directly

    All HTTP calls use a shared aiohttp.ClientSession.
    """

    def __init__(
        self,
        session: ClientSession,
        api_key: str,
        *,
        model: str | None = None,
        enabled: bool = True,
        backend: str = "openrouter",
    ):
        self.session = session
        self.backend = backend
        self.api_key = api_key

        if self.backend == "openrouter":
            self.model = model or "anthropic/claude-opus-4-6"
        else:
            self.model = model or "claude-haiku-4-5-20251001"

        self.enabled = enabled
        self._last_call_time: float = 0
        self._last_snapshot: AdvisorSnapshot | None = None
        self._last_adjustment: AdvisorAdjustment | None = None
        self._call_count: int = 0
        self._skip_count: int = 0

    def should_call(self, snapshot: AdvisorSnapshot) -> bool:
        """Decide if we need a fresh API call or can reuse last result."""
        if not self.enabled or not self.api_key:
            return False

        now = time.time()
        elapsed = now - self._last_call_time

        # Always call if enough time has passed
        if elapsed >= _CALL_THRESHOLDS["max_interval_s"]:
            return True

        # Don't call too frequently
        if elapsed < _CALL_THRESHOLDS["min_interval_s"]:
            return False

        # Call if conditions changed significantly
        if self._last_snapshot is None:
            return True

        prev = self._last_snapshot
        if abs(snapshot.buy_price - prev.buy_price) >= _CALL_THRESHOLDS["price_change"]:
            return True
        if abs(snapshot.battery_soc_pct - prev.battery_soc_pct) >= _CALL_THRESHOLDS["soc_change"]:
            return True
        if snapshot.lp_mode != prev.lp_mode:
            return True

        return False

    async def review(self, snapshot: AdvisorSnapshot) -> AdvisorAdjustment:
        """Review LP decision and return bounded adjustment.

        May skip API call if conditions haven't changed significantly,
        returning the previous adjustment.
        """
        if not self.enabled or not self.api_key:
            return AdvisorAdjustment(reasoning="Advisor disabled")

        if not self.should_call(snapshot):
            self._skip_count += 1
            if self._last_adjustment:
                log.debug(
                    "Advisor: reusing previous (skip #%d, %ds ago)",
                    self._skip_count,
                    int(time.time() - self._last_call_time),
                )
                return self._last_adjustment
            return AdvisorAdjustment(reasoning="No previous result, skipped")

        return await self._call_api(snapshot)

    async def _call_api(self, snapshot: AdvisorSnapshot) -> AdvisorAdjustment:
        """Make the API call via OpenRouter or Anthropic backend."""
        start = time.time()

        try:
            text = await self._raw_call(snapshot)
            elapsed_ms = (time.time() - start) * 1000
            self._last_call_time = time.time()
            self._last_snapshot = snapshot
            self._call_count += 1

            # Parse response — strip markdown wrapping if present
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            data = json.loads(text)

            adjustment = AdvisorAdjustment(
                soc_adjustment_pct=max(-15, min(15, float(data.get("soc_adjustment_pct", 0)))),
                mode_override=data.get("mode_override"),
                confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
                reasoning=str(data.get("reasoning", "")),
                risk_flags=data.get("risk_flags", []),
            )

            # Validate mode_override
            valid_modes = {"hold", "discharge", "grid_charge", "solar_charge", None}
            if adjustment.mode_override not in valid_modes:
                adjustment.mode_override = None

            self._last_adjustment = adjustment

            log.info(
                "Advisor: soc_adj=%+.0f%%, mode=%s, conf=%.2f (%dms, call #%d) — %s",
                adjustment.soc_adjustment_pct,
                adjustment.mode_override or "keep",
                adjustment.confidence,
                elapsed_ms,
                self._call_count,
                adjustment.reasoning[:80],
            )

            return adjustment

        except json.JSONDecodeError as e:
            log.warning("Advisor: failed to parse response: %s", e)
            return AdvisorAdjustment(reasoning=f"Parse error: {e}")
        except Exception as e:
            log.warning("Advisor: API call failed: %s", e)
            return AdvisorAdjustment(reasoning=f"API error: {e}")

    async def _raw_call(self, snapshot: AdvisorSnapshot) -> str:
        """Make the raw API call and return response text."""
        if self.backend == "openrouter":
            return await self._call_openrouter(snapshot)
        return await self._call_anthropic(snapshot)

    async def _call_openrouter(self, snapshot: AdvisorSnapshot) -> str:
        """Call Claude via OpenRouter (OpenAI-compatible API)."""
        async with self.session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 500,
                "messages": [
                    {"role": "system", "content": ADVISOR_SYSTEM_PROMPT},
                    {"role": "user", "content": snapshot.to_prompt()},
                ],
            },
            timeout=30,
        ) as response:
            response.raise_for_status()
            data = await response.json()
            return data["choices"][0]["message"]["content"].strip()

    async def _call_anthropic(self, snapshot: AdvisorSnapshot) -> str:
        """Call Claude via Anthropic Messages API directly."""
        async with self.session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 500,
                "system": ADVISOR_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": snapshot.to_prompt()}],
            },
            timeout=30,
        ) as response:
            response.raise_for_status()
            data = await response.json()
            return data["content"][0]["text"].strip()

    @property
    def stats(self) -> dict:
        """Return advisor usage stats."""
        return {
            "calls": self._call_count,
            "skips": self._skip_count,
            "enabled": self.enabled,
            "model": self.model,
            "backend": self.backend,
        }


def apply_adjustment(
    result: object,
    adjustment: AdvisorAdjustment,
    battery_capacity_kwh: float = 14.2,
    min_soc_pct: float = 20.0,
) -> tuple[object, dict]:
    """Apply advisor adjustment to LP result within safety bounds.

    Args:
        result: OptOutput from optimizer.
        adjustment: From advisor.review().
        battery_capacity_kwh: For SoC clamping.
        min_soc_pct: Absolute minimum SoC.

    Returns:
        (modified_result, changes_dict) — changes_dict logs what was modified.
    """
    changes = {
        "advisor_confidence": adjustment.confidence,
        "advisor_reasoning": adjustment.reasoning,
        "advisor_risk_flags": adjustment.risk_flags,
        "advisor_soc_adj": 0.0,
        "advisor_mode_override": None,
        "advisor_applied": False,
    }

    # Only apply if confidence >= 0.7
    if adjustment.confidence < 0.7:
        log.debug("Advisor: confidence %.2f < 0.7, keeping LP decision", adjustment.confidence)
        return result, changes

    original_soc = result.target_soc_pct
    original_mode = result.mode
    applied = False

    # Apply SoC adjustment
    if adjustment.soc_adjustment_pct != 0:
        new_soc = result.target_soc_pct + adjustment.soc_adjustment_pct
        new_soc = max(min_soc_pct, min(100.0, new_soc))

        if new_soc != result.target_soc_pct:
            result.target_soc_pct = new_soc
            result.target_register = int(round(new_soc * 10))
            # Clamp register to valid range
            result.target_register = max(100, min(1000, result.target_register))
            changes["advisor_soc_adj"] = adjustment.soc_adjustment_pct
            applied = True
            log.info(
                "Advisor applied: SoC %.0f%% -> %.0f%% (%+.0f%%)",
                original_soc,
                new_soc,
                adjustment.soc_adjustment_pct,
            )

    # Apply mode override (more restrictive — only hold/discharge transitions)
    if adjustment.mode_override and adjustment.mode_override != result.mode:
        # Only allow safe transitions
        safe_transitions = {
            ("discharge", "hold"),  # Hold instead of discharge (conservative)
            ("hold", "discharge"),  # Release instead of hold (aggressive)
        }
        transition = (result.mode, adjustment.mode_override)
        if transition in safe_transitions:
            result.mode = adjustment.mode_override
            changes["advisor_mode_override"] = adjustment.mode_override
            applied = True
            log.info(
                "Advisor applied: mode %s -> %s",
                original_mode,
                adjustment.mode_override,
            )
        else:
            log.debug(
                "Advisor: mode override %s -> %s not in safe transitions, skipped",
                result.mode,
                adjustment.mode_override,
            )

    # Update reason if modified
    if applied:
        result.reason = f"{result.reason} [Advisor: {adjustment.reasoning[:60]}]"

    changes["advisor_applied"] = applied
    return result, changes


def build_snapshot(
    result: object,
    forecasts: dict,
    config: object,
    conditions: dict | None = None,
) -> AdvisorSnapshot:
    """Build an AdvisorSnapshot from available MPC data.

    Args:
        result: OptOutput from optimizer.
        forecasts: Dict from ForecastBuilder.build_all().
        config: MPCConfig.
        conditions: Optional conditions dict from _capture_conditions().
    """
    now = datetime.now()
    buy_prices = forecasts.get("buy_price", [0.25])

    # Price trend: compare current vs 1h ahead
    current_price = buy_prices[0] if buy_prices else 0.25
    price_1h = buy_prices[min(12, len(buy_prices) - 1)] if len(buy_prices) > 1 else current_price
    if price_1h > current_price + 0.02:
        price_trend = "rising"
    elif price_1h < current_price - 0.02:
        price_trend = "falling"
    else:
        price_trend = "stable"

    # SoC trajectory from result
    traj = result.soc_trajectory_pct if hasattr(result, "soc_trajectory_pct") else []
    sph = 12  # steps per hour

    return AdvisorSnapshot(
        battery_soc_pct=forecasts.get("battery_soc_pct", 50),
        current_solar_w=forecasts.get("current_solar_w", 0),
        current_load_w=forecasts.get("current_load_w", 1000),
        solar_yield_today_kwh=conditions.get("solar_yield_today_kwh", 0) if conditions else 0,
        buy_price=current_price,
        sell_price=forecasts.get("sell_price", [0])[0] if forecasts.get("sell_price") else 0,
        is_spike=False,  # Caller should set this from amber data
        price_trend_1h=price_trend,
        lp_mode=result.mode,
        lp_target_soc_pct=result.target_soc_pct,
        lp_reason=result.reason,
        lp_cost_24h=result.total_cost,
        solar_forecast_kwh=conditions.get("vrm_solar_forecast_today_kwh", 0) if conditions else 0,
        solar_derate=forecasts.get("solar_derate", 1.0),
        solar_intraday_correction=forecasts.get("solar_intraday_correction", 1.0),
        cloud_coverage_pct=conditions.get("cloud_coverage_pct", 0) if conditions else 0,
        weather=conditions.get("weather_condition", "unknown") if conditions else "unknown",
        hour=now.hour,
        minute=now.minute,
        day_of_week=now.strftime("%A"),
        month=now.strftime("%B"),
        soc_1h=traj[min(sph, len(traj) - 1)] if traj else result.target_soc_pct,
        soc_2h=traj[min(2 * sph, len(traj) - 1)] if traj else result.target_soc_pct,
        soc_3h=traj[min(3 * sph, len(traj) - 1)] if traj else result.target_soc_pct,
        soc_4h=traj[min(4 * sph, len(traj) - 1)] if traj else result.target_soc_pct,
        price_mae_recent=conditions.get("price_mae", 0) if conditions else 0,
        solar_error_pct=conditions.get("solar_error_pct", 0) if conditions else 0,
    )
