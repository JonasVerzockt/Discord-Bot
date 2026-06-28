# AAM Discord Bot

Modularer Discord-Bot für die **Ameisen an die Macht**-Community. Kombiniert mehrere eigenständige Funktionen in einem Bot:

- **Review-Bot** – erkennt Shopbewertungen in einem Discord-Kanal, parst sie automatisch mit Claude Haiku (KI) und schreibt sie strukturiert in ein Google Sheet
- **AntCheck-Bot** – überwacht die Verfügbarkeit von Ameisenarten bei Online-Shops via AntCheck API und benachrichtigt User per DM sobald eine gesuchte Art verfügbar ist; Preise werden in der jeweiligen Währung inklusive EUR-Umrechnungshinweis angezeigt
- **Preis-Tracking** – beobachtet Preise einzelner Produkte und informiert per DM sobald sich ein Preis nach oben oder unten verändert; interaktive Auswahl über Shop → Produkt → Bestätigen
- **AI-Chat-Bot** – beantwortet Fragen im konfigurierten AI-Kanal auf @-Erwähnung mit Claude Sonnet, inkl. Konversationsgedächtnis (per Discord-Reply), Tagesbudget-Kontrolle und Shop-Wissen aus dem AAM Google Sheet *(im AAM Discord aktuell nicht öffentlich verfügbar)*
- **iNat-Tracker** – erkennt iNaturalist-Beobachtungslinks in einem konfigurierten Kanal innerhalb eines definierten Zeitfensters und trägt sie automatisch (Discord-ID, Anzeigename, Link, Datum) in ein separates Google Sheet ein

---

## Voraussetzungen

- Python 3.11+
- Discord-Bot-Token ([discord.com/developers](https://discord.com/developers/applications)) mit aktivierten Intents: **Message Content**, **Server Members**, **Reactions**
- Google Service Account JSON für Sheets-Zugriff (`service_account.json`)
- Anthropic API Key für Claude Haiku (KI-Parser)
- AntCheck API Key (für den Grabber)

---

## Installation

```bash
git clone https://github.com/JonasVerzockt/Discord-Bot
cd Discord-Bot
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Wichtig:** Der Bot nutzt `py-cord` für Slash Commands. `discord.py` und `py-cord` sind **nicht** kompatibel – nur eines darf installiert sein:
> ```bash
> pip uninstall discord.py -y
> pip install "py-cord>=2.4.0"
> ```

### Abhängigkeiten (`requirements.txt`)

| Paket | Zweck |
|-------|-------|
| `py-cord>=2.4.0` | Discord (Slash Commands, ApplicationContext) |
| `anthropic>=0.25.0` | Claude Haiku KI-Parser |
| `gspread>=6.0.0` | Google Sheets |
| `google-auth>=2.0.0` | Google Auth |
| `requests>=2.31.0` | HTTP (Grabber + Frankfurter Währungs-API) |
| `rapidfuzz>=3.0.0` | Fuzzy Shop-Matching |
| `psutil>=5.9.0` | System-Stats (`/system`) |
| `python-dotenv>=1.0.0` | `.env`-Dateien |
| `PyNaCl>=1.5.0` | Voice-Verschlüsselung (unterdrückt discord-Warning) |
| `davey` | Voice-Receive (unterdrückt discord-Warning) |

---

## Konfiguration

Kopiere `.env.example` nach `.env` und fülle alle Pflichtfelder aus:

```env
# ── Discord ───────────────────────────────────────────────────
DISCORD_TOKEN=dein_token_hier
REVIEW_CHANNEL_ID=123456789012345678      # Kanal für Shopbewertungen
BOT_OWNER_ID=123456789012345678           # Deine eigene Discord-User-ID
SERVER_IDS=123456789012345678             # Kommagetrennte Server-IDs

# ── Anthropic (KI-Parser) ─────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# ── Google Sheets ─────────────────────────────────────────────
GOOGLE_SPREADSHEET_ID=deine_spreadsheet_id_hier

# ── AntCheck API ──────────────────────────────────────────────
ANTCHECK_API_KEY=dein_api_key_hier
ANTCHECK_API_URL=https://antcheck.info
ANTCHECK_VERIFY_SSL=false                 # false bei self-signed Zertifikat

# ── KI-Chat-Bot ───────────────────────────────────────────────
AI_CHAT_CHANNEL_IDS=123456789012345678   # Kanal-ID, in dem der Bot antwortet
AI_CHAT_DAILY_BUDGET_USD=0.50            # Gesamtes Tagesbudget (alle User)
AI_CHAT_USER_DAILY_BUDGET_USD=0.10       # Pro-User-Tagesbudget

# ── Pfade (optional) ──────────────────────────────────────────
DATA_DIRECTORY=/opt/discord-bot          # Wo shops_data.json abgelegt wird

# ── Python ────────────────────────────────────────────────────
PYTHONUNBUFFERED=1
```

Alle Limits (Eingabezeichenanzahl, Output-Tokens, Konversationsgedächtnis, TTL) haben sinnvolle Defaults und müssen nur gesetzt werden wenn sie angepasst werden sollen – siehe `.env.example`.

Lege außerdem die Google Service Account Datei als `service_account.json` im Projektordner ab (wird in `.gitignore` ignoriert).

---

## Erster Start & Server-Einrichtung

```bash
# 1. Shopdaten initial laden (einmalig, danach per Cron-Job)
python grabber.py

# 2. Bot starten
python main.py
```

Auf jedem Discord-Server muss einmalig `/startup` ausgeführt werden (Admin):

```
/startup  language: de  channel: #bot-commands
```

Damit wird der Bot-Kanal festgelegt und die Serversprache gesetzt. Ohne `/startup` funktionieren alle Befehle, aber in jedem Kanal.

---

## Review-Bot

### Funktionsweise

Der Review-Bot überwacht den konfigurierten `REVIEW_CHANNEL_ID` auf neue Shopbewertungen.

**Erkennung:** Eine Nachricht wird als Bewertung erkannt wenn sie das 🛒-Emoji enthält **oder** sowohl `Shop:` als auch `Fazit`, `/10` oder `/5` enthält.

**Geteilte Nachrichten:** Schickt ein User mehrere Nachrichten hintereinander (z. B. weil Discord die Zeichengrenze erreicht), wartet der Bot `ACCUMULATION_DELAY` Sekunden (Standard: 8) nach der letzten Nachricht und führt alle Teile automatisch zu einer Review zusammen.

**Shop-Auflösung** (in dieser Reihenfolge):
1. `shop_mapping.csv` – manuell oder automatisch gelernte Mappings
2. Discord-Mention (`@User`) → Display-Name wenn URL-artig
3. Fuzzy-Match gegen bekannte Sheet-Shopnamen (≥80 % Ähnlichkeit)
4. → 🟡 Reaktion: Shop konnte nicht aufgelöst werden

**KI-Parsing:** Claude Haiku extrahiert aus dem Freitext strukturierte Felder:

| Feld | Beispiel |
|------|---------|
| Datum | `15.06.2026` |
| Shop-Name | `ANTSTORE` |
| Shop-Typ | `ameisenshop` |
| Produkte | `Camponotus ligniperdus Königin` |
| Geld ausgegeben | `24.90` |
| Bewertung | `8` (normalisiert auf 0–10) |
| Positiv | `Schnelle Lieferung; gute Verpackung` |
| Negativ | `Preis etwas hoch` |

**Sheet-Struktur:** Spalten A–I werden pro Bewertung in das Google Sheet „Rohdaten" geschrieben.

### Reaktionssystem

| Reaktion | Bedeutung |
|----------|-----------|
| 🟢 | Erfolgreich verarbeitet |
| 🟡 | Shop nicht erkannt oder Parse-Fehler |
| 🔴 | Retry fehlgeschlagen |

**Retry-Mechanismus:** Wenn eine Bewertung 🟡 bekommt, wird der unbekannte Shop-Identifier in `shop_mapping.csv` eingetragen (leer). Der Admin füllt die korrekte Shop-URL ein. Sobald ein User auf die 🟡-Reaktion klickt, liest der Bot die CSV neu und versucht die Verarbeitung erneut.

### Reconcile-Scan

Beim Start gleicht der Bot automatisch die letzten **90 Tage** Discord-History mit dem Google Sheet ab:
- Nachrichten die im Sheet stehen aber noch nicht gemappt sind → Mapping wird nachgetragen, Bot lernt den Shop automatisch
- Nachrichten die noch nicht im Sheet stehen → werden neu verarbeitet

Manuell auslösbar per `/rescan`.

---

## AntCheck-Bot

### Shopbewertungen (AAM-Rating)

Shopbewertungen kommen **nicht** von der AntCheck API, sondern aus dem Google Sheet „Händler A-Z" (Spalte A = Domain oder Name, Spalte C = Durchschnittsbewertung). Der Bot gleicht alle 48 Stunden die Sheet-Einträge mit den AntCheck-Shops ab und speichert die Bewertungen in der DB.

**Matching in zwei Stufen:**

1. **Domain-Exact-Match** – Aus der Shop-URL (oder manuellem Override) wird die Domain extrahiert (`www.` und Trailing-Slashes werden normalisiert) und direkt gegen den Sheet-Eintrag verglichen. So werden Shops mit identischer Basis-Domain aber unterschiedlicher TLD korrekt getrennt (`antstore.at` ≠ `antstore.net`).
2. **Fuzzy-Fallback** (≥81 %) – Für Shops ohne passenden Domain-Eintrag im Sheet wird der normalisierte Shop-Name gegen alle Sheet-Einträge verglichen. Generische TLDs (`.com`, `.net`, `.org`, `.shop`, `.store`, `.info`) werden dabei entfernt; Länder-TLDs (`.de`, `.at`, `.ch` usw.) bleiben erhalten, um Falsch-Matches zwischen ähnlich benannten Shops aus verschiedenen Ländern zu vermeiden.

Manuelle URL-Korrekturen (z.B. wenn die API eine falsche Domain liefert) können per `/shopurl set` dauerhaft gesetzt werden und überleben stündliche Shop-Reloads.

### Vollständiger Ablauf einer Benachrichtigung

**1. `/notification` ausführen**

```
/notification  genus: Messor  regions: de,at  exclude_species: capitatus
/notification  species: Lasius niger  regions: eu
/notification  species: Camponotus ligniperda  swiss_only: True
```

Validierungen vor dem Anlegen:
- Nicht beides (`species` und `genus`) gleichzeitig
- `species` muss Leerzeichen enthalten (Gattung + Art, keine reine Gattung)
- Region muss zu einem vorhandenen Shop passen
- Art/Gattung muss in `shops_data.json` vorkommen (überspringsbar mit `force: True`)
- Bei `eu` als Region: wird automatisch in alle EU-Ländercodes aufgelöst

**2. Sofort-Check nach Einrichten**

Direkt nach dem Anlegen der Benachrichtigung wird einmalig geprüft ob die Art bereits verfügbar ist.

**3. Hintergrund-Loop (alle 5 Minuten)**

Für alle `active`-Benachrichtigungen:
- Lädt `shops_data.json` + DB-Ratings + URL-Overrides
- Filtert nach Region (oder CH-Shops-Liste bei `swiss_only`)
- Filtert Shops auf der persönlichen Blacklist des Users raus
- Gleicht Ergebnisse mit `user_seen_products` ab → nur **neue** Produkte lösen eine DM aus

**4. DM bei Fund**

Produkte werden nach AAM-Rating sortiert (beste zuerst, ohne Rating ganz unten). Preise werden in der Originalwährung des Shops angezeigt, inklusive automatischer EUR-Umrechnung via [Frankfurter API](https://www.frankfurter.app) (kostenlos, kein API-Key, 6-Stunden-Cache):

```
34.49CAD (ca. 23.50€)
10.00-20.00CAD (ca. 6.80-13.60€)
59.99EUR
```

Bei mehr als ~2000 Zeichen werden mehrere DMs gesendet. Falls DMs blockiert sind, schreibt der Bot einen Ping in den Server-Kanal.

**5. Feedback nach DM**

Der Bot fragt per DM nach (48h Wartefenster):

| Reaktion | Was passiert |
|----------|-------------|
| 👍 Gekauft | Benachrichtigung abgeschlossen (`completed`). Gesehene Produkte werden geleert, sodass bei einer neuen `/notification` sofort wieder benachrichtigt wird. |
| 🔄 Weiter suchen | Status zurück auf `active`. Bereits gesehene Produkte bleiben gespeichert – nur neue Produkte triggern erneut. |
| Keine Antwort nach 48h | Status `expired`, Abschluss-DM |

**6. Jahres-Ablauf**

Benachrichtigungen die länger als 365 Tage `active` sind werden täglich als `expired` markiert und der User bekommt eine Abschluss-DM.

---

## Preis-Tracking

Ergänzend zur Verfügbarkeitsbenachrichtigung können User einzelne Produkte dauerhaft beobachten und werden automatisch per DM informiert, wenn sich der Preis verändert – unabhängig von Verfügbarkeit oder Region.

### Ablauf

**1. `/track_price species:<Art oder Gattung>` aufrufen**

Der Bot sucht alle Produkte (auch aktuell nicht verfügbare) zur angegebenen Art oder Gattung in `shops_data.json`. Falls Produkte gefunden werden, startet eine interaktive 3-Schritt-Auswahl per Discord-Menü:

1. **Shop auswählen** – Dropdown mit allen Shops, die passende Produkte haben (max. 25)
2. **Produkte auswählen** – Multi-Select-Dropdown der Produkte im gewählten Shop (max. 25); Produkte werden immer angezeigt, unabhängig davon ob ein Preis bekannt ist:
   - ✅ Verfügbar – aktueller Preis
   - ❌ Nicht verfügbar – aktueller (Nicht-Verfügbar-)Preis
   - ⏸️ Zuletzt gesehen – aktuell kein Preis in API, aber letzter bekannter Preis aus `price_history.db` vorhanden
   - ❓ Kein Preis bekannt – noch nie ein Preis erfasst (z. B. neues Produkt)
3. **Bestätigen** – Schaltflächen „Bestätigen" / „Abbrechen"; nach Bestätigung wird der aktuelle Preis als Baseline gesetzt und eine **öffentliche Ankündigung** im Bot-Kanal gepostet (z. B. `🎯 Jonas beobachtet jetzt den Preis für Oecophylla smaragdina bei Antstore (2 Produkt(e))!`)

Die Interaktion ist ephemeral (nur für den ausführenden User sichtbar) und läuft automatisch nach 3 Minuten ohne Eingabe ab.

**2. Hintergrund-Check (stündlich)**

Alle ~65 Minuten vergleicht der Bot den aktuellen Preis aus `price_history.db` mit dem zuletzt notierten Preis (`last_notified_min/max`):
- Kein Preis bisher gesetzt → Baseline setzen, keine DM
- Preis gesunken → DM mit 📉 (günstiger)
- Preis gestiegen → DM mit 📈 (teurer)
- Kein neuer Preis in DB → keine Aktion

Nach jeder Benachrichtigung wird der neue Preis als Baseline gespeichert.

**3. DM-Fallback**

Falls DMs des Users blockiert sind, wird der Server-Kanal als Fallback genutzt (gleiches Verhalten wie bei der Verfügbarkeitsbenachrichtigung).

---

## AI-Chat-Bot

> **Hinweis:** Der AI-Chat-Bot ist im AAM Discord aktuell **nicht öffentlich verfügbar**. Die Funktion ist vollständig implementiert und kann jederzeit aktiviert werden, wird aber momentan nur intern genutzt. Hintergrund: Die Community setzt bewusst auf echte Antworten von erfahrenen Haltern statt auf KI – viele Mitglieder schätzen den persönlichen Austausch und stehen KI-generierten Antworten skeptisch gegenüber. Der Bot bleibt als optionales Feature erhalten, das bei Bedarf aktiviert werden kann.

### Funktionsweise

Der AI-Chat-Bot reagiert ausschließlich auf **@-Erwähnungen** in den konfigurierten `AI_CHAT_CHANNEL_IDS`. Slash-Commands und eigene Bot-Nachrichten werden ignoriert.

**Konversationsgedächtnis:** Wenn ein User auf eine Bot-Antwort antwortet (Discord-Reply), wird die gespeicherte Gesprächshistorie geladen und der Kontext fortgeführt. Die KI „erinnert sich" bis zu `AI_CHAT_MAX_HISTORY_TURNS` Gesprächsrunden (Standard: 10) oder bis zur TTL-Grenze (Standard: 24 Stunden).

**Budget-Kontrolle (Tagesreset 00:00 UTC / 01:00 MEZ / 02:00 MESZ):**
- Globales Tagesbudget (`AI_CHAT_DAILY_BUDGET_USD`, Standard: $0,50) – gemeinsamer Pool aller User
- Pro-User-Tagesbudget (`AI_CHAT_USER_DAILY_BUDGET_USD`, Standard: $0,10) – individuelles Limit
- Ist eines der Budgets erschöpft, antwortet der Bot mit einer Fehlermeldung inkl. geschätzter Anforderungskosten und Resetzeit

**Dateianhänge:** Der Bot verarbeitet Anhänge die zusammen mit einer @-Erwähnung gesendet werden:

| Typ | Formate | Max. Größe |
|-----|---------|-----------|
| Bilder (Vision) | jpg, jpeg, png, gif, webp | 1 MB |
| Textdateien | txt, md, csv, log | 10 KB |
| Videos | – | nicht unterstützt (wird abgelehnt) |
| Sonstige | – | nicht unterstützt (wird abgelehnt) |

**System-Prompt:** Wird beim Start aus sprachspezifischen Dateien geladen – `ai_chat_system_prompt_de.txt`, `ai_chat_system_prompt_en.txt`, `ai_chat_system_prompt_eo.txt`. Der Platzhalter `{model}` wird automatisch durch das konfigurierte Modell ersetzt. Jeder Prompt ist vollständig in der jeweiligen Sprache verfasst und konfiguriert die KI als AAM-Community-Assistent für Ameisenhaltung, inkl. Quellenpflicht, Jugendschutz und Discord-Markdown-Formatierung. Die Legacy-Datei `ai_chat_system_prompt.txt` wird weiterhin als Deutsch-Fallback erkannt.

**Shop-Wissen:** Beim Start und alle 6 Stunden werden die Tabs **„Übersicht"** und **„Händler A-Z"** aus dem AAM Google Sheet geladen. Händler A-Z wird kompakt aufbereitet (`shopname ⭐9.97 (63x)`) und auf Shops mit **mindestens 4 Bewertungen** gefiltert. Der Shop-Block wird nur bei shop-relevanten Anfragen in den System-Prompt eingebettet – per **3-stufiger Vorqualifizierung**:

1. **Keyword-Check** (kostenlos): enthält die Nachricht shop-relevante Begriffe oder einen bekannten Shop-Namen? → ja: Shop-Daten rein
2. **Haiku-Klassifikation** (~$0.00025): kein Keyword gefunden – Haiku entscheidet ob die Frage indirekt shop-relevant ist (z.B. „wo kaufe ich günstig?")
3. **Sonnet-Hauptaufruf**: mit oder ohne Shop-Block je nach Stage 1/2. Haiku-Kosten werden immer zum Gesamtbetrag addiert und im Disclaimer angezeigt.

Nutzt denselben Service Account und dieselbe Spreadsheet-ID wie der Review-Bot – keine extra Konfiguration nötig.

**Disclaimer:** Jede Antwort wird automatisch im Code um einen Disclaimer ergänzt (nicht durch die KI selbst), inkl. der tatsächlichen Anforderungskosten und einem Link zum Quellcode:
> -# 🤖 KI-Antwort – nur zur Orientierung, kein Ersatz für Fachrat. Angaben immer selbst prüfen! · 💰 $0.00312 · Quellcode: https://github.com/JonasVerzockt/Discord-Bot

**Modell:** Standard `claude-haiku-4-5-20251001`, konfigurierbar per `AI_CHAT_MODEL` – aktuell `claude-sonnet-4-6` (unterstützt Text und Vision).

---

## Slash Commands

### Für alle User (nur im Bot-Kanal)

| Befehl | Parameter | Beschreibung |
|--------|-----------|-------------|
| `/notification` | `species` oder `genus` (Pflicht, nicht beides), `regions` (z.B. `de,at` oder `eu`), `swiss_only`, `exclude_species`, `force` | Verfügbarkeitsbenachrichtigung einrichten. `regions: eu` wird automatisch auf alle EU-Ländercodes aufgelöst. `exclude_species` schließt bestimmte Arten innerhalb einer Gattungs-Suche aus. `force: True` überspringt die Prüfung ob die Art in der DB vorkommt. |
| `/delete_notifications` | `ids` (komma- oder leerzeichengetrennte Benachrichtigungs-IDs) | Eigene Benachrichtigungen löschen. Die IDs sind aus `/history` ersichtlich. |
| `/history` | – | Zeigt die letzten 20 eigenen Benachrichtigungen mit ID, Art, Region und Status (active / completed / expired / failed). |
| `/testnotification` | – | Schickt eine Test-DM an sich selbst, um zu prüfen ob DMs vom Bot empfangen werden. |
| `/track_price` | `species` (Art oder Gattung, Pflicht) | Startet die interaktive Preis-Tracking-Einrichtung: zuerst Shop-Auswahl per Dropdown, dann Produkt-Auswahl (Mehrfachauswahl möglich), dann Bestätigung. Aktueller Preis wird als Baseline gesetzt – Benachrichtigung erfolgt nur bei zukünftigen Preisänderungen. |
| `/my_price_tracking` | – | Listet alle aktuell beobachteten Produkte mit dem zuletzt notierten Preis, dem aktuellen Preis aus der Preishistorie und dem Datum der letzten Benachrichtigung. |
| `/untrack_price` | – | Zeigt alle beobachteten Produkte als Multi-Select-Dropdown und entfernt die ausgewählten aus dem Tracking. |
| `/usersetting language` | `language` (`de` / `en` / `eo`) | Eigene Sprache setzen. Wirkt auf alle Bot-Antworten – Slash-Command-Ausgaben, DMs und KI-Antworten. |
| `/usersetting blacklist_add` | `shop` (Name oder Teile davon, Fuzzy-Match) | Shop dauerhaft von Verfügbarkeits-DMs ausschließen. Der Bot sucht den besten Treffer im Shop-Verzeichnis. |
| `/usersetting blacklist_remove` | `shop` | Shop wieder in Benachrichtigungen einschließen. |
| `/usersetting blacklist_list` | – | Eigene Blacklist anzeigen (Shop-Name + ID). |
| `/usersetting shop_list` | `country` (optional, z.B. `de`) | Alle bekannten Shops anzeigen, optional nach Länderkürzel gefiltert. Zeigt Name, URL und AAM-Rating. |
| `/ch_delivery add` | `shop` | Shop manuell zur CH-Lieferliste hinzufügen (für `swiss_only`-Benachrichtigungen). Automatische CH-Shops (aus `country=ch` in der API) werden immer einbezogen. |
| `/ch_delivery list` | – | CH-Lieferliste anzeigen: automatisch erkannte Shops (aus API) und manuell hinzugefügte. |
| `/ai_status` | – | Eigenen KI-Chat Budget-Status anzeigen: aktuell verbrauchte Kosten, verbleibendes persönliches und globales Tagesbudget sowie Uhrzeit des nächsten Resets. |
| `/help` | – | Befehlsübersicht (lokalisiert in der eingestellten Sprache). Antwort ist **öffentlich** sichtbar im Kanal. |

### Nur Admin / Nachrichten verwalten

| Befehl | Parameter | Beschreibung |
|--------|-----------|-------------|
| `/startup` | `language` (`de`/`en`/`eo`), `channel` | Bot-Kanal und Sprache für diesen Server festlegen. Muss einmalig pro Server aufgerufen werden. |
| `/status` | – | Zeigt Anzahl verarbeiteter Bewertungen, ausstehende (🟡) und fehlgeschlagene (🔴) Nachrichten. |
| `/pending` | – | Listet alle ausstehenden Nachrichten mit Message-ID, Grund und kurzem Nachrichtenausschnitt. |
| `/test` | `message_id` | KI-Parser testen ohne Sheet-Eintrag. Zeigt was die KI aus der Nachricht extrahieren würde. |
| `/rescan` | – | Gleicht die letzten 90 Tage Discord-History manuell mit dem Google Sheet ab. Nützlich nach manuellen Sheet-Korrekturen oder Bot-Ausfällen. |
| `/reprocess` | `ids` (Leerzeichen- oder kommagetrennte Message-IDs) | Bewertungsnachricht(en) neu verarbeiten. Mehrere IDs werden zu einem einzigen Sheet-Eintrag zusammengeführt (für geteilte Nachrichten). |
| `/export` | – | Gibt die ersten 50 Zeilen der Sheet-Rohdaten als JSON aus (zum Debuggen). |
| `/stats` | – | Benachrichtigungsstatistiken: Gesamtanzahl, aktive, abgelaufene, Top-10-gesuchte Arten. |
| `/system` | – | Systemstatus: Uptime, CPU-Auslastung, RAM-Verbrauch, DB-Größe, Alter der `shops_data.json`, Bot-Version. |
| `/reloadshops` | – | `shops_data.json` sofort neu einlesen und DB aktualisieren (ohne `average_rating` und `url_override` zu überschreiben). |
| `/shopmapping add` | `external_name`, `shop_id` | Externen Shopnamen (z.B. aus Discord-Review) dauerhaft einer internen Shop-ID zuordnen. |
| `/shopmapping show` | – | Alle gespeicherten Shop-Name-Mappings anzeigen. |
| `/shopmapping remove` | `external_name` | Mapping löschen. |
| `/shopurl set` | `shop_id`, `url` | Manuelle URL für einen Shop setzen. Überschreibt die API-URL dauerhaft und überlebt stündliche Shop-Reloads. Nützlich wenn die API eine falsche Domain liefert. |
| `/shopurl clear` | `shop_id` | Manuelle URL-Override entfernen – API-URL wird wieder genutzt. |
| `/shopurl list` | – | Alle aktiven URL-Overrides anzeigen. |
| `/ch_delivery remove` | `shop_id` | Shop aus CH-Lieferliste entfernen. Jeder User kann eigene Einträge entfernen; Admins können alle entfernen. |
| `/ai_reset` | `user` (optional) | KI-Chat Budget für einen bestimmten User oder global (alle User) zurücksetzen. Ohne `user`-Angabe wird das globale Budget zurückgesetzt. |
| `/ai_prompt` | – | Aktuell geladenen System-Prompt des KI-Chats anzeigen – in der eingestellten Sprache des ausführenden Users. |

---

## Hintergrundaufgaben (`cogs/tasks.py`)

| Task | Intervall | Beschreibung |
|------|-----------|-------------|
| Verfügbarkeitsprüfung | alle 5 Minuten | Prüft alle `active`-Benachrichtigungen gegen `shops_data.json` |
| Preis-Check | alle ~65 Minuten | Vergleicht aktuelle Preise aus `price_history.db` mit gespeicherten Baselines; sendet DM bei Preisänderung |
| Shop-Daten-Reload | stündlich | Liest `shops_data.json` neu, schreibt Shops in DB (ohne `average_rating` und `url_override` zu überschreiben) |
| Shop-Ratings-Sync | alle 48 Stunden | Liest AAM-Bewertungen aus Google Sheet „Händler A-Z": erst Domain-Exact-Match, dann Fuzzy-Fallback ≥81 % |
| Abgelaufene Benachrichtigungen | täglich | Markiert Benachrichtigungen >365 Tage als `expired` und sendet Abschluss-DM |
| DB VACUUM + ANALYZE | wöchentlich | Optimiert die SQLite-Datenbank |
| Bot-Status | jede Minute | Aktualisiert den Discord-Status (Uptime, Server, User) |
| AI-Chat Konversations-Cleanup | alle 6 Stunden | Löscht abgelaufene Konversationshistorien (>24h TTL) |
| AI-Chat Shop-Daten-Refresh | alle 6 Stunden | Liest Tabs „Übersicht" + „Händler A-Z" aus Google Sheet und aktualisiert den System-Prompt-Anhang |

---

## iNat-Tracker (`cogs/inat_tracker.py`)

Erkennt iNaturalist-Beobachtungslinks in einem Discord-Kanal und schreibt sie in ein separates Google Sheet – gedacht für Community-Events mit zeitlich begrenzter Erfassung.

**Funktionsweise:**
- Überwacht den konfigurierten `INAT_CHANNEL_ID` auf Nachrichten mit iNaturalist-Links
- Akzeptiert sowohl `http://` als auch `https://`-Links – schreibt immer `https`
- Verarbeitet nur Nachrichten innerhalb des konfigurierten Zeitfensters (`INAT_START` – `INAT_END`, Berliner Zeit)
- Vor dem Eintragen werden zwei Prüfungen durchgeführt:
  1. **Duplikat-Check:** Ist der Link bereits in Spalte D vorhanden? → ignorieren (wird geloggt)
  2. **Taxon-Check via iNaturalist API:** Gehört die Beobachtung zur Überfamilie Formicoidea (`taxon_id=1269340`)? → sonst ignorieren (wird geloggt)
- Reagiert mit ✅ wenn mindestens ein Link eingetragen wurde
- Ist die iNaturalist API nicht erreichbar: ⏳-Reaktion + automatischer Retry alle 5 Minuten bis die API antwortet; bei Erfolg wird ⏳ durch ✅ ersetzt
- Spalte C im Sheet wird bewusst nicht beschrieben (wird von der Tabelle selbst befüllt)

**Sheet-Struktur:**

| Spalte | Inhalt |
|--------|--------|
| A | Discord User-ID |
| B | Anzeigename auf dem Server (display_name) |
| C | *(leer – vom Sheet selbst befüllt)* |
| D | iNaturalist-Link (`https://www.inaturalist.org/observations/ID`) |
| E | Datum (Berliner Zeit, `DD.MM.YYYY`) |

**Konfiguration** (ganz oben in `cogs/inat_tracker.py`):

```python
INAT_CHANNEL_ID = 123456789012345678      # zu überwachender Kanal
INAT_SHEET_ID   = "DEINE_GOOGLE_SHEET_ID" # separates Sheet (nicht das Review-Sheet)
INAT_WORKSHEET  = "Tabelle1"              # Tab-Name
INAT_START      = "2026-06-26 18:00"      # Zeitfenster Beginn (Berliner Zeit)
INAT_END        = "2026-06-28 22:00"      # Zeitfenster Ende (Berliner Zeit)
```

Der Service Account (`service_account.json`) muss auch für das iNat-Sheet als Bearbeiter eingetragen sein.

---

## Grabber (`grabber.py`)

Eigenständiges Skript, das **nicht** Teil des Bots ist und separat läuft. Lädt Shops und Produkte von der AntCheck API v2 in zwei Schritten:

1. `GET /api/v2/ecommerce/shops?online=true&crawler_active=true` → alle aktiven Shops
2. `GET /api/v2/ecommerce/products?shop_id={id}&product_type=ants` → Produkte pro Shop

Ergebnis wird atomar als `shops_data.json` geschrieben (`.json.tmp` → rename).

Außerdem schreibt der Grabber aktuelle Preisdaten in `price_history.db` (Tabelle `product_price_history`) – diese Datei wird vom Bot für das Preis-Tracking gelesen (read-only).

**Empfohlener Cron-Job (stündlich):**

```cron
0 * * * * cd /opt/discord-bot && .venv/bin/python grabber.py >> /var/log/grabber.log 2>&1
```

---

## Datenbank

### `antcheckbot.db` (Bot-Datenbank)

SQLite-Datei, wird beim Start automatisch angelegt. Wichtige Tabellen:

| Tabelle | Inhalt |
|---------|--------|
| `shops` | Shopdaten: ID, Name, Land, URL, AAM-Bewertung, manuelle URL-Override |
| `notifications` | Benachrichtigungen mit Status (active / completed / expired / failed / pending_feedback) |
| `user_settings` | Sprache pro User |
| `server_settings` | Bot-Kanal + Sprache pro Server |
| `user_shop_blacklist` | Blacklisted Shops pro User |
| `shop_name_mappings` | Externer Shopname → interne Shop-ID (für Review-Bot) |
| `ch_delivery_shops` | Shops die nach CH liefern (manuell hinzugefügt) |
| `server_user_mappings` | User → Server-Zuordnung (für DM-Fallback) |
| `user_seen_products` | Bereits gemeldete Produkt-IDs (Deduplizierung) |
| `user_price_tracking` | Preis-Tracking: User → beobachtete Produkte mit Baseline-Preis und letzter Benachrichtigung |
| `review_tracking` | Discord-Nachrichten-ID → Sheet-Zeilennummer |
| `review_pending` | Ausstehende Nachrichten (unaufgelöster Shop / Parse-Fehler) |
| `global_stats` | Gesamtstatistiken (z.B. gelöschte Benachrichtigungen) |
| `eu_countries` | EU-Ländercodes (beim Start einmalig befüllt) |
| `ai_chat_budget` | KI-Chat Tagesbudgets pro User (date, user_id, cost_usd) |
| `ai_chat_history` | KI-Gesprächshistorie pro Bot-Nachricht-ID (TTL: 24h) |

### `price_history.db` (Grabber-Datenbank, read-only für den Bot)

Wird vom Grabber geschrieben und vom Bot nur gelesen. Enthält die Tabelle `product_price_history` mit dem Preisverlauf aller Produkte (product_id, min_price, max_price, currency_iso, recorded_at).

---

## Projektstruktur

```
.
├── main.py                  # Einstiegspunkt – lädt alle Cogs
├── config.py                # Zentrale Konfiguration + Umgebungsvariablen
├── grabber.py               # AntCheck API → shops_data.json + price_history.db
├── service_account.json     # Google Service Account (nicht im Git)
├── .env                     # Umgebungsvariablen (nicht im Git)
├── .env.example             # Vorlage
├── requirements.txt
├── shops_data.json          # Von grabber.py erzeugt (nicht im Git)
├── antcheckbot.db           # SQLite Bot-Datenbank (nicht im Git)
├── price_history.db         # SQLite Preishistorie – vom Grabber befüllt (nicht im Git)
├── shop_mapping.csv         # Manuelles Shop-Mapping (nicht im Git)
├── ai_chat_system_prompt_de.txt  # System-Prompt Deutsch
├── ai_chat_system_prompt_en.txt  # System-Prompt Englisch
├── ai_chat_system_prompt_eo.txt  # System-Prompt Esperanto
├── ai_chat_system_prompt.txt     # Legacy-Datei (wird als de-Fallback erkannt)
│
├── cogs/
│   ├── server_settings.py   # /startup + allowed_channel/admin_or_manage_messages Decorators
│   ├── reviews.py           # Review-Bot: on_message, on_edit, on_reaction, Reconcile
│   ├── admin.py             # /status /pending /test /rescan /reprocess /export
│   ├── user_settings.py     # /usersetting language / blacklist / shop_list
│   ├── notifications.py     # /notification /delete_notifications /history /testnotification
│   ├── price_tracking.py    # /track_price /my_price_tracking /untrack_price + Preis-Check Task
│   ├── stats.py             # /stats /system /help
│   ├── shop_admin.py        # /reloadshops /shopmapping /shopurl /ch_delivery
│   ├── tasks.py             # Alle Hintergrundaufgaben
│   ├── ai_chat.py           # KI-Chat-Bot: on_message, /ai_status, /ai_reset, /ai_prompt
│   └── inat_tracker.py      # iNat-Tracker: iNaturalist-Links → Google Sheets
│
├── utils/
│   ├── db.py                # SQLite-Helfer (execute_db, init_db, Schema)
│   ├── availability.py      # Verfügbarkeitsprüfung gegen shops_data.json
│   ├── currency.py          # Währungsumrechnung via Frankfurter API (6h Cache)
│   ├── sheet.py             # Google Sheets Cache (SheetCache) + Rating-Sync
│   ├── shop.py              # Shop-Auflösung + CSV-Mapping (Review-Bot)
│   ├── ai_parser.py         # Claude Haiku Parser (Review-Bot)
│   ├── ai_chat.py           # KI-Chat-Backend: Budget, History, API-Call
│   ├── sheets_shop_data.py  # Shop-Daten aus Google Sheets für KI-System-Prompt
│   ├── tracking.py          # Review-Tracking (Discord-ID → Sheet-Zeile)
│   ├── localization.py      # Lokalisierungssystem (de/en/eo)
│   └── logging_setup.py     # Rotating File Handler
│
└── locales/
    ├── de.json              # Deutsch
    ├── en.json              # English
    └── eo.json              # Esperanto
```

---

## Lokalisierung

Der Bot ist vollständig dreisprachig (de / en / eo). Die Sprache gilt für **alle** User-sichtbaren Ausgaben:

- **Bot-Texte** (Fehlermeldungen, Embed-Titel, Disclaimer, Slash-Command-Antworten): `locales/{de,en,eo}.json`. Neue Keys müssen in alle drei Dateien eingetragen werden.
- **KI-Antworten**: Die KI verwendet den System-Prompt der User-Sprache (`ai_chat_system_prompt_{lang}.txt`) und antwortet entsprechend auf Deutsch, Englisch oder Esperanto.

Sprachauswahl-Reihenfolge:

1. Eigene User-Einstellung (`/usersetting language`)
2. Server-Einstellung (`/startup language`)
3. Englisch als Fallback

---

## Deployment (Linux / systemd)

Empfohlene Verzeichnisstruktur auf dem Server:

```
/opt/discord-bot/
├── .venv/
├── .env
├── service_account.json
└── (alle Bot-Dateien)
```

Systemd-Unit `/etc/systemd/system/aam-bot.service`:

```ini
[Unit]
Description=AAM Discord Bot
After=network.target

[Service]
Type=simple
User=aam
WorkingDirectory=/opt/discord-bot
EnvironmentFile=/opt/discord-bot/.env
ExecStart=/opt/discord-bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable aam-bot
systemctl start aam-bot
journalctl -u aam-bot -f    # Logs live verfolgen
```

---

## Lizenz

Dieses Projekt steht unter der **GNU Affero General Public License v3.0 oder später (AGPL-3.0-or-later)**.

Copyright (C) 2026 Jonas Beier

Jede Person, die eine modifizierte Version dieses Bots als Netzwerkdienst betreibt, ist verpflichtet, den Quellcode ihrer Änderungen öffentlich zugänglich zu machen.

Weitere Details: [LICENSE](LICENSE) · [gnu.org/licenses/agpl-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
