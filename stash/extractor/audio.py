from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from stash.models import ContentPacket, ContentType

SUPPORTED_MIMES = {
    "audio/ogg",
    "audio/opus",
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
}

MAX_AUDIO_SIZE = 20_971_520  # 20MB
WHISPER_MODEL = "whisper-large-v3"


def resolve_ffmpeg_locations() -> tuple[str | None, str | None]:
    location = os.environ.get("FFMPEG_LOCATION")
    if not location:
        return None, None
    location = os.path.expanduser(location.strip())
    if os.path.isdir(location):
        ffmpeg_bin = os.path.join(location, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        return ffmpeg_bin, location
    return location, location


async def convert_to_mp3(src_path: str, tmp_dir: str = "/tmp/stash") -> tuple[str | None, str | None]:
    """Convert audio file to 16kHz mono mp3. Returns (mp3_path, error_message)."""
    mp3_path = src_path.rsplit(".", 1)[0] + ".mp3"

    if src_path.lower().endswith(".mp3"):
        return src_path, None

    ffmpeg_cmd, _ = resolve_ffmpeg_locations()
    ffmpeg_cmd = ffmpeg_cmd or "ffmpeg"
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_cmd, "-i", src_path, "-vn", "-ar", "16000",
            "-ac", "1", "-b:a", "64k", "-f", "mp3", "-y", mp3_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "FFmpeg not found. Install ffmpeg or set FFMPEG_LOCATION."
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        return None, f"FFmpeg conversion failed: {stderr.decode(errors='replace')[:200]}"

    return mp3_path, None


async def transcribe_with_groq(mp3_path: str) -> str:
    """Transcribe mp3 file using Groq Whisper. Returns transcript text."""
    from groq import AsyncGroq

    client = AsyncGroq()

    with open(mp3_path, "rb") as audio_file:
        response = await client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=audio_file,
            response_format="text",
        )

    return response


async def extract_audio(
    audio_bytes: bytes,
    mime_type: str,
    filename: str,
    raw_input: str,
    user_note: str | None = None,
    tmp_dir: str = "/tmp/stash",
) -> ContentPacket:
    if mime_type not in SUPPORTED_MIMES:
        return ContentPacket(
            content_type=ContentType.VOICE_NOTE,
            raw_input=raw_input,
            user_note=user_note,
            extraction_failed=True,
            extraction_error=f"Unsupported audio format: {mime_type}",
        )

    os.makedirs(tmp_dir, exist_ok=True)
    src_path: str | None = None
    mp3_path: str | None = None

    try:
        suffix = Path(filename).suffix or ".ogg"
        src_fd = tempfile.NamedTemporaryFile(
            dir=tmp_dir, suffix=suffix, delete=False
        )
        src_path = src_fd.name
        src_fd.write(audio_bytes)
        src_fd.close()

        if mime_type in ("audio/mpeg", "audio/mp3") and suffix.lower() == ".mp3":
            mp3_path = src_path
        else:
            mp3_path, err = await convert_to_mp3(src_path, tmp_dir)
            if err:
                return ContentPacket(
                    content_type=ContentType.VOICE_NOTE,
                    raw_input=raw_input,
                    user_note=user_note,
                    extraction_failed=True,
                    extraction_error=err,
                )

        transcript = await transcribe_with_groq(mp3_path)

        if not transcript or not transcript.strip():
            return ContentPacket(
                content_type=ContentType.VOICE_NOTE,
                raw_input=raw_input,
                user_note=user_note,
                extraction_failed=True,
                extraction_error="No speech detected in audio",
            )

        return ContentPacket(
            content_type=ContentType.VOICE_NOTE,
            raw_input=raw_input,
            transcript=transcript.strip(),
            user_note=user_note,
        )

    except asyncio.TimeoutError:
        return ContentPacket(
            content_type=ContentType.VOICE_NOTE,
            raw_input=raw_input,
            user_note=user_note,
            extraction_failed=True,
            extraction_error="Audio conversion timed out",
        )
    except Exception as e:
        return ContentPacket(
            content_type=ContentType.VOICE_NOTE,
            raw_input=raw_input,
            user_note=user_note,
            extraction_failed=True,
            extraction_error=f"Audio extraction failed: {str(e)[:200]}",
        )
    finally:
        for path in (src_path, mp3_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def check_audio_size(attachment_size: int) -> str | None:
    if attachment_size > MAX_AUDIO_SIZE:
        return f"Audio file too large: {attachment_size / 1_048_576:.1f}MB (max 20MB). Please send a shorter voice note."
    return None
