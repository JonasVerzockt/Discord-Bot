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
utils/map_views.py – Discord-Oberfläche der Halter-Karte (Auswahlmenüs, Buttons, Formulare).

Beitreten/Ändern, Tags, Zusatzregionen, Bestätigung, Event einreichen und Event-Admin
(Freigeben/Ablehnen/Schnell-Bearbeiten). Aus cogs/map.py herausgelöst (5.1.0); Verhalten unverändert.
"""

import logging
import discord
from config import BOARD_OWNER_ID
from utils.db import execute_db
from utils.localization import get_user_lang, l10n
from utils import geo, map_tags
from utils.map_events import _recur_code, _row_dt, _rrule_from
from utils.map_store import (
    _delete_entry, _event_row, _load_extra_regions, _region_label, _save_extra_regions,
    MAX_EXTRA_REGIONS,
)

from utils.map_actions import (
    _combine_end, _event_admin_text, _event_admin_view, _fmt_when, _parse_date, _save_join,
    _set_event_status, _submit_event,
)

logger = logging.getLogger("cogs.map")   # Log-Name wie vor dem Umbau


class TagSelect(discord.ui.Select):
    def __init__(self, lang: str, preselected=None, inline: bool = False, row=None):
        preselected = set(preselected or [])
        self._inline = inline     # True: Teil der /map_join-Abschlussansicht (View bleibt offen)
        opts = []
        for g in map_tags.TAG_GROUPS:
            for code, lbl in g["tags"]:
                opts.append(discord.SelectOption(
                    label=lbl.get(lang) or lbl.get("de") or code,
                    value=code, default=(code in preselected)))
        opts = opts[:25]
        super().__init__(placeholder=l10n.get("map_tags_prompt", lang),
                         min_values=0, max_values=len(opts), options=opts, row=row)
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
        if self._inline and isinstance(self.view, MapExtrasView):
            for opt in self.options:
                opt.default = opt.value in self.values
            self.view.note = msg
            await interaction.response.edit_message(content=self.view.text(), view=self.view)
            return
        msg += " " + l10n.get("map_remove_hint", self._lang)
        await interaction.response.edit_message(content=msg, view=None)


class TagView(discord.ui.View):
    def __init__(self, lang: str, preselected=None):
        super().__init__(timeout=300)
        self.add_item(TagSelect(lang, preselected))


# ── „Auch aktiv in“: bis zu 2 weitere Regionen (nur Bundesland/Kanton, kein Ort) ─────
_DACH_FLAGS = {"de": "🇩🇪", "at": "🇦🇹", "ch": "🇨🇭", "li": "🇱🇮"}



class MapExtrasView(discord.ui.View):
    """Abschluss von /map_join (18+): Tags + optional „Auch aktiv in“ (max. 2 Regionen).
    Jede Auswahl wird sofort gespeichert; „Fertig“ schließt die Ansicht."""

    def __init__(self, lang: str, base_msg: str, tags, extras, home: tuple[str, str] | None):
        super().__init__(timeout=900)
        self.lang = lang
        self.base_msg = base_msg
        self.note = ""
        self.home = home                                  # (Land, Region) des Wohnorts
        self.extras = [e for e in (extras or []) if e != home][:MAX_EXTRA_REGIONS]
        if self.extras:
            self.cc = self.extras[0][0]
        elif home and home[0] in _DACH_FLAGS:
            self.cc = home[0]
        else:
            self.cc = "de"
        self.tags = list(tags or [])
        self._build()

    def text(self) -> str:
        if self.extras:
            st = l10n.get("map_extras_status", self.lang,
                          regions=", ".join(_region_label(c, r) for c, r in self.extras))
        else:
            st = l10n.get("map_extras_none", self.lang)
        parts = [self.base_msg, "", l10n.get("map_extras_hint", self.lang), st]
        if self.note:
            parts.append(self.note)
        return "\n".join(parts)[:2000]

    def _build(self):
        self.clear_items()
        tsel = TagSelect(self.lang, self.tags, inline=True, row=0)
        self.add_item(tsel)
        c = discord.ui.Select(placeholder=l10n.get("map_extras_country_ph", self.lang), row=1,
                              options=[discord.SelectOption(label=n, value=v, emoji=_DACH_FLAGS[v],
                                                            default=(v == self.cc))
                                       for v, n in _COUNTRY_OPTS if v in _DACH_FLAGS])

        async def on_country(interaction):
            self.cc = c.values[0]
            self.note = ""
            self._build()
            await interaction.response.edit_message(content=self.text(), view=self)
        c.callback = on_country
        self.add_item(c)

        regions = [(code, name) for code, name in geo.all_regions().get(self.cc, {}).items()
                   if (self.cc, code) != self.home]
        regions.sort(key=lambda t: t[1])
        chunks = [regions[i:i + 25] for i in range(0, len(regions), 25)][:2]   # CH: 26 Kantone
        mine = {code for cc, code in self.extras if cc == self.cc}
        for i, chunk in enumerate(chunks):
            ph = l10n.get("map_extras_region_ph", self.lang)
            if len(chunks) > 1:
                ph += f" ({chunk[0][1][:1]}–{chunk[-1][1][:1]})"
            sel = discord.ui.Select(placeholder=ph[:150], row=2 + i, min_values=0,
                                    max_values=min(len(chunk), MAX_EXTRA_REGIONS),
                                    options=[discord.SelectOption(label=name[:100], value=code,
                                                                  default=(code in mine))
                                             for code, name in chunk])
            sel.callback = self._make_region_cb(sel, {code for code, _ in chunk})
            self.add_item(sel)

        done = discord.ui.Button(label=l10n.get("map_extras_done", self.lang),
                                 style=discord.ButtonStyle.success, row=4)

        async def on_done(interaction):
            self.stop()
            # Abschluss: Erklärtext weg, Status (Regionen/Tags) bleibt stehen.
            await interaction.response.edit_message(
                content=self.text().replace(l10n.get("map_extras_hint", self.lang) + "\n", ""),
                view=None)
        done.callback = on_done
        self.add_item(done)

    def _make_region_cb(self, sel, chunk_codes: set):
        async def cb(interaction):
            keep = [e for e in self.extras if not (e[0] == self.cc and e[1] in chunk_codes)]
            new = keep + [(self.cc, v) for v in sel.values]
            if len(new) > MAX_EXTRA_REGIONS:
                await interaction.response.send_message(
                    l10n.get("map_extras_too_many", self.lang, n=MAX_EXTRA_REGIONS), ephemeral=True)
                return
            self.extras = new
            await _save_extra_regions(interaction.client, interaction.user.id, self.extras)
            self.note = ""
            self._build()
            await interaction.response.edit_message(content=self.text(), view=self)
        return cb


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



# ── Geführte Abläufe (interaktiv) ─────────────────────────────────────────────
_COUNTRY_OPTS = [("de", "Deutschland"), ("at", "Österreich"), ("ch", "Schweiz"),
                 ("li", "Liechtenstein"), ("other", None)]   # None -> l10n
_EVENT_TYPE_CODES = ["fair", "meetup", "shop", "talk", "field", "other"]
_RECUR_CODES = ["none", "weekly", "monthly_weekday", "monthly_day", "yearly"]


def _mark_default(select: discord.ui.Select, value: str):
    """Gewählte Option als Default markieren, damit sie nach einem Neu-Rendern sichtbar bleibt."""
    for o in select.options:
        o.default = (o.value == value)


def _display_preview(first_name, discord_name: str, show_name: bool, lang: str) -> str:
    """So erscheint der Eintrag auf der Karte (gleiche Logik wie board.h_map_pins/h_map_list)."""
    if not show_name:
        return l10n.get("map_preview_anon", lang)
    fn = (first_name or "").strip()[:40]
    return f"{fn} ({discord_name})" if fn else discord_name


class MapJoinModal(discord.ui.Modal):
    def __init__(self, wizard: "MapJoinView", discord_name: str = ""):
        lang = wizard.lang
        self.discord_name = discord_name
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
        # Vorschau im Platzhalter: "Vorname (Discord-Name)" bzw. Hinweis bei anonym.
        if wizard.show_name:
            ph = l10n.get("map_wiz_fn_ph", lang, name=discord_name or "Discord-Name")
        else:
            ph = l10n.get("map_wiz_fn_ph_anon", lang)
        self.add_item(discord.ui.InputText(label=l10n.get("map_wiz_fn", lang)[:45],
                                           required=False, max_length=40, placeholder=ph[:100],
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
        if w.age_18:   # unter 18 erscheint nicht einzeln -> keine Namensvorschau
            name = self.discord_name or getattr(interaction.user, "display_name", interaction.user.name)
            prev = _display_preview(fn, name, w.show_name, w.lang)
            msg += "\n" + l10n.get("map_saved_preview", w.lang,
                                   display=discord.utils.escape_markdown(prev))
        logger.info("🗺️ map_join: %s (%s)", interaction.user.id, cc.upper())
        if w.age_18:
            # Tags + optional „Auch aktiv in“ (max. 2 weitere Bundesländer/Kantone)
            rows = await execute_db(interaction.client,
                "SELECT country, region_code, region_name FROM map_entries WHERE user_id=?",
                (str(interaction.user.id),), fetch=True) or []
            home = None
            if rows and rows[0]["region_code"]:
                home = (rows[0]["country"],
                        geo.canon_region(rows[0]["country"], rows[0]["region_code"],
                                         rows[0]["region_name"] or "")[0])
            extras = await _load_extra_regions(interaction.client, interaction.user.id)
            if home in extras:   # Wohnort jetzt in einer früheren Zusatzregion -> dort entfernen
                extras = [e for e in extras if e != home]
                await _save_extra_regions(interaction.client, interaction.user.id, extras)
            view = MapExtrasView(w.lang, msg, w.tags, extras, home)
            content = view.text()
        else:
            view, content = TagView(w.lang, w.tags), msg
        try:
            await interaction.response.edit_message(content=content, view=view)
        except Exception:
            await interaction.response.send_message(content, view=view, ephemeral=True)


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
            await interaction.response.send_modal(MapJoinModal(
                self, getattr(interaction.user, "display_name", interaction.user.name)))

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


async def _owner_only(interaction: discord.Interaction) -> bool:
    """Zusätzliche Absicherung: Event-Aktionen aus der PN nur durch den Betreiber."""
    if BOARD_OWNER_ID and interaction.user.id == BOARD_OWNER_ID:
        return True
    await interaction.response.send_message("❌ Nur für den Betreiber.", ephemeral=True)
    return False


class EventRejectModal(discord.ui.Modal):
    def __init__(self, eid: int):
        super().__init__(title=f"Event #{eid} ablehnen")
        self.eid = eid
        self.add_item(discord.ui.InputText(label="Grund (optional, geht an Einreichende)",
                                           required=False, max_length=500,
                                           style=discord.InputTextStyle.long))

    async def callback(self, interaction: discord.Interaction):
        if not await _owner_only(interaction):
            return
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
        if not await _owner_only(interaction):
            return
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
