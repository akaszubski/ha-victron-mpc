"""MPC Battery Optimizer using Linear Programming.

Solves a 24-hour optimal battery dispatch problem for Victron ESS
with Amber Electric wholesale pricing. The LP determines optimal
charge/discharge/import/export at each 5-minute interval to minimize
total electricity cost while respecting physical constraints.

Key insight for Victron: the optimizer computes the optimal SoC trajectory,
then we map each timestep to a Register 2901 value that achieves it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog


@dataclass
class OptInput:
    """All inputs for a single optimization run."""

    # Horizon
    horizon_steps: int  # Number of 5-min steps (288 for 24h)
    dt_hours: float  # Timestep in hours (5/60)

    # Battery state
    battery_soc_kwh: float  # Current SoC in kWh
    battery_capacity_kwh: float
    soc_min_kwh: float  # Hard floor in kWh
    soc_max_kwh: float  # Hard ceiling in kWh
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float

    # Grid limits
    max_grid_import_kw: float
    max_grid_export_kw: float

    # Forecasts — arrays of length horizon_steps
    solar_forecast_kw: list[float]
    load_forecast_kw: list[float]
    buy_price: list[float]  # $/kWh
    sell_price: list[float]  # $/kWh

    # Cost tunables
    battery_wear_cost: float
    grid_import_penalty: float

    # Sunset/terminal incentives
    sunset_step: int | None  # Index of sunset in horizon (None if after sunset)
    sunset_reward: float  # $/kWh reward for SoC at sunset
    terminal_reward: float  # $/kWh reward for SoC at end of horizon

    # Overnight preservation — reward for maintaining SoC during overnight hours
    overnight_hold_reward: float = 0.0
    overnight_steps: list[int] | None = None  # Step indices that are "overnight"

    # Time-varying SoC floor — per-step minimum SoC in kWh.
    # If provided (length = horizon_steps), overrides soc_min_kwh per step.
    # Used to enforce higher minimums during overnight hours.
    soc_min_schedule_kwh: list[float] | None = None

    # Cell balancing — force charge to 100% for BMS health
    force_full_charge: bool = False


@dataclass
class OptOutput:
    """Complete optimization results."""

    status: str  # "optimal", "fallback", "error"
    target_soc_pct: float  # Recommended SoC % for next interval
    target_register: int  # Register 2901 value (100-1000)
    mode: str  # "grid_charge", "solar_charge", "discharge", "hold"
    reason: str  # Human-readable explanation

    # Full trajectories (length = horizon_steps + 1 for soc)
    soc_trajectory_pct: list[float]
    charge_schedule_kw: list[float]
    discharge_schedule_kw: list[float]
    grid_import_schedule_kw: list[float]
    grid_export_schedule_kw: list[float]
    solar_used_schedule_kw: list[float]

    # Financials
    total_cost: float  # Total cost over horizon ($)
    cost_breakdown: dict  # {grid_cost, export_revenue, wear_cost}
    effective_price: float  # Current effective electricity price $/kWh

    # Solver metadata
    solver_status: str
    solve_time_ms: float


def optimize(inputs: OptInput) -> OptOutput:
    """Run the LP optimizer and return the optimal battery plan.

    The LP minimizes electricity cost over the forecast horizon:
        min  sum_t (grid_import * buy_price - grid_export * sell_price
                     + discharge * wear_cost + grid_import * import_penalty) * dt
              - sunset_reward * soc[sunset]
              - terminal_reward * soc[N]

    Subject to:
        Power balance at each timestep
        Battery SoC dynamics and bounds
        Physical power limits on all components
    """
    N = inputs.horizon_steps
    dt = inputs.dt_hours
    cap = inputs.battery_capacity_kwh  # noqa: F841
    eta_c = inputs.charge_efficiency
    eta_d = inputs.discharge_efficiency
    soc_init = inputs.battery_soc_kwh

    n_vars = 5 * N

    # Variable index helpers
    def pc(t):
        return t  # p_charge

    def pd(t):
        return N + t  # p_discharge

    def gi(t):
        return 2 * N + t  # grid_import

    def ge(t):
        return 3 * N + t  # grid_export

    def su(t):
        return 4 * N + t  # solar_used

    # === OBJECTIVE ===
    c = np.zeros(n_vars)
    for t in range(N):
        c[gi(t)] = inputs.buy_price[t] * dt + inputs.grid_import_penalty * dt
        c[ge(t)] = -inputs.sell_price[t] * dt
        c[pd(t)] = inputs.battery_wear_cost * dt

    # Sunset reward: encourage full battery at sunset
    # soc[sunset] = soc_init + sum_{k<sunset}(eta_c*pc[k] - pd[k]/eta_d)*dt
    # Adding -reward * soc[sunset] modifies objective coefficients
    if inputs.sunset_step is not None and 0 < inputs.sunset_step < N:
        for k in range(inputs.sunset_step):
            c[pc(k)] -= inputs.sunset_reward * eta_c * dt
            c[pd(k)] += inputs.sunset_reward * dt / eta_d

    # Terminal reward: encourage reasonable SoC at end of horizon
    for k in range(N):
        c[pc(k)] -= inputs.terminal_reward * eta_c * dt
        c[pd(k)] += inputs.terminal_reward * dt / eta_d

    # Force full charge: strong reward for reaching max SoC (cell balancing).
    # Applied as a large terminal-like reward that dominates other objectives,
    # pushing the optimizer to charge to 100% at some point during the horizon.
    if inputs.force_full_charge:
        # Use a reward much larger than typical prices to ensure it dominates
        full_charge_reward = 2.0  # $/kWh — much higher than any electricity price
        for k in range(N):
            c[pc(k)] -= full_charge_reward * eta_c * dt
            c[pd(k)] += full_charge_reward * dt / eta_d

    # Overnight hold reward: discourage discharge during overnight hours.
    # This preserves battery for morning price spikes.
    # Applied at the END of overnight (morning boundary) — rewards having
    # high SoC when morning prices start rising.
    if inputs.overnight_hold_reward > 0 and inputs.overnight_steps:
        # Use the last overnight step as the target — this is when morning starts
        morning_step = inputs.overnight_steps[-1]
        if 0 < morning_step < N:
            for k in range(morning_step):
                c[pc(k)] -= inputs.overnight_hold_reward * eta_c * dt
                c[pd(k)] += inputs.overnight_hold_reward * dt / eta_d

    # === EQUALITY CONSTRAINTS: Power balance ===
    # solar_used[t] + p_discharge[t] + grid_import[t]
    #   = load[t] + p_charge[t] + grid_export[t]
    A_eq = np.zeros((N, n_vars))
    b_eq = np.zeros(N)
    for t in range(N):
        A_eq[t, su(t)] = 1
        A_eq[t, pd(t)] = 1
        A_eq[t, gi(t)] = 1
        A_eq[t, pc(t)] = -1
        A_eq[t, ge(t)] = -1
        b_eq[t] = inputs.load_forecast_kw[t]

    # === INEQUALITY CONSTRAINTS: SoC bounds ===
    # soc[t] = soc_init + cumsum(eta_c*pc - pd/eta_d) * dt
    # Need: soc_min[t] <= soc[t] <= soc_max for all t=1..N
    #
    # Upper: sum_{k<t}(eta_c*pc[k] - pd[k]/eta_d)*dt <= soc_max - soc_init
    # Lower: -sum_{k<t}(eta_c*pc[k] - pd[k]/eta_d)*dt <= soc_init - soc_min[t]
    #
    # soc_min[t] can vary per step (e.g., higher overnight for safety margin)
    soc_min_schedule = inputs.soc_min_schedule_kwh
    if soc_min_schedule is None:
        soc_min_schedule = [inputs.soc_min_kwh] * N

    A_ub = np.zeros((2 * N, n_vars))
    b_ub = np.zeros(2 * N)

    for t in range(1, N + 1):
        # Upper SoC bound: cumulative energy <= soc_max - soc_init
        for k in range(t):
            A_ub[t - 1, pc(k)] = eta_c * dt
            A_ub[t - 1, pd(k)] = -dt / eta_d
        b_ub[t - 1] = inputs.soc_max_kwh - soc_init

        # Lower SoC bound: -cumulative energy <= soc_init - soc_min[t]
        soc_floor_t = soc_min_schedule[min(t - 1, len(soc_min_schedule) - 1)]
        for k in range(t):
            A_ub[N + t - 1, pc(k)] = -eta_c * dt
            A_ub[N + t - 1, pd(k)] = dt / eta_d
        b_ub[N + t - 1] = soc_init - soc_floor_t

    # === VARIABLE BOUNDS ===
    bounds = []
    for t in range(N):
        bounds.append((0, inputs.max_charge_kw))
    for t in range(N):
        bounds.append((0, inputs.max_discharge_kw))
    for t in range(N):
        bounds.append((0, inputs.max_grid_import_kw))
    for t in range(N):
        bounds.append((0, inputs.max_grid_export_kw))
    for t in range(N):
        bounds.append((0, max(0, inputs.solar_forecast_kw[t])))

    # === SOLVE ===
    t_start = time.time()
    result = linprog(
        c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
        bounds=bounds, method="highs",
    )
    solve_ms = (time.time() - t_start) * 1000

    if result.success:
        return _build_output(result, inputs, solve_ms)
    else:
        return _build_fallback(inputs, result.message, solve_ms)


def _build_output(result, inputs: OptInput, solve_ms: float) -> OptOutput:
    """Extract results from successful LP solve."""
    N = inputs.horizon_steps
    dt = inputs.dt_hours
    eta_c = inputs.charge_efficiency
    eta_d = inputs.discharge_efficiency
    cap = inputs.battery_capacity_kwh

    x = result.x
    p_charge = x[0:N]
    p_discharge = x[N : 2 * N]
    grid_import = x[2 * N : 3 * N]
    grid_export = x[3 * N : 4 * N]
    solar_used = x[4 * N : 5 * N]

    # Compute SoC trajectory
    soc_kwh = [inputs.battery_soc_kwh]
    for t in range(N):
        next_soc = soc_kwh[-1] + (eta_c * p_charge[t] - p_discharge[t] / eta_d) * dt
        soc_kwh.append(next_soc)

    soc_pct = [s / cap * 100 for s in soc_kwh]

    # Register 2901 is a FLOOR, not a target. The Victron ESS discharges
    # to cover loads freely as long as SoC > register value, and stops
    # (switches to grid) once SoC hits the register floor.
    #
    # Strategy: find the lowest SoC the optimizer plans to reach before
    # it next wants to charge, and set the register there. This lets the
    # ESS discharge continuously until MPC refreshes (every 5 min).
    #
    # For charge mode: set register HIGH (above current SoC) to force
    # grid charging up to target.
    target_soc_pct = soc_pct[1]  # next-step target for mode detection

    # Find the discharge floor: lowest planned SoC before the optimizer
    # next wants to charge.
    #
    # Lookahead window depends on price risk:
    #   - Normal prices (<$0.50): 1 hour — responsive to changes
    #   - High prices (>$0.50) or spikes: FULL trajectory until next charge
    #     At $25/kWh, even 5 min of grid costs $2.50. The battery must NEVER
    #     hit the floor during expensive periods. The LP already planned the
    #     optimal trajectory — trust it and give the ESS full room.
    max_price_ahead = max(inputs.buy_price[:min(12, N)])  # next hour's max
    current_price = inputs.buy_price[0]
    is_spike = current_price > 1.00 or max_price_ahead > 1.00

    if is_spike:
        # SPIKE: set floor to hard minimum. At $25/kWh we cannot risk
        # ANY grid usage. Drain battery as low as possible — the genset
        # has its own Victron-controlled low-SoC trigger and will start
        # automatically if battery gets critically low.
        discharge_floor_pct = inputs.soc_min_kwh / inputs.battery_capacity_kwh * 100
    elif current_price > 0.50 or max_price_ahead > 0.50:
        # Expensive but not spike — use full trajectory until next charge
        lookahead_steps = N
        discharge_floor_pct = soc_pct[0]
        for t in range(1, min(lookahead_steps + 1, len(soc_pct))):
            if t - 1 < N and p_charge[t - 1] > 0.05:
                break
            discharge_floor_pct = min(discharge_floor_pct, soc_pct[t])
    else:
        # Normal prices — 1 hour lookahead, responsive to changes
        lookahead_steps = min(12, N)
        discharge_floor_pct = soc_pct[0]
        for t in range(1, lookahead_steps + 1):
            if t - 1 < N and p_charge[t - 1] > 0.05:
                break
            if t < len(soc_pct):
                discharge_floor_pct = min(discharge_floor_pct, soc_pct[t])

    # For discharge/hold: set register to the floor (allows ESS to discharge)
    # For grid_charge: set register above current SoC (forces grid charge)
    # For solar_charge: set register AT current SoC (let solar charge naturally
    #   without triggering grid import — the ESS will accept solar regardless
    #   of register value, but setting register > SoC forces grid to fill the gap)
    #
    # CRITICAL: The Victron ESS treats register > current SoC as "charge to
    # this level by any means". If solar can't keep up, it pulls from grid.
    # Only set register > SoC when we WANT grid charging.
    if p_charge[0] > 0.05 and grid_import[0] > inputs.load_forecast_kw[0] + 0.1:
        # Grid charge mode — optimizer explicitly plans grid import above load.
        # Set register ABOVE current SoC to force ESS to charge from grid.
        target_register = _soc_to_register(target_soc_pct)
    elif p_charge[0] > 0.05:
        # Solar charge — battery charging from solar excess.
        # Set register to the HARD FLOOR (soc_min), not the trajectory.
        # The ESS charges naturally from solar when production > load.
        # Register must be well below current SoC to prevent grid import.
        #
        # CRITICAL: The Victron ESS imports from grid whenever register >= SoC.
        # Even register = SoC triggers "maintain" mode which pulls from grid
        # to compensate for load. Only register << SoC gives true solar-only.
        soc_floor_pct = inputs.soc_min_kwh / inputs.battery_capacity_kwh * 100
        target_register = _soc_to_register(soc_floor_pct)
    else:
        # Discharge or hold — set register to the discharge floor.
        target_register = _soc_to_register(discharge_floor_pct)

    # Determine mode
    mode, reason = _determine_mode(
        p_charge[0], p_discharge[0], grid_import[0], grid_export[0],
        solar_used[0], inputs.load_forecast_kw[0],
        inputs.buy_price[0], inputs.sell_price[0],
        soc_pct[0], target_soc_pct,
    )

    # Cost breakdown
    grid_cost = float(np.sum(grid_import * np.array(inputs.buy_price) * dt))
    export_revenue = float(np.sum(grid_export * np.array(inputs.sell_price) * dt))
    wear_cost = float(np.sum(p_discharge * inputs.battery_wear_cost * dt))

    # Effective price — what electricity is "worth" right now based on MPC plan
    effective_price = _compute_effective_price(
        grid_import[0], grid_export[0], solar_used[0],
        inputs.solar_forecast_kw[0], inputs.buy_price[0], inputs.sell_price[0],
    )

    return OptOutput(
        status="optimal",
        target_soc_pct=round(target_soc_pct, 1),
        target_register=target_register,
        mode=mode,
        reason=reason,
        soc_trajectory_pct=[round(s, 1) for s in soc_pct],
        charge_schedule_kw=[round(float(v), 2) for v in p_charge],
        discharge_schedule_kw=[round(float(v), 2) for v in p_discharge],
        grid_import_schedule_kw=[round(float(v), 2) for v in grid_import],
        grid_export_schedule_kw=[round(float(v), 2) for v in grid_export],
        solar_used_schedule_kw=[round(float(v), 2) for v in solar_used],
        total_cost=round(grid_cost - export_revenue + wear_cost, 4),
        cost_breakdown={
            "grid_cost": round(grid_cost, 4),
            "export_revenue": round(export_revenue, 4),
            "wear_cost": round(wear_cost, 4),
        },
        effective_price=round(effective_price, 4),
        solver_status=str(result.status),
        solve_time_ms=round(solve_ms, 1),
    )


def _build_fallback(inputs: OptInput, error_msg: str, solve_ms: float) -> OptOutput:
    """Safe fallback when solver fails."""
    N = inputs.horizon_steps
    current_pct = inputs.battery_soc_kwh / inputs.battery_capacity_kwh * 100
    # Conservative: hold at 40% overnight, 30% daytime
    fallback_pct = 40.0 if inputs.sunset_step is None else 30.0
    target_pct = max(fallback_pct, current_pct)

    return OptOutput(
        status="fallback",
        target_soc_pct=round(target_pct, 1),
        target_register=_soc_to_register(target_pct),
        mode="hold",
        reason=f"Solver failed ({error_msg}), using safe fallback",
        soc_trajectory_pct=[round(target_pct, 1)] * (N + 1),
        charge_schedule_kw=[0.0] * N,
        discharge_schedule_kw=[0.0] * N,
        grid_import_schedule_kw=[0.0] * N,
        grid_export_schedule_kw=[0.0] * N,
        solar_used_schedule_kw=[0.0] * N,
        total_cost=0.0,
        cost_breakdown={"grid_cost": 0, "export_revenue": 0, "wear_cost": 0},
        effective_price=0.30,
        solver_status=f"failed: {error_msg}",
        solve_time_ms=round(solve_ms, 1),
    )


def _soc_to_register(soc_pct: float) -> int:
    """Convert SoC percentage to Victron Register 2901 value.

    Register 2901 stores SoC as percentage * 10.
    Value 200 = 20%, 1000 = 100%.
    Clamped to valid range [100, 1000].
    """
    raw = int(round(soc_pct * 10))
    return max(100, min(1000, raw))


def _determine_mode(
    p_charge: float, p_discharge: float,
    grid_import: float, grid_export: float,
    solar_used: float, load_kw: float,
    buy_price: float, sell_price: float,
    current_soc_pct: float, target_soc_pct: float,
) -> tuple[str, str]:
    """Determine operating mode and human-readable reason."""
    threshold = 0.1  # kW threshold for "significant" power flow

    if p_charge > threshold and grid_import > load_kw + threshold:
        delta = target_soc_pct - current_soc_pct
        return "grid_charge", (
            f"Charging from grid at ${buy_price:.2f}/kWh "
            f"(+{delta:.0f}% SoC, {p_charge:.1f}kW)"
        )

    if p_charge > threshold and solar_used > threshold:
        return "solar_charge", (
            f"Solar charging at {solar_used:.1f}kW, "
            f"battery {current_soc_pct:.0f}% -> {target_soc_pct:.0f}%"
        )

    if p_discharge > threshold:
        savings = buy_price - sell_price
        return "discharge", (
            f"Discharging at {p_discharge:.1f}kW to avoid "
            f"${buy_price:.2f}/kWh grid (saving ${savings:.2f}/kWh)"
        )

    if grid_export > threshold:
        return "export", (
            f"Exporting {grid_export:.1f}kW to grid at ${sell_price:.2f}/kWh"
        )

    return "hold", (
        f"Holding at {current_soc_pct:.0f}% SoC, "
        f"grid at ${buy_price:.2f}/kWh"
    )


def _compute_effective_price(
    grid_import: float, grid_export: float,
    solar_used: float, solar_available: float,
    buy_price: float, sell_price: float,
) -> float:
    """Determine effective electricity price based on current MPC state.

    This value can be used to control other devices (e.g., hot water,
    EV charging) based on whether electricity is currently "cheap".
    """
    threshold = 0.1
    if grid_import > threshold:
        return buy_price
    if grid_export > threshold:
        return sell_price
    if solar_used < solar_available - threshold:
        # Solar being curtailed — extra usage is free
        return 0.0
    return buy_price
