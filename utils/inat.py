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
utils/inat.py – iNaturalist-Statistik: API-Zugriff, Speicherung, Auswertung.

Zählregeln (wie früher die Wettbewerbs-Tabelle):
  • Es zählen nur Beobachtungen, die zu Formicoidea gehören UND dem mit
    /inat_verknuepfen bestätigten iNat-Konto der postenden Person gehören.
  • Beobachtungen: jede gezählte Beobachtung einmal (Duplikat-Links zählen nicht doppelt).
  • Arten: verschiedene Taxa. Grobe Bestimmungen (Gattung, Untergattung, Tribus,
    Unterfamilie, Familie) zählen nur, wenn die Person in diesem Taxon nicht schon
    eine genauere Bestimmung hat (z. B. „Lasius“ zählt nicht, wenn „Lasius niger“ da ist).
  • Jahr = Beobachtungsdatum (observed_on), ersatzweise Postdatum.
Bestimmungen werden regelmäßig aktualisiert, aber nur für Beobachtungen, die in den
letzten REFRESH_DAYS Tagen gepostet wurden.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import aiohttp

from utils.db import execute_db, execute_many

logger = logging.getLogger(__name__)

API = "https://api.inaturalist.org/v1"
API_V2 = "https://api.inaturalist.org/v2"
UA = {"User-Agent": "AAM-Discord-Bot (board.jonasants.de)", "Accept": "application/json"}
FORMICOIDEA = 1269340
BATCH = 200                       # max. Beobachtungen pro Anfrage (iNat-Empfehlung)
REFRESH_DAYS = 182                # Bestimmungen bis ~6 Monate zurück aktualisieren
COARSE = {"genus", "subgenus", "tribe", "subtribe", "subfamily", "family", "superfamily"}
CODE_TTL = timedelta(hours=1)

OBS_RE = re.compile(r"https?://(?:www\.)?inaturalist\.org/observations/(\d+)", re.I)
LOGIN_RE = re.compile(r"^[A-Za-z0-9_\-]{2,40}$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── API ───────────────────────────────────────────────────────────────────────
async def _get(path: str, params: dict, base: str = API) -> dict | None:
    """GET auf die iNat-API. None bei Netzwerk-/Serverfehler (später erneut versuchen)."""
    try:
        async with aiohttp.ClientSession(headers=UA) as s:
            async with s.get(f"{base}{path}", params=params,
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 404:
                    return {"results": []}
                if r.status != 200:
                    logger.warning("🐜 iNat-API %s: HTTP %s", path, r.status)
                    return None
                return await r.json()
    except Exception as e:
        logger.warning("🐜 iNat-API %s nicht erreichbar: %s", path, e)
        return None


async def fetch_observations(ids: list[int]) -> dict | None:
    """{obs_id: info} für die angefragten IDs. Fehlende IDs = gelöscht/nicht vorhanden.
    None = API nicht erreichbar."""
    out: dict = {}
    for i in range(0, len(ids), BATCH):
        part = ids[i:i + BATCH]
        d = await _get("/observations", {
            "id": ",".join(str(x) for x in part), "per_page": str(BATCH),
            # Gleiche fields-Syntax wie im früheren Apps Script (dort im Einsatz erprobt)
            "fields": "id,observed_on,user(id,login),taxon(id,name,rank,ancestor_ids)",
        })
        if d is None:
            return None
        for r in d.get("results") or []:
            t = r.get("taxon") or {}
            anc = [int(x) for x in (t.get("ancestor_ids") or [])]
            tid = t.get("id")
            out[int(r["id"])] = {
                "inat_user_id": (r.get("user") or {}).get("id"),
                "inat_login": ((r.get("user") or {}).get("login") or "").lower(),
                "observed_on": r.get("observed_on") or None,
                "taxon_id": tid,
                "taxon_name": t.get("name"),
                "taxon_rank": t.get("rank"),
                "ancestors": anc,
                "is_ant": bool(tid) and (tid == FORMICOIDEA or FORMICOIDEA in anc),
            }
        if i + BATCH < len(ids):
            await asyncio.sleep(1)
    return out


async def find_user(login: str) -> dict | None:
    """{'id', 'login', 'name', 'description'} oder {} wenn unbekannt; None = API nicht erreichbar.
    Die Profil-Bio liefert nur die API v2 mit fields=description (v1 kennt das Feld nicht;
    beides auf dem Server geprüft). Suche per Login über v1 (autocomplete)."""
    d = await _get("/users/autocomplete", {"q": login, "per_page": "10"})
    if d is None:
        return None
    for u in d.get("results") or []:
        if (u.get("login") or "").lower() == login.lower():
            d2 = await _get(f"/users/{u['id']}", {"fields": "id,login,name,description"}, base=API_V2)
            if d2 is None:
                return None                       # v2 nicht erreichbar → später erneut prüfen
            desc = ((d2.get("results") or [{}])[0]).get("description")
            return {"id": u["id"], "login": u["login"], "name": u.get("name") or "",
                    "description": desc or ""}
    return {}


# ── Verknüpfung ───────────────────────────────────────────────────────────────
async def account_of(bot, discord_id) -> dict | None:
    r = await execute_db(bot, "SELECT inat_user_id, inat_login FROM inat_accounts WHERE discord_id=?",
                         (str(discord_id),), fetch=True)
    return dict(r[0]) if r else None


async def start_link(bot, discord_id, login: str) -> str:
    code = "AAM-" + secrets.token_hex(3).upper()
    await execute_db(bot, "INSERT INTO inat_link_codes(discord_id, inat_login, code, created_at) "
                          "VALUES (?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET "
                          "inat_login=excluded.inat_login, code=excluded.code, created_at=excluded.created_at",
                     (str(discord_id), login.lower(), code, _now()), commit=True)
    return code


async def pending_link(bot, discord_id) -> dict | None:
    r = await execute_db(bot, "SELECT inat_login, code, created_at FROM inat_link_codes WHERE discord_id=?",
                         (str(discord_id),), fetch=True)
    if not r:
        return None
    row = dict(r[0])
    if datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) \
            < datetime.now(timezone.utc) - CODE_TTL:
        return None
    return row


async def verify_link(bot, discord_id) -> tuple[str, dict | None]:
    """Prüft den Code im iNat-Profil. Rückgabe (status, user):
    ok | no_pending | api | unknown_user | code_missing | taken"""
    p = await pending_link(bot, discord_id)
    if not p:
        return "no_pending", None
    u = await find_user(p["inat_login"])
    if u is None:
        return "api", None
    if not u:
        return "unknown_user", None
    # Code muss in der Bio stehen (nur über API v2 abrufbar)
    if p["code"].lower() not in (u.get("description") or "").lower():
        return "code_missing", u
    other = await execute_db(bot, "SELECT discord_id FROM inat_accounts WHERE inat_user_id=? AND discord_id<>?",
                             (u["id"], str(discord_id)), fetch=True)
    if other:
        return "taken", u
    await execute_db(bot, "INSERT INTO inat_accounts(discord_id, inat_user_id, inat_login, verified_at) "
                          "VALUES (?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET "
                          "inat_user_id=excluded.inat_user_id, inat_login=excluded.inat_login, "
                          "verified_at=excluded.verified_at",
                     (str(discord_id), u["id"], u["login"].lower(), _now()), commit=True)
    await execute_db(bot, "DELETE FROM inat_link_codes WHERE discord_id=?", (str(discord_id),), commit=True)
    return "ok", u


async def unlink(bot, discord_id) -> bool:
    r = await execute_db(bot, "SELECT 1 FROM inat_accounts WHERE discord_id=?", (str(discord_id),), fetch=True)
    await execute_db(bot, "DELETE FROM inat_accounts WHERE discord_id=?", (str(discord_id),), commit=True)
    return bool(r)


# ── Beobachtungen speichern / aktualisieren ───────────────────────────────────
async def store_posts(bot, discord_id, message_id, obs_ids: list[int]) -> list[int]:
    """Merkt sich neu gepostete Links. Gibt die wirklich neuen IDs zurück."""
    if not obs_ids:
        return []
    q = ",".join("?" * len(obs_ids))
    have = {r["obs_id"] for r in await execute_db(
        bot, f"SELECT obs_id FROM inat_obs WHERE obs_id IN ({q})", tuple(obs_ids), fetch=True) or []}
    new = [o for o in dict.fromkeys(obs_ids) if o not in have]
    await execute_many(bot, "INSERT OR IGNORE INTO inat_obs(obs_id, discord_id, message_id, posted_at) "
                            "VALUES (?,?,?,?)",
                       [(o, str(discord_id), str(message_id), _now()) for o in new])
    return new


async def update_obs(bot, obs_ids: list[int]) -> bool:
    """Holt Bestimmung/Besitzer von der API und schreibt sie zurück. False = API down."""
    if not obs_ids:
        return True
    data = await fetch_observations(obs_ids)
    if data is None:
        return False
    rows, gone = [], []
    for o in obs_ids:
        info = data.get(o)
        if info is None:
            gone.append((_now(), o))
            continue
        rows.append((info["inat_user_id"], info["observed_on"], info["taxon_id"], info["taxon_name"],
                     info["taxon_rank"], json.dumps(info["ancestors"]), int(info["is_ant"]), _now(), o))
    await execute_many(bot, "UPDATE inat_obs SET inat_user_id=?, observed_on=?, taxon_id=?, taxon_name=?, "
                            "taxon_rank=?, ancestors=?, is_ant=?, checked_at=?, missing=0 WHERE obs_id=?", rows)
    await execute_many(bot, "UPDATE inat_obs SET checked_at=?, missing=1 WHERE obs_id=?", gone)
    return True


async def refresh(bot, full: bool = False) -> int:
    """Ungeprüfte immer, bei full alle der letzten REFRESH_DAYS Tage neu abrufen."""
    since = (datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT obs_id FROM inat_obs WHERE checked_at IS NULL"
    if full:
        sql += " OR posted_at >= ?"
    rows = await execute_db(bot, sql, (since,) if full else (), fetch=True) or []
    ids = [r["obs_id"] for r in rows]
    ok = await update_obs(bot, ids)
    return len(ids) if ok else -1


async def obs_state(bot, obs_id: int, discord_id) -> str:
    """counted | foreign | not_ant | unlinked | pending | missing"""
    r = await execute_db(bot, "SELECT inat_user_id, is_ant, checked_at, missing FROM inat_obs WHERE obs_id=?",
                         (obs_id,), fetch=True)
    if not r or r[0]["checked_at"] is None:
        return "pending"
    o = dict(r[0])
    if o["missing"]:
        return "missing"
    if not o["is_ant"]:
        return "not_ant"
    acc = await account_of(bot, discord_id)
    if not acc:
        return "unlinked"
    return "counted" if o["inat_user_id"] == acc["inat_user_id"] else "foreign"


# ── Auswertung ────────────────────────────────────────────────────────────────
async def _counted_rows(bot, year: int | None = None) -> list[dict]:
    rows = await execute_db(
        bot,
        "SELECT o.obs_id, o.discord_id, o.taxon_id, o.taxon_name, o.taxon_rank, o.ancestors, "
        "COALESCE(o.observed_on, substr(o.posted_at,1,10)) AS day "
        "FROM inat_obs o JOIN inat_accounts a ON a.discord_id=o.discord_id "
        "AND a.inat_user_id=o.inat_user_id WHERE o.is_ant=1 AND o.missing=0", fetch=True) or []
    out = [dict(r) for r in rows]
    if year:
        out = [r for r in out if (r["day"] or "")[:4] == str(year)]
    return out


def _taxa(rows: list[dict]) -> list[dict]:
    """Gezählte Taxa einer Person nach der Ausstech-Regel (grobe Ränge nur ohne Feineres)."""
    fine, coarse = {}, {}
    for r in rows:
        if not r["taxon_id"]:
            continue
        tgt = coarse if (r["taxon_rank"] or "") in COARSE else fine
        tgt.setdefault(r["taxon_id"], r)
    covered = set()
    for r in fine.values():
        covered.update(json.loads(r["ancestors"] or "[]"))
    return list(fine.values()) + [r for tid, r in coarse.items() if tid not in covered]


async def user_stats(bot, discord_id, year: int | None = None) -> dict:
    rows = [r for r in await _counted_rows(bot, year) if r["discord_id"] == str(discord_id)]
    taxa = _taxa(rows)
    return {"obs": len(rows), "species": len(taxa),
            "taxa": sorted(r["taxon_name"] for r in taxa if r["taxon_name"])}


async def ranking(bot, year: int | None = None) -> list[dict]:
    """[{discord_id, species, obs}] sortiert nach Arten, dann Beobachtungen."""
    per: dict = {}
    for r in await _counted_rows(bot, year):
        per.setdefault(r["discord_id"], []).append(r)
    out = [{"discord_id": d, "species": len(_taxa(rs)), "obs": len(rs)} for d, rs in per.items()]
    out.sort(key=lambda x: (-x["species"], -x["obs"]))
    return out


async def novelty(bot, obs_id: int, discord_id) -> tuple[bool, bool]:
    """(neu für die Person, neu für die Community) – nur Bestimmungen bis Artebene."""
    r = await execute_db(bot, "SELECT taxon_id, taxon_rank FROM inat_obs WHERE obs_id=?", (obs_id,), fetch=True)
    if not r or not r[0]["taxon_id"] or (r[0]["taxon_rank"] or "") in COARSE:
        return False, False
    tid = r[0]["taxon_id"]
    rows = [x for x in await _counted_rows(bot) if x["taxon_id"] == tid and x["obs_id"] != obs_id]
    return (not any(x["discord_id"] == str(discord_id) for x in rows)), (not rows)


async def community_year(bot, year: int) -> dict:
    """Community-Summen eines Jahres für den Jahresrückblick (ohne Namen):
    Beobachtungen, Arten (gleiche Ausstech-Regel wie je Person), meistbeobachtete Art,
    erstmals in der Community gefundene Arten und Zahl der Mitmachenden."""
    allrows = await _counted_rows(bot)
    rows = [r for r in allrows if (r["day"] or "")[:4] == str(year)]
    fine = [r for r in rows if r["taxon_id"] and (r["taxon_rank"] or "") not in COARSE]
    cnt: dict = {}
    for r in fine:
        cnt.setdefault(r["taxon_id"], [r["taxon_name"], 0])[1] += 1
    top = max(cnt.values(), key=lambda x: x[1]) if cnt else None
    first: dict = {}
    for r in allrows:
        if r["taxon_id"] and (r["taxon_rank"] or "") not in COARSE:
            d = r["day"] or ""
            if r["taxon_id"] not in first or d < first[r["taxon_id"]]:
                first[r["taxon_id"]] = d
    return {"obs": len(rows), "species": len(_taxa(rows)),
            "top": (top[0], top[1]) if top and top[1] > 1 else None,
            "new": sum(1 for d in first.values() if d[:4] == str(year)),
            "people": len({r["discord_id"] for r in rows})}
