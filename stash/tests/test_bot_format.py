"""Tests for the bot's reply formatting (kept pure-Python, no Discord)."""

from datetime import datetime, timezone

import pytest

from stash.bot import StashBot
from stash.config import StashConfig
from stash.models import SavedCard


def _make_config(tmp_path) -> StashConfig:
    return StashConfig(
        DISCORD_TOKEN="x",
        STASH_OWNER_ID=1,
        STASH_ENV="sandbox",
        GOOGLE_APPLICATION_CREDENTIALS=str(tmp_path / "fake-key.json"),
        GROQ_API_KEY="x",
        SANDBOX_FILE=str(tmp_path / "sb.json"),
        TMP_DIR=str(tmp_path / "tmp"),
    )


def _bot(tmp_path) -> StashBot:
    """Build a StashBot without invoking discord.Client.__init__."""
    cfg = _make_config(tmp_path)
    bot = StashBot.__new__(StashBot)
    bot._config = cfg
    return bot


class TestFormatSaved:
    def test_includes_id(self, tmp_path):
        bot = _bot(tmp_path)
        card = SavedCard(
            mymind_id="abc-123",
            title="Hello",
            category="Claude",
            tags=["ai"],
            summary="A summary.",
            source_url="https://example.com",
            saved_at=datetime.now(timezone.utc),
        )
        card._space_assigned = True
        out = bot._format_saved(card)
        assert "abc-123" in out
        assert "Saved to **Claude**" in out
        assert "Hello" in out

    def test_reports_unassigned_honestly(self, tmp_path):
        bot = _bot(tmp_path)
        card = SavedCard(
            mymind_id="abc-123",
            title="Hello",
            category="LinkedIn",
            tags=[],
            summary="",
            source_url=None,
            saved_at=datetime.now(timezone.utc),
        )
        card._space_assigned = False
        out = bot._format_saved(card)
        assert "couldn't assign" in out.lower() or "could not" in out.lower()
        assert "LinkedIn" in out

    def test_no_space_label(self, tmp_path):
        bot = _bot(tmp_path)
        card = SavedCard(
            mymind_id="abc-123",
            title="Hello",
            category="",
            tags=[],
            summary="",
            source_url=None,
            saved_at=datetime.now(timezone.utc),
        )
        out = bot._format_saved(card)
        assert "no space" in out.lower()
