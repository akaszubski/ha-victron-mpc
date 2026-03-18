# Victron MPC Battery Optimizer

[![HACS Validation](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hacs.yaml/badge.svg)](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hacs.yaml)
[![Hassfest](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/akaszubski/ha-victron-mpc/actions/workflows/hassfest.yaml)

HACS custom integration for **Victron ESS** battery optimization with **Amber Electric** wholesale pricing.

Uses Linear Programming (scipy HiGHS) to compute optimal 24-hour battery dispatch every 5 minutes, minimizing electricity cost while respecting battery health and physical constraints.

## Features

- **LP-optimized dispatch** — 288-step (5-min) rolling horizon via scipy
- **Amber Electric integration** — wholesale pricing, spike detection, feed-in optimization
- **Victron Modbus TCP** — direct register writes (R2901 ESS min SoC, R2706 feed-in)
- **Weather-classified solar forecast** — VRM historical P90/P70/P40/P15 by day type
- **Cloud layer derating** — Open-Meteo low/mid/high altitude cloud weighting
- **Overnight preservation** — configurable hold reward + hard SoC floor
- **Cell balancing** — periodic full charge for BMS health
- **Seasonal load adjustment** — VRM monthly consumption + temperature correction
- **AC demand detection** — real-time indoor temp + climate entity monitoring
- **Shadow mode** — validate decisions without writing registers

## Requirements

- Home Assistant 2024.8+
- Victron Cerbo GX with Modbus TCP enabled
- Amber Electric account (with HA integration configured)
- HA Modbus integration configured for Victron

## Installation

1. Add this repository to HACS as a custom repository
2. Install "Victron MPC Battery Optimizer"
3. Restart Home Assistant
4. Add integration via Settings → Integrations → Add Integration

## Status

**In development** — see [issues](https://github.com/akaszubski/ha-victron-mpc/issues) for roadmap.
