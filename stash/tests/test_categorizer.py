import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from stash.categorizer import _build_user_prompt, _is_claude_content, _parse_result, categorize
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

        with patch("stash.categorizer._call_gemini", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)
            assert result.title == "Note"
            assert result.confidence == 0.85

    @pytest.mark.asyncio
    async def test_fallback_on_timeout(self, taxonomy):
        from stash.categorizer import PRIMARY_MODEL, FALLBACK_MODEL

        pkt = ContentPacket(content_type=ContentType.TEXT, raw_input="test")
        mock_response = '{"title": "Fallback", "category": "Inbox", "tags": [], "summary": "x", "is_new_category": false, "confidence": 0.6, "reasoning": null}'

        call_count = []

        async def mock_gemini(model, prompt, image_data=None):
            call_count.append(model)
            if model == PRIMARY_MODEL:
                raise asyncio.TimeoutError()
            return mock_response

        with patch("stash.categorizer._call_gemini", side_effect=mock_gemini):
            result = await categorize(pkt, taxonomy)
            assert result.title == "Fallback"
            assert PRIMARY_MODEL in call_count
            assert FALLBACK_MODEL in call_count


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

        with patch("stash.categorizer._call_gemini", new=AsyncMock(return_value=mock_response)):
            result = await categorize(pkt, taxonomy)
            assert result.category == "Claude"
            assert result.confidence == 1.0
            assert "claude" in result.tags
            assert result.is_new_category is True
