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
cogs/chat_export.py – Admin-Werkzeug: kompletten Channel (per ID) als umfangreiche
JSON exportieren und als Datei-Anhang (ephemer) herunterladen.

Optionaler Datumsbereich (von/bis, Berliner Zeit, bis inklusive). Erfasst pro
Nachricht so viel wie über die Discord-API verfügbar ist: Autor, Zeitstempel
(erstellt/bearbeitet), Inhalt, Anhänge inkl. Bild-Links, Embeds, Sticker,
Reaktionen (Emoji + Anzahl; wer reagiert hat nur mit reaktionen_detail=true),
Antwort-/Referenzbezug, Mentions, Thread-Info. Bei Überschreitung des Upload-
Limits wird die Datei gzip-komprimiert (.json.gz).

NICHT abrufbar (Discord-API-Grenzen): gelöschte Nachrichten, frühere
Bearbeitungsversionen (nur aktueller Text + edited_at), „gelesen von"/Views.

Nur Admin/„Nachrichten verwalten"; der Guild-Lock (main.py) begrenzt auf den
gebundenen Server.
"""
import gzip
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from cogs.server_settings import admin_or_manage_messages, allowed_channel
from utils.embeds import ADMIN_COLOR
from utils.timez import BERLIN, berlin_from_dt

logger = logging.getLogger(__name__)

_DATE_FMT = "%Y-%m-%d"


def _parse_date(value: str | None):
    """'YYYY-MM-DD' (Berliner Tag) → tz-bewusstes datetime (00:00 Berlin) oder None."""
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, _DATE_FMT).replace(tzinfo=BERLIN)


def _ts(dt) -> dict | None:
    """Zeitstempel als UTC-ISO + Berliner Klartext (MEZ/MESZ)."""
    if dt is None:
        return None
    return {"utc": dt.astimezone(timezone.utc).isoformat(),
            "berlin": berlin_from_dt(dt, "%d.%m.%Y %H:%M:%S")}


def _attachment(a: discord.Attachment) -> dict:
    ct = a.content_type or ""
    return {
        "id": a.id, "filename": a.filename, "url": a.url, "proxy_url": a.proxy_url,
        "content_type": a.content_type, "size": a.size,
        "width": getattr(a, "width", None), "height": getattr(a, "height", None),
        "is_image": ct.startswith("image"), "is_video": ct.startswith("video"),
        "description": getattr(a, "description", None), "spoiler": a.is_spoiler(),
    }


async def _reaction(r: discord.Reaction, with_users: bool) -> dict:
    emoji = r.emoji
    is_custom = not isinstance(emoji, str)
    d = {
        "emoji": str(emoji),
        "name": getattr(emoji, "name", None) if is_custom else str(emoji),
        "id": getattr(emoji, "id", None) if is_custom else None,
        "custom": is_custom,
        "count": r.count,
        "url": (str(emoji.url) if is_custom and getattr(emoji, "url", None) else None),
    }
    if with_users:
        users = []
        try:
            async for u in r.users():
                users.append({"id": u.id, "name": str(u), "display_name": getattr(u, "display_name", None), "bot": u.bot})
        except Exception as e:
            logger.warning("export_chat: Reaktions-User %s nicht abrufbar: %s", d["emoji"], e)
        d["users"] = users
    return d


async def _message_to_dict(m: discord.Message, with_reaction_users: bool) -> dict:
    ref = None
    if m.reference:
        ref = {"message_id": m.reference.message_id,
               "channel_id": m.reference.channel_id,
               "guild_id": m.reference.guild_id}
    reactions = []
    for r in m.reactions:
        reactions.append(await _reaction(r, with_reaction_users))
    try:
        clean = m.clean_content
    except Exception:
        clean = m.content
    return {
        "id": m.id,
        "type": str(m.type),
        "jump_url": m.jump_url,
        "created_at": _ts(m.created_at),
        "edited_at": _ts(m.edited_at),
        "pinned": m.pinned,
        "tts": m.tts,
        "flags": [f for f, v in m.flags if v],
        "author": {
            "id": m.author.id, "name": str(m.author),
            "display_name": getattr(m.author, "display_name", None),
            "bot": m.author.bot, "system": m.author.system,
        },
        "content": m.content,
        "clean_content": clean,
        "attachments": [_attachment(a) for a in m.attachments],
        "embeds": [e.to_dict() for e in m.embeds],
        "stickers": [{"id": s.id, "name": s.name, "url": str(getattr(s, "url", "") or "")}
                     for s in m.stickers],
        "reactions": reactions,
        "reference": ref,
        "mentions": {
            "everyone": m.mention_everyone,
            "users": [{"id": u.id, "name": str(u)} for u in m.mentions],
            "roles": [{"id": r.id, "name": r.name} for r in m.role_mentions],
            "channels": [{"id": c.id, "name": getattr(c, "name", None)} for c in m.channel_mentions],
        },
        "thread": ({"id": m.thread.id, "name": m.thread.name} if getattr(m, "thread", None) else None),
        "webhook_id": m.webhook_id,
        "application_id": getattr(m, "application_id", None),
    }


class ChatExportCog(commands.Cog, name="ChatExport"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="export_chat",
        description="🔒 [Admin] Export a channel to a downloadable JSON",
        description_localizations={"de": "🔒 [Admin] Einen Channel als herunterladbare JSON exportieren"},
    )
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def export_chat(
        self,
        ctx: discord.ApplicationContext,
        channel_id: discord.Option(str, "Channel-/Thread-ID", name="channel", description_localizations={"de": "Channel-/Thread-ID"}, required=True),  # type: ignore[valid-type]
        von: discord.Option(str, "Start date YYYY-MM-DD (optional)", name="von", description_localizations={"de": "Startdatum JJJJ-MM-TT (optional)"}, required=False, default=None),  # type: ignore[valid-type]
        bis: discord.Option(str, "End date YYYY-MM-DD, inclusive (optional)", name="bis", description_localizations={"de": "Enddatum JJJJ-MM-TT, inklusive (optional)"}, required=False, default=None),  # type: ignore[valid-type]
        reaktionen_detail: discord.Option(bool, "Also list WHO reacted (slow for large channels)", name="reaktionen_detail", description_localizations={"de": "Auch auflisten, WER reagiert hat (langsam bei großen Channels)"}, required=False, default=False),  # type: ignore[valid-type]
    ):
        await ctx.defer(ephemeral=True)

        # Channel auflösen
        try:
            cid = int(channel_id.strip())
        except (TypeError, ValueError):
            await ctx.followup.send("❌ Ungültige Channel-ID (nur Zahlen).", ephemeral=True)
            return
        channel = self.bot.get_channel(cid)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(cid)
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "history"):
            await ctx.followup.send("❌ Channel nicht gefunden oder nicht lesbar (Text-Channel/Thread-ID angeben).", ephemeral=True)
            return

        # Datumsbereich (bis inklusive → before = bis + 1 Tag 00:00)
        try:
            after = _parse_date(von)
            _bis = _parse_date(bis)
            before = (_bis + timedelta(days=1)) if _bis else None
        except ValueError:
            await ctx.followup.send("❌ Datumsformat muss JJJJ-MM-TT sein (z.B. 2026-07-01).", ephemeral=True)
            return

        # Nachrichten einlesen (chronologisch, ohne Limit = alles)
        messages = []
        try:
            async for m in channel.history(limit=None, after=after, before=before, oldest_first=True):
                messages.append(await _message_to_dict(m, reaktionen_detail))
        except discord.Forbidden:
            await ctx.followup.send("❌ Keine Leseberechtigung für diesen Channel (View Channel + Read Message History nötig).", ephemeral=True)
            return
        except Exception as e:
            logger.error("export_chat: Verlauf lesen fehlgeschlagen (%s): %s", cid, e, exc_info=True)
            await ctx.followup.send(f"❌ Export fehlgeschlagen: {str(e)[:150]}", ephemeral=True)
            return

        payload = {
            "meta": {
                "channel": {"id": channel.id, "name": getattr(channel, "name", None), "type": str(channel.type)},
                "guild": {"id": getattr(getattr(channel, "guild", None), "id", None),
                          "name": getattr(getattr(channel, "guild", None), "name", None)},
                "exported_at": _ts(discord.utils.utcnow()),
                "exported_by": {"id": ctx.author.id, "name": str(ctx.author)},
                "range": {"von": von or None, "bis": bis or None},
                "reactions_detail": reaktionen_detail,
                "message_count": len(messages),
            },
            "messages": messages,
        }

        raw = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        limit = getattr(getattr(channel, "guild", None), "filesize_limit", None) or (8 * 1024 * 1024)
        base = f"chat_{channel.id}_{von or 'start'}_{bis or 'ende'}.json"

        data, fname, note = raw, base, ""
        if len(raw) > limit - 256 * 1024:            # gzip erst bei Überschreitung
            data = gzip.compress(raw)
            fname = base + ".gz"
            note = f" · gzip ({len(raw)//1024} KB → {len(data)//1024} KB)"
        if len(data) > limit:
            await ctx.followup.send(
                f"⚠️ Export ({len(data)//1024} KB) überschreitet das Upload-Limit dieses Servers "
                f"({limit//1024} KB) selbst komprimiert. Bitte engeren Zeitraum (von/bis) wählen.",
                ephemeral=True)
            return

        embed = discord.Embed(
            title="📤 Chat-Export",
            description=(f"**Channel:** {getattr(channel, 'mention', channel.id)}\n"
                        f"**Nachrichten:** {len(messages)}\n"
                        f"**Zeitraum:** {von or 'Anfang'} – {bis or 'Ende'}\n"
                        f"**Reaktions-Detail:** {'ja' if reaktionen_detail else 'nein'}\n"
                        f"**Datei:** {len(data)//1024} KB{note}"),
            color=ADMIN_COLOR,
        )
        await ctx.followup.send(embed=embed, file=discord.File(io.BytesIO(data), filename=fname), ephemeral=True)
        logger.info("📤 export_chat: %s Nachrichten aus #%s von %s (%s KB%s)",
                    len(messages), getattr(channel, "name", cid), ctx.author.id, len(data)//1024,
                    " gz" if fname.endswith(".gz") else "")


def setup(bot: discord.Bot):
    bot.add_cog(ChatExportCog(bot))
