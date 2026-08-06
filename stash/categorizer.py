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

# All free-tier, cross-provider so one outage can't take down every rung.
# Groq has no vision-capable free model, so image categorization uses a
# separate, OpenRouter-only chain below.
PRIMARY_MODEL = "llama-3.3-70b-versatile"  # Groq
FALLBACK_MODEL = "openai/gpt-oss-20b:free"  # OpenRouter
FALLBACK_MODEL_2 = "openai/gpt-oss-120b"  # Groq
MODEL_CHAIN = [PRIMARY_MODEL, FALLBACK_MODEL, FALLBACK_MODEL_2]

VISION_MODEL = "google/gemma-4-26b-a4b-it:free"  # OpenRouter
VISION_FALLBACK_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"  # OpenRouter
VISION_MODEL_CHAIN = [VISION_MODEL, VISION_FALLBACK_MODEL]

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
#   "Move this card into the LinkedIn space"
#   "Save this article to Tech"
#   "Put this video in Career Development"
#   "stash the screenshot in Design"
#   "-> Tech"  /  "=> Tech"
#   "in:LinkedIn"
#   "space: Claude"
_DIRECTIVE_PATTERNS = [
    # Verb-anchored at the start of the note. Allows up to 5 intervening
    # words between the verb (move/put/save/...) and the preposition
    # (in/into/to/under) so phrasings like "Move this card into the
    # LinkedIn space" work.
    re.compile(
        r"^\s*(?:put|save|add|move|stash|drop|file)\b"
        r"(?:\s+\w+){0,5}?"
        r"\s+(?:in(?:to)?|to|under)\s+"
        r"(?:the\s+)?"
        r"(?:space\s+(?:called\s+|named\s+)?)?"
        r"['\"]?(?P<name>[A-Za-z0-9][A-Za-z0-9 _\-&/+]*?)['\"]?"
        r"\s*(?:space|category|collection)?"
        r"\s*[.!?]?\s*$",
        re.IGNORECASE,
    ),
    # Arrow shortcuts: "-> Tech", "=> Career Development"
    re.compile(
        r"^\s*(?:->|=>)\s*['\"]?(?P<name>[A-Za-z0-9 _\-&/+]+?)['\"]?\s*$"
    ),
    # Colon / equals shortcuts: "in: LinkedIn", "space: Claude",
    # "category=Tech"
    re.compile(
        r"\b(?:in|to|space|category)\s*[:=]\s*"
        r"['\"]?(?P<name>[A-Za-z0-9 _\-&/+]+?)['\"]?"
        r"\s*[.!?]?\s*$",
        re.IGNORECASE,
    ),
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


async def _call_groq(
    model_name: str,
    user_prompt: str,
    image_data: tuple[bytes, str] | None = None,
) -> str:
    """Groq's free-tier catalog currently has no vision model, so image_data
    is accepted only for _run_chain signature uniformity and should never
    actually be passed a value here."""
    from groq import AsyncGroq

    client = AsyncGroq()
    response = await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


async def _call_openrouter(
    model_name: str,
    user_prompt: str,
    image_data: tuple[bytes, str] | None = None,
) -> str:
    """OpenRouter's OpenAI-compatible API. Supports optional image input for
    the vision chain's free vision-capable models."""
    import base64
    import os
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )

    if image_data:
        img_bytes, mime = image_data
        b64 = base64.b64encode(img_bytes).decode()
        user_content = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
    else:
        user_content = user_prompt

    response = await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.3,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


async def _run_chain(
    calls: list[tuple],
    user_prompt: str,
    image_data: tuple[bytes, str] | None = None,
) -> str:
    """Try each (call_fn, model_name) pair in order, falling through to the
    next on any failure or timeout. Raises the last error if every tier
    fails."""
    err: Exception | None = None
    for i, (call_fn, model_name) in enumerate(calls):
        try:
            return await asyncio.wait_for(
                call_fn(model_name, user_prompt, image_data),
                timeout=LATENCY_THRESHOLD,
            )
        except Exception as e:
            err = e
            if i + 1 < len(calls):
                logger.warning(
                    "%s failed (%s), falling back to %s",
                    model_name, e, calls[i + 1][1],
                )
    logger.error("All models in chain failed: %s", err)
    raise err


def _text_chain() -> list[tuple]:
    # Built fresh per call (not a module-level constant) so tests can patch
    # _call_groq/_call_openrouter by name and have it take effect here.
    return [
        (_call_groq, PRIMARY_MODEL),
        (_call_openrouter, FALLBACK_MODEL),
        (_call_groq, FALLBACK_MODEL_2),
    ]


def _vision_chain() -> list[tuple]:
    return [
        (_call_openrouter, VISION_MODEL),
        (_call_openrouter, VISION_FALLBACK_MODEL),
    ]


async def categorize(packet: ContentPacket, taxonomy: TaxonomyCache) -> CategoryResult:
    await taxonomy.refresh_if_stale()

    # Precedence:
    # 1. Explicit user directive in the same message ("put this in X") wins.
    # 2. Else Claude keyword path -> dedicated "Claude" space.
    # 3. Else general model inference against the existing taxonomy.
    directive_space = parse_space_directive(packet.user_note)

    if directive_space:
        return await _categorize_with_forced_space(packet, directive_space)

    if _is_claude_content(packet):
        return await _categorize_claude(packet, taxonomy)

    user_prompt = _build_user_prompt(packet, taxonomy)

    if packet.image_bytes and packet.image_mime:
        image_data = (packet.image_bytes, packet.image_mime)
        result_json = await _run_chain(_vision_chain(), user_prompt, image_data)
    else:
        result_json = await _run_chain(_text_chain(), user_prompt)

    result = _parse_result(result_json)
    result.verbatim_note = packet.user_note
    return result


async def _categorize_with_forced_space(
    packet: ContentPacket, forced_space: str
) -> CategoryResult:
    """User explicitly named a target space. Force category and still let
    the model generate a good title/summary/tags. The save layer is
    responsible for creating the space if it doesn't exist."""
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
        result_json = await _run_chain(_text_chain(), prompt)
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
        result_json = await _run_chain(_text_chain(), prompt)
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
