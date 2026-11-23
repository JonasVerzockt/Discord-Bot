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
utils/map_actions.py – Abläufe der Halter-Karte ohne eigene Oberfläche.

Eintrag speichern, Datums-/Zeiteingaben prüfen, Events einreichen/freigeben/ankündigen,
Einreichende benachrichtigen. Aus cogs/map.py herausgelöst (5.1.0); Verhalten unverändert.
"""

import logging
from datetime import datetime
import discord
from config import BOARD_OWNER_ID, BOARD_PUBLIC_URL, MAP_JITTER_METERS
from utils.db import execute_db
from utils.localization import get_user_lang, l10n
from utils import geo
from utils.map_events import _recur_code, _row_dt, _rrule_from
from utils.map_store import _event_row

logger = logging.getLogger("cogs.map")   # Log-Name wie vor dem Umbau


# Kanal, in dem neu freigegebene Termine angekündigt werden (fest eingetragen, keine .env nötig).
EVENT_ANNOUNCE_CHANNEL_ID = 543529652413530123
_EVENT_COLORS = {"fair": 0x3fb950, "meetup": 0x58a6ff, "shop": 0xe3833b,
                 "talk": 0xa371f7, "field": 0xd29922, "other": 0x8b949e}

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
    if not age_18:   # unter 18 nicht einzeln sichtbar -> keine „Auch aktiv in“-Regionen speichern
        await execute_db(bot, "DELETE FROM map_entry_regions WHERE user_id=?", (str(user.id),), commit=True)
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


async def _announce_event(bot, row):
    """Neu freigegebenen Termin mit allen Details im Ankündigungs-Kanal posten."""
    try:
        ch = bot.get_channel(EVENT_ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(EVENT_ANNOUNCE_CHANNEL_ID)
        L = lambda k: l10n.get(k, "de")
        emb = discord.Embed(title=f"📅 Neuer Termin: {row['title']}"[:256],
                            color=_EVENT_COLORS.get(row["type"] or "other", 0x8b949e))
        emb.add_field(name="Wann", value=_event_when(row, notes=False)[:1024], inline=False)
        rec = _recur_code(row["rrule"])
        if rec != "none":
            emb.add_field(name="Wiederholung", value=L("event_recur_" + rec)[:1024], inline=True)
        emb.add_field(name="Art", value=L("event_type_" + (row["type"] or "other"))[:1024], inline=True)
        loc = row["venue"] or "–"
        if row["plz"] and row["plz"] not in loc:
            loc += f", {row['plz']}"
        if row["country"]:
            loc += f" ({row['country'].upper()})"
        emb.add_field(name="Ort", value=loc[:1024], inline=False)
        if row["description"]:
            emb.description = row["description"][:1000]
        links = []
        if row["url"]:
            links.append(f"[Infos zum Termin]({row['url']})")
        if BOARD_PUBLIC_URL:
            links.append(f"[Zur Karte]({BOARD_PUBLIC_URL}/map)")
            links.append(f"[In Kalender]({BOARD_PUBLIC_URL}/map/events/{row['id']}.ics)")
        if links:
            emb.add_field(name="Links", value=" · ".join(links)[:1024], inline=False)
        emb.set_footer(text="Eigene Termine vorschlagen: /event_add")
        await ch.send(embed=emb)
    except Exception as e:
        logger.warning("Event-Ankündigung #%s fehlgeschlagen: %s", row["id"], e)


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
        if status == "approved":
            await _announce_event(bot, row)
        logger.info("🗺️ Event #%s %s von %s", eid, status, actor_id)
    return row
