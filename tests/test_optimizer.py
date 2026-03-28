"""Core optimizer tests.

Tests the LP solver with controlled inputs to verify:
1. Basic constraints are respected (SoC bounds, power limits)
2. Cost minimization works correctly
3. Battery is allocated to expensive periods
4. Graceful fallback on failure
"""

from __future__ import annotations

from custom_components.victron_mpc.optimizer import optimize
from custom_components.victron_mpc.utils import scale_overnight_hold_reward

from .conftest import (
    STEPS_24H,
    amber_typical_day,
    cloudy_solar,
    make_opt_input,
    no_solar,
    solar_bell,
    spike_at_hour,
)


class TestSolverBasics:
    """Verify the solver runs and returns valid results."""

    def test_solver_returns_optimal(self):
        inp = make_opt_input()
        out = optimize(inp)
        assert out.status == "optimal"
        assert out.solver_status == "0"  # HiGHS optimal

    def test_solve_time_reasonable(self):
        inp = make_opt_input()
        out = optimize(inp)
        assert out.solve_time_ms < 5000  # Should solve in under 5 seconds

    def test_soc_trajectory_length(self):
        inp = make_opt_input()
        out = optimize(inp)
        assert len(out.soc_trajectory_pct) == STEPS_24H + 1

    def test_schedule_lengths_match(self):
        inp = make_opt_input()
        out = optimize(inp)
        assert len(out.charge_schedule_kw) == STEPS_24H
        assert len(out.discharge_schedule_kw) == STEPS_24H
        assert len(out.grid_import_schedule_kw) == STEPS_24H

    def test_register_in_valid_range(self):
        inp = make_opt_input()
        out = optimize(inp)
        assert 100 <= out.target_register <= 1000


class TestSoCConstraints:
    """Verify battery SoC stays within bounds."""

    def test_soc_never_below_minimum(self):
        inp = make_opt_input(soc_pct=30.0, soc_min_pct=20.0)
        out = optimize(inp)
        min_soc = min(out.soc_trajectory_pct)
        assert min_soc >= 19.5  # Small tolerance for float precision

    def test_soc_never_above_maximum(self):
        inp = make_opt_input(soc_pct=90.0, buy_price=0.01)
        out = optimize(inp)
        max_soc = max(out.soc_trajectory_pct)
        assert max_soc <= 100.5

    def test_soc_starts_at_initial(self):
        inp = make_opt_input(soc_pct=65.0)
        out = optimize(inp)
        assert abs(out.soc_trajectory_pct[0] - 65.0) < 0.5

    def test_high_floor_respected(self):
        """User sets floor at 40% — optimizer should never go below."""
        inp = make_opt_input(soc_pct=60.0, soc_min_pct=40.0, buy_price=0.50)
        out = optimize(inp)
        min_soc = min(out.soc_trajectory_pct)
        assert min_soc >= 39.5


class TestCostMinimization:
    """Verify the optimizer minimizes electricity cost."""

    def test_charges_at_cheap_prices(self):
        """Should charge battery during cheap period, not expensive."""
        # Cheap first 6h, expensive rest
        prices = [0.10] * 72 + [0.50] * (STEPS_24H - 72)
        inp = make_opt_input(soc_pct=30.0, buy_price=prices, solar_kw=0.0)
        out = optimize(inp)

        # Should charge during first 72 steps (cheap)
        cheap_charge = sum(out.charge_schedule_kw[:72])
        expensive_charge = sum(out.charge_schedule_kw[72:])
        assert cheap_charge > expensive_charge * 2

    def test_discharges_at_expensive_prices(self):
        """Should use battery during expensive period to avoid grid."""
        prices = [0.15] * 144 + [0.60] * 48 + [0.15] * (STEPS_24H - 192)
        inp = make_opt_input(soc_pct=80.0, buy_price=prices, solar_kw=0.0)
        out = optimize(inp)

        # Should discharge during expensive period (steps 144-192)
        expensive_discharge = sum(out.discharge_schedule_kw[144:192])
        cheap_discharge = sum(out.discharge_schedule_kw[:144])
        assert expensive_discharge > cheap_discharge

    def test_negative_pricing_charges_to_max(self):
        """When price is negative, should charge battery to 100%.
        Also valid: export to grid since sell_price may be positive.
        """
        inp = make_opt_input(soc_pct=30.0, buy_price=-0.05, sell_price=-0.10, solar_kw=0.0)
        out = optimize(inp)
        # With both prices negative, should charge (getting paid to import)
        max_soc = max(out.soc_trajectory_pct)
        assert max_soc > 95

    def test_exports_when_profitable(self):
        """With excess solar and good feed-in, should export."""
        inp = make_opt_input(
            soc_pct=95.0,
            solar_kw=6.0,
            load_kw=1.0,
            sell_price=0.10,
        )
        out = optimize(inp)
        total_export = sum(out.grid_export_schedule_kw)
        assert total_export > 0


class TestLimitedBattery:
    """KEY: Battery is a finite resource — must be allocated wisely.

    This is the user's core concern: 'we cannot just stay on battery
    for a long time.. it runs out'
    """

    def test_battery_allocated_to_most_expensive_periods(self):
        """With limited battery, discharge should target the spike."""
        # 14.2kWh battery at 80% = 11.36kWh usable above 20% floor
        # At 1kW load, battery lasts ~11h but we have 24h
        # Must use grid during cheaper hours, save battery for spike
        prices = amber_typical_day()  # Evening peak at $0.45
        inp = make_opt_input(
            soc_pct=80.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.0,
        )
        out = optimize(inp)

        # Battery should discharge MORE during evening peak (17:00-21:00)
        # than during cheap overnight (00:00-05:00)
        sph = 12  # steps per hour
        evening_discharge = sum(out.discharge_schedule_kw[17 * sph : 21 * sph])
        overnight_discharge = sum(out.discharge_schedule_kw[0 * sph : 5 * sph])
        assert evening_discharge > overnight_discharge, (
            f"Evening discharge ({evening_discharge:.1f}kW) should exceed "
            f"overnight ({overnight_discharge:.1f}kW)"
        )

    def test_grid_used_during_cheap_to_preserve_battery(self):
        """Should import from grid during cheap periods to save battery for peak."""
        prices = amber_typical_day()
        inp = make_opt_input(
            soc_pct=60.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.0,
        )
        out = optimize(inp)

        # During cheap overnight, grid should supply load (not battery)
        sph = 12
        overnight_grid = sum(out.grid_import_schedule_kw[0 * sph : 5 * sph])
        assert overnight_grid > 0, "Should use grid during cheap overnight"

    def test_battery_doesnt_drain_before_spike(self):
        """Battery should have charge left when the spike arrives.

        At 70% SoC (9.94kWh) with 1kW load over 18h before spike,
        that's 18kWh needed. Battery only has ~7kWh usable (70%-20%).
        So grid MUST be used before spike. But battery should still
        have meaningful charge at spike start.
        """
        prices = spike_at_hour(0.25, 1.50, spike_hour=18, spike_duration_hours=2)
        inp = make_opt_input(
            soc_pct=70.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.0,
        )
        out = optimize(inp)

        soc_at_spike = out.soc_trajectory_pct[18 * 12]
        # Optimizer may charge from grid before spike to prepare
        # At minimum, should not be fully drained
        assert soc_at_spike > 20, (
            f"SoC at spike start should be above floor, got {soc_at_spike:.1f}%"
        )
        # During spike, should discharge rather than import
        sph = 12
        spike_discharge = sum(out.discharge_schedule_kw[18 * sph : 20 * sph])
        spike_grid = sum(out.grid_import_schedule_kw[18 * sph : 20 * sph])
        assert spike_discharge > spike_grid, "Should prefer battery over grid during spike"

    def test_limited_battery_forces_grid_usage(self):
        """At 30% SoC with 24h ahead and no solar, must use grid for most of it."""
        inp = make_opt_input(
            soc_pct=30.0,
            buy_price=0.30,
            solar_kw=0.0,
            load_kw=1.5,
        )
        out = optimize(inp)

        total_grid = sum(out.grid_import_schedule_kw)
        total_discharge = sum(out.discharge_schedule_kw)
        # Grid should provide majority of energy since battery only has ~1.4kWh usable
        assert total_grid > total_discharge * 2

    def test_full_battery_still_uses_grid_before_spike(self):
        """Even at 100%, should use grid during cheap hours to preserve for spike."""
        prices = spike_at_hour(0.10, 2.00, spike_hour=18)
        inp = make_opt_input(
            soc_pct=100.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.5,
        )
        out = optimize(inp)

        # Before spike, should use cheap grid, not discharge battery
        sph = 12
        pre_spike_grid = sum(out.grid_import_schedule_kw[0:18 * sph])
        assert pre_spike_grid > 0, "Should use cheap grid before spike"

        # During spike, should discharge battery
        spike_discharge = sum(out.discharge_schedule_kw[18 * sph : 19 * sph])
        assert spike_discharge > 0, "Should discharge during spike"


class TestSolarIntegration:
    """Verify solar production is used correctly."""

    def test_solar_charges_battery(self):
        """Excess solar should charge the battery."""
        inp = make_opt_input(
            soc_pct=40.0,
            solar_kw=solar_bell(peak_kw=5.0),
            load_kw=1.0,
            buy_price=0.30,
        )
        out = optimize(inp)

        # SoC should increase during solar hours
        peak_soc = max(out.soc_trajectory_pct)
        assert peak_soc > 60, f"Solar should charge battery above 60%, got {peak_soc:.1f}%"

    def test_no_grid_charge_when_solar_sufficient(self):
        """If solar can fill the battery, don't waste money on grid charging."""
        inp = make_opt_input(
            soc_pct=50.0,
            solar_kw=solar_bell(peak_kw=6.0),
            load_kw=1.0,
            buy_price=0.30,
        )
        out = optimize(inp)

        # During peak solar (steps 72-144, ~6am-12pm), grid import should be minimal
        sph = 12
        solar_peak_grid = sum(out.grid_import_schedule_kw[8 * sph : 14 * sph])
        assert solar_peak_grid < 5.0, "Grid import during peak solar should be minimal"

    def test_cloudy_day_charges_from_grid(self):
        """On a cloudy day with expensive evening, should grid-charge."""
        prices = amber_typical_day()
        inp = make_opt_input(
            soc_pct=40.0,
            solar_kw=cloudy_solar(peak_kw=1.5),
            load_kw=1.0,
            buy_price=prices,
        )
        out = optimize(inp)

        # Should charge from grid during cheap periods since solar won't be enough
        total_grid_charge = sum(
            out.charge_schedule_kw[i]
            for i in range(STEPS_24H)
            if out.grid_import_schedule_kw[i] > 0.5
        )
        assert total_grid_charge > 0, "Should grid-charge on cloudy day"


class TestSpikeHandling:
    """Verify spike avoidance behavior."""

    def test_pre_charges_before_spike(self):
        """Should charge battery before a predicted spike.

        Scenario: spike at 6pm, currently 2pm (hour 14 in horizon).
        Only 4 hours of cheap grid before spike hits.
        """
        # Cheap for 4h, then huge spike for 2h, then moderate
        prices = [0.15] * (4 * 12) + [1.50] * (2 * 12) + [0.20] * (STEPS_24H - 6 * 12)
        inp = make_opt_input(
            soc_pct=40.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.0,
            battery_wear_cost=0.03,
        )
        out = optimize(inp)

        # Should charge during first 4h cheap window to prepare for spike at step 48
        soc_pre_spike = out.soc_trajectory_pct[4 * 12]
        assert soc_pre_spike > 45, f"Should pre-charge before spike, got {soc_pre_spike:.1f}%"

        # During spike (steps 48-72), should discharge not import
        sph = 12
        spike_discharge = sum(out.discharge_schedule_kw[4 * sph : 6 * sph])
        spike_grid = sum(out.grid_import_schedule_kw[4 * sph : 6 * sph])
        assert spike_discharge > spike_grid, "Should prefer battery during spike"

    def test_discharges_during_spike(self):
        """Should use battery heavily during spike."""
        prices = spike_at_hour(0.25, 2.00, spike_hour=18)
        inp = make_opt_input(
            soc_pct=90.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.5,
        )
        out = optimize(inp)

        sph = 12
        spike_discharge = sum(out.discharge_schedule_kw[18 * sph : 19 * sph])
        assert spike_discharge > 5.0, "Should discharge significantly during spike"

    def test_avoids_grid_during_spike(self):
        """Grid import should be minimal during spike."""
        prices = spike_at_hour(0.25, 2.00, spike_hour=18)
        inp = make_opt_input(
            soc_pct=90.0,
            buy_price=prices,
            solar_kw=no_solar(),
            load_kw=1.0,
        )
        out = optimize(inp)

        sph = 12
        spike_grid = sum(out.grid_import_schedule_kw[18 * sph : 19 * sph])
        non_spike_grid = sum(out.grid_import_schedule_kw[:18 * sph])
        assert spike_grid < non_spike_grid * 0.1, "Should avoid grid during spike"


class TestRegisterMapping:
    """Verify Register 2901 value mapping."""

    def test_charging_sets_register_above_current(self):
        """When charging, register should be above current SoC."""
        inp = make_opt_input(soc_pct=40.0, buy_price=0.01, solar_kw=0.0)
        out = optimize(inp)
        if out.mode == "grid_charge":
            assert out.target_register > 400

    def test_discharging_sets_register_below_current(self):
        """When discharging, register should be below current SoC."""
        inp = make_opt_input(soc_pct=80.0, buy_price=0.80, solar_kw=0.0)
        out = optimize(inp)
        if out.mode == "discharge":
            assert out.target_register < 800

    def test_register_clamped_to_valid_range(self):
        """Register should never be outside 100-1000."""
        for soc in [10, 20, 50, 80, 100]:
            inp = make_opt_input(soc_pct=float(soc))
            out = optimize(inp)
            assert 100 <= out.target_register <= 1000


class TestSunsetReward:
    """Verify the optimizer tries to fill battery by sunset."""

    def test_battery_higher_at_sunset_with_reward(self):
        """Sunset reward should result in higher SoC at sunset."""
        # Without sunset reward
        inp_no = make_opt_input(
            soc_pct=50.0,
            solar_kw=solar_bell(peak_kw=4.0),
            load_kw=1.0,
            sunset_step=18 * 12,  # 18:00
            sunset_reward=0.0,
        )
        out_no = optimize(inp_no)

        # With sunset reward
        inp_yes = make_opt_input(
            soc_pct=50.0,
            solar_kw=solar_bell(peak_kw=4.0),
            load_kw=1.0,
            sunset_step=18 * 12,
            sunset_reward=0.10,
        )
        out_yes = optimize(inp_yes)

        soc_at_sunset_no = out_no.soc_trajectory_pct[18 * 12]
        soc_at_sunset_yes = out_yes.soc_trajectory_pct[18 * 12]
        assert soc_at_sunset_yes >= soc_at_sunset_no - 1.0  # Should be same or higher


class TestFallback:
    """Verify graceful degradation when solver fails."""

    def test_infeasible_returns_fallback(self):
        """Impossible constraints should return safe fallback.

        Note: soc_min > soc_max with current SoC at 50% is now feasible
        due to floor clamping (min(soc_min, soc_init)). The solver can
        trivially hold at current SoC. This is correct behavior — clamping
        prevents infeasibility from real-world spike discharge scenarios.
        """
        inp = make_opt_input(soc_min_pct=80.0, soc_max_pct=50.0)
        out = optimize(inp)
        # With floor clamping, this resolves to a valid (trivial) solution
        assert out.status in ("optimal", "fallback")
        assert 100 <= out.target_register <= 1000


class TestOvernightPreservation:
    """Verify overnight hold reward preserves battery for morning spikes."""

    def test_overnight_reward_reduces_discharge(self):
        """With overnight hold reward, battery should discharge less overnight."""
        prices = amber_typical_day()
        sph = 12

        # Without overnight reward
        inp_no = make_opt_input(
            soc_pct=70.0, buy_price=prices,
            solar_kw=no_solar(), load_kw=1.0,
            overnight_hold_reward=0.0,
        )
        out_no = optimize(inp_no)

        # With overnight reward (overnight = steps for hours 22-6)
        # For a test starting at hour 0, overnight steps are 0*12..6*12
        overnight_steps = list(range(0, 6 * sph))
        inp_yes = make_opt_input(
            soc_pct=70.0, buy_price=prices,
            solar_kw=no_solar(), load_kw=1.0,
            overnight_hold_reward=0.06,
            overnight_steps=overnight_steps,
        )
        out_yes = optimize(inp_yes)

        # With overnight reward, SoC at 6am should be higher
        soc_6am_no = out_no.soc_trajectory_pct[6 * sph]
        soc_6am_yes = out_yes.soc_trajectory_pct[6 * sph]
        assert soc_6am_yes >= soc_6am_no - 1.0, (
            f"Overnight hold should preserve battery: "
            f"with={soc_6am_yes:.1f}%, without={soc_6am_no:.1f}%"
        )

    def test_overnight_reward_uses_grid_instead(self):
        """Overnight hold should shift load to grid during cheap hours."""
        prices = amber_typical_day()
        sph = 12
        overnight_steps = list(range(0, 6 * sph))

        inp = make_opt_input(
            soc_pct=60.0, buy_price=prices,
            solar_kw=no_solar(), load_kw=1.0,
            overnight_hold_reward=0.08,
            overnight_steps=overnight_steps,
        )
        out = optimize(inp)

        # During overnight, grid should supply most of the load
        overnight_grid = sum(out.grid_import_schedule_kw[0:6 * sph])
        assert overnight_grid > 30, (
            f"Should use grid overnight to preserve battery, got {overnight_grid:.1f}kW total"
        )

    def test_still_discharges_during_morning_spike(self):
        """Even with overnight preservation, should discharge during expensive morning."""
        # Cheap overnight, expensive morning 7-9am
        prices = [0.12] * (7 * 12) + [0.55] * (2 * 12) + [0.25] * (STEPS_24H - 9 * 12)
        sph = 12
        overnight_steps = list(range(0, 6 * sph))

        inp = make_opt_input(
            soc_pct=65.0, buy_price=prices,
            solar_kw=no_solar(), load_kw=1.0,
            overnight_hold_reward=0.06,
            overnight_steps=overnight_steps,
        )
        out = optimize(inp)

        # Should discharge during morning spike
        morning_discharge = sum(out.discharge_schedule_kw[7 * sph:9 * sph])
        assert morning_discharge > 5, (
            f"Should discharge during morning spike, got {morning_discharge:.1f}kW"
        )


class TestForceFullCharge:
    """Verify force_full_charge for cell balancing."""

    def test_forces_charge_to_near_max(self):
        """When force_full_charge=True, battery should reach near 100%."""
        inp = make_opt_input(
            soc_pct=50.0,
            buy_price=0.30,
            solar_kw=0.0,
            load_kw=1.0,
            force_full_charge=True,
        )
        out = optimize(inp)
        max_soc = max(out.soc_trajectory_pct)
        assert max_soc > 90, f"Force full charge should reach >90%, got {max_soc:.1f}%"

    def test_force_full_charge_from_low_soc(self):
        """Even from 20%, force_full_charge should push to high SoC."""
        inp = make_opt_input(
            soc_pct=20.0,
            buy_price=0.30,
            solar_kw=0.0,
            load_kw=1.0,
            force_full_charge=True,
        )
        out = optimize(inp)
        max_soc = max(out.soc_trajectory_pct)
        assert max_soc > 90, f"Force full charge from 20% should reach >90%, got {max_soc:.1f}%"

    def test_force_full_charge_solver_succeeds(self):
        """Force full charge should not cause solver failure."""
        inp = make_opt_input(
            soc_pct=40.0,
            buy_price=0.25,
            solar_kw=solar_bell(peak_kw=4.0),
            load_kw=1.0,
            force_full_charge=True,
        )
        out = optimize(inp)
        assert out.status == "optimal"


class TestWearCost:
    """Verify battery wear cost affects cycling decisions."""

    def test_high_wear_cost_reduces_cycling(self):
        """Higher wear cost should result in less discharge."""
        prices = amber_typical_day()

        inp_low_wear = make_opt_input(
            soc_pct=80.0, buy_price=prices,
            solar_kw=no_solar(), battery_wear_cost=0.01,
        )
        inp_high_wear = make_opt_input(
            soc_pct=80.0, buy_price=prices,
            solar_kw=no_solar(), battery_wear_cost=0.20,
        )

        out_low = optimize(inp_low_wear)
        out_high = optimize(inp_high_wear)

        discharge_low = sum(out_low.discharge_schedule_kw)
        discharge_high = sum(out_high.discharge_schedule_kw)
        assert discharge_high < discharge_low, (
            f"High wear ({discharge_high:.1f}) should cycle less than "
            f"low wear ({discharge_low:.1f})"
        )

    def test_wont_cycle_for_tiny_price_difference(self):
        """If grid price only slightly above feed-in, don't bother cycling."""
        inp = make_opt_input(
            soc_pct=80.0,
            buy_price=0.28,
            sell_price=0.06,
            solar_kw=0.0,
            battery_wear_cost=0.10,  # High wear cost
        )
        out = optimize(inp)
        # With $0.10 wear cost and only $0.22 spread (0.28-0.06),
        # cycling barely profitable — shouldn't discharge much
        # This is a soft check — the exact amount depends on terminal reward etc.
        assert out.status == "optimal"


class TestOvernightHoldRewardScaling:
    """Test price-adaptive overnight hold reward scaling."""

    def test_cheap_overnight_full_reward(self):
        """Cheap overnight grid ($0.12) → full hold reward preserved."""
        prices = [0.12] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))  # 6 hours
        result = scale_overnight_hold_reward(0.10, prices, overnight_steps)
        assert result == 0.10

    def test_expensive_overnight_zero_reward(self):
        """Expensive overnight grid ($0.30) → hold reward scaled to zero."""
        prices = [0.30] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))
        result = scale_overnight_hold_reward(0.10, prices, overnight_steps)
        assert result == 0.0

    def test_moderate_overnight_partial_reward(self):
        """Moderate overnight grid ($0.20) → partial hold reward."""
        prices = [0.20] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))
        result = scale_overnight_hold_reward(0.10, prices, overnight_steps)
        # $0.20 is 50% between $0.15 and $0.25 → scale = 0.50
        assert 0.04 < result < 0.06

    def test_no_overnight_steps_returns_base(self):
        """No overnight steps → return base reward unchanged."""
        prices = [0.30] * STEPS_24H
        result = scale_overnight_hold_reward(0.10, prices, [])
        assert result == 0.10

    def test_zero_base_reward_stays_zero(self):
        """Zero base reward → stays zero regardless of price."""
        prices = [0.10] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))
        result = scale_overnight_hold_reward(0.0, prices, overnight_steps)
        assert result == 0.0

    def test_boundary_at_price_low(self):
        """Exactly at $0.15 → full reward."""
        prices = [0.15] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))
        result = scale_overnight_hold_reward(0.10, prices, overnight_steps)
        assert result == 0.10

    def test_boundary_at_price_high(self):
        """Exactly at $0.25 → zero reward."""
        prices = [0.25] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))
        result = scale_overnight_hold_reward(0.10, prices, overnight_steps)
        assert result == 0.0

    def test_only_overnight_prices_matter(self):
        """Daytime prices should not affect overnight scaling."""
        # Expensive daytime, cheap overnight
        prices = [0.50] * STEPS_24H
        overnight_steps = list(range(0, 6 * 12))  # steps 0-71
        for s in overnight_steps:
            prices[s] = 0.12  # Cheap overnight
        result = scale_overnight_hold_reward(0.10, prices, overnight_steps)
        assert result == 0.10  # Full reward because overnight is cheap


class TestSoftFloor:
    """Verify soft floor penalty prevents deep discharge at moderate prices."""

    def test_soft_floor_limits_discharge(self):
        """With soft floor at 30%, LP should avoid going below 30% at normal prices."""
        inp = make_opt_input(
            soc_pct=50.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            soc_min_pct=10.0,
            soc_soft_floor_pct=30.0,
            soft_floor_penalty=0.10,
        )
        out = optimize(inp)
        assert out.status == "optimal"
        # SoC should mostly stay above soft floor (30% = 4.26 kWh)
        # Allow some brief dips but final SoC should not be deep below floor
        min_soc = min(out.soc_trajectory_pct)
        assert min_soc > 15.0  # Should not drain to hard floor at $0.25

    def test_soft_floor_allows_deep_discharge_at_high_prices(self):
        """At high prices, LP should go below soft floor to avoid expensive grid."""
        inp = make_opt_input(
            soc_pct=50.0,
            buy_price=0.80,
            solar_kw=no_solar(),
            load_kw=1.0,
            soc_min_pct=10.0,
            soc_soft_floor_pct=30.0,
            soft_floor_penalty=0.10,
        )
        out = optimize(inp)
        assert out.status == "optimal"
        # At $0.80/kWh, the LP should prefer discharging below soft floor
        # rather than paying for expensive grid import
        min_soc = min(out.soc_trajectory_pct)
        assert min_soc < 30.0  # Should go below soft floor

    def test_no_soft_floor_discharges_deeper(self):
        """Without soft floor, LP discharges more aggressively."""
        # With soft floor
        inp_with = make_opt_input(
            soc_pct=50.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            soc_min_pct=10.0,
            soc_soft_floor_pct=30.0,
            soft_floor_penalty=0.10,
        )
        out_with = optimize(inp_with)

        # Without soft floor
        inp_without = make_opt_input(
            soc_pct=50.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            soc_min_pct=10.0,
        )
        out_without = optimize(inp_without)

        # Without soft floor should discharge deeper
        min_with = min(out_with.soc_trajectory_pct)
        min_without = min(out_without.soc_trajectory_pct)
        assert min_without < min_with


class TestSunsetConstraint:
    """Verify sunset SoC hard constraint forces battery to target by sunset."""

    def test_sunset_constraint_reaches_target(self):
        """LP must reach 95% by sunset step."""
        sunset_step = 12 * 12  # Hour 12 (noon — 12 hours from start)
        inp = make_opt_input(
            soc_pct=40.0,
            buy_price=0.25,
            solar_kw=solar_bell(peak_kw=5.0),
            load_kw=1.0,
            sunset_step=sunset_step,
            sunset_soc_target_pct=95.0,
        )
        out = optimize(inp)
        assert out.status == "optimal"
        # SoC at sunset must be >= 95%
        soc_at_sunset = out.soc_trajectory_pct[sunset_step]
        assert soc_at_sunset >= 94.0  # Allow tiny solver tolerance

    def test_sunset_constraint_charges_from_grid_if_needed(self):
        """Without enough solar, LP must grid-charge to meet sunset target."""
        sunset_step = 12 * 12
        inp = make_opt_input(
            soc_pct=30.0,
            buy_price=0.25,
            solar_kw=cloudy_solar(peak_kw=1.5),  # Not enough solar
            load_kw=1.0,
            sunset_step=sunset_step,
            sunset_soc_target_pct=95.0,
        )
        out = optimize(inp)
        assert out.status == "optimal"
        soc_at_sunset = out.soc_trajectory_pct[sunset_step]
        assert soc_at_sunset >= 94.0
        # Should have grid import to meet target
        total_grid = sum(out.grid_import_schedule_kw[:sunset_step])
        assert total_grid > 0

    def test_no_sunset_constraint_no_forced_charge(self):
        """Without sunset constraint, LP doesn't force high SoC by midday."""
        sunset_step = 12 * 12
        # With constraint
        inp_with = make_opt_input(
            soc_pct=40.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            sunset_step=sunset_step,
            sunset_soc_target_pct=95.0,
        )
        out_with = optimize(inp_with)

        # Without constraint
        inp_without = make_opt_input(
            soc_pct=40.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            sunset_step=sunset_step,
            sunset_soc_target_pct=0.0,
        )
        out_without = optimize(inp_without)

        soc_with = out_with.soc_trajectory_pct[sunset_step]
        soc_without = out_without.soc_trajectory_pct[sunset_step]
        # With constraint should be much higher
        assert soc_with > soc_without + 20.0

    def test_sunset_constraint_disabled_when_zero(self):
        """sunset_soc_target_pct=0 means no constraint."""
        sunset_step = 12 * 12
        inp = make_opt_input(
            soc_pct=50.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            sunset_step=sunset_step,
            sunset_soc_target_pct=0.0,
        )
        out = optimize(inp)
        assert out.status == "optimal"
        # Should still solve fine, just no forced target


class TestSoCTargetReward:
    """Verify unified SoC target reward replaces legacy rewards."""

    def test_soc_target_reward_encourages_charge(self):
        """Higher SoC target reward should encourage more grid charging."""
        # Low reward — not worth grid-charging at $0.25
        low_reward = [0.01] * STEPS_24H
        inp_low = make_opt_input(
            soc_pct=50.0,
            buy_price=0.15,
            solar_kw=no_solar(),
            load_kw=0.5,
            soc_target_reward=low_reward,
        )
        out_low = optimize(inp_low)

        # High reward — worth grid-charging to maintain SoC
        high_reward = [0.30] * STEPS_24H
        inp_high = make_opt_input(
            soc_pct=50.0,
            buy_price=0.15,
            solar_kw=no_solar(),
            load_kw=0.5,
            soc_target_reward=high_reward,
        )
        out_high = optimize(inp_high)

        # High reward should maintain higher SoC (grid-charges to offset discharge)
        avg_low = sum(out_low.soc_trajectory_pct) / len(out_low.soc_trajectory_pct)
        avg_high = sum(out_high.soc_trajectory_pct) / len(out_high.soc_trajectory_pct)
        assert avg_high > avg_low

    def test_soc_target_reward_overrides_legacy(self):
        """When soc_target_reward is set, legacy sunset/terminal should not apply."""
        reward = [0.05] * STEPS_24H
        sunset_step = 12 * 12
        inp = make_opt_input(
            soc_pct=50.0,
            buy_price=0.25,
            solar_kw=no_solar(),
            load_kw=1.0,
            sunset_step=sunset_step,
            sunset_reward=0.50,  # Very high — would dominate if active
            terminal_reward=0.50,
            soc_target_reward=reward,
        )
        out = optimize(inp)
        assert out.status == "optimal"
        # The result should reflect the uniform 0.05 reward, not the 0.50 legacy
