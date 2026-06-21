from __future__ import annotations

import asyncio
import logging
import os
import shutil
import traceback

import discord

from stash.agent import AgentResult, PendingTool, execute_pending, handle_text
from stash.alerts import DiscordAlertHandler, send_alert
from stash.categorizer import FALLBACK_MODEL as CAT_FALLBACK
from stash.categorizer import PRIMARY_MODEL as CAT_PRIMARY
from stash.categorizer import categorize
from stash.commands import try_handle as try_command
from stash.config import load_config, StashConfig
from stash.extractor.audio import check_audio_size, extract_audio
from stash.extractor.image import extract_image
from stash.extractor.video import extract_video
from stash.gateway import create_gateway
from stash.gateway.interface import MindGateway
from stash.help import STARTUP_GREETING_TEMPLATE
from stash.models import CategoryResult, ContentPacket, ContentType, SavedCard
from stash.router import route_message
from stash.settings import MODEL_LABELS, Settings, fallback_for
from stash.taxonomy import TaxonomyCache
from stash.tools import Tool, build_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stash")

CONFIDENCE_THRESHOLD = 0.75
CONFIRMATION_TIMEOUT = 300  # 5 minutes
GATEWAY_MODE = "cookie-local"  # "jwt-railway" on main branch, "cookie-local" on stable/cookie-local
VERSION = "2.1.1"
SETTINGS_FILE = "stash_settings.json"


class StashBot(discord.Client):
    def __init__(self, config: StashConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self._config = config
        self._gateway: MindGateway | None = None
        self._taxonomy: TaxonomyCache | None = None
        self._tool_registry: dict[str, Tool] = {}
        self._settings: Settings = Settings.load(
            os.path.join(self._settings_dir(config), SETTINGS_FILE)
        )
        self._awaiting_confirmation: set[int] = set()  # channel IDs waiting for user reply
        self._processed: set[int] = set()  # message IDs already handled (kept permanently)

    @staticmethod
    def _settings_dir(config: StashConfig) -> str:
        # Live alongside the sandbox file when in sandbox mode; otherwise the
        # tmp dir (Railway-friendly: persists across the process's lifetime).
        if config.ENV == "sandbox":
            return os.path.dirname(os.path.abspath(config.SANDBOX_FILE)) or "."
        return config.TMP_DIR

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

        self._tool_registry = build_registry(self._gateway)
        logger.info("  %d agent tools registered", len(self._tool_registry))

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

        alert_handler = DiscordAlertHandler(self, self._config.OWNER_ID)
        logging.getLogger().addHandler(alert_handler)

        owner = await self.fetch_user(self._config.OWNER_ID)
        if owner:
            try:
                dm = await owner.create_dm()
                await dm.send(self._startup_message())
            except discord.Forbidden:
                logger.warning("Cannot DM owner — DMs disabled")

    def _startup_message(self) -> str:
        agent_model = self._settings.agent_model
        return STARTUP_GREETING_TEMPLATE.format(
            version=VERSION,
            gateway_mode=GATEWAY_MODE,
            env=self._config.ENV,
            agent_label=MODEL_LABELS.get(agent_model, agent_model),
            fallback_label=MODEL_LABELS.get(
                fallback_for(agent_model), fallback_for(agent_model)
            ),
            categorizer_primary=CAT_PRIMARY,
            categorizer_fallback=CAT_FALLBACK,
            space_count=len(self._taxonomy.spaces),
            tag_count=len(self._taxonomy.tags),
            tool_count=len(self._tool_registry),
        )

    async def on_error(self, event: str, *args, **kwargs) -> None:
        error_text = traceback.format_exc()[:500]
        logger.error("Unhandled error in %s: %s", event, error_text)
        try:
            user = await self.fetch_user(self._config.OWNER_ID)
            dm = await user.create_dm()
            await dm.send(
                f"**Unhandled error in `{event}`**\n"
                f"```{error_text}```"
            )
        except Exception:
            pass

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

        # Text-only messages with no URL/no attachment go to the interactive
        # agent (list spaces, search, etc.). URLs and attachments stay on
        # the capture pipeline.
        if content_type == ContentType.TEXT and not message.attachments:
            await self._handle_agent(message, primary, user_note)
            return

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

    async def _handle_agent(
        self, message: discord.Message, text: str, annotation: str | None
    ) -> None:
        """Route a plain-text message through prefix commands or the agent."""
        prompt = text or ""
        if annotation:
            prompt = f"{prompt}\n[[{annotation}]]" if prompt else annotation
        if not prompt.strip():
            return

        # Prefix commands (/help, /model, /stats) bypass Gemini entirely.
        cmd_result = await try_command(
            prompt, settings=self._settings, registry=self._tool_registry
        )
        if cmd_result.handled:
            await message.reply(cmd_result.text[:1900])
            return

        result = await handle_text(
            prompt,
            registry=self._tool_registry,
            taxonomy=self._taxonomy,
            model=self._settings.agent_model,
        )

        if result.pending:
            await self._confirm_and_execute(message, result.pending)
            return

        reply_text = result.text or "Hmm, no answer to give."
        await message.reply(reply_text[:1900])

    async def _confirm_and_execute(
        self, message: discord.Message, pending: PendingTool
    ) -> None:
        """Ask the user to confirm a destructive tool; execute on 'ok'."""
        await message.reply(
            f"{pending.preview}\nReply `ok` to confirm, anything else cancels."
        )

        channel_id = message.channel.id
        self._awaiting_confirmation.add(channel_id)

        def check(m: discord.Message) -> bool:
            return m.author.id == self._config.OWNER_ID and m.channel.id == channel_id

        try:
            reply = await self.wait_for("message", check=check, timeout=CONFIRMATION_TIMEOUT)
        except asyncio.TimeoutError:
            await message.reply("Timed out — not running.")
            return
        finally:
            self._awaiting_confirmation.discard(channel_id)

        text = reply.content.strip().lower()
        if text not in ("ok", "yes", "y", "confirm"):
            await message.reply("Cancelled.")
            return

        executed = await execute_pending(pending, self._tool_registry)
        await message.reply((executed.text or "Done.")[:1900])

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
                card = await self._gateway.save_image(
                    packet.image_bytes, packet.image_mime or "image/png",
                    result.title, result.tags, result.category, result.summary,
                )
            elif packet.source_url:
                card = await self._gateway.save_url(
                    packet.source_url, result.title, result.tags, result.category, result.summary,
                )
            else:
                text = packet.transcript or packet.page_text or packet.raw_input
                card = await self._gateway.save_note(
                    text, result.title, result.tags, result.category,
                )

            if card and result.verbatim_note and hasattr(self._gateway, "post_verbatim_note"):
                try:
                    await self._gateway.post_verbatim_note(card.mymind_id, result.verbatim_note)
                    card._note_saved = True
                except Exception as note_err:
                    logger.warning("Note failed to attach: %s", note_err)
                    card._note_saved = False

            return card
        except Exception as e:
            from stash.gateway.mymind import AuthError
            if isinstance(e, AuthError) or "auth" in str(e).lower() or "expired" in str(e).lower():
                await message.reply(
                    "Bot needs re-auth. Run `scripts/export_cookies.py` on Windows, "
                    "update Railway env vars, redeploy."
                )
                await send_alert(
                    self, self._config.OWNER_ID,
                    "mymind Auth Expired", e,
                    "Re-run scripts/export_cookies.py and update Railway vars",
                )
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
        note_saved = getattr(card, "_note_saved", None)
        space_assigned = getattr(card, "_space_assigned", None)

        if not card.category:
            header = "Saved (no space)"
        elif space_assigned is False:
            header = f"Saved (couldn't assign to **{card.category}** — left in Everything)"
        else:
            header = f"Saved to **{card.category}**"

        lines = [
            header,
            f"Title: {card.title}",
            f"ID: `{card.mymind_id}`" if card.mymind_id else "ID: (none)",
            f"Tags: {tags_str}",
            f"Summary: {card.summary[:120]}",
        ]
        if card.source_url:
            lines.append(f"Source: {card.source_url}")
        if note_saved is True:
            lines.append("Your note saved to Mind Notes")
        elif note_saved is False:
            lines.append("Saved (note failed to attach)")
        return "\n".join(lines)


def main() -> None:
    config = load_config()
    bot = StashBot(config)
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
