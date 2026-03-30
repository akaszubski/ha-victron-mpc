"""GenAI health monitor for Victron MPC Battery Optimizer.

Calls the OpenRouter API hourly to reason about system health.
Catches subtle issues that deterministic checks miss -- the kind of
problems a human would notice by looking at the numbers and thinking
"this doesn't smell right."

Runs every 12th coordinator cycle (5 min x 12 = 60 min).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Cycle counter -- run every 12th cycle (hourly)
GENAI_CYCLE_INTERVAL = 12


def build_health_snapshot(
    coordinator_data: dict[str, Any],
    extra: dict[str, Any],
) -> str:
    """Build a concise data snapshot for the GenAI prompt.

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
You are monitoring a home battery system. Your job is to assess whether the system is \
achieving its BUSINESS GOALS — not just checking parameters, but reasoning about whether \
the overall strategy makes sense.

## System: Victron Quattro 48/8000 + Pylontech 14.2kWh, Amber Electric wholesale, \
7kW solar (shaded by trees until ~11am).

## BUSINESS GOALS (in priority order)

1. MINIMISE COST: Use the cheapest energy source at every moment. Discharge battery \
   when grid is expensive, charge when grid is cheap, use solar whenever available. \
   Every watt from grid during discharge mode is money wasted.

2. NEVER BE CAUGHT EXPOSED: Always have battery reserve for the unexpected — price \
   spikes come with 5 min notice, load can double when appliances turn on, solar can \
   disappear behind clouds. Being at floor with no solar is the worst position. \
   Conservative is better than optimal-on-paper.

3. FULL BY SUNSET: Battery must reach 95%+ before sunset to cover the expensive evening \
   peak (5-9pm, $0.27-0.30/kWh). Missing sunset target means buying peak power from grid.

4. EXPLOIT PRICE DIFFERENCES: Charge when grid is cheap relative to the day's average, \
   discharge when expensive. Export during spikes when feed-in tariff exceeds battery wear + \
   recharge cost. Charge when paid to consume (negative pricing). The LP handles the math — \
   the GenAI checks whether the strategy direction makes sense.

5. PROTECT THE BATTERY: 30% overnight floor (emergency reserve + genset start buffer). \
   Cell balancing full charge every 14 days. Don't cycle unnecessarily — wear cost is real.

6. AUTONOMOUS OPERATION: System should run without human intervention. If something goes \
   wrong (register override, stale data, API failure), it should self-correct or alert. \
   No silent failures.

## HOW WE ACHIEVE THIS (implementation checks)
- R2900 (ESS BatteryLife State): MUST be 10 or 12 (BL disabled). \
  If 2 = BatteryLife active (overriding MPC). If 9 = Keep Charged (grid charging at max rate). \
  Either is CRITICAL RED.
- R2901 (ESS Min SoC): ENCODING: register value = SoC% x 10 (e.g., 290 = 29.0%, 300 = 30%, \
  800 = 80%, 1000 = 100%). The "Target Register (R2901 written)" field shows the raw register \
  value; "R2901 Readback" shows the percentage. To compare: divide written value by 10, then \
  compare to readback %. Readback should be close to target register. The register changes \
  every 5-min cycle and the Modbus sensor polls independently, so 3-5% timing jitter is NORMAL. \
  Only flag if: (a) readback is >10% different from target AND persists, or (b) readback is \
  ABOVE current SoC during non-grid-charge mode (this is the dangerous condition — it causes \
  grid charging). Small mismatches during normal discharge are timing noise, not overrides.
- R2901 must be BELOW SoC during discharge/hold/solar_charge. \
  If R2901 readback >= SoC and mode is NOT grid_charge, system is grid-charging unintentionally. \
  THIS is the critical register check — not small mismatches between written and readback values. RED.
- R2700 (Grid Setpoint): should be ~50W. If 0, ESS oscillates into small exports. YELLOW.
- R37 (Power Setpoint): should be ~50W during discharge. If >200W during discharge, ESS is \
  importing from grid instead of using battery. RED.

## Mode Rules
- discharge: battery power should be negative (discharging), grid import <100W (50W setpoint + noise)
- solar_charge: R2901 at hard floor (~30%), battery charging from solar, grid import <100W
- grid_charge: R2901 ABOVE SoC, grid import expected, battery charging
- hold: battery near zero, grid serves load

## Power Flow
- Power balance: solar + grid_import + battery_discharge ≈ load + battery_charge + grid_export + losses (~50-100W)
- If balance is off by >500W, a sensor may be stale or wrong. YELLOW.
- Grid export should be 0W during discharge mode (R2706=0 blocks export)
- If grid export >50W during non-export mode, feed-in register may be wrong. YELLOW.

## Price & Cost Efficiency
- Use AMBER BANDS as the primary price signal — they are seasonally adjusted and reflect \
  where the current price sits relative to the market. Bands: extremely_low, very_low, low, \
  neutral, high, spike.
- extremely_low / very_low: should be charging (grid_charge_boost kicks in). If NOT charging \
  during extremely_low, opportunity is being missed. YELLOW.
- low: hold or gentle discharge is fine. Charging is sensible if evening is forecast expensive.
- neutral: discharge from battery is normal — saving vs buying later.
- high: discharge expected — battery is saving significant money.
- spike (>$1/kWh): aggressive discharge + export if FIT profitable. If NOT discharging, RED.
- negative (<$0): must be grid-charging (paid to consume). If not charging, RED.
- In winter, ALL bands shift up. Charging at "low" ($0.25) to survive "high" ($0.50) evening \
  is correct — don't flag this. The bands handle the seasonal adjustment.
- The LP optimizes across 24h — it sees future prices. The GenAI checks whether the strategy \
  direction aligns with the band, not the absolute dollar amount.

## Solar Insurance (Shading Gap)
- Solar is shaded by trees until ~11am. During 09:00-11:30 with low solar (<300W):
  - SoC should NOT be at floor (30%). Insurance should hold it at ~40-50%.
  - If SoC is at 30% floor during shading gap with no solar, insurance may not be working. YELLOW.
- After 11am with clear sky: solar should be >2kW and battery should be charging
- Solar forecast vs yield: by 2pm, yield should be >40% of forecast. If <30%, forecast may be wrong. YELLOW.

## System Health
- Battery max charge/discharge: 7.1kW. If battery power >7100W, sensor error. YELLOW.
- Inverter: 8kVA continuous. Load >8000W is concerning.
- SoC should track planned trajectory within ~10%. Larger deviation means forecast was wrong or \
  something overrode the plan. YELLOW if >15% off.

## Sunset Constraint
- SoC must reach 95% by sunset. If SoC <90% within 2 hours of sunset and not charging, YELLOW.
- After sunset, battery should be near 100% for evening peak discharge ($0.27-0.30).
- Evening peak is 5-9pm. LP should hold battery at 100% until ~8pm, then discharge.

## Overnight Preservation
- 30% hard floor overnight (22:00-06:00). SoC should not drop below 30% overnight.
- Overnight hold reward keeps battery from draining during cheap overnight ($0.15-0.20).
- If SoC drops below 35% overnight, overnight hold may be misconfigured. YELLOW.

## Cell Balancing
- Force full charge (100%) every 14 days for Pylontech LFP cell balancing.
- If SoC hasn't reached 100% in the last 14 days, balancing is overdue. YELLOW.

## Spike Handling
- Amber spike (>$1/kWh): LP should discharge aggressively, R2901 at hard floor (10%).
- During spike with FIT >$0.10 and SoC >30%: R2706=70 (export for profit).
- Post-spike: LP should plan recharge at cheapest available price.
- Negative pricing (<$0): should grid-charge (paid to consume). R2901 should be high, R2706=70.

## Amber Band Logic
- extremely_low/very_low bands: grid_charge_boost added to SoC reward — encourages charging. \
  The boost is a tunable that adapts with the SoC profile.
- low band: half boost applied.
- These boosts work WITH the LP's 24h optimization, not against it.

## SoC Profile Economics
- Pre-peak reward ($0.20) must exceed grid cost ($0.15 + $0.02 penalty = $0.17) for LP to grid-charge.
- If reward < grid cost, LP will never charge from grid during that period.
- Morning reward ($0.10) values battery during 6-9am peak.
- Overnight reward ($0.03) is intentionally low — discharge at moderate prices is fine.

## AC / Load Spikes
- AC units draw 2-3kW each. Hot afternoon/evening can add 4-6kW sustained.
- Load forecast may underestimate on hot days. If actual load >150% of forecast, flag. YELLOW.
- Hair dryer, oven, dishwasher: transient 1-3kW spikes are normal, not a concern.

## Known Threats
- Old Mac runner process: if snapshot mentions mac_runner_found=true, this is CRITICAL RED. \
  The old process writes conflicting register values and was killed 2026-03-30.
- BatteryLife revival: R2900 reverting from 10/12 to 2 after Cerbo reboot. CRITICAL RED.
- Duplicate register writers: if R2901 readback oscillates between two values every 5 min, \
  two processes are fighting. RED.
- YAML automations: all 12 automation.mpc_* must be OFF. They re-enable on HA restart. \
  If any are ON, they will override HACS register writes. RED.

Respond in JSON only:
{"status": "GREEN|YELLOW|RED", "summary": "one line", "details": "explanation if YELLOW or RED, empty if GREEN"}\
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
            return {
                "status": result.get("status", "UNKNOWN"),
                "summary": result.get("summary", ""),
                "details": result.get("details", ""),
            }

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
            return {
                "status": status_m.group(1),
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
