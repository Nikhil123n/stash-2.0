# Stash

Stash is a single-owner Discord bot for personal knowledge capture and
mymind library management. This README is for operators and developers who
need to run, deploy, or extend the bot. Discord is the user interface,
mymind is the storage layer, and the Python bot process connects capture,
categorization, and agentic library-management workflows.

It still captures content (links, screenshots, voice notes,
YouTube/reel URLs) and saves categorized cards in [mymind](https://mymind.com).
As of v2.3.0, plain-text Discord messages can also manage the library through
a Groq/OpenRouter function-calling agent — Stash runs entirely on free-tier
LLM providers, no Gemini/Vertex AI dependency.

## How it works

1. Drop content into a Discord DM or channel
2. Bot extracts meaning (transcribes audio, reads images, parses URLs)
3. The categorizer classifies against your existing mymind taxonomy
4. Card is saved to mymind with title, category, tags, and summary

Plain-text messages with no URL or attachment now take a second path:

1. Prefix commands such as `/help`, `/model`, and `/stats` are handled locally
2. Other text goes to the agent
3. The agent selects a gateway-backed tool when live mymind data or mutation is needed
4. Destructive tools (`move_card_to_space`, `delete_card`) require confirmation

Examples:

```text
list my spaces
what's in the Claude space?
find cards about python
show me my recent saves
what did I save this week?
surprise me
save a note: review weekly planning system
move card <id> to Tech
delete card <id>
```

## Setup

```bash
cp .env.example .env
# Fill in all values in .env
```

### Required credentials

| Variable | Source |
|----------|--------|
| `DISCORD_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) |
| `STASH_OWNER_ID` | Your Discord user ID (enable Developer Mode, right-click yourself) |
| `GROQ_API_KEY` | [Groq Console](https://console.groq.com) — primary provider for agent + categorizer |
| `OPENROUTER_API_KEY` | [OpenRouter dashboard](https://openrouter.ai/keys) — fallback provider + all image/vision categorization |

### mymind auth — two schemes, only one active

`main` uses **cookie-based** auth exclusively. A separate, unmaintained
**JWT-key-based** scheme exists only on the archived `approach/jwt` branch.
Don't mix the two — `stash.config`/`stash.gateway.mymind` on `main` only ever
read the cookie-based set below.

**Cookie-based (active on `main`)**

| Variable | Source |
|----------|--------|
| `MYMIND_JWT` | Exported by `python scripts/export_cookies.py` |
| `MYMIND_CID` | Exported by `python scripts/export_cookies.py` |
| `MYMIND_AUTHENTICITY_TOKEN` | Exported by `python scripts/export_cookies.py` |

**JWT-key-based (`approach/jwt` branch only — not read by `main`)**

| Variable | Source |
|----------|--------|
| `MYMIND_KID` | mymind API key ID (that branch's own setup flow) |
| `MYMIND_SECRET` | mymind API key secret (that branch's own setup flow) |

### Optional settings

| Variable | Purpose |
|----------|---------|
| `FFMPEG_LOCATION` | Path to ffmpeg/ffprobe binaries if not on PATH (e.g., `/usr/bin` or `C:\ffmpeg\bin`) |
| `SANDBOX_FILE` | Sandbox JSON path, default `./sandbox_data.json` |
| `TMP_DIR` | Temp media/settings directory, default `/tmp/stash` |

### Run locally (sandbox mode)

```bash
pip install -r requirements.txt
STASH_ENV=sandbox python -m stash.bot
```

Sandbox mode saves to a local JSON file instead of mymind.

### Run with Docker

```bash
docker build -t stash .
docker run --env-file .env stash
```

### Deploy to Northflank (current)

The `Dockerfile` is the build target — no code changes needed versus any
other host.

1. Run locally first to authenticate with mymind:
   ```bash
   mymind login
   ```
   Complete the browser login when prompted so cookies are available in the
   local keyring.

2. Export cookies:
   ```bash
   python scripts/export_cookies.py
   ```

3. Connect this GitHub repo in the Northflank dashboard and create a service
   from it (Dockerfile build). Add the three cookie-based `MYMIND_*` values
   plus the other required vars (see tables above) under that service's
   environment variables.

4. Deploy. Bot runs with zero mymind API credits.

5. When bot DMs you "needs re-auth":
   Repeat steps 1-2, update the Northflank env vars, redeploy (~2 minutes).

<details>
<summary>Archived: Railway deployment (used until Aug 2026)</summary>

Railway's free trial ran out, so production moved to Northflank. These steps
are kept for reference only — do not use them for new deploys.

1. Run locally first to authenticate with mymind:
   ```bash
   mymind login
   ```
   Complete the browser login when prompted so cookies are available in the
   local keyring.

2. Export cookies for Railway:
   ```bash
   python scripts/export_cookies.py
   ```

3. Add the three `MYMIND_*` values to Railway environment variables,
   along with the other required vars.

4. Deploy. Bot runs with zero mymind API credits.

5. When bot DMs you "needs re-auth":
   Repeat steps 1-3, update Railway vars, redeploy (~2 minutes).

</details>

## Testing

```bash
python -m pytest -q
```

Current local verification: `174 passed`.

## Architecture

```
Discord message -> InputRouter -> Extractor -> Categorizer -> Gateway -> mymind
                                                                      -> Discord reply
```

- **InputRouter**: Classifies messages by content type
- **Extractors**: video (yt-dlp + Whisper), audio (Whisper), image (passthrough)
- **Categorizer**: two free-tier chains, cross-provider. Text: Groq Llama
  3.3 70B -> OpenRouter GPT-OSS 20B -> Groq GPT-OSS 120B. Images: OpenRouter
  Gemma 4 26B -> OpenRouter Nemotron Nano Omni (Groq has no free vision
  model); taxonomy-aware prompting
- **Gateway**: mymind API wrapper (production) or local JSON (sandbox)
- **Agent**: Groq/OpenRouter function-calling layer for text-only
  library-management messages, with automatic fallback on failure
- **Tools**: Gateway-backed actions for list/search/create/save/move/delete/stats/recent/random

## Current Discord Commands

- `/help` - show the in-Discord guide
- `/model` - show current agent model
- `/model flash` - use Llama 3.3 70B via Groq for the agent (default)
- `/model pro` - use GPT-OSS 20B via OpenRouter for the agent
- `/stats` - show library stats without a model round trip

Every save reply and card-listing response now includes the mymind card ID
where available, so follow-up move/delete commands can reference it directly.

## Space Directives

User notes can force a target space before general categorization runs:

```text
save to LinkedIn <url>
put this video in Claude <url>
move this card into the LinkedIn space <url>
-> Tech <url>
in: Career Development <url>
```

Directive precedence is: explicit directive, Claude/Anthropic keyword fast
path, then general model inference.

## Operational Gotchas

- Plain text now routes to the agent path. A plain note is saved only when
  the agent selects the `save_note` tool.
- The agent retries once against `settings.fallback_for(model)` on
  timeout/error before giving up (`stash.agent._call_with_agent_fallback`).
- The categorizer runs two independent 2-3 tier cross-provider fallback
  chains — text and vision — see `stash.categorizer.MODEL_CHAIN` /
  `VISION_MODEL_CHAIN`. Free-tier model catalogs change frequently; if a
  model id 404s, check the provider's live `/models` endpoint before
  assuming the code is wrong.
- The production gateway uses unofficial mymind internals and cookie auth.
- `mymind-api` is installed from upstream `main`, so rebuilds can change
  behavior without a Stash commit.
- Runtime agent settings are stored in `stash_settings.json`; in production
  the file lives under `TMP_DIR`, which may not persist across restarts.

## License

MIT — see [LICENSE](LICENSE).
