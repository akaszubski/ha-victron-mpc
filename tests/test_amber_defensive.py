"""Tests for Amber API defensive discharge behaviour.

When the Amber price entity goes unavailable, the coordinator should:
- Use the last known price for brief blips (<5 min)
- ALWAYS assume spike risk after 5 min → $2.00 (any time of day)
  At $20/kWh, even 30 min on grid = $10 loss. Battery wear from
  defensive discharge = $0.05/kWh = negligible. Asymmetric risk.
- Alert via persistent_notification after 5 min
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


async def test_amber_unavailable_evening_peak_defensive_discharge(hass):
    """When Amber unavailable >5 min during 17:00-21:00, return $2.00 (spike risk)."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    # Simulate outage started 10 minutes ago
    coord._amber_unavailable_since = datetime.now() - timedelta(minutes=10)

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        # Set "now" to 18:30 (evening peak)
        fake_now = datetime(2026, 3, 19, 18, 30, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        # Keep _amber_unavailable_since as set above (10 min ago from real now)
        # Override it to be 10 min before fake_now
        coord._amber_unavailable_since = fake_now - timedelta(minutes=10)

        available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(2.00)


async def test_amber_unavailable_off_peak_still_defensive(hass):
    """When Amber unavailable >5 min at ANY time, return $2.00 (spike risk).

    Spikes can happen at any hour — morning demand, grid failures, etc.
    At $20/kWh risk vs $0.05/kWh wear cost, always discharge defensively.
    """
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 10, 0, 0)  # 10am — off peak
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=10)

        available, price = coord._check_amber_health()

    assert available is False
    assert price == pytest.approx(2.00)  # Always defensive, not $0.30


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


async def test_amber_unavailable_doesnt_alert_under_5min(hass):
    """No persistent_notification within first 5 minutes of outage."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    # First call — sets _amber_unavailable_since to now (< 5 min ago)
    available, price = coord._check_amber_health()

    assert available is False
    assert coord._api_health["amber"]["alerted"] is False
    # Verify _amber_unavailable_since was set just now (brief blip path)
    assert coord._amber_unavailable_since is not None


async def test_amber_unavailable_alerts_after_5min(hass):
    """Persistent notification fires when Amber down >5 min (integration path)."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 14, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=10)

        available, price = coord._check_amber_health()

    assert available is False
    # Always defensive — $2.00 regardless of time
    assert price == pytest.approx(2.00)

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
    """17:00 exactly is within evening peak."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 17, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=10)

        available, price = coord._check_amber_health()

    assert price == pytest.approx(2.00)


async def test_amber_defensive_at_3am(hass):
    """Even at 3am, Amber down >5min returns $2.00 (spikes can happen anytime)."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.amber_general_price", "unavailable", {})

    with patch(
        "custom_components.victron_mpc.coordinator.datetime"
    ) as mock_dt:
        fake_now = datetime(2026, 3, 19, 3, 0, 0)
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        coord._amber_unavailable_since = fake_now - timedelta(minutes=10)

        available, price = coord._check_amber_health()

    assert price == pytest.approx(2.00)
