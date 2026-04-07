"""GenAI health monitor for Victron MPC Battery Optimizer.

Two-layer architecture:

Layer 1 (Deterministic): Pure Python checks every 5-min cycle. No LLM.
    Catches all RED conditions — register mismatches, grid anomalies,
    rogue processes, safety violations.

Layer 2 (Strategic GenAI): Hourly LLM call with strategic context only.
    Reviews mode, SoC trajectory, pricing strategy. Never returns RED.
    Deterministic checks handle all critical issues.

Runs every 12th coordinator cycle (5 min x 12 = 60 min) for GenAI layer.
Deterministic checks run every cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Cycle counter -- GenAI runs every 12th cycle (hourly)
GENAI_CYCLE_INTERVAL = 12

# Prompt version — incremented each time the SYSTEM_PROMPT is significantly revised.
# History: v1 initial, v2 add normal-pattern calibration, v3 add false-positive guidance,
# v4 add phase-by-phase pattern library (current)
PROMPT_VERSION = "v4"


# ---------------------------------------------------------------------------
# Layer 1: Deterministic checks (every cycle, no LLM)
# ---------------------------------------------------------------------------

def _extract_fields(
    coordinator_data: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Extract fields from coordinator data, handling nested and flat formats.

    Production coordinator returns nested dicts (decision.state, decision.battery_soc_pct).
    Tests may pass flat dicts (mode, battery_soc_pct at top level).

    Returns:
        Dict with normalized field names for deterministic checks.
    """
    decision = coordinator_data.get("decision", {})
    if isinstance(decision, dict) and "state" in decision:
        mode = decision.get("state", "unknown")
        soc_pct = decision.get("battery_soc_pct", None)
        shadow_mode = decision.get("shadow_mode", False)
        grid_import_w = decision.get("grid_import_w", extra.get("grid_import_w", 0))
        schedule = decision.get("schedule_30min", "")
    else:
        mode = coordinator_data.get("mode", "unknown")
        soc_pct = coordinator_data.get("battery_soc_pct", None)
        shadow_mode = coordinator_data.get("shadow_mode", False)
        grid_import_w = extra.get("grid_import_w", coordinator_data.get("grid_import_w", 0))
        schedule = coordinator_data.get("schedule_30min", "")

    # Buy price (may be nested dict or scalar)
    buy_price_raw = coordinator_data.get("buy_price", None)
    if isinstance(buy_price_raw, dict):
        buy_price = buy_price_raw.get("state", None)
    else:
        buy_price = buy_price_raw

    # Solar forecast (may be nested dict or scalar)
    solar_raw = coordinator_data.get("solar_forecast_today", None)
    if isinstance(solar_raw, dict):
        solar_forecast = solar_raw.get("state", None)
    else:
        solar_forecast = solar_raw

    return {
        "mode": mode,
        "soc_pct": soc_pct,
        "shadow_mode": shadow_mode,
        "grid_import_w": grid_import_w,
        "schedule": schedule,
        "buy_price": buy_price,
        "solar_forecast": solar_forecast,
        "r2900": extra.get("r2900", -1),
        "r2901_readback_pct": extra.get("r2901_readback_pct", -1),
        "mac_runner_found": extra.get("mac_runner_found", False),
        "yaml_automations_on": extra.get("yaml_automations_on", []),
        "amber_band": extra.get("amber_band", ""),
    }


def run_deterministic_checks(
    coordinator_data: dict[str, Any],
    extra: dict[str, Any],
) -> list[dict[str, str]]:
    """Run deterministic health checks every cycle.

    Returns a list of RED findings. Empty list = all checks passed.
    Each finding: {"check": str, "status": "RED", "reason": str}.

    Args:
        coordinator_data: The coordinator's current sensor data dict.
        extra: Additional readings (R2900, R2901 readback, etc.)

    Returns:
        List of RED check results. Empty = healthy.
    """
    results: list[dict[str, str]] = []
    fields = _extract_fields(coordinator_data, extra)

    # 1. R2900 not in (10, 12) and not -1 (unavailable)
    try:
        r2900 = fields["r2900"]
        if r2900 != -1 and r2900 not in (10, 11, 12):
            results.append({
                "check": "r2900_ess_mode",
                "status": "RED",
                "reason": (
                    f"R2900 ESS mode is {r2900} (expected 10, 11, or 12). "
                    f"BatteryLife may be overriding MPC."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check r2900 failed: %s", exc)

    # 2. R2901 readback >= SoC during non-grid-charge mode
    try:
        r2901 = fields["r2901_readback_pct"]
        soc = fields["soc_pct"]
        mode = fields["mode"]
        if (
            r2901 is not None
            and r2901 != -1
            and soc is not None
            and mode not in ("grid_charge", "unknown")
            and r2901 > soc
        ):
            results.append({
                "check": "r2901_above_soc",
                "status": "RED",
                "reason": (
                    f"R2901 readback ({r2901}%) > SoC ({soc}%) during "
                    f"{mode} mode. ESS is unintentionally grid-charging."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check r2901 failed: %s", exc)

    # 3. Grid import > 200W during discharge
    try:
        grid_w = fields["grid_import_w"]
        mode = fields["mode"]
        soc_for_grid_check = fields.get("soc_pct")
        near_floor = soc_for_grid_check is not None and soc_for_grid_check <= 33
        if mode == "discharge" and grid_w is not None and grid_w > 200 and not near_floor:
            results.append({
                "check": "grid_import_during_discharge",
                "status": "RED",
                "reason": (
                    f"Grid importing {grid_w}W during discharge mode. "
                    f"Battery should be serving load."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check grid_import failed: %s", exc)

    # 4. Mac runner found
    try:
        if fields["mac_runner_found"]:
            results.append({
                "check": "mac_runner_active",
                "status": "RED",
                "reason": (
                    "Old Mac runner process detected. Two processes writing "
                    "registers will cause conflicts."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check mac_runner failed: %s", exc)

    # 5. YAML automations ON
    try:
        yaml_on = fields["yaml_automations_on"]
        if yaml_on:
            results.append({
                "check": "yaml_automations_on",
                "status": "RED",
                "reason": (
                    f"YAML automations are ON: {yaml_on}. "
                    f"These override HACS register writes."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check yaml_automations failed: %s", exc)

    # 6. Shadow mode True
    try:
        if fields["shadow_mode"]:
            results.append({
                "check": "shadow_mode_active",
                "status": "RED",
                "reason": (
                    "Shadow mode is active. Registers are NOT being written. "
                    "System is running open-loop."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check shadow_mode failed: %s", exc)

    # 7. SoC < 30% during overnight hours (22:00-06:00)
    try:
        soc = fields["soc_pct"]
        now = datetime.now()
        is_overnight = now.hour >= 22 or now.hour < 6
        if is_overnight and soc is not None and soc < 30:
            results.append({
                "check": "soc_below_floor_overnight",
                "status": "RED",
                "reason": (
                    f"SoC is {soc}% during overnight hours "
                    f"({now.strftime('%H:%M')}). Below 30% floor."
                ),
            })
    except Exception as exc:
        _LOGGER.warning("Deterministic check overnight_soc failed: %s", exc)

    return results


# ---------------------------------------------------------------------------
# Layer 2: Strategic GenAI (hourly, only when deterministic GREEN)
# ---------------------------------------------------------------------------

def build_strategic_snapshot(
    coordinator_data: dict[str, Any],
    extra: dict[str, Any],
) -> str:
    """Build a strategic-only snapshot for the GenAI prompt.

    Includes ONLY strategic information: mode, SoC, prices, trajectory.
    MUST NOT include registers, power flows, or operational details.

    Args:
        coordinator_data: The coordinator's current sensor data dict.
        extra: Additional readings.

    Returns:
        Formatted string with strategic context only.
    """
    fields = _extract_fields(coordinator_data, extra)

    # Solar forecast remaining today
    solar_forecast = fields.get("solar_forecast", "?")

    # Build trajectory summary from schedule_30min
    schedule = fields.get("schedule", "")
    trajectory_str = ""
    if schedule and schedule != "":
        trajectory_str = str(schedule)[:400]

    now = datetime.now()

    lines = [
        f"Time: {now.strftime('%H:%M')} ({now.strftime('%A')})",
        f"Mode: {fields['mode']}",
        f"SoC: {fields['soc_pct']}%",
        f"Buy Price: ${fields['buy_price']}/kWh" if fields["buy_price"] is not None else "Buy Price: ?",
        f"Amber Band: {fields['amber_band']}" if fields["amber_band"] else "Amber Band: ?",
        f"Solar Forecast Remaining Today: {solar_forecast} kWh",
    ]

    if trajectory_str:
        lines.append(f"Planned trajectory (next 8h): {trajectory_str}")

    return "\n".join(lines)


# Keep old build_health_snapshot for backward compatibility
def build_health_snapshot(
    coordinator_data: dict[str, Any],
    extra: dict[str, Any],
) -> str:
    """Build a concise data snapshot for the GenAI prompt.

    DEPRECATED: Use build_strategic_snapshot for new code.
    Kept for backward compatibility with existing tests.

    Args:
        coordinator_data: The coordinator's current sensor data dict.
        extra: Additional readings (R2900, R2901 readback, R37 setpoint, etc.)

    Returns:
        Formatted string snapshot for the prompt.
    """
    # Extract from nested coordinator data structure
    decision = coordinator_data.get("decision", {})
    if isinstance(decision, dict) and "state" in decision:
        # Full nested dict from _build_sensor_data
        mode = decision.get("state", "unknown")
        soc_pct = decision.get("battery_soc_pct", "?")
        target_register = decision.get("target_register", "?")
        spike = decision.get("spike", False)
        shadow_mode = decision.get("shadow_mode", "?")
        schedule = decision.get("schedule_30min", "")
        soc_1h = decision.get("soc_1h_pct", "?")
        soc_2h = decision.get("soc_2h_pct", "?")
        grid_import_w = decision.get("grid_import_w", extra.get("grid_import_w", "?"))
    else:
        # Flat dict (from test scenarios)
        mode = coordinator_data.get("mode", "unknown")
        soc_pct = coordinator_data.get("battery_soc_pct", "?")
        target_register = coordinator_data.get("target_register", "?")
        spike = coordinator_data.get("spike", False)
        shadow_mode = coordinator_data.get("shadow_mode", "?")
        schedule = coordinator_data.get("schedule_30min", "")
        soc_1h = coordinator_data.get("soc_1h_pct", "?")
        soc_2h = coordinator_data.get("soc_2h_pct", "?")
        grid_import_w = extra.get("grid_import_w", "?")

    # Extract buy/sell price (may be nested dict or scalar)
    buy_price_raw = coordinator_data.get("buy_price", "?")
    buy_price = buy_price_raw.get("state", buy_price_raw) if isinstance(buy_price_raw, dict) else buy_price_raw
    sell_price_raw = coordinator_data.get("sell_price", "?")
    sell_price = sell_price_raw.get("state", sell_price_raw) if isinstance(sell_price_raw, dict) else sell_price_raw

    # Extract cloud (may be nested)
    cloud_raw = coordinator_data.get("cloud_coverage", "?")
    cloud_pct = cloud_raw.get("state", cloud_raw) if isinstance(cloud_raw, dict) else cloud_raw

    # Extract solar forecast (may be nested)
    solar_raw = coordinator_data.get("solar_forecast_today", "?")
    solar_forecast = solar_raw.get("state", solar_raw) if isinstance(solar_raw, dict) else solar_raw

    # Extract solar/load input (scalar)
    solar_w = coordinator_data.get("solar_input_w", 0)
    load_w = coordinator_data.get("load_input_w", "?")

    # Extract battery plan for feedin register
    battery_plan = coordinator_data.get("battery_plan", {})
    feedin_register = battery_plan.get("feedin_register", "?") if isinstance(battery_plan, dict) else coordinator_data.get("feedin_register", "?")

    lines = [
        f"Timestamp: {datetime.now().isoformat()}",
        f"Mode: {mode}",
        f"SoC: {soc_pct}%",
        f"Target Register (R2901 written): {target_register}",
        f"R2901 Readback: {extra.get('r2901_readback_pct', '?')}%",
        f"R2900 (ESS Mode): {extra.get('r2900', '?')} (should be 10 or 12)",
        f"R37 Power Setpoint: {extra.get('r37_setpoint_w', '?')}W",
        f"Battery Power: {extra.get('battery_power_w', '?')}W (negative=discharge)",
        f"Grid Import: {grid_import_w}W",
        f"Grid Export: {extra.get('grid_export_w', 0)}W",
        f"Solar: {solar_w}W",
        f"Load: {load_w}W",
        f"Buy Price: ${buy_price}/kWh",
        f"Sell Price: ${sell_price}/kWh",
        f"Cloud: {cloud_pct}%",
        f"Weather: {extra.get('weather', '?')}",
        f"Solar Forecast Today: {solar_forecast} kWh",
        f"Solar Yield So Far: {extra.get('solar_yield_kwh', '?')} kWh",
        f"Spike: {spike}",
        f"Shadow Mode: {shadow_mode}",
        f"Amber Band: {extra.get('amber_band', '?')}",
        f"Mac Runner Found: {extra.get('mac_runner_found', False)}",
        f"YAML Automations ON: {extra.get('yaml_automations_on', [])}",
        f"Feedin Register (R2706): {feedin_register}",
        f"Hours Since Full Charge: {extra.get('hours_since_full_charge', '?')}",
    ]

    # Add trajectory if available
    if schedule and schedule != "":
        lines.append(f"Planned trajectory (next 8h): {schedule[:500]}")

    # Add SoC lookahead
    if soc_1h != "?":
        lines.append(f"SoC in 1h: {soc_1h}%")
    if soc_2h != "?":
        lines.append(f"SoC in 2h: {soc_2h}%")

    # Add solar forecast hourly from decision attributes
    for key in ("forecast_1h_w", "forecast_2h_w", "forecast_3h_w", "forecast_4h_w"):
        val = decision.get(key) if isinstance(decision, dict) else coordinator_data.get(key)
        if val is not None:
            lines.append(f"Solar {key}: {val}W")

    return "\n".join(lines)


SYSTEM_PROMPT = """\
You are a strategic advisor for a home battery system with Victron ESS \
and Amber Electric wholesale pricing.

All operational checks have PASSED — registers, power flows, and safety conditions \
are verified by deterministic monitors. Your job is ONLY strategic review.

## NORMAL DAILY PATTERN (calibrated from production data)

These are EXPECTED behaviors — do NOT flag as YELLOW:

Phase 1 — Overnight (22:00-06:00): SoC drifts from 40-55% down to 30-35%. \
Grid import 400-800W is NORMAL (battery at floor, grid serves house load). \
Mode: hold or gentle discharge. This is not an emergency.

Phase 2 — Shaded Morning (06:00-11:00): SoC 30-38%, solar 0-500W (trees block panels). \
Grid import 600-1200W is NORMAL. Battery may discharge slightly to supplement solar. \
Low solar before 11am is physical shading, not a forecast error.

Phase 3 — Solar Ramp (11:00-13:00): Solar breaks through shade, 500W-4000W. \
SoC climbs from 35% toward 60%+. Mode switches to solar_charge. \
Grid import drops to near zero. This ramp happens every day.

Phase 4 — Peak Solar (13:00-16:00): Solar 3000-5000W, battery charges at 2-4kW. \
SoC climbs to 80-100%. On cloudy days, may only reach 70-80% — this is fine.

Phase 5 — Evening Peak (17:00-21:00): TYPICALLY battery discharges at $0.20-0.30/kWh. \
SoC drops from 80-100% toward 35-50%. Grid import near zero. \
HOWEVER: if Amber band is very_low or extremely_low, grid_charge is correct — \
the LP buys cheap grid now to avoid higher prices overnight. Do NOT flag this.

Phase 6 — Late Evening (21:00-22:00): Prices drop, discharge slows. \
SoC may drop to 30-40% before overnight hold kicks in.

## CRITICAL: Amber band overrides time-of-day assumptions

Wholesale pricing is volatile — the Amber Band in the snapshot ALWAYS takes precedence \
over typical time-of-day patterns above. \
extremely_low/very_low at ANY hour: grid_charge is rational (grid_charge_boost active). \
high/spike at ANY hour: discharge is expected. Grid_charge during high/spike IS a concern.

## WHEN TO FLAG YELLOW (genuine strategic concerns only)

- Sunset target at risk: SoC < 70% after 15:00 with declining solar AND cloudy forecast
- Unusual price pattern: spike predicted but battery is low, or extremely_low not being exploited
- Multi-day trend: consecutive days of not reaching 95% by sunset suggests tuning issue
- Load anomaly: sustained 3kW+ load (AC) that the trajectory doesn't account for

## WHEN TO STAY GREEN

- SoC at 30-35% overnight or during morning shading — this is the designed floor
- Grid import during hold/floor — battery can't serve load at floor, grid fills the gap
- Solar < 500W before 11am — trees, not clouds
- SoC only reaching 75% on a cloudy day — system adapts, not a failure
- Gentle overnight SoC decline — inverter efficiency losses, normal physics
- Grid_charge during evening when Amber band is very_low or extremely_low — LP is buying cheap

Your default answer is GREEN. The system is well-tuned and operates correctly \
the vast majority of the time. Return GREEN unless you can identify something \
clearly wrong that does not match ANY of the normal phases above.

YELLOW means: "this specific condition will cause meaningful financial loss or \
miss a critical target if not addressed in the next 1-2 hours." A vague concern \
or theoretical risk is not YELLOW — it must be concrete and imminent. If in doubt, \
return GREEN.

Keep it brief — one sentence for GREEN, 2-3 sentences only for YELLOW.
Never return RED — deterministic checks handle critical issues.

Respond in JSON only:
{"status": "GREEN|YELLOW", "summary": "one line", "details": "explanation if YELLOW"}\
"""


async def run_genai_health_check(
    session,  # aiohttp ClientSession
    api_key: str,
    snapshot: str,
) -> dict[str, str]:
    """Call OpenRouter API to analyze system health.

    Args:
        session: aiohttp ClientSession for HTTP requests.
        api_key: OpenRouter API key.
        snapshot: Formatted data snapshot string.

    Returns:
        Dict with keys: status, summary, details.
    """
    if not api_key:
        return {"status": "SKIP", "summary": "No OpenRouter API key configured", "details": ""}

    text = ""
    try:
        payload = {
            "model": "anthropic/claude-haiku-4.5",
            "max_tokens": 800,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Current system snapshot:\n\n{snapshot}"},
            ],
        }

        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            timeout=60,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                _LOGGER.warning("GenAI health check API error %d: %s", resp.status, body[:200])
                return {
                    "status": "ERROR",
                    "summary": f"API error {resp.status}",
                    "details": body[:200],
                }

            data = await resp.json()
            text = data["choices"][0]["message"]["content"]
            _LOGGER.info("GenAI raw response (%d chars): %s", len(text), repr(text[:500]))

            # Parse JSON response — strip markdown code blocks if present
            clean = text.strip()
            if clean.startswith("```"):
                # Remove opening ``` or ```json line
                clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()
            # Also try extracting JSON object directly if still wrapped
            if not clean.startswith("{"):
                import re
                match = re.search(r'\{.*\}', clean, re.DOTALL)
                if match:
                    clean = match.group(0)

            result = json.loads(clean)

            parsed = {
                "status": result.get("status", "UNKNOWN"),
                "summary": result.get("summary", ""),
                "details": result.get("details", ""),
            }

            # GenAI layer cannot produce RED — downgrade to YELLOW
            if parsed["status"] == "RED":
                _LOGGER.info(
                    "GenAI returned RED — downgrading to YELLOW "
                    "(deterministic checks handle RED conditions)"
                )
                parsed["status"] = "YELLOW"
                parsed["details"] = (
                    f"[Downgraded from RED] {parsed['details']}"
                )

            return parsed

    except json.JSONDecodeError:
        # Response may be truncated by max_tokens — try to extract status and summary
        import re as _re
        status_m = _re.search(r'"status"\s*:\s*"(GREEN|YELLOW|RED)"', text)
        summary_m = _re.search(r'"summary"\s*:\s*"([^"]+)"', text)
        if status_m:
            _LOGGER.info(
                "GenAI: recovered truncated response: %s",
                status_m.group(1),
            )
            status = status_m.group(1)
            # Downgrade RED from truncated response too
            if status == "RED":
                status = "YELLOW"
            return {
                "status": status,
                "summary": summary_m.group(1) if summary_m else "(truncated)",
                "details": "(response truncated by token limit)",
            }
        _LOGGER.warning(
            "GenAI health check returned non-JSON: %s",
            text[:200] if text else "empty",
        )
        return {"status": "ERROR", "summary": "Non-JSON response from API", "details": text[:200] if text else ""}
    except Exception as exc:
        _LOGGER.warning("GenAI health check failed: %s", exc)
        return {"status": "ERROR", "summary": str(exc), "details": ""}
