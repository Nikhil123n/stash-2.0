import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from stash.categorizer import (
    _build_user_prompt,
    _is_claude_content,
    _parse_result,
    categorize,
    parse_space_directive,
)
from stash.gateway.sandbox import SandboxGateway
from stash.models import CategoryResult, ContentPacket, ContentType
from stash.taxonomy import TaxonomyCache


@pytest.fixture
async def taxonomy(tmp_path):
    gw = SandboxGateway(str(tmp_path / "cat_sandbox.json"))
    await gw.initialize()
    await gw.create_space("Tech")
    await gw.create_space("Career")
    await gw.create_tag("python")
    await gw.create_tag("resume")
    cache = TaxonomyCache(gw)
    await cache.initialize()
    return cache


class TestBuildUserPrompt:
    @pytest.mark.asyncio
    async def test_transcript_prompt(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.YOUTUBE_URL,
            raw_input="https://youtube.com/watch?v=x",
            transcript="A talk about async Python patterns.",
            user_note="great tutorial",
        )
        prompt = _build_user_prompt(pkt, taxonomy)
        assert "Tech" in prompt
        assert "Career" in prompt
        assert "python" in prompt
        assert "resume" in prompt
        assert "Transcript: A talk about async Python" in prompt
        assert "great tutorial" in prompt
        assert "ground truth" in prompt
        assert "youtube_url" in prompt
        assert "ALWAYS prefer an existing space" in prompt

    @pytest.mark.asyncio
    async def test_image_prompt(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.IMAGE,
            raw_input="screenshot.png",
            image_bytes=b"fake",
            image_mime="image/png",
        )
        prompt = _build_user_prompt(pkt, taxonomy)
        assert "[image attached]" in prompt
        assert "Transcript:" not in prompt

    @pytest.mark.asyncio
    async def test_failed_extraction_prompt(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.REEL_URL,
            raw_input="https://instagram.com/reel/x",
            source_url="https://instagram.com/reel/x",
            extraction_failed=True,
            extraction_error="Could not download",
            user_note="fitness content",
        )
        prompt = _build_user_prompt(pkt, taxonomy)
        assert "extraction failed" in prompt
        assert "Source URL:" in prompt
        assert "fitness content" in prompt
        assert "ground truth" in prompt

    @pytest.mark.asyncio
    async def test_sanitization_applied(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.TEXT,
            raw_input="test",
            transcript="normal text\x00with\x07control\x01chars```injection```",
        )
        prompt = _build_user_prompt(pkt, taxonomy)
        assert "\x00" not in prompt
        assert "\x07" not in prompt
        assert "```" not in prompt
        assert "~~~injection~~~" in prompt


class TestParseResult:
    def test_valid_json(self):
        raw = '{"title": "Test", "category": "Tech", "tags": ["python"], "summary": "A test.", "is_new_category": false, "confidence": 0.9, "reasoning": "clear"}'
        result = _parse_result(raw)
        assert result.title == "Test"
        assert result.category == "Tech"
        assert result.tags == ["python"]
        assert result.confidence == 0.9
        assert result.is_new_category is False

    def test_fenced_json(self):
        raw = '```json\n{"title": "X", "category": "Y", "tags": [], "summary": "Z", "is_new_category": true, "confidence": 0.5, "reasoning": null}\n```'
        result = _parse_result(raw)
        assert result.title == "X"
        assert result.confidence == 0.5

    def test_missing_fields_defaults(self):
        raw = '{}'
        result = _parse_result(raw)
        assert result.title == "Untitled"
        assert result.category == "Inbox"
        assert result.tags == []
        assert result.confidence == 0.0

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            _parse_result("not json at all")


class TestCategorize:
    @pytest.mark.asyncio
    async def test_primary_model_success(self, taxonomy):
        pkt = ContentPacket(content_type=ContentType.TEXT, raw_input="test note")
        mock_response = '{"title": "Note", "category": "Tech", "tags": ["python"], "summary": "A note.", "is_new_category": false, "confidence": 0.85, "reasoning": "clear"}'

        with patch("stash.categorizer._call_groq", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)
            assert result.title == "Note"
            assert result.confidence == 0.85

    @pytest.mark.asyncio
    async def test_fallback_to_openrouter_on_groq_failure(self, taxonomy):
        from stash.categorizer import FALLBACK_MODEL

        pkt = ContentPacket(content_type=ContentType.TEXT, raw_input="test")
        mock_response = '{"title": "Fallback", "category": "Inbox", "tags": [], "summary": "x", "is_new_category": false, "confidence": 0.6, "reasoning": null}'

        openrouter_calls = []

        async def mock_groq(model, prompt, image_data=None):
            raise asyncio.TimeoutError()

        async def mock_openrouter(model, prompt, image_data=None):
            openrouter_calls.append(model)
            return mock_response

        with patch("stash.categorizer._call_groq", side_effect=mock_groq), \
             patch("stash.categorizer._call_openrouter", side_effect=mock_openrouter):
            result = await categorize(pkt, taxonomy)
            assert result.title == "Fallback"
            assert openrouter_calls == [FALLBACK_MODEL]

    @pytest.mark.asyncio
    async def test_second_groq_fallback_when_openrouter_also_fails(self, taxonomy):
        from stash.categorizer import FALLBACK_MODEL_2, PRIMARY_MODEL

        pkt = ContentPacket(content_type=ContentType.TEXT, raw_input="test")
        mock_response = '{"title": "SecondFallback", "category": "Inbox", "tags": [], "summary": "x", "is_new_category": false, "confidence": 0.5, "reasoning": null}'

        groq_calls = []

        async def mock_groq(model, prompt, image_data=None):
            groq_calls.append(model)
            if model == PRIMARY_MODEL:
                raise RuntimeError("groq primary down")
            return mock_response

        with patch("stash.categorizer._call_groq", side_effect=mock_groq), \
             patch("stash.categorizer._call_openrouter", new=AsyncMock(side_effect=RuntimeError("openrouter down"))):
            result = await categorize(pkt, taxonomy)
            assert result.title == "SecondFallback"
            assert groq_calls == [PRIMARY_MODEL, FALLBACK_MODEL_2]

    @pytest.mark.asyncio
    async def test_all_fallbacks_fail_raises(self, taxonomy):
        pkt = ContentPacket(content_type=ContentType.TEXT, raw_input="test")

        with patch("stash.categorizer._call_groq", new=AsyncMock(side_effect=RuntimeError("groq down"))), \
             patch("stash.categorizer._call_openrouter", new=AsyncMock(side_effect=RuntimeError("openrouter down"))):
            with pytest.raises(RuntimeError, match="groq down"):
                await categorize(pkt, taxonomy)

    @pytest.mark.asyncio
    async def test_image_uses_vision_chain_not_groq(self, taxonomy):
        """Images should never reach _call_groq (Groq has no free vision model)."""
        from stash.categorizer import VISION_MODEL

        pkt = ContentPacket(
            content_type=ContentType.IMAGE,
            raw_input="screenshot.png",
            image_bytes=b"fake",
            image_mime="image/png",
        )
        mock_response = '{"title": "Screenshot", "category": "Tech", "tags": [], "summary": "x", "is_new_category": false, "confidence": 0.7, "reasoning": null}'

        vision_calls = []

        async def mock_openrouter(model, prompt, image_data=None):
            vision_calls.append((model, image_data))
            return mock_response

        with patch("stash.categorizer._call_groq", new=AsyncMock(side_effect=AssertionError("must not call groq for images"))), \
             patch("stash.categorizer._call_openrouter", side_effect=mock_openrouter):
            result = await categorize(pkt, taxonomy)
            assert result.title == "Screenshot"
            assert vision_calls[0][0] == VISION_MODEL
            assert vision_calls[0][1] == (b"fake", "image/png")

    @pytest.mark.asyncio
    async def test_image_falls_back_to_vision_fallback_model(self, taxonomy):
        from stash.categorizer import VISION_FALLBACK_MODEL, VISION_MODEL

        pkt = ContentPacket(
            content_type=ContentType.IMAGE,
            raw_input="screenshot.png",
            image_bytes=b"fake",
            image_mime="image/png",
        )
        mock_response = '{"title": "Screenshot2", "category": "Tech", "tags": [], "summary": "x", "is_new_category": false, "confidence": 0.7, "reasoning": null}'

        calls = []

        async def mock_openrouter(model, prompt, image_data=None):
            calls.append(model)
            if model == VISION_MODEL:
                raise RuntimeError("vision primary down")
            return mock_response

        with patch("stash.categorizer._call_openrouter", side_effect=mock_openrouter):
            result = await categorize(pkt, taxonomy)
            assert result.title == "Screenshot2"
            assert calls == [VISION_MODEL, VISION_FALLBACK_MODEL]


class TestClaudeDetection:
    def test_detects_claude_keyword(self):
        pkt = ContentPacket(
            content_type=ContentType.TEXT,
            raw_input="Check out this Claude Code tutorial",
        )
        assert _is_claude_content(pkt) is True

    def test_detects_anthropic(self):
        pkt = ContentPacket(
            content_type=ContentType.UNKNOWN_URL,
            raw_input="https://anthropic.com/news",
            user_note="Anthropic released new model",
        )
        assert _is_claude_content(pkt) is True

    def test_detects_mcp(self):
        pkt = ContentPacket(
            content_type=ContentType.YOUTUBE_URL,
            raw_input="https://youtube.com/watch?v=x",
            transcript="In this video we build an MCP server for Claude",
        )
        assert _is_claude_content(pkt) is True

    def test_no_match_for_unrelated(self):
        pkt = ContentPacket(
            content_type=ContentType.TEXT,
            raw_input="How to make pasta carbonara",
        )
        assert _is_claude_content(pkt) is False

    @pytest.mark.asyncio
    async def test_categorize_claude_content(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.TEXT,
            raw_input="Claude Code is amazing for coding",
            user_note="Best AI coding tool",
        )

        mock_response = '{"title": "Claude Code Review", "summary": "A review of Claude Code for development."}'

        with patch("stash.categorizer._call_groq", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)
            assert result.category == "Claude"
            assert result.confidence == 1.0
            assert "claude" in result.tags
            assert result.is_new_category is True


class TestParseSpaceDirective:
    def test_put_this_in(self):
        assert parse_space_directive("put this in the Claude") == "Claude"

    def test_save_to(self):
        assert parse_space_directive("save to LinkedIn") == "LinkedIn"

    def test_add_to(self):
        assert parse_space_directive("add to Career Development") == "Career Development"

    def test_arrow(self):
        assert parse_space_directive("-> Tech") == "Tech"

    def test_in_colon(self):
        assert parse_space_directive("in: LinkedIn") == "LinkedIn"

    def test_space_called(self):
        assert parse_space_directive("put this in the space called Reels") == "Reels"

    def test_no_directive_returns_none(self):
        assert parse_space_directive("great tutorial on async") is None
        assert parse_space_directive("just a note") is None

    def test_empty(self):
        assert parse_space_directive("") is None
        assert parse_space_directive(None) is None

    def test_stop_word_rejected(self):
        assert parse_space_directive("put this in this") is None
        assert parse_space_directive("save it to it") is None

    def test_too_long_rejected(self):
        long_phrase = "save to " + "word " * 10
        assert parse_space_directive(long_phrase) is None

    # Intervening-noun phrasings (the bug user hit)
    def test_move_this_card_into(self):
        assert parse_space_directive(
            "Move this card into the LinkedIn space"
        ) == "LinkedIn"

    def test_save_this_article_to(self):
        assert parse_space_directive("Save this article to Tech") == "Tech"

    def test_put_this_video_in(self):
        assert parse_space_directive(
            "Put this video in Career Development"
        ) == "Career Development"

    def test_stash_the_screenshot_in(self):
        assert parse_space_directive("stash the screenshot in Design") == "Design"

    def test_move_it_into_space_named(self):
        assert parse_space_directive(
            "move it into the space named Reels"
        ) == "Reels"

    def test_file_under(self):
        assert parse_space_directive("file under Tech") == "Tech"


class TestCategorizeWithDirective:
    @pytest.mark.asyncio
    async def test_directive_overrides_claude_keyword(self, taxonomy):
        # Message mentions Claude (would normally route to Claude path)
        # but the user explicitly says "put this in LinkedIn".
        pkt = ContentPacket(
            content_type=ContentType.REEL_URL,
            raw_input="https://instagram.com/reel/x",
            source_url="https://instagram.com/reel/x",
            transcript="Discussion about Claude Code productivity.",
            user_note="put this in LinkedIn",
        )

        mock_response = '{"title": "Productivity Tips", "summary": "Tips for using Claude productively."}'

        with patch("stash.categorizer._call_groq", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)

        assert result.category == "LinkedIn"
        assert result.is_new_category is True
        assert result.confidence == 1.0
        assert "LinkedIn" in (result.reasoning or "")

    @pytest.mark.asyncio
    async def test_directive_uses_model_for_title(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.UNKNOWN_URL,
            raw_input="https://example.com",
            source_url="https://example.com",
            user_note="save to Tech",
        )

        mock_response = '{"title": "Cool Article", "summary": "An overview."}'

        with patch("stash.categorizer._call_groq", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)

        assert result.category == "Tech"
        assert result.title == "Cool Article"
        assert result.summary == "An overview."

    @pytest.mark.asyncio
    async def test_directive_survives_model_failure(self, taxonomy):
        pkt = ContentPacket(
            content_type=ContentType.TEXT,
            raw_input="some text",
            user_note="put this in Reels",
        )

        with patch("stash.categorizer._call_groq", new=AsyncMock(side_effect=RuntimeError("down"))), \
             patch("stash.categorizer._call_openrouter", new=AsyncMock(side_effect=RuntimeError("down"))):
            result = await categorize(pkt, taxonomy)

        assert result.category == "Reels"
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_no_directive_still_routes_to_claude(self, taxonomy):
        # Claude keyword present, no directive — original behavior preserved.
        pkt = ContentPacket(
            content_type=ContentType.YOUTUBE_URL,
            raw_input="https://youtube.com/watch?v=x",
            transcript="Building an MCP server with Claude.",
            user_note="great tutorial",
        )
        mock_response = '{"title": "MCP Tutorial", "summary": "How to build an MCP server."}'
        with patch("stash.categorizer._call_groq", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)
        assert result.category == "Claude"
