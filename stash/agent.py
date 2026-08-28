"""Tool-calling orchestrator (Groq / OpenRouter, OpenAI-compatible APIs).

Plain-text Discord messages flow through `handle_text()`. The agent asks the
configured model to pick a tool from the registry; if it picks a safe tool,
we run it and return a formatted reply. If it picks a destructive tool, we
surface a `PendingTool` so the bot can ask the user for confirmation. If no
tool is picked, we fall back to the model's conversational text. On failure,
`handle_text()` retries once against `settings.fallback_for(model)`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from stash.security import sanitize_for_prompt
from stash.settings import MODEL_FLASH, MODEL_PRO, fallback_for
from stash.taxonomy import TaxonomyCache
from stash.tools import Tool


logger = logging.getLogger(__name__)


DEFAULT_AGENT_MODEL = MODEL_FLASH
AGENT_TIMEOUT = 20.0

# Which OpenAI-compatible API a given agent model lives on.
PROVIDER_FOR_MODEL = {MODEL_FLASH: "groq", MODEL_PRO: "groq"}

SYSTEM_PROMPT = (
    "You are Stash, a personal mymind assistant chatting with the owner in "
    "Discord. Your job is to help them navigate, organize and interact with "
    "their saved cards. You have a set of tools (functions) for reading and "
    "modifying mymind. Rules:\n"
    "1. When the user asks a question that needs live data from mymind "
    "(spaces, cards, search results), CALL the matching tool. Do not "
    "fabricate spaces, card titles or counts.\n"
    "2. When the user is just chatting, replying or thinking out loud, "
    "answer conversationally without calling a tool. Be concise, warm and a "
    "bit playful.\n"
    "3. For destructive actions (delete_card, move_card_to_space), call the "
    "tool — the host system will request user confirmation before it runs.\n"
    "4. Prefer fuzzy matching on existing space names rather than inventing "
    "new ones. If a space the user mentions doesn't exist, ask if they want "
    "you to create it before saving anything there.\n"
)


@dataclass
class PendingTool:
    """A destructive tool call awaiting user confirmation."""
    name: str
    args: dict
    preview: str


@dataclass
class AgentResult:
    text: str | None = None
    pending: PendingTool | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: dict | None = None
    error: str | None = None


# ── Model glue ───────────────────────────────────────────────────────────


def _build_client(model_name: str):
    """Groq and OpenRouter both expose an OpenAI-compatible chat completions
    API, so one client type covers both — only base_url/api_key differ."""
    provider = PROVIDER_FOR_MODEL.get(model_name, "groq")
    if provider == "groq":
        from groq import AsyncGroq
        return AsyncGroq()
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


async def _call_model_with_tools(
    user_text: str,
    system_prompt: str,
    registry: dict[str, Tool],
    model_name: str = DEFAULT_AGENT_MODEL,
) -> dict:
    """Call the model with tool-calling enabled.

    Returns a normalized dict:
        {"function_call": {"name": str, "args": dict} | None,
         "text": str | None}
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in registry.values()
    ]

    client = _build_client(model_name)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=1024,
        ),
        timeout=AGENT_TIMEOUT,
    )

    message = response.choices[0].message
    if message.tool_calls:
        call = message.tool_calls[0]
        try:
            args = json.loads(call.function.arguments) if call.function.arguments else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        return {"function_call": {"name": call.function.name, "args": args}, "text": None}

    return {"function_call": None, "text": message.content or None}


# ── Result formatting ────────────────────────────────────────────────────


def _format_tool_result(tool_name: str, args: dict, result: dict) -> str:
    """Render a tool result as a Discord-friendly text reply."""
    if not result.get("ok", False):
        return f"Couldn't do that: {result.get('error', 'unknown error')}"

    if tool_name == "list_spaces":
        spaces = result.get("spaces", [])
        if not spaces:
            return "No spaces yet in your mymind."
        lines = [f"**{len(spaces)} space(s):**"]
        for s in spaces:
            cnt = s.get("card_count", 0)
            lines.append(f"- {s['name']} ({cnt})")
        return "\n".join(lines)

    if tool_name == "list_cards_in_space":
        cards = result.get("cards", [])
        space = result.get("space", "?")
        if not cards:
            return f"**{space}** has no cards yet."
        lines = [f"**{space}** ({result.get('count', len(cards))} cards):"]
        for c in cards:
            lines.append(_render_card_line(c))
        return "\n".join(lines)

    if tool_name in ("search_cards", "recent_cards"):
        cards = result.get("cards", [])
        if not cards:
            return "No cards matched."
        lines = [f"**{len(cards)} card(s):**"]
        for c in cards:
            lines.append(_render_card_line(c))
        return "\n".join(lines)

    if tool_name == "create_space":
        if result.get("created"):
            return f"Created space **{result['name']}** (id: `{result.get('id', '')}`)."
        return f"Space **{result['name']}** already exists (id: `{result.get('id', '')}`)."

    if tool_name == "save_note":
        space = result.get("space")
        line = f"Saved note **{result.get('title', '(untitled)')}**"
        if space:
            line += f" to **{space}**"
        if result.get("id"):
            line += f"\nID: `{result['id']}`"
        return line

    if tool_name == "move_card_to_space":
        if result.get("ok"):
            extra = " (space created)" if result.get("space_created") else ""
            return (
                f"Moved card `{result.get('card_id', '')}` to **{result['space']}**"
                f"{extra}."
            )
        return "Move failed."

    if tool_name == "delete_card":
        if result.get("ok"):
            return f"Card `{result.get('card_id', '')}` deleted."
        return "Delete failed."

    if tool_name == "library_stats":
        lines = [
            "**Library overview:**",
            f"- Spaces: {result.get('space_count', 0)}",
            f"- Tags: {result.get('tag_count', 0)}",
            f"- Cards seen (recent window): {result.get('total_cards_seen', 0)}",
            f"- Saved in last 7 days: {result.get('saved_this_week', 0)}",
        ]
        largest = result.get("largest_spaces") or []
        if largest:
            lines.append("- Largest spaces: " + ", ".join(
                f"{s['name']} ({s['count']})" for s in largest
            ))
        return "\n".join(lines)

    if tool_name == "cards_this_week":
        cards = result.get("cards", [])
        if not cards:
            return f"Nothing saved in the last {result.get('days', 7)} days."
        lines = [f"**{len(cards)} card(s) in last {result.get('days', 7)} days:**"]
        for c in cards:
            lines.append(_render_card_line(c))
        return "\n".join(lines)

    if tool_name == "random_card":
        c = result.get("card") or {}
        line = f"**Random card:** {c.get('title') or '(untitled)'}"
        if c.get("id"):
            line += f"\nID: `{c['id']}`"
        if c.get("source_url"):
            line += f"\n<{c['source_url']}>"
        return line

    return f"Done: {result}"


def _render_card_line(c: dict) -> str:
    """One bullet line for a card in a list: ID then title then URL."""
    cid = c.get("id") or ""
    title = c.get("title") or "(untitled)"
    line = f"- `{cid}` {title}" if cid else f"- {title}"
    if c.get("source_url"):
        line += f" — <{c['source_url']}>"
    return line


def _build_confirmation_preview(tool: Tool, args: dict) -> str:
    if tool.confirm_template:
        try:
            return tool.confirm_template.format(**args)
        except Exception:
            pass
    return f"Run `{tool.name}` with {args}?"


# ── Entry points ─────────────────────────────────────────────────────────


def _taxonomy_block(taxonomy: TaxonomyCache | None) -> str:
    if not taxonomy:
        return ""
    spaces = ", ".join(s.get("name", "") for s in taxonomy.spaces) or "(none)"
    tags = ", ".join(taxonomy.tags[:50]) or "(none)"
    return f"\n\nKnown spaces: {spaces}\nKnown tags: {tags}"


async def _call_with_agent_fallback(
    prompt: str, registry: dict[str, Tool], model: str
) -> dict:
    """Try `model`; on any failure retry once against its fallback
    (settings.fallback_for). A fallback-call failure propagates uncaught."""
    try:
        return await _call_model_with_tools(prompt, SYSTEM_PROMPT, registry, model_name=model)
    except Exception as e:
        fallback_model = fallback_for(model)
        logger.warning(
            "Agent model %s failed (%s), retrying with %s", model, e, fallback_model
        )
        return await _call_model_with_tools(
            prompt, SYSTEM_PROMPT, registry, model_name=fallback_model
        )


async def handle_text(
    text: str,
    *,
    registry: dict[str, Tool],
    taxonomy: TaxonomyCache | None = None,
    model: str = DEFAULT_AGENT_MODEL,
) -> AgentResult:
    """Handle a plain-text user message. Picks a tool or chats. Retries once
    against the fallback model on timeout/error before giving up."""
    safe = sanitize_for_prompt(text)
    prompt = f"User message: {safe}{_taxonomy_block(taxonomy)}"

    try:
        response = await _call_with_agent_fallback(prompt, registry, model)
    except asyncio.TimeoutError:
        return AgentResult(error="Agent timed out.", text="Took too long — try again?")
    except Exception as e:
        logger.exception("Agent call failed: %s: %s", type(e).__name__, str(e)[:250])
        return AgentResult(error=str(e), text="Something went sideways. Try again?")

    fc = response.get("function_call")
    if not fc:
        text_reply = response.get("text") or "Not sure how to help with that yet."
        return AgentResult(text=text_reply)

    tool_name = fc["name"]
    tool_args = fc.get("args") or {}
    tool = registry.get(tool_name)
    if not tool:
        return AgentResult(
            error=f"Unknown tool {tool_name}",
            text="I picked a tool I don't actually have. Try rephrasing?",
        )

    if tool.destructive:
        return AgentResult(
            pending=PendingTool(
                name=tool_name,
                args=tool_args,
                preview=_build_confirmation_preview(tool, tool_args),
            ),
        )

    try:
        result = await tool.handler(**tool_args)
    except TypeError as e:
        return AgentResult(
            error=str(e),
            text=f"Tool `{tool_name}` got bad arguments. {e}",
        )
    except Exception as e:
        logger.exception("Tool execution failed: %s", tool_name)
        return AgentResult(
            error=str(e),
            text=f"Tool `{tool_name}` failed: {str(e)[:120]}",
        )

    text_reply = _format_tool_result(tool_name, tool_args, result)
    return AgentResult(
        text=text_reply,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=result,
    )


async def execute_pending(
    pending: PendingTool,
    registry: dict[str, Tool],
) -> AgentResult:
    """Run a previously-confirmed destructive tool call."""
    tool = registry.get(pending.name)
    if not tool:
        return AgentResult(error=f"Unknown tool {pending.name}", text="Tool vanished.")
    try:
        result = await tool.handler(**pending.args)
    except Exception as e:
        logger.exception("Confirmed tool failed: %s", pending.name)
        return AgentResult(error=str(e), text=f"Failed: {str(e)[:120]}")
    return AgentResult(
        text=_format_tool_result(pending.name, pending.args, result),
        tool_name=pending.name,
        tool_args=pending.args,
        tool_result=result,
    )
