\# Stash — Project Memory



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

All 14 steps complete. 44 tests passing.

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
11. gateway/mymind.py - DONE (create_tag is NotImplementedError stub)
12. bot.py - DONE
13. Tests - DONE (44 passing)
14. Dockerfile + README.md - DONE



\## Decisions made

- Used MYMIND_KID/SECRET (not email/password) per .env.example and container-friendly approach
- Added create_tag(name) -> str to gateway interface per user directive
- Pydantic for config.py validation only; plain dataclasses for models.py
- TMP_DIR configurable via env (defaults to /tmp/stash) for cross-platform dev
- audio.py exposes convert_to_mp3 and transcribe_with_groq as reusable helpers for video.py
- Gateway factory lazy-imports MyMindGateway only in production mode
- Categorizer uses response_mime_type="application/json" for structured output
- Bot entry point is `python -m stash.bot` (module execution)



\## Known gotchas

- YouTube downloads blocked by bot detection on local Windows (works in Docker/cloud with clean IP)
- ffmpeg not available on Windows dev machine; conversion tests skip gracefully
- yt-dlp can leave partial downloads on some failure paths; glob cleanup in finally handles this
- Python 3.14 on dev machine; Dockerfile pins 3.12-slim for production stability

