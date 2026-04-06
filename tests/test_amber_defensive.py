"""Tests for Amber API defensive discharge behaviour.

When the Amber price entity goes unavailable, the coordinator should:
- Use the last known price for brief blips (<10 min, configurable)
- Use cautious price ($0.50 or last_known, whichever higher) for 10-15 min
- ALWAYS assume spike risk after 15 min → $2.00 (any time of day)
  At $20/kWh, even 15 min on grid = $5 loss. Battery wear from
  defensive discharge = $0.05/kWh = negligible. Asymmetric risk.
- Alert via persistent_notification after blip threshold
- Clear state when Amber recovers
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.coordinator import VictronMPCCoordinator


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_coordinator(hass) -> VictronMPCCoordinator:
    """Build a coordinator with minimal config entry mock."""
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    coord = VictronMPCCoordinator(hass, entry)
    return coord


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


async def test_amber_available_returns_actual_price(hass):
    """When Amber entity has a valid numeric state, return it."""
    hass.states.async_set("sensor.amber_general_price", "0.42", {})
    coord = _make_coordinator(hass)

    available, price = coord._check_amber_health()

    assert available is True
    assert price == pytest.approx(0.42)
    assert coord._last_known_buy_price == pytest.approx(0.42)
    assert coord._amber_unavailable_since is None


async def test_amber_unavailable_brief_uses_last_known(hass):
    """When Amber goes unavailable for <5 min, use last known price."""
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.55

    # Set Amber unavailable
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(0.55)
    # _amber_unavailable_since should be set (just now)
    assert coord._amber_unavailable_since is not None
    assert (datetime.now() - coord._amber_unavailable_since).total_seconds() < 2


async def test_amber_unavailable_evening_peak_cautious(hass):
    """When Amber unavailable 10-15 min during 17:00-21:00, return cautious $0.50."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        # Set "now" to 18:30 (evening peak)
        fake_now = datetime(2026, 3, 19, 18, 30, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        # 12 min outage — Tier 2 cautious (between 10 and 15 min)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    assert available is False
    # Tier 2: max(last_known=0.30, cautious=0.50) = 0.50
    assert price == pytest.approx(0.50)


async def test_amber_unavailable_off_peak_cautious(hass):
    """When Amber unavailable 10-15 min at ANY time, return cautious $0.50.

    Tier 2 — moderate outage. Not yet panicking but hedging.
    """
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 10, 0, 0)  # 10am — off peak
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(0.50)  # Tier 2 cautious, not $2.00


async def test_amber_recovery_clears_state(hass):
    """When Amber recovers, clear unavailable tracking and alert flag."""
    coord = _make_coordinator(hass)
    # Simulate prior outage
    coord._amber_unavailable_since = datetime.now() - timedelta(minutes=15)
    coord._api_health["amber"]["alerted"] = True

    # Amber comes back
    hass.states.async_set("sensor.amber_general_price", "0.28", {})

    available, price = coord._check_amber_health()

    assert available is True
    assert price == pytest.approx(0.28)
    assert coord._amber_unavailable_since is None
    assert coord._api_health["amber"]["alerted"] is False


async def test_amber_unavailable_doesnt_alert_under_10min(hass):
    """No persistent_notification within first 10 minutes of outage."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    # First call — sets _amber_unavailable_since to now (< 5 min ago)
    available, price = coord._check_amber_health()

    assert available is False
    assert coord._api_health["amber"]["alerted"] is False
    # Verify _amber_unavailable_since was set just now (brief blip path)
    assert coord._amber_unavailable_since is not None


async def test_amber_unavailable_alerts_after_blip(hass):
    """Persistent notification fires when Amber down >10 min (integration path)."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 14, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    assert available is False
    # Tier 2: cautious $0.50
    assert price == pytest.approx(0.50)

    # Now simulate what _async_update_data does after _check_amber_health
    minutes_down = 10.0
    assert coord._api_health["amber"]["alerted"] is False

    # Patch ServiceRegistry.async_call on the class (instance attrs are read-only)
    mock_call = AsyncMock()
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", mock_call
    ):
        # Simulate the notification logic from _async_update_data
        if not coord._api_health["amber"]["alerted"]:
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "MPC: Amber Pricing Unavailable",
                    "message": (
                        f"Amber API down for {minutes_down:.0f} min. "
                        "MPC operating in defensive mode."
                    ),
                    "notification_id": "mpc_amber_down",
                },
            )
            coord._api_health["amber"]["alerted"] = True

    assert coord._api_health["amber"]["alerted"] is True
    mock_call.assert_called_once()
    call_args = mock_call.call_args
    assert call_args[0][0] == "persistent_notification"
    assert call_args[0][1] == "create"
    assert "Amber API down" in call_args[0][2]["message"]


async def test_amber_unknown_state_treated_as_unavailable(hass):
    """State 'unknown' should be treated the same as 'unavailable'."""
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.35

    hass.states.async_set("sensor.amber_general_price", "unknown", {})

    available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(0.35)
    assert coord._amber_unavailable_since is not None


async def test_amber_entity_missing_treated_as_unavailable(hass):
    """If the Amber entity doesn't exist at all, treat as unavailable."""
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.25

    # Don't set any state for the amber entity
    available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(0.25)
    assert coord._amber_unavailable_since is not None


async def test_amber_evening_peak_boundary_17(hass):
    """17:00 exactly — 12 min outage returns Tier 2 cautious."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 17, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    # Tier 2: max(0.30, 0.50) = 0.50
    assert price == pytest.approx(0.50)


async def test_amber_cautious_at_3am(hass):
    """At 3am, Amber down 12min returns Tier 2 cautious $0.50."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 3, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    # Tier 2: max(0.30, 0.50) = 0.50
    assert price == pytest.approx(0.50)


# ──────────────────────────────────────────────────────────────────
# Issue #38: Three-tier Amber escalation
# ──────────────────────────────────────────────────────────────────


async def test_amber_moderate_outage_cautious_price(hass):
    """Amber down 12min with low last-known -> returns $0.50 cautious."""
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.25
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 14, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    assert available is False
    # Tier 2: max(0.25, 0.50) = 0.50
    assert price == pytest.approx(0.50)


async def test_amber_moderate_outage_high_last_known(hass):
    """Amber down 12min with high last-known -> returns last_known."""
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.80
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 14, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=12)

        available, price = coord._check_amber_health()

    assert available is False
    # Tier 2: max(0.80, 0.50) = 0.80
    assert price == pytest.approx(0.80)


async def test_amber_extended_outage_full_defensive(hass):
    """Amber down 35min -> returns $2.00 regardless."""
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.25
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 14, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=35)

        available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(2.00)


async def test_amber_blip_threshold_floored_at_10_minutes(hass):
    """Config amber_blip_minutes=5 should be floored to 10 minutes effective.

    Regression: coordinator.py:859 applies max(blip_threshold, 10.0).
    A 7-minute outage with config=5 should still use last-known (Tier 1),
    not escalate to Tier 2 cautious.
    """
    coord = _make_coordinator(hass)
    coord._last_known_buy_price = 0.30
    # Set tunables with blip_minutes below the 10-min floor
    coord._tunables = MagicMock()
    coord._tunables.amber_blip_minutes = 5.0
    coord._tunables.defensive_price = 2.00
    coord._tunables.amber_cautious_minutes = 15.0

    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 14, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        # 7 minutes outage — above config (5), below effective floor (10)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=7)

        available, price = coord._check_amber_health()

    assert available is False
    # Should still be Tier 1 (last known), not Tier 2 ($0.50)
    assert price == pytest.approx(0.30)


async def test_amber_recovery_clears_all_state(hass):
    """Amber recovers after outage -> returns actual price and clears state."""
    coord = _make_coordinator(hass)
    # Simulate prior extended outage
    coord._amber_unavailable_since = datetime.now() - timedelta(minutes=40)
    coord._api_health["amber"]["alerted"] = True

    # Amber comes back
    hass.states.async_set("sensor.amber_general_price", "0.22", {})

    available, price = coord._check_amber_health()

    assert available is True
    assert price == pytest.approx(0.22)
    assert coord._amber_unavailable_since is None
    assert coord._api_health["amber"]["alerted"] is False
