\# Stash — Project Memory

# Current Project Memory (v2.1.1)

This file is for AI coding agents working in the Stash repository. The
section below is the current source-aligned guidance; older notes farther
down are retained as historical context only when they conflict with this
section.

Stash is now both a capture bot and a Discord-native mymind management
assistant. URLs and attachments still use the capture pipeline:

```text
Router -> Extractor -> Categorizer -> Gateway -> mymind
```

Plain-text messages now use the agent pipeline:

```text
Prefix command or Gemini function call -> Tool registry -> Gateway -> mymind
```

Current modules added after the original memory:

- `stash/agent.py`: Gemini function-calling orchestrator.
- `stash/tools.py`: gateway-backed tool registry.
- `stash/commands.py`: `/help`, `/model`, `/stats`.
- `stash/settings.py`: persisted agent model preference.
- `stash/help.py`: Discord help and startup greeting.

Current required env vars:

- `DISCORD_TOKEN`
- `STASH_OWNER_ID`
- `STASH_ENV`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GROQ_API_KEY`

Current production mymind auth vars:

- `MYMIND_JWT`
- `MYMIND_CID`
- `MYMIND_AUTHENTICITY_TOKEN`

`MYMIND_KID` and `MYMIND_SECRET` are no longer read by the current code.

Current verification:

```text
python -m pytest -q
172 passed
```

Current branch in this checkout is `main`. Do not switch branches or rewrite
history unless the user explicitly asks.

Current gotchas:

- Plain text routes to the agent; a note is saved only if the agent chooses
  `save_note`.
- Agent fallback is advertised in UI text but not implemented in
  `stash.agent.handle_text()`.
- Categorizer fallback is implemented as Flash -> Pro.
- Production mymind integration is unofficial and cookie-based.
- `mymind-api` installs from upstream `main`, which can make rebuilds fragile.



\## What this project is

Discord bot that captures content → transcribes/analyzes → saves to mymind.

Single user, no UI, pure backend Python.



\## Stack

\- Python 3.12, discord.py, asyncio

\- Vertex AI Gemini (via google-cloud-aiplatform SDK, NOT OpenRouter)

\- Groq Whisper for transcription

\- yt-dlp + ffmpeg for video audio extraction

\- mymind-api (iamumeransari) as storage layer



\## Non-negotiables

\- STASH\_ENV=sandbox during ALL development

\- Never instantiate MyMindGateway in sandbox mode

\- Credentials only from .env, never hardcoded, never logged

\- sanitize\_for\_prompt() on ALL user content before Gemini calls

\- Always delete /tmp/stash/ files in finally blocks

\- Build order from STASH\_SPEC.md Section 9 — do not skip steps



\## Current build status

All original build steps are complete. Current suite status: 172 tests passing.

1. models.py - DONE
2. config.py - DONE
3. gateway/interface.py + gateway/sandbox.py - DONE
4. taxonomy.py - DONE
5. security.py - DONE
6. extractor/image.py - DONE
7. extractor/audio.py - DONE
8. extractor/video.py - DONE
9. categorizer.py - DONE
10. router.py - DONE
11. gateway/mymind.py - DONE (`create_tag` is a no-op in production gateway; sandbox persists tags)
12. bot.py - DONE
13. Tests - DONE (172 passing)
14. Dockerfile + README.md - DONE



\## Decisions made

- Current code uses cookie env vars (`MYMIND_JWT`, `MYMIND_CID`, `MYMIND_AUTHENTICITY_TOKEN`); older `MYMIND_KID`/`MYMIND_SECRET` assumptions are superseded
- Added create_tag(name) -> str to gateway interface per user directive
- Pydantic for config.py validation only; plain dataclasses for models.py
- TMP_DIR configurable via env (defaults to /tmp/stash) for cross-platform dev
- audio.py exposes convert_to_mp3 and transcribe_with_groq as reusable helpers for video.py
- Gateway factory lazy-imports MyMindGateway only in production mode
- Categorizer uses response_mime_type="application/json" for structured output
- Bot entry point is `python -m stash.bot` (module execution)



\## Historical Branch Strategy

Current checkout for this audit is `main`; the branch table below is legacy project memory.

| Branch | Purpose |
|--------|---------|
| main | Production — merge from approach/cookie via PR only |
| approach/cookie | Active development — all work happens here |
| approach/jwt | Archived JWT implementation — do not delete, do not modify |

\### Workflow
- Work on approach/cookie
- When ready for prod: PR approach/cookie -> main
- Never commit directly to main
- approach/jwt is read-only archive



\## Known gotchas

- YouTube downloads blocked by bot detection on local Windows (works in Docker/cloud with clean IP)
- ffmpeg not available on Windows dev machine; conversion tests skip gracefully
- yt-dlp can leave partial downloads on some failure paths; glob cleanup in finally handles this
- Python 3.14 on dev machine; Dockerfile pins 3.12-slim for production stability
