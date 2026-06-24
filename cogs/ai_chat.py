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
cogs/ai_chat.py – Discord-Cog fuer den KI-Chat-Bot.

Reagiert auf:
  - @-Erwaehnung des Bots im konfigurierten AI_CHAT_CHANNEL_IDS

Konversations-Fortsetzung:
  Wenn ein User auf eine Bot-Antwort antwortet (Discord-Reply), wird die
  gespeicherte Konversations-Historie geladen und weitergefuehrt.
  Die KI "erinnert sich" an den Gespraechskontext bis zur TTL-Grenze.

Slash-Commands:
  /ai_status  – Zeigt globales + eigenes Tagesbudget (ephemeral)
  /ai_reset   – (Admin) Budget eines Users oder global zuruecksetzen
"""

import logging

import discord
from discord.ext import commands, tasks

import config as cfg
from utils.ai_chat import (
    chat,
    chunk_discord,
    cleanup_expired_conversations,
    get_budget_status,
    init_ai_chat_tables,
    load_conversation,
    save_conversation,
)

logger = logging.getLogger(__name__)


# ── Hilfsfunktion fuer Budget-Fortschrittsbalken ─────────────────────────────

def _bar(pct: float, width: int = 10) -> str:
    """Erzeugt einen einfachen Textfortschrittsbalken."""
    filled = round(min(100.0, max(0.0, pct)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ── Cog ──────────────────────────────────────────────────────────────────────

class AiChatCog(commands.Cog):
    """
    KI-Chat-Bot mit Budget-Kontrolle und
    Konversations-Unterstuetzung via Discord-Replies.
    """

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        # Tabellen sicher anlegen (idempotent)
        init_ai_chat_tables()
        # Cleanup-Loop starten
        self.cleanup_loop.start()
        logger.info("✅ AiChatCog geladen")

    def cog_unload(self) -> None:
        self.cleanup_loop.cancel()

    # ── Hintergrundtask: abgelaufene Konversationen loeschen ────────────────

    @tasks.loop(hours=6)
    async def cleanup_loop(self) -> None:
        deleted = cleanup_expired_conversations()
        if deleted:
            logger.info(f"[AI-Chat] Cleanup: {deleted} abgelaufene Konversationen geloescht")

    @cleanup_loop.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ── Hilfsmethoden ────────────────────────────────────────────────────────

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
        Gibt die Message-ID der vorherigen Bot-AI-Antwort zurueck,
        auf die geantwortet wird – aber NUR wenn dafuer gespeicherte
        AI-Chat-Historie vorhanden ist.
        Verhindert, dass normale Bot-Nachrichten (z. B. Reviews) als
        Konversationskopf behandelt werden.
        """
        if not message.reference or not message.reference.message_id:
            return None
        ref_id = message.reference.message_id
        # Nur wenn wir tatsaechlich AI-Chat-Historie fuer diese ID haben
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

        # Getippte Nachricht vorab auf Laenge pruefen (vor Dateiinhalt-Anhang)
        if len(user_text) > cfg.AI_CHAT_MAX_INPUT_CHARS:
            await message.reply(
                f"❌ Deine Nachricht ist zu lang "
                f"({len(user_text):,}/{cfg.AI_CHAT_MAX_INPUT_CHARS:,} Zeichen). "
                f"Bitte kueze sie."
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
                await message.reply(
                    f"❌ Videos werden nicht unterstuetzt – ich kann leider keine Videodateien analysieren."
                )
                return

            # Text-Dateien
            if ext in text_exts:
                if attachment.size > 10_000:
                    await message.reply(
                        f"❌ Die Datei **{attachment.filename}** ist zu groß "
                        f"({attachment.size / 1024:.1f} KB). Maximum: 10 KB."
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
                    logger.warning(f"[AI-Chat] Textanhang Lesefehler: {e}")

            # Unbekannte Dateitypen ablehnen
            elif ext not in image_exts:
                await message.reply(
                    f"❌ Der Dateityp **{ext or 'unbekannt'}** wird nicht unterstuetzt. "
                    f"Erlaubt: Bilder (jpg, png, gif, webp) und Textdateien (txt, md, csv, log)."
                )
                return

            # Bilder
            elif ext in image_exts:
                if attachment.size > 1_000_000:  # max 1 MB
                    await message.reply(
                        f"❌ Das Bild **{attachment.filename}** ist zu groß "
                        f"({attachment.size / 1024:.1f} KB). Maximum: 1 MB."
                    )
                    return
                try:
                    img_bytes = await attachment.read()
                    images.append((img_bytes, image_mime[ext]))
                except Exception as e:
                    logger.warning(f"[AI-Chat] Bildanhang Lesefehler: {e}")

        if not user_text:
            return

        # Vorherige Bot-Antwort-ID (fuer Konversations-Fortsetzung)
        prev_id = self._get_prev_bot_msg_id(message)

        # Typing-Indikator waehrend der API-Call laeuft
        async with message.channel.typing():
            result = await chat(
                user_id=message.author.id,
                user_message=user_text,
                prev_bot_message_id=prev_id,
                channel_id=message.channel.id,
                images=images or None,
            )

        # Disclaimer an Antwort anhaengen
        DISCLAIMER = (
            "\n-# 🤖 KI-Antwort – nur zur Orientierung, kein Ersatz fuer Fachrat. "
            "Angaben immer selbst pruefen! · "
            "Quellcode: <https://github.com/JonasVerzockt/Discord-Bot>"
        )
        answer_with_disclaimer = result["answer"] + DISCLAIMER

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
                logger.error(f"[AI-Chat] Sendefehler: {e}")
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
        description="Zeigt deinen KI-Chat Budget-Status fuer heute",
    )
    async def ai_status(self, ctx: discord.ApplicationContext) -> None:
        """Zeigt globales und persoenliches Tagesbudget (nur fuer dich sichtbar)."""
        s = get_budget_status(ctx.author.id)

        if s["global_pct"] >= 90 or s["user_pct"] >= 90:
            color = discord.Color.red()
        elif s["global_pct"] >= 70 or s["user_pct"] >= 70:
            color = discord.Color.orange()
        else:
            color = discord.Color.green()

        embed = discord.Embed(
            title="🤖 KI-Chat Budget-Status",
            color=color,
            description="Budgets werden taeglich um **00:00 UTC** (01:00 MEZ / 02:00 MESZ) zurueckgesetzt.",
        )
        embed.add_field(
            name="🌍 Globales Tagesbudget",
            value=(
                f"`{_bar(s['global_pct'])}` {s['global_pct']:.1f} %\n"
                f"**${s['global_used']:.4f}** / ${s['global_limit']:.2f}"
            ),
            inline=False,
        )
        embed.add_field(
            name="👤 Dein Tagesbudget",
            value=(
                f"`{_bar(s['user_pct'])}` {s['user_pct']:.1f} %\n"
                f"**${s['user_used']:.4f}** / ${s['user_limit']:.2f}"
            ),
            inline=False,
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(
        name="ai_reset",
        description="(Admin) Budget eines Users oder global zuruecksetzen",
    )
    @commands.has_permissions(manage_messages=True)
    async def ai_reset(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(
            discord.Member,
            description="User dessen Budget zurueckgesetzt wird (leer = global)",
            required=False,
            default=None,
        ),
    ) -> None:
        """Setzt das Tagesbudget fuer einen User oder global auf 0 zurueck."""
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

        label = f"<@{user.id}>" if user else "global"
        await ctx.respond(
            f"✅ Budget fuer **{label}** wurde fuer heute zurueckgesetzt.",
            ephemeral=True,
        )

    @discord.slash_command(
        name="ai_prompt",
        description="(Admin) Aktuellen System-Prompt des KI-Chats anzeigen",
    )
    @commands.has_permissions(manage_messages=True)
    async def ai_prompt(self, ctx: discord.ApplicationContext) -> None:
        """Gibt den aktiven System-Prompt aus ai_chat_system_prompt.txt aus (ephemeral)."""
        prompt = cfg.AI_CHAT_SYSTEM_PROMPT
        # Discord-Limit: 2000 Zeichen pro Nachricht – in Codeblock einbetten
        header = "📋 **Aktiver System-Prompt** (`ai_chat_system_prompt.txt`):\n"
        content = f"```\n{prompt}\n```"
        full = header + content
        if len(full) <= 2000:
            await ctx.respond(full, ephemeral=True)
        else:
            # Zu lang: als Dateianhang senden
            import io
            await ctx.respond(
                "📋 **Aktiver System-Prompt** (zu lang fuer Chat, als Datei):",
                file=discord.File(io.BytesIO(prompt.encode()), filename="system_prompt.txt"),
                ephemeral=True,
            )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AiChatCog(bot))
