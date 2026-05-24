# Stash

Personal Discord bot that captures content (links, screenshots, voice notes, YouTube/reel URLs) and saves them as categorized cards in [mymind](https://mymind.com).

## How it works

1. Drop content into a Discord DM or channel
2. Bot extracts meaning (transcribes audio, reads images, parses URLs)
3. Gemini categorizes against your existing mymind taxonomy
4. Card is saved to mymind with title, category, tags, and summary

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
| `MYMIND_KID` | `access.mymind.com/extensions` |
| `MYMIND_SECRET` | `access.mymind.com/extensions` |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service account JSON with Vertex AI access |
| `GROQ_API_KEY` | [Groq Console](https://console.groq.com) |

### Optional settings

| Variable | Purpose |
|----------|---------|
| `FFMPEG_LOCATION` | Path to ffmpeg/ffprobe binaries if not on PATH (e.g., `/usr/bin` or `C:\ffmpeg\bin`) |

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

### Deploy to Railway

1. Connect GitHub repo to Railway
2. Set all env vars in Railway dashboard
3. Railway auto-detects the Dockerfile

## Testing

```bash
pytest -v
```

## Architecture

```
Discord message -> InputRouter -> Extractor -> Categorizer -> Gateway -> mymind
                                                                      -> Discord reply
```

- **InputRouter**: Classifies messages by content type
- **Extractors**: video (yt-dlp + Whisper), audio (Whisper), image (passthrough)
- **Categorizer**: Gemini 2.5 Pro/Flash with taxonomy-aware prompting
- **Gateway**: mymind API wrapper (production) or local JSON (sandbox)
