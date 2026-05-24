# STASH — Project Specification
**Version:** 1.0  
**For:** Claude Code  
**Purpose:** Build the complete backend for the Stash Discord bot

---

## 0. What is Stash?

Stash is a personal knowledge capture bot. The user (a single person — this is NOT a multi-tenant SaaS) drops content into a private Discord DM or channel: links, screenshots, voice notes, YouTube URLs, Instagram/TikTok reels. The bot processes each item, extracts meaning from the full content (not just the title), categorizes it against the user's existing mymind taxonomy, and saves it as a structured card in mymind.

**There is no UI.** The entire product is Discord as the input interface, mymind as the storage layer, and the bot as the processing backend.

---

## 1. Architecture Overview

```
Discord (iPhone input)
    │
    ▼
InputRouter         ← classifies raw Discord message into ContentType
    │
    ▼
Extractor           ← produces a normalized ContentPacket
    │               (text, transcript, image bytes, or combined)
    ▼
Categorizer         ← calls Gemini with ContentPacket + cached taxonomy
    │               ← returns structured JSON: title, category, tags, summary, confidence
    ▼
TaxonomyResolver    ← matches proposed category/tags against cache
    │               ← creates new space/tag in mymind if needed, updates cache
    ▼
MyMindGateway       ← writes the final card to mymind
    │               ← isolated behind an adapter interface (see §6)
    ▼
Discord Reply       ← confirms save with card details; asks for clarification if low confidence
```

Everything is async Python (`asyncio`). No web server needed. The bot process IS the server.

---

## 2. Project Structure

```
stash/
├── bot.py                  ← Discord client, event loop, entry point
├── router.py               ← InputRouter: classify incoming messages
├── extractor/
│   ├── __init__.py
│   ├── video.py            ← yt-dlp + Groq Whisper (YouTube, TikTok, Instagram reels)
│   ├── image.py            ← prepares image bytes for Gemini Vision
│   └── audio.py            ← voice note → Groq Whisper transcript
├── categorizer.py          ← Gemini call, structured JSON output, confidence check
├── taxonomy.py             ← TaxonomyCache: in-memory spaces+tags, refresh logic
├── gateway/
│   ├── __init__.py
│   ├── interface.py        ← abstract MindGateway (Protocol class)
│   ├── mymind.py           ← MyMindGateway: wraps iamumeransari/mymind-api
│   └── sandbox.py          ← SandboxGateway: local JSON file, no mymind calls
├── models.py               ← ContentPacket, CategoryResult, SavedCard dataclasses
├── config.py               ← loads env vars, validates at startup
├── security.py             ← sanitization, PII scrubbing before any external call
├── tests/
│   ├── test_router.py
│   ├── test_extractor.py
│   ├── test_categorizer.py
│   └── test_gateway_sandbox.py
├── .env.example
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## 3. Data Model

### ContentType (enum)
```python
class ContentType(str, Enum):
    YOUTUBE_URL = "youtube_url"
    REEL_URL    = "reel_url"       # Instagram, TikTok, Facebook
    IMAGE       = "image"          # screenshot, photo attachment
    VOICE_NOTE  = "voice_note"     # Discord audio attachment
    TEXT        = "text"           # plain text, note, snippet
    UNKNOWN_URL = "unknown_url"    # link but platform unrecognized
```

### ContentPacket (core data object)
```python
@dataclass
class ContentPacket:
    content_type: ContentType
    raw_input: str                  # original URL or text
    transcript: str | None          # from Whisper (video/audio)
    image_bytes: bytes | None       # for Gemini Vision
    image_mime: str | None          # "image/png", "image/jpeg"
    page_text: str | None           # scraped text fallback
    user_note: str | None           # optional note added by user in same message
    source_url: str | None
    extraction_failed: bool = False
    extraction_error: str | None = None
```

**Rule:** Every extractor returns a ContentPacket. After extraction, all branches converge at Categorizer. The Categorizer does not know or care how the ContentPacket was produced.

### CategoryResult
```python
@dataclass
class CategoryResult:
    title: str
    category: str                   # proposed space name
    tags: list[str]
    summary: str                    # 1–2 sentences max
    is_new_category: bool
    confidence: float               # 0.0–1.0
    reasoning: str | None           # Gemini's self-explanation (debug only, not stored)
```

### SavedCard
```python
@dataclass
class SavedCard:
    mymind_id: str
    title: str
    category: str
    tags: list[str]
    summary: str
    source_url: str | None
    saved_at: datetime
```

---

## 4. Component Specs

### 4.1 InputRouter (`router.py`)

Receives a `discord.Message`. Returns `(ContentType, primary_content: str, user_note: str | None)`.

Logic:
- If message has an attachment with MIME `image/*` → `IMAGE`, pass attachment bytes
- If message has an attachment with MIME `audio/*` → `VOICE_NOTE`
- If message text contains a URL:
  - Match against YouTube domains (`youtube.com/watch`, `youtu.be`) → `YOUTUBE_URL`
  - Match against Instagram (`instagram.com/reel`, `instagram.com/p`), TikTok (`tiktok.com`), Facebook (`facebook.com/reel`, `fb.watch`) → `REEL_URL`
  - Anything else with http/https → `UNKNOWN_URL`
- If message is plain text with no URL and no attachment → `TEXT`
- If message has both a URL and extra text after the URL, the extra text is `user_note`

### 4.2 Extractor (`extractor/`)

#### video.py — VideoExtractor
Handles `YOUTUBE_URL` and `REEL_URL`.

```
1. Use yt-dlp to download audio-only (bestaudio, mp3, max 25MB)
   - Set yt-dlp options: no cookies, quiet=True, no_progress, temp dir in /tmp/stash/
   - Timeout: 60 seconds total for download
2. If download fails (geo-block, auth-wall, rate-limit, private):
   - Set extraction_failed=True, extraction_error=<yt-dlp stderr>
   - Return ContentPacket with user_note preserved — bot will ask user to add note
3. If download succeeds:
   - Call Groq Whisper (whisper-large-v3) with audio file
   - Chunk audio if > 25MB using ffmpeg (split at silence boundaries)
   - Set transcript in ContentPacket
4. Always delete temp audio files in a finally block
```

**Do not attempt to download video frames.** Audio + Whisper transcript is sufficient. Gemini will categorize from the transcript.

#### image.py — ImageExtractor
Handles `IMAGE`.

```
1. Read Discord attachment bytes
2. Validate: must be image/png, image/jpeg, image/webp, image/gif — reject others
3. Size limit: reject if > 20MB (Gemini Files API limit)
4. Return ContentPacket with image_bytes and image_mime set
```

No OCR pre-processing. Gemini Vision handles reading the image directly.

#### audio.py — AudioExtractor
Handles `VOICE_NOTE`.

```
1. Download Discord audio attachment to /tmp/stash/
2. Convert to mp3 via ffmpeg if not already
3. Send to Groq Whisper → transcript
4. Delete temp file in finally block
5. Return ContentPacket with transcript set
```

### 4.3 TaxonomyCache (`taxonomy.py`)

**This is critical for performance and correctness. Do not skip.**

```python
class TaxonomyCache:
    spaces: list[dict]      # [{id, name, ...}]
    tags: list[str]         # flat list of tag names
    last_refreshed: datetime
    REFRESH_INTERVAL = timedelta(minutes=10)
```

Lifecycle:
- `await cache.initialize()` → called once at bot startup, fetches spaces + tags from mymind
- `cache.get_taxonomy_for_prompt()` → returns formatted string for injection into Gemini prompt
- `await cache.refresh_if_stale()` → checks `last_refreshed`, fetches if expired — called at start of each categorization
- `await cache.on_new_space_created(name)` → appends to local cache immediately without full refresh
- `await cache.on_new_tag_created(name)` → same

**Never fetch from mymind on every request.** The cache is the source of truth during a session. The 10-minute refresh is the reconciliation mechanism.

### 4.4 Categorizer (`categorizer.py`)

Receives `ContentPacket` + `TaxonomyCache`. Returns `CategoryResult`.

#### Model selection
- Primary: **Vertex AI Gemini 2.5 Pro** (for images and long transcripts)
- Fallback: **Vertex AI Gemini 2.5 Flash** (if Pro quota exceeded or latency > 10s)
- Use `google-cloud-aiplatform` SDK directly — NOT OpenRouter (Vertex credits only work via Vertex SDK)

#### Prompt structure

```
SYSTEM:
You are a personal knowledge assistant. Your job is to categorize content 
into the user's second brain. Be concise and accurate.

USER:
Here is the user's existing taxonomy in mymind:
SPACES (categories): {space_names}
TAGS: {tag_names}

Content to categorize:
Type: {content_type}
{if transcript} Transcript: {transcript} {/if}
{if user_note} User note: {user_note} {/if}
{if image} [image attached] {/if}

Instructions:
1. Infer the topic from the FULL content provided (transcript, image, or text).
2. Choose the best matching SPACE from the existing list. If none fits well, propose a new space name.
3. Select 2–4 relevant tags from the existing list. You may add 1 new tag if clearly needed.
4. Write a 1–2 sentence summary of what this content is actually about.
5. Rate your confidence 0.0–1.0. Be honest. If the content was unclear or extraction failed, score lower.

Respond ONLY with valid JSON. No markdown fences, no preamble.
{
  "title": "...",
  "category": "...",
  "tags": ["...", "..."],
  "summary": "...",
  "is_new_category": true/false,
  "confidence": 0.0,
  "reasoning": "..."
}
```

For image inputs, pass `image_bytes` as a Vertex AI `Part.from_data(mime_type=..., data=...)` alongside the text prompt.

#### Confidence threshold
- `>= 0.75` → auto-save, bot confirms with card details
- `< 0.75` → bot sends a confirmation message with the proposed category/tags and waits for user reply
  - User can reply `ok`, `yes`, a new category name, or new tags
  - Timeout: 5 minutes. If no reply, save anyway with `low_confidence=True` tag added

### 4.5 Gateway (`gateway/`)

#### interface.py
```python
from typing import Protocol

class MindGateway(Protocol):
    async def save_url(self, url: str, title: str, tags: list[str], space: str, note: str) -> SavedCard: ...
    async def save_note(self, text: str, title: str, tags: list[str], space: str) -> SavedCard: ...
    async def save_image(self, image_bytes: bytes, mime: str, title: str, tags: list[str], space: str, note: str) -> SavedCard: ...
    async def get_spaces(self) -> list[dict]: ...
    async def get_tags(self) -> list[str]: ...
    async def create_space(self, name: str) -> dict: ...
```

#### mymind.py — MyMindGateway
Wraps `iamumeransari/mymind-api` Python client.

Key behaviors:
- Credentials (`MYMIND_EMAIL`, `MYMIND_PASSWORD` or session tokens) loaded from env, never hardcoded
- Token stored only in memory (never written to disk in production)
- Auto-retry on 401: attempt token refresh once, then raise `AuthError`
- Rate limiting: add `asyncio.sleep(0.5)` between consecutive mymind calls to avoid triggering their internal limits (they are undocumented but empirically ~2 req/sec is safe)
- All responses parsed into internal dataclasses — raw mymind JSON never leaks out of this module

#### sandbox.py — SandboxGateway
**This is the default gateway in dev/test mode.** When `STASH_ENV=sandbox`, all saves go to a local JSON file at `./sandbox_data.json`. No network calls to mymind. Structure mirrors mymind's data model exactly so integration tests are meaningful.

```json
{
  "spaces": [{"id": "1", "name": "Career"}],
  "tags": ["resume", "interview"],
  "cards": [
    {
      "id": "abc123",
      "title": "3 Resume Tips",
      "category": "Career",
      "tags": ["resume"],
      "summary": "...",
      "saved_at": "2026-05-23T12:00:00Z"
    }
  ]
}
```

---

## 5. Security Requirements

This section is non-negotiable. The mymind API is unofficial and reverse-engineered. The attack surface is: credentials leaking, user content leaking to logs, and malicious content in Discord messages being injected into AI prompts.

### 5.1 Credential Handling
- All secrets in `.env` only: `DISCORD_TOKEN`, `MYMIND_KID`, `MYMIND_SECRET`, `GOOGLE_APPLICATION_CREDENTIALS`, `GROQ_API_KEY`
- `.env` is in `.gitignore` — committed `.env.example` has only key names, no values
- Secrets never logged at any log level, including `DEBUG`
- `config.py` validates all required env vars at startup and raises `ConfigError` immediately if any are missing — no silent fallbacks

### 5.2 Prompt Injection Defense
User-supplied content (URLs, text, transcripts, OCR'd text from images) is untrusted and may contain adversarial text designed to manipulate the Gemini prompt.

Mitigations in `security.py`:
```python
def sanitize_for_prompt(text: str, max_chars: int = 8000) -> str:
    # 1. Truncate to max_chars to prevent context flooding
    text = text[:max_chars]
    # 2. Remove null bytes and non-printable control characters (except newlines/tabs)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # 3. Escape sequences that could break the JSON structure of the prompt
    text = text.replace('```', '~~~')  # prevent markdown fence injection
    return text
```

Apply `sanitize_for_prompt()` to: `transcript`, `user_note`, `page_text` before injecting into any prompt. Do NOT apply to `image_bytes` (binary data, no injection risk).

**Role separation in prompts:** The Gemini prompt always has a fixed SYSTEM role with instructions. User content only appears in the USER turn, clearly labeled. Never interpolate user content into the SYSTEM prompt string.

### 5.3 Data in Transit
- mymind API calls go over HTTPS (enforced by the mymind-api client)
- Vertex AI SDK uses Google's auth (service account) — credentials via `GOOGLE_APPLICATION_CREDENTIALS` env var pointing to a service account JSON file
- Groq calls over HTTPS

### 5.4 Temp File Cleanup
All temp files in `/tmp/stash/` must be deleted in `finally` blocks, even on exceptions. Use `tempfile.NamedTemporaryFile(dir="/tmp/stash/", delete=False)` and track paths explicitly.

On bot startup, wipe `/tmp/stash/` if it exists (leftover files from previous crash).

### 5.5 Discord Authorization
The bot should only respond to a specific Discord user ID (`STASH_OWNER_ID` env var). All other messages are silently ignored. This is a personal tool — not a public bot.

```python
if message.author.id != config.OWNER_ID:
    return  # silently drop
```

### 5.6 mymind Sandbox Isolation
When `STASH_ENV=sandbox`, the `MyMindGateway` is never instantiated. Code paths that would call mymind in production go to `SandboxGateway` instead. This ensures that during development, a bug cannot accidentally write garbage to the user's real mymind account.

Enforced at the gateway factory level:
```python
def create_gateway(config) -> MindGateway:
    if config.ENV == "sandbox":
        return SandboxGateway(config.SANDBOX_FILE)
    elif config.ENV == "production":
        return MyMindGateway(config)
    else:
        raise ConfigError(f"Unknown STASH_ENV: {config.ENV}")
```

---

## 6. Error Handling & User Feedback

The bot must always reply to the user, even on failure. Silent failures are unacceptable — the user dropped something into the void and got nothing back.

| Scenario | Bot Response |
|---|---|
| Video download fails (geo-block, private, rate-limited) | "Couldn't grab the audio from that link. Add a quick note about what it covers and I'll save it manually." |
| Groq Whisper fails | "Transcription failed. Saving with title and URL only. Tags: [proposed]" |
| Gemini quota exceeded | "AI categorization unavailable right now. Saved to Inbox for later review." |
| mymind save fails (5xx) | "mymind is having issues. I'll retry in 30 seconds... [retry result]" |
| mymind auth expired | "mymind credentials expired. Please re-run auth setup." |
| Confidence < 0.75 | "Proposed: [Career > Resume Tips]. Reply 'ok' to confirm or type a different category." |
| Unknown content type | "Not sure what to do with this. Is this a link, a note, or something else?" |

---

## 7. Dependencies

```
# requirements.txt
discord.py>=2.3.0
yt-dlp>=2024.1.0
groq>=0.5.0
google-cloud-aiplatform>=1.50.0
mymind-api @ git+https://github.com/iamumeransari/mymind-api  # pin to a commit hash
python-dotenv>=1.0.0
pydantic>=2.0.0          # for config validation
ffmpeg-python>=0.2.0     # ffmpeg wrapper
aiofiles>=23.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

System dependencies (installed in Dockerfile):
```
ffmpeg
```

---

## 8. Deployment

### Dockerfile
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /tmp/stash
CMD ["python", "bot.py"]
```

### Environment variables (`.env.example`)
```
DISCORD_TOKEN=
STASH_OWNER_ID=             # your Discord user ID (integer)
STASH_ENV=sandbox           # "sandbox" or "production"
MYMIND_KID=                 # from access.mymind.com/extensions
MYMIND_SECRET=              # from access.mymind.com/extensions
GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-key.json
GROQ_API_KEY=
SANDBOX_FILE=./sandbox_data.json
FFMPEG_LOCATION=         # optional: path to ffmpeg/ffprobe binaries if not on PATH
```

### Deploy target
Railway.app — connect GitHub repo, set env vars in Railway dashboard, deploy. The `Dockerfile` is the build target.

**Do NOT commit `gcp-key.json` or `.env`.** Add both to `.gitignore`.

---

## 9. Build Order for Claude Code

Build in this sequence. Each step should be independently runnable/testable before moving on.

1. `models.py` — dataclasses only, no dependencies
2. `config.py` — env loading and validation
3. `gateway/interface.py` + `gateway/sandbox.py` — no external deps, fully testable
4. `taxonomy.py` — depends on gateway interface only
5. `security.py` — pure functions, no deps
6. `extractor/image.py` — simple bytes handling
7. `extractor/audio.py` — Groq dep
8. `extractor/video.py` — yt-dlp + Groq
9. `categorizer.py` — Vertex AI dep
10. `router.py` — discord.py dep
11. `gateway/mymind.py` — mymind-api dep (test against sandbox first)
12. `bot.py` — wires everything together
13. Tests for each module
14. `Dockerfile` + `README.md`

---

## 10. What NOT to Build

- No web dashboard
- No database (mymind is the store; sandbox JSON is for dev only)
- No multi-user support (single owner, hardcoded by Discord user ID)
- No message history / conversation threading (each Discord message is independent)
- No retry queue / job system (keep it simple: retry once inline, surface error to user)
- No OpenRouter — use Vertex SDK directly for all Gemini calls

---

## 11. Open Questions (Decide Before Building)

1. **mymind image upload:** The `iamumeransari/mymind-api` client supports `save_url` and `create_note`. Confirm it supports uploading raw image bytes before implementing `gateway/mymind.py`. If not, save images as notes with a base64 embed or skip image upload to mymind (just categorize and save metadata).

2. **Instagram/TikTok yt-dlp reliability:** Test locally before assuming it works. If yt-dlp fails consistently on reels, the fallback (ask user for a note) becomes the primary flow for that content type. Document which platforms work.

3. **Vertex AI service account:** You need a GCP project with Vertex AI API enabled and a service account with `roles/aiplatform.user`. Create this before running the bot.

4. **mymind token refresh:** The `iamumeransari/mymind-api` client stores tokens in system keychain on desktop. In a containerized deployment, this won't work. Investigate whether `MYMIND_KID` + `MYMIND_SECRET` from `access.mymind.com/extensions` are long-lived static keys (the `nawwal/mymind-mcp` approach) or short-lived OAuth tokens. If they're static, use them directly. If they expire, you need a headless re-auth flow.
