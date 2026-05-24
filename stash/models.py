from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ContentType(str, Enum):
    YOUTUBE_URL = "youtube_url"
    REEL_URL = "reel_url"
    IMAGE = "image"
    VOICE_NOTE = "voice_note"
    TEXT = "text"
    UNKNOWN_URL = "unknown_url"


@dataclass
class ContentPacket:
    content_type: ContentType
    raw_input: str
    transcript: str | None = None
    image_bytes: bytes | None = None
    image_mime: str | None = None
    page_text: str | None = None
    user_note: str | None = None
    source_url: str | None = None
    extraction_failed: bool = False
    extraction_error: str | None = None


@dataclass
class CategoryResult:
    title: str
    category: str
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    is_new_category: bool = False
    confidence: float = 0.0
    reasoning: str | None = None


@dataclass
class SavedCard:
    mymind_id: str
    title: str
    category: str
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    source_url: str | None = None
    saved_at: datetime = field(default_factory=datetime.now)
