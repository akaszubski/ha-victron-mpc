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
PROMPT_VERSION = "v7"


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

    # Weather context — critical for understanding solar forecast reliability
    weather = extra.get("weather", "unknown")
    cloud_pct = coordinator_data.get("cloud_coverage", {})
    if isinstance(cloud_pct, dict):
        cloud_pct = cloud_pct.get("state", "?")
    weather_confidence = coordinator_data.get("decision", {})
    if isinstance(weather_confidence, dict):
        weather_confidence = weather_confidence.get("weather_confidence", "?")
    else:
        weather_confidence = "?"
    current_solar_w = coordinator_data.get("solar_input_w", "?")

    # LP structured intent — the optimizer's reasoning chain
    decision = coordinator_data.get("decision", {})
    if isinstance(decision, dict):
        intent = decision.get("intent", {})
        override_applied = decision.get("override_applied", False)
        override_reason = decision.get("override_reason", "")
    else:
        intent = {}
        override_applied = False
        override_reason = ""

    # Format intent for GenAI
    intent_lines = []
    if intent:
        intent_lines.append(f"LP Action: {intent.get('action', '?')}")
        intent_lines.append(f"LP Reasoning: {intent.get('why', '?')}")
        assumptions = intent.get("key_assumptions", {})
        if assumptions:
            intent_lines.append("LP Key Assumptions:")
            for k, v in assumptions.items():
                intent_lines.append(f"  {k}: {v}")
        outcomes = intent.get("expected_outcomes", {})
        if outcomes:
            intent_lines.append("LP Expected Outcomes:")
            for k, v in outcomes.items():
                if v is not None:
                    intent_lines.append(f"  {k}: {v}")
        constraints = intent.get("constraints_active", {})
        active = [k for k, v in constraints.items() if v]
        if active:
            intent_lines.append(f"Active Constraints: {', '.join(active)}")
    # Format principles for GenAI
    principles = intent.get("principles_active", [])
    if principles:
        intent_lines.append("Principles Assessment:")
        for p in principles:
            status = "\u2713" if p.get("satisfied") else "\u2717 UNSATISFIED"
            intent_lines.append(f"  [{status}] {p['id']}: {p.get('detail', '')}")

    if override_applied and override_reason:
        intent_lines.append(f"Override Active: {override_reason}")

    lines = [
        "== OBSERVED STATE ==",
        f"Time: {now.strftime('%H:%M')} ({now.strftime('%A')})",
        f"SoC: {fields['soc_pct']}%",
        f"Buy Price: ${fields['buy_price']}/kWh" if fields["buy_price"] is not None else "Buy Price: ?",
        f"Amber Band: {fields['amber_band']}" if fields["amber_band"] else "Amber Band: ?",
        f"Weather: {weather}",
        f"Cloud Coverage: {cloud_pct}%",
        f"Weather Confidence Factor: {weather_confidence} (1.0=clear, 0.5=rain)",
        f"Current Solar Output: {current_solar_w}W",
        f"Solar Forecast Remaining Today: {solar_forecast} kWh",
        "",
        "== LP STATED INTENT ==",
        *intent_lines,
    ]
    # Remove empty lines except the separator
    lines = [l for l in lines if l or l == ""]

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
You are an alignment auditor for a home battery LP optimizer (Victron ESS + Amber \
Electric wholesale pricing).

All operational checks have PASSED. Deterministic monitors handle safety. \
Your SOLE job is to validate that the LP optimizer's STATED INTENT aligns with \
OBSERVED REALITY.

## YOUR ROLE: Intent-Execution Alignment

The snapshot has two sections:
1. **OBSERVED STATE** — what sensors actually show (SoC, weather, solar, prices)
2. **LP STATED INTENT** — the optimizer's reasoning: what it chose, why, its \
   key assumptions, and expected outcomes

Your job: check whether the LP's stated assumptions match observed reality. \
If they align → GREEN. If they contradict → YELLOW.

## ALIGNMENT CHECKS (in priority order)

1. **Assumption check**: Do the LP's "key_assumptions" match observed state?
   - LP assumes solar_total_remaining_kwh=5.0 but weather is rainy, cloud 99%, \
     current solar only 200W → MISALIGNED (LP overestimates solar)
   - LP assumes buy_price_now=$0.15 and observed buy price is $0.15 → ALIGNED

2. **Action-condition check**: Does the chosen action make sense given conditions?
   - LP says grid_charge, weather is rainy, solar < 500W → ALIGNED \
     (grid_charge compensates for missing solar)
   - LP says solar_charge but current solar is 0W and cloud 100% → MISALIGNED
   - LP says discharge during spike → ALIGNED (selling expensive)

3. **Outcome plausibility**: Are expected outcomes achievable?
   - LP expects soc_at_sunset=95% but only 0.5 kWh solar remaining and SoC is 40% \
     and no grid_charge planned → MISALIGNED
   - LP expects soc_in_1h rising and it's grid_charging → ALIGNED

IMPORTANT: solar_next_1h_kwh is a FORECAST SUM of 12 five-minute steps with cloud \
derating. It will normally be LESS than current_solar_kw * 1h when solar is declining \
or clouds are forecast. This is expected behavior, NOT a misalignment.

## WHEN TO FLAG YELLOW

ONLY when you find a concrete misalignment:
- LP assumptions contradict observed sensor data
- LP action contradicts its own stated reasoning
- LP expected outcomes are physically impossible given constraints

## WHEN TO STAY GREEN

- LP intent is coherent with observed conditions — even if you'd choose differently
- You lack information to disprove an LP assumption — trust the LP
- The LP has 24h of price/solar/load forecasts you don't see — if its reasoning \
  is internally consistent with what you CAN see, it's GREEN

Your default is GREEN. The LP is well-tuned and correct the vast majority of the \
time. YELLOW only for concrete, provable misalignment between stated intent and \
observed reality.

## PRINCIPLE VALIDATION

The LP reports which operating principles are active and whether each is satisfied.
Your primary job is checking UNSATISFIED principles:

For each "✗ UNSATISFIED" principle:
1. Is the LP aware it's unsatisfied? (it should be — it reported it)
2. Is there a good reason? (e.g., sunset_readiness unsatisfied because grid price is \
$0.30 — too expensive to charge)
3. Does the tradeoff make sense given other principles? (e.g., sacrificing \
sunset_readiness to serve cost_minimisation at $0.30)

GREEN if:
- All principles satisfied, OR
- Unsatisfied principles have justified tradeoffs (cost vs readiness, etc.)

YELLOW if:
- A principle is unsatisfied AND the conditions don't justify it \
(e.g., sunset_readiness unsatisfied but grid is cheap at $0.10 — should be charging)
- The LP's tradeoff reasoning contradicts observed data

If GREEN: one sentence confirming alignment. \
If YELLOW: state which specific assumption or action is misaligned and why.

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

            # Ensure summary is never empty — helps debugging and audit
            if not parsed.get("summary"):
                parsed["summary"] = f"GenAI returned {parsed['status']} (no summary provided)"

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
