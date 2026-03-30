"""Tests for GenAI health monitor."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.victron_mpc.genai_monitor import (
    GENAI_CYCLE_INTERVAL,
    build_health_snapshot,
    run_genai_health_check,
)


class TestBuildHealthSnapshot:
    """Tests for build_health_snapshot."""

    def test_basic_snapshot_contains_all_fields(self):
        """Snapshot includes mode, SoC, prices, and other key fields."""
        coordinator_data = {
            "mode": "discharge",
            "battery_soc_pct": 72.5,
            "target_register": 300,
            "battery_power_w": -1500,
            "solar_input_w": 3200,
            "load_input_w": 1800,
            "buy_price": 0.25,
            "sell_price": 0.06,
            "cloud_coverage": 40,
            "solar_forecast_today": 18.5,
            "spike": False,
            "shadow_mode": False,
        }
        extra = {
            "r2901_readback_pct": 30.0,
            "r2900": 12,
            "r37_setpoint_w": 0,
            "grid_import_w": 50,
            "grid_export_w": 0,
            "weather": "sunny",
            "solar_yield_kwh": 8.2,
        }

        snapshot = build_health_snapshot(coordinator_data, extra)

        assert "Mode: discharge" in snapshot
        assert "SoC: 72.5%" in snapshot
        assert "R2901 Readback: 30.0%" in snapshot
        assert "R2900 (ESS Mode): 12" in snapshot
        assert "Grid Import: 50W" in snapshot
        assert "Solar: 3200W" in snapshot
        assert "Buy Price: $0.25/kWh" in snapshot
        assert "Weather: sunny" in snapshot
        assert "Solar Yield So Far: 8.2 kWh" in snapshot

    def test_snapshot_includes_trajectory_when_available(self):
        """Schedule is included when present in coordinator data."""
        coordinator_data = {
            "schedule_30min": [{"time": "10:00", "soc_pct": 70}] * 20,
        }
        extra = {}

        snapshot = build_health_snapshot(coordinator_data, extra)

        assert "Planned trajectory" in snapshot

    def test_snapshot_handles_missing_keys(self):
        """Snapshot uses '?' for missing data instead of crashing."""
        snapshot = build_health_snapshot({}, {})

        assert "Mode: unknown" in snapshot
        assert "SoC: ?%" in snapshot
        assert "R2901 Readback: ?%" in snapshot

    def test_snapshot_no_trajectory_when_absent(self):
        """No trajectory line when schedule_30min is not present."""
        snapshot = build_health_snapshot({}, {})

        assert "Planned trajectory" not in snapshot


class TestRunGenaiHealthCheck:
    """Tests for run_genai_health_check."""

    @pytest.mark.asyncio
    async def test_skips_when_no_api_key(self):
        """Returns SKIP status when API key is empty."""
        result = await run_genai_health_check(None, "", "snapshot")

        assert result["status"] == "SKIP"
        assert "No OpenRouter API key" in result["summary"]

    @pytest.mark.asyncio
    async def test_successful_green_response(self):
        """Parses a clean GREEN JSON response."""
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "GREEN",
                                "summary": "All systems nominal",
                                "details": "",
                            }
                        )
                    }
                }
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot data")

        assert result["status"] == "GREEN"
        assert result["summary"] == "All systems nominal"
        assert result["details"] == ""

    @pytest.mark.asyncio
    async def test_successful_red_response(self):
        """Parses a RED JSON response with details."""
        api_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "RED",
                                "summary": "Grid charging during discharge mode",
                                "details": "R2901 is set above current SoC",
                            }
                        )
                    }
                }
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot data")

        assert result["status"] == "RED"
        assert "Grid charging" in result["summary"]

    @pytest.mark.asyncio
    async def test_handles_markdown_code_block_response(self):
        """Strips markdown code fences from model response."""
        wrapped = '```json\n{"status": "YELLOW", "summary": "Minor issue", "details": "test"}\n```'
        api_response = {"choices": [{"message": {"content": wrapped}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "YELLOW"
        assert result["summary"] == "Minor issue"

    @pytest.mark.asyncio
    async def test_handles_markdown_code_block_with_extra_text(self):
        """Strips markdown code fences even when extra text surrounds the JSON."""
        # Regression: some models return prose before/after the code block
        wrapped = 'Here is the analysis:\n```json\n{"status": "GREEN", "summary": "OK", "details": "fine"}\n```\nEnd.'
        api_response = {"choices": [{"message": {"content": wrapped}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "GREEN"
        assert result["summary"] == "OK"

    @pytest.mark.asyncio
    async def test_handles_bare_code_block_no_language_tag(self):
        """Strips bare ``` fences (no language tag)."""
        wrapped = '```\n{"status": "RED", "summary": "Bad", "details": "very bad"}\n```'
        api_response = {"choices": [{"message": {"content": wrapped}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "RED"
        assert result["summary"] == "Bad"

    @pytest.mark.asyncio
    async def test_handles_api_error_status(self):
        """Returns ERROR on non-200 HTTP status."""
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.text = AsyncMock(return_value="Rate limited")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "ERROR"
        assert "429" in result["summary"]

    @pytest.mark.asyncio
    async def test_handles_non_json_response(self):
        """Returns ERROR when model returns non-JSON text."""
        api_response = {"choices": [{"message": {"content": "I cannot parse this as JSON"}}]}

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "ERROR"
        assert "Non-JSON" in result["summary"]

    @pytest.mark.asyncio
    async def test_handles_network_exception(self):
        """Returns ERROR on network failure."""
        session = MagicMock()
        session.post = MagicMock(side_effect=ConnectionError("Network down"))

        result = await run_genai_health_check(session, "sk-or-test-key", "snapshot")

        assert result["status"] == "ERROR"
        assert "Network down" in result["summary"]

    @pytest.mark.asyncio
    async def test_sends_correct_headers(self):
        """Verifies Authorization Bearer header is sent."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-my-key", "snapshot")

        call_kwargs = session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == "Bearer sk-or-my-key"
        assert headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_uses_correct_model(self):
        """Verifies the anthropic/claude-haiku-4-5-20251001 model is used via OpenRouter."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_kwargs = session.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["model"] == "anthropic/claude-haiku-4-5-20251001"
        assert payload["max_tokens"] == 300

    @pytest.mark.asyncio
    async def test_uses_openrouter_url(self):
        """Verifies the OpenRouter API endpoint is called."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_args = session.post.call_args
        url = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
        assert url == "https://openrouter.ai/api/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_system_prompt_in_messages(self):
        """Verifies system prompt is sent as a message (OpenAI format)."""
        api_response = {
            "choices": [
                {"message": {"content": json.dumps({"status": "GREEN", "summary": "OK", "details": ""})}}
            ]
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)

        await run_genai_health_check(session, "sk-or-key", "snapshot")

        call_kwargs = session.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        # System prompt should NOT be a top-level key (Anthropic format)
        assert "system" not in payload


class TestCycleInterval:
    """Tests for the cycle interval constant."""

    def test_interval_is_12(self):
        """12 cycles x 5 min = 60 min hourly check."""
        assert GENAI_CYCLE_INTERVAL == 12
