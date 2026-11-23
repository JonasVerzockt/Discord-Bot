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
cogs/map.py – Halter-Karte: Slash-Befehle + Events.

Datenschutz: Opt-in, grob/gefuzzt, Discord-Name nie gespeichert (nur optional Vorname),
U18 ohne Einzel-Sichtbarkeit (nur anonyme Zählung), Löschung bei Server-Austritt
(siehe cogs/tasks.py), jährliche Bestätigung. Siehe dev-notes/map_feature_concept.md.

Aufteilung:
  cogs/map.py           – dieses Cog: Slash-Befehle, Button-Auswertung (on_interaction)
  utils/map_views.py    – Discord-Oberfläche (Menüs, Buttons, Formulare)            [5.1.0]
  utils/map_actions.py  – Abläufe: speichern, Events einreichen/freigeben/ankündigen [5.1.0]
  utils/map_store.py    – Datenzugriff (Einträge, Regionen, Zusagen, Events)        [5.0.0]
  utils/map_events.py   – Termin-Logik (RRULE, nächstes Vorkommen) – auch für die Web-Karte
"""

import asyncio
import logging
from datetime import datetime
import discord
from discord.ext import commands
from config import MAP_ENABLED
from utils.db import execute_db
from utils.localization import get_user_lang, l10n
from utils import geo, map_geodata
from utils.map_events import _rrule_from
from utils.map_store import _delete_entry, _event_row
from cogs.server_settings import admin_or_manage_messages, allowed_channel

from utils.map_actions import _combine_end, _EVENT_TYPES, _RECUR, _set_event_status
from utils.map_views import (
    _owner_only, _refresh_admin_message, EventAddModal, EventQuickEditModal,
    EventRejectModal, MapConfirmView, MapJoinView, TagView,
)

logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)


class MapCog(commands.Cog, name="Map"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # Persistente View registrieren, damit die Jahres-PN-Buttons nach Neustart wirken.
        try:
            bot.add_view(MapConfirmView())
        except Exception as e:
            logger.debug("MapConfirmView add_view: %s", e)
        # PLZ-Datensatz beim Start vorladen (danach im Speicher, kein Datei-Zugriff pro Abfrage).
        try:
            geo.load()
            logger.info("🗺️ Map-Cog geladen · PLZ-Datensatz: %d Einträge%s",
                        geo.count(), "" if geo.count() else " (Leitziffer-Fallback aktiv)")
        except Exception as e:
            logger.debug("geo.load beim Start: %s", e)

    # ── Buttons der Betreiber-PN (persistente custom_ids "mapev:<aktion>:<id>") ──
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.custom_id or ""
        if not cid.startswith("mapev:"):
            return
        try:
            _, action, eid = cid.split(":", 2)
            eid = int(eid)
        except ValueError:
            return
        if not await _owner_only(interaction):
            return
        if action == "approve":
            row = await _set_event_status(self.bot, eid, "approved", actor_id=interaction.user.id)
            await _refresh_admin_message(interaction, eid, row is None)
        elif action == "reject":
            await interaction.response.send_modal(EventRejectModal(eid))
        elif action == "edit":
            row = await _event_row(self.bot, eid)
            if not row:
                await interaction.response.send_message("❌ Event nicht gefunden.", ephemeral=True)
                return
            await interaction.response.send_modal(EventQuickEditModal(row))

    # ── /map_join (geführt) ──────────────────────────────────────────────────
    @discord.slash_command(name="map_join",
        description="Join the keeper map (voluntary, coarse location, GDPR compliant)",
        description_localizations={"de": "Auf der Halter-Karte eintragen (freiwillig, grob, DSGVO-konform)"})
    @allowed_channel()
    async def map_join(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        uid = str(ctx.author.id)
        rows = await execute_db(self.bot, "SELECT * FROM map_entries WHERE user_id=?", (uid,), fetch=True)
        existing = dict(rows[0]) if rows else None
        tags = []
        if existing:
            cur = await execute_db(self.bot, "SELECT tag_code FROM map_entry_tags WHERE user_id=?",
                                   (uid,), fetch=True) or []
            tags = [r["tag_code"] for r in cur]
        intro = l10n.get("map_wiz_intro", lang)
        if existing:
            intro = l10n.get("map_wiz_existing", lang) + "\n\n" + intro
        await ctx.respond(intro[:2000], view=MapJoinView(lang, existing, tags), ephemeral=True)

    # ── /map_remove ──────────────────────────────────────────────────────────
    @discord.slash_command(name="map_remove", description="Delete your own map entry",
        description_localizations={"de": "Eigenen Karteneintrag löschen"})
    @allowed_channel()
    async def map_remove(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(self.bot, "SELECT 1 FROM map_entries WHERE user_id=?",
                                (str(ctx.author.id),), fetch=True)
        if not rows:
            await ctx.followup.send(l10n.get("map_remove_none", lang), ephemeral=True); return
        await _delete_entry(self.bot, ctx.author.id)
        await ctx.followup.send(l10n.get("map_removed", lang), ephemeral=True)

    # ── /map_tags ────────────────────────────────────────────────────────────
    @discord.slash_command(name="map_tags", description="Set or change tags for your map entry",
        description_localizations={"de": "Tags für den Karteneintrag setzen/ändern"})
    @allowed_channel()
    async def map_tags_cmd(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(self.bot, "SELECT 1 FROM map_entries WHERE user_id=?",
                                (str(ctx.author.id),), fetch=True)
        if not rows:
            await ctx.followup.send(l10n.get("map_tags_none_first", lang), ephemeral=True); return
        cur = await execute_db(self.bot, "SELECT tag_code FROM map_entry_tags WHERE user_id=?",
                               (str(ctx.author.id),), fetch=True) or []
        pre = [r["tag_code"] for r in cur]
        await ctx.followup.send(l10n.get("map_tags_prompt", lang),
                                view=TagView(lang, pre), ephemeral=True)

    # ── /map_confirm ─────────────────────────────────────────────────────────
    @discord.slash_command(name="map_confirm", description="Confirm your map entry is still current",
        description_localizations={"de": "Karteneintrag als aktuell bestätigen"})
    @allowed_channel()
    async def map_confirm(
        self, ctx: discord.ApplicationContext,
        age_18: discord.Option(bool, "Ich bin (weiterhin) 18 oder älter", required=True),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        # 18+ wird bei der Bestätigung neu geprüft (z.B. jemand wird inzwischen volljährig,
        # oder umgekehrt) und steuert die Einzel-Sichtbarkeit (show_entry).
        rc = await execute_db(self.bot,
            "UPDATE map_entries SET last_confirmed_at=datetime('now'), reminder_sent_at=NULL, "
            "age_ok=?, show_entry=? WHERE user_id=?",
            (1 if age_18 else 0, 1 if age_18 else 0, str(ctx.author.id)), commit=True)
        if not rc:
            await ctx.followup.send(l10n.get("map_remove_none", lang), ephemeral=True); return
        msg = l10n.get("map_confirm_kept", lang) if age_18 else l10n.get("map_saved_u18", lang)
        await ctx.followup.send(msg + " " + l10n.get("map_remove_hint", lang), ephemeral=True)

    # ── /event_add (geführt; alle dürfen einreichen, Freigabe durch Admin) ───
    @discord.slash_command(name="event_add",
        description="Suggest an event, fair or meetup for the map (approved by admins)",
        description_localizations={"de": "Termin/Messe/Treffen für die Karte vorschlagen (Freigabe durch Admin)"})
    @allowed_channel()
    async def event_add(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ctx.send_modal(EventAddModal(lang))

    # ── Admin: Events freigeben ──────────────────────────────────────────────
    @discord.slash_command(name="event_pending", description="🔒 [Admin] Show pending event suggestions",
        description_localizations={"de": "🔒 [Admin] Offene Event-Vorschläge anzeigen"},
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def event_pending(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        rows = await execute_db(self.bot,
            "SELECT id, title, start_at, venue, country FROM map_events "
            "WHERE status='pending' ORDER BY id", fetch=True) or []
        if not rows:
            await ctx.followup.send("Keine offenen Event-Vorschläge.", ephemeral=True); return
        lines = [f"#{r['id']} · {r['title']} · {r['start_at']} · {r['venue'] or '?'} "
                 f"({r['country'] or '?'})" for r in rows]
        await ctx.followup.send("**Offen:**\n" + "\n".join(lines)[:1900], ephemeral=True)

    @discord.slash_command(name="event_approve", description="🔒 [Admin] Approve an event",
        description_localizations={"de": "🔒 [Admin] Event freigeben"},
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def event_approve(self, ctx: discord.ApplicationContext,
                            event_id: discord.Option(int, "Event-ID", required=True)):
        await ctx.defer(ephemeral=True)
        row = await _set_event_status(self.bot, event_id, "approved", actor_id=ctx.author.id)
        await ctx.followup.send(("✅ Freigegeben." if row else "❌ Nicht gefunden/bereits bearbeitet."),
                                ephemeral=True)

    @discord.slash_command(name="event_reject", description="🔒 [Admin] Reject an event",
        description_localizations={"de": "🔒 [Admin] Event ablehnen"},
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def event_reject(self, ctx: discord.ApplicationContext,
                           event_id: discord.Option(int, "Event-ID", required=True),
                           reason: discord.Option(str, "Grund (optional, geht an die einreichende Person)",
                                                  required=False, default=None)):
        await ctx.defer(ephemeral=True)
        row = await _set_event_status(self.bot, event_id, "rejected", (reason or "").strip() or None,
                                      actor_id=ctx.author.id)
        await ctx.followup.send(("🗑 Abgelehnt." if row else "❌ Nicht gefunden/bereits bearbeitet."),
                                ephemeral=True)

    @discord.slash_command(name="event_exclude",
        description="🔒 [Admin] Cancel a single date of a recurring event",
        description_localizations={"de": "🔒 [Admin] Einzelnen Serien-Termin absagen (EXDATE)"},
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def event_exclude(self, ctx: discord.ApplicationContext,
                            event_id: discord.Option(int, "Event-ID", required=True),
                            date: discord.Option(str, "Ausfall-Datum JJJJ-MM-TT", required=True)):
        """Trägt für eine Serie einen ausgefallenen Termin ein. Das Datum wird mit der
        Startzeit der Serie kombiniert, damit es genau das Vorkommen trifft."""
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(self.bot,
            "SELECT start_at, rrule, exdates FROM map_events WHERE id=?",
            (event_id,), fetch=True)
        if not rows:
            await ctx.followup.send("❌ Nicht gefunden.", ephemeral=True); return
        ev = rows[0]
        if not ev["rrule"]:
            await ctx.followup.send("❌ Das ist keine Serie (kein Wiederholungsmuster).", ephemeral=True); return
        d = None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                d = datetime.strptime(date.strip(), fmt); break
            except ValueError:
                continue
        if d is None:
            await ctx.followup.send(l10n.get("event_bad_date", lang), ephemeral=True); return
        # Startzeit der Serie übernehmen, damit der EXDATE genau ein Vorkommen trifft.
        try:
            st = datetime.fromisoformat(ev["start_at"].replace("T", " "))
            d = d.replace(hour=st.hour, minute=st.minute, second=st.second)
        except Exception:
            pass
        iso = d.strftime("%Y-%m-%dT%H:%M:%S")
        existing = [x.strip() for x in (ev["exdates"] or "").split(",") if x.strip()]
        if iso not in existing:
            existing.append(iso)
        await execute_db(self.bot,
            "UPDATE map_events SET exdates=? WHERE id=?",
            (",".join(existing), event_id), commit=True)
        await ctx.followup.send(f"✅ Termin {iso[:10]} für Event #{event_id} abgesagt (EXDATE).",
                                ephemeral=True)


    @discord.slash_command(name="event_edit",
        description="🔒 [Admin] Edit a submitted or active event (only given fields)",
        description_localizations={"de": "🔒 [Admin] Eingereichtes/aktives Event anpassen (nur angegebene Felder)"},
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def event_edit(
        self, ctx: discord.ApplicationContext,
        event_id: discord.Option(int, "Event-ID", required=True),
        title: discord.Option(str, "Titel", required=False, default=None),
        date: discord.Option(str, "Datum JJJJ-MM-TT", required=False, default=None),
        time: discord.Option(str, "Uhrzeit HH:MM ('-' = ganztägig)", required=False, default=None),
        end_date: discord.Option(str, "Enddatum ('-' zum Löschen)", required=False, default=None),
        end_time: discord.Option(str, "Endzeit HH:MM ('-' zum Löschen)", required=False, default=None),
        location: discord.Option(str, "Ort/Veranstaltungsort", required=False, default=None),
        country: discord.Option(str, "Land", required=False, default=None),
        plz: discord.Option(str, "PLZ (für Kartenpunkt bei DACH)", required=False, default=None),
        url: discord.Option(str, "Link ('-' zum Löschen)", required=False, default=None),
        etype: discord.Option(str, "Art", choices=_EVENT_TYPES, required=False, default=None),
        recurring: discord.Option(str, "Wiederholung", choices=_RECUR, required=False, default=None),
        description: discord.Option(str, "Beschreibung ('-' zum Löschen)", required=False, default=None),
        status: discord.Option(str, "Status", required=False, default=None,
            choices=[discord.OptionChoice(name="pending", value="pending"),
                     discord.OptionChoice(name="approved", value="approved"),
                     discord.OptionChoice(name="rejected", value="rejected")]),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(self.bot, "SELECT * FROM map_events WHERE id=?",
                                (event_id,), fetch=True)
        if not rows:
            await ctx.followup.send("❌ Event nicht gefunden.", ephemeral=True); return
        ev = rows[0]
        _clear = {"-", "none", "", "kein", "leer"}

        # Startzeitpunkt aus vorhandenem Wert + Overrides neu zusammensetzen.
        try:
            base = datetime.fromisoformat(ev["start_at"].replace("T", " "))
        except Exception:
            base = datetime.now()
        had_time = not ev["all_day"]
        d_str = (date.strip() if date else base.strftime("%Y-%m-%d"))
        if time is not None:
            use_time = time.strip().lower() not in _clear
            t_str = time.strip() if use_time else ""
        else:
            use_time, t_str = had_time, (base.strftime("%H:%M") if had_time else "")
        dstr = d_str + ((" " + t_str) if (use_time and t_str) else "")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(dstr, fmt); break
            except ValueError:
                continue
        if dt is None:
            await ctx.followup.send(l10n.get("event_bad_date", lang), ephemeral=True); return
        all_day = 0 if (use_time and t_str) else 1

        # Ende (Datum + optionale Uhrzeit): vorhandene Werte übernehmen, Angaben überschreiben.
        try:
            old_end = datetime.fromisoformat(ev["end_at"].replace("T", " ")) if ev["end_at"] else None
        except Exception:
            old_end = None
        old_end_has_time = bool(old_end and (old_end.hour or old_end.minute))
        if end_date is not None:
            ed_str = "" if end_date.strip().lower() in _clear else end_date.strip()
        else:
            ed_str = old_end.strftime("%d.%m.%Y") if old_end else ""
        if end_time is not None:
            et_str = "" if end_time.strip().lower() in _clear else end_time.strip()
        else:
            # Alte Endzeit entfällt automatisch, wenn das Event ganztägig wird.
            et_str = old_end.strftime("%H:%M") if (old_end_has_time and not all_day) else ""
        if end_date is not None and not ed_str and end_time is None:
            et_str = ""                       # Enddatum gelöscht -> Ende komplett entfernen
        end_dt, _eht, err = _combine_end(dt, not all_day, ed_str, et_str)
        if err:
            await ctx.followup.send(l10n.get(err, lang), ephemeral=True); return
        end_at = end_dt.strftime("%Y-%m-%dT%H:%M:%S") if end_dt else None

        new_country = ((country.strip().lower() or None) if country is not None else ev["country"])
        new_plz = ((plz.strip()[:12] or None) if plz is not None else ev["plz"])
        # Koordinaten nur neu bestimmen, wenn Land/PLZ angefasst wurden.
        if country is not None or plz is not None:
            lat = lon = None
            if new_country and geo.is_dach(new_country) and new_plz:
                rec = geo.resolve(new_country, new_plz)
                if rec:
                    lat, lon = rec["lat"], rec["lon"]
        else:
            lat, lon = ev["lat"], ev["lon"]

        rrule = _rrule_from(recurring, dt) if recurring is not None else ev["rrule"]
        new_title = title.strip()[:120] if title else ev["title"]
        new_loc = location.strip()[:160] if location else ev["venue"]
        new_type = etype or ev["type"]
        new_status = status or ev["status"]
        new_url = (None if url.strip().lower() in _clear else url.strip()[:300]) if url is not None else ev["url"]
        new_desc = (None if description.strip().lower() in _clear else description.strip()[:1000]) if description is not None else ev["description"]

        await execute_db(self.bot,
            """UPDATE map_events SET title=?, type=?, country=?, lat=?, lon=?, venue=?,
                 plz=?, start_at=?, end_at=?, all_day=?, rrule=?, url=?, description=?,
                 status=? WHERE id=?""",
            (new_title, new_type, new_country, lat, lon, new_loc, new_plz,
             dt.strftime("%Y-%m-%dT%H:%M:%S"), end_at, all_day, rrule, new_url, new_desc,
             new_status, event_id), commit=True)
        await ctx.followup.send(f"✅ Event #{event_id} aktualisiert.", ephemeral=True)
        logger.info("🗺️ event_edit #%s von %s", event_id, ctx.author.id)

    @discord.slash_command(name="map_refresh",
        description="🔒 [Admin] Reload map geodata now (Leaflet/GeoJSON/postcodes)",
        description_localizations={"de": "🔒 [Admin] Karten-Geodaten jetzt neu laden (Leaflet/GeoJSON/PLZ)"},
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def map_refresh(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        lines = await asyncio.to_thread(map_geodata.refresh, True)   # force
        geo.load()
        await ctx.followup.send("🗺️ Geodaten aktualisiert:\n" + "\n".join(lines)[:1900],
                                ephemeral=True)
        logger.info("🗺️ map_refresh von %s: %s", ctx.author.id, " · ".join(lines))


def setup(bot: discord.Bot):
    if not MAP_ENABLED:
        logger.info("🗺️ Map-Cog inaktiv (MAP_ENABLED=false)")
        return
    bot.add_cog(MapCog(bot))
