# Stash

Stash is a single-owner Discord bot for personal knowledge capture and
mymind library management. This README is for operators and developers who
need to run, deploy, or extend the bot. Discord is the user interface,
mymind is the storage layer, and the Python bot process connects capture,
categorization, and agentic library-management workflows.

It still captures content (links, screenshots, voice notes,
YouTube/reel URLs) and saves categorized cards in [mymind](https://mymind.com).
As of v2.1.1, plain-text Discord messages can also manage the library through
a Gemini function-calling agent.

## How it works

1. Drop content into a Discord DM or channel
2. Bot extracts meaning (transcribes audio, reads images, parses URLs)
3. Gemini categorizes against your existing mymind taxonomy
4. Card is saved to mymind with title, category, tags, and summary

Plain-text messages with no URL or attachment now take a second path:

1. Prefix commands such as `/help`, `/model`, and `/stats` are handled locally
2. Other text goes to the Gemini agent
3. Gemini selects a gateway-backed tool when live mymind data or mutation is needed
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
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service account JSON with Vertex AI access |
| `GROQ_API_KEY` | [Groq Console](https://console.groq.com) |

Production mymind auth is cookie-based in the current code:

| Variable | Source |
|----------|--------|
| `MYMIND_JWT` | Exported by `python scripts/export_cookies.py` |
| `MYMIND_CID` | Exported by `python scripts/export_cookies.py` |
| `MYMIND_AUTHENTICITY_TOKEN` | Exported by `python scripts/export_cookies.py` |

`MYMIND_KID` and `MYMIND_SECRET` are older setup assumptions and are not read
by `stash.config` or `stash.gateway.mymind`.

### Optional settings

| Variable | Purpose |
|----------|---------|
| `FFMPEG_LOCATION` | Path to ffmpeg/ffprobe binaries if not on PATH (e.g., `/usr/bin` or `C:\ffmpeg\bin`) |
| `GCP_PROJECT_ID` | Vertex AI project ID used by `vertexai.init` |
| `GCP_LOCATION` | Vertex AI region, default `us-central1` |
| `GCP_CREDENTIALS_JSON` | Base64 service-account JSON; decoded to `/tmp/stash/gcp-key.json` at startup |
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

### Deploy to Railway (zero mymind credits)

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

## Testing

```bash
python -m pytest -q
```

Current local verification for the June 21 codebase: `172 passed`.

## Architecture

```
Discord message -> InputRouter -> Extractor -> Categorizer -> Gateway -> mymind
                                                                      -> Discord reply
```

- **InputRouter**: Classifies messages by content type
- **Extractors**: video (yt-dlp + Whisper), audio (Whisper), image (passthrough)
- **Categorizer**: Gemini 2.5 Flash with Pro fallback and taxonomy-aware prompting
- **Gateway**: mymind API wrapper (production) or local JSON (sandbox)
- **Agent**: Gemini function-calling layer for text-only library-management messages
- **Tools**: Gateway-backed actions for list/search/create/save/move/delete/stats/recent/random

## Current Discord Commands

- `/help` - show the in-Discord guide
- `/model` - show current agent model
- `/model flash` - use Gemini 2.5 Flash for the agent
- `/model pro` - use Gemini 2.5 Pro for the agent
- `/stats` - show library stats without a Gemini round trip

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
path, then general Gemini inference.

## Operational Gotchas

- Plain text now routes to the agent path. A plain note is saved only when
  the agent selects the `save_note` tool.
- The agent UI reports a fallback model, but `stash.agent.handle_text` does
  not currently retry with that fallback after timeout or exception.
- The categorizer path does implement Flash -> Pro fallback.
- The production gateway uses unofficial mymind internals and cookie auth.
- `mymind-api` is installed from upstream `main`, so rebuilds can change
  behavior without a Stash commit.
- Runtime agent settings are stored in `stash_settings.json`; in production
  the file lives under `TMP_DIR`, which may not persist across restarts.
