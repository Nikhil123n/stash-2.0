"""Prefix-command parser.

Some interactions are pure meta operations on the bot itself (help, model
switching) — they shouldn't pay a Gemini round-trip and shouldn't depend
on the LLM picking the right tool. This module intercepts them before the
agent runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from stash.agent import _format_tool_result
from stash.help import HELP_TEXT
from stash.settings import Settings
from stash.tools import Tool


@dataclass
class CommandResult:
    handled: bool
    text: str = ""


def _looks_like_command(text: str) -> str | None:
    """Return the lowercased command name (without slash) if `text` is a
    prefix command, else None."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    # Either "/cmd ..." or a bare keyword shortcut for help.
    if stripped.startswith("/"):
        head = stripped[1:].split(None, 1)[0].lower()
        return head or None
    head = stripped.split(None, 1)[0].lower()
    if head in ("help", "?"):
        return "help"
    return None


def _arg_after(text: str) -> str:
    parts = text.strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def try_handle(
    text: str,
    *,
    settings: Settings,
    registry: dict[str, Tool],
) -> CommandResult:
    """If `text` is a recognized prefix command, run it and return the reply."""
    cmd = _looks_like_command(text)
    if cmd is None:
        return CommandResult(handled=False)

    if cmd in ("help", "h", "?"):
        return CommandResult(handled=True, text=HELP_TEXT)

    if cmd == "model":
        arg = _arg_after(text.lstrip("/"))
        if not arg:
            return CommandResult(handled=True, text=settings.describe())
        changed, msg = settings.set_agent_model(arg)
        if changed:
            return CommandResult(handled=True, text=msg + "\n\n" + settings.describe())
        return CommandResult(handled=True, text=msg)

    if cmd == "stats":
        tool = registry.get("library_stats")
        if not tool:
            return CommandResult(handled=True, text="Stats tool unavailable.")
        result = await tool.handler()
        return CommandResult(
            handled=True,
            text=_format_tool_result("library_stats", {}, result),
        )

    # Unknown command -> hand back to the agent (not handled).
    return CommandResult(handled=False)
