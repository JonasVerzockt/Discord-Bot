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
utils/board_i18n.py – Sprachkatalog & Helfer für das Feedback-Board (cogs/board.py).

Drei Sprachen: Deutsch (de, Standard), Englisch (en), Esperanto (eo).
Umschaltung ausschließlich über den URL-Parameter ``?lang=xx`` (KEIN Cookie);
auf jeder Seite steht ein Flaggen-Umschalter im Header. Fällt der Parameter weg,
wird die Browser-Sprache (Accept-Language) herangezogen, sonst Deutsch.

Bewusst NICHT lokalisiert: die einzelnen Health-Kacheln des Status-Panels
(Name + Detailtext). Deren Name dient zugleich als stabiler Schlüssel für die
Vorfall-Historie (board_incidents.check_key) und die Kachel-Links
(/status/check/{key}); eine Übersetzung würde diese Verknüpfung brechen.
Lokalisiert werden hier nur die Gesamt-Ampel, die Sektions-Titel/-Notizen und
alle leserseitigen Texte der Seiten.

Nutzung (in board.py):
    from utils.board_i18n import LANGS, pick_lang, translate, type_label, flash_text
    lang = pick_lang(req)
    t = lambda key, **kw: translate(lang, key, **kw)
"""
from __future__ import annotations

# Reihenfolge = Anzeigereihenfolge der Flaggen im Header.
LANGS = ("de", "en", "eo")
DEFAULT_LANG = "de"

# Flaggen/Labels für den Umschalter. Esperanto hat kein Länder-Emoji – wir nutzen
# das grüne Quadrat (Anlehnung an die Esperanto-Flagge) plus Kürzel.
FLAGS = {
    "de": ("\U0001F1E9\U0001F1EA", "DE"),   # 🇩🇪
    "en": ("\U0001F1EC\U0001F1E7", "EN"),   # 🇬🇧
    "eo": ("\U0001F7E9", "EO"),             # 🟩
}
FLAG_TITLE = {
    "de": "Deutsch",
    "en": "English",
    "eo": "Esperanto",
}


def pick_lang(req) -> str:
    """Ermittelt die Sprache: ?lang= (falls gültig) > Accept-Language > Standard (de)."""
    q = (req.query.get("lang") or "").lower().strip()
    if q in LANGS:
        return q
    accept = (req.headers.get("Accept-Language") or "").lower()
    # Grobe, robuste Auswertung: erste passende Sprache im Header gewinnt.
    for part in accept.replace(" ", "").split(","):
        code = part.split(";")[0].split("-")[0]
        if code in LANGS:
            return code
    return DEFAULT_LANG


# ── Katalog ───────────────────────────────────────────────────────────────────
# Pro Schlüssel ein Dict {de,en,eo}. Platzhalter via str.format (z.B. {n}, {v}).
T: dict[str, dict[str, str]] = {
    # Chrome / Navigation / Footer
    "brand": {"de": "AAM-Bot · Ideen & Bugs", "en": "AAM-Bot · Ideas & Bugs", "eo": "AAM-Bot · Ideoj & Cimoj"},
    "nav_board": {"de": "Board", "en": "Board", "eo": "Tabulo"},
    "nav_submit": {"de": "Einreichen", "en": "Submit", "eo": "Sendi"},
    "nav_stats": {"de": "📊 Statistiken", "en": "📊 Statistics", "eo": "📊 Statistiko"},
    "stats_soon": {"de": "Die Statistik-Seite wird gerade aufgebaut – schau bald wieder vorbei.", "en": "The statistics page is being built – check back soon.", "eo": "La statistika paĝo estas konstruata – revenu baldaŭ."},
    "nav_support": {"de": "💖 Unterstützen", "en": "💖 Support", "eo": "💖 Subteni"},
    "nav_owner": {"de": "Owner", "en": "Owner", "eo": "Posedanto"},
    "nav_admin": {"de": "Admin", "en": "Admin", "eo": "Administro"},
    "nav_logout": {"de": "Logout", "en": "Logout", "eo": "Elsaluti"},
    "nav_login": {"de": "Owner-Login", "en": "Owner login", "eo": "Posedanto-ensaluto"},
    "footer_run": {
        "de": "Dieses Board & der Bot werden privat betrieben. Wer die Serverkosten und die Weiterentwicklung unterstützen möchte:",
        "en": "This board & the bot are run privately. If you'd like to support the server costs and ongoing development:",
        "eo": "Ĉi tiu tabulo & la roboto estas private funkciigataj. Se vi volas subteni la servilkostojn kaj la pluevoluigon:",
    },
    "footer_source": {"de": "Quellcode", "en": "Source code", "eo": "Fontkodo"},

    # Board-Seite
    "status_head": {"de": "🩺 Bot- & Server-Status", "en": "🩺 Bot & server status", "eo": "🩺 Roboto- & servilstato"},
    "ver_title": {"de": "Aktuell laufende Bot-Version", "en": "Currently running bot version", "eo": "Nun funkcianta robotversio"},
    "stand_title": {"de": "Zeitpunkt der letzten Aktualisierung (alle 5 s)", "en": "Time of last update (every 5 s)", "eo": "Tempo de la lasta ĝisdatigo (ĉiujn 5 s)"},
    "stand_label": {"de": "Stand:", "en": "As of:", "eo": "Stato:"},
    "details": {"de": "Details", "en": "Details", "eo": "Detaloj"},
    "incident_history": {"de": "Vorfall-Historie ansehen", "en": "View incident history", "eo": "Vidi okazaĵ-historion"},
    "js_noconn": {"de": "Auto-Update: keine Verbindung zu /status.json", "en": "Auto-update: no connection to /status.json", "eo": "Aŭtomata ĝisdatigo: neniu konekto al /status.json"},
    "board_intro": {
        "de": "Öffentliche Ideen & gemeldete Bugs. Jeder darf anonym einreichen und hochvoten – neue Einreichungen erscheinen erst nach Prüfung.",
        "en": "Public ideas & reported bugs. Anyone may submit anonymously and upvote – new submissions appear only after review.",
        "eo": "Publikaj ideoj & raportitaj cimoj. Ĉiu rajtas anonime sendi kaj voĉdoni – novaj sendaĵoj aperas nur post kontrolo.",
    },
    "legend_priority": {"de": "Priorität:", "en": "Priority:", "eo": "Prioritato:"},
    "prio_p0": {"de": "kritisch (Blocker)", "en": "critical (blocker)", "eo": "kriza (barilo)"},
    "prio_p1": {"de": "hoch", "en": "high", "eo": "alta"},
    "prio_p2": {"de": "mittel", "en": "medium", "eo": "meza"},
    "prio_p3": {"de": "niedrig", "en": "low", "eo": "malalta"},
    "legend_upvotes": {"de": "▲ = Upvotes (Community-Priorisierung)", "en": "▲ = upvotes (community prioritisation)", "eo": "▲ = voĉdonoj (komunuma prioritatigo)"},
    "legend_comments": {"de": "💬 = Kommentar(e) vorhanden", "en": "💬 = comment(s) present", "eo": "💬 = komento(j) ĉeestas"},
    "legend_more": {"de": "„⤢ mehr“ öffnet die Detailseite", "en": "“⤢ more” opens the detail page", "eo": "„⤢ pli“ malfermas la detalpaĝon"},
    "done_in": {"de": "erledigt in {v}", "en": "done in {v}", "eo": "farita en {v}"},
    "more": {"de": "⤢ mehr", "en": "⤢ more", "eo": "⤢ pli"},
    "n_comments_title": {"de": "{n} Kommentar(e)", "en": "{n} comment(s)", "eo": "{n} komento(j)"},

    # Spaltentitel (PUBLIC_COLS)
    "col_open": {"de": "🗳️ Offen / Backlog", "en": "🗳️ Open / Backlog", "eo": "🗳️ Malfermitaj / Restaĵo"},
    "col_planned": {"de": "📌 Geplant", "en": "📌 Planned", "eo": "📌 Planitaj"},
    "col_in_progress": {"de": "🔧 In Arbeit", "en": "🔧 In progress", "eo": "🔧 En laboro"},
    "col_done": {"de": "✅ Erledigt", "en": "✅ Done", "eo": "✅ Faritaj"},
    "col_rejected": {"de": "🚫 Abgelehnt", "en": "🚫 Rejected", "eo": "🚫 Malakceptitaj"},

    # Gesamt-Ampel
    "overall_down": {"de": "Teilweise ausgefallen", "en": "Partially down", "eo": "Parte paneinta"},
    "overall_warn": {"de": "Läuft mit Einschränkungen", "en": "Running with limitations", "eo": "Funkcias kun limigoj"},
    "overall_ok": {"de": "Alles läuft", "en": "All systems go", "eo": "Ĉio funkcias"},

    # Sektions-Titel + Notizen (Status-Panel)
    "sec_core": {"de": "🧩 Kern", "en": "🧩 Core", "eo": "🧩 Kerno"},
    "sec_core_note": {"de": "Verbindung & Datenbanken", "en": "Connection & databases", "eo": "Konekto & datumbazoj"},
    "sec_jobs": {"de": "⚙️ Hintergrund-Jobs im Bot", "en": "⚙️ Background jobs in the bot", "eo": "⚙️ Fonaj taskoj en la roboto"},
    "sec_jobs_note": {"de": "discord.ext.tasks-Loops im Bot-Prozess", "en": "discord.ext.tasks loops in the bot process", "eo": "discord.ext.tasks-bukloj en la robotprocezo"},
    "sec_cron": {"de": "⏰ Externe Cronjobs (als Nutzer aam)", "en": "⏰ External cron jobs (as user aam)", "eo": "⏰ Eksteraj cron-taskoj (kiel uzanto aam)"},
    "sec_cron_note": {"de": "2 Cronjobs · Status anhand Aktualität der erzeugten Dateien", "en": "2 cron jobs · status based on freshness of generated files", "eo": "2 cron-taskoj · stato laŭ aktualeco de la generitaj dosieroj"},

    # Einreichen (Submit)
    "submit_h": {"de": "Idee oder Bug einreichen", "en": "Submit an idea or bug", "eo": "Sendi ideon aŭ cimon"},
    "submit_anon": {
        "de": "Anonym möglich. Deine Einreichung wird zuerst geprüft und erscheint dann öffentlich.",
        "en": "Anonymous is fine. Your submission is reviewed first and then appears publicly.",
        "eo": "Anonime eblas. Via sendaĵo unue estas kontrolata kaj poste aperas publike.",
    },
    "submit_terms": {
        "de": "Mit dem Absenden akzeptierst du die Board-Nutzungsbedingungen: sachliche Ideen/Bugs zum Bot, keine persönlichen/sensiblen Daten und keine beleidigenden oder rechtswidrigen Inhalte. Der Betreiber kann Einträge ablehnen, bearbeiten oder löschen.",
        "en": "By submitting you accept the board terms of use: factual ideas/bugs about the bot, no personal/sensitive data and no offensive or unlawful content. The operator may reject, edit or delete entries.",
        "eo": "Sendante vi akceptas la uzkondiĉojn de la tabulo: faktecaj ideoj/cimoj pri la roboto, neniuj personaj/sentemaj datumoj kaj neniu ofenda aŭ kontraŭleĝa enhavo. La funkciiganto rajtas malakcepti, redakti aŭ forigi enskribojn.",
    },
    "f_type": {"de": "Art", "en": "Type", "eo": "Tipo"},
    "f_title": {"de": "Titel *", "en": "Title *", "eo": "Titolo *"},
    "f_desc": {"de": "Beschreibung", "en": "Description", "eo": "Priskribo"},
    "f_name": {"de": "Dein Name (optional)", "en": "Your name (optional)", "eo": "Via nomo (nedeviga)"},
    "ph_anon": {"de": "anonym", "en": "anonymous", "eo": "anonima"},
    "btn_send": {"de": "Absenden", "en": "Send", "eo": "Sendi"},
    "cancel": {"de": "Abbrechen", "en": "Cancel", "eo": "Nuligi"},
    "type_bug": {"de": "Bug", "en": "Bug", "eo": "Cimo"},
    "type_feature": {"de": "Feature", "en": "Feature", "eo": "Funkcio"},
    "type_idea": {"de": "Idee", "en": "Idea", "eo": "Ideo"},

    # Detailseite
    "back_board": {"de": "← Board", "en": "← Board", "eo": "← Tabulo"},
    "upvotes_n": {"de": "▲ {n} Upvotes", "en": "▲ {n} upvotes", "eo": "▲ {n} voĉdonoj"},
    "submitted_at": {"de": "Eingereicht: {d}", "en": "Submitted: {d}", "eo": "Sendita: {d}"},
    "comments_h": {"de": "💬 Kommentare", "en": "💬 Comments", "eo": "💬 Komentoj"},
    "edit_or_comment": {"de": "✏️ Bearbeiten / Kommentar", "en": "✏️ Edit / comment", "eo": "✏️ Redakti / komenti"},

    # Login
    "login_h": {"de": "Owner-Login", "en": "Owner login", "eo": "Posedanto-ensaluto"},
    "f_token": {"de": "Admin-Token", "en": "Admin token", "eo": "Administra ĵetono"},
    "btn_login": {"de": "Anmelden", "en": "Log in", "eo": "Ensaluti"},

    # Admin
    "queue_h": {"de": "🛡️ Moderations-Queue ({n})", "en": "🛡️ Moderation queue ({n})", "eo": "🛡️ Moderada atendovico ({n})"},
    "nothing_review": {"de": "Nichts zu prüfen.", "en": "Nothing to review.", "eo": "Nenio por kontroli."},
    "btn_approve": {"de": "✔ Freigeben", "en": "✔ Approve", "eo": "✔ Aprobi"},
    "btn_reject": {"de": "✖ Ablehnen", "en": "✖ Reject", "eo": "✖ Malakcepti"},
    "btn_delete": {"de": "🗑 Löschen", "en": "🗑 Delete", "eo": "🗑 Forigi"},
    "all_entries_h": {"de": "Alle Einträge ({n})", "en": "All entries ({n})", "eo": "Ĉiuj enskriboj ({n})"},
    "admin_legend_edit": {"de": "Zum Bearbeiten von Titel/Beschreibung & für Kommentare ✏️ nutzen.", "en": "Use ✏️ to edit title/description & for comments.", "eo": "Uzu ✏️ por redakti titolon/priskribon & por komentoj."},
    "th_title": {"de": "Titel", "en": "Title", "eo": "Titolo"},
    "th_status_meta": {"de": "Status / Prio / Komponente / Version", "en": "Status / prio / component / version", "eo": "Stato / prio / komponanto / versio"},
    "btn_save": {"de": "Speichern", "en": "Save", "eo": "Konservi"},
    "csv_h": {"de": "📥 CSV-Import (rückwirkende Historie)", "en": "📥 CSV import (retroactive history)", "eo": "📥 CSV-importo (retroaktiva historio)"},
    "csv_import": {"de": "Importieren", "en": "Import", "eo": "Importi"},
    "csv_help": {
        "de": "Spalten (Reihenfolge/Groß-klein egal, Trenner , oder ; ): type,title,body,status,component,priority,version,created_at,source<br>Pflicht: <b>title</b>. Gültige <b>status</b>: open, planned, in_progress, done, rejected, duplicate, pending (Standard: done). Gültige <b>type</b>: bug, feature, idea.<br>Nach dem Import erscheint eine Meldung „N importiert, M übersprungen“; Details zu Skips stehen im Bot-Log.",
        "en": "Columns (any order/case, separator , or ; ): type,title,body,status,component,priority,version,created_at,source<br>Required: <b>title</b>. Valid <b>status</b>: open, planned, in_progress, done, rejected, duplicate, pending (default: done). Valid <b>type</b>: bug, feature, idea.<br>After import a message “N imported, M skipped” appears; skip details are in the bot log.",
        "eo": "Kolumnoj (ajna ordo/uskleco, apartigilo , aŭ ; ): type,title,body,status,component,priority,version,created_at,source<br>Deviga: <b>title</b>. Validaj <b>status</b>: open, planned, in_progress, done, rejected, duplicate, pending (defaŭlte: done). Validaj <b>type</b>: bug, feature, idea.<br>Post la importo aperas mesaĝo „N importitaj, M preterlasitaj“; detaloj pri preterlasoj estas en la robotprotokolo.",
    },

    # Bearbeiten (Edit)
    "edit_h": {"de": "✏️ Eintrag #{id} bearbeiten", "en": "✏️ Edit entry #{id}", "eo": "✏️ Redakti enskribon #{id}"},
    "public_view": {"de": "Öffentliche Ansicht", "en": "Public view", "eo": "Publika vido"},
    "back_admin": {"de": "← Admin", "en": "← Admin", "eo": "← Administro"},
    "f_status": {"de": "Status", "en": "Status", "eo": "Stato"},
    "f_priority": {"de": "Priorität", "en": "Priority", "eo": "Prioritato"},
    "f_component": {"de": "Komponente", "en": "Component", "eo": "Komponanto"},
    "f_version": {"de": "Version", "en": "Version", "eo": "Versio"},
    "btn_save_disk": {"de": "💾 Speichern", "en": "💾 Save", "eo": "💾 Konservi"},
    "comments_count_h": {"de": "💬 Kommentare ({n})", "en": "💬 Comments ({n})", "eo": "💬 Komentoj ({n})"},
    "new_comment": {"de": "Neuer Kommentar", "en": "New comment", "eo": "Nova komento"},
    "ph_comment": {"de": "Kommentar…", "en": "Comment…", "eo": "Komento…"},
    "f_author": {"de": "Autor", "en": "Author", "eo": "Aŭtoro"},
    "add_comment": {"de": "Kommentar hinzufügen", "en": "Add comment", "eo": "Aldoni komenton"},

    # Status-Detailseite
    "current_label": {"de": "Aktuell:", "en": "Current:", "eo": "Nuna:"},
    "inc_intro": {
        "de": "Aufgezeichnet werden „nicht OK“-Phasen (gelb/rot). Endet eine Phase, wird automatisch vermerkt, wann der Check wieder OK war.",
        "en": "Recorded are “not OK” phases (yellow/red). When a phase ends, it is automatically noted when the check was OK again.",
        "eo": "Registriĝas „ne-OK“-fazoj (flava/ruĝa). Kiam fazo finiĝas, aŭtomate notiĝas kiam la kontrolo denove estis OK.",
    },
    "inc_recent_h": {"de": "Letzte Vorfälle (max. 10)", "en": "Recent incidents (max. 10)", "eo": "Lastaj okazaĵoj (maks. 10)"},
    "inc_none": {"de": "Keine Vorfälle aufgezeichnet. 🎉", "en": "No incidents recorded. 🎉", "eo": "Neniuj okazaĵoj registritaj. 🎉"},
    "inc_since": {"de": "seit", "en": "since", "eo": "ekde"},
    "inc_ok_since": {"de": "wieder OK seit", "en": "OK again since", "eo": "denove OK ekde"},
    "inc_running": {"de": "läuft noch", "en": "still ongoing", "eo": "ankoraŭ daŭras"},
    "ph_admin_note": {"de": "Admin-Notiz…", "en": "Admin note…", "eo": "Administra noto…"},

    # Flash-Meldungen (per Code über ?m= übergeben)
    "flash_thanks_review": {"de": "Danke, wird geprüft.", "en": "Thanks, it will be reviewed.", "eo": "Dankon, ĝi estos kontrolita."},
    "flash_too_many": {"de": "Zu viele Einreichungen – bitte später erneut.", "en": "Too many submissions – please try again later.", "eo": "Tro multaj sendaĵoj – bonvolu reprovi poste."},
    "flash_title_missing": {"de": "Titel fehlt.", "en": "Title is missing.", "eo": "Titolo mankas."},
    "flash_submitted": {"de": "Danke! Deine Einreichung wird geprüft und erscheint dann öffentlich.", "en": "Thanks! Your submission will be reviewed and then appear publicly.", "eo": "Dankon! Via sendaĵo estos kontrolita kaj poste aperos publike."},
    "flash_no_csv": {"de": "Kein CSV empfangen.", "en": "No CSV received.", "eo": "Neniu CSV ricevita."},
    "flash_wrong_token": {"de": "Falsches Token.", "en": "Wrong token.", "eo": "Malĝusta ĵetono."},
    "flash_imported": {"de": "{n} importiert{skipped}", "en": "{n} imported{skipped}", "eo": "{n} importitaj{skipped}"},
    "flash_skipped": {"de": ", {s} übersprungen", "en": ", {s} skipped", "eo": ", {s} preterlasitaj"},
}


def translate(lang: str, key: str, **kw) -> str:
    """Übersetzt *key* in *lang* (Fallback: Deutsch, dann der Key selbst).
    Platzhalter werden via str.format eingesetzt."""
    entry = T.get(key)
    if not entry:
        return key
    text = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


# Anzeige-Label für die Einreichungs-Typen (bug/feature/idea) – lokalisiert,
# der gespeicherte Wert bleibt der englische Schlüssel.
_TYPE_KEYS = {"bug": "type_bug", "feature": "type_feature", "idea": "type_idea"}


def type_label(lang: str, typ: str) -> str:
    return translate(lang, _TYPE_KEYS.get((typ or "").lower(), "type_idea"))


def flash_text(lang: str, code: str, n: str = "", s: str = "") -> str:
    """Übersetzt einen Flash-Code (?m=code). Unbekannte Codes werden unverändert
    zurückgegeben (Abwärtskompatibilität: alte Volltext-Meldungen bleiben lesbar)."""
    if not code:
        return ""
    if code == "imported":
        skipped = translate(lang, "flash_skipped", s=s) if s and s != "0" else ""
        return translate(lang, "flash_imported", n=n or "0", skipped=skipped)
    key = "flash_" + code
    if key in T:
        return translate(lang, key)
    return code   # unbekannt -> unverändert anzeigen (z.B. alter Volltext)
