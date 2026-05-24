from unittest.mock import MagicMock

import pytest

from stash.models import ContentType
from stash.router import route_message


def _make_msg(content="", attachments=None):
    msg = MagicMock()
    msg.content = content
    msg.attachments = attachments or []
    return msg


def _make_attachment(url, content_type, size=1000):
    att = MagicMock()
    att.url = url
    att.content_type = content_type
    att.size = size
    return att


class TestRouteMessage:
    def test_image_attachment(self):
        msg = _make_msg("caption", [_make_attachment("http://cdn/img.png", "image/png")])
        ct, primary, note = route_message(msg)
        assert ct == ContentType.IMAGE
        assert primary == "http://cdn/img.png"
        assert note == "caption"

    def test_image_attachment_no_caption(self):
        msg = _make_msg("", [_make_attachment("http://cdn/img.jpg", "image/jpeg")])
        ct, primary, note = route_message(msg)
        assert ct == ContentType.IMAGE
        assert note is None

    def test_voice_note(self):
        msg = _make_msg("", [_make_attachment("http://cdn/voice.ogg", "audio/ogg")])
        ct, primary, note = route_message(msg)
        assert ct == ContentType.VOICE_NOTE
        assert note is None

    def test_youtube_url(self):
        msg = _make_msg("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.YOUTUBE_URL
        assert "dQw4w9WgXcQ" in primary
        assert note is None

    def test_youtube_short_url(self):
        msg = _make_msg("https://youtu.be/dQw4w9WgXcQ check this out")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.YOUTUBE_URL
        assert note == "check this out"

    def test_instagram_reel(self):
        msg = _make_msg("https://www.instagram.com/reel/C123abc/")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.REEL_URL

    def test_tiktok(self):
        msg = _make_msg("https://www.tiktok.com/@user/video/7123456789")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.REEL_URL

    def test_facebook_reel(self):
        msg = _make_msg("https://www.facebook.com/reel/456")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.REEL_URL

    def test_fb_watch(self):
        msg = _make_msg("https://fb.watch/abc123")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.REEL_URL

    def test_unknown_url(self):
        msg = _make_msg("https://medium.com/great-article")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.UNKNOWN_URL

    def test_unknown_url_with_note(self):
        msg = _make_msg("https://example.com/page read later")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.UNKNOWN_URL
        assert note == "read later"

    def test_plain_text(self):
        msg = _make_msg("Remember to buy groceries")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.TEXT
        assert primary == "Remember to buy groceries"
        assert note is None

    def test_image_priority_over_url(self):
        msg = _make_msg(
            "https://youtube.com/watch?v=x",
            [_make_attachment("http://cdn/img.jpg", "image/jpeg")],
        )
        ct, _, _ = route_message(msg)
        assert ct == ContentType.IMAGE

    def test_empty_message(self):
        msg = _make_msg("")
        ct, primary, note = route_message(msg)
        assert ct == ContentType.TEXT
        assert primary == ""
