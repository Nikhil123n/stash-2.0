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
        self._wipe_tmp()

        self._gateway = create_gateway(self._config)
        if hasattr(self._gateway, "initialize"):
            await self._gateway.initialize()

        if self._config.ENV == "production" and hasattr(self._gateway, "test_connection"):
            from stash.gateway.mymind import AuthError
            try:
                await self._gateway.test_connection()
                logger.info("mymind connection verified")
            except AuthError as e:
                logger.critical("mymind auth failed at startup: %s", e)
                raise SystemExit(1)

        self._taxonomy = TaxonomyCache(self._gateway)
        await self._taxonomy.initialize()
        logger.info("Taxonomy loaded: %d spaces, %d tags", len(self._taxonomy.spaces), len(self._taxonomy.tags))

    def _wipe_tmp(self) -> None:
        tmp_dir = self._config.TMP_DIR
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)

    async def on_ready(self) -> None:
        logger.info("Stash bot online as %s (env=%s, pid=%d)", self.user, self._config.ENV, os.getpid())

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != self._config.OWNER_ID:
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
        try:
            if result.is_new_category:
                await self._taxonomy.on_new_space_created(result.category)
            for tag in result.tags:
                await self._taxonomy.on_new_tag_created(tag)
        except NotImplementedError:
            pass
        except Exception as e:
            logger.warning("Taxonomy update failed: %s", e)

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
            error_str = str(e).lower()
            if "401" in error_str or "auth" in error_str or "credential" in error_str:
                await message.reply("mymind credentials expired. Please re-run auth setup.")
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
