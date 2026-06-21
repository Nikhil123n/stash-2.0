"""Tests for the agent orchestrator (with Gemini mocked out)."""

from unittest.mock import AsyncMock, patch

import pytest

from stash.agent import (
    AgentResult,
    PendingTool,
    _build_confirmation_preview,
    _format_tool_result,
    execute_pending,
    handle_text,
)
from stash.gateway.sandbox import SandboxGateway
from stash.taxonomy import TaxonomyCache
from stash.tools import build_registry


@pytest.fixture
async def env(tmp_path):
    gw = SandboxGateway(str(tmp_path / "agent.json"))
    await gw.initialize()
    await gw.create_space("Claude")
    await gw.create_space("Tech")
    await gw.create_tag("python")
    cache = TaxonomyCache(gw)
    await cache.initialize()
    registry = build_registry(gw)
    return gw, cache, registry


def _stub_gemini(function_call=None, text=None):
    """Build an AsyncMock matching _call_gemini_with_tools' return shape."""
    return AsyncMock(return_value={"function_call": function_call, "text": text})


class TestHandleText:
    @pytest.mark.asyncio
    async def test_list_spaces_dispatch(self, env):
        gw, taxonomy, registry = env
        stub = _stub_gemini(function_call={"name": "list_spaces", "args": {}})
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text(
                "list my spaces", registry=registry, taxonomy=taxonomy
            )
        assert result.pending is None
        assert result.tool_name == "list_spaces"
        assert "Claude" in result.text
        assert "Tech" in result.text

    @pytest.mark.asyncio
    async def test_list_cards_in_space_dispatch(self, env):
        gw, taxonomy, registry = env
        await gw.save_url("https://example.com", "Reel about Claude", [], "Claude", "n")
        stub = _stub_gemini(function_call={
            "name": "list_cards_in_space",
            "args": {"space_name": "Claude"},
        })
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text(
                "what's in claude?", registry=registry, taxonomy=taxonomy
            )
        assert result.tool_name == "list_cards_in_space"
        assert "Claude" in result.text
        assert "Reel about Claude" in result.text

    @pytest.mark.asyncio
    async def test_create_space_dispatch(self, env):
        gw, taxonomy, registry = env
        stub = _stub_gemini(function_call={
            "name": "create_space", "args": {"name": "LinkedIn"},
        })
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text(
                "make a LinkedIn space", registry=registry, taxonomy=taxonomy
            )
        assert "LinkedIn" in result.text
        spaces = await gw.get_spaces()
        assert any(s["name"] == "LinkedIn" for s in spaces)

    @pytest.mark.asyncio
    async def test_destructive_returns_pending_without_executing(self, env):
        gw, taxonomy, registry = env
        card = await gw.save_note("doomed", "Doomed", [], "")
        stub = _stub_gemini(function_call={
            "name": "delete_card", "args": {"card_id": card.mymind_id},
        })
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text(
                f"delete card {card.mymind_id}",
                registry=registry, taxonomy=taxonomy,
            )
        assert result.pending is not None
        assert result.pending.name == "delete_card"
        # Card still exists — destructive tool was NOT executed
        remaining = await gw.search_cards(query="Doomed")
        assert len(remaining) == 1

    @pytest.mark.asyncio
    async def test_conversational_fallback(self, env):
        _, taxonomy, registry = env
        stub = _stub_gemini(function_call=None, text="Hey there!")
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text("hi", registry=registry, taxonomy=taxonomy)
        assert result.tool_name is None
        assert result.text == "Hey there!"

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_reply(self, env):
        _, taxonomy, registry = env
        stub = _stub_gemini(function_call={"name": "no_such_tool", "args": {}})
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text("x", registry=registry, taxonomy=taxonomy)
        assert result.error is not None
        assert result.text is not None

    @pytest.mark.asyncio
    async def test_bad_args_returns_error_reply(self, env):
        _, taxonomy, registry = env
        stub = _stub_gemini(function_call={
            "name": "list_cards_in_space", "args": {"wrong_kwarg": "x"},
        })
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text("x", registry=registry, taxonomy=taxonomy)
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_gemini_exception_handled(self, env):
        _, taxonomy, registry = env
        stub = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("stash.agent._call_gemini_with_tools", new=stub):
            result = await handle_text("hi", registry=registry, taxonomy=taxonomy)
        assert result.error == "boom"
        assert result.text is not None

    @pytest.mark.asyncio
    async def test_model_param_is_forwarded(self, env):
        """The `model` kwarg should be passed through to the Gemini call."""
        _, taxonomy, registry = env
        captured = {}

        async def fake(user_text, system_prompt, registry_arg, model_name=None):
            captured["model"] = model_name
            return {"function_call": None, "text": "ok"}

        with patch("stash.agent._call_gemini_with_tools", new=fake):
            await handle_text(
                "hi", registry=registry, taxonomy=taxonomy,
                model="gemini-2.5-pro",
            )
        assert captured["model"] == "gemini-2.5-pro"


class TestExecutePending:
    @pytest.mark.asyncio
    async def test_execute_pending_runs_tool(self, env):
        gw, _, registry = env
        card = await gw.save_note("doomed", "Doomed", [], "")
        pending = PendingTool(
            name="delete_card",
            args={"card_id": card.mymind_id},
            preview="...",
        )
        result = await execute_pending(pending, registry)
        assert result.tool_name == "delete_card"
        remaining = await gw.search_cards(query="Doomed")
        assert remaining == []


class TestFormatters:
    def test_format_list_spaces(self):
        out = _format_tool_result(
            "list_spaces", {}, {"ok": True, "count": 2, "spaces": [
                {"name": "Claude", "card_count": 3},
                {"name": "Tech", "card_count": 0},
            ]},
        )
        assert "Claude" in out
        assert "Tech" in out
        assert "3" in out

    def test_format_failed_result(self):
        out = _format_tool_result("list_spaces", {}, {"ok": False, "error": "nope"})
        assert "nope" in out

    def test_confirmation_preview_uses_template(self):
        from stash.tools import Tool

        async def noop():
            return {}

        tool = Tool(
            name="x", description="", parameters={"type": "object", "properties": {}},
            handler=noop, destructive=True,
            confirm_template="Delete {card_id}?",
        )
        preview = _build_confirmation_preview(tool, {"card_id": "abc"})
        assert "Delete abc?" == preview
