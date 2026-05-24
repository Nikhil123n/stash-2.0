from __future__ import annotations

import asyncio
import json
import logging
import functools

from stash.models import CategoryResult, ContentPacket
from stash.security import sanitize_for_prompt
from stash.taxonomy import TaxonomyCache

logger = logging.getLogger(__name__)

PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.5-pro"
LATENCY_THRESHOLD = 30.0

SYSTEM_PROMPT = (
    "You are a personal knowledge assistant. Your job is to categorize content "
    "into the user's second brain. Be concise and accurate."
)

USER_PROMPT_TEMPLATE = """\
Here is the user's existing taxonomy in mymind:
{taxonomy}

Content to categorize:
Type: {content_type}
{transcript_block}\
{user_note_block}\
{image_block}\

Instructions:
1. Infer the topic from the FULL content provided (transcript, image, or text).
2. Choose the best matching SPACE from the existing list. If none fits well, propose a new space name.
3. Select 2–4 relevant tags from the existing list. You may add 1 new tag if clearly needed.
4. Write a 1–2 sentence summary of what this content is actually about.
5. Rate your confidence 0.0–1.0. Be honest. If the content was unclear or extraction failed, score lower.

Respond ONLY with valid JSON. No markdown fences, no preamble.
{{"title": "...", "category": "...", "tags": ["...", "..."], "summary": "...", "is_new_category": true/false, "confidence": 0.0, "reasoning": "..."}}\
"""


async def categorize(packet: ContentPacket, taxonomy: TaxonomyCache) -> CategoryResult:
    await taxonomy.refresh_if_stale()

    user_prompt = _build_user_prompt(packet, taxonomy)
    image_data = None
    if packet.image_bytes and packet.image_mime:
        image_data = (packet.image_bytes, packet.image_mime)

    try:
        result_json = await asyncio.wait_for(
            _call_gemini(PRIMARY_MODEL, user_prompt, image_data),
            timeout=LATENCY_THRESHOLD,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("Primary model failed (%s), falling back to %s", e, FALLBACK_MODEL)
        try:
            result_json = await _call_gemini(FALLBACK_MODEL, user_prompt, image_data)
        except Exception as fallback_err:
            logger.error("Fallback model also failed: %s", fallback_err)
            raise

    return _parse_result(result_json)


def _build_user_prompt(packet: ContentPacket, taxonomy: TaxonomyCache) -> str:
    transcript_block = ""
    if packet.transcript:
        safe_transcript = sanitize_for_prompt(packet.transcript)
        transcript_block = f"Transcript: {safe_transcript}\n"

    user_note_block = ""
    if packet.user_note:
        safe_note = sanitize_for_prompt(packet.user_note)
        user_note_block = f"User note: {safe_note}\n"

    image_block = ""
    if packet.image_bytes:
        image_block = "[image attached]\n"

    content_type_str = packet.content_type.value
    if packet.extraction_failed:
        content_type_str += f" (extraction failed: {packet.extraction_error or 'unknown'})"
        if packet.source_url:
            content_type_str += f"\nSource URL: {packet.source_url}"

    return USER_PROMPT_TEMPLATE.format(
        taxonomy=taxonomy.get_taxonomy_for_prompt(),
        content_type=content_type_str,
        transcript_block=transcript_block,
        user_note_block=user_note_block,
        image_block=image_block,
    )


_vertexai_initialized = False


async def _call_gemini(
    model_name: str,
    user_prompt: str,
    image_data: tuple[bytes, str] | None = None,
) -> str:
    import vertexai
    from vertexai.generative_models import GenerativeModel, Part

    global _vertexai_initialized
    if not _vertexai_initialized:
        import os
        vertexai.init(
            project=os.environ.get("GCP_PROJECT_ID"),
            location=os.environ.get("GCP_LOCATION", "us-central1"),
        )
        _vertexai_initialized = True

    loop = asyncio.get_running_loop()

    def _sync_call() -> str:
        model = GenerativeModel(
            model_name,
            system_instruction=SYSTEM_PROMPT,
        )

        contents = []
        if image_data:
            img_bytes, mime = image_data
            contents.append(Part.from_data(data=img_bytes, mime_type=mime))
        contents.append(Part.from_text(user_prompt))

        response = model.generate_content(
            contents,
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 2048,
                "response_mime_type": "application/json",
            },
        )
        return response.text

    return await loop.run_in_executor(None, _sync_call)


def _parse_result(raw_json: str) -> CategoryResult:
    raw_json = raw_json.strip()
    if raw_json.startswith("```"):
        raw_json = raw_json.split("\n", 1)[1] if "\n" in raw_json else raw_json[3:]
        if raw_json.endswith("```"):
            raw_json = raw_json[:-3]
        raw_json = raw_json.strip()

    data = json.loads(raw_json)

    return CategoryResult(
        title=data.get("title", "Untitled"),
        category=data.get("category", "Inbox"),
        tags=data.get("tags", []),
        summary=data.get("summary", ""),
        is_new_category=data.get("is_new_category", False),
        confidence=float(data.get("confidence", 0.0)),
        reasoning=data.get("reasoning"),
    )
