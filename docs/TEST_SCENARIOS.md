# MPC Test Scenarios & Failure Matrix

Complete coverage matrix for all failure modes, price events, weather conditions,
and combination scenarios. Use this as the regression test checklist.

## Infrastructure Failures

| # | Scenario | Solar Forecast | Price Forecast | Register Writes | Detection | Contingency | Status |
|---|---|---|---|---|---|---|---|
| 1 | All normal | Solcast + VRM cap | Amber 30h | Optimizer | - | - | COVERED |
| 2 | Internet down | VRM cache (24h) then HA history | Amber stale, defensive discharge | Stale safety R2901=300 | Amber unavailable | Works 24h+, HA history fallback | COVERED |
| 3 | Internet down >24h | HA history profile (7d) | Defensive pricing ($2 peak/$0.30 off) | Conservative hold 30% | Data stale sensor | HA history + defensive discharge | COVERED |
| 4 | Grid down | Normal (if internet via 4G) | Amber irrelevant | ESS standalone | Genset auto-start | Victron native | COVERED |
| 5 | Grid + internet down | Bell curve | No pricing | ESS standalone | Genset auto-start | Victron independent of MPC | COVERED |
| 6 | Modbus TCP down | Normal | Normal | FAIL - can't write | modbus_connected OFF + notification | Registers stay at last value, user alerted | COVERED |
| 7 | HA crash/restart | Cold cache | Amber reloads | Last value during restart | setup_retry state | Coordinator retries | COVERED |
| 8 | Pi SD card failure | Everything stops | Everything stops | Frozen | Nothing running | Manual intervention | HARDWARE RISK |

## API Failures (Internet Up)

| # | Scenario | What Happens | Detection | Contingency | Status |
|---|---|---|---|---|---|
| 9 | Amber API down | Price unavailable, defensive pricing | mpc_stale_data after 10min | $2.00 peak / $0.30 off-peak + notification | COVERED |
| 10 | Amber down + spike happening | Defensive discharge during 17-21h | Amber unavailable >5min | $2.00 assumed price forces discharge | COVERED |
| 11 | VRM API down | Solcast still works | Warning logged | Solcast then bell curve | COVERED |
| 12 | VRM down + no Solcast | HA history profile (7d) | Logged | Local data captures shading/patterns | COVERED |
| 13 | Solcast API down | Entity goes stale | Falls through to VRM | VRM historical still good | COVERED |
| 14 | Open-Meteo down | Cloud layers unavailable | cloud_source: met.no_total | met.no total cloud | COVERED |
| 15 | PetrolSpy down | Cache (24h) then default | Logged | Configured default price | COVERED |
| 16 | All APIs down | Bell curve, $0.30 flat | Multiple warnings | Conservative operation | DEGRADED |

## Price Event Scenarios

| # | Scenario | Expected Behavior | Override Logic | Status |
|---|---|---|---|---|
| 17 | Price spike ($1-25/kWh) | R2901=100, discharge everything | `is_spike or buy > $1.0` | COVERED |
| 18 | Negative pricing | R2901=1000, charge (paid to consume) | `buy_price < 0` | COVERED |
| 19 | Spike + Amber down | Defensive discharge if 17-21h | $2.00 assumed during peak | COVERED |
| 20 | Spike + internet down | Defensive discharge if 17-21h | $2.00 assumed during peak | COVERED |
| 21 | Negative + Amber down | Can't detect negative | Misses free money | MISSED OPPORTUNITY |
| 22 | Feed-in spike (high FIT) | R2706=70, export for profit | sell_price > $0.10 + SoC > 30% | COVERED |
| 23 | Feed-in spike + Amber down | Can't detect high FIT | R2706=0 (block export) | MISSED REVENUE |

## Overnight Scenarios

| # | Scenario | Expected | Mechanism | Status |
|---|---|---|---|---|
| 24 | Normal overnight | Hold ~63%, 30% hard floor | overnight_hold_reward + soc_min_schedule | COVERED |
| 25 | Overnight + cheap prices (<$0.15) | Full hold reward | scale_overnight_hold_reward = 1.0 | COVERED |
| 26 | Overnight + expensive (>$0.25) | Reward zero, discharge | scale_overnight_hold_reward = 0.0 | COVERED |
| 27 | Overnight + AC (hot night) | Higher load forecast | indoor_temp_ac_boost | COVERED |
| 28 | Overnight + SoC hits 30% | Switch to grid | 30% hard constraint in LP | COVERED |
| 29 | Overnight + internet down | Hold at last state | Stale safety R2901=300 | COVERED |
| 30 | Overnight + genset starts | Genset charges battery | MPC monitors, genset independent | COVERED |

## Solar/Weather Scenarios

| # | Scenario | Expected | Mechanism | Status |
|---|---|---|---|---|
| 31 | Clear day, good solar | Charge to 100% | Solcast + VRM P90 envelope cap | COVERED |
| 32 | Cloudy day | Low production forecast | Solcast satellite sees cloud | COVERED |
| 33 | Partly cloudy, variable | Variable production | Cloud layers update 30min | COVERED |
| 34 | Solcast over-forecast (clear) | Raw 35kWh capped to ~20kWh | VRM P90 hourly shading envelope | COVERED |
| 35 | Morning shade, afternoon sun | Per-hour shading pattern | VRM P90 per-hour per-month | COVERED |
| 36 | Unexpected rain mid-day | Intraday correction | _maybe_adjust_day_type downgrades | COVERED |
| 37 | Weather entity unavailable | Can't classify day type | Default partly_cloudy | DEGRADED |

## System/Hardware Scenarios

| # | Scenario | Expected | Mechanism | Status |
|---|---|---|---|---|
| 38 | Cell balancing due | Force charge to 100% | force_full_charge flag | COVERED |
| 39 | Battery near empty (10%) | Grid + genset backup | Hardware SoC floor + auto-start | COVERED |
| 40 | Solver fails (infeasible) | Conservative hold | _build_fallback | COVERED |
| 41 | Solver slow (>2s) | Delayed decision | Completes in executor, logged | COVERED |
| 42 | Two MPC instances writing | Register conflicts | YAML automations disabled | COVERED |
| 43 | Mac runner still running | Pushes stale sensors | Harmless, automations off | COVERED |

## Combination Scenarios (Multi-Failure)

| # | Scenario | Risk Level | Status |
|---|---|---|---|
| 44 | Internet down + grid down | Low - ESS standalone | COVERED |
| 45 | Amber + VRM + Solcast all down | Medium - HA history + defensive pricing | COVERED |
| 46 | Modbus down + price spike | HIGH - can't discharge at $25/kWh, user alerted | MONITORED |
| 47 | HA restart during spike | Low - 60s blind spot | ACCEPTABLE |
| 48 | Pi reboot overnight | Low - registers hold | COVERED |

## Coverage Summary

| Status | Count | Description |
|---|---|---|
| COVERED | 43 | Full contingency, tested or validated |
| DEGRADED | 2 | Works but with reduced forecast quality (#16, #37) |
| MONITORED | 1 | Cannot auto-fix, but user alerted immediately (#46) |
| MISSED OPP | 2 | Misses revenue opportunity, not dangerous (#21, #23) |
| HARDWARE | 1 | SD card failure - outside software scope (#8) |

**178 automated tests** cover these scenarios. Run `python -m pytest tests/ -v` to verify.

## Priority Fixes (All Implemented)

1. **HA recorder history** (v0.3.0, #26) — local solar/load fallback from 7 days of recorder data. Fixed scenarios #2, #3, #12.
2. **Amber-down defensive discharge** (v0.3.0, #27) — when Amber unavailable >5min, assumes $2.00/kWh during evening peak (17-21h) to protect against undetected spikes. Fixed scenarios #10, #19, #20.
3. **Modbus health monitoring** (v0.3.0, #28) — tracks consecutive write failures, alerts via persistent notification and binary sensor after 3 failures. Fixed scenario #6, improved #46 to MONITORED status (cannot auto-fix hardware, but user is alerted immediately).

## How to Test

### Simulated Failures (shadow mode)
- Amber down: disable Amber integration temporarily
- VRM down: remove VRM token from config entry
- Solcast down: disable Solcast integration
- Internet down: block outbound on Pi firewall
- Modbus down: stop Modbus integration

### Price Events (shadow mode)
- Spike: wait for real spike, or create test automation that sets spike sensor
- Negative: wait for real negative, or mock Amber price entity

### Overnight (live, low risk)
- Validated 2026-03-18: discharge 95% -> hold at 63% -> maintain overnight

### Solar (live, compare forecasts)
- Clear day: compare Solcast raw vs VRM P90 cap vs actual yield
- Cloudy day: validated 2026-03-19: Solcast 12kWh vs VRM 15.6kWh
