from __future__ import annotations

import asyncio
import json
import logging
import functools
import re

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
Here are the EXACT existing spaces in the user's mymind:
{space_names}

TAGS: {tag_names}

Content to categorize:
Type: {content_type}
{transcript_block}\
{user_note_block}\
{image_block}\

Instructions:
1. Infer the topic from the FULL content provided (transcript, image, or text).
2. Choose a SPACE using these rules (follow strictly in order):
   a. ALWAYS prefer an existing space over creating a new one
   b. If the content fits an existing space even loosely, use it
   c. Only propose a NEW space name if the content is completely unrelated to ALL existing spaces
   d. Prefer the more specific space over generic: "Leg Day" beats "Health" for workout content
   e. Return the EXACT space name as it appears in the list above — do not rephrase, abbreviate, or rename
   f. "Technology" not "Tech", "Career Development" not "Career"
3. is_new_category must be true ONLY if you propose a name not in the list above. If you matched an existing space, is_new_category MUST be false.
4. Select 2-4 relevant tags from the existing list. You may add 1 new tag if clearly needed.
5. Write a 1-2 sentence summary of what this content is actually about.
6. Rate your confidence 0.0-1.0. Be honest. If the content was unclear or extraction failed, score lower.

Respond ONLY with valid JSON. No markdown fences, no preamble.
{{"title": "...", "category": "...", "tags": ["...", "..."], "summary": "...", "is_new_category": true/false, "confidence": 0.0, "reasoning": "..."}}\
"""

TITLE_SUMMARY_PROMPT = """\
Generate a title and 1-sentence summary for this content.
Type: {content_type}
{transcript_block}\
{user_note_block}\

Respond ONLY with valid JSON:
{{"title": "...", "summary": "..."}}\
"""

CLAUDE_KEYWORDS = [
    "claude", "anthropic", "claude.ai", "claude code",
    "mcp server", "model context protocol", "claude sonnet",
    "claude opus", "claude haiku", "artifacts",
]


# Patterns that capture an explicit space directive in the user's note.
# Examples that match (case-insensitive):
#   "put this in Claude"
#   "save to LinkedIn"
#   "add to Career Development"
#   "-> Tech"  /  "=> Tech"
#   "in:LinkedIn"
#   "space: Claude"
_DIRECTIVE_PATTERNS = [
    re.compile(
        r"\b(?:put|save|add|move|stash|drop)\s+(?:this|it)?\s*"
        r"(?:in(?:to)?|to|under)\s+(?:the\s+)?"
        r"(?:space\s+called\s+|space\s+|the\s+)?"
        r"['\"]?(?P<name>[A-Za-z0-9 _\-&/+]+?)['\"]?"
        r"\s*(?:space|category|collection)?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:->|=>)\s*['\"]?(?P<name>[A-Za-z0-9 _\-&/+]+?)['\"]?\s*$"),
    re.compile(r"\b(?:in|to|space|category)\s*[:=]\s*['\"]?(?P<name>[A-Za-z0-9 _\-&/+]+?)['\"]?\s*$", re.IGNORECASE),
]

_DIRECTIVE_STOP_WORDS = {
    "this", "it", "that", "here", "there", "today", "tomorrow", "now",
    "later", "please", "asap", "please.", "the",
}


def parse_space_directive(text: str | None) -> str | None:
    """Extract an explicit target space name from a user note, or None.

    Conservative: only fires when the phrase clearly names a destination
    space. Returns the cleaned-up space name (preserving the user's casing
    where possible).
    """
    if not text:
        return None
    candidate = text.strip()
    if not candidate:
        return None

    for pattern in _DIRECTIVE_PATTERNS:
        m = pattern.search(candidate)
        if not m:
            continue
        name = (m.group("name") or "").strip(" .,;:!?\"'")
        if not name:
            continue
        if name.lower() in _DIRECTIVE_STOP_WORDS:
            continue
        # Reject obvious non-names (too long => probably a sentence)
        if len(name) > 60 or len(name.split()) > 5:
            continue
        return name
    return None


def _is_claude_content(packet: ContentPacket) -> bool:
    text = " ".join(filter(None, [
        packet.transcript,
        packet.page_text,
        packet.user_note,
        packet.raw_input,
    ])).lower()
    return any(keyword in text for keyword in CLAUDE_KEYWORDS)


async def categorize(packet: ContentPacket, taxonomy: TaxonomyCache) -> CategoryResult:
    await taxonomy.refresh_if_stale()

    # Precedence:
    # 1. Explicit user directive in the same message ("put this in X") wins.
    # 2. Else Claude keyword path -> dedicated "Claude" space.
    # 3. Else general Gemini inference against the existing taxonomy.
    directive_space = parse_space_directive(packet.user_note)

    if directive_space:
        return await _categorize_with_forced_space(packet, directive_space)

    if _is_claude_content(packet):
        return await _categorize_claude(packet, taxonomy)

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

    result = _parse_result(result_json)
    result.verbatim_note = packet.user_note
    return result


async def _categorize_with_forced_space(
    packet: ContentPacket, forced_space: str
) -> CategoryResult:
    """User explicitly named a target space. Force category and still let
    Gemini generate a good title/summary/tags. The save layer is responsible
    for creating the space if it doesn't exist."""
    transcript_block = ""
    if packet.transcript:
        transcript_block = f"Transcript: {sanitize_for_prompt(packet.transcript)[:500]}\n"
    user_note_block = ""
    if packet.user_note:
        user_note_block = f"User note: {sanitize_for_prompt(packet.user_note)}\n"

    prompt = TITLE_SUMMARY_PROMPT.format(
        content_type=packet.content_type.value,
        transcript_block=transcript_block,
        user_note_block=user_note_block,
    )

    title = packet.user_note[:60] if packet.user_note else (packet.raw_input[:60] or "Untitled")
    summary = packet.user_note or (packet.raw_input[:120] if packet.raw_input else "")
    try:
        result_json = await asyncio.wait_for(
            _call_gemini(PRIMARY_MODEL, prompt, None),
            timeout=LATENCY_THRESHOLD,
        )
        data = json.loads(result_json.strip())
        title = data.get("title", title) or title
        summary = data.get("summary", summary) or summary
    except Exception as e:
        logger.warning("Title/summary gen failed for forced space, using fallback: %s", e)

    return CategoryResult(
        title=title,
        category=forced_space,
        tags=[],
        summary=summary,
        is_new_category=True,  # save layer resolves: existing -> no-op, missing -> create
        confidence=1.0,
        reasoning=f"Explicit user directive to space '{forced_space}'",
        verbatim_note=packet.user_note,
    )


async def _categorize_claude(packet: ContentPacket, taxonomy: TaxonomyCache) -> CategoryResult:
    """Fast path for Claude/Anthropic content — skip full categorization."""
    is_new = not any(s["name"].lower() == "claude" for s in taxonomy.spaces)

    transcript_block = ""
    if packet.transcript:
        transcript_block = f"Transcript: {sanitize_for_prompt(packet.transcript)[:500]}\n"
    user_note_block = ""
    if packet.user_note:
        user_note_block = f"User note: {sanitize_for_prompt(packet.user_note)}\n"

    prompt = TITLE_SUMMARY_PROMPT.format(
        content_type=packet.content_type.value,
        transcript_block=transcript_block,
        user_note_block=user_note_block,
    )

    try:
        result_json = await asyncio.wait_for(
            _call_gemini(PRIMARY_MODEL, prompt, None),
            timeout=LATENCY_THRESHOLD,
        )
        data = json.loads(result_json.strip())
        title = data.get("title", "Claude Content")
        summary = data.get("summary", "")
    except Exception:
        title = packet.user_note[:60] if packet.user_note else "Claude Content"
        summary = packet.user_note or packet.raw_input[:100]

    return CategoryResult(
        title=title,
        category="Claude",
        tags=["claude", "ai-tools"],
        summary=summary,
        is_new_category=is_new,
        confidence=1.0,
        reasoning="Matched Claude/Anthropic keyword",
        verbatim_note=packet.user_note,
    )


def _build_user_prompt(packet: ContentPacket, taxonomy: TaxonomyCache) -> str:
    transcript_block = ""
    if packet.transcript:
        safe_transcript = sanitize_for_prompt(packet.transcript)
        transcript_block = f"Transcript: {safe_transcript}\n"

    user_note_block = ""
    if packet.user_note:
        safe_note = sanitize_for_prompt(packet.user_note)
        user_note_block = f"User's own note (treat as ground truth for categorization): {safe_note}\n"

    image_block = ""
    if packet.image_bytes:
        image_block = "[image attached]\n"

    content_type_str = packet.content_type.value
    if packet.extraction_failed:
        content_type_str += f" (extraction failed: {packet.extraction_error or 'unknown'})"
        if packet.source_url:
            content_type_str += f"\nSource URL: {packet.source_url}"

    space_names = ", ".join(s["name"] for s in taxonomy.spaces) or "(none yet)"
    tag_names = ", ".join(taxonomy.tags[:100]) or "(none yet)"

    return USER_PROMPT_TEMPLATE.format(
        space_names=space_names,
        tag_names=tag_names,
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
