from __future__ import annotations

import asyncio
import functools
import os
import uuid
from pathlib import Path

import yt_dlp

from stash.extractor.audio import convert_to_mp3, resolve_ffmpeg_locations, transcribe_with_groq
from stash.models import ContentPacket, ContentType

_REEL_PLATFORMS = {"instagram", "tiktok", "facebook", "fb.watch"}


def _is_reel_platform(url: str) -> bool:
    lower = url.lower()
    return any(p in lower for p in _REEL_PLATFORMS)


def _parse_ytdlp_error(stderr: str, url: str) -> str:
    lower = stderr.lower()
    if "sign in to confirm your age" in lower:
        return "This video requires age verification — can't download without login."
    if "sign in to confirm" in lower or "not a bot" in lower:
        return "YouTube is requiring verification — can't download from this environment."
    if "private video" in lower:
        return "This video is private and can't be accessed."
    if "video unavailable" in lower:
        return "This video is unavailable (deleted or region-locked)."
    if _is_reel_platform(url):
        return "Couldn't download reel audio. Add a note about what it covers and I'll save it."
    return f"Download failed: {stderr[:150]}"


async def extract_video(
    url: str,
    content_type: ContentType,
    user_note: str | None = None,
    tmp_dir: str = "/tmp/stash",
) -> ContentPacket:
    os.makedirs(tmp_dir, exist_ok=True)
    download_id = str(uuid.uuid4())
    output_template = os.path.join(tmp_dir, f"{download_id}.%(ext)s")
    downloaded_path: str | None = None
    mp3_path: str | None = None

    try:
        _, ffmpeg_location = resolve_ffmpeg_locations()
        ydl_opts = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 30,
            "nocheckcertificate": False,
            "no_color": True,
        }
        if ffmpeg_location:
            ydl_opts["ffmpeg_location"] = ffmpeg_location

        loop = asyncio.get_running_loop()
        try:
            downloaded_path = await asyncio.wait_for(
                loop.run_in_executor(None, functools.partial(_download, ydl_opts, url, tmp_dir, download_id)),
                timeout=60,
            )
        except asyncio.TimeoutError:
            return ContentPacket(
                content_type=content_type,
                raw_input=url,
                source_url=url,
                user_note=user_note,
                extraction_failed=True,
                extraction_error="Download timed out after 60 seconds.",
            )
        except yt_dlp.utils.DownloadError as e:
            error_msg = _parse_ytdlp_error(str(e), url)
            return ContentPacket(
                content_type=content_type,
                raw_input=url,
                source_url=url,
                user_note=user_note,
                extraction_failed=True,
                extraction_error=error_msg,
            )

        if not downloaded_path or not os.path.exists(downloaded_path):
            error_msg = (
                "Couldn't download reel audio. Add a note about what it covers and I'll save it."
                if _is_reel_platform(url)
                else "Download produced no output file."
            )
            return ContentPacket(
                content_type=content_type,
                raw_input=url,
                source_url=url,
                user_note=user_note,
                extraction_failed=True,
                extraction_error=error_msg,
            )

        if downloaded_path.lower().endswith(".mp3"):
            mp3_path = downloaded_path
        else:
            mp3_path, err = await convert_to_mp3(downloaded_path, tmp_dir)
            if err:
                return ContentPacket(
                    content_type=content_type,
                    raw_input=url,
                    source_url=url,
                    user_note=user_note,
                    extraction_failed=True,
                    extraction_error=err,
                )

        transcript = await transcribe_with_groq(mp3_path)

        if not transcript or not transcript.strip():
            return ContentPacket(
                content_type=content_type,
                raw_input=url,
                source_url=url,
                user_note=user_note,
                extraction_failed=True,
                extraction_error="No speech detected in audio",
            )

        return ContentPacket(
            content_type=content_type,
            raw_input=url,
            source_url=url,
            transcript=transcript.strip(),
            user_note=user_note,
        )

    except Exception as e:
        error_msg = (
            "Couldn't download reel audio. Add a note about what it covers and I'll save it."
            if _is_reel_platform(url)
            else f"Video extraction failed: {str(e)[:200]}"
        )
        return ContentPacket(
            content_type=content_type,
            raw_input=url,
            source_url=url,
            user_note=user_note,
            extraction_failed=True,
            extraction_error=error_msg,
        )
    finally:
        for path in (downloaded_path, mp3_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        # Clean any leftover files from this download_id (partial downloads, intermediate formats)
        for leftover in Path(tmp_dir).glob(f"{download_id}.*"):
            try:
                leftover.unlink()
            except OSError:
                pass


def _download(ydl_opts: dict, url: str, tmp_dir: str, download_id: str) -> str | None:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # yt-dlp with FFmpegExtractAudio postprocessor outputs .mp3
    expected = os.path.join(tmp_dir, f"{download_id}.mp3")
    if os.path.exists(expected):
        return expected

    # Fallback: find any file matching the download_id
    for f in Path(tmp_dir).glob(f"{download_id}.*"):
        return str(f)

    return None
