# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jonas Beier
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
cogs/ai_chat.py – Discord-Cog für den KI-Chat-Bot.

Reagiert auf:
  - @-Erwaehnung des Bots im konfigurierten AI_CHAT_CHANNEL_IDS

Konversations-Fortsetzung:
  Wenn ein User auf eine Bot-Antwort antwortet (Discord-Reply), wird die
  gespeicherte Konversations-Historie geladen und weitergefuehrt.
  Die KI "erinnert sich" an den Gespraechskontext bis zur TTL-Grenze.

Slash-Commands:
  /ai_status  – Zeigt globales + eigenes Tagesbudget (ephemeral)
  /ai_reset   – (Admin) Budget eines Users oder global zurücksetzen
"""

import logging

import discord
from discord.ext import commands, tasks

import config as cfg
from utils.localization import l10n, get_user_lang
from utils.ai_chat import (
    chat,
    chunk_discord,
    cleanup_expired_conversations,
    get_budget_status,
    init_ai_chat_tables,
    load_conversation,
    save_conversation,
)
from utils.sheets_shop_data import refresh as refresh_shop_data

logger = logging.getLogger(__name__)


# ── Hilfsfunktion für Budget-Fortschrittsbalken ──────────────────────────────

def _bar(pct: float, width: int = 10) -> str:
    """Erzeugt einen einfachen Textfortschrittsbalken."""
    filled = round(min(100.0, max(0.0, pct)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ── Cog ───────────────────────────────────────────────────────────────────────

class AiChatCog(commands.Cog):
    """
    KI-Chat-Bot mit Budget-Kontrolle und
    Konversations-Unterstuetzung via Discord-Replies.
    """

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        # Tabellen sicher anlegen (idempotent)
        init_ai_chat_tables()
        # Hintergrundtasks starten
        self.cleanup_loop.start()
        self.shop_data_loop.start()
        logger.info("✅ AiChatCog geladen")

    def cog_unload(self) -> None:
        self.cleanup_loop.cancel()
        self.shop_data_loop.cancel()

    # ── Hintergrundtask: abgelaufene Konversationen löschen ──────────────────

    @tasks.loop(hours=6)
    async def cleanup_loop(self) -> None:
        deleted = cleanup_expired_conversations()
        if deleted:
            logger.info(f"🤖 [AI-Chat] Cleanup: {deleted} abgelaufene Konversationen gelöscht")

    @cleanup_loop.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ── Hintergrundtask: Shop-Daten aus Google Sheets laden ───────────────────

    @tasks.loop(hours=6)
    async def shop_data_loop(self) -> None:
        ok = refresh_shop_data()
        if ok:
            logger.info("🤖 [AI-Chat] Shop-Daten aus Google Sheets aktualisiert")
        else:
            logger.debug("🔍 [AI-Chat] Shop-Daten nicht aktualisiert (nicht konfiguriert oder Fehler)")

    @shop_data_loop.before_loop
    async def _before_shop_data(self) -> None:
        await self.bot.wait_until_ready()

    # ── Hilfsmethoden ─────────────────────────────────────────────────────────

    def _is_ai_channel(self, channel_id: int) -> bool:
        """True wenn der Kanal ein konfigurierter AI-Chat-Kanal ist."""
        return bool(cfg.AI_CHAT_CHANNEL_IDS) and channel_id in cfg.AI_CHAT_CHANNEL_IDS

    def _is_mentioned(self, message: discord.Message) -> bool:
        """True wenn der Bot in der Nachricht erwaehnt wird."""
        return self.bot.user in message.mentions

    def _strip_mention(self, message: discord.Message) -> str:
        """Entfernt die @Bot-Erwaehnung aus dem Nachrichtentext."""
        text = message.content
        for fmt in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            text = text.replace(fmt, "")
        return text.strip()

    def _get_prev_bot_msg_id(self, message: discord.Message) -> int | None:
        """
        Gibt die Message-ID der vorherigen Bot-AI-Antwort zurück,
        auf die geantwortet wird – aber NUR wenn dafür gespeicherte
        AI-Chat-Historie vorhanden ist.
        Verhindert, dass normale Bot-Nachrichten (z. B. Reviews) als
        Konversationskopf behandelt werden.
        """
        if not message.reference or not message.reference.message_id:
            return None
        ref_id = message.reference.message_id
        # Nur wenn wir tatsächlich AI-Chat-Historie für diese ID haben
        if load_conversation(ref_id) is not None:
            return ref_id
        return None

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Eigene Nachrichten und andere Bots immer ignorieren
        if message.author.bot:
            return

        # Nur im konfigurierten AI-Kanal reagieren
        if not self._is_ai_channel(message.channel.id):
            return

        # Slash-Commands ignorieren
        if message.content.startswith("/"):
            return

        # Nur auf @-Erwaehnung reagieren (auch im AI-Kanal)
        if not self._is_mentioned(message):
            return

        # @-Erwaehnung aus dem Nachrichtentext entfernen
        user_text = self._strip_mention(message)

        # Sprache des Users bestimmen (einmalig für alle Fehlermeldungen in on_message)
        lang = await get_user_lang(
            self.bot, message.author.id,
            message.guild.id if message.guild else None,
        )

        # Getippte Nachricht vorab auf Laenge prüfen (vor Dateiinhalt-Anhang)
        if len(user_text) > cfg.AI_CHAT_MAX_INPUT_CHARS:
            await message.reply(
                l10n.get(
                    "ai_msg_too_long", lang,
                    length=f"{len(user_text):,}",
                    max=f"{cfg.AI_CHAT_MAX_INPUT_CHARS:,}",
                )
            )
            return

        # Anhaenge verarbeiten
        text_exts  = {".txt", ".md", ".csv", ".log"}
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        image_mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        }
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"}
        images: list[tuple[bytes, str]] = []

        for attachment in message.attachments:
            ext = ("." + attachment.filename.rsplit(".", 1)[-1].lower()) if "." in attachment.filename else ""

            # Videos ablehnen
            if ext in video_exts:
                await message.reply(l10n.get("ai_video_unsupported", lang))
                return

            # Text-Dateien
            if ext in text_exts:
                if attachment.size > 10_000:
                    await message.reply(
                        l10n.get(
                            "ai_file_too_large", lang,
                            filename=attachment.filename,
                            size=f"{attachment.size / 1024:.1f}",
                        )
                    )
                    return
                try:
                    raw = await attachment.read()
                    file_text = raw.decode("utf-8", errors="replace").strip()
                    if file_text:
                        user_text = (
                            f"{user_text}\n\n[Dateiinhalt: {attachment.filename}]\n{file_text}"
                            if user_text else
                            f"[Dateiinhalt: {attachment.filename}]\n{file_text}"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ [AI-Chat] Textanhang Lesefehler: {e}")

            # Unbekannte Dateitypen ablehnen
            elif ext not in image_exts:
                await message.reply(
                    l10n.get(
                        "ai_filetype_unsupported", lang,
                        ext=ext or "unbekannt",
                    )
                )
                return

            # Bilder
            elif ext in image_exts:
                if attachment.size > 1_000_000:  # max 1 MB
                    await message.reply(
                        l10n.get(
                            "ai_image_too_large", lang,
                            filename=attachment.filename,
                            size=f"{attachment.size / 1024:.1f}",
                        )
                    )
                    return
                try:
                    img_bytes = await attachment.read()
                    images.append((img_bytes, image_mime[ext]))
                except Exception as e:
                    logger.warning(f"⚠️ [AI-Chat] Bildanhang Lesefehler: {e}")

        if not user_text:
            return

        # Vorherige Bot-Antwort-ID (für Konversations-Fortsetzung)
        prev_id = self._get_prev_bot_msg_id(message)

        # Typing-Indikator waehrend der API-Call laeuft
        async with message.channel.typing():
            result = await chat(
                user_id=message.author.id,
                user_message=user_text,
                prev_bot_message_id=prev_id,
                channel_id=message.channel.id,
                images=images or None,
                user_lang=lang,
            )

        # Disclaimer (inkl. tatsächliche Kosten) an Antwort anhaengen
        cost_str  = f"${result['cost']:.5f}" if result["cost"] > 0 else ""
        cost_part = f" · 💰 {cost_str}" if cost_str else ""
        disclaimer = l10n.get("ai_disclaimer", lang, cost_part=cost_part)
        answer_with_disclaimer = result["answer"] + disclaimer

        # Antwort in Discord-Chunks senden (max. 2000 Zeichen pro Nachricht)
        chunks   = chunk_discord(answer_with_disclaimer)
        sent_msg = None

        for i, chunk in enumerate(chunks):
            try:
                if i == 0:
                    sent_msg = await message.reply(chunk)
                else:
                    sent_msg = await message.channel.send(chunk)
            except discord.HTTPException as e:
                logger.error(f"❌ [AI-Chat] Sendefehler: {e}")
                break

        # Konversations-Historie speichern (nur bei Erfolg und bekannter Msg-ID)
        if result["ok"] and sent_msg and result["history"]:
            save_conversation(
                bot_message_id=sent_msg.id,
                user_id=message.author.id,
                channel_id=message.channel.id,
                history=result["history"],
            )

    # ── Slash Commands ────────────────────────────────────────────────────────

    @discord.slash_command(
        name="ai_status",
        description="Zeigt deinen KI-Chat Budget-Status für heute",
    )
    async def ai_status(self, ctx: discord.ApplicationContext) -> None:
        """Zeigt globales und persoenliches Tagesbudget (nur für dich sichtbar)."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        s = get_budget_status(ctx.author.id)

        if s["global_pct"] >= 90 or s["user_pct"] >= 90:
            color = discord.Color.red()
        elif s["global_pct"] >= 70 or s["user_pct"] >= 70:
            color = discord.Color.orange()
        else:
            color = discord.Color.green()

        embed = discord.Embed(
            title=l10n.get("ai_status_title", lang),
            color=color,
            description=l10n.get("ai_status_description", lang),
        )
        embed.add_field(
            name=l10n.get("ai_status_global_field", lang),
            value=(
                f"`{_bar(s['global_pct'])}` {s['global_pct']:.1f} %\n"
                f"**${s['global_used']:.4f}** / ${s['global_limit']:.2f}"
            ),
            inline=False,
        )
        embed.add_field(
            name=l10n.get("ai_status_user_field", lang),
            value=(
                f"`{_bar(s['user_pct'])}` {s['user_pct']:.1f} %\n"
                f"**${s['user_used']:.4f}** / ${s['user_limit']:.2f}"
            ),
            inline=False,
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(
        name="ai_reset",
        description="(Admin) Budget eines Users oder global zurücksetzen",
    )
    @commands.has_permissions(manage_messages=True)
    async def ai_reset(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(
            discord.Member,
            description="User dessen Budget zurückgesetzt wird (leer = global)",
            required=False,
            default=None,
        ),
    ) -> None:
        """Setzt das Tagesbudget für einen User oder global auf 0 zurück."""
        import sqlite3 as _sq
        today = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%d")

        target_uid = user.id if user else 0
        with _sq.connect(str(cfg.DB_FILE)) as con:
            con.execute(
                "UPDATE ai_chat_budget SET cost_usd=0.0 WHERE date=? AND user_id=?",
                (today, target_uid),
            )

        lang  = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        label = f"<@{user.id}>" if user else "global"
        await ctx.respond(
            l10n.get("ai_reset_success", lang, label=label),
            ephemeral=True,
        )

    @discord.slash_command(
        name="ai_prompt",
        description="(Admin) Aktuellen System-Prompt des KI-Chats anzeigen",
    )
    @commands.has_permissions(manage_messages=True)
    async def ai_prompt(self, ctx: discord.ApplicationContext) -> None:
        """Gibt den aktiven System-Prompt in der Sprache des Users aus (ephemeral)."""
        lang   = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        prompt = (
            cfg.AI_CHAT_SYSTEM_PROMPTS.get(lang)
            or cfg.AI_CHAT_SYSTEM_PROMPTS.get("en")
            or cfg.AI_CHAT_SYSTEM_PROMPT
        )
        # Discord-Limit: 2000 Zeichen pro Nachricht – in Codeblock einbetten
        header  = l10n.get("ai_prompt_header", lang) + "\n"
        content = f"```\n{prompt}\n```"
        full    = header + content
        if len(full) <= 2000:
            await ctx.respond(full, ephemeral=True)
        else:
            # Zu lang: als Dateianhang senden
            import io
            await ctx.respond(
                l10n.get("ai_prompt_file_header", lang),
                file=discord.File(
                    io.BytesIO(prompt.encode()),
                    filename=f"system_prompt_{lang}.txt",
                ),
                ephemeral=True,
            )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AiChatCog(bot))
