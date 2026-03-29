"""GenAI health monitor for Victron MPC Battery Optimizer.

Calls the Anthropic Claude API hourly to reason about system health.
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
    lines = [
        f"Timestamp: {datetime.now().isoformat()}",
        f"Mode: {coordinator_data.get('mode', 'unknown')}",
        f"SoC: {coordinator_data.get('battery_soc_pct', '?')}%",
        f"Target Register (R2901 written): {coordinator_data.get('target_register', '?')}",
        f"R2901 Readback: {extra.get('r2901_readback_pct', '?')}%",
        f"R2900 (ESS Mode): {extra.get('r2900', '?')} (should be 10 or 12)",
        f"R37 Power Setpoint: {extra.get('r37_setpoint_w', '?')}W",
        f"Battery Power: {coordinator_data.get('battery_power_w', '?')}W (negative=discharge)",
        f"Grid Import: {extra.get('grid_import_w', '?')}W",
        f"Grid Export: {extra.get('grid_export_w', 0)}W",
        f"Solar: {coordinator_data.get('solar_input_w', 0)}W",
        f"Load: {coordinator_data.get('load_input_w', '?')}W",
        f"Buy Price: ${coordinator_data.get('buy_price', '?')}/kWh",
        f"Sell Price: ${coordinator_data.get('sell_price', '?')}/kWh",
        f"Cloud: {coordinator_data.get('cloud_coverage', '?')}%",
        f"Weather: {extra.get('weather', '?')}",
        f"Solar Forecast Today: {coordinator_data.get('solar_forecast_today', '?')} kWh",
        f"Solar Yield So Far: {extra.get('solar_yield_kwh', '?')} kWh",
        f"Spike: {coordinator_data.get('spike', False)}",
        f"Shadow Mode: {coordinator_data.get('shadow_mode', '?')}",
    ]

    # Add trajectory if available
    schedule = coordinator_data.get("schedule_30min")
    if schedule:
        lines.append(f"Planned trajectory (next 8h): {schedule[:16]}")

    return "\n".join(lines)


SYSTEM_PROMPT = """\
You are a power systems analyst monitoring a home battery optimizer.
You are given a snapshot of the current state. Reason about whether the system is working correctly.

Key rules:
- During discharge mode: grid import should be <100W, battery should be negative (discharging)
- During solar_charge: register should be at floor (~30%), solar charges naturally
- During grid_charge: register should be ABOVE SoC to force grid charging
- R2900 should be 10 or 12 (BatteryLife disabled). If 2 or 9, CRITICAL.
- R2901 readback should match what was written. If oscillating, something is overriding.
- Power balance: solar + grid_import + battery_discharge ~ load + battery_charge + grid_export
- If buy price < $0.10, grid_charge may be sensible. If buy price > $0.30, discharge is expected.
- SoC should roughly track the planned trajectory (within ~10%)

Respond in JSON only:
{"status": "GREEN|YELLOW|RED", "summary": "one line", "details": "explanation if YELLOW or RED, empty if GREEN"}\
"""


async def run_genai_health_check(
    session,  # aiohttp ClientSession
    api_key: str,
    snapshot: str,
) -> dict[str, str]:
    """Call Claude API to analyze system health.

    Args:
        session: aiohttp ClientSession for HTTP requests.
        api_key: Anthropic API key.
        snapshot: Formatted data snapshot string.

    Returns:
        Dict with keys: status, summary, details.
    """
    if not api_key:
        return {"status": "SKIP", "summary": "No Anthropic API key configured", "details": ""}

    text = ""
    try:
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": f"Current system snapshot:\n\n{snapshot}"}],
        }

        async with session.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=30,
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
            text = data["content"][0]["text"]

            # Parse JSON response
            # Handle cases where Claude wraps in markdown code blocks
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
                clean = clean.rsplit("```", 1)[0]
            clean = clean.strip()

            result = json.loads(clean)
            return {
                "status": result.get("status", "UNKNOWN"),
                "summary": result.get("summary", ""),
                "details": result.get("details", ""),
            }

    except json.JSONDecodeError as exc:
        _LOGGER.warning(
            "GenAI health check returned non-JSON: %s",
            text[:200] if text else str(exc),
        )
        return {"status": "ERROR", "summary": "Non-JSON response from API", "details": str(exc)}
    except Exception as exc:
        _LOGGER.warning("GenAI health check failed: %s", exc)
        return {"status": "ERROR", "summary": str(exc), "details": ""}
