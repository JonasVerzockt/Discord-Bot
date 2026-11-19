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
utils/account.py – Datenschicht für „Mein Konto“ (Board-Seite /konto).

  • Persönliche Einstellungen (Tabelle user_prefs, im Speicher zwischengespeichert):
    Termin-Erinnerung an/aus, Ruhezeiten, „überall abmelden“, Kalender-Link.
  • PN-Verlauf (dm_log): ein Hook auf Messageable.send merkt sich eine kurze
    Vorschau jeder PN an eine Person (30 Tage, höchstens 50 je Person).
  • Datenauskunft (export_user) und Löschen je Bereich (delete_area).

Keine Discord-Befehle hier; die stehen in cogs/account.py, die Seite in cogs/board.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta

import discord

from utils.db import execute_db
from utils.timez import BERLIN

logger = logging.getLogger(__name__)

DM_LOG_DAYS = 30
DM_LOG_MAX = 50

# ── Einstellungen (user_prefs) ────────────────────────────────────────────────
DEFAULTS = {"rsvp_reminder": 1, "quiet_start": None, "quiet_end": None,
            "logout_before": 0, "ics_salt": None,
            "intros": 1, "ach_seen_at": None, "milestones": None}
_PREFS: dict[str, dict] = {}


async def load_prefs(bot) -> int:
    """Alle Einstellungen in den Speicher laden (beim Start)."""
    rows = await execute_db(bot, "SELECT * FROM user_prefs", fetch=True) or []
    _PREFS.clear()
    for r in rows:
        _PREFS[str(r["user_id"])] = {k: r[k] for k in DEFAULTS}
    return len(rows)


def prefs(uid) -> dict:
    return {**DEFAULTS, **_PREFS.get(str(uid), {})}


async def set_prefs(bot, uid, **values) -> None:
    uid = str(uid)
    cols = [k for k in values if k in DEFAULTS]
    if not cols:
        return
    merged = {**prefs(uid), **{k: values[k] for k in cols}}
    await execute_db(bot,
        "INSERT INTO user_prefs (user_id, rsvp_reminder, quiet_start, quiet_end, logout_before, ics_salt, "
        "intros, ach_seen_at, milestones, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(user_id) DO UPDATE SET "
        "rsvp_reminder=excluded.rsvp_reminder, quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end, "
        "logout_before=excluded.logout_before, ics_salt=excluded.ics_salt, intros=excluded.intros, "
        "ach_seen_at=excluded.ach_seen_at, milestones=excluded.milestones, updated_at=excluded.updated_at",
        (uid, int(merged["rsvp_reminder"]), merged["quiet_start"], merged["quiet_end"],
         int(merged["logout_before"] or 0), merged["ics_salt"], int(merged["intros"]),
         merged["ach_seen_at"], merged["milestones"]), commit=True)
    _PREFS[uid] = {k: merged[k] for k in DEFAULTS}


def quiet_window(start, end, hour: int) -> bool:
    """Liegt die Stunde im Fenster [start, end)? Darf über Mitternacht laufen. start==end = aus."""
    if start is None or end is None or start == end:
        return False
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


def is_quiet(uid, now: datetime | None = None) -> bool:
    """Hat die Person gerade Ruhezeit (Berliner Zeit)?"""
    p = _PREFS.get(str(uid))
    if not p:
        return False
    h = (now or datetime.now(BERLIN)).astimezone(BERLIN).hour
    return quiet_window(p.get("quiet_start"), p.get("quiet_end"), h)


def rsvp_reminder_on(uid) -> bool:
    return bool(prefs(uid)["rsvp_reminder"])


def session_ok(uid, issued: int) -> bool:
    """Board-Sitzung noch gültig (nicht vor „überall abmelden“ begonnen)?"""
    return int(issued) >= int(prefs(uid)["logout_before"] or 0)


async def ics_salt(bot, uid, rotate: bool = False) -> str:
    s = prefs(uid)["ics_salt"]
    if rotate or not s:
        s = secrets.token_hex(8)
        await set_prefs(bot, uid, ics_salt=s)
    return s


def uid_for_ics_salt(uid: str, salt: str) -> bool:
    return bool(salt) and prefs(uid)["ics_salt"] == salt


# ── PN-Verlauf ────────────────────────────────────────────────────────────────
_MD = re.compile(r"(\*\*|__|~~|`+|^>+\s?|\|\|)", re.M)
_ORIG_SEND = None


def _preview(content, kwargs) -> str:
    parts = []
    if content:
        parts.append(str(content))
    embeds = list(kwargs.get("embeds") or [])
    if kwargs.get("embed") is not None:
        embeds.insert(0, kwargs["embed"])
    for e in embeds[:1]:
        for x in (getattr(e, "title", None), getattr(e, "description", None)):
            if x:
                parts.append(str(x))
        if not getattr(e, "description", None):
            for f in list(getattr(e, "fields", []) or [])[:2]:
                parts.append(f"{f.name}: {f.value}")
    if kwargs.get("file") is not None or kwargs.get("files"):
        parts.append("📎")
    txt = _MD.sub("", " ".join(parts))
    txt = re.sub(r"<a?:(\w+):\d+>", r":\1:", txt)          # Custom-Emojis lesbar
    txt = " ".join(txt.split())
    return (txt[:157] + "…") if len(txt) > 160 else txt


def install_dm_hook(bot) -> None:
    """Merkt sich jede PN an eine Person (nur Vorschau) für den Verlauf in /konto.
    Antworten in einem PN-Gespräch (z. B. KI-Chat) laufen über den DM-Kanal und
    werden bewusst NICHT erfasst, nur PNs, die der Bot von sich aus an eine Person schickt."""
    global _ORIG_SEND
    import discord.abc as A
    if _ORIG_SEND is not None:
        return
    _ORIG_SEND = A.Messageable.send

    async def send(self, content=None, **kwargs):
        msg = await _ORIG_SEND(self, content, **kwargs)
        try:
            if isinstance(self, (discord.User, discord.Member)) and not getattr(self, "bot", False):
                pv = _preview(content, kwargs)
                if pv:
                    asyncio.create_task(execute_db(bot,
                        "INSERT INTO dm_log (user_id, preview) VALUES (?, ?)",
                        (str(self.id), pv), commit=True))
        except Exception as e:                      # Verlauf darf nie den Versand stören
            logger.debug("dm_log: %s", e)
        return msg

    A.Messageable.send = send


async def prune_dm_log(bot) -> None:
    cut = (datetime.utcnow() - timedelta(days=DM_LOG_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    await execute_db(bot, "DELETE FROM dm_log WHERE sent_at < ?", (cut,), commit=True)
    await execute_db(bot,
        "DELETE FROM dm_log WHERE id IN (SELECT d.id FROM dm_log d WHERE "
        "(SELECT COUNT(*) FROM dm_log d2 WHERE d2.user_id=d.user_id AND d2.id>d.id) >= ?)",
        (DM_LOG_MAX,), commit=True)


# ── Datenauskunft ─────────────────────────────────────────────────────────────
# (Schlüssel im Export, SQL, uid als int?)
_EXPORT: list[tuple[str, str, bool]] = [
    ("settings", "SELECT * FROM user_settings WHERE user_id=?", False),
    ("account_prefs", "SELECT * FROM user_prefs WHERE user_id=?", False),
    ("notifications", "SELECT * FROM notifications WHERE user_id=?", False),
    ("seen_products", "SELECT * FROM user_seen_products WHERE user_id=?", False),
    ("shop_blacklist", "SELECT * FROM user_shop_blacklist WHERE user_id=?", False),
    ("price_tracking", "SELECT * FROM user_price_tracking WHERE user_id=?", False),
    ("server_mappings", "SELECT * FROM server_user_mappings WHERE user_id=?", False),
    ("ch_delivery_added", "SELECT * FROM ch_delivery_shops WHERE added_by=?", False),
    ("ai_budget_last30", "SELECT date, cost_usd FROM ai_chat_budget WHERE user_id=? ORDER BY date DESC LIMIT 30", True),
    ("ai_spend_ledger", "SELECT * FROM ai_chat_user_spend WHERE user_id=? ORDER BY period_type, period_key DESC", True),
    ("ai_model_preference", "SELECT * FROM ai_chat_user_model WHERE user_id=?", True),
    ("ai_chat_history", "SELECT message_id, channel_id, created_at, expires_at, history_json FROM ai_chat_history WHERE user_id=?", True),
    ("species_watch", "SELECT * FROM user_species_watch WHERE user_id=?", False),
    ("species_watch_seen", "SELECT * FROM user_species_watch_seen WHERE user_id=?", False),
    ("species_watch_variant_seen", "SELECT * FROM user_species_watch_variant_seen WHERE user_id=?", False),
    ("pending_variant_removed", "SELECT * FROM pending_variant_removed WHERE user_id=?", False),
    ("digest_subscription", "SELECT * FROM digest_subscribers WHERE user_id=?", False),
    ("achievements", "SELECT * FROM achievements WHERE user_id=?", False),
    ("events", "SELECT * FROM user_events WHERE user_id=?", False),
    ("inat_account", "SELECT * FROM inat_accounts WHERE discord_id=?", False),
    ("inat_link_code", "SELECT * FROM inat_link_codes WHERE discord_id=?", False),
    ("inat_observations", "SELECT * FROM inat_obs WHERE discord_id=?", False),
    ("review_authorship", "SELECT * FROM review_authors WHERE user_id=?", False),
    ("command_log", "SELECT * FROM command_log WHERE user_id=?", False),
    ("offer_keywords", "SELECT * FROM offer_keywords WHERE user_id=?", False),
    ("offer_alert_seen", "SELECT * FROM offer_alert_seen WHERE user_id=?", False),
    ("offer_alert_msgs", "SELECT * FROM offer_alert_msgs WHERE user_id=?", False),
    ("offer_quiet_queue", "SELECT * FROM quiet_offer_queue WHERE user_id=?", False),
    ("dm_history", "SELECT sent_at, preview FROM dm_log WHERE user_id=? ORDER BY id DESC", False),
    ("map_entry", "SELECT * FROM map_entries WHERE user_id=?", False),
    ("map_entry_tags", "SELECT * FROM map_entry_tags WHERE user_id=?", False),
    ("map_entry_regions", "SELECT * FROM map_entry_regions WHERE user_id=?", False),
    ("map_events_submitted", "SELECT * FROM map_events WHERE submitted_by=?", False),
    ("map_event_rsvp", "SELECT * FROM map_event_rsvp WHERE user_id=?", False),
    ("map_rsvp_reminded", "SELECT * FROM map_rsvp_reminded WHERE user_id=?", False),
]


async def export_user(bot, uid) -> dict:
    """Alle zu einer Discord-ID gespeicherten Daten (Art. 15 DSGVO)."""
    uid = str(int(uid))
    data: dict = {"user_id": uid, "exported_at": datetime.utcnow().isoformat() + "Z"}
    for key, sql, as_int in _EXPORT:
        try:
            rows = await execute_db(bot, sql, (int(uid) if as_int else uid,), fetch=True) or []
            data[key] = [dict(r) for r in rows]
        except Exception:
            data[key] = []
    # Gepostete Rabattcodes werden über den Discord-Benutzernamen zugeordnet.
    try:
        u = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
        uname = getattr(u, "name", None)
        data["discount_codes_posted"] = [dict(r) for r in await execute_db(
            bot, "SELECT * FROM discount_codes WHERE author=?", (uname,), fetch=True) or []] if uname else []
    except Exception:
        data["discount_codes_posted"] = []
    return data


# ── Löschen je Bereich ────────────────────────────────────────────────────────
# Bereich → [(SQL, uid als int?)]. Nicht löschbar: Befehlsprotokoll (Moderation, wird nach
# COMMAND_LOG_RETENTION_DAYS automatisch gelöscht) und das heutige KI-Budget (Missbrauchsschutz).
AREAS: dict[str, list[tuple[str, bool]]] = {
    "notifications": [
        ("DELETE FROM notifications WHERE user_id=?", False),
        ("DELETE FROM user_seen_products WHERE user_id=?", False),
        ("DELETE FROM server_user_mappings WHERE user_id=?", False),
    ],
    "prices": [
        ("DELETE FROM user_price_tracking WHERE user_id=?", False),
        ("DELETE FROM user_species_watch WHERE user_id=?", False),
        ("DELETE FROM user_species_watch_seen WHERE user_id=?", False),
        ("DELETE FROM user_species_watch_variant_seen WHERE user_id=?", False),
        ("DELETE FROM pending_variant_removed WHERE user_id=?", False),
    ],
    "settings": [
        ("DELETE FROM user_settings WHERE user_id=?", True),
        ("DELETE FROM user_settings WHERE user_id=?", False),
        ("DELETE FROM user_shop_blacklist WHERE user_id=?", False),
        ("DELETE FROM digest_subscribers WHERE user_id=?", False),
        ("DELETE FROM offer_keywords WHERE user_id=?", False),
        ("DELETE FROM offer_alert_seen WHERE user_id=?", False),
        ("DELETE FROM offer_alert_msgs WHERE user_id=?", False),
        ("DELETE FROM quiet_offer_queue WHERE user_id=?", False),
    ],
    "map": [
        ("DELETE FROM map_entry_tags WHERE user_id=?", False),
        ("DELETE FROM map_entry_regions WHERE user_id=?", False),
        ("DELETE FROM map_entries WHERE user_id=?", False),
        ("DELETE FROM map_event_rsvp WHERE user_id=?", False),
        ("DELETE FROM map_rsvp_reminded WHERE user_id=?", False),
        ("UPDATE map_events SET submitted_by=NULL WHERE submitted_by=?", False),
    ],
    "inat": [
        ("DELETE FROM inat_accounts WHERE discord_id=?", False),
        ("DELETE FROM inat_link_codes WHERE discord_id=?", False),
        ("DELETE FROM inat_obs WHERE discord_id=?", False),
    ],
    "achievements": [
        ("DELETE FROM achievements WHERE user_id=?", False),
        ("DELETE FROM user_events WHERE user_id=?", False),
    ],
    "ai": [
        ("DELETE FROM ai_chat_history WHERE user_id=?", True),
        ("DELETE FROM ai_chat_user_model WHERE user_id=?", True),
        ("DELETE FROM ai_chat_user_spend WHERE user_id=?", True),
        ("DELETE FROM ai_chat_budget WHERE user_id=? AND date < date('now')", True),
    ],
    "reviews": [
        ("DELETE FROM review_authors WHERE user_id=?", False),
    ],
    "history": [
        ("DELETE FROM dm_log WHERE user_id=?", False),
    ],
}


async def delete_area(bot, uid, area: str) -> int:
    """Löscht einen Bereich (oder 'all'). Gibt die Zahl betroffener Zeilen zurück."""
    uid = str(int(uid))
    names = list(AREAS) if area == "all" else [area]
    total = 0
    for name in names:
        for sql, as_int in AREAS.get(name, []):
            try:
                total += max(0, await execute_db(bot, sql, (int(uid) if as_int else uid,), commit=True) or 0)
            except Exception as e:
                logger.warning("konto delete %s: %s", name, e)
    if area in ("settings", "all") and str(uid) in _PREFS:
        # Einstellungen zurücksetzen, „überall abmelden“ aber behalten (sonst wären alte
        # Sitzungen wieder gültig).
        keep = prefs(uid)["logout_before"]
        await execute_db(bot, "DELETE FROM user_prefs WHERE user_id=?", (uid,), commit=True)
        _PREFS.pop(uid, None)
        if keep:
            await set_prefs(bot, uid, logout_before=keep)
    logger.info("🗑️ Konto: Bereich %s gelöscht (%d Zeilen)", area, total)
    return total


# ── Daten für die Seite ───────────────────────────────────────────────────────
async def _rows(bot, sql, params=()):
    return [dict(r) for r in (await execute_db(bot, sql, params, fetch=True) or [])]


async def collect(bot, uid, lang: str) -> dict:
    """Alles, was /konto anzeigt (nur eigene Daten)."""
    from utils.localization import l10n
    uid = str(uid)
    d: dict = {}
    d["notifications"] = await _rows(bot,
        "SELECT id, species, regions, status, excluded_species, created_at, notified_at "
        "FROM notifications WHERE user_id=? ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 "
        "ELSE 2 END, created_at DESC", (uid,))
    d["tracked"] = await _rows(bot,
        "SELECT product_id, variant_id, variant_title, species, product_title, product_url, shop_name, "
        "currency_iso, last_notified_min, last_notified_max, target_price, target_mode, added_at "
        "FROM user_price_tracking WHERE user_id=? ORDER BY added_at DESC", (uid,))
    d["watches"] = await _rows(bot,
        "SELECT species, is_genus, created_at FROM user_species_watch WHERE user_id=? ORDER BY species", (uid,))
    d["keywords"] = await _rows(bot,
        "SELECT keyword, keyword_raw, created_at FROM offer_keywords WHERE user_id=? ORDER BY created_at", (uid,))
    lr = await _rows(bot, "SELECT language FROM user_settings WHERE user_id=? OR user_id=?", (uid, int(uid)))
    d["language"] = lr[0]["language"] if lr else ""
    d["digest"] = bool(await _rows(bot, "SELECT 1 FROM digest_subscribers WHERE user_id=?", (uid,)))
    d["blacklist"] = [r["shop_id"] for r in await _rows(bot,
        "SELECT shop_id FROM user_shop_blacklist WHERE user_id=?", (uid,))]
    d["prefs"] = prefs(uid)

    # Karte
    me = await _rows(bot, "SELECT * FROM map_entries WHERE user_id=?", (uid,))
    d["map"] = me[0] if me else None
    d["map_tags"] = [r["tag_code"] for r in await _rows(bot,
        "SELECT tag_code FROM map_entry_tags WHERE user_id=?", (uid,))]
    today = datetime.now(BERLIN).strftime("%Y-%m-%d")
    d["rsvps"] = await _rows(bot,
        "SELECT r.event_id, r.occ_date, e.title, e.type, e.start_at, e.end_at, e.all_day, e.venue, e.plz, "
        "e.country, e.url FROM map_event_rsvp r JOIN map_events e ON e.id=r.event_id "
        "WHERE r.user_id=? AND r.occ_date>=? AND e.status='approved' ORDER BY r.occ_date", (uid, today))

    # iNat
    try:
        from utils import inat as I
        d["inat"] = await I.account_of(bot, uid)
        d["inat_pending"] = await I.pending_link(bot, uid)
        y = datetime.now(BERLIN).year
        d["inat_year"] = await I.user_stats(bot, uid, y)
        d["inat_all"] = await I.user_stats(bot, uid)
    except Exception as e:
        logger.debug("konto inat: %s", e)
        d.update(inat=None, inat_pending=None, inat_year={"obs": 0, "species": 0, "taxa": []},
                 inat_all={"obs": 0, "species": 0, "taxa": []})

    # Erfolge (nur lesen, nichts freischalten)
    try:
        from utils.achievements import ACHIEVEMENTS, gather_stats, evaluate, rank_for, next_rank_threshold
        have = {r["achievement_id"]: r["unlocked_at"] for r in await _rows(bot,
            "SELECT achievement_id, unlocked_at FROM achievements WHERE user_id=?", (uid,))}
        u = bot.get_user(int(uid)) if hasattr(bot, "get_user") else None
        res = evaluate(await gather_stats(bot, uid, getattr(u, "name", None)))
        ach = []
        for a, cur, tgt, unl in res:
            got = a.id in have or bool(unl)       # erfüllt = beim nächsten Befehl ohnehin freigeschaltet
            ach.append({"id": a.id, "emoji": a.emoji, "cat": a.cat, "hidden": a.hidden,
                        "unlocked": got, "date": (have.get(a.id) or "")[:10],
                        "cur": cur, "tgt": tgt,
                        "title": l10n.get(f"ach_{a.id}_t", lang), "desc": l10n.get(f"ach_{a.id}_d", lang)})
        n = sum(1 for a in ach if a["unlocked"])
        _i, rkey, remoji = rank_for(n)
        d["ach"] = ach
        d["rank"] = {"emoji": remoji, "name": l10n.get(f"rank_{rkey}", lang), "n": n,
                     "total": len(ACHIEVEMENTS), "next": next_rank_threshold(n)}
    except Exception as e:
        logger.warning("konto achievements: %s", e)
        d["ach"], d["rank"] = [], None

    # Bewertungen (Zuordnung nur für dich sichtbar)
    d["reviews"] = await _rows(bot,
        "SELECT message_id, review_date, shop, rating FROM review_authors WHERE user_id=? "
        "ORDER BY review_date DESC LIMIT 100", (uid,))
    d["review_link"] = ""
    try:
        from config import REVIEW_CHANNEL_ID, MAP_GUILD_ID
        ch = bot.get_channel(REVIEW_CHANNEL_ID) if REVIEW_CHANNEL_ID else None
        gid = getattr(getattr(ch, "guild", None), "id", None) or MAP_GUILD_ID
        if REVIEW_CHANNEL_ID and gid:
            d["review_link"] = f"https://discord.com/channels/{gid}/{REVIEW_CHANNEL_ID}/"
    except Exception:
        pass

    # PN-Verlauf
    d["history"] = await _rows(bot,
        "SELECT sent_at, preview FROM dm_log WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, DM_LOG_MAX))

    # KI-Chat-Budget (nur der eigene Tagesanteil, in Prozent)
    try:
        from utils.ai_chat import get_budget_status, _reset_time_str
        b = get_budget_status(int(uid))
        d["ai"] = {"pct": int(min(100.0, round(b.get("user_pct", 0.0)))),
                   "global_pct": int(min(100.0, round(b.get("global_pct", 0.0)))),
                   "reset": _reset_time_str()}
    except Exception as e:
        logger.debug("konto ai: %s", e)
        d["ai"] = None
    return d


def now_epoch() -> int:
    return int(time.time())
