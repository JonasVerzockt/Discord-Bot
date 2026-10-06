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
"""
import asyncio
import logging
from datetime import datetime

import discord
from discord.ext import commands

from config import MAP_ENABLED, MAP_JITTER_METERS, BOARD_OWNER_ID, BOARD_PUBLIC_URL
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils import geo, map_tags, map_geodata
from cogs.server_settings import admin_or_manage_messages, allowed_channel

logger = logging.getLogger(__name__)

_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

_EVENT_TYPES = [
    discord.OptionChoice(name="Börse/Messe", value="fair"),
    discord.OptionChoice(name="Community-Treffen", value="meetup"),
    discord.OptionChoice(name="Shop-Event", value="shop"),
    discord.OptionChoice(name="Workshop/Vortrag", value="talk"),
    discord.OptionChoice(name="Exkursion/Beobachtung", value="field"),
    discord.OptionChoice(name="Sonstiges", value="other"),
]

_RECUR = [
    discord.OptionChoice(name="einmalig", value="none"),
    discord.OptionChoice(name="wöchentlich", value="weekly"),
    discord.OptionChoice(name="monatlich (Wochentag, z.B. 1. Sonntag)", value="monthly_weekday"),
    discord.OptionChoice(name="monatlich (fester Tag)", value="monthly_day"),
    discord.OptionChoice(name="jährlich", value="yearly"),
]


async def _upsert_entry(bot, uid, country, geo_rec, first_name, show_entry, age_ok,
                        contact_ok=1, show_name=1, coarse=0):
    lat = geo_rec["lat_fuzzed"] if geo_rec else None
    lon = geo_rec["lon_fuzzed"] if geo_rec else None
    rc = geo_rec["region_code"] if geo_rec else None
    rn = geo_rec["region_name"] if geo_rec else None
    pp = geo_rec["plz_prefix"] if geo_rec else None
    await execute_db(bot,
        """INSERT INTO map_entries
             (user_id, country, region_code, region_name, plz_prefix, lat_fuzzed,
              lon_fuzzed, first_name, show_entry, show_name, age_ok, contact_ok, coarse,
              consent_at, last_confirmed_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             country=excluded.country, region_code=excluded.region_code,
             region_name=excluded.region_name, plz_prefix=excluded.plz_prefix,
             lat_fuzzed=excluded.lat_fuzzed, lon_fuzzed=excluded.lon_fuzzed,
             first_name=excluded.first_name, show_entry=excluded.show_entry,
             show_name=excluded.show_name, age_ok=excluded.age_ok,
             contact_ok=excluded.contact_ok, coarse=excluded.coarse,
             last_confirmed_at=datetime('now'),
             updated_at=datetime('now')""",
        (str(uid), country, rc, rn, pp, lat, lon, first_name, show_entry, show_name,
         age_ok, contact_ok, coarse),
        commit=True)


class TagSelect(discord.ui.Select):
    def __init__(self, lang: str, preselected=None):
        preselected = set(preselected or [])
        opts = []
        for g in map_tags.TAG_GROUPS:
            for code, lbl in g["tags"]:
                opts.append(discord.SelectOption(
                    label=lbl.get(lang) or lbl.get("de") or code,
                    value=code, default=(code in preselected)))
        super().__init__(placeholder=l10n.get("map_tags_prompt", lang),
                         min_values=0, max_values=len(opts), options=opts[:25])
        self._lang = lang

    async def callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        bot = interaction.client
        await execute_db(bot, "DELETE FROM map_entry_tags WHERE user_id=?", (uid,), commit=True)
        for code in self.values:
            if map_tags.is_valid(code):
                await execute_db(bot,
                    "INSERT OR IGNORE INTO map_entry_tags (user_id, tag_code) VALUES (?,?)",
                    (uid, code), commit=True)
        if self.values:
            msg = l10n.get("map_tags_saved", self._lang,
                           tags=", ".join(map_tags.labels(self.values, self._lang)))
        else:
            msg = l10n.get("map_tags_cleared", self._lang)
        msg += " " + l10n.get("map_remove_hint", self._lang)
        await interaction.response.edit_message(content=msg, view=None)


class TagView(discord.ui.View):
    def __init__(self, lang: str, preselected=None):
        super().__init__(timeout=300)
        self.add_item(TagSelect(lang, preselected))


class MapConfirmView(discord.ui.View):
    """Persistente View für die jährliche Bestätigungs-PN (übersteht Bot-Neustart)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Bleiben/Aktualisieren", style=discord.ButtonStyle.success,
                       custom_id="map_confirm_stay")
    async def stay(self, button, interaction: discord.Interaction):
        bot = interaction.client
        lang = await get_user_lang(bot, interaction.user.id, None)
        await execute_db(bot,
            "UPDATE map_entries SET last_confirmed_at=datetime('now'), reminder_sent_at=NULL "
            "WHERE user_id=?", (str(interaction.user.id),), commit=True)
        await interaction.response.edit_message(content=l10n.get("map_confirm_kept", lang), view=None)

    @discord.ui.button(label="Löschen", style=discord.ButtonStyle.danger,
                       custom_id="map_confirm_delete")
    async def delete(self, button, interaction: discord.Interaction):
        bot = interaction.client
        lang = await get_user_lang(bot, interaction.user.id, None)
        await _delete_entry(bot, interaction.user.id)
        await interaction.response.edit_message(content=l10n.get("map_confirm_deleted", lang), view=None)


async def _delete_entry(bot, uid):
    uid = str(uid)
    await execute_db(bot, "DELETE FROM map_entry_tags WHERE user_id=?", (uid,), commit=True)
    await execute_db(bot, "DELETE FROM map_entries WHERE user_id=?", (uid,), commit=True)


async def _delete_rsvps(bot, uid):
    """Alle Teilnahme-Angaben einer Person löschen (z. B. beim Server-Austritt)."""
    await execute_db(bot, "DELETE FROM map_event_rsvp WHERE user_id=?", (str(uid),), commit=True)


def _rrule_from(kind: str, dt: datetime) -> str | None:
    if kind == "weekly":
        return "FREQ=WEEKLY"
    if kind == "monthly_day":
        return f"FREQ=MONTHLY;BYMONTHDAY={dt.day}"
    if kind == "monthly_weekday":
        ordn = ((dt.day - 1) // 7) + 1
        return f"FREQ=MONTHLY;BYDAY={ordn}{_WEEKDAYS[dt.weekday()]}"
    if kind == "yearly":
        return "FREQ=YEARLY"
    return None


# ── Geführte Abläufe (interaktiv) ─────────────────────────────────────────────
_COUNTRY_OPTS = [("de", "Deutschland"), ("at", "Österreich"), ("ch", "Schweiz"),
                 ("li", "Liechtenstein"), ("other", None)]   # None -> l10n
_EVENT_TYPE_CODES = ["fair", "meetup", "shop", "talk", "field", "other"]
_RECUR_CODES = ["none", "weekly", "monthly_weekday", "monthly_day", "yearly"]


def _mark_default(select: discord.ui.Select, value: str):
    """Gewählte Option als Default markieren, damit sie nach einem Neu-Rendern sichtbar bleibt."""
    for o in select.options:
        o.default = (o.value == value)


def _parse_date(s: str, with_time: str | None = None):
    s = (s or "").strip()
    t = (with_time or "").strip()
    if t:
        fmts, val = ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"), f"{s} {t}"
    else:
        fmts, val = ("%d.%m.%Y", "%Y-%m-%d"), s
    for fmt in fmts:
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def _parse_hhmm(s: str):
    """'HH:MM' -> (h, m) oder None."""
    try:
        t = datetime.strptime((s or "").strip(), "%H:%M")
        return t.hour, t.minute
    except ValueError:
        return None


def _combine_end(start: datetime, start_has_time: bool, end_date: str, end_time: str):
    """Ende aus optionalem Enddatum + optionaler Endzeit bilden.

    Rückgabe (end_dt | None, end_has_time, fehler_l10n_key | None).
    - Nur Endzeit  -> Ende am Starttag.
    - Nur Enddatum -> Ende ohne Uhrzeit (00:00 gespeichert, gilt als "ganzer letzter Tag").
    - Endzeit 00:00 wird als 23:59 gespeichert (00:00 steht intern für "keine Endzeit").
    """
    end_date, end_time = (end_date or "").strip(), (end_time or "").strip()
    if not end_date and not end_time:
        return None, False, None
    if end_time and not start_has_time:
        return None, False, "event_end_needs_time"
    if end_date:
        ed = _parse_date(end_date)
        if ed is None:
            return None, False, "event_bad_date"
    else:
        ed = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if end_time:
        hm = _parse_hhmm(end_time)
        if hm is None:
            return None, False, "event_bad_date"
        h, m = hm if hm != (0, 0) else (23, 59)
        end_dt = ed.replace(hour=h, minute=m, second=0, microsecond=0)
        if end_dt <= start:
            return None, False, "event_end_before_start"
        return end_dt, True, None
    if ed.date() < start.date():
        return None, False, "event_end_before_start"
    return ed, False, None


def _fmt_when(dt: datetime, has_time: bool, end_dt, end_has_time: bool, until: str) -> str:
    """'12.12.2026 10:00–17:00' · '06.03.2027 10:00 bis 07.03.2027 18:00' · '03.09.2027 bis 04.09.2027'."""
    when = dt.strftime("%d.%m.%Y %H:%M") if has_time else dt.strftime("%d.%m.%Y")
    if not end_dt:
        return when
    if end_has_time and end_dt.date() == dt.date():
        return when + "–" + end_dt.strftime("%H:%M")
    if end_dt.date() == dt.date():
        return when
    return f"{when} {until} " + end_dt.strftime("%d.%m.%Y %H:%M" if end_has_time else "%d.%m.%Y")


async def _save_join(bot, user, lang, cc, plz, first_name, age_18, show_name, contactable,
                     coarse=False):
    """Speichert den Karteneintrag. Gibt (ok, Nachricht) zurück."""
    fn = (first_name or "").strip()[:40] or None
    flag18 = 1 if age_18 else 0
    if geo.is_dach(cc):
        if not plz:
            return False, l10n.get("map_need_plz", lang)
        rec = geo.resolve_coarse(cc, plz) if coarse else geo.resolve(cc, plz)
        if not rec:
            return False, l10n.get("map_unknown_plz", lang, plz=plz.strip()[:10])
        flat, flon = geo.fuzz(rec["lat"], rec["lon"], user.id, MAP_JITTER_METERS)
        georec = {"lat_fuzzed": flat, "lon_fuzzed": flon,
                  "region_code": rec["region_code"], "region_name": rec["region_name"],
                  "plz_prefix": rec["plz_prefix"]}
        await _upsert_entry(bot, user.id, cc, georec, fn, flag18, flag18,
                            contact_ok=1 if contactable else 0, show_name=1 if show_name else 0,
                            coarse=1 if coarse else 0)
        msg = l10n.get("map_saved_pin", lang, region=rec["region_name"]) if age_18 \
            else l10n.get("map_saved_u18", lang)
        if coarse and age_18:
            msg += "\n" + l10n.get("map_saved_coarse", lang, area=rec["area"] + "x" * geo.COARSE_DROP)
    else:
        cc = (cc or "").strip().lower()[:2]
        if len(cc) != 2 or not cc.isalpha():
            return False, l10n.get("map_need_country", lang)
        await _upsert_entry(bot, user.id, cc, None, fn, flag18, flag18,
                            contact_ok=1 if contactable else 0, show_name=1 if show_name else 0)
        msg = l10n.get("map_saved_list", lang, country=cc.upper()) if age_18 \
            else l10n.get("map_saved_u18", lang)
    return True, msg


class MapJoinModal(discord.ui.Modal):
    def __init__(self, wizard: "MapJoinView"):
        lang = wizard.lang
        super().__init__(title=l10n.get("map_wiz_modal_title", lang)[:45])
        self.wizard = wizard
        if geo.is_dach(wizard.country):
            # Auch beim groben PLZ-Gebiet wird die volle PLZ erwartet (Prüfung + Gebietsermittlung);
            # gespeichert werden nur Region, PLZ-Präfix und die gefuzzte Gebietsmitte.
            plz_key = "map_wiz_plz_coarse" if wizard.coarse else "map_wiz_plz"
            # Die PLZ wird nie gespeichert -> kann auch beim Bearbeiten nicht vorausgefüllt werden.
            self.add_item(discord.ui.InputText(label=l10n.get(plz_key, lang)[:45],
                                               required=True, max_length=10))
        else:
            ex_cc = (wizard.existing or {}).get("country") or ""
            self.add_item(discord.ui.InputText(label=l10n.get("map_wiz_cc", lang)[:45],
                                               required=True, min_length=2, max_length=2,
                                               value=ex_cc if (ex_cc and not geo.is_dach(ex_cc)) else None))
        self.add_item(discord.ui.InputText(label=l10n.get("map_wiz_fn", lang)[:45],
                                           required=False, max_length=40,
                                           value=(wizard.existing or {}).get("first_name") or None))

    async def callback(self, interaction: discord.Interaction):
        w = self.wizard
        first = (self.children[0].value or "").strip()
        fn = self.children[1].value
        if geo.is_dach(w.country):
            cc, plz = w.country, first
        else:
            cc, plz = first, None
        ok, msg = await _save_join(interaction.client, interaction.user, w.lang, cc, plz, fn,
                                   w.age_18, w.show_name, w.contactable, w.coarse)
        if not ok:
            # Auswahl bleibt erhalten, User kann erneut auf "Zustimmen und weiter" klicken.
            await interaction.response.send_message(msg, ephemeral=True)
            return
        w.stop()
        logger.info("🗺️ map_join: %s (%s)", interaction.user.id, cc.upper())
        try:
            await interaction.response.edit_message(content=msg, view=TagView(w.lang, w.tags))
        except Exception:
            await interaction.response.send_message(msg, view=TagView(w.lang, w.tags), ephemeral=True)


class MapJoinView(discord.ui.View):
    """Schritt 1 von /map_join: Land, Alter, Namensanzeige, Kontakt + Einwilligung per Button."""
    def __init__(self, lang: str, existing: dict | None = None, tags=None):
        super().__init__(timeout=900)
        self.lang = lang
        self.existing = existing      # vorhandener Eintrag -> Auswahl vorausfüllen
        self.tags = list(tags or [])  # vorhandene Tags -> in der Tag-Auswahl vorausgewählt
        self.country = None
        self.age_18 = None
        self.show_name = True         # Standard: Name anzeigen (falls Vorname angegeben)
        self.contactable = False      # Standard: NICHT kontaktierbar (nur bei aktivem Ja)
        self.coarse = False           # Standard: genaue PLZ (gefuzzt); optional grobes PLZ-Gebiet
        if existing:
            cc = (existing.get("country") or "").lower()
            self.country = cc if geo.is_dach(cc) else ("other" if cc else None)
            self.age_18 = bool(existing.get("age_ok"))
            self.show_name = bool(existing.get("show_name", 1))
            self.contactable = bool(existing.get("contact_ok"))
            self.coarse = bool(existing.get("coarse"))

        c = discord.ui.Select(placeholder=l10n.get("map_wiz_country_ph", lang), row=0,
            options=[discord.SelectOption(label=(n or l10n.get("map_wiz_other", lang)), value=v,
                                          emoji={"de": "🇩🇪", "at": "🇦🇹", "ch": "🇨🇭",
                                                 "li": "🇱🇮"}.get(v, "🌍"))
                     for v, n in _COUNTRY_OPTS])
        a = discord.ui.Select(placeholder=l10n.get("map_wiz_age_ph", lang), row=1, options=[
            discord.SelectOption(label=l10n.get("map_wiz_age_18", lang), value="18"),
            discord.SelectOption(label=l10n.get("map_wiz_age_u18", lang), value="u18")])
        # Optionen als Mehrfachauswahl: nichts gewählt = Standard (Name zeigen, kein Kontakt, genaue PLZ).
        o = discord.ui.Select(placeholder=l10n.get("map_wiz_opts_ph", lang), row=2,
                              min_values=0, max_values=3, options=[
            discord.SelectOption(label=l10n.get("map_wiz_name_anon", lang), value="anon", emoji="🙈"),
            discord.SelectOption(label=l10n.get("map_wiz_contact_yes", lang), value="contact", emoji="💬"),
            discord.SelectOption(label=l10n.get("map_wiz_coarse", lang), value="coarse", emoji="🎯",
                                 description=l10n.get("map_wiz_coarse_desc", lang)[:100])])

        async def on_country(interaction):
            self.country = c.values[0]; _mark_default(c, self.country)
            await interaction.response.defer()

        async def on_age(interaction):
            self.age_18 = (a.values[0] == "18"); _mark_default(a, a.values[0])
            await interaction.response.defer()

        async def on_opts(interaction):
            vals = set(o.values or [])
            self.show_name = "anon" not in vals
            self.contactable = "contact" in vals
            self.coarse = "coarse" in vals
            for opt in o.options:
                opt.default = opt.value in vals
            await interaction.response.defer()

        # Vorhandenen Eintrag als Vorauswahl markieren.
        if self.country:
            _mark_default(c, self.country)
        if self.age_18 is not None:
            _mark_default(a, "18" if self.age_18 else "u18")
        for opt in o.options:
            opt.default = ((opt.value == "anon" and not self.show_name) or
                           (opt.value == "contact" and self.contactable) or
                           (opt.value == "coarse" and self.coarse))

        c.callback, a.callback, o.callback = on_country, on_age, on_opts
        for item in (c, a, o):
            self.add_item(item)

        go = discord.ui.Button(label=l10n.get("map_wiz_btn_next", lang),
                               style=discord.ButtonStyle.success, row=3)
        stop = discord.ui.Button(label=l10n.get("map_wiz_btn_cancel", lang),
                                 style=discord.ButtonStyle.secondary, row=3)

        async def on_go(interaction):
            if not self.country or self.age_18 is None:
                await interaction.response.send_message(l10n.get("map_wiz_need_choice", self.lang),
                                                        ephemeral=True)
                return
            await interaction.response.send_modal(MapJoinModal(self))

        async def on_stop(interaction):
            self.stop()
            await interaction.response.edit_message(content=l10n.get("map_wiz_cancelled", self.lang),
                                                    view=None)

        go.callback, stop.callback = on_go, on_stop
        self.add_item(go)
        self.add_item(stop)


class EventAddModal(discord.ui.Modal):
    """Schritt 1 von /event_add: Pflichtfelder + Uhrzeit/Beschreibung."""
    def __init__(self, lang: str):
        super().__init__(title=l10n.get("event_wiz_modal_title", lang)[:45])
        self.lang = lang
        L = lambda k: l10n.get(k, lang)[:45]
        self.add_item(discord.ui.InputText(label=L("event_wiz_title"), required=True, max_length=120))
        self.add_item(discord.ui.InputText(label=L("event_wiz_date"), required=True, max_length=10,
                                           placeholder="24.05.2027"))
        self.add_item(discord.ui.InputText(label=L("event_wiz_time"), required=False, max_length=5,
                                           placeholder="10:00"))
        self.add_item(discord.ui.InputText(label=L("event_wiz_location"), required=True, max_length=160))
        self.add_item(discord.ui.InputText(label=L("event_wiz_desc"), required=False, max_length=1000,
                                           style=discord.InputTextStyle.long))

    async def callback(self, interaction: discord.Interaction):
        title, date, time, loc, desc = (ch.value or "" for ch in self.children)
        title, loc, time = title.strip(), loc.strip(), time.strip()
        if not (title and date.strip() and loc):
            await interaction.response.send_message(l10n.get("event_need_fields", self.lang), ephemeral=True)
            return
        dt = _parse_date(date, time or None)
        if dt is None:
            await interaction.response.send_message(l10n.get("event_bad_date", self.lang), ephemeral=True)
            return
        view = EventAddView(self.lang, {"title": title, "dt": dt, "has_time": bool(time),
                                        "location": loc, "description": desc.strip() or None})
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class EventMoreModal(discord.ui.Modal):
    """Optionale Zusatzangaben: PLZ, Enddatum, Endzeit, Link."""
    def __init__(self, wizard: "EventAddView"):
        lang = wizard.lang
        super().__init__(title=l10n.get("event_wiz_more_title", lang)[:45])
        self.wizard = wizard
        d = wizard.data
        L = lambda k: l10n.get(k, lang)[:45]
        self.add_item(discord.ui.InputText(label=L("event_wiz_plz"), required=False, max_length=10,
                                           value=d.get("plz") or None))
        self.add_item(discord.ui.InputText(label=L("event_wiz_end"), required=False, max_length=10,
                                           placeholder="25.05.2027",
                                           value=d["end_dt"].strftime("%d.%m.%Y") if d.get("end_dt") else None))
        self.add_item(discord.ui.InputText(label=L("event_wiz_end_time"), required=False, max_length=5,
                                           placeholder="18:00",
                                           value=d["end_dt"].strftime("%H:%M") if d.get("end_has_time") else None))
        self.add_item(discord.ui.InputText(label=L("event_wiz_url"), required=False, max_length=300,
                                           value=d.get("url") or None))

    async def callback(self, interaction: discord.Interaction):
        w = self.wizard
        plz, end, end_time, url = ((ch.value or "").strip() for ch in self.children)
        end_dt, end_has_time, err = _combine_end(w.data["dt"], w.data["has_time"], end, end_time)
        if err:
            await interaction.response.send_message(l10n.get(err, w.lang), ephemeral=True)
            return
        if url and not url.lower().startswith(("https://", "http://")):
            await interaction.response.send_message(l10n.get("event_bad_url", w.lang), ephemeral=True)
            return
        w.data.update({"plz": plz or None, "end_dt": end_dt, "end_has_time": end_has_time,
                       "url": url or None})
        try:
            await interaction.response.edit_message(content=w.summary(), view=w)
        except Exception:
            await interaction.response.send_message(w.summary(), ephemeral=True)


class EventAddView(discord.ui.View):
    """Schritt 2 von /event_add: Art, Wiederholung, Land, Zusatzangaben, Einreichen."""
    def __init__(self, lang: str, data: dict):
        super().__init__(timeout=900)
        self.lang = lang
        self.data = data
        data.setdefault("etype", "other")
        data.setdefault("recurring", "none")
        data.setdefault("country", None)

        t = discord.ui.Select(placeholder=l10n.get("event_wiz_type_ph", lang), row=0, options=[
            discord.SelectOption(label=l10n.get(f"event_type_{c}", lang), value=c)
            for c in _EVENT_TYPE_CODES])
        r = discord.ui.Select(placeholder=l10n.get("event_wiz_recur_ph", lang), row=1, options=[
            discord.SelectOption(label=l10n.get(f"event_recur_{c}", lang), value=c)
            for c in _RECUR_CODES])
        c = discord.ui.Select(placeholder=l10n.get("event_wiz_country_ph", lang), row=2, options=[
            discord.SelectOption(label=(n or l10n.get("event_wiz_country_none", lang)),
                                 value=(v if v != "other" else "none"))
            for v, n in _COUNTRY_OPTS])

        async def on_type(interaction):
            data["etype"] = t.values[0]; _mark_default(t, t.values[0])
            await interaction.response.edit_message(content=self.summary(), view=self)

        async def on_recur(interaction):
            data["recurring"] = r.values[0]; _mark_default(r, r.values[0])
            await interaction.response.edit_message(content=self.summary(), view=self)

        async def on_country(interaction):
            v = c.values[0]; _mark_default(c, v)
            data["country"] = None if v == "none" else v
            await interaction.response.edit_message(content=self.summary(), view=self)

        t.callback, r.callback, c.callback = on_type, on_recur, on_country
        for item in (t, r, c):
            self.add_item(item)

        more = discord.ui.Button(label=l10n.get("event_wiz_btn_more", lang),
                                 style=discord.ButtonStyle.secondary, row=3)
        send = discord.ui.Button(label=l10n.get("event_wiz_btn_submit", lang),
                                 style=discord.ButtonStyle.success, row=4)
        stop = discord.ui.Button(label=l10n.get("map_wiz_btn_cancel", lang),
                                 style=discord.ButtonStyle.secondary, row=4)

        async def on_more(interaction):
            await interaction.response.send_modal(EventMoreModal(self))

        async def on_send(interaction):
            cc, plz = self.data.get("country"), self.data.get("plz")
            if plz and cc and geo.is_dach(cc) and not geo.resolve(cc, plz):
                await interaction.response.send_message(
                    l10n.get("map_unknown_plz", self.lang, plz=plz[:10]), ephemeral=True)
                return
            self.stop()
            await interaction.response.edit_message(content=l10n.get("event_added_pending", self.lang),
                                                    view=None)
            await _submit_event(interaction.client, interaction.user, self.data)

        async def on_stop(interaction):
            self.stop()
            await interaction.response.edit_message(content=l10n.get("map_wiz_cancelled", self.lang),
                                                    view=None)

        more.callback, send.callback, stop.callback = on_more, on_send, on_stop
        for item in (more, send, stop):
            self.add_item(item)

    def summary(self) -> str:
        d, lang = self.data, self.lang
        when = _fmt_when(d["dt"], d["has_time"], d.get("end_dt"), d.get("end_has_time", False),
                         l10n.get("event_wiz_until", lang))
        lines = [l10n.get("event_wiz_step2", lang),
                 "",
                 f"**{d['title']}**",
                 f"📅 {when}",
                 f"📍 {d['location']}" + (f" ({d['country'].upper()})" if d.get("country") else "")
                 + (f", PLZ {d['plz']}" if d.get("plz") else ""),
                 f"🏷️ {l10n.get('event_type_' + d['etype'], lang)} · "
                 f"🔁 {l10n.get('event_recur_' + d['recurring'], lang)}",
                 f"🔗 {d['url']}" if d.get("url") else None]
        return "\n".join(x for x in lines if x is not None)[:1900]


async def _submit_event(bot, user, d: dict):
    """Speichert den Event-Vorschlag (pending) und informiert den Betreiber per PN."""
    dt, end_dt = d["dt"], d.get("end_dt")
    cc = d.get("country")
    plz = d.get("plz")
    lat = lon = None
    if cc and geo.is_dach(cc) and plz:
        rec = geo.resolve(cc, plz)           # Events: exakter öffentlicher Ort (kein Fuzz)
        if rec:
            lat, lon = rec["lat"], rec["lon"]
    rrule = _rrule_from(d["recurring"], dt)
    await execute_db(bot,
        """INSERT INTO map_events
             (title, type, country, lat, lon, venue, plz, start_at, end_at, all_day,
              rrule, url, description, submitted_by, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
        (d["title"][:120], d["etype"], cc, lat, lon, d["location"][:160],
         (plz or "")[:12] or None, dt.strftime("%Y-%m-%dT%H:%M:%S"),
         end_dt.strftime("%Y-%m-%dT%H:%M:%S") if end_dt else None,
         0 if d["has_time"] else 1,                 # keine Uhrzeit -> ganztägig
         rrule, (d.get("url") or "")[:300] or None,
         (d.get("description") or "")[:1000] or None, str(user.id)),
        commit=True)
    logger.info("🗺️ event_add: '%s' von %s (pending)", d["title"][:60], user.id)

    # Betreiber-PN bei jeder Einreichung – mit Buttons zum Freigeben/Ablehnen/Bearbeiten.
    if not BOARD_OWNER_ID:
        return
    try:
        ev = await execute_db(bot,
            "SELECT id FROM map_events WHERE submitted_by=? ORDER BY id DESC LIMIT 1",
            (str(user.id),), fetch=True)
        row = await _event_row(bot, ev[0]["id"]) if ev else None
        if not row:
            return
        owner = await bot.fetch_user(BOARD_OWNER_ID)
        olang = await get_user_lang(bot, BOARD_OWNER_ID, None)
        await owner.send(await _event_admin_text(bot, row, olang), view=_event_admin_view(row["id"]))
    except Exception as e:
        logger.debug("event_add Owner-PN fehlgeschlagen: %s", e)


# ── Event-Freigabe: Admin-PN mit Buttons, Benachrichtigung der Einreichenden ─────
def _recur_code(rrule) -> str:
    """Gespeicherte RRULE -> Code der Wiederholungs-Auswahl (für Anzeige/Neuberechnung)."""
    r = (rrule or "").upper()
    if "FREQ=WEEKLY" in r:
        return "weekly"
    if "FREQ=MONTHLY" in r:
        return "monthly_weekday" if "BYDAY" in r else "monthly_day"
    if "FREQ=YEARLY" in r:
        return "yearly"
    return "none"


def _row_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("T", " ")) if s else None
    except ValueError:
        return None


def _event_when(row, until: str = "bis", notes: bool = True) -> str:
    """Zeitraum eines gespeicherten Events (DB-Zeile) als Text."""
    st, en = _row_dt(row["start_at"]), _row_dt(row["end_at"])
    if st is None:
        return "?"
    has_time = not row["all_day"]
    end_has_time = bool(en and has_time and (en.hour or en.minute))
    when = _fmt_when(st, has_time, en, end_has_time, until)
    if notes:
        if not has_time:
            when += " (ganztägig)"
        elif not end_has_time:
            when += " (keine Endzeit)"
    return when


async def _event_row(bot, eid):
    rows = await execute_db(bot, "SELECT * FROM map_events WHERE id=?", (int(eid),), fetch=True)
    return rows[0] if rows else None


async def _event_admin_text(bot, row, lang: str) -> str:
    """Text der Betreiber-PN (alle Felder) aus der DB-Zeile."""
    L = lambda k: l10n.get(k, lang)
    cc, plz = row["country"], row["plz"]
    if row["lat"] is not None:
        pin = f"✅ ja ({row['lat']:.4f}, {row['lon']:.4f})"
    elif plz and cc and geo.is_dach(cc):
        pin = "⚠️ nein – PLZ nicht auflösbar"
    elif not cc:
        pin = "⚠️ nein – kein Land gewählt (nur Liste/Kalender)"
    elif not geo.is_dach(cc):
        pin = "⚠️ nein – Land außerhalb DACH (nur Liste/Kalender)"
    else:
        pin = "⚠️ nein – keine PLZ angegeben (nur Liste/Kalender)"
    desc = (row["description"] or "").strip()
    if len(desc) > 300:
        desc = desc[:300].rstrip() + " …"
    who = "?"
    if row["submitted_by"]:
        try:
            u = bot.get_user(int(row["submitted_by"])) or await bot.fetch_user(int(row["submitted_by"]))
            who = f"{getattr(u, 'display_name', u.name)} ({row['submitted_by']})"
        except Exception:
            who = str(row["submitted_by"])
    rrule = row["rrule"]
    status = {"pending": "zur Freigabe", "approved": "✅ freigegeben",
              "rejected": "🗑 abgelehnt"}.get(row["status"], row["status"])
    lines = [
        f"🗺️ **Event-Vorschlag #{row['id']}** ({status})",
        f"• Titel: {row['title']}",
        f"• Art: {L('event_type_' + (row['type'] or 'other'))}",
        f"• Wann: {_event_when(row)}",
        f"• Wiederholung: {L('event_recur_' + _recur_code(rrule))}" + (f" (`{rrule}`)" if rrule else ""),
        f"• Ort: {row['venue'] or '–'}",
        f"• Land: {cc.upper() if cc else '–'} · PLZ: {plz or '–'}",
        f"• Karten-Pin: {pin}",
        f"• Link: <{row['url']}>" if row["url"] else "• Link: –",
        f"• Beschreibung: {desc}" if desc else "• Beschreibung: –",
        f"• Von: {who}",
    ]
    if row["status"] == "pending":
        lines.append("Buttons unten – oder `/event_edit event_id:%s` für Land, PLZ, Art, Link usw." % row["id"])
    return "\n".join(lines)[:2000]


def _event_admin_view(eid) -> discord.ui.View:
    """Buttons der Betreiber-PN. Feste custom_ids (mapev:<aktion>:<id>) -> wirken auch nach
    einem Bot-Neustart, ausgewertet in MapCog.on_interaction."""
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Freigeben", emoji="✅", style=discord.ButtonStyle.success,
                                 custom_id=f"mapev:approve:{eid}"))
    v.add_item(discord.ui.Button(label="Ablehnen", emoji="🗑", style=discord.ButtonStyle.danger,
                                 custom_id=f"mapev:reject:{eid}"))
    v.add_item(discord.ui.Button(label="Bearbeiten", emoji="✏️", style=discord.ButtonStyle.secondary,
                                 custom_id=f"mapev:edit:{eid}"))
    return v


def _map_url() -> str:
    return f"{BOARD_PUBLIC_URL}/map" if BOARD_PUBLIC_URL else ""


async def _notify_submitter(bot, row, approved: bool, reason: str | None, actor_id=None):
    """PN an die einreichende Person (nicht, wenn sie selbst freigibt/ablehnt)."""
    uid = row["submitted_by"]
    if not uid or (actor_id is not None and str(actor_id) == str(uid)):
        return
    try:
        user = await bot.fetch_user(int(uid))
        lang = await get_user_lang(bot, int(uid), None)
        if approved:
            msg = l10n.get("event_approved_dm", lang, title=row["title"], when=_event_when(row, notes=False))
            if _map_url():
                msg += "\n" + _map_url()
        else:
            msg = l10n.get("event_rejected_dm", lang, title=row["title"], when=_event_when(row, notes=False))
            if reason:
                msg += "\n" + l10n.get("event_rejected_reason", lang, reason=reason[:500])
        await user.send(msg[:2000])
    except Exception as e:
        logger.debug("Event-Benachrichtigung an %s fehlgeschlagen: %s", uid, e)


async def _set_event_status(bot, eid, status: str, reason: str | None = None, actor_id=None):
    """pending -> approved/rejected. Gibt die aktualisierte Zeile zurück (None = nicht offen)."""
    rc = await execute_db(bot,
        "UPDATE map_events SET status=? WHERE id=? AND status='pending'",
        (status, int(eid)), commit=True)
    if not rc:
        return None
    row = await _event_row(bot, eid)
    if row:
        await _notify_submitter(bot, row, status == "approved", reason, actor_id)
        logger.info("🗺️ Event #%s %s von %s", eid, status, actor_id)
    return row


class EventRejectModal(discord.ui.Modal):
    def __init__(self, eid: int):
        super().__init__(title=f"Event #{eid} ablehnen")
        self.eid = eid
        self.add_item(discord.ui.InputText(label="Grund (optional, geht an Einreichende)",
                                           required=False, max_length=500,
                                           style=discord.InputTextStyle.long))

    async def callback(self, interaction: discord.Interaction):
        reason = (self.children[0].value or "").strip() or None
        row = await _set_event_status(interaction.client, self.eid, "rejected", reason, interaction.user.id)
        await _refresh_admin_message(interaction, self.eid, row is None)


class EventQuickEditModal(discord.ui.Modal):
    """Schnell-Bearbeitung aus der PN: Titel, Datum, Uhrzeit, Ende, Ort."""
    def __init__(self, row):
        super().__init__(title=f"Event #{row['id']} bearbeiten")
        self.eid = row["id"]
        st, en = _row_dt(row["start_at"]), _row_dt(row["end_at"])
        has_time = not row["all_day"]
        end_txt = ""
        if en:
            end_txt = en.strftime("%d.%m.%Y")
            if has_time and (en.hour or en.minute):
                end_txt = (en.strftime("%H:%M") if st and en.date() == st.date()
                           else en.strftime("%d.%m.%Y %H:%M"))
        self.add_item(discord.ui.InputText(label="Titel", max_length=120, value=row["title"]))
        self.add_item(discord.ui.InputText(label="Datum TT.MM.JJJJ", max_length=10,
                                           value=st.strftime("%d.%m.%Y") if st else None))
        self.add_item(discord.ui.InputText(label="Uhrzeit HH:MM (leer = ganztägig)", required=False,
                                           max_length=5, value=st.strftime("%H:%M") if (st and has_time) else None))
        self.add_item(discord.ui.InputText(label="Ende: TT.MM.JJJJ und/oder HH:MM (optional)", required=False,
                                           max_length=16, value=end_txt or None))
        self.add_item(discord.ui.InputText(label="Ort", max_length=160, value=row["venue"] or None))

    async def callback(self, interaction: discord.Interaction):
        title, date, time, end, loc = ((c.value or "").strip() for c in self.children)
        bot = interaction.client
        row = await _event_row(bot, self.eid)
        if not row:
            await interaction.response.send_message("❌ Event nicht gefunden.", ephemeral=True); return
        dt = _parse_date(date, time or None)
        if dt is None or not title or not loc:
            await interaction.response.send_message(l10n.get("event_bad_date", "de"), ephemeral=True); return
        end_date = " ".join(p for p in end.split() if ":" not in p)
        end_time = " ".join(p for p in end.split() if ":" in p)
        end_dt, _eht, err = _combine_end(dt, bool(time), end_date, end_time)
        if err:
            await interaction.response.send_message(l10n.get(err, "de"), ephemeral=True); return
        rrule = row["rrule"]
        if rrule:                                  # Serienregel an neues Datum anpassen
            rrule = _rrule_from(_recur_code(rrule), dt) or rrule
        await execute_db(bot,
            "UPDATE map_events SET title=?, venue=?, start_at=?, end_at=?, all_day=?, rrule=? WHERE id=?",
            (title[:120], loc[:160], dt.strftime("%Y-%m-%dT%H:%M:%S"),
             end_dt.strftime("%Y-%m-%dT%H:%M:%S") if end_dt else None,
             0 if time else 1, rrule, self.eid), commit=True)
        logger.info("🗺️ Event #%s per PN bearbeitet von %s", self.eid, interaction.user.id)
        await _refresh_admin_message(interaction, self.eid, False)


async def _refresh_admin_message(interaction: discord.Interaction, eid, not_open: bool):
    """Betreiber-PN nach einer Aktion neu aufbauen (Buttons nur solange offen)."""
    bot = interaction.client
    row = await _event_row(bot, eid)
    if row is None:
        await interaction.response.edit_message(content=f"❌ Event #{eid} existiert nicht mehr.", view=None)
        return
    lang = await get_user_lang(bot, interaction.user.id, None)
    text = await _event_admin_text(bot, row, lang)
    if not_open:
        text = (text + f"\n\nℹ️ War bereits bearbeitet (Status: {row['status']}).")[:2000]
    view = _event_admin_view(eid) if row["status"] == "pending" else None
    await interaction.response.edit_message(content=text, view=view)


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
        if not BOARD_OWNER_ID or interaction.user.id != BOARD_OWNER_ID:
            await interaction.response.send_message("❌ Nur für den Betreiber.", ephemeral=True)
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
        description="Auf der Halter-Karte eintragen (freiwillig, grob, DSGVO-konform)")
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
    @discord.slash_command(name="map_remove", description="Eigenen Karteneintrag löschen")
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
    @discord.slash_command(name="map_tags", description="Tags für den Karteneintrag setzen/ändern")
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
    @discord.slash_command(name="map_confirm", description="Karteneintrag als aktuell bestätigen")
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
        description="Termin/Messe/Treffen für die Karte vorschlagen (Freigabe durch Admin)")
    @allowed_channel()
    async def event_add(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ctx.send_modal(EventAddModal(lang))

    # ── Admin: Events freigeben ──────────────────────────────────────────────
    @discord.slash_command(name="event_pending", description="🔒 [Admin] Offene Event-Vorschläge anzeigen",
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

    @discord.slash_command(name="event_approve", description="🔒 [Admin] Event freigeben",
        default_member_permissions=discord.Permissions(manage_messages=True))
    @admin_or_manage_messages()
    @allowed_channel()
    async def event_approve(self, ctx: discord.ApplicationContext,
                            event_id: discord.Option(int, "Event-ID", required=True)):
        await ctx.defer(ephemeral=True)
        row = await _set_event_status(self.bot, event_id, "approved", actor_id=ctx.author.id)
        await ctx.followup.send(("✅ Freigegeben." if row else "❌ Nicht gefunden/bereits bearbeitet."),
                                ephemeral=True)

    @discord.slash_command(name="event_reject", description="🔒 [Admin] Event ablehnen",
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
        description="🔒 [Admin] Einzelnen Serien-Termin absagen (EXDATE)",
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
        description="🔒 [Admin] Eingereichtes/aktives Event anpassen (nur angegebene Felder)",
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
        description="🔒 [Admin] Karten-Geodaten jetzt neu laden (Leaflet/GeoJSON/PLZ)",
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
