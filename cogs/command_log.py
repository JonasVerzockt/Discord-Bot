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
cogs/command_log.py – Zentrales Befehls-Nutzungsprotokoll (Moderation).

Ein einziger Cog mit globalen Listenern (keine Änderung pro Befehl nötig):
  • on_application_command_completion – erfolgreiche Slash-Befehle
  • on_application_command_error      – fehlgeschlagene/abgelehnte Slash-Befehle
  • on_message                        – bekannte Text-Trigger (Allowlist)

Jeder Eintrag geht in die DB-Tabelle command_log und – falls MOD_LOG_CHANNEL_ID
gesetzt ist – zusätzlich gebündelt (alle paar Sekunden als Sammel-Embed) in den
Mod-Kanal. Sensible Parameterwerte werden ausgeblendet. Kanal-Nachrichten bleiben
dauerhaft; nur die DB-Zeilen werden nach COMMAND_LOG_RETENTION_DAYS bereinigt.
"""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from config import MOD_LOG_CHANNEL_ID, COMMAND_LOG_RETENTION_DAYS
from utils.db import execute_db
from utils.localization import l10n, get_user_lang

logger = logging.getLogger(__name__)

# Parameternamen, deren WERT nicht geloggt wird (nur der Name bleibt sichtbar).
_SENSITIVE_PARAMS = {"user_id", "user", "text", "code"}
_HIDDEN = "‹ausgeblendet›"

# Bekannte Text-Trigger (lowercase, exakt) – nur diese werden protokolliert.
_TEXT_TRIGGERS = {"!help", "!hilfe", "rangliste"}

_FLUSH_SECONDS = 10          # Sammel-Post-Intervall in den Mod-Kanal
_MAX_BUFFER    = 1000        # Sicherheitslimit gegen unbegrenztes Wachstum
_EMBED_LIMIT   = 4000        # < 4096 (Discord-Embed-Beschreibung)


def _chunk_lines(lines: list[str], limit: int = _EMBED_LIMIT) -> list[str]:
    chunks, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            chunks.append(cur.rstrip("\n")); cur = ""
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


class CommandLogCog(commands.Cog, name="CommandLog"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._buffer: list[dict] = []
        self._warned_missing_channel = False
        self.flush_log.start()
        self.cleanup_log.start()

    def cog_unload(self):
        self.flush_log.cancel()
        self.cleanup_log.cancel()

    # ── Parameter aufbereiten ────────────────────────────────────────────────
    @staticmethod
    def _format_params(ctx: discord.ApplicationContext) -> str:
        opts = getattr(ctx, "selected_options", None) or []
        parts = []
        for o in opts:
            try:
                name = o.get("name")
                value = o.get("value")
            except AttributeError:
                continue
            if name is None:
                continue
            shown = _HIDDEN if name in _SENSITIVE_PARAMS else value
            parts.append(f"{name}={shown}")
        return " ".join(parts)

    # ── Persistenz + Puffer ──────────────────────────────────────────────────
    async def _record(self, *, user, command, params, channel, guild, status):
        uid   = str(getattr(user, "id", "")) or None
        uname = getattr(user, "name", None) or "?"
        cid   = str(getattr(channel, "id", "")) or None
        gid   = str(getattr(guild, "id", "")) if guild else None
        try:
            await execute_db(
                self.bot,
                """INSERT INTO command_log
                   (user_id, user_name, command, params, channel_id, server_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uid, uname, command, params or "", cid, gid, status),
                commit=True,
            )
        except Exception as e:
            logger.error("❌ command_log DB-Insert fehlgeschlagen: %s", e)

        if MOD_LOG_CHANNEL_ID:
            self._buffer.append({
                "ts": datetime.now(timezone.utc).strftime("%d.%m %H:%M:%S"),
                "uid": uid, "uname": uname, "command": command,
                "params": params or "", "cid": cid, "status": status,
            })
            if len(self._buffer) > _MAX_BUFFER:
                self._buffer = self._buffer[-_MAX_BUFFER:]

    # ── Listener ─────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_application_command_completion(self, ctx: discord.ApplicationContext):
        try:
            await self._record(
                user=ctx.author, command=ctx.command.qualified_name,
                params=self._format_params(ctx),
                channel=ctx.channel, guild=ctx.guild, status="ok",
            )
        except Exception as e:
            logger.debug("command_log completion error: %s", e)

    @commands.Cog.listener()
    async def on_application_command_error(self, ctx: discord.ApplicationContext, error):
        try:
            cmd = ctx.command.qualified_name if ctx.command else "?"
            await self._record(
                user=ctx.author, command=cmd,
                params=self._format_params(ctx),
                channel=ctx.channel, guild=ctx.guild,
                status=f"error:{type(error).__name__}",
            )
        except Exception as e:
            logger.debug("command_log error-hook error: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        content = (message.content or "").strip().lower()
        if content not in _TEXT_TRIGGERS:
            return
        await self._record(
            user=message.author, command=content, params="",
            channel=message.channel, guild=message.guild, status="text",
        )

    # ── Gebündelter Kanal-Post ───────────────────────────────────────────────
    @tasks.loop(seconds=_FLUSH_SECONDS)
    async def flush_log(self):
        if not self._buffer or not MOD_LOG_CHANNEL_ID:
            return
        channel = self.bot.get_channel(MOD_LOG_CHANNEL_ID)
        if channel is None:
            if not self._warned_missing_channel:
                logger.warning("⚠️ MOD_LOG_CHANNEL_ID gesetzt, aber Kanal nicht gefunden.")
                self._warned_missing_channel = True
            self._buffer.clear()
            return

        entries = self._buffer[:]
        self._buffer.clear()
        lang = await get_user_lang(self.bot, 0, channel.guild.id if channel.guild else None)

        lines = []
        for e in entries:
            user = f"<@{e['uid']}>" if e["uid"] else e["uname"]
            chan = f"<#{e['cid']}>" if e["cid"] else "?"
            mark = "❌ " if str(e["status"]).startswith("error") else ("💬 " if e["status"] == "text" else "")
            params = f" {e['params']}" if e["params"] else ""
            lines.append(f"{mark}`{e['ts']}` {user} `{e['command']}`{params} · {chan}")

        try:
            for i, chunk in enumerate(_chunk_lines(lines)):
                embed = discord.Embed(description=chunk, color=discord.Color.dark_grey())
                if i == 0:
                    embed.title = l10n.get("cmdlog_title", lang)
                await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.error("❌ command_log Kanal-Post fehlgeschlagen: %s", e)

    @flush_log.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    # ── DB-Retention ─────────────────────────────────────────────────────────
    @tasks.loop(hours=24)
    async def cleanup_log(self):
        try:
            days = int(COMMAND_LOG_RETENTION_DAYS)
            rc = await execute_db(
                self.bot,
                "DELETE FROM command_log WHERE created_at < datetime('now', ?)",
                (f"-{days} days",), commit=True,
            )
            if rc:
                logger.info("🧹 command_log: %s alte Einträge gelöscht (> %s Tage)", rc, days)
        except Exception as e:
            logger.error("❌ command_log Cleanup fehlgeschlagen: %s", e)

    @cleanup_log.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(CommandLogCog(bot))
