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
cogs/offer_alerts.py – Schlagwort-Alerts für den Angebote-Kanal (/offer_alert).

Nutzer hinterlegen Wunsch-Schlagworte (Arten ODER beliebige Begriffe). Passt ein
Angebot dazu, kommt eine private Nachricht mit Link.

Ablauf (Variante B):
  • Beim Einrichten (/offer_alert add): EINMALIGER Backfill der letzten
    OFFER_BACKFILL_DAYS Tage – nur noch offene (nicht verkaufte) Angebote,
    gebündelt per PN.
  • Danach laufend (Vorwärts-Scanner): neue Angebote, die älter als
    OFFER_ALERT_DELAY_MIN Minuten sind (Team-Review-Puffer).
  • Auf jede Alert-PN kann der Nutzer reagieren: ✅ = erledigt (Schlagwort
    entfernen) · 🔄 = weiter suchen.

„Verkauft"-Erkennung (aus echten #angebote-Daten abgeleitet) – PRO ZEILE und pro
Nachricht: :sold:-Emote (im Text oder als Reaktion), ~~Durchstreichung~~ sowie
starke Begriffe (verkauft/sold/vergeben/reserviert). Mehr-Positions-Posts werden
zeilenweise geprüft, sodass nur wirklich noch offene Positionen gemeldet werden.

Kanal via Env OFFER_CHANNEL_ID (0/leer = Feature inaktiv).
"""
import asyncio
import io
import logging
import re
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from config import (OFFER_CHANNEL_ID, OFFER_ALERT_DELAY_MIN, OFFER_BACKFILL_DAYS,
                    OFFER_SOLD_EMOTE_ID)
from utils.db import execute_db, execute_many
from utils.embeds import EMBED_COLOR, ADMIN_COLOR
from utils.timez import berlin_from_dt, align_delay_seconds
from cogs.server_settings import admin_or_manage_messages, allowed_channel

logger = logging.getLogger(__name__)

MAX_KEYWORDS = 25
_DONE, _KEEP = "✅", "🔄"

# Starke „verkauft"-Begriffe (bewusst OHNE mehrdeutige wie „weg"/„erledigt").
_STRONG_SOLD = re.compile(r"(?<!\w)(verkauft|sold|vergeben|reserviert|reserved)(?!\w)", re.I)
# Gesamt-Nachricht-Marker (ganze Anzeige beendet).
_WHOLE_SOLD = re.compile(r"(?<!\w)(alles (verkauft|weg|vergeben)|komplett verkauft|eos|end of sale)(?!\w)", re.I)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)

_HINT = ("ℹ️ Schon verkauft? Bitte den Anbieter, hinter der betreffenden Zeile das "
         ":sold:-Emote zu setzen bzw. mit :sold: auf die Nachricht zu reagieren – "
         "oder melde es dem Team.")


# ── Text-Helfer: Normalisierung, Matching, Sold-Erkennung ─────────────────────

def _plain(text: str) -> str:
    """Kleinschreibung, Markdown-Betonung (*_`~) entfernt, Whitespace reduziert –
    für Schlagwort- und Begriffssuche."""
    t = (text or "").lower()
    t = re.sub(r"[*_`~]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _kw_in(text_plain: str, kw: str) -> bool:
    """Schlagwort als eigenständiges Wort/Phrase (Wortgrenzen) – nicht zu locker
    (kein Teilwort-Treffer), nicht zu streng (Groß/klein & Markdown egal)."""
    if not kw:
        return False
    return re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text_plain) is not None


def _line_sold(line_raw: str) -> bool:
    """Ist diese einzelne Zeile als verkauft markiert?"""
    if str(OFFER_SOLD_EMOTE_ID) in line_raw:                # :sold:-Emote im Text
        return True
    if _STRONG_SOLD.search(_plain(line_raw)):               # verkauft/reserviert/…
        return True
    # Durchgestrichen: ganze Zeile ODER Rest nach Entfernen der ~~…~~-Spans nur noch
    # Preis/Satzzeichen (z.B. „~~1x Lasius niger~~ 20€") -> Position verkauft.
    if "~~" in line_raw:
        rest = _STRIKE.sub(" ", line_raw)
        rest = re.sub(r"\d+[.,]?\d*\s*(€|eur|euro|\$|vb)?", " ", rest, flags=re.I)  # Preise raus
        rest = re.sub(r"[^0-9a-zäöüß]+", " ", rest, flags=re.I).strip()
        if not rest:
            return True
    return False


def _message_sold_whole(content: str, reactions, author_id=None) -> bool:
    """Ist die GESAMTE Nachricht verkauft/beendet?"""
    for r in (reactions or []):
        emo = getattr(r, "emoji", None)
        if getattr(emo, "id", None) == OFFER_SOLD_EMOTE_ID:  # :sold:-Reaktion
            return True
    if _WHOLE_SOLD.search(_plain(content)):
        return True
    # komplette Nachricht durchgestrichen (jede nicht-leere Zeile ~~…~~)
    lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
    if lines and all(ln.startswith("~~") and ln.endswith("~~") for ln in lines):
        return True
    return False


def _keyword_open_in_line(line_raw: str, kw: str) -> bool:
    """Kommt das Schlagwort in dieser Zeile vor UND ist die Zeile offen (nicht verkauft)?"""
    plain = _plain(line_raw)
    if not _kw_in(plain, kw):
        return False
    if _line_sold(line_raw):
        return False
    # Schlagwort steckt (nur) in einem durchgestrichenen Teilstück? -> verkauft
    for span in _STRIKE.findall(line_raw):
        if _kw_in(_plain(span), kw):
            return False
    return True


def _open_hits(content: str, reactions, keywords, author_id=None) -> dict:
    """{keyword_norm: offene Trefferzeile} für alle Schlagworte, die in der
    Nachricht eine NOCH OFFENE Position treffen. Leer, wenn ganze Nachricht verkauft."""
    if _message_sold_whole(content, reactions, author_id):
        return {}
    lines = (content or "").splitlines() or [content or ""]
    hits: dict[str, str] = {}
    for kw in keywords:
        for ln in lines:
            if _keyword_open_in_line(ln, kw):
                hits[kw] = ln.strip()
                break
    return hits


def _first_open_line(content: str) -> str | None:
    for ln in (content or "").splitlines() or [content or ""]:
        s = ln.strip()
        if s and not _line_sold(ln):
            return s
    return None


async def _keyword_autocomplete(ctx: discord.AutocompleteContext):
    """Schlägt die eigenen Schlagworte des Nutzers vor (für /offer_alert remove)."""
    rows = await execute_db(
        ctx.bot, "SELECT keyword_raw FROM offer_keywords WHERE user_id=? ORDER BY created_at",
        (str(ctx.interaction.user.id),), fetch=True) or []
    q = (ctx.value or "").lower()
    return [r["keyword_raw"] for r in rows if q in (r["keyword_raw"] or "").lower()][:25]


class OfferAlertsCog(commands.Cog, name="OfferAlerts"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        if OFFER_CHANNEL_ID:
            self.scan_offers.start()

    def cog_unload(self):
        if OFFER_CHANNEL_ID and self.scan_offers.is_running():
            self.scan_offers.cancel()

    # ── Slash-Command-Gruppe ──────────────────────────────────────────────────
    offer_alert = discord.SlashCommandGroup(
        name="offer_alert",
        description="Keyword alerts for the offers channel",
        description_localizations={"de": "Schlagwort-Alerts für den Angebote-Kanal"},
    )

    @offer_alert.command(name="add", description="Add a keyword to be alerted about",
                         description_localizations={"de": "Ein Schlagwort für Alerts hinterlegen"})
    @commands.guild_only()
    @allowed_channel()
    async def add(self, ctx: discord.ApplicationContext,
                  schlagwort: discord.Option(str, "Keyword (species or any term)", name="schlagwort", description_localizations={"de": "Schlagwort (Art oder beliebiger Begriff)"}, required=True)):  # type: ignore[valid-type]
        await ctx.defer(ephemeral=True)
        if not OFFER_CHANNEL_ID:
            await ctx.followup.send("⚠️ Das Angebote-Alert-Feature ist nicht konfiguriert (OFFER_CHANNEL_ID fehlt).", ephemeral=True)
            return
        raw = (schlagwort or "").strip()
        kw = _plain(raw)
        if len(kw) < 2 or len(kw) > 80:
            await ctx.followup.send("⚠️ Das Schlagwort muss 2–80 Zeichen haben.", ephemeral=True)
            return
        uid = str(ctx.author.id)
        cnt = await execute_db(self.bot, "SELECT COUNT(*) AS n FROM offer_keywords WHERE user_id=?", (uid,), fetch=True)
        if cnt and cnt[0]["n"] >= MAX_KEYWORDS:
            await ctx.followup.send(f"⚠️ Maximal {MAX_KEYWORDS} Schlagworte pro Person.", ephemeral=True)
            return
        rc = await execute_db(self.bot, "INSERT OR IGNORE INTO offer_keywords (user_id, keyword, keyword_raw) VALUES (?,?,?)",
                              (uid, kw, raw), commit=True)
        if not rc:
            await ctx.followup.send(f"ℹ️ »{raw}« ist bereits hinterlegt.", ephemeral=True)
            return
        n = await self._backfill(ctx.author, uid, kw, raw)
        if n == -1:
            tail = ("Schlagwort gespeichert, aber ich konnte den Angebote-Kanal nicht lesen "
                    "(fehlende Rechte).")
        elif n > 0:
            tail = (f"✅ »{raw}« gespeichert. Ich habe dir **{n}** noch offene Angebote der letzten "
                    f"{OFFER_BACKFILL_DAYS} Tage per PN geschickt und melde ab jetzt neue.")
        else:
            tail = (f"✅ »{raw}« gespeichert. Aktuell keine offenen Treffer der letzten "
                    f"{OFFER_BACKFILL_DAYS} Tage – ich melde dir neue Angebote automatisch.")
        await ctx.followup.send(tail + "\n_(Kommt keine PN an, sind deine Server-DMs evtl. deaktiviert.)_", ephemeral=True)

    @offer_alert.command(name="list", description="Show your keywords",
                         description_localizations={"de": "Deine Schlagworte anzeigen"})
    @commands.guild_only()
    @allowed_channel()
    async def list_kw(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        rows = await execute_db(self.bot, "SELECT keyword_raw FROM offer_keywords WHERE user_id=? ORDER BY created_at",
                                (str(ctx.author.id),), fetch=True) or []
        if not rows:
            await ctx.followup.send("Du hast noch keine Schlagworte. Lege eins mit `/offer_alert add` an.", ephemeral=True)
            return
        body = "\n".join(f"• {r['keyword_raw']}" for r in rows)
        await ctx.followup.send(f"🔎 **Deine Angebots-Schlagworte ({len(rows)}):**\n{body}", ephemeral=True)

    @offer_alert.command(name="remove", description="Remove a keyword",
                         description_localizations={"de": "Ein Schlagwort entfernen"})
    @commands.guild_only()
    @allowed_channel()
    async def remove(self, ctx: discord.ApplicationContext,
                     schlagwort: discord.Option(str, "Keyword to remove", name="schlagwort", description_localizations={"de": "Zu entfernendes Schlagwort"}, autocomplete=_keyword_autocomplete, required=True)):  # type: ignore[valid-type]
        await ctx.defer(ephemeral=True)
        kw = _plain(schlagwort or "")
        rc = await execute_db(self.bot, "DELETE FROM offer_keywords WHERE user_id=? AND keyword=?",
                              (str(ctx.author.id), kw), commit=True)
        if rc:
            await ctx.followup.send(f"🗑️ »{schlagwort.strip()}« entfernt.", ephemeral=True)
        else:
            await ctx.followup.send("Kein passendes Schlagwort gefunden (siehe `/offer_alert list`).", ephemeral=True)

    @offer_alert.command(name="check", description="🔒 [Admin] List all offers currently recognized as available",
                         description_localizations={"de": "🔒 [Admin] Alle aktuell als verfügbar erkannten Angebote auflisten"})
    @admin_or_manage_messages()
    @allowed_channel()
    async def check(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        if not OFFER_CHANNEL_ID:
            await ctx.followup.send("⚠️ Nicht konfiguriert (OFFER_CHANNEL_ID fehlt).", ephemeral=True)
            return
        ch = self.bot.get_channel(OFFER_CHANNEL_ID)
        if ch is None or not hasattr(ch, "history"):
            await ctx.followup.send("⚠️ Angebote-Kanal nicht gefunden/lesbar.", ephemeral=True)
            return
        now = discord.utils.utcnow()
        after = now - timedelta(days=OFFER_BACKFILL_DAYS)
        before = now - timedelta(minutes=OFFER_ALERT_DELAY_MIN)
        rows = []
        try:
            async for m in ch.history(after=after, before=before, oldest_first=False, limit=None):
                if m.author.bot or m.webhook_id or not (m.content or "").strip():
                    continue
                if _message_sold_whole(m.content, m.reactions, m.author.id):
                    continue
                ol = _first_open_line(m.content)
                if ol is None:
                    continue
                rows.append((m, ol))
        except discord.Forbidden:
            await ctx.followup.send("⚠️ Keine Leseberechtigung für den Angebote-Kanal.", ephemeral=True)
            return
        header = (f"🔎 **Noch als verfügbar erkannt: {len(rows)}** Angebote "
                  f"(letzte {OFFER_BACKFILL_DAYS} Tage, ohne die letzten {OFFER_ALERT_DELAY_MIN} min)")
        lines = [f"[{berlin_from_dt(m.created_at, '%d.%m.%Y')}] {m.author.display_name}: "
                 f"{ol[:100]} — {m.jump_url}" for m, ol in rows]
        text = header + "\n" + "\n".join(lines)
        if len(text) > 1800:
            f = discord.File(io.BytesIO(text.encode("utf-8")), filename="offer_check.txt")
            await ctx.followup.send(header, file=f, ephemeral=True)
        else:
            await ctx.followup.send(text if rows else header, ephemeral=True)

    # ── Alerts / Backfill / Scanner ───────────────────────────────────────────
    async def _send_alert_dm(self, user, uid: str, kw: str, title: str, intro: str, body: str):
        """Schickt eine Alert-PN (mit Hinweis + ✅/🔄) und merkt sich die Nachricht
        für den Reaktions-Lebenszyklus. True, wenn zugestellt."""
        embed = discord.Embed(title=title[:256], description=(f"{intro}\n\n{body}")[:4000], color=EMBED_COLOR)
        embed.add_field(name="​", value=_HINT, inline=False)
        embed.set_footer(text="✅ = erledigt (Schlagwort entfernen)   ·   🔄 = weiter suchen")
        try:
            dm = await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return False
        for e in (_DONE, _KEEP):
            try:
                await dm.add_reaction(e)
            except Exception:
                pass
        await execute_db(self.bot, "INSERT OR REPLACE INTO offer_alert_msgs (message_id, user_id, keyword) VALUES (?,?,?)",
                         (str(dm.id), uid, kw), commit=True)
        return True

    async def _backfill(self, user, uid: str, kw: str, raw: str) -> int:
        """Einmaliger Rückblick (OFFER_BACKFILL_DAYS) für EIN Schlagwort. Rückgabe:
        Zahl offener Treffer, 0 = keine, -1 = Kanal nicht lesbar."""
        ch = self.bot.get_channel(OFFER_CHANNEL_ID)
        if ch is None or not hasattr(ch, "history"):
            return -1
        now = discord.utils.utcnow()
        after = now - timedelta(days=OFFER_BACKFILL_DAYS)
        before = now - timedelta(minutes=OFFER_ALERT_DELAY_MIN)
        matches = []
        try:
            async for m in ch.history(after=after, before=before, oldest_first=False, limit=None):
                if m.author.bot or m.webhook_id:
                    continue
                hits = _open_hits(m.content, m.reactions, {kw}, m.author.id)
                if kw in hits:
                    matches.append((m, hits[kw]))
        except discord.Forbidden:
            return -1
        if not matches:
            return 0
        await execute_many(self.bot, "INSERT OR IGNORE INTO offer_alert_seen (user_id, message_id, keyword) VALUES (?,?,?)",
                           [(uid, str(m.id), kw) for m, _ in matches])
        shown = matches[:20]
        lines = []
        for m, snip in shown:
            when = berlin_from_dt(m.created_at, "%d.%m.%Y")
            s = (snip or m.content or "").strip().replace("\n", " ")[:120]
            lines.append(f"• [{when}] {s} — {m.jump_url}")
        extra = len(matches) - len(shown)
        if extra > 0:
            lines.append(f"… und {extra} weitere.")
        await self._send_alert_dm(
            user, uid, kw,
            title=f"🔎 Bestehende Angebote zu »{raw}«",
            intro=f"Ich habe **{len(matches)}** noch offene Angebote der letzten {OFFER_BACKFILL_DAYS} Tage gefunden:",
            body="\n".join(lines))
        return len(matches)

    @tasks.loop(minutes=10)
    async def scan_offers(self):
        """Vorwärts-Scanner: neue Angebote (älter als der Puffer) gegen alle
        hinterlegten Schlagworte prüfen und offene Treffer per PN melden."""
        if not OFFER_CHANNEL_ID:
            return
        ch = self.bot.get_channel(OFFER_CHANNEL_ID)
        if ch is None or not hasattr(ch, "history"):
            return
        subs = await execute_db(self.bot, "SELECT user_id, keyword, keyword_raw FROM offer_keywords", fetch=True) or []
        cursor = await self._get_cursor()
        if cursor is None:
            # Erstlauf: neueste Nachricht als Startpunkt (kein Alt-Backlog vorwärts).
            last_id = 0
            try:
                async for m in ch.history(limit=1):
                    last_id = m.id
            except Exception:
                return
            await self._set_cursor(last_id)
            return
        if not subs:
            return
        by_kw: dict[str, list] = {}
        for s in subs:
            by_kw.setdefault(s["keyword"], []).append((s["user_id"], s["keyword_raw"]))
        distinct = set(by_kw)
        cutoff = discord.utils.utcnow() - timedelta(minutes=OFFER_ALERT_DELAY_MIN)
        new_cursor = int(cursor)
        try:
            async for m in ch.history(after=discord.Object(id=int(cursor)), oldest_first=True, limit=None):
                if m.created_at > cutoff:
                    break                      # noch im Review-Puffer -> später erneut
                new_cursor = m.id
                if m.author.bot or m.webhook_id:
                    continue
                hits = _open_hits(m.content, m.reactions, distinct, m.author.id)
                for kw, line in hits.items():
                    for uid, raw in by_kw.get(kw, []):
                        await self._forward_alert(uid, kw, raw or kw, m, line)
        except Exception as e:
            logger.warning("offer_alerts scan: %s", e)
        if new_cursor != int(cursor):
            await self._set_cursor(new_cursor)

    @scan_offers.before_loop
    async def _before_scan(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(align_delay_seconds(10 * 60))   # an der Uhr ausrichten

    async def _forward_alert(self, uid: str, kw: str, raw: str, m: discord.Message, line: str):
        seen = await execute_db(self.bot, "SELECT 1 FROM offer_alert_seen WHERE user_id=? AND message_id=? AND keyword=?",
                                (uid, str(m.id), kw), fetch=True)
        if seen:
            return
        await execute_db(self.bot, "INSERT OR IGNORE INTO offer_alert_seen (user_id, message_id, keyword) VALUES (?,?,?)",
                         (uid, str(m.id), kw), commit=True)
        try:
            user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
        except Exception:
            return
        when = berlin_from_dt(m.created_at, "%d.%m.%Y %H:%M")
        snip = (line or m.content or "").strip().replace("\n", " ")[:200]
        await self._send_alert_dm(user, uid, kw,
                                  title=f"🔔 Neues Angebot zu »{raw}«",
                                  intro=f"[{when}] {snip}",
                                  body=m.jump_url)

    # ── Reaktions-Lebenszyklus (✅ erledigt / 🔄 weiter) ───────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        rows = await execute_db(self.bot, "SELECT user_id, keyword FROM offer_alert_msgs WHERE message_id=?",
                                (str(payload.message_id),), fetch=True)
        if not rows:
            return
        row = rows[0]
        if str(payload.user_id) != row["user_id"]:
            return
        emoji = str(payload.emoji)
        if emoji not in (_DONE, _KEEP):
            return
        await execute_db(self.bot, "DELETE FROM offer_alert_msgs WHERE message_id=?", (str(payload.message_id),), commit=True)
        try:
            user = self.bot.get_user(payload.user_id) or await self.bot.fetch_user(payload.user_id)
        except Exception:
            user = None
        if emoji == _DONE:
            await execute_db(self.bot, "DELETE FROM offer_keywords WHERE user_id=? AND keyword=?",
                             (row["user_id"], row["keyword"]), commit=True)
            if user:
                try:
                    await user.send("✅ Erledigt – das Schlagwort wurde entfernt, ich suche nicht mehr danach.")
                except Exception:
                    pass
        else:
            if user:
                try:
                    await user.send("🔄 Alles klar, ich suche weiter nach neuen Angeboten.")
                except Exception:
                    pass

    # ── Cursor-Helfer ─────────────────────────────────────────────────────────
    async def _get_cursor(self):
        rows = await execute_db(self.bot, "SELECT last_message_id FROM offer_cursor WHERE id=1", fetch=True)
        return rows[0]["last_message_id"] if rows else None

    async def _set_cursor(self, mid):
        await execute_db(self.bot, "INSERT INTO offer_cursor (id, last_message_id) VALUES (1, ?) "
                         "ON CONFLICT(id) DO UPDATE SET last_message_id=excluded.last_message_id",
                         (str(mid),), commit=True)


def setup(bot: discord.Bot):
    bot.add_cog(OfferAlertsCog(bot))
