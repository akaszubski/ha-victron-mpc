"""Data fetcher for autotune historical cache.

Fetches VRM solar/load and HA Amber price data, builds DayData JSON files.
Uses stdlib only (urllib.request) -- zero HA imports.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .types import DayData

# ──────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────

DEFAULT_HA_URL = "http://192.168.0.215:8123"
DEFAULT_VRM_INSTALLATION_ID = "143481"
DEFAULT_LATITUDE = -37.81


def _vrm_get(token: str, inst_id: str, path: str) -> dict:
    """GET from VRM API with auth header.

    Args:
        token: VRM bearer token.
        inst_id: VRM installation ID.
        path: API path after /v2/installations/{inst_id}/.

    Returns:
        Parsed JSON response as dict.

    Raises:
        urllib.error.HTTPError: On non-2xx response.
    """
    url = f"https://vrmapi.victronenergy.com/v2/installations/{inst_id}/{path}"
    req = urllib.request.Request(url, headers={"X-Authorization": f"Token {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _ha_get(url: str, token: str, path: str) -> list:
    """GET from HA REST API with bearer token.

    Args:
        url: Base HA URL (e.g. http://192.168.0.215:8123).
        token: HA long-lived access token.
        path: API path (e.g. /api/history/period/...).

    Returns:
        Parsed JSON response as list.

    Raises:
        urllib.error.HTTPError: On non-2xx response.
    """
    full_url = f"{url}{path}"
    req = urllib.request.Request(
        full_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ──────────────────────────────────────────────────────────────────
# VRM data fetching
# ──────────────────────────────────────────────────────────────────


def fetch_vrm_hourly(
    vrm_token: str,
    inst_id: str,
    start_date: str,
    end_date: str,
) -> dict[str, dict]:
    """Fetch hourly solar yield and load from VRM.

    Args:
        vrm_token: VRM bearer token.
        inst_id: VRM installation ID.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD), inclusive.

    Returns:
        Dict mapping date string to {"solar_kw": [24 floats], "load_kw": [24 floats]}.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(
        days=1
    )

    start_unix = int(start_dt.timestamp())
    end_unix = int(end_dt.timestamp())

    path = (
        f"stats?type=custom&start={start_unix}&end={end_unix}"
        f"&interval=hours"
        f"&attributeCodes[]=solar_yield"
        f"&attributeCodes[]=total_consumption"
    )

    data = _vrm_get(vrm_token, inst_id, path)
    records = data.get("records", {})

    solar_records = records.get("solar_yield", [])
    load_records = records.get("total_consumption", [])

    result: dict[str, dict] = {}

    # Process solar
    for entry in solar_records:
        ts = entry[0]
        wh = entry[1] if len(entry) > 1 else 0.0
        if ts > 1e12:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        hour = dt.hour
        if date_str not in result:
            result[date_str] = {
                "solar_kw": [0.0] * 24,
                "load_kw": [0.0] * 24,
            }
        if 0 <= hour < 24:
            result[date_str]["solar_kw"][hour] = float(wh) / 1000.0

    # Process load
    for entry in load_records:
        ts = entry[0]
        kwh = entry[1] if len(entry) > 1 else 0.0
        if ts > 1e12:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        hour = dt.hour
        if date_str not in result:
            result[date_str] = {
                "solar_kw": [0.0] * 24,
                "load_kw": [0.0] * 24,
            }
        if 0 <= hour < 24:
            result[date_str]["load_kw"][hour] = float(kwh)

    return result


# ──────────────────────────────────────────────────────────────────
# Amber price fetching
# ──────────────────────────────────────────────────────────────────


def _bucket_ha_history_hourly(
    states: list[dict],
    date_str: str,
    default: float,
) -> list[float]:
    """Bucket HA state change history into 24 hourly values.

    Uses last-known-value for each hour bucket.

    Args:
        states: HA history state entries with "state" and "last_changed" keys.
        date_str: The date being processed (YYYY-MM-DD).
        default: Default value if no data for an hour.

    Returns:
        List of 24 hourly values.
    """
    hourly = [default] * 24
    last_value = default

    for entry in states:
        try:
            value = float(entry["state"])
        except (ValueError, TypeError):
            continue

        last_changed = entry.get("last_changed", entry.get("last_updated", ""))
        if not last_changed:
            continue

        # Parse ISO timestamp
        ts_str = last_changed.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts_str)
        except ValueError:
            continue

        entry_date = dt.strftime("%Y-%m-%d")
        if entry_date == date_str:
            hour = dt.hour
            # Fill this hour and forward with this value
            for h in range(hour, 24):
                if hourly[h] == default or h == hour:
                    hourly[h] = value
            last_value = value
        elif entry_date < date_str:
            last_value = value

    # Fill leading hours with last known value before this date
    for h in range(24):
        if hourly[h] == default and last_value != default:
            hourly[h] = last_value
        elif hourly[h] != default:
            break

    return hourly


def fetch_amber_prices(
    ha_url: str,
    ha_token: str,
    start_date: str,
    end_date: str,
) -> dict[str, dict]:
    """Fetch Amber buy and sell prices from HA history API.

    Args:
        ha_url: Home Assistant base URL.
        ha_token: HA long-lived access token.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD), inclusive.

    Returns:
        Dict mapping date string to {"buy": [24 floats], "sell": [24 floats]}.
    """
    start_iso = f"{start_date}T00:00:00+00:00"
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    end_iso = f"{end_dt.strftime('%Y-%m-%d')}T00:00:00+00:00"

    result: dict[str, dict] = {}

    for sensor, key, default in [
        ("sensor.amber_general_price", "buy", 0.25),
        ("sensor.amber_feed_in_price", "sell", 0.05),
    ]:
        path = f"/api/history/period/{start_iso}?filter_entity_id={sensor}&end_time={end_iso}"
        try:
            response = _ha_get(ha_url, ha_token, path)
            states = response[0] if response else []
        except Exception:
            states = []

        # Process each date in range
        current = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        while current <= end:
            ds = current.strftime("%Y-%m-%d")
            if ds not in result:
                result[ds] = {"buy": [0.25] * 24, "sell": [0.05] * 24}
            result[ds][key] = _bucket_ha_history_hourly(states, ds, default)
            current += timedelta(days=1)

    return result


# ──────────────────────────────────────────────────────────────────
# Pure helper functions
# ──────────────────────────────────────────────────────────────────


def interpolate_hourly_to_5min(hourly: list[float]) -> list[float]:
    """Expand 24 hourly values to 288 five-minute values.

    Each hourly value is held constant for 12 five-minute steps.

    Args:
        hourly: List of 24 hourly values.

    Returns:
        List of 288 five-minute values.

    Raises:
        ValueError: If hourly does not have exactly 24 entries.
    """
    if len(hourly) != 24:
        raise ValueError(f"Expected 24 hourly values, got {len(hourly)}")

    result: list[float] = []
    for value in hourly:
        result.extend([value] * 12)
    return result


def compute_sunset_step(date_str: str, latitude: float = DEFAULT_LATITUDE) -> int:
    """Compute sunset step index from date and latitude using solar position math.

    Args:
        date_str: Date as YYYY-MM-DD.
        latitude: Latitude in degrees (negative for south).

    Returns:
        Step index (0-287) corresponding to sunset time.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    day_of_year = dt.timetuple().tm_yday

    # Solar declination (degrees)
    declination_deg = 23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0))

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination_deg)

    # Hour angle at sunset
    cos_hour_angle = -math.tan(lat_rad) * math.tan(dec_rad)

    # Clamp for polar regions
    cos_hour_angle = max(-1.0, min(1.0, cos_hour_angle))

    hour_angle_deg = math.degrees(math.acos(cos_hour_angle))
    sunset_hour = 12.0 + hour_angle_deg / 15.0

    step = int(sunset_hour * 12)
    return max(0, min(287, step))


def compute_overnight_steps() -> list[int]:
    """Compute step indices for overnight hours 22:00-06:00.

    Returns:
        Sorted list of 96 step indices covering hours 0-5 and 22-23.
    """
    steps: list[int] = []
    # Hours 0-5: steps 0-71
    for h in range(6):
        for s in range(12):
            steps.append(h * 12 + s)
    # Hours 22-23: steps 264-287
    for h in range(22, 24):
        for s in range(12):
            steps.append(h * 12 + s)
    return sorted(steps)


def derive_price_band(buy_price: float) -> str:
    """Derive Amber price band from buy price.

    Args:
        buy_price: Buy price in $/kWh.

    Returns:
        Band string: extremely_low, very_low, low, neutral, high, or spike.
    """
    if buy_price < 0:
        return "extremely_low"
    if buy_price < 0.08:
        return "very_low"
    if buy_price < 0.15:
        return "low"
    if buy_price < 0.35:
        return "neutral"
    if buy_price < 0.80:
        return "high"
    return "spike"


def build_day_json(
    date_str: str,
    solar_hourly: list[float],
    load_hourly: list[float],
    buy_hourly: list[float],
    sell_hourly: list[float],
    latitude: float = DEFAULT_LATITUDE,
) -> dict:
    """Construct a DayData-compatible dict from hourly data.

    Args:
        date_str: Date as YYYY-MM-DD.
        solar_hourly: 24 hourly solar kW values.
        load_hourly: 24 hourly load kW values.
        buy_hourly: 24 hourly buy price values ($/kWh).
        sell_hourly: 24 hourly sell price values ($/kWh).
        latitude: Latitude for sunset calculation.

    Returns:
        Dict with all DayData fields, ready for JSON serialization.
    """
    solar_5min = interpolate_hourly_to_5min(solar_hourly)
    load_5min = interpolate_hourly_to_5min(load_hourly)
    buy_5min = interpolate_hourly_to_5min(buy_hourly)
    sell_5min = interpolate_hourly_to_5min(sell_hourly)

    sunset_step = compute_sunset_step(date_str, latitude)
    overnight_steps = compute_overnight_steps()
    price_bands = [derive_price_band(p) for p in buy_5min]

    return {
        "date": date_str,
        "solar_kw_5min": solar_5min,
        "load_kw_5min": load_5min,
        "buy_price_5min": buy_5min,
        "sell_price_5min": sell_5min,
        "sunset_step": sunset_step,
        "overnight_steps": overnight_steps,
        "start_soc_kwh": 7.1,
        "price_bands": price_bands,
    }


# ──────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────


def fetch_and_save(
    days: int,
    data_dir: Path,
    vrm_token: str,
    inst_id: str,
    ha_url: str,
    ha_token: str,
    latitude: float = DEFAULT_LATITUDE,
) -> int:
    """Fetch historical data and save as JSON files.

    Args:
        days: Number of days to fetch (counting back from yesterday).
        data_dir: Directory to save JSON files.
        vrm_token: VRM bearer token.
        inst_id: VRM installation ID.
        ha_url: Home Assistant base URL.
        ha_token: HA long-lived access token.
        latitude: Latitude for sunset calculation.

    Returns:
        Number of day files saved.
    """
    data_dir.mkdir(parents=True, exist_ok=True)

    end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"Fetching VRM data {start_date} to {end_date}...")
    vrm_data = fetch_vrm_hourly(vrm_token, inst_id, start_date, end_date)

    print(f"Fetching Amber prices {start_date} to {end_date}...")
    amber_data = fetch_amber_prices(ha_url, ha_token, start_date, end_date)

    count = 0
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    while current <= end_dt:
        ds = current.strftime("%Y-%m-%d")
        solar_hourly = vrm_data.get(ds, {}).get("solar_kw", [0.0] * 24)
        load_hourly = vrm_data.get(ds, {}).get("load_kw", [0.0] * 24)
        buy_hourly = amber_data.get(ds, {}).get("buy", [0.25] * 24)
        sell_hourly = amber_data.get(ds, {}).get("sell", [0.05] * 24)

        day_json = build_day_json(ds, solar_hourly, load_hourly, buy_hourly, sell_hourly, latitude)

        out_path = data_dir / f"{ds}.json"
        with open(out_path, "w") as f:
            json.dump(day_json, f, indent=2)

        count += 1
        current += timedelta(days=1)

    print(f"Saved {count} day files to {data_dir}")
    return count


def main() -> None:
    """CLI entry point for fetch_data.

    Usage:
        python -m autotune.fetch_data --days 30 --data-dir autotune/data/
    """
    parser = argparse.ArgumentParser(description="Fetch historical data for autotune cache")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to fetch (default: 30)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("autotune/data"),
        help="Directory to save day JSON files (default: autotune/data/)",
    )
    parser.add_argument(
        "--latitude",
        type=float,
        default=DEFAULT_LATITUDE,
        help=f"Latitude for sunset calculation (default: {DEFAULT_LATITUDE})",
    )
    args = parser.parse_args()

    vrm_token = os.environ.get("VRM_TOKEN", "")
    inst_id = os.environ.get("VRM_INSTALLATION_ID", DEFAULT_VRM_INSTALLATION_ID)
    ha_url = os.environ.get("HA_URL", DEFAULT_HA_URL)
    ha_token = os.environ.get("HA_TOKEN", "")

    if not vrm_token:
        print("ERROR: VRM_TOKEN environment variable not set")
        raise SystemExit(1)
    if not ha_token:
        print("ERROR: HA_TOKEN environment variable not set")
        raise SystemExit(1)

    count = fetch_and_save(
        days=args.days,
        data_dir=args.data_dir,
        vrm_token=vrm_token,
        inst_id=inst_id,
        ha_url=ha_url,
        ha_token=ha_token,
        latitude=args.latitude,
    )
    print(f"Done. {count} days fetched.")


if __name__ == "__main__":
    main()
