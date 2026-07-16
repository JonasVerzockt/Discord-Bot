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
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from config import MOD_LOG_CHANNEL_ID, COMMAND_LOG_RETENTION_DAYS
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.timez import now_berlin, berlin_from_utc_naive
from cogs.server_settings import admin_or_manage_messages

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

    @discord.slash_command(
        name="command_log",
        description="(Admin) Show a user's command usage log",
        description_localizations={"de": "(Admin) Befehls-Nutzungsprotokoll eines Users anzeigen"},
    )
    @admin_or_manage_messages()
    async def command_log_cmd(
        self,
        ctx: discord.ApplicationContext,
        user_id: discord.Option(str, "Discord user ID", description_localizations={"de": "Discord-User-ID", "en-US": "Discord user ID"}, required=True),  # type: ignore[valid-type]
        period: discord.Option(str, "Time window, e.g. 1m, 1h, 1d, 1w (empty = all)", description_localizations={"de": "Zeitraum, z.B. 1m, 1h, 1d, 1w (leer = alle)", "en-US": "Time window, e.g. 1m, 1h, 1d, 1w (empty = all)"}, required=False, default=None),  # type: ignore[valid-type]
    ):
        await _cmdlog_query_impl(self, ctx, user_id, period)

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
                "ts": now_berlin("%d.%m %H:%M:%S"),
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


_QUERY_LIMIT = 100  # max. Treffer pro Abfrage (jüngste zuerst)
_UNIT_SQL = {"m": "minutes", "h": "hours", "d": "days", "w": "days"}


def _parse_period(text: str):
    """'1m'/'2h'/'3d'/'1w' -> (sqlite_modifier, normalisiertes_label) oder (None, None) bei ungültig."""
    m = re.match(r"^\s*(\d+)\s*([mhdw])\s*$", (text or "").lower())
    if not m:
        return None, None
    n, unit = int(m.group(1)), m.group(2)
    amount = n * 7 if unit == "w" else n
    return f"-{amount} {_UNIT_SQL[unit]}", f"{n}{unit}"


async def _cmdlog_query_impl(cog, ctx, user_id, period):
    lang = await get_user_lang(cog.bot, ctx.author.id, ctx.guild_id)
    uid = (user_id or "").strip()
    if not uid.isdigit():
        await ctx.respond(l10n.get("invalid_ids", lang), ephemeral=True)
        return
    await ctx.defer(ephemeral=True)

    modifier = None
    label = ""
    if period:
        modifier, norm = _parse_period(period)
        if modifier is None:
            await ctx.followup.send(l10n.get("cmdlog_bad_period", lang), ephemeral=True)
            return
        label = l10n.get("cmdlog_period", lang, period=norm)

    base = "FROM command_log WHERE user_id=?"
    params = [uid]
    if modifier:
        base += " AND created_at >= datetime('now', ?)"
        params.append(modifier)

    total_rows = await execute_db(cog.bot, f"SELECT COUNT(*) AS c {base}", tuple(params), fetch=True)
    total = total_rows[0]["c"] if total_rows else 0
    if not total:
        await ctx.followup.send(l10n.get("cmdlog_none", lang, user=uid, period=label), ephemeral=True)
        return

    rows = await execute_db(
        cog.bot,
        f"SELECT created_at, command, params, channel_id, status {base} "
        f"ORDER BY created_at DESC LIMIT {_QUERY_LIMIT}",
        tuple(params), fetch=True,
    )
    lines = [l10n.get("cmdlog_query_header", lang, user=uid, shown=len(rows), total=total, period=label)]
    for r in rows:
        st = r["status"] or ""
        mark = "❌ " if st.startswith("error") else ("💬 " if st == "text" else "")
        p = f" {r['params']}" if r["params"] else ""
        chan = f"<#{r['channel_id']}>" if r["channel_id"] else "?"
        ts_local = berlin_from_utc_naive(r["created_at"], "%Y-%m-%d %H:%M:%S", "%d.%m %H:%M:%S")
        lines.append(f"{mark}`{ts_local}` `{r['command']}`{p} · {chan}")

    chunks = _chunk_lines(lines)
    first = True
    for chunk in chunks:
        embed = discord.Embed(description=chunk, color=discord.Color.dark_grey())
        if first:
            embed.title = l10n.get("cmdlog_title", lang)
            first = False
        await ctx.followup.send(embed=embed, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(CommandLogCog(bot))
