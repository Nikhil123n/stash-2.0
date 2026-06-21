"""Tests for the prefix-command dispatcher."""

import pytest

from stash.commands import try_handle
from stash.gateway.sandbox import SandboxGateway
from stash.settings import MODEL_FLASH, MODEL_PRO, Settings
from stash.tools import build_registry


@pytest.fixture
async def env(tmp_path):
    gw = SandboxGateway(str(tmp_path / "cmd.json"))
    await gw.initialize()
    settings = Settings.load(str(tmp_path / "s.json"))
    registry = build_registry(gw)
    return gw, settings, registry


class TestCommandDispatch:
    @pytest.mark.asyncio
    async def test_help_slash(self, env):
        _, settings, registry = env
        out = await try_handle("/help", settings=settings, registry=registry)
        assert out.handled is True
        assert "quick guide" in out.text.lower()

    @pytest.mark.asyncio
    async def test_help_bare_keyword(self, env):
        _, settings, registry = env
        out = await try_handle("help", settings=settings, registry=registry)
        assert out.handled is True

    @pytest.mark.asyncio
    async def test_help_question_mark(self, env):
        _, settings, registry = env
        out = await try_handle("?", settings=settings, registry=registry)
        assert out.handled is True

    @pytest.mark.asyncio
    async def test_model_no_arg_shows_current(self, env):
        _, settings, registry = env
        out = await try_handle("/model", settings=settings, registry=registry)
        assert out.handled is True
        assert "Flash" in out.text

    @pytest.mark.asyncio
    async def test_model_switch(self, env):
        _, settings, registry = env
        out = await try_handle("/model pro", settings=settings, registry=registry)
        assert out.handled is True
        assert settings.agent_model == MODEL_PRO

    @pytest.mark.asyncio
    async def test_model_switch_alias(self, env):
        _, settings, registry = env
        out = await try_handle("/model accurate", settings=settings, registry=registry)
        assert out.handled is True
        assert settings.agent_model == MODEL_PRO

    @pytest.mark.asyncio
    async def test_model_unknown(self, env):
        _, settings, registry = env
        out = await try_handle("/model turbo", settings=settings, registry=registry)
        assert out.handled is True
        assert "Unknown" in out.text
        assert settings.agent_model == MODEL_FLASH

    @pytest.mark.asyncio
    async def test_stats(self, env):
        gw, settings, registry = env
        await gw.create_space("Tech")
        out = await try_handle("/stats", settings=settings, registry=registry)
        assert out.handled is True
        assert "Library overview" in out.text

    @pytest.mark.asyncio
    async def test_unrelated_text_not_handled(self, env):
        _, settings, registry = env
        out = await try_handle("list my spaces", settings=settings, registry=registry)
        assert out.handled is False

    @pytest.mark.asyncio
    async def test_empty_not_handled(self, env):
        _, settings, registry = env
        out = await try_handle("", settings=settings, registry=registry)
        assert out.handled is False

    @pytest.mark.asyncio
    async def test_unknown_slash_command_not_handled(self, env):
        _, settings, registry = env
        out = await try_handle("/whatever", settings=settings, registry=registry)
        assert out.handled is False
