import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from stash.extractor.audio import check_audio_size, extract_audio, SUPPORTED_MIMES
from stash.extractor.image import extract_image, ALLOWED_MIMES, MAX_IMAGE_SIZE
from stash.models import ContentType


class TestImageExtractor:
    @pytest.mark.asyncio
    async def test_valid_png(self):
        pkt = await extract_image(b"\x89PNG" + b"\x00" * 100, "image/png", "shot.png")
        assert not pkt.extraction_failed
        assert pkt.image_mime == "image/png"
        assert pkt.image_bytes == b"\x89PNG" + b"\x00" * 100
        assert pkt.content_type == ContentType.IMAGE

    @pytest.mark.asyncio
    async def test_valid_jpeg(self):
        pkt = await extract_image(b"\xff\xd8" + b"\x00" * 50, "image/jpeg", "photo.jpg", "my note")
        assert not pkt.extraction_failed
        assert pkt.user_note == "my note"

    @pytest.mark.asyncio
    async def test_unsupported_mime(self):
        pkt = await extract_image(b"data", "application/pdf", "file.pdf")
        assert pkt.extraction_failed
        assert "Unsupported" in pkt.extraction_error

    @pytest.mark.asyncio
    async def test_too_large(self):
        big = b"\x00" * (MAX_IMAGE_SIZE + 1)
        pkt = await extract_image(big, "image/png", "huge.png")
        assert pkt.extraction_failed
        assert "too large" in pkt.extraction_error

    @pytest.mark.asyncio
    async def test_all_allowed_mimes(self):
        for mime in ALLOWED_MIMES:
            pkt = await extract_image(b"data", mime, "file")
            assert not pkt.extraction_failed


class TestAudioExtractor:
    def test_size_check_ok(self):
        assert check_audio_size(1000) is None
        assert check_audio_size(20_971_520) is None

    def test_size_check_too_large(self):
        err = check_audio_size(20_971_521)
        assert err is not None
        assert "20MB" in err

    @pytest.mark.asyncio
    async def test_unsupported_mime(self):
        pkt = await extract_audio(b"data", "audio/flac", "test.flac", "voice note")
        assert pkt.extraction_failed
        assert "Unsupported" in pkt.extraction_error

    @pytest.mark.asyncio
    async def test_supported_mimes(self):
        for mime in SUPPORTED_MIMES:
            assert mime.startswith("audio/")

    @pytest.mark.asyncio
    async def test_ffmpeg_failure(self):
        pkt = await extract_audio(
            b"not real audio", "audio/ogg", "test.ogg", "voice note",
            tmp_dir="/tmp/stash_test"
        )
        assert pkt.extraction_failed

    @pytest.mark.asyncio
    async def test_empty_transcript(self):
        with patch("stash.extractor.audio.convert_to_mp3", new=AsyncMock(return_value=("/tmp/fake.mp3", None))):
            with patch("stash.extractor.audio.transcribe_with_groq", new=AsyncMock(return_value="")):
                pkt = await extract_audio(
                    b"audio", "audio/mpeg", "test.mp3", "voice",
                    tmp_dir="/tmp/stash_test"
                )
                assert pkt.extraction_failed
                assert "No speech detected" in pkt.extraction_error
