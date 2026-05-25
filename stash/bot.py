from __future__ import annotations

import asyncio
import logging
import os
import shutil

import discord

from stash.categorizer import categorize
from stash.config import load_config, StashConfig
from stash.extractor.audio import check_audio_size, extract_audio
from stash.extractor.image import extract_image
from stash.extractor.video import extract_video
from stash.gateway import create_gateway
from stash.gateway.interface import MindGateway
from stash.models import CategoryResult, ContentPacket, ContentType, SavedCard
from stash.router import route_message
from stash.taxonomy import TaxonomyCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stash")

CONFIDENCE_THRESHOLD = 0.75
CONFIRMATION_TIMEOUT = 300  # 5 minutes
GATEWAY_MODE = "cookie-local"  # "jwt-railway" on main branch, "cookie-local" on stable/cookie-local
VERSION = "2.0.0"


class StashBot(discord.Client):
    def __init__(self, config: StashConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self._config = config
        self._gateway: MindGateway | None = None
        self._taxonomy: TaxonomyCache | None = None
        self._awaiting_confirmation: set[int] = set()  # channel IDs waiting for user reply
        self._processed: set[int] = set()  # message IDs already handled (kept permanently)

    async def setup_hook(self) -> None:
        logger.info("=" * 50)
        logger.info("Stash v%s starting up", VERSION)
        logger.info("Gateway: %s", GATEWAY_MODE)
        logger.info("Environment: %s", self._config.ENV)
        logger.info("=" * 50)

        logger.info("[1/4] Preparing temp directory...")
        self._wipe_tmp()
        self._restore_gcp_credentials()
        logger.info("  OK")

        logger.info("[2/4] Connecting to mymind (%s)...", GATEWAY_MODE)
        self._gateway = create_gateway(self._config)
        if hasattr(self._gateway, "initialize"):
            await self._gateway.initialize()

        if self._config.ENV == "production" and hasattr(self._gateway, "test_connection"):
            from stash.gateway.mymind import AuthError
            try:
                await self._gateway.test_connection()
                logger.info("  mymind: connected")
            except AuthError as e:
                logger.critical("  mymind: FAILED — %s", e)
                raise SystemExit(1)

        logger.info("[3/4] Loading taxonomy...")
        self._taxonomy = TaxonomyCache(self._gateway)
        await self._taxonomy.initialize()
        logger.info("  %d spaces, %d tags loaded", len(self._taxonomy.spaces), len(self._taxonomy.tags))

        logger.info("[4/4] Services ready")

    def _wipe_tmp(self) -> None:
        tmp_dir = self._config.TMP_DIR
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)

    def _restore_gcp_credentials(self) -> None:
        """Re-write GCP credentials file if using base64 env var (wiped by _wipe_tmp)."""
        from stash.config import _setup_gcp_credentials
        _setup_gcp_credentials()

    async def on_ready(self) -> None:
        logger.info("Bot online as %s (pid=%d)", self.user, os.getpid())

        owner = await self.fetch_user(self._config.OWNER_ID)
        if owner:
            try:
                dm = await owner.create_dm()
                await dm.send(
                    f"**Stash v{VERSION} online**\n"
                    f"Gateway: `{GATEWAY_MODE}`\n"
                    f"Spaces: {len(self._taxonomy.spaces)} | Tags: {len(self._taxonomy.tags)}"
                )
            except discord.Forbidden:
                logger.warning("Cannot DM owner — DMs disabled")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != self._config.OWNER_ID:
            return
        if hasattr(self._gateway, '_auth_failed') and self._gateway._auth_failed:
            await message.reply("Bot needs re-auth. Waiting for cookie refresh and redeploy.")
            return
        if message.author.bot:
            return
        if message.channel.id in self._awaiting_confirmation:
            return
        if message.id in self._processed:
            return

        self._processed.add(message.id)
        try:
            await self._handle_message(message)
        except Exception as e:
            logger.exception("Unhandled error processing message")
            await message.reply(f"Something went wrong: {str(e)[:100]}")

    async def _handle_message(self, message: discord.Message) -> None:
        content_type, primary, user_note = route_message(message)

        packet = await self._extract(message, content_type, primary, user_note)
        if packet is None:
            return

        if packet.extraction_failed:
            if not packet.user_note and not packet.transcript:
                await message.reply(
                    packet.extraction_error or "Extraction failed for unknown reason."
                )
                return

        result = await self._categorize(message, packet)
        if result is None:
            return

        if result.confidence >= CONFIDENCE_THRESHOLD:
            card = await self._save(message, packet, result)
            if card:
                await message.reply(self._format_saved(card))
        else:
            confirmed_result = await self._ask_confirmation(message, result)
            if confirmed_result:
                card = await self._save(message, packet, confirmed_result)
                if card:
                    await message.reply(self._format_saved(card))

    async def _extract(
        self, message: discord.Message, content_type: ContentType, primary: str, user_note: str | None
    ) -> ContentPacket | None:
        tmp_dir = self._config.TMP_DIR

        if content_type == ContentType.IMAGE:
            attachment = next(
                (a for a in message.attachments if a.content_type and a.content_type.startswith("image/")),
                None,
            )
            if not attachment:
                await message.reply("Couldn't find the image attachment.")
                return None
            image_bytes = await attachment.read()
            return await extract_image(image_bytes, attachment.content_type, primary, user_note)

        if content_type == ContentType.VOICE_NOTE:
            attachment = next(
                (a for a in message.attachments if a.content_type and a.content_type.startswith("audio/")),
                None,
            )
            if not attachment:
                await message.reply("Couldn't find the audio attachment.")
                return None
            size_err = check_audio_size(attachment.size)
            if size_err:
                await message.reply(size_err)
                return None
            audio_bytes = await attachment.read()
            return await extract_audio(
                audio_bytes, attachment.content_type, attachment.filename, primary, user_note, tmp_dir
            )

        if content_type in (ContentType.YOUTUBE_URL, ContentType.REEL_URL):
            return await extract_video(primary, content_type, user_note, tmp_dir)

        if content_type == ContentType.UNKNOWN_URL:
            return ContentPacket(
                content_type=ContentType.UNKNOWN_URL,
                raw_input=primary,
                source_url=primary,
                user_note=user_note,
            )

        if content_type == ContentType.TEXT:
            return ContentPacket(
                content_type=ContentType.TEXT,
                raw_input=primary,
                user_note=user_note,
                page_text=primary,
            )

        await message.reply("Not sure what to do with this. Is this a link, a note, or something else?")
        return None

    async def _categorize(self, message: discord.Message, packet: ContentPacket) -> CategoryResult | None:
        try:
            return await categorize(packet, self._taxonomy)
        except Exception as e:
            logger.error("Categorization failed: %s", e)
            await message.reply("AI categorization unavailable right now. Saved to Inbox for later review.")
            result = CategoryResult(
                title=packet.raw_input[:60] or "Untitled",
                category="Inbox",
                tags=["uncategorized"],
                summary="Categorization failed — saved for manual review.",
                confidence=0.0,
            )
            card = await self._save(message, packet, result)
            if card:
                await message.reply(self._format_saved(card))
            return None

    async def _ask_confirmation(self, message: discord.Message, result: CategoryResult) -> CategoryResult | None:
        prompt = (
            f"**Proposed:** {result.category} > {result.title}\n"
            f"Tags: {', '.join(result.tags)}\n"
            f"Confidence: {result.confidence:.0%}\n\n"
            f"Reply `ok` to confirm, or type a different category/tags."
        )
        await message.reply(prompt)

        channel_id = message.channel.id
        self._awaiting_confirmation.add(channel_id)

        def check(m: discord.Message) -> bool:
            return m.author.id == self._config.OWNER_ID and m.channel.id == channel_id

        try:
            reply = await self.wait_for("message", check=check, timeout=CONFIRMATION_TIMEOUT)
        except asyncio.TimeoutError:
            result.tags.append("low_confidence")
            return result
        finally:
            self._awaiting_confirmation.discard(channel_id)

        text = reply.content.strip().lower()
        if text in ("ok", "yes", "y", "confirm"):
            return result

        # User provided a different category
        result.category = reply.content.strip()
        result.is_new_category = True
        return result

    async def _save(self, message: discord.Message, packet: ContentPacket, result: CategoryResult) -> SavedCard | None:
        # Update local taxonomy cache (no API calls — just in-memory)
        if result.is_new_category and not any(
            s["name"].lower() == result.category.lower() for s in self._taxonomy.spaces
        ):
            self._taxonomy.spaces.append({"id": "", "name": result.category})
        for tag in result.tags:
            if tag not in self._taxonomy.tags:
                self._taxonomy.tags.append(tag)

        try:
            if packet.content_type == ContentType.IMAGE and packet.image_bytes:
                return await self._gateway.save_image(
                    packet.image_bytes, packet.image_mime or "image/png",
                    result.title, result.tags, result.category, result.summary,
                )
            elif packet.source_url:
                return await self._gateway.save_url(
                    packet.source_url, result.title, result.tags, result.category, result.summary,
                )
            else:
                text = packet.transcript or packet.page_text or packet.raw_input
                return await self._gateway.save_note(
                    text, result.title, result.tags, result.category,
                )
        except Exception as e:
            from stash.gateway.mymind import AuthError
            if isinstance(e, AuthError) or "auth" in str(e).lower() or "expired" in str(e).lower():
                logger.warning("mymind auth failed: %s", e)
                await message.reply(
                    "Bot needs re-auth. Run `scripts/export_cookies.py` on Windows, "
                    "update Railway env vars, redeploy."
                )
                try:
                    owner = await self.fetch_user(self._config.OWNER_ID)
                    if owner:
                        dm = await owner.create_dm()
                        await dm.send(
                            "**Stash re-auth required**\n"
                            "mymind cookies expired. Run:\n"
                            "```\npython scripts/export_cookies.py\n```\n"
                            "Then update Railway env vars and redeploy."
                        )
                except discord.Forbidden:
                    pass
            else:
                logger.error("mymind save failed: %s", e)
                await message.reply("mymind is having issues. Retrying in 30 seconds...")
                await asyncio.sleep(30)
                try:
                    if packet.source_url:
                        return await self._gateway.save_url(
                            packet.source_url, result.title, result.tags, result.category, result.summary,
                        )
                    else:
                        text = packet.transcript or packet.page_text or packet.raw_input
                        return await self._gateway.save_note(
                            text, result.title, result.tags, result.category,
                        )
                except Exception as retry_err:
                    await message.reply(f"Retry also failed: {str(retry_err)[:100]}")
            return None

    def _format_saved(self, card: SavedCard) -> str:
        tags_str = ", ".join(card.tags) if card.tags else "none"
        lines = [
            f"Saved to **{card.category}**",
            f"Title: {card.title}",
            f"Tags: {tags_str}",
            f"Summary: {card.summary[:120]}",
        ]
        if card.source_url:
            lines.append(f"Source: {card.source_url}")
        return "\n".join(lines)


def main() -> None:
    config = load_config()
    bot = StashBot(config)
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
