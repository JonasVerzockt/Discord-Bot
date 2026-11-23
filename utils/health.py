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
utils/health.py – Health-Checks & Alarme: Status-Kacheln, Systemmetriken, Vorfall-Historie, Owner-DMs.

Genutzt vom Board (Status-Seite, Monitor-Loop) und von /bot status.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import os
import time
import psutil
from pathlib import Path
import discord
from datetime import datetime, timezone
from utils.board_db import board_exec, board_one, board_query
from utils.db import execute_db
from utils.timez import BERLIN, berlin_from_utc_naive
from utils.board_i18n import translate
from config import (
    AI_CHAT_PUBLIC, BOARD_AI_CHAT, BOARD_OWNER_ID, BOARD_PUBLIC_URL, DATA_DIRECTORY,
    SHOPS_DATA_FILE, SPECIES_CATALOG_FILE,
)

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


def _file_age_seconds(path) -> float | None:
    """Alter der Datei in Sekunden (mtime) oder None, wenn sie fehlt/unlesbar ist."""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _fmt_age(sec: float | None) -> str:
    """Menschlich lesbares Alter, z.B. 'vor 2 h 14 min' / 'vor 3 Tagen'."""
    if sec is None:
        return "unbekannt"
    sec = int(sec)
    if sec < 90:
        return "gerade eben"
    m = sec // 60
    if m < 60:
        return f"vor {m} min"
    h = m // 60
    if h < 24:
        rem = m % 60
        return f"vor {h} h {rem} min" if rem else f"vor {h} h"
    d = h // 24
    return f"vor {d} {'Tag' if d == 1 else 'Tagen'}"


def _gb(nbytes) -> str:
    """Bytes -> GB-String mit einer Nachkommastelle."""
    try:
        return f"{nbytes / 1024 ** 3:.1f}"
    except (TypeError, ValueError, ZeroDivisionError):
        return "?"


def _system_metrics() -> dict:
    """Momentane Systemauslastung (anzeige-only, KEIN Incident-Logging, ohne Einfluss
    auf den Gesamtstatus): CPU als Load-Average (1/5/15 min, relativ zu den Kernen),
    RAM- und SSD-Auslastung. Ampel je Metrik. Einzelne Fehler -> None für die Metrik."""
    out: dict = {}
    try:
        la1, la5, la15 = os.getloadavg()                 # nur Linux/Unix
        cores = psutil.cpu_count() or 1
        ratio = la1 / cores
        out["cpu"] = {"state": "ok" if ratio < 0.7 else ("warn" if ratio < 1.0 else "down"),
                      "load": [round(la1, 2), round(la5, 2), round(la15, 2)], "cores": cores}
    except Exception:                                    # z.B. kein getloadavg (Windows)
        out["cpu"] = None
    try:
        vm = psutil.virtual_memory()
        out["ram"] = {"state": "ok" if vm.percent < 75 else ("warn" if vm.percent < 90 else "down"),
                      "percent": round(vm.percent), "used": vm.used, "total": vm.total}
    except Exception:
        out["ram"] = None
    try:
        du = psutil.disk_usage("/")
        out["disk"] = {"state": "ok" if du.percent < 80 else ("warn" if du.percent < 90 else "down"),
                       "percent": round(du.percent), "used": du.used, "total": du.total}
    except Exception:
        out["disk"] = None
    return out


def _metrics_chip(m: dict) -> str:
    """Kompakte, farbige HTML-Zeile (Load · RAM · SSD) für die Status-Übersicht.
    Wird server-seitig gerendert (No-JS-Fallback) und vom 5-s-Poll aktualisiert."""
    _cls = {"ok": "mok", "warn": "mwarn", "down": "mdown"}
    parts = []
    cpu, ram, disk = m.get("cpu"), m.get("ram"), m.get("disk")
    if cpu:
        lo = cpu["load"]
        title = f"1/5/15 min: {lo[0]:.2f} / {lo[1]:.2f} / {lo[2]:.2f} · {cpu['cores']} Kerne"
        parts.append(f'<span class="{_cls.get(cpu["state"], "")}" title="{title}">Load {lo[0]:.2f}</span>')
    if ram:
        parts.append(f'<span class="{_cls.get(ram["state"], "")}" title="{_gb(ram["used"])}/{_gb(ram["total"])} GB">RAM {ram["percent"]}%</span>')
    if disk:
        parts.append(f'<span class="{_cls.get(disk["state"], "")}" title="{_gb(disk["used"])}/{_gb(disk["total"])} GB">SSD {disk["percent"]}%</span>')
    return "⚙️ " + " · ".join(parts) if parts else ""


_WD_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _loop_next(loop) -> str:
    """Nächster geplanter Lauf eines tasks.loop in Berliner Zeit (MEZ/MESZ).

    ``next_iteration`` liefert discord.py UTC-aware; die Umrechnung erfolgt explizit
    nach Europe/Berlin – unabhängig von der Server-Zeitzone. Das Datum wird nur dann
    mitgezeigt, wenn der nächste Lauf NICHT heute ist (sonst nur Uhrzeit), damit bei
    seltenen Jobs (wöchentlich/…) 'HH:MM' nicht mehrdeutig ist:
      heute   → '19:15 MESZ'
      morgen  → 'morgen 09:00 MESZ'
      später  → 'So, 03.08. 09:00 MESZ'"""
    nxt = getattr(loop, "next_iteration", None)
    if nxt is None:
        return ""
    try:
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        local = nxt.astimezone(BERLIN)
        label = "MESZ" if local.dst() else "MEZ"
        days = (local.date() - datetime.now(BERLIN).date()).days
        if days <= 0:
            return f"{local:%H:%M:%S} {label}"
        if days == 1:
            return f"morgen {local:%H:%M:%S} {label}"
        return f"{_WD_DE[local.weekday()]}, {local:%d.%m.} {local:%H:%M:%S} {label}"
    except Exception:
        return ""


def _loop_interval(loop) -> str:
    """Kurzbeschreibung des Loop-Intervalls, z.B. 'alle 65 min' / 'alle 2 h'.
    Für zeitgesteuerte Loops (fester Uhrzeit-Trigger) ''."""
    try:
        # py-cord speichert diese Werte als float → int, damit z.B. 'alle 5 min'
        # statt 'alle 5.0 min' erscheint.
        h = int(getattr(loop, "hours", 0) or 0)
        m = int(getattr(loop, "minutes", 0) or 0)
        s = int(getattr(loop, "seconds", 0) or 0)
        total_min = h * 60 + m
        if total_min:
            if h and not m:
                if h == 1:
                    return "stündlich"
                if h % 24 == 0:
                    d = h // 24
                    return "täglich" if d == 1 else f"alle {d} Tage"
                return f"alle {h} h"
            return f"alle {total_min} min"
        if s:
            return f"alle {s} s"
    except Exception:
        pass
    return ""


# Registry ALLER In-Bot-Hintergrundjobs (discord.ext.tasks-Loops):
# (Cog-Name, Loop-Attribut, Anzeige-Label, kritisch?, Notiz) – kritisch → 'down' bei
# Ausfall, sonst 'warn'. Notiz = optionaler Zusatz (z.B. wenn der Loop öfter tickt als
# er tatsächlich etwas tut).
_BOT_JOBS = [
    ("Tasks",         "check_availability",       "Verfügbarkeits-Check",              True,  ""),
    ("PriceTracking", "flush_removed_variants",   "Entfallene Varianten (Sammel-DM)",  False, ""),
    ("Digest",        "weekly_digest",            "Wochen-Digest",                     False, "Versand nur montags"),
    ("Tasks",         "sync_shop_ratings",        "Shop-Bewertungen synchronisieren",  False, ""),
    ("Tasks",         "expire_old_notifications", "Alte Benachrichtigungen entfernen", False, ""),
    ("Tasks",         "optimize_db",              "DB-Optimierung (VACUUM)",           False, ""),
    ("Tasks",         "update_bot_status",        "Bot-Statusanzeige aktualisieren",   False, ""),
    ("CommandLog",    "flush_log",                "Command-Log schreiben",             False, ""),
    ("CommandLog",    "cleanup_log",              "Command-Log aufräumen (Retention)", False, ""),
    ("OfferAlerts",   "scan_offers",              "Angebote-Schlagwort-Scanner",       False, ""),
    ("AiChatCog",     "cleanup_loop",             "KI-Chat · Verläufe aufräumen",      False, ""),
    ("AiChatCog",     "shop_data_loop",           "KI-Chat · Shop-Daten-Refresh",      False, ""),
]


def _pipeline_tile(bot) -> dict:
    """Kachel für die stündliche Daten-Pipeline (Shop-Reload → Preis → Arten), die
    im Tasks-Cog als ``reload_shops_task`` läuft. Zeigt nächsten Lauf + je Schritt
    das Ergebnis des letzten Laufs (aus ``TasksCog.pipeline_last``)."""
    name = "Daten-Pipeline (Shop-Reload → Preis → Arten)"
    try:
        cog = bot.get_cog("Tasks") if bot else None
        loop = getattr(cog, "reload_shops_task", None) if cog else None
        if loop is None:
            return dict(name=name, state="down", detail="Cog/Loop nicht geladen")
        if loop.failed():
            return dict(name=name, state="down", detail="fehlerhaft (Exception im Loop)")
        if not loop.is_running():
            return dict(name=name, state="down", detail="gestoppt")
        nxt = _loop_next(loop)
        detail = "läuft · stündlich" + (f" · nächster Lauf {nxt}" if nxt else "")
        state = "ok"
        last = getattr(cog, "pipeline_last", None)
        if last and last.get("steps"):
            marks = " · ".join(f"{lbl} {'✓' if ok else '✗'}" for lbl, ok in last["steps"])
            detail += f" · zuletzt {last.get('at', '')}: {marks}"
            if not all(ok for _, ok in last["steps"]):
                state = "warn"
        return dict(name=name, state=state, detail=detail)
    except Exception as e:
        return dict(name=name, state="warn", detail=str(e)[:80])


def _job_tile(bot, cog_name: str, attr: str, label: str, critical: bool, note: str = "") -> dict:
    """Health-Kachel für einen discord.ext.tasks-Loop: läuft / fehlerhaft / gestoppt?
    Bei laufendem Loop zusätzlich Intervall + nächster Lauf (Berliner Zeit) + optionale Notiz."""
    down = "down" if critical else "warn"
    try:
        cog = bot.get_cog(cog_name) if bot else None
        loop = getattr(cog, attr, None) if cog else None
        if loop is None:
            return dict(name=label, state=down, detail="Cog/Loop nicht geladen")
        if loop.failed():
            return dict(name=label, state="down", detail="fehlerhaft (Exception im Loop)")
        if not loop.is_running():
            return dict(name=label, state=down, detail="gestoppt")
        nxt = _loop_next(loop)
        iv = _loop_interval(loop)
        detail = "läuft" + (f" · {iv}" if iv else "") + (f" · nächster Lauf {nxt}" if nxt else "")
        if note:
            detail += f" · {note}"
        return dict(name=label, state="ok", detail=detail)
    except Exception as e:
        return dict(name=label, state="warn", detail=str(e)[:80])


def _grabber_cron_tile() -> dict:
    """EIN Cronjob (stündlich, als Nutzer 'aam') erzeugt in einem Lauf BEIDE Dateien:
    ``shops_data.json`` (jeder Lauf) und ``price_history.db`` (Preis-Historie).
    Deshalb EINE gemeinsame Kachel statt zweier getrennter (es gibt nicht zwei Jobs).

    Ampel-Status nach ``shops_data.json`` (wird jeden Lauf neu geschrieben → verlässlich).
    Die Preis-Historie wird zwar nur bei echten Preisänderungen fortgeschrieben, der
    Grabber ``touch()``t sie aber nach jedem erfolgreichen Lauf – bleibt sie trotzdem
    deutlich zurück (> 7 Tage), deutet das auf einen Ausfall des Preis-Schritts hin → gelb."""
    name = "Grabber · Shop-Daten + Preis-Historie (stündlich)"
    age = _file_age_seconds(SHOPS_DATA_FILE)
    if age is None:
        return dict(name=name, state="down", detail="shops_data.json fehlt")
    state = "ok" if age < 3 * 3600 else ("warn" if age < 24 * 3600 else "down")
    ph = _file_age_seconds(Path(DATA_DIRECTORY) / "price_history.db")
    if ph is None:
        ph_txt = "Preis-Historie fehlt"
    else:
        ph_txt = f"Preis-Historie {_fmt_age(ph)}"
        if ph > 168 * 3600 and state == "ok":
            state = "warn"
            ph_txt += " ⚠️"
    return dict(name=name, state=state, detail=f"Shop-Daten {_fmt_age(age)} · {ph_txt}")


def _cron_tile(name: str, path, *, warn_h: int, down_h: int, optional: bool = False) -> dict:
    """Health-Kachel für einen EXTERNEN Cronjob (läuft als Nutzer 'aam', nicht im
    Bot-Prozess). Status wird aus dem Alter der erzeugten Datei abgeleitet."""
    age = _file_age_seconds(path)
    if age is None:
        if optional:
            return dict(name=name, state="off", detail="noch nicht erzeugt (optional)")
        return dict(name=name, state="down", detail=f"{Path(path).name} fehlt")
    state = "ok" if age < warn_h * 3600 else ("warn" if age < down_h * 3600 else "down")
    return dict(name=name, state=state, detail=f"aktualisiert {_fmt_age(age)}")


def _sheet_tile() -> dict:
    """Google Sheet: wann zuletzt Warnhinweise erfolgreich abgeglichen wurden (stündliche
    Pipeline). Nutzt den Merker aus utils.sheet – kein zusätzlicher Abruf."""
    name = "Google Sheet"
    try:
        from utils.sheet import SYNC_STATUS
    except Exception:
        return dict(name=name, state="off", detail="nicht verfügbar")
    now = time.time()
    # Jeder erfolgreiche Zugriff zählt: Bewertungen beim Start, Warnhinweise stündlich,
    # Shop-Bewertungen alle 48 h.
    labels = {"reviews": "Bewertungen geladen", "warnings": "Warnhinweise abgeglichen",
              "ratings": "Shop-Bewertungen abgeglichen"}
    ok_kind = max(SYNC_STATUS["ok"], key=SYNC_STATUS["ok"].get, default=None)
    ok = SYNC_STATUS["ok"].get(ok_kind) if ok_kind else None
    fail = max(SYNC_STATUS["fail"].values(), default=None)
    if ok is None:
        if now - SYNC_STATUS.get("started", now) < 2 * 3600:
            return dict(name=name, state="ok", detail="noch kein Abgleich seit dem Start")
        return dict(name=name, state="down" if fail else "warn",
                    detail="kein erfolgreicher Abgleich seit dem Start")
    age = now - ok
    state = "ok" if age < 3 * 3600 else ("warn" if age < 24 * 3600 else "down")
    detail = f"{labels.get(ok_kind, 'Zugriff')} {_fmt_age(age)}"
    if fail and fail > ok:
        state = "warn" if state == "ok" else state
        detail += f" · letzter Versuch fehlgeschlagen ({_fmt_age(now - fail)})"
    return dict(name=name, state=state, detail=detail)


async def _ai_cost_tile(bot) -> dict:
    """KI-Kosten heute (UTC) gegen Tagesbudget und 14-Tage-Schnitt."""
    name = "KI-Kosten heute"
    try:
        from config import AI_CHAT_DAILY_BUDGET_USD as budget
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = await execute_db(
            bot, "SELECT date, cost_usd FROM ai_chat_budget WHERE user_id=0 "
                 "AND date >= date('now', '-14 days')", fetch=True) or []
    except Exception as e:
        return dict(name=name, state="off", detail=str(e)[:80])
    cost = sum(r["cost_usd"] for r in rows if r["date"] == today)
    avg = sum(r["cost_usd"] for r in rows if r["date"] != today) / 14
    state, why = "ok", ""
    if budget and cost >= budget:
        state, why = "down", " · Tagesbudget erreicht"
    elif budget and cost >= 0.8 * budget:
        state, why = "warn", " · über 80 % des Tagesbudgets"
    elif avg > 0 and cost >= 3 * avg and cost >= 0.10:
        state, why = "warn", " · deutlich über dem Schnitt"
    eur = lambda v: f"{v:.2f}".replace(".", ",")
    detail = f"{eur(cost)} $ von {eur(budget)} $ · Ø 14 Tage {eur(avg)} $/Tag{why}"
    return dict(name=name, state=state, detail=detail)


def _inat_tile() -> dict:
    """iNaturalist-API: letzter Erfolg / Fehler aus utils.inat.API_STATUS."""
    from utils import inat as I
    ok, err = I.API_STATUS.get("ok_at"), I.API_STATUS.get("err_at")
    if not ok and not err:
        return dict(name="iNaturalist-API", state="off", detail="seit dem Start noch nicht abgefragt")
    if err and (not ok or err > ok):
        return dict(name="iNaturalist-API", state="warn",
                    detail=f"letzter Abruf fehlgeschlagen ({I.API_STATUS.get('err')}) · "
                           f"{berlin_from_utc_naive(err, '%Y-%m-%d %H:%M:%S')}")
    return dict(name="iNaturalist-API", state="ok",
                detail=f"letzter erfolgreicher Abruf {berlin_from_utc_naive(ok, '%Y-%m-%d %H:%M:%S')}")


async def _collect_health(app, lang: str = "de"):
    """Sammelt alle Health-Checks in Sektionen (Kern · In-Bot-Jobs · externe Cronjobs).
    Jeder Check ist gekapselt (ein Fehler bricht die Seite nicht ab). state ∈ ok|warn|down|off;
    'off' (grau) = bewusst deaktiviert/optional und zählt NICHT gegen den Gesamtstatus.
    Rückgabe: (overall, sections). Lokalisiert werden Gesamt-Ampel + Sektions-Titel/-Notizen;
    die einzelnen Kachel-Namen/-Details bleiben Deutsch (dienen als stabile Vorfall-Schlüssel)."""
    bot = app.get("bot")

    # ── Sektion 1: Kern (Verbindung, Datenbanken, Feature-Flags) ──────────────
    core: list[dict] = []
    try:
        if bot is None:
            core.append(dict(name="Discord-Bot", state="down", detail="Bot-Objekt nicht verfügbar"))
        elif not bot.is_ready():
            core.append(dict(name="Discord-Bot", state="warn", detail="verbindet …"))
        else:
            lat = bot.latency  # Sekunden; kann inf/nan sein, bevor der erste Heartbeat kam
            if lat != lat or lat in (float("inf"), 0):
                core.append(dict(name="Discord-Bot", state="warn", detail="online · Latenz unbekannt"))
            else:
                ms = round(lat * 1000)
                core.append(dict(name="Discord-Bot", state="ok" if ms < 500 else "warn",
                                 detail=f"online · {ms} ms Latenz"))
    except Exception as e:
        core.append(dict(name="Discord-Bot", state="down", detail=str(e)[:80]))
    try:
        await execute_db(bot, "SELECT 1", fetch=True)
        core.append(dict(name="Hauptdatenbank", state="ok", detail="erreichbar"))
    except Exception as e:
        core.append(dict(name="Hauptdatenbank", state="down", detail=str(e)[:80]))
    try:
        await board_query("SELECT 1")
        core.append(dict(name="Board-Datenbank", state="ok", detail="erreichbar"))
    except Exception as e:
        core.append(dict(name="Board-Datenbank", state="down", detail=str(e)[:80]))
    core.append(dict(name="KI-Chat (Discord-Kanal)", state="ok" if AI_CHAT_PUBLIC else "off",
                     detail="aktiv" if AI_CHAT_PUBLIC else "deaktiviert"))
    chat_cog = bot is not None and "AiChatCog" in getattr(bot, "cogs", {})
    core.append(dict(name="KI-Chat (Board)", state="ok" if (BOARD_AI_CHAT and chat_cog) else "off",
                     detail="aktiv für eingeloggte Mitglieder" if (BOARD_AI_CHAT and chat_cog) else "deaktiviert"))
    core.append(_inat_tile())
    core.append(_sheet_tile())
    core.append(await _ai_cost_tile(bot))

    # ── Sektion 2: Hintergrund-Jobs IM Bot-Prozess (discord.ext.tasks) ────────
    jobs = [_pipeline_tile(bot)] + [_job_tile(bot, c, a, lbl, crit, note)
                                    for (c, a, lbl, crit, note) in _BOT_JOBS]

    # ── Sektion 3: EXTERNE Cronjobs (laufen als Nutzer 'aam', nicht im Bot) ───
    cron = [
        _grabber_cron_tile(),
        _cron_tile("Artenliste · AntCat-Build (monatlich)", SPECIES_CATALOG_FILE,
                   warn_h=40 * 24, down_h=1000 * 24, optional=True),
    ]

    sections = [
        dict(title=translate(lang, "sec_core"), note=translate(lang, "sec_core_note"), checks=core),
        dict(title=translate(lang, "sec_jobs"), note=translate(lang, "sec_jobs_note"), checks=jobs),
        dict(title=translate(lang, "sec_cron"), note=translate(lang, "sec_cron_note"), checks=cron),
    ]
    states = {c["state"] for sec in sections for c in sec["checks"]}
    if "down" in states:
        overall = ("down", translate(lang, "overall_down"))
    elif "warn" in states:
        overall = ("warn", translate(lang, "overall_warn"))
    else:
        overall = ("ok", translate(lang, "overall_ok"))
    return overall, sections


async def _record_incidents(bot) -> None:
    """Schreibt die Vorfall-Historie fort: pro Check offenen Vorfall öffnen/aktualisieren
    (warn/down) bzw. schließen (ok/off → ended_at). Wird minütlich vom Monitor-Loop
    aufgerufen. 'off' (grau/deaktiviert) zählt wie OK (kein Vorfall)."""
    try:
        _, sections = await _collect_health({"bot": bot})
    except Exception as e:
        logger.warning("⚠️ Incident-Monitor: Health-Erhebung fehlgeschlagen: %s", e)
        return
    for c in (chk for sec in sections for chk in sec["checks"]):
        key, state, detail = c["name"], c["state"], c.get("detail", "")
        try:
            open_row = await board_one(
                "SELECT id, state FROM board_incidents WHERE check_key=? AND ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (key,))
            if state in ("warn", "down"):
                if open_row is None:
                    await board_exec(
                        "INSERT INTO board_incidents (check_key, state, detail) VALUES (?,?,?)",
                        (key, state, detail))
                elif open_row["state"] != state:   # warn<->down: Zustand/Detail aktualisieren
                    await board_exec(
                        "UPDATE board_incidents SET state=?, detail=? WHERE id=?",
                        (state, detail, open_row["id"]))
            elif open_row is not None:             # wieder OK -> Vorfall schließen
                await board_exec(
                    "UPDATE board_incidents SET ended_at=datetime('now') WHERE id=?",
                    (open_row["id"],))
        except Exception as e:
            logger.warning("⚠️ Incident-Monitor: Check '%s' fehlgeschlagen: %s", key, e)


_ALERT_AFTER_MIN = 10      # erst melden, wenn ein Vorfall so lange offen ist
_ALERT_GAP_H = 3           # je Prüfung höchstens eine neue Meldung in diesem Zeitraum
_ALERT_ICON = {"warn": "🟡", "down": "🔴"}


def _parse_utc(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _dur(sec: float) -> str:
    m = int(sec // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} Tagen"


def _status_link(key: str) -> str:
    from urllib.parse import quote as _q
    return f"{BOARD_PUBLIC_URL}/status/check/{_q(key, safe='')}" if BOARD_PUBLIC_URL else ""


async def _owner_dm(bot, text: str) -> bool:
    """DM an BOARD_OWNER_ID. False, wenn (noch) nicht zustellbar → nächste Minute erneut."""
    if not BOARD_OWNER_ID or bot is None or not bot.is_ready():
        return False
    try:
        user = bot.get_user(BOARD_OWNER_ID) or await bot.fetch_user(BOARD_OWNER_ID)
        await user.send(text[:1900])
        return True
    except discord.Forbidden:
        logger.warning("🔔 Alarm-DM blockiert (DMs geschlossen?)")
        return True          # nicht endlos wiederholen
    except Exception as e:
        logger.warning("🔔 Alarm-DM fehlgeschlagen: %s", e)
        return False


async def _process_alerts(bot) -> None:
    """Meldet länger offene Vorfälle per DM, eskaliert gelb→rot und meldet Entwarnung."""
    if not BOARD_OWNER_ID:
        return
    now = datetime.now(timezone.utc)
    rows = await board_query(
        "SELECT i.id, i.check_key, i.state, i.detail, i.started_at, a.notified, a.state_sent "
        "FROM board_incidents i LEFT JOIN board_alerts a ON a.incident_id = i.id "
        "WHERE i.ended_at IS NULL AND i.started_at <= datetime('now', ?)",
        (f"-{_ALERT_AFTER_MIN} minutes",))
    for r in rows:
        key, state = r["check_key"], r["state"]
        since = _parse_utc(r["started_at"])
        dur = _dur((now - since).total_seconds()) if since else "?"
        link = _status_link(key)
        if r["notified"] is None:
            recent = await board_one(
                "SELECT 1 FROM board_alerts WHERE check_key=? AND notified=1 "
                "AND sent_at >= datetime('now', ?)", (key, f"-{_ALERT_GAP_H} hours"))
            if recent:
                await board_exec("INSERT OR IGNORE INTO board_alerts (incident_id, check_key, notified, "
                                 "state_sent) VALUES (?,?,0,?)", (r["id"], key, state))
                continue
            text = (f"{_ALERT_ICON.get(state, '⚠️')} **{key}** ist seit {dur} "
                    f"{'rot' if state == 'down' else 'gelb'}\n{r['detail'] or ''}"
                    + (f"\n{link}" if link else ""))
            if await _owner_dm(bot, text):
                await board_exec("INSERT OR IGNORE INTO board_alerts (incident_id, check_key, notified, "
                                 "state_sent) VALUES (?,?,1,?)", (r["id"], key, state))
        elif r["notified"] == 1 and r["state_sent"] == "warn" and state == "down":
            text = f"🔴 **{key}** ist jetzt rot (seit {dur} gestört)\n{r['detail'] or ''}" + (
                f"\n{link}" if link else "")
            if await _owner_dm(bot, text):
                await board_exec("UPDATE board_alerts SET state_sent='down' WHERE incident_id=?", (r["id"],))
    done = await board_query(
        "SELECT i.id, i.check_key, i.started_at, i.ended_at FROM board_incidents i "
        "JOIN board_alerts a ON a.incident_id = i.id "
        "WHERE a.notified = 1 AND a.recovered_sent = 0 AND i.ended_at IS NOT NULL")
    for r in done:
        a, b = _parse_utc(r["started_at"]), _parse_utc(r["ended_at"])
        dur = _dur((b - a).total_seconds()) if a and b else "?"
        if await _owner_dm(bot, f"✅ **{r['check_key']}** wieder OK nach {dur}"):
            await board_exec("UPDATE board_alerts SET recovered_sent=1 WHERE incident_id=?", (r["id"],))
