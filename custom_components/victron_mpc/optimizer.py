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

# Hysteresis for BMS integer rounding — don't grid_charge for ≤1% dip below floor
GRID_CHARGE_HYSTERESIS_PCT = 1.0

# Mode persistence threshold (kW) — suppress mode changes between non-grid-charge
# modes when net power flow is below this value. Reduces daytime thrashing
# caused by LP re-solving every 5 min with small SoC/price changes. GitHub #78.
MODE_PERSISTENCE_THRESHOLD_KW = 0.3


@dataclass
class OptInput:
    """All inputs for a single optimization run."""

    # Horizon
    horizon_steps: int  # Number of 5-min steps (288 for 24h)
    dt_hours: float  # Timestep in hours (5/60)

    # Battery state
    battery_soc_kwh: float  # Current SoC in kWh
    battery_capacity_kwh: float
    soc_min_kwh: float  # Hard floor in kWh (absolute minimum, hardware safety)
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

    # Soft floor band — penalty-based preferred minimum (30%), above hard floor.
    # LP prefers SoC above soft floor but won't panic-buy expensive grid to recover.
    soc_soft_floor_kwh: float = 0.0  # 0 = disabled, >0 = penalty-based soft floor
    soft_floor_penalty: float = 0.0  # $/kWh/h penalty for SoC below soft floor

    # Overnight preservation — reward for maintaining SoC during overnight hours
    overnight_hold_reward: float = 0.0
    overnight_steps: list[int] | None = None  # Step indices that are "overnight"

    # Time-varying SoC floor — per-step minimum SoC in kWh.
    # If provided (length = horizon_steps), overrides soc_min_kwh per step.
    # Used to enforce higher minimums during overnight hours.
    soc_min_schedule_kwh: list[float] | None = None

    # Grid-charge boost — incentivize charging when grid is cheap.
    grid_charge_boost: float = 0.0

    # Cell balancing — force charge to 100% for BMS health
    force_full_charge: bool = False

    # Hard sunset SoC constraint — LP must reach this % by sunset_step.
    # LP finds cheapest path to target. 0 = disabled.
    sunset_soc_target_pct: float = 0.0

    # Time-varying SoC target — reward for battery fullness at each step.
    # Replaces: sunset_reward, terminal_reward, overnight_hold_reward, grid_charge_boost
    # When set (length = horizon_steps), the unified reward is used instead of
    # the legacy separate reward mechanisms. None = use legacy rewards.
    soc_target_reward: list[float] | None = None

    # Previous mode — used for mode persistence to reduce daytime thrashing.
    # When set, marginal mode changes between non-grid-charge modes are suppressed.
    previous_mode: str | None = None


@dataclass
class OptOutput:
    """Complete optimization results."""

    status: str  # "optimal", "fallback", "error"
    target_soc_pct: float  # Recommended SoC % for next interval
    target_register: int  # Register 2901 value (100-1000)
    mode: str  # "grid_charge", "solar_charge", "discharge", "hold"
    reason: str  # Human-readable explanation
    intent: dict  # Structured LP reasoning for GenAI validation

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
    eta_c = inputs.charge_efficiency
    eta_d = inputs.discharge_efficiency
    soc_init = inputs.battery_soc_kwh

    # Soft floor band: add slack variables if soft floor is active
    use_soft_floor = (
        inputs.soc_soft_floor_kwh > inputs.soc_min_kwh
        and inputs.soft_floor_penalty > 0
    )
    n_vars = 6 * N if use_soft_floor else 5 * N

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

    def bf(t):
        return 5 * N + t  # below_floor slack (kWh below soft floor)

    # === OBJECTIVE ===
    c = np.zeros(n_vars)
    for t in range(N):
        c[gi(t)] = float(inputs.buy_price[t]) * dt + inputs.grid_import_penalty * dt
        c[ge(t)] = -float(inputs.sell_price[t]) * dt
        c[pd(t)] = inputs.battery_wear_cost * dt

    # Soft floor penalty: cost per kWh below soft floor per step.
    # LP prefers to stay above 30% but won't panic-buy expensive grid.
    # Penalty accumulates over time — long periods below floor trigger
    # grid-charge at moderate prices, short dips are tolerated.
    if use_soft_floor:
        for t in range(N):
            c[bf(t)] = inputs.soft_floor_penalty * dt

    # === INCENTIVE REWARDS ===
    # Two paths: unified SoC target reward (new) or legacy separate rewards.
    # soc_target_reward replaces sunset/terminal/overnight/boost with a single
    # time-varying reward that values stored energy at each timestep.
    if inputs.soc_target_reward is not None:
        # Unified SoC target reward — at each step, reward having high SoC.
        for t in range(N):
            reward_t = inputs.soc_target_reward[t] if t < len(inputs.soc_target_reward) else 0.0
            if reward_t > 0:
                c[pc(t)] -= reward_t * eta_c * dt
                c[pd(t)] += reward_t * dt / eta_d

        # Force full charge still applies on top (cell balancing is independent)
        if inputs.force_full_charge:
            full_charge_reward = 2.0
            for k in range(N):
                c[pc(k)] -= full_charge_reward * eta_c * dt
                c[pd(k)] += full_charge_reward * dt / eta_d
    else:
        # Legacy rewards — only active if soc_target_reward is not set.
        # Sunset reward: encourage full battery at sunset
        if inputs.sunset_step is not None and 0 < inputs.sunset_step < N:
            for k in range(inputs.sunset_step):
                c[pc(k)] -= inputs.sunset_reward * eta_c * dt
                c[pd(k)] += inputs.sunset_reward * dt / eta_d

        # Terminal reward: encourage reasonable SoC at end of horizon
        for k in range(N):
            c[pc(k)] -= inputs.terminal_reward * eta_c * dt
            c[pd(k)] += inputs.terminal_reward * dt / eta_d

        # Force full charge: strong reward for reaching max SoC (cell balancing).
        if inputs.force_full_charge:
            full_charge_reward = 2.0  # $/kWh — much higher than any electricity price
            for k in range(N):
                c[pc(k)] -= full_charge_reward * eta_c * dt
                c[pd(k)] += full_charge_reward * dt / eta_d

        # Overnight hold reward: discourage discharge during overnight hours.
        if inputs.overnight_hold_reward > 0 and inputs.overnight_steps:
            morning_step = inputs.overnight_steps[-1]
            if 0 < morning_step < N:
                for k in range(morning_step):
                    c[pc(k)] -= inputs.overnight_hold_reward * eta_c * dt
                    c[pd(k)] += inputs.overnight_hold_reward * dt / eta_d

        # Grid-charge boost: incentivize charging when grid is cheap.
        if inputs.grid_charge_boost > 0 and len(inputs.buy_price) > 0:
            avg_price = sum(float(p) for p in inputs.buy_price) / len(inputs.buy_price)
            for t in range(N):
                price_t = float(inputs.buy_price[t])
                if price_t < avg_price and avg_price > 0:
                    bonus = inputs.grid_charge_boost * (avg_price - price_t) / avg_price
                    c[pc(t)] -= bonus * eta_c * dt

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
    # Hard floor: absolute constraint — never go below this.
    # Soft floor: penalty-based — LP prefers to stay above but won't
    # panic-buy expensive grid to recover. Uses slack variables.
    #
    # Clamp hard floor to current SoC if already below — prevents infeasibility.
    effective_hard_floor = min(inputs.soc_min_kwh, soc_init)
    soc_min_schedule = inputs.soc_min_schedule_kwh
    if soc_min_schedule is None:
        soc_min_schedule = [effective_hard_floor] * N
    else:
        soc_min_schedule = [min(v, soc_init) for v in soc_min_schedule]

    # Sunset SoC constraint: soc[sunset_step] >= target
    use_sunset_constraint = (
        inputs.sunset_step is not None
        and 0 < inputs.sunset_step < N
        and inputs.sunset_soc_target_pct > 0
    )

    # Calculate n_ub accounting for all constraint types
    n_ub = 2 * N  # upper + hard lower per step
    if use_soft_floor:
        n_ub += N
    if use_sunset_constraint:
        n_ub += 1

    A_ub = np.zeros((n_ub, n_vars))
    b_ub = np.zeros(n_ub)

    for t in range(1, N + 1):
        # Upper SoC bound: cumulative energy <= soc_max - soc_init
        for k in range(t):
            A_ub[t - 1, pc(k)] = eta_c * dt
            A_ub[t - 1, pd(k)] = -dt / eta_d
        b_ub[t - 1] = inputs.soc_max_kwh - soc_init

        # Hard lower SoC bound: -cumulative energy <= soc_init - hard_floor
        hard_floor_t = soc_min_schedule[min(t - 1, len(soc_min_schedule) - 1)]
        for k in range(t):
            A_ub[N + t - 1, pc(k)] = -eta_c * dt
            A_ub[N + t - 1, pd(k)] = dt / eta_d
        b_ub[N + t - 1] = soc_init - hard_floor_t

    # Soft floor constraints: slack variable absorbs the deficit.
    # -cumsum[t] - below_floor[t] <= soc_init - soft_floor
    # LP minimizes penalty * below_floor, so it prefers SoC above soft floor
    # but can go below if grid-charging is too expensive.
    if use_soft_floor:
        soft_floor = inputs.soc_soft_floor_kwh
        for t in range(1, N + 1):
            row = 2 * N + t - 1
            for k in range(t):
                A_ub[row, pc(k)] = -eta_c * dt
                A_ub[row, pd(k)] = dt / eta_d
            A_ub[row, bf(t - 1)] = -1.0
            b_ub[row] = soc_init - soft_floor

    # Sunset SoC hard constraint: -cumsum[sunset] <= soc_init - target
    # Forces LP to reach sunset_soc_target_pct by sunset_step.
    if use_sunset_constraint:
        sunset_row = n_ub - 1
        sunset_target_kwh = inputs.sunset_soc_target_pct / 100.0 * inputs.battery_capacity_kwh
        # Clamp to capacity to prevent overshoot
        effective_target = min(sunset_target_kwh, inputs.soc_max_kwh)
        for k in range(inputs.sunset_step):
            A_ub[sunset_row, pc(k)] = -eta_c * dt
            A_ub[sunset_row, pd(k)] = dt / eta_d
        b_ub[sunset_row] = soc_init - effective_target

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
    # Soft floor slack: 0 to band width (soft_floor - hard_floor)
    if use_soft_floor:
        band_width = inputs.soc_soft_floor_kwh - inputs.soc_min_kwh
        for t in range(N):
            bounds.append((0, max(0, band_width)))

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
    max_price_ahead = max(float(p) for p in inputs.buy_price[:min(12, N)])
    current_price = float(inputs.buy_price[0])
    is_spike = current_price > 1.00 or max_price_ahead > 1.00

    if is_spike:
        # SPIKE: set floor to hard minimum. Drain battery as low as possible.
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

    # Register mapping — 3-way logic for grid_charge vs solar_charge vs discharge.
    #
    # CRITICAL: The Victron ESS treats register > current SoC as "charge to
    # this level by any means". If solar can't keep up, it pulls from grid.
    # Only set register > SoC when we WANT grid charging.
    hysteresis_hold = False
    if (p_charge[0] > 0.05
            and grid_import[0] > inputs.load_forecast_kw[0] + 0.1
            and inputs.solar_forecast_kw[0] < inputs.load_forecast_kw[0]):
        # Grid charge mode — but check for BMS hysteresis
        soc_gap = target_soc_pct - soc_pct[0]
        if soc_gap <= GRID_CHARGE_HYSTERESIS_PCT:
            # Minor BMS rounding dip — hold instead of grid_charge
            soc_floor_pct = inputs.soc_min_kwh / inputs.battery_capacity_kwh * 100
            register_floor = max(10.0, soc_floor_pct - 1.0)
            target_register = _soc_to_register(register_floor)
            hysteresis_hold = True
        else:
            target_register = _soc_to_register(target_soc_pct)
    elif p_charge[0] > 0.05:
        # Solar charge — solar excess is charging battery.
        # Set register to hard floor so ESS doesn't pull from grid.
        soc_floor_pct = inputs.soc_min_kwh / inputs.battery_capacity_kwh * 100
        # Register 1% below floor prevents ESS grid pull when register ~ SoC.
        register_floor = max(10.0, soc_floor_pct - 1.0)
        target_register = _soc_to_register(register_floor)
        # Always target 100% when solar is available. Free solar should never
        # be curtailed. Every kWh stored avoids grid tomorrow.
        if (len(inputs.solar_forecast_kw) > 0
                and inputs.solar_forecast_kw[0] > inputs.load_forecast_kw[0]):
            target_soc_pct = 100.0
    else:
        # Discharge or hold — use the trajectory floor MINUS a buffer.
        # The 5% buffer prevents grid import from register ~ SoC noise.
        soc_floor_pct = inputs.soc_min_kwh / inputs.battery_capacity_kwh * 100
        register_min = max(10.0, soc_floor_pct - 1.0)
        buffered_floor = max(register_min, discharge_floor_pct - 5.0)
        target_register = _soc_to_register(buffered_floor)

    # Determine mode
    mode, reason = _determine_mode(
        p_charge[0], p_discharge[0], grid_import[0], grid_export[0],
        solar_used[0], inputs.load_forecast_kw[0],
        inputs.buy_price[0], inputs.sell_price[0],
        soc_pct[0], target_soc_pct,
        solar_forecast_kw=float(inputs.solar_forecast_kw[0]),
    )

    # Mode persistence: avoid thrashing between non-grid-charge modes
    # when power flows are marginal. Grid_charge transitions are always immediate
    # since they use different register logic (register = target, not floor).
    # GitHub issue #78.
    if (inputs.previous_mode is not None
            and mode != inputs.previous_mode
            and mode != "grid_charge"
            and inputs.previous_mode != "grid_charge"
            and abs(float(p_charge[0]) - float(p_discharge[0])) < MODE_PERSISTENCE_THRESHOLD_KW):
        reason = (
            f"Mode retained ({inputs.previous_mode}) — marginal power flow "
            f"({abs(float(p_charge[0]) - float(p_discharge[0])):.2f}kW "
            f"< {MODE_PERSISTENCE_THRESHOLD_KW}kW)"
        )
        mode = inputs.previous_mode

    # Override grid_charge to hold when hysteresis detected
    if hysteresis_hold and mode == "grid_charge":
        mode = "hold"
        reason = f"BMS rounding ({target_soc_pct - soc_pct[0]:.1f}% gap), holding at floor"

    # Cost breakdown
    grid_cost = float(np.sum(grid_import * np.array(inputs.buy_price) * dt))
    export_revenue = float(np.sum(grid_export * np.array(inputs.sell_price) * dt))
    wear_cost = float(np.sum(p_discharge * inputs.battery_wear_cost * dt))

    # Effective price — what electricity is "worth" right now based on MPC plan
    effective_price = _compute_effective_price(
        grid_import[0], grid_export[0], solar_used[0],
        inputs.solar_forecast_kw[0], inputs.buy_price[0], inputs.sell_price[0],
    )

    # Build structured intent — LP's reasoning chain for GenAI validation
    solar_next_1h = sum(float(s) for s in inputs.solar_forecast_kw[:12]) * dt
    load_next_1h = sum(float(l) for l in inputs.load_forecast_kw[:12]) * dt
    # Sum solar only to sunset (today's remaining), not full 24h horizon.
    # After sunset, sunset_step is None — check if current solar is negligible
    # to avoid reporting next-day solar as "remaining today".
    if inputs.sunset_step is not None:
        sunset_idx = inputs.sunset_step
    elif float(inputs.solar_forecast_kw[0]) < 0.1:
        # Sun has set (no current solar) — remaining today is 0
        sunset_idx = 0
    else:
        # Sunrise before sunset computed (edge case) — use full horizon
        sunset_idx = N
    solar_total_remaining = sum(float(s) for s in inputs.solar_forecast_kw[:sunset_idx]) * dt
    avg_buy_next_4h = (
        sum(float(p) for p in inputs.buy_price[:min(48, N)]) / min(48, N)
        if N > 0 else 0
    )
    peak_buy_next_4h = max(
        (float(p) for p in inputs.buy_price[:min(48, N)]), default=0
    )
    soc_at_sunset = (
        float(round(soc_pct[inputs.sunset_step], 1))
        if inputs.sunset_step is not None and inputs.sunset_step < len(soc_pct)
        else None
    )

    # Build principles assessment — evaluate operating principles against LP results
    total_cost = grid_cost - export_revenue + wear_cost
    from .principles import evaluate_principles

    # Convert soc_pct to plain floats to avoid numpy types in JSON
    soc_pct_float = [float(s) for s in soc_pct]

    principles = evaluate_principles(
        mode=mode,
        soc_pct=soc_pct_float,
        buy_price_now=float(inputs.buy_price[0]),
        solar_forecast_kw=[float(s) for s in inputs.solar_forecast_kw],
        solar_used=[float(s) for s in solar_used],
        p_discharge=[float(d) for d in p_discharge],
        battery_capacity_kwh=inputs.battery_capacity_kwh,
        total_cost=total_cost,
        grid_cost=grid_cost,
        wear_cost=wear_cost,
        sunset_step=inputs.sunset_step,
        dt=dt,
        is_spike=is_spike,
    )

    intent = {
        "action": mode,
        "why": reason,
        "key_assumptions": {
            "solar_next_1h_kwh": round(solar_next_1h, 2),
            "solar_total_remaining_kwh": round(solar_total_remaining, 2),
            "load_next_1h_kwh": round(load_next_1h, 2),
            "buy_price_now": round(float(inputs.buy_price[0]), 4),
            "avg_buy_next_4h": round(avg_buy_next_4h, 4),
            "peak_buy_next_4h": round(peak_buy_next_4h, 4),
        },
        "expected_outcomes": {
            "soc_in_1h_pct": float(round(soc_pct[min(12, len(soc_pct) - 1)], 1)),
            "soc_in_2h_pct": float(round(soc_pct[min(24, len(soc_pct) - 1)], 1)),
            "soc_at_sunset_pct": float(soc_at_sunset) if soc_at_sunset is not None else None,
            "total_cost_24h": round(grid_cost - export_revenue + wear_cost, 4),
        },
        "constraints_active": {
            "sunset_target": inputs.sunset_step is not None,
            "overnight_floor": any(
                soc_pct[t] <= inputs.soc_min_kwh / inputs.battery_capacity_kwh * 100 + 2
                for t in range(min(len(soc_pct), N))
            ),
            "spike_response": is_spike,
        },
        "principles_active": principles,
        "principles": principles,
        "override_applied": False,
    }

    return OptOutput(
        status="optimal",
        target_soc_pct=round(target_soc_pct, 1),
        target_register=target_register,
        mode=mode,
        reason=reason,
        intent=intent,
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
        intent={
            "action": "hold",
            "why": f"Solver failed: {error_msg}",
            "key_assumptions": {},
            "expected_outcomes": {},
            "constraints_active": {},
            "principles_active": [
                {
                    "id": "cost_minimisation",
                    "satisfied": False,
                    "detail": "Solver failed — using safe fallback",
                }
            ],
        },
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


    # _build_principles removed — replaced by principles.evaluate_principles()


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
    solar_forecast_kw: float = 0.0,
) -> tuple[str, str]:
    """Determine operating mode and human-readable reason.

    Args:
        solar_forecast_kw: Current solar forecast (kW). Used to guard against
            solar_charge mode when forecast is negligible (e.g. at night).
    """
    threshold = 0.1  # kW threshold for "significant" power flow
    # Solar forecast must be meaningful to qualify as solar_charge.
    # Prevents LP micro-allocations from triggering solar_charge at night
    # when Solcast provides tiny twilight/atmospheric values (0.01-0.05 kW).
    solar_min_kw = 0.2

    if p_charge > threshold and grid_import > load_kw + threshold:
        delta = target_soc_pct - current_soc_pct
        return "grid_charge", (
            f"Charging from grid at ${buy_price:.2f}/kWh "
            f"(+{delta:.0f}% SoC, {p_charge:.1f}kW)"
        )

    if (p_charge > threshold and solar_used > threshold
            and solar_forecast_kw >= solar_min_kw):
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


def compute_sunset_target(
    buy_price: list[float],
    solar_forecast_kw: list[float],
    load_forecast_kw: list[float],
    sunset_step: int | None,
    battery_capacity_kwh: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    dt_hours: float,
    *,
    max_charge_kw: float = 3.5,
    min_target_pct: float = 60.0,
    max_target_pct: float = 95.0,
    risk_buffer_pct: float = 10.0,
) -> float:
    """Compute dynamic sunset SoC target based on forecasts.

    Instead of a fixed 95%, calculate what SoC is actually needed at sunset
    to cover overnight load until tomorrow's solar can take over. Factors:

    1. Overnight load: energy needed from sunset to next solar
    2. Cheap grid opportunity: if cheap overnight, can recharge from grid
    3. Price volatility: if prices are volatile/spikey, keep more buffer

    Returns:
        Target SoC percentage at sunset, clamped to [min_target_pct, max_target_pct]
    """
    N = len(buy_price)
    if sunset_step is None or sunset_step <= 0 or sunset_step >= N:
        return max_target_pct  # After sunset or invalid — be conservative

    # === 1. Overnight load: energy needed from sunset to next solar ===
    next_solar_step = N  # Default: end of horizon
    for t in range(sunset_step, N):
        if solar_forecast_kw[t] > 0.1:
            next_solar_step = t
            break

    overnight_load_kwh = 0.0
    for t in range(sunset_step, next_solar_step):
        overnight_load_kwh += float(load_forecast_kw[t]) * dt_hours

    overnight_need_kwh = overnight_load_kwh / discharge_efficiency

    # === 2. Cheap grid opportunity ===
    cheap_grid_kwh = 0.0
    overnight_prices = [float(buy_price[t]) for t in range(sunset_step, next_solar_step) if t < N]
    if overnight_prices:
        median_price = sorted(overnight_prices)[len(overnight_prices) // 2]
        cheap_threshold = min(0.20, median_price)
        for t in range(sunset_step, next_solar_step):
            if t < N and float(buy_price[t]) <= cheap_threshold:
                cheap_grid_kwh += max_charge_kw * charge_efficiency * dt_hours

    # === 3. Price volatility buffer ===
    price_volatility_buffer = 0.0
    if len(overnight_prices) > 1:
        mean_price = sum(overnight_prices) / len(overnight_prices)
        variance = sum((p - mean_price) ** 2 for p in overnight_prices) / len(overnight_prices)
        std_dev = variance ** 0.5
        if std_dev > 0.10:
            price_volatility_buffer = 5.0
        if any(p > 0.50 for p in overnight_prices):
            price_volatility_buffer = 10.0

    # === 4. Calculate target ===
    net_need_kwh = max(0.0, overnight_need_kwh - cheap_grid_kwh)
    target_pct = (net_need_kwh / battery_capacity_kwh) * 100.0
    target_pct += risk_buffer_pct + price_volatility_buffer
    target_pct = max(min_target_pct, min(max_target_pct, target_pct))

    return round(target_pct, 1)
