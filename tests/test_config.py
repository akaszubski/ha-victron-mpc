"""Tests for configuration dataclasses.

Verifies dataclass construction, serialization, config entry loading,
and tunable bounds validation.
"""

import json
from pathlib import Path

from custom_components.victron_mpc.config import (
    TUNABLE_BOUNDS,
    MPCTunables,
    VictronSystem,
)


class TestVictronSystem:
    def test_defaults(self):
        sys = VictronSystem()
        assert sys.battery_capacity_kwh == 14.2
        assert sys.charge_efficiency == 0.95

    def test_genset_cost(self):
        sys = VictronSystem()
        # (2.20 * 1.5) / 4.0 + 0.05 = 0.875
        assert abs(sys.genset_cost_per_kwh - 0.875) < 0.01

    def test_genset_cost_dynamic(self):
        sys = VictronSystem(genset_diesel_price_per_litre=3.00)
        # (3.00 * 1.5) / 4.0 + 0.05 = 1.175
        assert abs(sys.genset_cost_per_kwh - 1.175) < 0.01

    def test_from_config_entry(self):
        sys = VictronSystem.from_config_entry({
            "battery_capacity_kwh": 10.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
        })
        assert sys.battery_capacity_kwh == 10.0
        assert sys.max_charge_kw == 5.0


class TestMPCTunables:
    def test_defaults(self):
        t = MPCTunables()
        assert t.battery_wear_cost == 0.02
        assert t.overnight_hold_reward == 0.05

    def test_horizon_steps(self):
        t = MPCTunables()
        assert t.horizon_steps == 288  # 24h * 60 / 5
        assert t.dt_hours == 5 / 60
        assert t.steps_per_hour == 12

    def test_to_dict_roundtrip(self):
        t = MPCTunables()
        d = t.to_dict()
        t2 = MPCTunables.from_dict(d)
        assert t2.battery_wear_cost == t.battery_wear_cost
        assert t2.overnight_hold_reward == t.overnight_hold_reward

    def test_from_dict_ignores_unknown(self):
        t = MPCTunables.from_dict({"battery_wear_cost": 0.08, "unknown_key": 999})
        assert t.battery_wear_cost == 0.08

    def test_from_config_entry(self):
        t = MPCTunables.from_config_entry({
            "battery_wear_cost": 0.03,
            "soc_floor_pct": 25,
            "shadow_mode": True,  # Not a tunable, should be ignored
        })
        assert t.battery_wear_cost == 0.03
        assert t.soc_floor_pct == 25.0

    def test_from_config_entry_clamps_bounds(self):
        t = MPCTunables.from_config_entry({
            "battery_wear_cost": 0.50,  # Way above max 0.10
            "soc_floor_pct": 5.0,       # Below min 10.0
        })
        assert t.battery_wear_cost == 0.10
        assert t.soc_floor_pct == 10.0

    def test_cloud_layer_weights_defaults(self):
        t = MPCTunables()
        assert t.solar_cloud_layer_weights["high"] == 0.15
        assert t.solar_cloud_layer_weights["mid"] == 0.5
        assert t.solar_cloud_layer_weights["low"] == 0.9

    def test_day_type_percentiles_defaults(self):
        t = MPCTunables()
        assert t.solar_day_type_percentiles["clear"] == 0.90
        assert t.solar_day_type_percentiles["rain"] == 0.15


class TestTunableBounds:
    def test_all_bounds_have_valid_ranges(self):
        for key, (lo, hi) in TUNABLE_BOUNDS.items():
            assert lo < hi, f"{key}: min {lo} >= max {hi}"

    def test_all_bounded_fields_exist(self):
        t = MPCTunables()
        for key in TUNABLE_BOUNDS:
            assert hasattr(t, key), f"TUNABLE_BOUNDS has '{key}' but MPCTunables doesn't"

    def test_defaults_within_bounds(self):
        t = MPCTunables()
        for key, (lo, hi) in TUNABLE_BOUNDS.items():
            val = getattr(t, key)
            assert lo <= val <= hi, f"{key}={val} outside [{lo}, {hi}]"


class TestSensorTranslationKeys:
    """Every sensor translation_key in sensor.py must exist in translations/en.json."""

    def test_all_translation_keys_present_in_en_json(self):
        """Read sensor.py translation_keys and verify each exists in en.json."""
        from custom_components.victron_mpc.sensor import SENSOR_DESCRIPTIONS

        translations_path = (
            Path(__file__).parent.parent
            / "custom_components"
            / "victron_mpc"
            / "translations"
            / "en.json"
        )
        translations = json.loads(translations_path.read_text())
        sensor_translations = translations.get("entity", {}).get("sensor", {})

        for desc in SENSOR_DESCRIPTIONS:
            key = desc.translation_key
            assert key in sensor_translations, (
                f"translation_key '{key}' from sensor.py "
                f"missing in translations/en.json entity.sensor"
            )


class TestParameterizedSafetyThresholds:
    """Custom safety thresholds override defaults in MPCTunables."""

    def test_custom_spike_threshold(self):
        t = MPCTunables(spike_threshold=2.50)
        assert t.spike_threshold == 2.50

    def test_custom_defensive_price(self):
        t = MPCTunables(defensive_price=3.00)
        assert t.defensive_price == 3.00

    def test_custom_feedin_export_threshold(self):
        t = MPCTunables(feedin_export_threshold=0.05)
        assert t.feedin_export_threshold == 0.05

    def test_custom_amber_blip_minutes(self):
        t = MPCTunables(amber_blip_minutes=15.0)
        assert t.amber_blip_minutes == 15.0

    def test_multiple_custom_thresholds_override_defaults(self):
        t = MPCTunables(
            spike_threshold=1.50,
            defensive_price=5.00,
            feedin_export_threshold=0.20,
            feedin_soc_threshold=50.0,
        )
        defaults = MPCTunables()
        assert t.spike_threshold != defaults.spike_threshold
        assert t.defensive_price != defaults.defensive_price
        assert t.feedin_export_threshold != defaults.feedin_export_threshold
        assert t.feedin_soc_threshold != defaults.feedin_soc_threshold
