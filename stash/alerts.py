from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

logger = logging.getLogger(__name__)


async def send_alert(
    bot: discord.Client,
    owner_id: int,
    title: str,
    error: Exception,
    context: str | None = None,
) -> None:
    env = "production"
    try:
        import os
        env = os.environ.get("STASH_ENV", "unknown")
    except Exception:
        pass

    error_text = f"{type(error).__name__}: {str(error)[:200]}"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    parts = [
        f"**{title}**",
        f"**Error:** {error_text}",
    ]
    if context:
        parts.append(f"**Context:** {context}")
    parts.append(f"**Time:** {timestamp}")
    parts.append(f"**Env:** {env}")

    msg = "\n".join(parts)

    try:
        user = await bot.fetch_user(owner_id)
        dm = await user.create_dm()
        await dm.send(msg)
    except Exception:
        pass


class DiscordAlertHandler(logging.Handler):
    """Sends ERROR and CRITICAL log records to Discord DM with cooldown."""

    COOLDOWN_SECONDS = 60

    def __init__(self, bot: discord.Client, owner_id: int) -> None:
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.owner_id = owner_id
        self._cooldown: dict[str, datetime] = {}

    def emit(self, record: logging.LogRecord) -> None:
        key = record.getMessage()[:50]
        now = datetime.now(timezone.utc)

        if key in self._cooldown:
            elapsed = (now - self._cooldown[key]).total_seconds()
            if elapsed < self.COOLDOWN_SECONDS:
                return

        self._cooldown[key] = now

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send(record))
        except RuntimeError:
            pass

    async def _send(self, record: logging.LogRecord) -> None:
        emoji = "🚨" if record.levelno >= logging.CRITICAL else "⚠️"
        msg = (
            f"{emoji} **{record.levelname}** in `{record.name}`\n"
            f"```{record.getMessage()[:300]}```\n"
            f"`{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`"
        )
        try:
            user = await self.bot.fetch_user(self.owner_id)
            dm = await user.create_dm()
            await dm.send(msg)
        except Exception:
            pass
