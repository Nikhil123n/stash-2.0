"""Agentic tool registry.

Each tool wraps a gateway operation behind a simple async handler and a
Vertex-AI-compatible JSON-schema description. The agent (see stash/agent.py)
exposes these as FunctionDeclarations so Gemini can pick the right one.

Tools never call the mymind HTTP API directly — they go through MindGateway,
which already owns auth, retries and sandbox/production switching.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable

from stash.gateway.interface import MindGateway


ToolHandler = Callable[..., Awaitable[dict]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    destructive: bool = False
    confirm_template: str | None = None  # human-readable preview for confirmation


def _fuzzy_pick(name: str, spaces: list[dict]) -> dict | None:
    """Return the best-matching space dict by name, or None."""
    if not name:
        return None
    norm = name.lower().strip()
    for s in spaces:
        if s.get("name", "").lower().strip() == norm:
            return s
    for s in spaces:
        existing = s.get("name", "").lower().strip()
        if norm in existing or existing in norm:
            return s
    best = None
    best_ratio = 0.0
    for s in spaces:
        existing = s.get("name", "").lower().strip()
        ratio = SequenceMatcher(None, norm, existing).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = s
    if best_ratio >= 0.75:
        return best
    return None


def _trim_card(c: dict) -> dict:
    """Compact card dict for chat output / model context."""
    return {
        "id": c.get("id", ""),
        "title": c.get("title") or "(untitled)",
        "type": c.get("type", ""),
        "tags": c.get("tags", []),
        "source_url": c.get("source_url"),
    }


# ── Handlers ─────────────────────────────────────────────────────────────


async def _list_spaces(gateway: MindGateway) -> dict:
    spaces = await gateway.get_spaces()
    return {
        "ok": True,
        "count": len(spaces),
        "spaces": [
            {
                "name": s.get("name", ""),
                "id": s.get("id", ""),
                "card_count": s.get("card_count", 0),
            }
            for s in spaces
        ],
    }


async def _list_cards_in_space(gateway: MindGateway, space_name: str, limit: int = 25) -> dict:
    spaces = await gateway.get_spaces()
    match = _fuzzy_pick(space_name, spaces)
    if not match:
        return {
            "ok": False,
            "error": f"No space matches '{space_name}'.",
            "available_spaces": [s["name"] for s in spaces],
        }
    cards = await gateway.get_space_cards(match["id"])
    return {
        "ok": True,
        "space": match["name"],
        "count": len(cards),
        "cards": [_trim_card(c) for c in cards[:limit]],
    }


async def _search_cards(
    gateway: MindGateway,
    query: str | None = None,
    tag: str | None = None,
    card_type: str | None = None,
    domain: str | None = None,
    limit: int = 15,
) -> dict:
    tags = [t.strip() for t in tag.split(",")] if tag else None
    cards = await gateway.search_cards(
        query=query, tags=tags, card_type=card_type, domain=domain, limit=limit
    )
    return {
        "ok": True,
        "count": len(cards),
        "cards": [_trim_card(c) for c in cards],
    }


async def _recent_cards(gateway: MindGateway, limit: int = 10) -> dict:
    cards = await gateway.search_cards(limit=limit)
    return {
        "ok": True,
        "count": len(cards),
        "cards": [_trim_card(c) for c in cards],
    }


async def _create_space(gateway: MindGateway, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Space name is required."}
    spaces = await gateway.get_spaces()
    existing = _fuzzy_pick(name, spaces)
    if existing and existing["name"].lower().strip() == name.lower().strip():
        return {"ok": True, "created": False, "name": existing["name"], "id": existing["id"]}
    space = await gateway.create_space(name)
    return {"ok": True, "created": True, "name": space["name"], "id": space["id"]}


async def _save_note(
    gateway: MindGateway,
    text: str,
    title: str | None = None,
    space: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    if not text:
        return {"ok": False, "error": "Note text is required."}
    card = await gateway.save_note(
        text=text,
        title=title or text[:60],
        tags=tags or [],
        space=space or "",
    )
    return {
        "ok": True,
        "id": card.mymind_id,
        "title": card.title,
        "space": card.category or None,
        "assigned": getattr(card, "_space_assigned", None),
    }


async def _move_card_to_space(
    gateway: MindGateway, card_id: str, space_name: str
) -> dict:
    if not card_id or not space_name:
        return {"ok": False, "error": "card_id and space_name are required."}
    space_id, created = await gateway.resolve_space(space_name)
    if not space_id:
        return {"ok": False, "error": f"Could not find or create space '{space_name}'."}
    ok = await gateway.assign_to_space(card_id, space_id)
    return {
        "ok": ok,
        "card_id": card_id,
        "space": space_name,
        "space_created": created,
    }


async def _delete_card(gateway: MindGateway, card_id: str) -> dict:
    if not card_id:
        return {"ok": False, "error": "card_id is required."}
    ok = await gateway.delete_card(card_id)
    return {"ok": ok, "card_id": card_id}


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Accept ISO 8601 with or without trailing 'Z'
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _library_stats(gateway: MindGateway) -> dict:
    spaces = await gateway.get_spaces()
    tags = await gateway.get_tags()
    # Pull a large window to estimate "this week" without paging
    cards = await gateway.search_cards(limit=500)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    saved_this_week = 0
    for c in cards:
        dt = _parse_dt(str(c.get("created") or c.get("modified") or ""))
        if dt and dt >= cutoff:
            saved_this_week += 1

    largest = sorted(
        ((s.get("name", ""), s.get("card_count", 0)) for s in spaces),
        key=lambda t: t[1],
        reverse=True,
    )[:5]

    return {
        "ok": True,
        "total_cards_seen": len(cards),
        "saved_this_week": saved_this_week,
        "space_count": len(spaces),
        "tag_count": len(tags),
        "largest_spaces": [{"name": n, "count": c} for n, c in largest if n],
    }


async def _cards_this_week(gateway: MindGateway, days: int = 7, limit: int = 25) -> dict:
    cards = await gateway.search_cards(limit=200)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    recent = []
    for c in cards:
        dt = _parse_dt(str(c.get("created") or c.get("modified") or ""))
        if dt and dt >= cutoff:
            recent.append(c)
        if len(recent) >= limit:
            break
    return {
        "ok": True,
        "count": len(recent),
        "days": days,
        "cards": [_trim_card(c) for c in recent],
    }


async def _random_card(gateway: MindGateway) -> dict:
    cards = await gateway.search_cards(limit=200)
    if not cards:
        return {"ok": False, "error": "Nothing saved yet."}
    pick = random.choice(cards)
    return {"ok": True, "card": _trim_card(pick)}


# ── Vertex JSON schemas ──────────────────────────────────────────────────


def build_registry(gateway: MindGateway) -> dict[str, Tool]:
    """Build the tool registry bound to a gateway instance.

    Returned dict is keyed by tool name. The agent iterates `.values()` to
    construct FunctionDeclarations.
    """

    async def list_spaces() -> dict:
        return await _list_spaces(gateway)

    async def list_cards_in_space(space_name: str, limit: int = 25) -> dict:
        return await _list_cards_in_space(gateway, space_name, limit=limit)

    async def search_cards(
        query: str | None = None,
        tag: str | None = None,
        card_type: str | None = None,
        domain: str | None = None,
        limit: int = 15,
    ) -> dict:
        return await _search_cards(
            gateway, query=query, tag=tag, card_type=card_type, domain=domain, limit=limit
        )

    async def recent_cards(limit: int = 10) -> dict:
        return await _recent_cards(gateway, limit=limit)

    async def create_space(name: str) -> dict:
        return await _create_space(gateway, name)

    async def save_note(
        text: str,
        title: str | None = None,
        space: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        return await _save_note(gateway, text=text, title=title, space=space, tags=tags)

    async def move_card_to_space(card_id: str, space_name: str) -> dict:
        return await _move_card_to_space(gateway, card_id, space_name)

    async def delete_card(card_id: str) -> dict:
        return await _delete_card(gateway, card_id)

    async def library_stats() -> dict:
        return await _library_stats(gateway)

    async def cards_this_week(days: int = 7, limit: int = 25) -> dict:
        return await _cards_this_week(gateway, days=days, limit=limit)

    async def random_card() -> dict:
        return await _random_card(gateway)

    registry: dict[str, Tool] = {}

    def add(tool: Tool) -> None:
        registry[tool.name] = tool

    add(Tool(
        name="list_spaces",
        description=(
            "List every space (collection) in mymind with its card count. "
            "Use this when the user asks what spaces exist or how their "
            "library is organized."
        ),
        parameters={"type": "object", "properties": {}},
        handler=list_spaces,
    ))

    add(Tool(
        name="list_cards_in_space",
        description=(
            "List cards saved inside a specific mymind space. The space_name "
            "argument is matched fuzzily against existing space names. Use "
            "when the user asks for the contents of a space, e.g. "
            "'list cards in Claude' or 'what's in the LinkedIn space?'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {
                    "type": "string",
                    "description": "Name of the space to list cards from.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of cards to return (default 25).",
                },
            },
            "required": ["space_name"],
        },
        handler=list_cards_in_space,
    ))

    add(Tool(
        name="search_cards",
        description=(
            "Search the user's entire mymind library by free-text query, "
            "tag, content type, and/or source domain. Combine filters as "
            "needed. Use when the user wants to find a specific card or set "
            "of cards across all spaces."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query across titles and descriptions.",
                },
                "tag": {
                    "type": "string",
                    "description": "Tag name or comma-separated list of tags. AND-ed.",
                },
                "card_type": {
                    "type": "string",
                    "description": "Card type filter (e.g. WebPage, Image, Video, Note).",
                },
                "domain": {
                    "type": "string",
                    "description": "Source domain filter (e.g. youtube.com).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 15).",
                },
            },
        },
        handler=search_cards,
    ))

    add(Tool(
        name="recent_cards",
        description=(
            "Return the user's most recently saved or modified cards across "
            "all spaces. Use for prompts like 'what did I just save?' or "
            "'show me my latest stash'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max cards to return (default 10).",
                },
            },
        },
        handler=recent_cards,
    ))

    add(Tool(
        name="create_space",
        description=(
            "Create a new space in mymind by name. Idempotent: returns the "
            "existing space if the name already matches one. Use when the "
            "user explicitly asks to make/add/create a new space."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the new space."},
            },
            "required": ["name"],
        },
        handler=create_space,
    ))

    add(Tool(
        name="save_note",
        description=(
            "Save a plain-text note to mymind (no URL, no attachment). "
            "Useful when the user says things like 'note: ...' or 'remind "
            "me that ...' without any link. Optionally assigns the note to "
            "a space and adds tags."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note body."},
                "title": {"type": "string", "description": "Optional short title."},
                "space": {
                    "type": "string",
                    "description": "Optional space name. Auto-created if missing.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags.",
                },
            },
            "required": ["text"],
        },
        handler=save_note,
    ))

    add(Tool(
        name="move_card_to_space",
        description=(
            "Move (assign) an existing card into a space. Requires the "
            "card_id (slug) — typically obtained from a previous search or "
            "list operation. Destructive in the sense that it changes the "
            "card's organization."
        ),
        parameters={
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Card slug/id."},
                "space_name": {
                    "type": "string",
                    "description": "Target space name. Auto-created if missing.",
                },
            },
            "required": ["card_id", "space_name"],
        },
        handler=move_card_to_space,
        destructive=True,
        confirm_template="Move card `{card_id}` to space **{space_name}**?",
    ))

    add(Tool(
        name="delete_card",
        description=(
            "Permanently delete a card from mymind by id. Irreversible. "
            "Only call when the user explicitly asks to delete/remove a "
            "specific card."
        ),
        parameters={
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Card slug/id."},
            },
            "required": ["card_id"],
        },
        handler=delete_card,
        destructive=True,
        confirm_template="Delete card `{card_id}` permanently?",
    ))

    add(Tool(
        name="library_stats",
        description=(
            "Quick overview of the user's mymind library: total cards seen, "
            "saved-this-week count, number of spaces and tags, and the "
            "largest spaces. Use for 'stats', 'overview', 'how big is my "
            "stash?'."
        ),
        parameters={"type": "object", "properties": {}},
        handler=library_stats,
    ))

    add(Tool(
        name="cards_this_week",
        description=(
            "List cards saved in the last N days (default 7). Use when the "
            "user asks for a recent digest, 'what did I save this week', "
            "'last few days', etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Window in days (default 7).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max cards to return (default 25).",
                },
            },
        },
        handler=cards_this_week,
    ))

    add(Tool(
        name="random_card",
        description=(
            "Pick one random card from the user's library and resurface it. "
            "Use for 'surprise me', 'pick a random card', 'show me anything'."
        ),
        parameters={"type": "object", "properties": {}},
        handler=random_card,
    ))

    return registry
