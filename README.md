# AAM Discord Bot

Modularer Discord-Bot für die **Ameisen an die Macht**-Community. Kombiniert mehrere eigenständige Funktionen in einem Bot:

- **Review-Bot** – erkennt Shopbewertungen in einem Discord-Kanal, parst sie automatisch mit Claude Haiku (KI) und schreibt sie strukturiert in ein Google Sheet
- **AntCheck-Bot** – überwacht die Verfügbarkeit von Ameisenarten bei Online-Shops via AntCheck API und benachrichtigt User per DM sobald eine gesuchte Art verfügbar ist; Preise werden in der jeweiligen Währung inklusive EUR-Umrechnungshinweis angezeigt
- **Preis-Tracking** – beobachtet Preise einzelner Produkte und informiert per DM sobald sich ein Preis ändert; interaktive Auswahl über Shop → Produkt → Bestätigen. Alternativ: **Arten-Beobachtung** für eine ganze Art oder Gattung shopübergreifend – benachrichtigt bei Preisänderungen (Neuerscheinungen werden still in die Beobachtung aufgenommen, aber nicht separat gemeldet – dafür gibt es `/notification`)
- **Rabattcode-Tracker** – sammelt automatisch Rabattcodes aus einem Discord-Kanal (KI-Extraktion via Claude Haiku) und stellt die aktuell gültigen Codes per `/codes` bereit
- **AI-Chat-Bot** – beantwortet Fragen im konfigurierten AI-Kanal auf @-Erwähnung mit Claude Sonnet, inkl. Konversationsgedächtnis (per Discord-Reply), Tagesbudget-Kontrolle und Shop-Wissen aus dem AAM Google Sheet *(im AAM Discord aktuell nicht öffentlich verfügbar)*
- **iNat-Tracker** – erkennt iNaturalist-Beobachtungslinks in einem konfigurierten Kanal innerhalb eines definierten Zeitfensters und trägt sie automatisch (Discord-ID, Anzeigename, Link, Datum) in ein separates Google Sheet ein

---

## Inhaltsverzeichnis

0. [Inhaltsverzeichnis](#inhaltsverzeichnis)
1. [Voraussetzungen](#voraussetzungen)
2. [Installation](#installation)
3. [Konfiguration](#konfiguration)
4. [Erster Start & Server-Einrichtung](#erster-start--server-einrichtung)
5. [Review-Bot](#review-bot)
6. [AntCheck-Bot](#antcheck-bot)
7. [Preis-Tracking](#preis-tracking)
8. [Rabattcode-Tracker](#rabattcode-tracker)
9. [AI-Chat-Bot](#ai-chat-bot)
10. [iNat-Tracker](#inat-tracker)
11. [Slash Commands](#slash-commands)
12. [Hintergrundaufgaben](#hintergrundaufgaben)
13. [Grabber](#grabber)
14. [Datenbank](#datenbank)
15. [Projektstruktur](#projektstruktur)
16. [Lokalisierung](#lokalisierung)

---

## Voraussetzungen

- Python 3.11+
- Discord-Bot-Token ([discord.com/developers](https://discord.com/developers/applications)) mit aktivierten Intents: **Message Content**, **Server Members**, **Reactions**
- Google Service Account JSON für Sheets-Zugriff (`service_account.json`)
- Anthropic API Key für Claude Haiku (KI-Parser)
- AntCheck API Key (für den Grabber)

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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
| `matplotlib>=3.7.0` | Ranking-Bild (iNat-Treppchen, lokal gerendert) |

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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

# ── Rabattcode-Tracker ────────────────────────────────────────
DISCOUNT_CHANNEL_ID=123456789012345678   # Kanal mit Rabattcodes (leer/0 = inaktiv)
# DISCOUNT_PARSER_MODEL=claude-haiku-4-5-20251001   # Modell für die Code-Extraktion

# ── Pfade (optional) ──────────────────────────────────────────
DATA_DIRECTORY=/opt/discord-bot          # Wo shops_data.json abgelegt wird

# ── Python ────────────────────────────────────────────────────
PYTHONUNBUFFERED=1
```

Alle Limits (Eingabezeichenanzahl, Output-Tokens, Konversationsgedächtnis, TTL) haben sinnvolle Defaults und müssen nur gesetzt werden wenn sie angepasst werden sollen – siehe `.env.example`.

Lege außerdem die Google Service Account Datei als `service_account.json` im Projektordner ab (wird in `.gitignore` ignoriert).

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Review-Bot

### Funktionsweise

Der Review-Bot überwacht den konfigurierten `REVIEW_CHANNEL_ID` auf neue Shopbewertungen.

**Erkennung:** Eine Nachricht wird als Bewertung erkannt wenn sie das 🛒-Emoji enthält **oder** sowohl `Shop:` als auch `Fazit`, `/10` oder `/5` enthält.

**Geteilte Nachrichten:** Schickt ein User mehrere Nachrichten hintereinander (z. B. weil Discord die Zeichengrenze erreicht), wartet der Bot `ACCUMULATION_DELAY` Sekunden (Standard: 8) nach der letzten Nachricht und führt alle Teile automatisch zu einer Review zusammen.

**Shop-Auflösung** (in dieser Reihenfolge):
1. `shop_mapping.csv` – manuell oder automatisch gelernte Mappings
2. Discord-Mention (`@User`) → Display-Name wenn URL-artig
3. Fuzzy-Match gegen bekannte Sheet-Shopnamen (≥81 % Ähnlichkeit)
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

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Preis-Tracking

Ergänzend zur Verfügbarkeitsbenachrichtigung gibt es zwei Modi:

### Modus 1: Einzelprodukt-Tracking

Beobachtet gezielt konkrete Produkte und benachrichtigt per DM bei jeder Preisänderung.

**`/track_price species:<Art oder Gattung>`**

1. **Shop auswählen** – Dropdown (max. 24 Shops) + erste Option „🔭 Alle Shops beobachten" (→ Modus 2)
2. **Produkte auswählen** – Multi-Select; Status als Emoji-Icon direkt am Eintrag sichtbar:
   - ✅ Verfügbar – aktueller Preis
   - ❌ Nicht verfügbar – aktueller Preis
   - ⏸️ Zuletzt gesehen – letzter bekannter Preis aus `price_history.db`
   - ❓ Kein Preis bekannt – noch nie erfasst
   
   Wenn mehrere Produkte dieselbe Art haben, wird die ID als Fallback angehängt (`Messor galla (#42)`). Sobald die API Varianteninfo in `description` liefert, wird diese stattdessen genutzt.

3. **Bestätigen** – aktueller Preis als Baseline, öffentliche Ankündigung im Kanal

**Hintergrund-Check alle ~65 Minuten:** Preis gesunken → 📉-DM, gestiegen → 📈-DM.

### Modus 2: Arten-Beobachtung (alle Shops)

Beobachtet **alle** Produkte einer Art oder Gattung **shopübergreifend** – ohne Shop- oder Produktauswahl.

**Aktivieren:** Im Shop-Dropdown „🔭 Alle Shops beobachten" wählen → Bestätigung.

**DM wird ausgelöst bei:**
- **Preisänderung** an einem bekannten Produkt → 📉 / 📈

Neue Produkte werden beim nächsten Check automatisch zur Baseline hinzugefügt und ab dann auf Preisänderungen beobachtet – ohne eigene DM (Neuerscheinungen deckt `/notification` ab).

Beim Einrichten werden alle aktuell bekannten Produkte sofort als Baseline gespeichert (kein Spam).

**Hintergrund-Check alle ~67 Minuten** (läuft parallel zu Modus 1).

`/my_price_tracking` zeigt Arten-Beobachtungen (🔭) oben getrennt von Einzelprodukten (🏷️).  
`/untrack_price` zeigt beides gemeinsam im Dropdown – in einer Interaktion entfernbar.

### DM-Fallback

Falls DMs des Users blockiert sind, wird der Server-Kanal als Fallback genutzt.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Rabattcode-Tracker

Liest in einem konfigurierten Kanal (`DISCOUNT_CHANNEL_ID`) Nachrichten, extrahiert per Claude Haiku Rabattcodes (Shop, Code, Rabatthöhe, Gültigkeitszeitraum, ggf. Mindestbestellwert) und speichert sie in der Datenbank. Ist kein Kanal gesetzt, bleibt das Feature inaktiv.

### Funktionsweise

- **Einmal pro Nachricht:** Jede verarbeitete `message_id` wird in `discount_scanned` festgehalten, damit dieselbe Nachricht nie zweimal an Haiku geschickt wird.
- **Backfill beim Start:** Beim ersten `on_ready` wird der gesamte Kanal (älteste zuerst) durchgegangen; bereits gescannte Nachrichten werden übersprungen. Mehrfaches `on_ready` (Reconnects) löst keinen erneuten Scan aus.
- **Live:** Neue Posts im Kanal werden sofort verarbeitet (Reaktion 🏷️ bei gefundenem Code).
- **Kein Keyword-Vorfilter:** Jede nicht-leere Nachricht geht an Haiku, das im Zweifel selbst entscheidet (kein Code → leeres Ergebnis). Rein bildbasierte Posts ohne Text werden ohne API-Aufruf übersprungen und nur als gescannt markiert.
- **Datumslogik:** Relative/teilweise Angaben werden anhand des Nachrichtendatums aufgelöst (`nur heute`, `bis morgen`, `bis 14.06.`, `vom X bis Y`); Saison-Aktionen ohne Enddatum (Black Friday, Ostern, …) erhalten ein geschätztes Enddatum; `dauerhaft`/`immer` ⇒ permanenter Code ohne Enddatum. Codes **ohne** Enddatum (und nicht permanent) gelten ab 90 Tagen nach der Quellnachricht automatisch als abgelaufen, damit alte Saison-Codes nicht ewig als „aktuell" erscheinen.
- **Shop-Normalisierung:** Für Anzeige und Duplikat-Erkennung wird der Shop auf seine Domain reduziert (`Ant Farm Supplies`, `antfarmsupplies.com`, `AntFarmSupplies.com` ⇒ derselbe Shop).
- **Mehrere Codes pro Nachricht** werden unterstützt (z. B. Sammel-Posts mit mehreren Shops).

### Anzeige

`/codes` listet standardmäßig nur gültige Codes: permanente, solche ohne Enddatum, alle mit `valid_until` ≥ heute sowie manuell als gültig markierte. Abgelaufene werden ausgeblendet, Duplikate (gleicher Shop + Code) zusammengefasst. Mit der Option `show_expired:true` werden zusätzlich abgelaufene (⌛) und manuell deaktivierte (🚫) Codes angezeigt.

**Manuelle Steuerung:** Admins können mit `/codes_set <code> <status>` einen Code übersteuern – `valid` (immer gültig), `invalid` (immer ausgeblendet) oder `auto` (zurück zur Datumslogik); optional auf einen `shop` begrenzt. Mit `/codes_rescan` lässt sich der Kanal nach noch nicht gescannten Nachrichten durchsuchen (bereits Gescanntes wird übersprungen). Ein kompletter Neuaufbau erfolgt bewusst nicht per Befehl – dafür die Tabellen `discount_codes`/`discount_scanned` manuell leeren.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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

**System-Prompt:** Wird beim Start aus sprachspezifischen Dateien geladen – `ai_chat_system_prompt_de.txt`, `ai_chat_system_prompt_en.txt`, `ai_chat_system_prompt_eo.txt`. Der Platzhalter `{model}` wird automatisch durch das konfigurierte Modell ersetzt. Jeder Prompt ist vollständig in der jeweiligen Sprache verfasst und konfiguriert die KI als AAM-Community-Assistent für Ameisenhaltung, inkl. Quellenpflicht, Jugendschutz und Discord-Markdown-Formatierung. Die `en`-Datei ist Pflicht und dient als Fallback für alle Sprachen – fehlt sie, wird beim Start ein Fehler geloggt und der KI-Chat lehnt Anfragen mit einer Fehlermeldung ab.

**Shop-Wissen:** Beim Start und alle 6 Stunden werden die Tabs **„Übersicht"** und **„Händler A-Z"** aus dem AAM Google Sheet geladen. Händler A-Z wird kompakt aufbereitet (`shopname ⭐9.97 (63x)`) und auf Shops mit **mindestens 4 Bewertungen** gefiltert. Der Shop-Block wird nur bei shop-relevanten Anfragen in den System-Prompt eingebettet – per **3-stufiger Vorqualifizierung**:

1. **Keyword-Check** (kostenlos): enthält die Nachricht shop-relevante Begriffe oder einen bekannten Shop-Namen? → ja: Shop-Daten rein
2. **Haiku-Klassifikation** (~$0.00025): kein Keyword gefunden – Haiku entscheidet ob die Frage indirekt shop-relevant ist (z.B. „wo kaufe ich günstig?")
3. **Sonnet-Hauptaufruf**: mit oder ohne Shop-Block je nach Stage 1/2. Haiku-Kosten werden immer zum Gesamtbetrag addiert und im Disclaimer angezeigt.

Nutzt denselben Service Account und dieselbe Spreadsheet-ID wie der Review-Bot – keine extra Konfiguration nötig.

**Disclaimer:** Jede Antwort wird automatisch im Code um einen Disclaimer ergänzt (nicht durch die KI selbst), inkl. der tatsächlichen Anforderungskosten und einem Link zum Quellcode:
> -# 🤖 KI-Antwort – nur zur Orientierung, kein Ersatz für Fachrat. Angaben immer selbst prüfen! · 💰 $0.00312 · Quellcode: https://github.com/JonasVerzockt/Discord-Bot

**Modell:** Standard `claude-haiku-4-5-20251001`, konfigurierbar per `AI_CHAT_MODEL` – aktuell `claude-sonnet-4-6` (unterstützt Text und Vision).

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## iNat-Tracker

Erkennt iNaturalist-Beobachtungslinks in einem Discord-Kanal und schreibt sie in ein separates Google Sheet – gedacht für Community-Events mit zeitlich begrenzter Erfassung.

**Funktionsweise:**
- Überwacht den konfigurierten `INAT_CHANNEL_ID` auf Nachrichten mit iNaturalist-Links (mit oder ohne `www.`)
- Akzeptiert sowohl `http://` als auch `https://`-Links – schreibt immer `https`
- Verarbeitet nur Nachrichten innerhalb des konfigurierten Zeitfensters (`INAT_START` – `INAT_END`, Berliner Zeit)
- Vor dem Eintragen werden zwei Prüfungen durchgeführt:
  1. **Duplikat-Check:** Ist der Link bereits in Spalte D vorhanden? → ignorieren (wird geloggt)
  2. **Taxon-Check via iNaturalist API:** Gehört die Beobachtung zur Überfamilie Formicoidea (`taxon_id=1269340`)? → sonst ignorieren (wird geloggt)
- Reagiert mit ✅ wenn mindestens ein Link eingetragen wurde
- Ist die iNaturalist API nicht erreichbar: ⏳-Reaktion + automatischer Retry alle 5 Minuten bis die API antwortet; bei Erfolg wird ⏳ durch ✅ ersetzt
- Spalte C im Sheet wird bewusst nicht beschrieben (wird von der Tabelle selbst befüllt)

**Ranking-Snapshot:**

Nach jeweils `INAT_SNAPSHOT_EVERY` (Standard: 5) neu eingetragenen Beobachtungen liest der Bot den Tab `INAT_UEBERSICHT` (Standard: `Übersicht`, Spalten **A = Rang, B = Name, C = Anzahl Arten**, Kopfzeile in Zeile 1) und rendert daraus **lokal mit matplotlib** eine farbige Treppchen-Grafik (Top 3 in Gold/Silber/Bronze, Platz 4+ als Tabelle), die er im Channel postet. Es wird **kein** Google-PNG-Export mehr verwendet – das Bild entsteht komplett im Bot, daher keine flakigen Export-Fehler. Bei **Gleichstand** (gleiche Artenzahl) teilen sich mehrere Personen denselben Rang und dieselbe Treppchen-Stufe (Competition-Ranking: 1, 1, 3, …).

Ablauf:
1. Warten bis Spalte Z2 im Übersicht-Tab leer ist (evtl. läuft noch ein anderer Job)
2. Apps Script via Web App triggern (falls `INAT_WEBAPP_URL` konfiguriert)
3. 10 Sekunden warten damit das Script Z2 auf `block` setzen kann
4. Warten bis Z2 wieder leer ist – max. `INAT_Z2_TIMEOUT` Sekunden (Standard: 600)
5. Daten `A1:C` lesen, lokal als Treppchen-PNG (matplotlib) rendern und im Channel posten. Schlägt das Rendern fehl, wird das Ranking als **Text-Tabelle** (bzw. als `ranking.txt`, falls zu lang) gepostet – die Rangliste geht also nie verloren.

Das Z2-Flag (`block`) wird vom Apps Script gesetzt solange es rechnet und gelöscht wenn es fertig ist – der Bot wartet geduldig.

**Manueller Trigger:** Schreibt jemand im iNat-Channel exakt `Rangliste` (nur dieses Wort), wird der Snapshot-Prozess sofort ausgelöst – unabhängig vom Eintrags-Zähler, aber nur **innerhalb des konfigurierten Zeitfensters** (`INAT_START`–`INAT_END`). Cooldown: 1 Minute (⏱️-Reaktion wenn zu früh).

**Sheet-Struktur (Rohdaten-Tab):**

| Spalte | Inhalt |
|--------|--------|
| A | Discord Username (z.B. `jonasverzockt`) |
| B | Anzeigename auf dem Server (display_name) |
| C | *(leer – vom Sheet selbst befüllt)* |
| D | iNaturalist-Link (`https://www.inaturalist.org/observations/ID`) |
| E | Datum (Berliner Zeit, `DD.MM.YYYY`) |

**Konfiguration** (ganz oben in `cogs/inat_tracker.py`):

```python
INAT_CHANNEL_ID      = 123456789012345678       # zu überwachender Kanal
INAT_SHEET_ID        = "DEINE_GOOGLE_SHEET_ID"  # separates Sheet (nicht das Review-Sheet)
INAT_WORKSHEET       = "Rohdaten"               # Tab mit den Rohdaten
INAT_UEBERSICHT      = "Übersicht"              # Tab mit dem Ranking (für Snapshot)
INAT_START           = "2026-06-05 00:00"       # Zeitfenster Beginn (Berliner Zeit)
INAT_END             = "2026-10-30 20:00"       # Zeitfenster Ende (Berliner Zeit)
INAT_SNAPSHOT_EVERY  = 5                        # Snapshot nach jeweils N Einträgen
INAT_Z2_TIMEOUT      = 600                      # Max. Wartezeit auf Z2-Freigabe (Sekunden)
```

Über `.env` optional:

```env
INAT_WEBAPP_URL=https://script.google.com/macros/s/.../exec   # Apps Script Web App URL
INAT_WEBAPP_SECRET=dein-secret                                  # Muss mit BOT_TRIGGER_SECRET im Script übereinstimmen
```

Der Service Account (`service_account.json`) muss auch für das iNat-Sheet als Bearbeiter eingetragen sein und den Scope `drive.readonly` für den PNG-Export besitzen.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Slash Commands

### Für alle User (nur im Bot-Kanal)

| Befehl | Parameter | Beschreibung |
|--------|-----------|-------------|
| `/notification` | `species` oder `genus` (Pflicht, nicht beides), `regions` (z.B. `de,at` oder `eu`), `swiss_only`, `exclude_species`, `force` | Verfügbarkeitsbenachrichtigung einrichten. `regions: eu` wird automatisch auf alle EU-Ländercodes aufgelöst. `exclude_species` schließt bestimmte Arten innerhalb einer Gattungs-Suche aus. `force: True` überspringt die Prüfung ob die Art in der DB vorkommt. |
| `/delete_notifications` | `ids` (komma- oder leerzeichengetrennte Benachrichtigungs-IDs) | Eigene Benachrichtigungen löschen. Die IDs sind aus `/history` ersichtlich. |
| `/history` | – | Zeigt die letzten 20 eigenen Benachrichtigungen mit ID, Art, Region und Status (active / completed / expired / failed). Als zweites Embed: Übersicht über aktive Preis-Tracking-Einträge (Einzelprodukte mit Shops und ältestem Eintrag, Arten-Beobachtungen mit Datum). |
| `/testnotification` | – | Schickt eine Test-DM an sich selbst, um zu prüfen ob DMs vom Bot empfangen werden. |
| `/track_price` | `species` (Art oder Gattung, Pflicht) | Startet die interaktive Preis-Tracking-Einrichtung. Erste Option im Shop-Dropdown ist **Alle Shops beobachten** (Arten-Beobachtung: Preisänderungen + Neuerscheinungen shopübergreifend). Alternativ: spezifischer Shop mit Produkt-Auswahl (Mehrfachauswahl). Aktueller Preis als Baseline. |
| `/my_price_tracking` | – | Listet alle aktiven Preis-Beobachtungen: oben Arten-Beobachtungen (🔭, alle Shops) mit Startdatum, darunter Einzelprodukte mit aktuellem Preis. |
| `/untrack_price` | – | Zeigt Einzelprodukte und Arten-Beobachtungen gemeinsam im Multi-Select-Dropdown und entfernt die ausgewählten. |
| `/usersetting language` | `language` (`de` / `en` / `eo`) | Eigene Sprache setzen. Wirkt auf alle Bot-Antworten – Slash-Command-Ausgaben, DMs und KI-Antworten. |
| `/usersetting blacklist_add` | `shop` (Name oder Teile davon, Fuzzy-Match) | Shop dauerhaft von Verfügbarkeits-DMs ausschließen. Der Bot sucht den besten Treffer im Shop-Verzeichnis. |
| `/usersetting blacklist_remove` | `shop` | Shop wieder in Benachrichtigungen einschließen. |
| `/usersetting blacklist_list` | – | Eigene Blacklist anzeigen (Shop-Name + ID). |
| `/usersetting shop_list` | `country` (optional, z.B. `de`) | Alle bekannten Shops anzeigen, optional nach Länderkürzel gefiltert. Zeigt Name, URL und AAM-Rating. |
| `/ch_delivery add` | `shop` | Shop manuell zur CH-Lieferliste hinzufügen (für `swiss_only`-Benachrichtigungen). Automatische CH-Shops (aus `country=ch` in der API) werden immer einbezogen. |
| `/ch_delivery list` | – | CH-Lieferliste anzeigen: automatisch erkannte Shops (aus API) und manuell hinzugefügte. |
| `/ai_status` | – | Eigenen KI-Chat Budget-Status anzeigen: aktuell verbrauchte Kosten, verbleibendes persönliches und globales Tagesbudget sowie Uhrzeit des nächsten Resets. |
| `/codes` | `show_expired` (optional) | Aktuell gültige Rabattcodes anzeigen (permanente, ohne Enddatum, noch nicht abgelaufene sowie manuell gültig markierte). Pro Shop+Code nur ein Eintrag. Mit `show_expired:true` werden auch abgelaufene (⌛) und manuell deaktivierte (🚫) Codes mit angezeigt. |
| `/help` | – | Befehlsübersicht (lokalisiert in der eingestellten Sprache). Antwort ist **öffentlich** sichtbar im Kanal. |

### Nur Admin / Nachrichten verwalten

| Befehl | Parameter | Beschreibung |
|--------|-----------|-------------|
| `/startup` | `language` (`de`/`en`/`eo`), `channel` | Bot-Kanal und Sprache für diesen Server festlegen. Muss einmalig pro Server aufgerufen werden. |
| `/status` | – | Zeigt die Anzahl der Bewertungen im Google Sheet, die Zahl der verarbeiteten Reviews und die ausstehenden (🟡) Nachrichten. |
| `/pending` | – | Listet alle ausstehenden Nachrichten mit Message-ID, Grund und kurzem Nachrichtenausschnitt. |
| `/test` | `text` | KI-Parser mit einem frei eingegebenen Bewertungstext testen (ohne Sheet-Eintrag). Zeigt das von der KI extrahierte JSON. |
| `/rescan` | – | Gleicht die letzten 90 Tage Discord-History manuell mit dem Google Sheet ab. Nützlich nach manuellen Sheet-Korrekturen oder Bot-Ausfällen. |
| `/reprocess` | `ids` (Leerzeichen- oder kommagetrennte Message-IDs) | Bewertungsnachricht(en) neu verarbeiten. Mehrere IDs werden zu einem einzigen Sheet-Eintrag zusammengeführt (für geteilte Nachrichten). |
| `/export` | `user_id` (optional) | Ohne Parameter: alle DB-Tabellen als JSON-Datei (Admin-Debug, max. 500 Zeilen/Tabelle). Mit `user_id`: alle gespeicherten Daten des Users als JSON per DM (DSGVO-Auskunft). |
| `/stats` | – | Benachrichtigungsstatistiken: aktive, abgeschlossene, abgelaufene und gelöschte Benachrichtigungen sowie die Top-5-gesuchten Arten. |
| `/system` | – | Systemstatus: Uptime, Server-/Nutzerzahl, DB-Integrität, Gesamtzahl Benachrichtigungen, Alter der `shops_data.json`, Latenz, CPU- und RAM-Auslastung, Betriebssystem. |
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
| `/codes_set` | `code`, `status` (`valid` / `invalid` / `auto`), `shop` (optional) | Einen Rabattcode manuell als **immer gültig**, **ungültig** oder zurück auf **automatisch** (Datumslogik) setzen. Ohne `shop` werden alle Einträge mit diesem Code aktualisiert, sonst nur die des angegebenen Shops. |
| `/codes_rescan` | – | Rabattcode-Kanal nach noch nicht gescannten Nachrichten durchsuchen (z. B. nachdem der Bot offline war). Bereits gescannte Nachrichten werden übersprungen. |

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Hintergrundaufgaben

| Task | Intervall | Beschreibung |
|------|-----------|-------------|
| Verfügbarkeitsprüfung | alle 5 Minuten | Prüft alle `active`-Benachrichtigungen gegen `shops_data.json` |
| Preis-Check Einzelprodukte | alle ~65 Minuten | Vergleicht aktuelle Preise aus `price_history.db` mit gespeicherten Baselines; sendet DM bei Preisänderung |
| Arten-Beobachtung alle Shops | alle ~67 Minuten | Prüft alle Arten-Beobachtungen shopübergreifend; sendet DM bei Preisänderung; neue Produkte werden still zur Baseline hinzugefügt |
| Shop-Daten-Reload | stündlich | Liest `shops_data.json` neu, schreibt Shops in DB (ohne `average_rating` und `url_override` zu überschreiben) |
| Shop-Ratings-Sync | alle 48 Stunden | Liest AAM-Bewertungen aus Google Sheet „Händler A-Z": erst Domain-Exact-Match, dann Fuzzy-Fallback ≥81 % |
| Abgelaufene Benachrichtigungen | täglich | Markiert Benachrichtigungen >365 Tage als `expired` und sendet Abschluss-DM |
| DB VACUUM + ANALYZE | wöchentlich | Optimiert die SQLite-Datenbank |
| Bot-Status | alle 2 Minuten | Rotierender Discord-Status mit Ameisen-Sprüchen (20 Quotes) |
| AI-Chat Konversations-Cleanup | alle 6 Stunden | Löscht abgelaufene Konversationshistorien (>24h TTL) |
| AI-Chat Shop-Daten-Refresh | alle 6 Stunden | Liest Tabs „Übersicht" + „Händler A-Z" aus Google Sheet und aktualisiert den System-Prompt-Anhang |

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Grabber

Eigenständiges Skript, das **nicht** Teil des Bots ist und separat läuft. Lädt Shops und Produkte von der AntCheck API v2 in zwei Schritten:

1. `GET /api/v2/ecommerce/shops?online=true&crawler_active=true` → alle aktiven Shops
2. `GET /api/v2/ecommerce/products?shop_id={id}&product_type=ants` → Produkte pro Shop

Ergebnis wird atomar als `shops_data.json` geschrieben (`.json.tmp` → rename).

Außerdem schreibt der Grabber aktuelle Preisdaten in `price_history.db` (Tabelle `product_price_history`) – diese Datei wird vom Bot für das Preis-Tracking gelesen (read-only).

**Empfohlener Cron-Job (stündlich):**

```cron
0 * * * * cd /opt/discord-bot && .venv/bin/python grabber.py >> /var/log/grabber.log 2>&1
```

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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
| `user_species_watch` | Arten-Beobachtung: User → beobachtete Arten/Gattungen shopübergreifend |
| `user_species_watch_seen` | Bekannte Produkt-IDs + letzter Preis je Arten-Beobachtung (Baseline) |
| `review_tracking` | Discord-Nachrichten-ID → Sheet-Zeilennummer |
| `review_pending` | Ausstehende Nachrichten (unaufgelöster Shop / Parse-Fehler) |
| `global_stats` | Gesamtstatistiken (z.B. gelöschte Benachrichtigungen) |
| `eu_countries` | EU-Ländercodes (beim Start einmalig befüllt) |
| `ai_chat_budget` | KI-Chat Tagesbudgets pro User (date, user_id, cost_usd) |
| `ai_chat_history` | KI-Gesprächshistorie pro Bot-Nachricht-ID (TTL: 24h) |
| `discount_scanned` | Bereits an Haiku geschickte Nachrichten-IDs (Rabattcode-Tracker, nur einmal parsen) |
| `discount_codes` | Extrahierte Rabattcodes (Shop, Code, Rabatt, Gültigkeit, Mindestbestellwert, `status_override` für manuell gültig/ungültig) |

### `price_history.db` (Grabber-Datenbank, read-only für den Bot)

Wird vom Grabber geschrieben und vom Bot nur gelesen. Enthält die Tabelle `product_price_history` mit dem Preisverlauf aller Produkte (product_id, min_price, max_price, currency_iso, recorded_at).

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

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
│   ├── inat_tracker.py      # iNat-Tracker: iNaturalist-Links → Google Sheets
│   └── discount_codes.py    # Rabattcode-Tracker: Haiku-Parsing + /codes /codes_rescan
│
├── utils/
│   ├── db.py                # SQLite-Helfer (execute_db, init_db, Schema)
│   ├── availability.py      # Verfügbarkeitsprüfung gegen shops_data.json
│   ├── currency.py          # Währungsumrechnung via Frankfurter API (6h Cache)
│   ├── sheet.py             # Google Sheets Cache (SheetCache) + Rating-Sync
│   ├── shop.py              # Shop-Auflösung + CSV-Mapping (Review-Bot)
│   ├── ai_parser.py         # Claude Haiku Parser (Review-Bot)
│   ├── discount_parser.py   # Claude Haiku Parser (Rabattcodes)
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

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Lokalisierung

Der Bot ist vollständig dreisprachig (**de** / **en** / **eo**). Die eingestellte Sprache gilt für **alle** User-sichtbaren Ausgaben: Slash-Command-Antworten, DMs (Verfügbarkeit, Preis-Tracking, Feedback), KI-Chat-Antworten und die Rabattcode-Ausgaben.

**Sprachauflösung** (in dieser Reihenfolge):

1. Persönliche Einstellung des Users (`/usersetting language`)
2. Server-Einstellung (`/startup`)
3. Fallback `en`

Für Bot-initiierte Kanal-Nachrichten ohne direkten User-Kontext wird die Server-Sprache verwendet.

**Technik:**

- Alle Texte liegen als JSON in `locales/de.json`, `locales/en.json` und `locales/eo.json` – in allen Dateien dieselbe Key-Menge.
- Geladen beim Start über die `Localization`-Klasse (`utils/localization.py`); Zugriff im Code via `l10n.get("key", lang, **platzhalter)`.
- Fehlt ein Key in der Zielsprache, wird automatisch auf `en` zurückgegriffen, danach auf den Key-Namen selbst (`[key]`) – es fällt also nie eine Ausgabe komplett aus.
- Platzhalter wie `{species}`, `{shop}` oder `{date}` werden zur Laufzeit eingesetzt.

**Neue Sprache hinzufügen:** eine weitere `locales/<code>.json` mit denselben Keys anlegen – sie wird beim Start automatisch eingelesen. Damit die Sprache auch auswählbar ist, müssen die `choices`-Listen von `/usersetting language` und `/startup` (aktuell `

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)
