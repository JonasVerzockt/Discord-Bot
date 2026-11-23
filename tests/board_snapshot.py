# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/board_snapshot.py – rendert alle GET-Seiten des Boards offline (ohne Discord)
und speichert das (normalisierte) HTML/JSON als Snapshot.

Zweck: Sicherheitsnetz für Umbauten am Board. Snapshot vor und nach einer Änderung
erzeugen und vergleichen – identische Ausgabe = kein sichtbares Verhalten verändert.

  python tests/board_snapshot.py <ausgabe-ordner>
  BOARD_TEST_SHEET_XLSX=/pfad/sheet.xlsx python tests/board_snapshot.py <ausgabe-ordner>   # mit Sheet-Daten
  python tests/board_snapshot.py --compare <ordner-vorher> <ordner-nachher>

Der Bot wird durch einen minimalen Fake ersetzt; alle Daten liegen in einem
temporären Ordner (Test-DBs + synthetische shops_data.json).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("BOARD_SNAPSHOT_ROOT") or Path(__file__).resolve().parent.parent).resolve()

OWNER_ID = "111111111111111111"
MEMBER_ID = "222222222222222222"
SALT = "snapshot-test-salt"


def _setup_env(tmp: Path) -> None:
    os.environ.update({
        "DATA_DIR": str(tmp), "DATA_DIRECTORY": str(tmp),
        "DB_FILE": str(tmp / "bot.db"), "BOARD_DB_FILE": str(tmp / "board.db"),
        "SHOPS_DATA_FILE": str(tmp / "shops_data.json"),
        "BOARD_ENABLED": "true", "MAP_ENABLED": "true", "BOARD_AI_CHAT": "true",
        "BOARD_OWNER_ID": OWNER_ID, "BOARD_HASH_SALT": SALT,
        "BOARD_PUBLIC_URL": "https://board.example", "BOARD_OAUTH_CLIENT_ID": "1",
        "BOARD_OAUTH_REDIRECT_URI": "https://board.example/map/callback",
        "SWARM_CHANNEL_ID": "333", "MAP_GUILD_ID": "444", "SWARM_FORECAST": "true",
        "SWARM_OSM_GEOCODING": "false", "SWARM_WEATHER": "false",
        "DISCORD_TOKEN": "x", "ANTHROPIC_API_KEY": "", "GOOGLE_SPREADSHEET_ID": "offline-test",
    })


def _shops_data() -> dict:
    def prod(pid, species, title, price, stock=True, variants=()):
        return {"id": pid, "species": species, "canonical_species": species, "title": title,
                "min_price": price, "max_price": price + 5, "currency_iso": "EUR",
                "in_stock": stock, "is_active": True, "antcheck_url": f"https://antcheck.info/p/{pid}",
                "shop_url": f"https://shop.example/p/{pid}",
                "variants": [{"title": v, "price": price + i, "currency_iso": "EUR",
                              "in_stock": True, "is_active": True} for i, v in enumerate(variants)]}
    return {
        "_meta": {"fetched_at": "2026-11-23T10:00:00+00:00", "shop_count": 2,
                  "product_count": 4, "variant_count": 3},
        "1": {"id": 1, "name": "AntMeUp", "country": "de", "url": "https://antmeup.example",
              "average_rating": 4.5, "products": [
                  prod(11, "Lasius niger", "Lasius niger Kolonie", 15, variants=("Königin", "Kolonie klein")),
                  prod(12, "Camponotus ligniperda", "Camponotus ligniperda", 40),
                  prod(13, "Messor barbarus", "Messor barbarus", 25, stock=False)]},
        "3": {"id": 3, "name": "Antbaron", "country": "de", "url": "https://antbaron.de",
              "products": [prod(31, "Messor structor", "Messor structor", 22)]},
        "2": {"id": 2, "name": "Antstore", "country": "ch", "url": "https://antstore.example",
              "products": [prod(21, "Lasius niger", "Lasius niger Starter", 18, variants=("Set",))]},
    }


def _cell(v) -> str:
    """Excel-Wert → Text wie Google Sheets ihn per get_all_values() liefert."""
    import datetime as _dt
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, _dt.datetime):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class _ReadOnlyWorksheet:
    def __init__(self, title: str, rows: list[list[str]]):
        self.title = title
        self._rows = rows

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def __getattr__(self, name):          # append/update/batch_clear … → bewusst gesperrt
        def _blocked(*a, **k):
            raise RuntimeError(f"Test-Sheet ist schreibgeschützt ({name})")
        return _blocked


class _OfflineSheets:
    """Ersatz für den gspread-Client.

    Ist BOARD_TEST_SHEET_XLSX gesetzt (Excel-Export des AAM-Sheets), werden die Tabs
    NUR LESEND daraus bedient; ohne Datei schlägt jeder Zugriff kontrolliert fehl.
    Die Excel-Datei gehört NICHT ins Repo (enthält Rohdaten mit Discord-Namen)."""
    def __init__(self):
        self._tabs: dict[str, _ReadOnlyWorksheet] = {}
        path = os.environ.get("BOARD_TEST_SHEET_XLSX", "")
        if path and Path(path).is_file():
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            for ws in wb.worksheets:
                rows = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
                while rows and not any(rows[-1]):
                    rows.pop()
                self._tabs[ws.title] = _ReadOnlyWorksheet(ws.title, rows)
            wb.close()

    def open_by_key(self, key):
        if not self._tabs:
            raise RuntimeError("Google Sheets offline (Test, keine Excel-Datei)")
        return self

    def worksheets(self):
        return list(self._tabs.values())

    def worksheet(self, title):
        if title not in self._tabs:
            raise RuntimeError(f"Tab {title!r} fehlt im Test-Sheet")
        return self._tabs[title]


class FakeMember:
    def __init__(self, uid, name):
        self.id = int(uid); self.display_name = name; self.name = name.lower()
        self.mention = f"<@{uid}>"
        self.joined_at = None


class FakeGuild:
    """AAM-Server mit zwei Mitgliedern (Owner + Mitglied) – macht „eingeloggt + Mitglied“ testbar."""
    id = 444
    name = "AAM Test"

    def __init__(self):
        self._m = {int(OWNER_ID): FakeMember(OWNER_ID, "Owner"),
                   int(MEMBER_ID): FakeMember(MEMBER_ID, "Mitglied")}
        self.members = list(self._m.values())
        self.member_count = len(self.members)

    def get_member(self, uid):
        return self._m.get(int(uid))


class FakeBot:
    """Minimaler Ersatz für discord.Bot – nur, was das Board liest."""
    def __init__(self, loop):
        self.loop = loop
        self.cogs = {}
        self.latency = 0.042
        self.user = None
        self._guild = FakeGuild()
        self.guilds = [self._guild]

    def get_cog(self, name):
        return None

    def get_guild(self, gid):
        return self._guild if int(gid) == FakeGuild.id else None

    def get_channel(self, cid):
        return None

    def get_user(self, uid):
        return None

    def is_ready(self):
        return True

    async def fetch_user(self, uid):
        raise RuntimeError("offline")


def _sig(*parts: str) -> str:
    return hmac.new(SALT.encode(), "|".join(parts).encode(), hashlib.sha3_512).hexdigest()


EXP = (int(time.time()) // 86400 + 3) * 86400      # fester Ablauf je Lauf (→ stabile Tokens)


def _admin_csrf() -> str:
    return _sig("csrf", OWNER_ID, str(EXP))


def _member_csrf(uid: str) -> str:
    return _sig("mcsrf", str(uid))[:32]


def _cookies(role: str) -> dict:
    exp = EXP
    if role == "admin":
        return {"board_admin": f"{OWNER_ID}.{exp}.{_sig('admin', OWNER_ID, str(exp))}",
                "board_member": f"{OWNER_ID}.{exp}.{_sig('member', OWNER_ID, str(exp))}"}
    if role == "member":
        return {"board_member": f"{MEMBER_ID}.{exp}.{_sig('member', MEMBER_ID, str(exp))}"}
    return {}


PATHS = [
    "/", "/?type=bug", "/?status=done", "/stats", "/art", "/art?g=Lasius", "/art?stock=1&pmin=10&pmax=30",
    "/art/Lasius%20niger", "/art/Unbekannt%20art", "/inat", "/shops", "/shop/antmeup.example",
    "/shop/unbekannt.example", "/shop/antbaron.de", "/rueckblick", "/rueckblick/2025", "/impressum", "/datenschutz",
    "/partner", "/gewinnspiel", "/status.json", "/status/check/shops", "/status/check/grabber",
    "/submit", "/submission/1", "/submission/999", "/admin/login", "/admin", "/admin/1/edit",
    "/map", "/map/regions.json", "/map/pins.json", "/map/list.json", "/map/events.json",
    "/map/stats.json", "/map/events.ics", "/map/events/1.ics", "/map/events/2.ics", "/intro.json", "/hilfe", "/schwarmflug",
    "/schwarmflug/status.json", "/admin/schwarmflug", "/konto", "/konto/daten.json",
    "/chat/models", "/static/stats.js", "/static/map.js", "/favicon.ico", "/nicht-vorhanden",
]

# Werte, die sich bei jedem Lauf ändern (Zeit, Zufall, Systemlast) → Platzhalter
_NORMALIZE = [
    (re.compile(r"\d{2}\.\d{2}\.\d{4},? \d{2}:\d{2}(:\d{2})?( (MEZ|MESZ|UTC))?"), "<DT>"),
    (re.compile(r"\b\d{2}:\d{2}(:\d{2})? (MEZ|MESZ|UTC)"), "<T>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(\+00:00|Z)?"), "<ISO>"),
    (re.compile(r"\b\d{8}T\d{6}Z?\b"), "<ICSDT>"),
    (re.compile(r"[0-9a-f]{64,128}"), "<HEX>"),
    (re.compile(r"\b1[78]\d{8}\b"), "<EPOCH>"),
    (re.compile(r"(\?v=)[\w.-]+"), r"\1<V>"),
    (re.compile(r"(\d+(\.\d+)?) ?(%|GB|MB|s|ms|Tage?|Std\.|Min\.|h|min|days?|hours?)\b"), r"<N>\3"),
]


def normalize(text: str) -> str:
    for rx, rep in _NORMALIZE:
        text = rx.sub(rep, text)
    return text


async def _seed(bot) -> None:
    from utils.db import init_db, execute_db
    from utils.board_db import board_init, board_exec
    await init_db(bot)
    await board_init()
    await board_exec("INSERT INTO board_submissions (type,title,body,status) VALUES (?,?,?,?)",
                     ("bug", "Offers leer", "AntMeUp zeigt nichts", "open"))
    await board_exec("INSERT INTO board_submissions (type,title,body,status) VALUES (?,?,?,?)",
                     ("idea", "Dark mode", "bitte", "pending"))
    await board_exec("INSERT INTO board_comments (submission_id,body) VALUES (?,?)", (1, "Ist behoben"))
    await execute_db(bot, "INSERT INTO shops (id,name,country,url,average_rating) VALUES (1,'AntMeUp','de','https://antmeup.example',4.5)", commit=True)
    await execute_db(bot, "INSERT INTO map_entries (user_id,country,region_code,region_name,plz_prefix,"
                          "lat_fuzzed,lon_fuzzed,first_name,show_entry,show_name,age_ok,contact_ok) "
                          "VALUES (?,?,?,?,?,?,?,?,1,1,1,1)",
                     (MEMBER_ID, "de", "DE-BY", "Bayern", "80", 48.14, 11.58, "Max"), commit=True)
    await execute_db(bot, "INSERT INTO map_events (title,type,country,lat,lon,venue,plz,start_at,end_at,"
                          "status,submitted_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("Ameisen-Stammtisch", "meetup", "de", 48.1, 11.5, "Biergarten", "80331",
                      "2027-03-06T18:00", "2027-03-06T22:00", "approved", MEMBER_ID), commit=True)
    await execute_db(bot, "INSERT INTO map_events (title,type,country,start_at,rrule,status,all_day) "
                          "VALUES (?,?,?,?,?,?,1)",
                     ("Börse (monatlich)", "fair", "at", "2027-01-10T00:00", "FREQ=MONTHLY;COUNT=6",
                      "approved"), commit=True)
    # Sheet-Caches befüllen (wie die Hintergrund-Jobs) – mit Excel-Datei echte Daten, sonst leer
    from utils.sheet import sync_ratings_from_sheet, sync_warnings_from_sheet
    from utils.sheets_shop_data import refresh as refresh_shop_block
    for fn in (sync_ratings_from_sheet, sync_warnings_from_sheet):
        try:
            print(f"  {fn.__name__}: {await fn(bot)}")
        except Exception as e:
            print(f"  {fn.__name__}: übersprungen ({e})")
    try:
        print(f"  shop_data refresh: {refresh_shop_block()}")
    except Exception as e:
        print(f"  shop_data refresh: übersprungen ({e})")


def _posts() -> list:
    """(rolle, pfad, formular, header) – deckt alle POST-Handler mit typischen Eingaben ab."""
    a = {"csrf": _admin_csrf()}
    m = {"csrf": _member_csrf(MEMBER_ID)}
    o = {"csrf": _member_csrf(OWNER_ID)}
    cap = {"captcha": "7", "captcha_sig": _sig("captcha", "7")}
    mapcsrf = {"X-Map-CSRF": _member_csrf(MEMBER_ID)}
    P = [
        ("guest", "/submit", {"title": "", "body": "x"}, None),
        ("guest", "/submit", {"title": "Spam", "website": "http://spam"}, None),
        ("guest", "/submit", {"type": "bug", "title": "Neuer Bug", "body": "Details", "submitter_name": "Gast"}, None),
        ("guest", "/upvote/1", {}, None),
        ("guest", "/upvote/1", {}, None),
        ("member", "/upvote/3", {}, None),
        ("guest", "/impressum/contact", {"name": "A", "email": "a@b.c", "message": "Hallo", **cap}, None),
        ("guest", "/impressum/contact", {"name": "A", "message": "Hallo", "captcha": "8", "captcha_sig": "x"}, None),
        ("guest", "/admin/1/status", {"status": "done", **a}, None),
        ("admin", "/admin/2/approve", a, None),
        ("admin", "/admin/1/status", {"status": "in_progress", **a}, None),
        ("admin", "/admin/1/comment", {"body": "Wird untersucht", "author": "Owner", **a}, None),
        ("admin", "/admin/1/edit", {"title": "Offers leer (AntMeUp)", "body": "neu", "type": "bug",
                                    "status": "open", "component": "", "priority": "high", "version": "4.2.5", **a}, None),
        ("admin", "/admin/comment/1/delete", a, None),
        ("admin", "/admin/import", {"file": "type;title;body\nidea;Import-Idee;Text\n", **a}, None),
        ("admin", "/admin/3/reject", a, None),
        ("admin", "/admin/3/delete", a, None),
        ("admin", "/admin/1/status", {"status": "done", "csrf": "falsch"}, None),
        ("admin", "/status/incident/1/note", {"note": "Notiz", **a}, None),
        ("admin", "/admin/schwarmflug/fix", {"mid": "1", "where": "DE-BY", **a}, None),
        ("admin", "/admin/schwarmflug/names", {"name": "Lasius", "taxa": "Lasius niger", **a}, None),
        ("member", "/map/events/1/rsvp", {}, mapcsrf),
        ("member", "/map/events/1/rsvp", {}, {"X-Map-CSRF": "falsch"}),
        ("guest", "/map/events/1/rsvp", {}, mapcsrf),
        ("member", "/konto/einstellungen", {"language": "en", "digest": "1", "quiet": "22-7", **m}, None),
        ("member", "/konto/einstellungen", {"language": "", **m}, None),
        ("member", "/konto/blacklist/neu", {"shop": "1", **m}, None),
        ("member", "/konto/blacklist/entfernen", {"shop": "1", **m}, None),
        ("member", "/konto/schlagwort/neu", {"kw": "Camponotus", **m}, None),
        ("member", "/konto/schlagwort/entfernen", {"id": "1", **m}, None),
        ("member", "/konto/benachrichtigung/neu", {"species": "Lasius niger", "region": "de", **m}, None),
        ("member", "/konto/benachrichtigung", {"do": "pause", "id": "1", **m}, None),
        ("member", "/konto/benachrichtigung", {"do": "resume", "id": "1", **m}, None),
        ("member", "/konto/beobachtung/neu", {"species": "Lasius niger", "force": "1", **m}, None),
        ("member", "/konto/beobachtung/entfernen", {"species": "Lasius niger", **m}, None),
        ("member", "/konto/preis/neu", {"pid": "11", "vid": "", **m}, None),
        ("member", "/konto/zielpreis", {"pid": "11", "price": "12,50", **m}, None),
        ("member", "/konto/preis/entfernen", {"pid": "11", **m}, None),
        ("member", "/konto/schwarm/neu", {"taxon": "Lasius", "country": "de", "region": "DE-BY", **m}, None),
        ("member", "/konto/schwarm/prognose", {"level": "2", **m}, None),
        ("member", "/konto/schwarm/entfernen", {"id": "1", **m}, None),
        ("member", "/konto/kalender/neu", m, None),
        ("member", "/konto/karte/tags", {"tag": "lasius", **m}, None),
        ("member", "/konto/termin/absagen", {"eid": "1", "occ": "2027-03-06", **m}, None),
        ("member", "/konto/unbekannt", m, None),
        ("member", "/konto/einstellungen", {"language": "de", "csrf": "falsch"}, None),
        ("admin", "/konto/karte/loeschen", {"sure": "1", **o}, None),
        ("member", "/konto/loeschen", {"area": "map", "sure": "1", **m}, None),
        ("member", "/konto/abmelden", m, None),
        ("member", "/konto/einstellungen", {"language": "de", **m}, None),
    ]
    return P


async def run(out: Path) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="board-snap-"))
    _setup_env(tmp)
    (tmp / "shops_data.json").write_text(json.dumps(_shops_data()), encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    random.seed(1)
    # Google Sheets offline: Client-Erzeugung ersetzen → utils.sheet lädt mit leeren Caches.
    import gspread
    gspread.service_account = lambda *a, **k: _OfflineSheets()
    # Zufalls-Tokens reproduzierbar (z. B. Kalender-Salt in „Mein Konto“)
    import secrets as _secrets
    _rng = random.Random(42)
    _secrets.token_hex = lambda n=32: "".join(_rng.choice("0123456789abcdef") for _ in range(2 * n))
    _secrets.token_urlsafe = lambda n=32: "".join(_rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))
    # Wechselkurse fest vorgeben (kein Netzabruf)
    from datetime import datetime, timezone
    import utils.currency as _cur
    _cur._RATES = {"EUR": 1.0, "CHF": 1.05, "USD": 0.92, "GBP": 1.17}
    _cur._LAST_FETCH = datetime.now(timezone.utc)
    _cur._fetch_rates_sync = lambda: None
    # Systemmetriken einfrieren (Status-Seite/-JSON)
    import psutil
    from collections import namedtuple
    _Mem = namedtuple("_Mem", "total available percent used free")
    _Disk = namedtuple("_Disk", "total used free percent")
    os.getloadavg = lambda: (0.5, 0.4, 0.3)
    psutil.cpu_percent = lambda *a, **k: 12.5
    psutil.cpu_count = lambda *a, **k: 4
    psutil.virtual_memory = lambda: _Mem(8 << 30, 4 << 30, 50.0, 4 << 30, 4 << 30)
    psutil.disk_usage = lambda p: _Disk(100 << 30, 40 << 30, 60 << 30, 40.0)

    from aiohttp.test_utils import TestServer, TestClient
    try:
        from web import build_app            # nach dem Umbau
    except ImportError:
        from cogs.board import build_app     # vor dem Umbau

    loop = asyncio.get_running_loop()
    bot = FakeBot(loop)
    await _seed(bot)
    app = build_app(bot)
    out.mkdir(parents=True, exist_ok=True)
    index = {}
    async with TestClient(TestServer(app)) as client:
        try:
            from web.core.security import _hits      # nach dem Umbau
        except ImportError:
            from cogs.board import _hits             # vor dem Umbau
        for role in ("guest", "member", "admin"):
            for lang in ("de", "en", "eo"):
                _hits.clear()                          # Rate-Limits je Durchgang zurücksetzen
                for path in PATHS:
                    sep = "&" if "?" in path else "?"
                    url = f"{path}{sep}lang={lang}"
                    random.seed(1)
                    resp = await client.get(url, cookies=_cookies(role), allow_redirects=False)
                    body = await resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                    if ctype.startswith(("text/", "application/json", "application/javascript")):
                        text = normalize(body.decode("utf-8", "replace"))
                    else:
                        text = f"<binary {len(body)} bytes sha256={hashlib.sha256(body).hexdigest()}>"
                    loc = resp.headers.get("Location", "")
                    key = f"{role} {url}"
                    fname = hashlib.sha1(key.encode()).hexdigest()[:16] + ".txt"
                    (out / fname).write_text(text, encoding="utf-8")
                    index[key] = {"status": resp.status, "location": normalize(loc),
                                  "ctype": ctype.split(";")[0], "file": fname}
        # ── Aktionen (POST) – Reihenfolge zählt, der DB-Zustand baut aufeinander auf ──
        _hits.clear()
        for i, (role, path, form, headers) in enumerate(_posts()):
            random.seed(1)
            resp = await client.post(path, data=form, headers=headers or {},
                                     cookies=_cookies(role), allow_redirects=False)
            body = normalize((await resp.read()).decode("utf-8", "replace"))
            key = f"POST#{i:02d} {role} {path}"
            fname = hashlib.sha1(key.encode()).hexdigest()[:16] + ".txt"
            (out / fname).write_text(body, encoding="utf-8")
            index[key] = {"status": resp.status, "location": normalize(resp.headers.get("Location", "")),
                          "ctype": resp.headers.get("Content-Type", "").split(";")[0], "file": fname}
    # ── Datenbank-Inhalt nach allen Aktionen ──
    import sqlite3
    for dbname in ("bot.db", "board.db"):
        con = sqlite3.connect(tmp / dbname)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        lines = []
        for t in tables:
            rows = con.execute(f'SELECT * FROM "{t}"').fetchall()
            for r in sorted(normalize(json.dumps(list(r), ensure_ascii=False, default=str)) for r in rows):
                lines.append(f"{t}: {r}")
        con.close()
        key = f"DB {dbname}"
        fname = hashlib.sha1(key.encode()).hexdigest()[:16] + ".txt"
        (out / fname).write_text("\n".join(lines), encoding="utf-8")
        index[key] = {"status": 0, "location": "", "ctype": "db", "file": fname}
    (out / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    codes = {}
    for v in index.values():
        codes[v["status"]] = codes.get(v["status"], 0) + 1
    print(f"{len(index)} Anfragen → {out}  Statuscodes: {codes}")
    return 0


def compare(a: Path, b: Path) -> int:
    ia = json.loads((a / "index.json").read_text())
    ib = json.loads((b / "index.json").read_text())
    diffs = 0
    for key in sorted(set(ia) | set(ib)):
        va, vb = ia.get(key), ib.get(key)
        if va is None or vb is None:
            print("FEHLT", "vorher" if va is None else "nachher", key); diffs += 1; continue
        ta = (a / va["file"]).read_text(); tb = (b / vb["file"]).read_text()
        meta_a = {k: va[k] for k in ("status", "location", "ctype")}
        meta_b = {k: vb[k] for k in ("status", "location", "ctype")}
        if meta_a != meta_b or ta != tb:
            diffs += 1
            print(f"DIFF {key}: {meta_a} vs {meta_b}" if meta_a != meta_b else f"DIFF {key}: Inhalt")
    print(f"{len(ia)} vorher / {len(ib)} nachher, Unterschiede: {diffs}")
    return 1 if diffs else 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--compare":
        sys.exit(compare(Path(sys.argv[2]), Path(sys.argv[3])))
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(2)
    sys.exit(asyncio.run(run(Path(sys.argv[1]))))
