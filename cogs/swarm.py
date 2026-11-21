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
cogs/swarm.py – Schwarmflug-Radar.

Liest den bestehenden Schwarmflug-Kanal (SWARM_CHANNEL_ID): neue, bearbeitete und gelöschte
Nachrichten werden laufend ausgewertet. Kein neues Formular, die Community meldet wie bisher.

  • /schwarmflug                     Link zum Radar + was in den letzten 48 Stunden geflogen ist
  • /schwarmflug_vorlage             Meldung nach der angepinnten Vorlage ausfüllen, Text zum Kopieren
  • Saisonstart                      erste Meldung des Jahres: Board-Banner/Intro, PN auf Wunsch, Erfolg
  • Wetter                           stündlich Wetter zur Flugzeit nachtragen (Open-Meteo, utils/weather.py)
  • Prognose                         April bis September um 6/9/12/15/18 Uhr (utils/forecast.py); abends PN an
                                     Alarm-Inhaber (Stufe aus Mein Konto) und bei sehr guten Chancen ein Post im Kanal
  • /wartung aktion:prognose         Prognose sofort rechnen, Modelle neu lernen (Admin)
  • /wartung aktion:schwarmflug      ganzen Kanalverlauf (erneut) einlesen (Admin)
  • Schwarmflug-Alarm                PN, wenn eine beobachtete Art in der eigenen Region fliegt
                                     (Einrichtung auf dem Board unter „Mein Konto“)

0/leer SWARM_CHANNEL_ID = Feature aus.
"""
import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from config import SWARM_CHANNEL_ID, BOARD_PUBLIC_URL, SWARM_WEATHER, SWARM_FORECAST, SWARM_FORECAST_POST
from cogs.server_settings import admin_or_manage_messages, allowed_channel
from utils import swarm as S
from utils import geocode
from utils.account import is_quiet
from utils.db import execute_db
from utils.embeds import EMBED_COLOR, ADMIN_COLOR
from utils.localization import l10n, get_user_lang, get_guild_lang
from utils.timez import align_delay_seconds, BERLIN

logger = logging.getLogger(__name__)

_OK = "✅"
_ALERT_WINDOW_H = 12


async def _ac_taxon_ref(ctx: discord.AutocompleteContext):
    """Vorschläge: Gattungen/Arten aus den Schwarmflug-Meldungen, dann Arten aus dem Verzeichnis."""
    q = (ctx.value or "").strip().lower()
    try:
        rows = await execute_db(ctx.bot, "SELECT genus, species, COUNT(*) AS n FROM swarm_reports "
                                          "GROUP BY genus, species ORDER BY n DESC LIMIT 400", fetch=True) or []
        names = [r["genus"] + (" " + r["species"] if r["species"] else "") for r in rows]
    except Exception:
        names = []
    out = [n for n in names if q in n.lower()][:25]
    if len(out) < 25:
        try:
            from cogs.directory import _ac_species
            for x in await _ac_species(ctx):
                v = x if isinstance(x, str) else getattr(x, "name", str(x))
                if v not in out:
                    out.append(v)
        except Exception:
            pass
    return out[:25]


async def _ac_region_ref(ctx: discord.AutocompleteContext):
    from utils import geo
    q = (ctx.value or "").strip().lower()
    names = sorted({n for regs in geo.all_regions().values() for n in regs.values()})
    return [n for n in names if q in n.lower()][:25]


def _roles(m: discord.Message) -> dict:
    return {str(r.id): r.name for r in getattr(m, "role_mentions", []) or []}


class Swarm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._scan_running = False
        if SWARM_CHANNEL_ID:
            self.alert_loop.start()
            self.geo_loop.start()
            if SWARM_WEATHER:
                self.weather_loop.start()
            if SWARM_FORECAST:
                self.forecast_loop.start()
            bot.loop.create_task(self._load())

    async def _load(self):
        await self.bot.wait_until_ready()
        try:
            # Erfolge zählen nur Meldungen ab dem ersten Start mit Schwarmflug-Kanal (keine alten Posts)
            await execute_db(self.bot, "INSERT OR IGNORE INTO swarm_meta (key, value) VALUES ('ach_since', ?)",
                             (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),), commit=True)
            await S.load_season(self.bot)
            await S.reclassify_abroad(self.bot)
        except Exception as e:
            logger.info("🪽 Saisonstart nicht geladen: %s", e)

    def cog_unload(self):
        self.alert_loop.cancel()
        if self.geo_loop.is_running():
            self.geo_loop.cancel()
        if self.weather_loop.is_running():
            self.weather_loop.cancel()
        if self.forecast_loop.is_running():
            self.forecast_loop.cancel()

    # ── Auswertung einer Nachricht ───────────────────────────────────────────
    async def process(self, m: discord.Message, use_ai: bool = True, geo: bool = True) -> int:
        """geo=False (Einlesen des Verlaufs): Ort nicht sofort auflösen; das erledigt geo_loop
        im Hintergrund (OpenStreetMap erlaubt nur 1 Anfrage/s)."""
        if m.author.bot or m.webhook_id or not (m.content or "").strip():
            return 0
        p = S.parse_message(m.content, _roles(m), m.created_at)
        source = "rule"
        if not p["rule_ok"]:
            if use_ai and len(re.findall(r"\w+", m.content)) >= 4:
                ai = await asyncio.to_thread(S.ai_parse, S._clean(m.content, _roles(m)), m.created_at)
                if ai is not None:
                    keep = {k: p.get(k) for k in ("wind", "weather", "protected")}
                    p = {**keep, **ai}
                    source = "ai"
            elif not p["taxa"]:
                pass
            else:
                p["taxa"] = []           # längeres Gespräch ohne KI: nicht als Meldung zählen
        if not p.get("taxa"):
            # Nachtrag ohne Art (z. B. „Ort: Köln“): Ort an die Meldung kurz davor hängen
            if p.get("place") or p.get("region"):
                await self._fill_location(m, p)
            await S.store(self.bot, m.id, m.author.id, m.created_at, {"taxa": []}, source)
            return 0
        # Ohne Ort: von der beantworteten bzw. letzten eigenen Meldung übernehmen
        if not (p.get("place") or p.get("region")):
            prev = None
            ref = getattr(m.reference, "message_id", None)
            if ref:
                r = await execute_db(self.bot, "SELECT * FROM swarm_reports WHERE message_id=? LIMIT 1",
                                     (str(ref),), fetch=True)
                prev = dict(r[0]) if r and str(r[0]["author_id"]) == str(m.author.id) else None
            prev = prev or await S.previous_of(self.bot, m.author.id, m.created_at)
            if prev:
                p.update(place=prev["place"], country=prev["country"], region=prev["region"])
                if prev.get("lat") is not None:
                    return await S.store(self.bot, m.id, m.author.id, m.created_at, p, source,
                                         prev["lat"], prev["lon"], "inherit")
        if not p.get("abroad"):
            # KI-Ergebnis oder übernommener Ort: Land außerhalb DACH?
            p["abroad"] = S.foreign_country(p.get("place") or "") or \
                (p["country"] if p.get("country") and p["country"] not in S.DACH else None)
        lat = lon = gsrc = None
        if p.get("abroad"):
            # Ausland: Land merken, keine Ortssuche (Nominatim ist auf DACH begrenzt, das ergäbe Fehltreffer)
            p["country"], p["region"] = p["abroad"], ""
            return await S.store(self.bot, m.id, m.author.id, m.created_at, p, source, None, None, "abroad")
        if p.get("place") and geo:
            g = await geocode.locate(self.bot, p["place"], p.get("country") or "", p.get("region") or "")
            if g:
                lat, lon, gsrc = g["lat"], g["lon"], g["src"]
                if not p.get("region") and g.get("region"):
                    p["country"], p["region"] = g["country"], g["region"]
        return await S.store(self.bot, m.id, m.author.id, m.created_at, p, source, lat, lon, gsrc)

    async def _fill_location(self, m, p):
        since = (m.created_at - timedelta(minutes=30)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = await execute_db(self.bot,
            "SELECT DISTINCT message_id FROM swarm_reports WHERE author_id=? AND posted_at>=? AND posted_at<=? "
            "AND region='' AND place=''", (str(m.author.id), since,
            m.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")), fetch=True) or []
        if not rows:
            return
        c = S.foreign_country(p.get("place") or "")
        if c:
            for r in rows:
                await execute_db(self.bot, "UPDATE swarm_reports SET place=?, country=?, region='', lat=NULL, lon=NULL, "
                                           "geo_src='abroad' WHERE message_id=?", (p["place"], c, r["message_id"]),
                                 commit=True)
            return
        lat = lon = gsrc = None
        if p.get("place"):
            g = await geocode.locate(self.bot, p["place"], p.get("country") or "", p.get("region") or "")
            if g:
                lat, lon, gsrc = g["lat"], g["lon"], g["src"]
                if not p.get("region"):
                    p["country"], p["region"] = g["country"], g["region"] or ""
        for r in rows:
            await execute_db(self.bot,
                "UPDATE swarm_reports SET place=?, country=?, region=?, lat=?, lon=?, geo_src=? WHERE message_id=?",
                (p.get("place") or "", p.get("country") or "", p.get("region") or "", lat, lon, gsrc,
                 r["message_id"]), commit=True)

    # ── Kanal-Ereignisse ─────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not SWARM_CHANNEL_ID or m.channel.id != SWARM_CHANNEL_ID:
            return
        try:
            n = await self.process(m)
        except Exception as e:
            logger.error("🪽 Schwarmflug-Meldung %s nicht ausgewertet: %s", m.id, e, exc_info=True)
            return
        if n:
            try:
                await m.add_reaction(_OK)
            except discord.HTTPException:
                pass
            try:
                start = await S.season_check(self.bot, m.id)
                if start:
                    logger.info("🪽 Schwarmflug-Saison %s eröffnet: %s", start["year"], start["label"])
            except Exception as e:
                logger.warning("🪽 Saisonstart-Prüfung fehlgeschlagen: %s", e)
            try:
                from utils.achievements import check_and_grant
                await check_and_grant(self.bot, m.author, await get_user_lang(self.bot, m.author.id, None))
            except Exception as e:
                logger.debug("🪽 Erfolge: %s", e)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if not SWARM_CHANNEL_ID or payload.channel_id != SWARM_CHANNEL_ID:
            return
        ch = self.bot.get_channel(SWARM_CHANNEL_ID)
        try:
            m = await ch.fetch_message(payload.message_id)
            await self.process(m)
        except Exception as e:
            logger.info("🪽 Bearbeitung %s nicht neu ausgewertet: %s", payload.message_id, e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if SWARM_CHANNEL_ID and payload.channel_id == SWARM_CHANNEL_ID:
            await S.remove(self.bot, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if SWARM_CHANNEL_ID and payload.channel_id == SWARM_CHANNEL_ID:
            for mid in payload.message_ids:
                await S.remove(self.bot, mid)

    # ── Admin: ganzen Verlauf einlesen ───────────────────────────────────────
    @discord.slash_command(name="swarm_rescan",
                           description="🔒 [Admin] Read the whole swarm flight channel (again)",
                           description_localizations={"de": "🔒 [Admin] Schwarmflug-Kanal komplett (neu) einlesen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def swarm_rescan(self, ctx: discord.ApplicationContext,
                           alle: discord.Option(bool, "Also re-read messages already scanned",
                                                description_localizations={"de": "Auch schon gelesene Nachrichten neu auswerten"},
                                                default=False) = False,
                           ki: discord.Option(bool, "Use AI for free-text messages (costs a little)",
                                              description_localizations={"de": "KI für Freitext nutzen (kostet etwas)"},
                                              default=False) = False):
        # Über /wartung kommt nur ctx an; die Optionen sind dann noch Option-Objekte.
        alle = alle if isinstance(alle, bool) else False
        ki = ki if isinstance(ki, bool) else False
        await ctx.defer(ephemeral=True)
        ch = self.bot.get_channel(SWARM_CHANNEL_ID) if SWARM_CHANNEL_ID else None
        if ch is None:
            await ctx.followup.send("❌ SWARM_CHANNEL_ID ist nicht gesetzt oder der Kanal ist nicht erreichbar.",
                                    ephemeral=True)
            return
        if self._scan_running:
            await ctx.followup.send("⏳ Das Einlesen läuft bereits.", ephemeral=True)
            return
        self._scan_running = True
        # Läuft im Hintergrund: Discord erlaubt die Antwort auf einen Befehl nur 15 Minuten lang.
        self.bot.loop.create_task(self._rescan_job(ctx.author, ch, alle, ki))
        await ctx.followup.send("⏳ Ich lese den Schwarmflug-Kanal jetzt im Hintergrund ein. Das dauert ein paar "
                                "Minuten; das Ergebnis schicke ich dir per PN. Die Orte löse ich danach nach und nach "
                                "auf (OpenStreetMap erlaubt nur eine Anfrage pro Sekunde).", ephemeral=True)

    async def _rescan_job(self, user, ch, alle: bool, ki: bool):
        t0 = time.monotonic()
        n_msg = n_rep = n_skip = 0
        err = ""
        try:
            seen = set() if alle else {r["message_id"] for r in await execute_db(
                self.bot, "SELECT message_id FROM swarm_scanned", fetch=True) or []}
            async for m in ch.history(limit=None, oldest_first=True):
                if str(m.id) in seen:
                    n_skip += 1
                    continue
                try:
                    n_rep += await self.process(m, use_ai=ki, geo=False)
                except Exception as e:
                    logger.warning("🪽 Rescan %s: %s", m.id, e)
                n_msg += 1
                if n_msg % 25 == 0:
                    await asyncio.sleep(0)          # andere Befehle zwischendurch beantworten
                if n_msg % 500 == 0:
                    logger.info("🪽 Rescan: %d Nachrichten ausgewertet …", n_msg)
        except Exception as e:
            err = str(e)[:300]
            logger.error("🪽 Rescan abgebrochen: %s", e, exc_info=True)
        finally:
            self._scan_running = False
        pending = await execute_db(self.bot, "SELECT COUNT(DISTINCT place || '|' || country || '|' || region) AS n "
                                             "FROM swarm_reports WHERE place<>'' AND lat IS NULL AND geo_src IS NULL",
                                   fetch=True)
        n_geo = pending[0]["n"] if pending else 0
        logger.info("🪽 Rescan fertig: %d Nachrichten, %d Meldungen, %d übersprungen, %d Orte offen",
                    n_msg, n_rep, n_skip, n_geo)
        e = discord.Embed(title="🪽 Schwarmflug-Kanal eingelesen" if not err else "⚠️ Einlesen abgebrochen",
                          color=ADMIN_COLOR,
                          description=f"Nachrichten ausgewertet: **{n_msg}**\nMeldungen (Art je Nachricht): **{n_rep}**\n"
                                      f"Übersprungen (schon gelesen): {n_skip}\nDauer: {time.monotonic() - t0:.0f} s\n"
                                      f"Orte zum Auflösen: {n_geo} (läuft im Hintergrund, etwa {max(1, n_geo // 50)} Min.)"
                                      + (f"\n\nFehler: {err}" if err else ""))
        if BOARD_PUBLIC_URL:
            e.add_field(name="Radar", value=f"{BOARD_PUBLIC_URL}/schwarmflug", inline=False)
        try:
            await user.send(embed=e)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── Orte im Hintergrund auflösen (nach dem Einlesen) ─────────────────────
    @tasks.loop(minutes=5)
    async def geo_loop(self):
        rows = [dict(r) for r in await execute_db(self.bot,
            "SELECT place, country, region, COUNT(*) AS n FROM swarm_reports WHERE place<>'' AND lat IS NULL "
            "AND geo_src IS NULL GROUP BY place, country, region ORDER BY n DESC LIMIT 200", fetch=True) or []]
        for r in rows:
            c = S.foreign_country(r["place"])
            if c:
                await execute_db(self.bot, "UPDATE swarm_reports SET country=?, region='', geo_src='abroad' "
                                           "WHERE place=? AND lat IS NULL", (c, r["place"]), commit=True)
                continue
            try:
                g = await geocode.locate(self.bot, r["place"], r["country"] or "", r["region"] or "")
            except Exception as e:
                logger.info("🗺️ Ortsauflösung: %s", e)
                return
            if g:
                await execute_db(self.bot,
                    "UPDATE swarm_reports SET lat=?, lon=?, geo_src=?, country=CASE WHEN region='' THEN ? ELSE country END, "
                    "region=CASE WHEN region='' THEN ? ELSE region END "
                    "WHERE place=? AND country=? AND region=? AND lat IS NULL",
                    (g["lat"], g["lon"], g["src"], g["country"] or "", g.get("region") or "",
                     r["place"], r["country"], r["region"]), commit=True)
            else:
                # Nur endgültig „nicht gefunden“ (im Cache vermerkt) abhaken; Netzfehler → später erneut
                if await geocode._cache_get(self.bot, geocode._norm(f"{r['place']}|{r['country'] or ''}")) is None:
                    return
                await execute_db(self.bot, "UPDATE swarm_reports SET geo_src='none' WHERE place=? AND country=? "
                                           "AND region=? AND lat IS NULL", (r["place"], r["country"], r["region"]),
                                 commit=True)

    @geo_loop.before_loop
    async def _before_geo(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    # ── /schwarmflug ─────────────────────────────────────────────────────────
    @discord.slash_command(name="schwarmflug", description="Swarm flight radar: what flew in the last 48 hours",
                           description_localizations={"de": "Schwarmflug-Radar: was in den letzten 48 Stunden geflogen ist"})
    @allowed_channel()
    async def schwarmflug(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        since = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d")
        rs = await S.rows(self.bot, since)
        cnt: dict = {}
        for r in rs:
            reg = S.region_name(r["country"], r["region"]) if r["region"] else ""
            cnt.setdefault(r["label"], set()).add(reg)
        lines = [f"• **{k}**" + (f" ({', '.join(sorted(x for x in v if x))[:120]})" if any(v) else "")
                 for k, v in sorted(cnt.items(), key=lambda x: -len(x[1]))[:15]]
        e = discord.Embed(title=l10n.get("swarm_cmd_title", lang), color=EMBED_COLOR,
                          description="\n".join(lines) if lines else l10n.get("swarm_cmd_none", lang))
        if BOARD_PUBLIC_URL:
            e.add_field(name="🗺️", value=l10n.get("swarm_cmd_link", lang, url=f"{BOARD_PUBLIC_URL}/schwarmflug"),
                        inline=False)
        await ctx.respond(embed=e, ephemeral=True)

    # ── Schwarmflug-Alarm ────────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def alert_loop(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=_ALERT_WINDOW_H)).strftime("%Y-%m-%d %H:%M:%S")
        reps = [dict(r) for r in await execute_db(self.bot,
            f"SELECT * FROM swarm_reports WHERE posted_at>=? AND indoor=0 AND {S.DACH_SQL}", (since,), fetch=True) or []]
        await execute_db(self.bot, "DELETE FROM swarm_alert_sent WHERE message_id NOT LIKE 'start-%' AND "
                         "message_id NOT IN (SELECT message_id FROM swarm_reports WHERE posted_at>=?)",
                         (since,), commit=True)
        await self._season_notices()
        if not reps:
            return
        alerts = [dict(r) for r in await execute_db(self.bot, "SELECT * FROM swarm_alerts", fetch=True) or []]
        if not alerts:
            return
        sent = {(r["user_id"], r["message_id"]) for r in await execute_db(self.bot,
            "SELECT user_id, message_id FROM swarm_alert_sent", fetch=True) or []}
        todo: dict = {}
        for a in alerts:
            if a["taxon"] == "*":
                continue                   # Saisonstart-PN, siehe _season_notices
            parts = a["taxon"].split()
            for r in reps:
                if str(r["author_id"]) == str(a["user_id"]) or (a["user_id"], r["message_id"]) in sent:
                    continue
                if r["genus"] != parts[0] or (len(parts) > 1 and r["species"] != parts[1]):
                    continue
                if a["country"] and (r["country"] != a["country"] or (a["region"] and r["region"] != a["region"])):
                    continue
                todo.setdefault(a["user_id"], {})[r["message_id"]] = r
        for uid, msgs in todo.items():
            if is_quiet(uid):
                continue                   # nach der Ruhezeit (falls noch im Zeitfenster)
            await self._send_alert(uid, list(msgs.values()))

    async def _season_notices(self):
        """PN „Saison eröffnet“ an alle mit Alarm „*“ (Mein Konto), höchstens 3 Tage nach dem Start."""
        start = S.season_now()
        if not start:
            return
        try:
            d0 = datetime.fromisoformat(start["created_at"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return
        if datetime.now(timezone.utc) - d0 > timedelta(days=3):
            return
        key = f"start-{start['year']}"
        subs = await execute_db(self.bot, "SELECT DISTINCT user_id FROM swarm_alerts WHERE taxon='*' AND user_id NOT IN "
                                          "(SELECT user_id FROM swarm_alert_sent WHERE message_id=?)", (key,), fetch=True) or []
        ch = self.bot.get_channel(SWARM_CHANNEL_ID)
        gid = getattr(getattr(ch, "guild", None), "id", None)
        for r in subs:
            uid = r["user_id"]
            if is_quiet(uid) or str(uid) == str(start["author_id"]):
                continue                   # Ruhezeit: beim nächsten Lauf erneut
            await execute_db(self.bot, "INSERT OR IGNORE INTO swarm_alert_sent (user_id, message_id) VALUES (?,?)",
                             (uid, key), commit=True)
            try:
                user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                lang = await get_user_lang(self.bot, uid, None)
                where = S.region_name(start["country"], start["region"]) if start["region"] else "?"
                fd = start["flight_date"]
                e = discord.Embed(title=l10n.get("swarm_start_title", lang, y=start["year"]), color=EMBED_COLOR,
                                  description=l10n.get("swarm_start_body", lang, label=start["label"], where=where,
                                                       d=f"{fd[8:10]}.{fd[5:7]}."))
                if gid:
                    e.add_field(name="Discord", value=f"https://discord.com/channels/{gid}/{SWARM_CHANNEL_ID}/{start['message_id']}")
                if BOARD_PUBLIC_URL:
                    e.set_footer(text=l10n.get("swarm_alert_footer", lang, url=f"{BOARD_PUBLIC_URL}/konto#schwarmflug"))
                await user.send(embed=e)
            except Exception:
                pass

    # ── Wetter zur Flugzeit (Open-Meteo) ─────────────────────────────────────
    @tasks.loop(hours=1)
    async def weather_loop(self):
        from utils import weather
        try:
            await weather.backfill(self.bot)
        except Exception as e:
            logger.warning("🌦️ Wetter-Nachtrag fehlgeschlagen: %s", e)

    @weather_loop.before_loop
    async def _before_weather(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(align_delay_seconds(60 * 60) + 7 * 60)

    # ── Prognose ─────────────────────────────────────────────────────────────
    @tasks.loop(minutes=10)
    async def forecast_loop(self):
        from utils import forecast as FC
        now = datetime.now(BERLIN)
        day = now.date().isoformat()
        try:
            if not await FC.points(self.bot) or (now.weekday() == 0 and now.hour == 4 and now.minute < 10):
                await FC.build_points(self.bot)
            if not await FC.models(self.bot):
                await FC.train_models(self.bot)                   # zuerst Regeln, Modelle folgen
            in_season = now.month in FC.SEASON
            # Lernen: außerhalb der Saison jederzeit, in der Saison nur nachts
            if not in_season or now.hour < 5 or now.hour >= 22:
                got = await FC.train_fetch(self.bot, max_requests=60)
                if got and await FC.train_pending(self.bot) == 0:
                    await FC.train_models(self.bot)
            if now.weekday() == 0 and now.hour == 3 and now.minute < 10:
                await FC.train_models(self.bot)                   # wöchentlich mit neuen Meldungen
            if not in_season:
                return
            slot = next((h for h in FC.RUN_SLOTS if h == now.hour), None)
            if slot is not None:
                done = await execute_db(self.bot, "SELECT 1 FROM fc_runs WHERE day=? AND slot=?", (day, slot), fetch=True)
                if not done:
                    await execute_db(self.bot, "INSERT OR IGNORE INTO fc_runs (day, slot) VALUES (?,?)", (day, slot),
                                     commit=True)
                    res = await FC.run_forecast(self.bot)
                    logger.info("🔮 Prognose-Lauf %s Uhr: %s", slot, res)
            if 18 <= now.hour < 21:
                last = await execute_db(self.bot, "SELECT 1 FROM fc_runs WHERE day=? AND slot=18", (day,), fetch=True)
                if last:
                    tomorrow = (now.date() + timedelta(days=1)).isoformat()
                    await self._forecast_dms(tomorrow)
                    if SWARM_FORECAST_POST:
                        await self._forecast_post(tomorrow)
        except Exception as e:
            logger.warning("🔮 Prognose fehlgeschlagen: %s", e, exc_info=True)

    @forecast_loop.before_loop
    async def _before_fc(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(align_delay_seconds(10 * 60) + 90)

    def _fc_lines(self, items: list, lang: str, show_level: bool = True) -> list[str]:
        out = []
        for e in items:
            extra = []
            if e["h_from"] is not None:
                extra.append(l10n.get("fc_hours", lang, a=e["h_from"], b=e["h_to"]))
            if e["temp"] is not None:
                extra.append(l10n.get("fc_temp", lang, t=e["temp"]))
            lvl = (" " + l10n.get(f"fc_level_{e['level']}", lang)) if show_level else ""
            out.append(f"• **{e['taxon']}**{lvl}: {', '.join(e['regions'])}" + (f" ({', '.join(extra)})" if extra else ""))
        return out

    async def _forecast_dms(self, day: str):
        """Abend-PN für morgen an alle mit Schwarmflug-Alarm, ab der Stufe aus Mein Konto."""
        from utils import forecast as FC
        alerts = [dict(r) for r in await execute_db(self.bot, "SELECT * FROM swarm_alerts WHERE taxon<>'*'",
                                                    fetch=True) or []]
        if not alerts:
            return
        prefs = {r["user_id"]: r["level"] for r in await execute_db(self.bot, "SELECT * FROM fc_pref", fetch=True) or []}
        sent = {r["user_id"] for r in await execute_db(self.bot, "SELECT user_id FROM fc_sent WHERE day=?", (day,),
                                                       fetch=True) or []}
        rows = await FC.by_region(self.bot, day, 1)
        by_user: dict = {}
        for a in alerts:
            by_user.setdefault(a["user_id"], []).append(a)
        for uid, al in by_user.items():
            pref = prefs.get(uid, "rare")
            if pref == "off" or uid in sent or is_quiet(uid):
                continue
            need = FC.LEVEL_OF_PREF.get(pref, 3)
            hits = [r for r in rows if r["level"] >= need and any(
                FC.taxon_matches(a["taxon"], r["taxon"]) and (not a["country"] or (
                    a["country"] == r["country"] and (not a["region"] or a["region"] == r["region"]))) for a in al)]
            if not hits:
                continue
            await execute_db(self.bot, "INSERT OR IGNORE INTO fc_sent (user_id, day) VALUES (?,?)", (uid, day), commit=True)
            try:
                user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                lang = await get_user_lang(self.bot, uid, None)
                d = date.fromisoformat(day)
                e = discord.Embed(title=l10n.get("fc_dm_title", lang, d=d.strftime("%d.%m.")), color=EMBED_COLOR,
                                  description=l10n.get("fc_dm_intro", lang) + "\n" +
                                  "\n".join(self._fc_lines(FC.condense(hits, 8, 4), lang)) + "\n\n" +
                                  l10n.get("fc_disclaimer", lang))
                if BOARD_PUBLIC_URL:
                    e.set_footer(text=l10n.get("fc_dm_footer", lang, url=f"{BOARD_PUBLIC_URL}/konto#schwarmflug"))
                await user.send(embed=e)
            except Exception:
                pass

    async def _forecast_post(self, day: str):
        """Ein Post am Vorabend, nur bei sehr guten Chancen (Stufe 3, nur gelernte Modelle)."""
        from utils import forecast as FC
        if await execute_db(self.bot, "SELECT 1 FROM fc_posts WHERE day=?", (day,), fetch=True):
            return
        ch = self.bot.get_channel(SWARM_CHANNEL_ID)
        if ch is None:
            return
        items = FC.condense(await FC.by_region(self.bot, day, 3), 6, 5)
        await execute_db(self.bot, "INSERT OR IGNORE INTO fc_posts (day, message_id) VALUES (?,?)",
                         (day, None), commit=True)
        if not items:
            return
        lang = await get_guild_lang(self.bot, ch.guild.id) if getattr(ch, "guild", None) else "de"
        d = date.fromisoformat(day)
        e = discord.Embed(title=l10n.get("fc_post_title", lang, d=d.strftime("%d.%m.")), color=EMBED_COLOR,
                          description=l10n.get("fc_post_intro", lang) + "\n" + "\n".join(self._fc_lines(items, lang, show_level=False)) +
                          "\n\n" + l10n.get("fc_post_outro", lang))
        e.set_footer(text=l10n.get("fc_post_footer", lang) + (f" · {BOARD_PUBLIC_URL}/schwarmflug" if BOARD_PUBLIC_URL else ""))
        try:
            msg = await ch.send(embed=e)
            await execute_db(self.bot, "UPDATE fc_posts SET message_id=? WHERE day=?", (str(msg.id), day), commit=True)
        except discord.HTTPException as ex:
            logger.warning("🔮 Prognose-Post fehlgeschlagen: %s", ex)

    @discord.slash_command(name="swarm_season_reset",
                           description="🔒 [Admin] Show the nuptial flight season start and discard a wrong one",
                           description_localizations={"de": "🔒 [Admin] Saisonstart anzeigen und einen falschen verwerfen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def swarm_season_reset(self, ctx: discord.ApplicationContext,
                                 verwerfen: discord.Option(bool, "Discard the current season start",
                                                           description_localizations={"de": "Aktuellen Saisonstart verwerfen"},
                                                           default=False) = False):
        verwerfen = verwerfen if isinstance(verwerfen, bool) else False
        await ctx.defer(ephemeral=True)
        y = datetime.now(BERLIN).year
        cur = S.SEASON.get(y)
        lines = []
        if cur:
            lines.append(f"Saisonstart {y}: **{cur['label']}** am {cur['flight_date']} "
                         f"({S.region_name(cur['country'], cur['region'])}), Nachricht {cur['message_id']}")
        else:
            lines.append(f"Für {y} gibt es noch keinen bestätigten Saisonstart.")
        if cur and not verwerfen:
            lines.append("Mit `verwerfen:true` wird er verworfen; die Meldung zählt dann nicht mehr. "
                         "Direkt über `/swarm_season_reset verwerfen:true` (über /wartung nur anzeigen).")
        if cur and verwerfen:
            await S.season_reset(self.bot, y)
            new = await S.season_check(self.bot)
            lines.append("✅ Verworfen. " + (f"Neuer Saisonstart: **{new['label']}** am {new['flight_date']}."
                                            if new else "Es gibt gerade keinen anderen bestätigten Kandidaten."))
        await ctx.followup.send(embed=discord.Embed(title="🎉 Schwarmflug-Saisonstart", color=ADMIN_COLOR,
                                                    description="\n".join(lines)), ephemeral=True)

    @discord.slash_command(name="swarm_forecast",
                           description="🔒 [Admin] Run the nuptial flight forecast now and relearn the models",
                           description_localizations={"de": "🔒 [Admin] Schwarmflug-Prognose jetzt rechnen und Modelle neu lernen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def swarm_forecast(self, ctx: discord.ApplicationContext):
        from utils import forecast as FC
        await ctx.defer(ephemeral=True)
        t0 = time.monotonic()
        pts = await FC.build_points(self.bot)
        got = await FC.train_fetch(self.bot, max_requests=40)
        mods = await FC.train_models(self.bot)
        res = await FC.run_forecast(self.bot)
        pend = await FC.train_pending(self.bot)
        bud = await FC.budget_today(self.bot)
        e = discord.Embed(title="🔮 Schwarmflug-Prognose", color=ADMIN_COLOR, description="\n".join([
            f"Punkte: {pts['grid']} Gitter, {pts['hot']} Hotspots ({pts['train']} Lernpunkte)",
            f"Lerndaten: {got} Punkt-Jahre geladen, {pend} offen",
            f"Modelle: {mods['model']} gelernt, {mods['rule']} Regeln",
            f"Vorhersage: {res.get('pred', 0)} Einträge" + (f" ({res['skipped']})" if res.get("skipped") else ""),
            f"Open-Meteo heute: " + (", ".join(f"{k} {v:g}" for k, v in bud.items()) or "0"),
            f"Dauer: {time.monotonic() - t0:.0f} s"]))
        await ctx.followup.send(embed=e, ephemeral=True)

    # ── /schwarmflug_vorlage ─────────────────────────────────────────────────
    @discord.slash_command(name="schwarmflug_vorlage",
                           description="Fill in a nuptial flight report using the channel template and copy it",
                           description_localizations={"de": "Schwarmflug-Meldung nach Vorlage ausfüllen und Text kopieren"})
    @allowed_channel()
    async def schwarmflug_vorlage(
        self, ctx: discord.ApplicationContext,
        art: discord.Option(str, "Species or genus (suggestions as you type)",
                            description_localizations={"de": "Art oder Gattung (Vorschläge beim Tippen)"},
                            autocomplete=_ac_taxon_ref, max_length=80),
        region: discord.Option(str, "Federal state / canton (region role)",
                               description_localizations={"de": "Bundesland / Kanton (Regionsrolle)"},
                               autocomplete=_ac_region_ref, required=False, max_length=60, default=""),
        ort: discord.Option(str, "Place (as exact as you like, on the board only members see it)",
                            description_localizations={"de": "Ort (so genau du willst, auf dem Board nur für Mitglieder)"},
                            required=False, max_length=120, default=""),
        uhrzeit: discord.Option(str, "Date and time (empty = now)",
                                description_localizations={"de": "Datum und Uhrzeit (leer = jetzt)"},
                                required=False, max_length=40, default=""),
        temperatur: discord.Option(str, "Temperature, e.g. 26", description_localizations={"de": "Temperatur, z. B. 26"},
                                   required=False, max_length=20, default=""),
        wind: discord.Option(str, "Wind", description_localizations={"de": "Wind"},
                             required=False, max_length=60, default=""),
        wetter: discord.Option(str, "Weather", description_localizations={"de": "Wetter"},
                               required=False, max_length=80, default=""),
        geschuetzt: discord.Option(str, "Specially/strictly protected?",
                                   description_localizations={"de": "Besonders/streng geschützt?"},
                                   choices=["ja", "nein"], required=False, default=""),
        zusatz: discord.Option(str, "Additional information", description_localizations={"de": "Zusatzinformationen"},
                               required=False, max_length=300, default=""),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        when = uhrzeit.strip() or datetime.now(BERLIN).strftime("%d.%m.%y %H:%M")
        place = ort.strip()
        role = None
        if region:
            hit = S.region_from_name(region)
            guild = ctx.guild or getattr(self.bot.get_channel(SWARM_CHANNEL_ID), "guild", None)
            if hit and guild:
                nm = S.region_name(*hit).lower()
                role = next((r for r in guild.roles if r.name.lower().lstrip("@ ") == nm), None)
            reg_txt = role.mention if role else (S.region_name(*hit) if hit else region.strip())
            place = f"{place} {reg_txt}".strip()
        temp = temperatur.strip()
        if temp and re.fullmatch(r"-?\d+(?:[.,]\d+)?", temp):
            temp += " °C"
        data = {"taxa": art.strip(), "protected": geschuetzt, "when": when, "temp": temp, "wind": wind,
                "place": place, "weather": wetter, "extra": zusatz}
        text = S.build_template(data).replace("```", "\x27\x27\x27")
        where = f"<#{SWARM_CHANNEL_ID}>" if SWARM_CHANNEL_ID else "#schwarmflug-meldungen"
        msg = l10n.get("swarm_tpl_copy", lang, channel=where) + f"\n```\n{text}\n```"
        if role:
            msg += "\n" + l10n.get("swarm_tpl_role", lang)
        await ctx.respond(msg[:2000], ephemeral=True)

    async def _send_alert(self, uid, reps):
        for r in reps:
            await execute_db(self.bot, "INSERT OR IGNORE INTO swarm_alert_sent (user_id, message_id) VALUES (?,?)",
                             (uid, r["message_id"]), commit=True)
        try:
            user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
        except Exception:
            return
        lang = await get_user_lang(self.bot, uid, None)
        ch = self.bot.get_channel(SWARM_CHANNEL_ID)
        gid = getattr(getattr(ch, "guild", None), "id", None)
        lines = []
        for r in reps[:10]:
            where = S.region_name(r["country"], r["region"]) if r["region"] else ""
            when = (r["flight_time"] or "") + (" Uhr" if r["flight_time"] and lang == "de" else "")
            link = f"https://discord.com/channels/{gid}/{SWARM_CHANNEL_ID}/{r['message_id']}" if gid else ""
            temp = f", {r['temp_c']:g} °C" if r["temp_c"] is not None else ""
            lines.append(f"• **{r['label']}** {where} {when}{temp} {link}".replace("  ", " "))
        e = discord.Embed(title=l10n.get("swarm_alert_title", lang), color=EMBED_COLOR,
                          description=l10n.get("swarm_alert_intro", lang) + "\n" + "\n".join(lines))
        if BOARD_PUBLIC_URL:
            e.set_footer(text=l10n.get("swarm_alert_footer", lang, url=f"{BOARD_PUBLIC_URL}/konto#schwarmflug"))
        try:
            await user.send(embed=e)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @alert_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(align_delay_seconds(5 * 60))


def setup(bot):
    bot.add_cog(Swarm(bot))
