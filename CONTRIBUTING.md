# Contributing to Victron MPC Battery Optimizer

Thank you for your interest in contributing! This guide covers the development workflow.

## Prerequisites

- Python 3.12+
- A working Home Assistant development environment (recommended) or just Python for running tests
- Familiarity with Victron ESS and Amber Electric concepts

## Development setup

1. Clone the repository:
   ```bash
   git clone https://github.com/akaszubski/ha-victron-mpc.git
   cd ha-victron-mpc
   ```

2. Install test dependencies:
   ```bash
   pip install -r requirements_test.txt
   ```

3. Run the test suite:
   ```bash
   python -m pytest tests/ -v
   ```

## Running tests

The project has 230+ tests covering the optimizer, forecasts, config, GenAI monitor, and failure scenarios.

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_optimizer.py -v

# Run a specific test
python -m pytest tests/test_optimizer.py::test_basic_optimization -v

# Run with short output
python -m pytest tests/ -q
```

All tests must pass before submitting a PR.

## Project structure

```
custom_components/victron_mpc/
  __init__.py          # HA integration setup
  config_flow.py       # Setup wizard (5 steps)
  coordinator.py       # DataUpdateCoordinator — main loop
  optimizer.py         # LP solver (scipy HiGHS)
  forecasts.py         # Solar/load/price forecast builder
  config.py            # Dataclasses (VictronSystem, MPCTunables)
  genai_monitor.py     # Deterministic + GenAI health checks
  advisor.py           # AI advisor overlay (optional)
  sensor.py            # HA sensor entities
  number.py            # HA number entities (tunables)
  switch.py            # Shadow mode switch
  binary_sensor.py     # Problem indicators
  api/
    fuel_price.py      # PetrolSpy diesel prices
    modbus.py          # Modbus register helpers
    open_meteo.py      # Cloud layer data
    solcast.py         # Solcast solar forecast
    vrm.py             # VRM API client
tests/
  conftest.py          # Shared fixtures
  test_optimizer.py    # LP solver tests
  test_forecasts.py    # Forecast chain tests
  test_config.py       # Configuration tests
  test_genai_monitor.py  # Health monitor tests
  ...
```

## Coding standards

- Follow existing code patterns in the repository
- Use type hints for all public function signatures
- Keep functions focused — the optimizer and coordinator are already large, prefer small helpers
- Add tests for new functionality
- Use `LOGGER` from `const.py` for logging (not `print()`)

## Pull request workflow

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes with tests
4. Ensure all tests pass: `python -m pytest tests/ -v`
5. Commit with a descriptive message (e.g., `feat: add time-of-use pricing support`)
6. Push and open a PR against `main`

## Commit message format

We use conventional commits:

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation only
- `test:` — adding or updating tests
- `refactor:` — code change that neither fixes a bug nor adds a feature

## Important notes

- **Shadow mode**: The integration starts in shadow mode by default. Test with shadow mode ON before enabling live register writes.
- **Register safety**: Never write R2700 (Grid Setpoint). R2901 and R2706 are the only registers MPC controls.
- **Amber Electric**: This integration currently requires Amber Electric (Australia only). If you're adapting it for another pricing source, please open an issue to discuss the approach first.
