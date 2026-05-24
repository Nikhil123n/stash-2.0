from __future__ import annotations

from stash.models import ContentPacket, ContentType

ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB


class ImageExtractionError(Exception):
    pass


async def extract_image(
    image_bytes: bytes,
    mime_type: str,
    raw_input: str,
    user_note: str | None = None,
) -> ContentPacket:
    if mime_type not in ALLOWED_MIMES:
        return ContentPacket(
            content_type=ContentType.IMAGE,
            raw_input=raw_input,
            user_note=user_note,
            extraction_failed=True,
            extraction_error=f"Unsupported image type: {mime_type}. Allowed: {', '.join(sorted(ALLOWED_MIMES))}",
        )

    if len(image_bytes) > MAX_IMAGE_SIZE:
        return ContentPacket(
            content_type=ContentType.IMAGE,
            raw_input=raw_input,
            user_note=user_note,
            extraction_failed=True,
            extraction_error=f"Image too large: {len(image_bytes)} bytes (max {MAX_IMAGE_SIZE})",
        )

    return ContentPacket(
        content_type=ContentType.IMAGE,
        raw_input=raw_input,
        image_bytes=image_bytes,
        image_mime=mime_type,
        user_note=user_note,
    )
