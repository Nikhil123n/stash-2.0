from __future__ import annotations

import re

import discord

from stash.models import ContentType

URL_PATTERN = re.compile(r'(https?://[^\s<>]+)')
MENTION_PATTERN = re.compile(r'<@!?\d+>')

YOUTUBE_DOMAINS = {"youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com"}
REEL_DOMAINS = {
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com",
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "fb.watch",
}

YOUTUBE_PATH_PATTERNS = {"/watch", "/shorts/", "/live/"}
REEL_PATH_PATTERNS = {"/reel/", "/reels/", "/p/", "/reel", "/video/"}


def _extract_domain(url: str) -> str:
    url = url.split("://", 1)[-1]
    domain = url.split("/", 1)[0].split("?", 1)[0].lower()
    return domain


def _classify_url(url: str) -> ContentType:
    domain = _extract_domain(url)

    if domain in YOUTUBE_DOMAINS:
        return ContentType.YOUTUBE_URL

    if domain in REEL_DOMAINS:
        return ContentType.REEL_URL

    return ContentType.UNKNOWN_URL


def _extract_user_note(text: str, url: str) -> str | None:
    note = text.replace(url, "").strip()
    return note if note else None


def _strip_mentions(text: str) -> str:
    return MENTION_PATTERN.sub("", text).strip()


def route_message(message: discord.Message) -> tuple[ContentType, str, str | None]:
    """Classify a Discord message into content type, primary content, and optional user note."""

    # Check for image attachment
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            user_note = _strip_mentions(message.content) or None
            return ContentType.IMAGE, attachment.url, user_note

    # Check for audio attachment (voice note)
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("audio/"):
            user_note = _strip_mentions(message.content) or None
            return ContentType.VOICE_NOTE, attachment.url, user_note

    # Check for URLs in text
    text = _strip_mentions(message.content)
    urls = URL_PATTERN.findall(text)

    if urls:
        url = urls[0]
        content_type = _classify_url(url)
        user_note = _extract_user_note(text, url)
        return content_type, url, user_note

    # Plain text
    if text:
        return ContentType.TEXT, text, None

    return ContentType.TEXT, "", None
