# Victron MPC Battery Optimizer — HACS Integration

HACS custom component for Home Assistant that runs an LP-optimized battery dispatch controller for Victron ESS systems with Amber Electric wholesale pricing.

**Production** on Raspberry Pi since 2026-03-18. Writes Modbus registers every 5 minutes.

## Architecture

```
coordinator.py (5-min cycle)
  → forecasts.py (Solcast + VRM + Open-Meteo + Amber → 288-step arrays)
  → optimizer.py (scipy LP → optimal SoC trajectory)
  → api/modbus.py (write R2901 ESS min SoC, R2706 feed-in limit)
  → sensor.py / number.py / switch.py (expose to HA)
```

## Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `optimizer.py` | 663 | LP solver — **kept in sync with homeassistant/scripts/mpc/optimizer.py** |
| `coordinator.py` | 1227 | 5-min cycle orchestrator |
| `forecasts.py` | 1954 | Three-layer solar + load + price forecast builder |
| `config.py` | 240 | VictronSystem + MPCTunables dataclasses |
| `config_flow.py` | 357 | 5-step setup wizard (Modbus → Battery → Amber → Sensors → VRM) |
| `api/modbus.py` | | Register read/write helpers |
| `api/vrm.py` | | VRM REST API for solar history |
| `api/solcast.py` | | Solcast integration detector |
| `api/open_meteo.py` | | Cloud layer data for solar derating |
| `api/fuel_price.py` | | PetrolSpy diesel pricing for genset cost |

## Sync with homeassistant repo

This repo is the **production** codebase. The **development** codebase is at `~/Dev/homeassistant/scripts/mpc/`.

- `optimizer.py` must be logically identical in both repos
- HACS version adds defensive `float()` casts for HA entity values (may be numpy)
- HACS version has extra `len() > 0` guards
- When changing optimizer logic: edit local → run 404 tests → copy to HACS → run 211 tests

**Do NOT diverge the optimizer.** Previous divergence caused: soft floor constraints missing from LP matrix (zero effect in production), double-stacked rewards, Amber band hardcoded to "low".

## Testing

```bash
python -m pytest tests/ -v   # 211 tests, ~5s
```

Test files:
- `test_optimizer.py` — LP solver, soft floor, sunset constraint, SoC profile, grid-charge alongside solar
- `test_issue_fixes.py` — regressions for GitHub issues #31-35
- `test_forecasts.py` — price/solar/load forecast building
- `test_config.py` — defaults, bounds, config entry loading
- `test_amber_defensive.py` — Amber API failure handling
- `test_cloud_layers.py` — cloud layer derating
- `test_modbus_health.py` / `test_modbus_helpers.py` — register logic

## Key Parameters

| Parameter | Value | Why |
|-----------|-------|-----|
| `soc_profile_pre_peak` | $0.20 | Must exceed grid cost ($0.15 + $0.02) for LP to grid-charge |
| `battery_wear_cost` | $0.02 | Pylontech LFP at ~60% DoD |
| `soft_floor_penalty` | $0.10 | 30% soft floor — penalty-based, not hard constraint |
| `soc_floor_pct` | 10% | Hard floor (hardware minimum) |
| `sunset_soc_target_pct` | 95% | Fixed — `compute_sunset_target()` exists but paused |
| `grid_charge_boost` | $0.15 | Bonus for extremely_low/very_low Amber bands |

## Register 2901 Logic (CRITICAL)

- **grid_charge**: register = target SoC (above current, forces grid import)
- **solar_charge**: register = hard floor - 1% (prevents grid pull)
- **discharge/hold**: register = trajectory floor - 5% buffer
- **NEVER** set register >= current SoC unless you want grid charging

## Deploy to Pi

```bash
# From ~/Dev/ha-victron-mpc:
scp custom_components/victron_mpc/*.py root@192.168.0.215:/config/custom_components/victron_mpc/
scp custom_components/victron_mpc/api/*.py root@192.168.0.215:/config/custom_components/victron_mpc/api/

# Write safe register BEFORE restart
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "http://192.168.0.215:8123/api/services/modbus/write_register" \
  -d '{"hub": "cerbo", "unit": 100, "address": 2901, "value": 200}'

# Restart
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://192.168.0.215:8123/api/services/homeassistant/restart"

# Verify: grid import < 50W, check logs for errors
```

**ALWAYS deploy ALL .py files. ALWAYS write register=200 before restart. ALWAYS verify grid import after.**

## Known Issues

- `forecasts.py` current period Amber band must read from first forecast entry (was hardcoded "low" — fixed 2026-03-28)
- `soc_profile_enabled` default is True but number entity may override on restart
- `overnight_hold_reward` set via number entity (volatile) — check actual value
- Some number entities have generic names (`number.victron_mpc_battery_optimizer_2`) — HACS naming issue
