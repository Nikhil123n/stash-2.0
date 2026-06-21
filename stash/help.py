"""Static help / FAQ text shown by /help and on first DM after startup."""

from __future__ import annotations


HELP_TEXT = """\
**Stash — quick guide**

**Save things**
- Paste any URL (YouTube, Instagram/TikTok reel, X post, article, blog).
- Drop a screenshot — I'll OCR + categorize it.
- Send a voice note — I'll transcribe + categorize it.
- Add a note alongside: it becomes the user note on the card.

**Force a target space**
Use natural directives in the same message:
- `put this in Claude — <url>`
- `save to LinkedIn <url>`
- `add to Career Development <url>`
- `-> Tech <url>`

If the space doesn't exist, I create it. If I can't assign, I'll say so honestly.

**Ask things (no URL needed — just talk to me)**
- `list my spaces`
- `what's in the Claude space?`
- `find cards about python`
- `show me my recent saves`
- `surprise me` / `random card`
- `what did I save this week?`
- `stats` / `library overview`

**Manage**
- `delete card <id>` — I'll ask before deleting
- `move card <id> to <space>` — I'll ask before moving
- `create a space called LinkedIn`
- `save a note: groceries tomorrow`

**Bot commands**
- `/help` — this guide
- `/model` — show current AI model
- `/model flash` — switch to Gemini 2.5 Flash (fast, default)
- `/model pro` — switch to Gemini 2.5 Pro (slower, more accurate)
- `/stats` — library overview

**Privacy**
I only respond to DMs from the owner. Cookies are env-only. Temp files
are wiped on every restart.
"""


STARTUP_GREETING_TEMPLATE = """\
**Stash v{version} online**
Gateway: `{gateway_mode}` ({env})
Agent model: {agent_label}
Fallback model: {fallback_label}
Categorizer: {categorizer_primary} -> {categorizer_fallback}
Spaces: {space_count} | Tags: {tag_count} | Tools: {tool_count}

Type `/help` for what you can do.
"""
