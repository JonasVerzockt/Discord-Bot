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

# Flaggen für den Umschalter als Inline-SVG (KEINE Emoji-Flaggen: die rendern u.a.
# unter Windows nur als Buchstaben "DE"/"GB"). Jeweils (SVG, Kürzel). Das SVG wird
# im Template mit |safe ausgegeben. de = Bundesflagge, en = Union Jack (vereinfacht),
# eo = Esperanto-Flagge (grün mit weißem Kanton + grünem Stern „verda stelo").
_SVG_DE = ('<svg class=fl viewBox="0 0 5 3" preserveAspectRatio="none">'
           '<rect width="5" height="3" fill="#000"/>'
           '<rect y="1" width="5" height="1" fill="#D00"/>'
           '<rect y="2" width="5" height="1" fill="#FFCE00"/></svg>')
_SVG_EN = ('<svg class=fl viewBox="0 0 60 30" preserveAspectRatio="none">'
           '<rect width="60" height="30" fill="#012169"/>'
           '<path d="M0,0 60,30 M60,0 0,30" stroke="#fff" stroke-width="6"/>'
           '<path d="M0,0 60,30 M60,0 0,30" stroke="#C8102E" stroke-width="2.5"/>'
           '<path d="M30,0 V30 M0,15 H60" stroke="#fff" stroke-width="10"/>'
           '<path d="M30,0 V30 M0,15 H60" stroke="#C8102E" stroke-width="6"/></svg>')
_SVG_EO = ('<svg class=fl viewBox="0 0 60 30" preserveAspectRatio="none">'
           '<rect width="60" height="30" fill="#009900"/>'
           '<rect width="15" height="15" fill="#fff"/>'
           '<polygon fill="#009900" points="7.5,1.3 8.97,5.48 13.4,5.58 9.88,8.27 '
           '11.14,12.52 7.5,10 3.86,12.52 5.12,8.27 1.6,5.58 6.03,5.48"/></svg>')
FLAGS = {
    "de": (_SVG_DE, "DE"),
    "en": (_SVG_EN, "EN"),
    "eo": (_SVG_EO, "EO"),
}
FLAG_TITLE = {
    "de": "Deutsch",
    "en": "English",
    "eo": "Esperanto",
}


def pick_lang(req) -> str:
    """Ermittelt die Sprache: ?lang= (falls gültig) > Accept-Language > Standard (de).

    Gibt bewusst die WHITELIST-Konstante aus LANGS zurück (nicht den rohen User-Wert):
    So ist der Rückgabewert nachweislich untainted – er fließt in Redirect-Locations
    (`?lang=…`) ein, und diese Konstruktion verhindert Open-Redirect/Header-Injection
    (CodeQL py/url-redirection) unabhängig von der Eingabe."""
    q = (req.query.get("lang") or "").lower().strip()
    for code in LANGS:
        if q == code:
            return code                      # Literal aus LANGS -> untainted
    accept = (req.headers.get("Accept-Language") or "").lower()
    # Grobe, robuste Auswertung: erste passende Sprache im Header gewinnt.
    for part in accept.replace(" ", "").split(","):
        want = part.split(";")[0].split("-")[0]
        for code in LANGS:
            if want == code:
                return code                  # ebenfalls Literal aus LANGS
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
    # Stats-Seite: Kopf, Navigation, Sektionen
    "st_intro": {"de": "Auswertungen zu Shops und Produkten aus den Grabber-Daten. Alle Zahlen beziehen sich auf den unten genannten Datenstand.", "en": "Analytics on shops and products from the grabber data. All figures refer to the data snapshot noted below.", "eo": "Analizoj pri butikoj kaj produktoj el la grabber-datumoj. Ĉiuj ciferoj rilatas al la sube menciita datumstato."},
    "st_nav": {"de": "Springe zu:", "en": "Jump to:", "eo": "Salti al:"},
    "st_data_as_of": {"de": "Datenstand: {d}", "en": "Data as of: {d}", "eo": "Datumstato: {d}"},
    "st_generated": {"de": "Erzeugt: {d}", "en": "Generated: {d}", "eo": "Generita: {d}"},
    "st_fx_note": {"de": "Fremdwährungen in EUR umgerechnet (Kurse: EZB/Frankfurter, Fallback für seltene Währungen; im Speicher bis 6 h gecacht).", "en": "Foreign currencies converted to EUR (rates: ECB/Frankfurter, fallback for rare currencies; cached in memory up to 6 h).", "eo": "Fremdaj valutoj konvertitaj al EUR (kursoj: EEB/Frankfurter, retrostreĉo por maloftaj valutoj; kaŝmemorigita ĝis 6 h)."},
    "st_cache_note": {"de": "Live berechnet, im Speicher bis zu 15 min zwischengespeichert.", "en": "Computed live, cached in memory for up to 15 min.", "eo": "Kalkulita realtempe, kaŝmemorigita ĝis 15 min."},
    "st_error": {"de": "Statistikdaten sind momentan nicht verfügbar (Datenquelle fehlt oder wird gerade erzeugt).", "en": "Statistics data is currently unavailable (source missing or being generated).", "eo": "Statistikaj datumoj nun ne haveblas (fonto mankas aŭ estas generata)."},
    "st_wip": {"de": "Dieser Bereich wird gerade gebaut.", "en": "This section is being built.", "eo": "Ĉi tiu sekcio estas konstruata."},
    "st_sec_overview": {"de": "Marktüberblick", "en": "Market overview", "eo": "Merkata superrigardo"},
    "st_sec_species": {"de": "Arten & Gattungen", "en": "Species & genera", "eo": "Specioj & genroj"},
    "st_sec_shops": {"de": "Shop-Vergleich", "en": "Shop comparison", "eo": "Butika komparo"},
    "st_sec_prices": {"de": "Preise", "en": "Prices", "eo": "Prezoj"},
    "st_sec_availability": {"de": "Verfügbarkeit", "en": "Availability", "eo": "Havebleco"},
    "st_sec_quality": {"de": "Datenqualität", "en": "Data quality", "eo": "Datumkvalito"},
    "st_sec_trends": {"de": "Zeitverläufe", "en": "Trends over time", "eo": "Tempaj tendencoj"},
    # Punkt 1: Marktüberblick – KPI-Kacheln, Diagramme
    "kpi_shops": {"de": "Shops", "en": "Shops", "eo": "Butikoj"},
    "kpi_shops_with": {"de": "Shops mit Produkten", "en": "Shops with products", "eo": "Butikoj kun produktoj"},
    "kpi_live": {"de": "Angebote (Ameisen)", "en": "Offers (ants)", "eo": "Ofertoj (formikoj)"},
    "kpi_merch": {"de": "Merch / Zubehör", "en": "Merch / accessories", "eo": "Var- / akcesoraĵoj"},
    "kpi_species": {"de": "Arten", "en": "Species", "eo": "Specioj"},
    "kpi_genera": {"de": "Gattungen", "en": "Genera", "eo": "Genroj"},
    "kpi_instock_pct": {"de": "Lagerquote (Ameisen)", "en": "In-stock rate (ants)", "eo": "Stok-kvoto (formikoj)"},
    "kpi_countries": {"de": "Länder", "en": "Countries", "eo": "Landoj"},
    "ch_countries_title": {"de": "Shops pro Land (Top 10)", "en": "Shops per country (top 10)", "eo": "Butikoj laŭ lando (supraj 10)"},
    "ch_countries_axis": {"de": "Shops", "en": "Shops", "eo": "Butikoj"},
    "ch_stock_title": {"de": "Verfügbarkeit der Ameisen-Angebote", "en": "Availability of ant offers", "eo": "Havebleco de formik-ofertoj"},
    "lbl_instock": {"de": "lagernd", "en": "in stock", "eo": "en stoko"},
    "lbl_outstock": {"de": "nicht lagernd", "en": "out of stock", "eo": "ne en stoko"},
    "lbl_other": {"de": "übrige", "en": "other", "eo": "aliaj"},
    "lbl_shops": {"de": "Shops", "en": "Shops", "eo": "Butikoj"},
    # Punkt 2: Arten & Gattungen
    "sp_genera_title": {"de": "Top-Gattungen (nach Angeboten)", "en": "Top genera (by offers)", "eo": "Supraj genroj (laŭ ofertoj)"},
    "sp_reach_title": {"de": "Beliebteste Arten (in wie vielen Shops gelistet)", "en": "Most popular species (number of shops listing them)", "eo": "Plej popularaj specioj (en kiom da butikoj)"},
    "sp_rarities_title": {"de": "Raritäten", "en": "Rarities", "eo": "Maloftaĵoj"},
    "sp_rarities_count": {"de": "{n} Arten gibt es in nur einem einzigen Shop.", "en": "{n} species are available in only a single shop.", "eo": "{n} specioj haveblas en nur unu butiko."},
    "sp_rarities_show": {"de": "Beispiele anzeigen ({n})", "en": "Show examples ({n})", "eo": "Montri ekzemplojn ({n})"},
    "sp_longtail_title": {"de": "Verteilung: Arten nach Shop-Reichweite", "en": "Distribution: species by shop reach", "eo": "Distribuo: specioj laŭ butik-atingo"},
    "sp_longtail_x": {"de": "in wie vielen Shops", "en": "number of shops", "eo": "en kiom da butikoj"},
    "sp_longtail_y": {"de": "Arten", "en": "species", "eo": "specioj"},
    "lbl_offers": {"de": "Angebote", "en": "Offers", "eo": "Ofertoj"},
    "lbl_species": {"de": "Arten", "en": "Species", "eo": "Specioj"},
    # Punkt 3: Shop-Vergleich
    "sh_offers_title": {"de": "Sortimentsgröße (Ameisen-Angebote je Shop)", "en": "Assortment size (ant offers per shop)", "eo": "Sortiment-grando (formik-ofertoj po butiko)"},
    "sh_breadth_title": {"de": "Sortimentsbreite (verschiedene Arten je Shop)", "en": "Assortment breadth (distinct species per shop)", "eo": "Sortiment-larĝo (malsamaj specioj po butiko)"},
    "sh_exclusive_title": {"de": "Exklusiv-Arten (Shop ist einziger Anbieter)", "en": "Exclusive species (shop is the only seller)", "eo": "Ekskluzivaj specioj (butiko estas la sola vendanto)"},
    "sh_scatter_title": {"de": "Breite vs. Tiefe je Shop", "en": "Breadth vs. depth per shop", "eo": "Larĝo kontraŭ profundo po butiko"},
    "sh_scatter_x": {"de": "verschiedene Arten", "en": "distinct species", "eo": "malsamaj specioj"},
    "sh_scatter_y": {"de": "Angebote", "en": "offers", "eo": "ofertoj"},
    # Punkt 4: Preise (EUR)
    "kpi_price_median": {"de": "Median", "en": "Median", "eo": "Mediano"},
    "kpi_price_mean": {"de": "Durchschnitt", "en": "Average", "eo": "Averaĝo"},
    "kpi_price_p25": {"de": "25. Perzentil", "en": "25th percentile", "eo": "25-a percentilo"},
    "kpi_price_p75": {"de": "75. Perzentil", "en": "75th percentile", "eo": "75-a percentilo"},
    "kpi_price_min": {"de": "Minimum", "en": "Minimum", "eo": "Minimumo"},
    "kpi_price_max": {"de": "Maximum", "en": "Maximum", "eo": "Maksimumo"},
    "pr_basis_note": {"de": "Basis: Einstiegspreis je Ameisen-Angebot (niedrigster positiver Variantenpreis), in EUR umgerechnet.", "en": "Basis: entry price per ant offer (lowest positive variant price), converted to EUR.", "eo": "Bazo: enira prezo po formik-oferto (plej malalta pozitiva variant-prezo), konvertita al EUR."},
    "pr_hist_title": {"de": "Preisverteilung (EUR)", "en": "Price distribution (EUR)", "eo": "Prezdistribuo (EUR)"},
    "pr_hist_x": {"de": "Preis in EUR", "en": "Price in EUR", "eo": "Prezo en EUR"},
    "pr_hist_y": {"de": "Angebote", "en": "Offers", "eo": "Ofertoj"},
    "pr_genus_title": {"de": "Median-Preis je Top-Gattung (EUR)", "en": "Median price per top genus (EUR)", "eo": "Mediana prezo po supra genro (EUR)"},
    "pr_genus_axis": {"de": "Median in EUR", "en": "Median in EUR", "eo": "Mediano en EUR"},
    "pr_spread_title": {"de": "Größte Preisspanne je Art (günstigster–teuerster Anbieter, EUR)", "en": "Largest price range per species (cheapest–priciest seller, EUR)", "eo": "Plej granda prezintervalo po specio (plej malmultekosta–plej multekosta vendanto, EUR)"},
    "pr_spread_axis": {"de": "EUR", "en": "EUR", "eo": "EUR"},
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


# Lokalisierte Ländernamen (Babel/CLDR) – wie bei /shop_list. Cache pro Sprache.
_LOCALE_CACHE: dict = {}


def _locale(lang: str):
    if lang not in _LOCALE_CACHE:
        try:
            from babel import Locale
            _LOCALE_CACHE[lang] = Locale.parse(lang)
        except Exception:
            try:
                from babel import Locale
                _LOCALE_CACHE[lang] = Locale.parse("en")
            except Exception:
                _LOCALE_CACHE[lang] = None
    return _LOCALE_CACHE[lang]


def country_name(lang: str, iso: str) -> str:
    """ISO-Ländercode -> lokalisierter Name (Fallback: Großbuchstaben-Code)."""
    code = (iso or "").upper()
    if not code or code == "??":
        return "?"
    loc = _locale(lang)
    if loc is None:
        return code
    try:
        return loc.territories.get(code, code)
    except Exception:
        return code


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
