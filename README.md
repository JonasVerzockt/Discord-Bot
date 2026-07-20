# AAM Discord Bot

**Aktuelle Version:** `1.0.23` · Lizenz: AGPL-3.0-or-later

Modularer Discord-Bot für die **Ameisen an die Macht**-Community. Kombiniert mehrere eigenständige Funktionen in einem Bot:

- **Review-Bot** – erkennt Shopbewertungen in einem Discord-Kanal, parst sie automatisch mit Claude Haiku (KI) und schreibt sie strukturiert in ein Google Sheet
- **AntCheck-Bot** – überwacht die Verfügbarkeit von Ameisenarten bei Online-Shops via AntCheck API und benachrichtigt User per DM sobald eine gesuchte Art verfügbar ist; Preise werden in der jeweiligen Währung inklusive EUR-Umrechnungshinweis angezeigt
- **Preis-Tracking** – beobachtet Preise einzelner Produkte und informiert per DM sobald sich ein Preis ändert; interaktive Auswahl über Shop → Produkt → Bestätigen. Alternativ: **Arten-Beobachtung** für eine ganze Art oder Gattung shopübergreifend – benachrichtigt bei Preisänderungen (Neuerscheinungen werden still in die Beobachtung aufgenommen, aber nicht separat gemeldet – dafür gibt es `/notification`)
- **Rabattcode-Tracker** – sammelt automatisch Rabattcodes aus einem Discord-Kanal (KI-Extraktion via Claude Haiku), erkennt sie auch in geposteten **Bildern** (Screenshots, Flyer, Shop-Werbung) per Vision und stellt die aktuell gültigen Codes per `/codes` bereit
- **AI-Chat-Bot** – beantwortet Fragen im konfigurierten AI-Kanal auf @-Erwähnung mit Claude Sonnet, inkl. Konversationsgedächtnis (per Discord-Reply), Tagesbudget-Kontrolle und Shop-Wissen aus dem AAM Google Sheet *(im AAM Discord aktuell nicht öffentlich verfügbar)*
- **iNat-Tracker** – erkennt iNaturalist-Beobachtungslinks in einem konfigurierten Kanal innerhalb eines definierten Zeitfensters und trägt sie automatisch (Discord-ID, Anzeigename, Link, Datum) in ein separates Google Sheet ein
- **Erfolge** – sammelbare Achievements (sichtbare + versteckte), abrufbar per `/achievements` mit Fortschritt und DM-Ping beim Freischalten – **ohne Rollen**, rein persönlich

---

## Sicherheit: Server-Bindung (Guild-Lock)

> **Diese Bot-Instanz arbeitet ausschließlich auf einem einzigen, fest eingebauten Discord-Server:**
> **`375031723601297409`** (*Ameisen an die Macht*).

Wird der Bot-Account auf einen **fremden** Server eingeladen, funktioniert dort **kein einziger Befehl** und der Bot **verlässt den Server automatisch wieder**. Die Durchsetzung erfolgt mehrschichtig in [`main.py`](main.py):

- **`ALLOWED_GUILD_ID`** – prominente Konstante ganz oben in `main.py` (überschreibbar per Umgebungsvariable `ALLOWED_GUILD_ID`).
- **`on_guild_join`** – bei Einladung auf einen fremden Server verlässt der Bot ihn sofort (mit Log-Warnung).
- **`on_ready`** – beim Start wird jeder bereits beigetretene fremde Server verlassen.
- **Globaler Befehls-Check** (`_guild_lock_check`) – blockt als zusätzliche Sicherheitsebene jeden Befehl außerhalb des gebundenen Servers.

**Warum?** Der Quellcode steht unter der **AGPLv3** – jede und jeder darf den Bot forken und selbst betreiben. Das bedeutet aber ausdrücklich: **mit eigenem Bot-Token und eigener Instanz**. Dieser konkrete Bot-*Account* soll nur für den einen offiziellen Server laufen und nicht versehentlich (oder absichtlich) auf fremden Servern Daten sammeln oder antworten.

**Eigene Instanz betreiben?** Einfach in `main.py` die `ALLOWED_GUILD_ID` auf die eigene Server-ID setzen (oder `ALLOWED_GUILD_ID` als Umgebungsvariable definieren) und einen **eigenen** Bot-Token verwenden.

---

## Inhaltsverzeichnis

1. [Sicherheit: Server-Bindung (Guild-Lock)](#sicherheit-server-bindung-guild-lock)
2. [Voraussetzungen](#voraussetzungen)
3. [Installation](#installation)
4. [Konfiguration](#konfiguration)
5. [Erster Start & Server-Einrichtung](#erster-start--server-einrichtung)
6. [Betrieb als systemd-Dienst](#betrieb-als-systemd-dienst)
7. [Review-Bot](#review-bot)
8. [AntCheck-Bot](#antcheck-bot)
9. [Preis-Tracking](#preis-tracking)
10. [Wochen-Digest](#wochen-digest)
11. [Rabattcode-Tracker](#rabattcode-tracker)
12. [AI-Chat-Bot](#ai-chat-bot)
13. [iNat-Tracker](#inat-tracker)
14. [Erfolge](#erfolge)
15. [Slash Commands](#slash-commands)
16. [Hintergrundaufgaben](#hintergrundaufgaben)
17. [Grabber](#grabber)
18. [Datenbank](#datenbank)
19. [Projektstruktur](#projektstruktur)
20. [Lokalisierung](#lokalisierung)
21. [Credits & Danksagung](#credits--danksagung)

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
| `requests>=2.31.0` | HTTP (Grabber + Währungs-APIs) |
| `rapidfuzz>=3.0.0` | Fuzzy Shop-Matching |
| `psutil>=5.9.0` | System-Stats (`/system`) |
| `python-dotenv>=1.0.0` | `.env`-Dateien |
| `Babel>=2.8.0` | Lokalisierte Ländernamen (CLDR) |
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
# ALLOWED_GUILD_ID=375031723601297409     # Guild-Lock: Bot läuft NUR auf diesem Server (Default: AAM); eigene Instanz -> eigene ID + eigener Token

# ── Anthropic (KI-Parser) ─────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...
# REVIEW_PARSER_MODEL=claude-haiku-4-5-20251001   # Modell für die Review-Extraktion
# ACCUMULATION_DELAY=8                     # Sek. warten, um geteilte Review-Nachrichten zusammenzuführen

# ── Google Sheets ─────────────────────────────────────────────
GOOGLE_SPREADSHEET_ID=deine_spreadsheet_id_hier

# ── AntCheck API ──────────────────────────────────────────────
ANTCHECK_API_KEY=dein_api_key_hier
ANTCHECK_API_URL=https://antcheck.info
ANTCHECK_VERIFY_SSL=false                 # false bei self-signed Zertifikat
# ANTCHECK_TIMEOUT=30                      # Timeout pro API-Anfrage in Sek. (Grabber)
# ANTCHECK_RETRIES=3                       # Anzahl Wiederholungen bei API-Fehlern
# ANTCHECK_RETRY_DELAY=5                   # Wartezeit zwischen Wiederholungen in Sek.

# ── KI-Chat-Bot ───────────────────────────────────────────────
AI_CHAT_CHANNEL_IDS=123456789012345678   # Kanal-ID, in dem der Bot antwortet
AI_CHAT_DAILY_BUDGET_USD=0.50            # Gesamtes Tagesbudget (alle User)
AI_CHAT_USER_DAILY_BUDGET_USD=0.10       # Pro-User-Tagesbudget
# AI_CHAT_MODEL=claude-haiku-4-5-20251001          # Chat-Modell
# AI_CHAT_CLASSIFY_MODEL=claude-haiku-4-5-20251001 # Modell für die Shop-Relevanz-Klassifikation
AI_CHAT_PUBLIC=false                     # true = KI-Befehle in /help zeigen + KI öffentlich zugänglich
# AI_CHAT_MAX_INPUT_CHARS=1500             # Max. Eingabezeichen pro Anfrage
# AI_CHAT_MAX_OUTPUT_TOKENS=800            # Max. Output-Tokens pro Antwort
# AI_CHAT_MAX_HISTORY_TURNS=10             # Gespeicherte Gesprächsrunden pro Konversation
# AI_CHAT_CONVERSATION_TTL_HOURS=24        # Aufbewahrung einer Konversation in Stunden

# ── Rabattcode-Tracker ────────────────────────────────────────
DISCOUNT_CHANNEL_ID=123456789012345678   # Kanal mit Rabattcodes (leer/0 = inaktiv)
# DISCOUNT_PARSER_MODEL=claude-haiku-4-5-20251001   # Modell für die Code-Extraktion
# DISCOUNT_VISION_ENABLED=true             # Bilder (Screenshots/Flyer) auf Codes prüfen
# DISCOUNT_VISION_MAX_IMAGES=4             # Max. Bilder pro Nachricht an die Vision-API
# DISCOUNT_VISION_MAX_BYTES=4000000        # Max. Bildgröße in Bytes (4 MB)

# ── Command-Log (Moderation, optional) ────────────────────────
# MOD_LOG_CHANNEL_ID=123456789012345678    # Mod-only-Kanal fürs Befehls-Log (leer/0 = kein Kanal-Post)
# COMMAND_LOG_RETENTION_DAYS=365           # DB-Aufbewahrung der Log-Zeilen (Tage)

# ── Pfade (optional) ──────────────────────────────────────────
DATA_DIRECTORY=/opt/discord-bot          # Wo shops_data.json abgelegt wird
# SHOPS_DATA_FILE=/pfad/zu/shops_data.json  # Voller Pfad-Override (statt DATA_DIRECTORY)

# ── Python ────────────────────────────────────────────────────
PYTHONUNBUFFERED=1
```

Alle auskommentierten Variablen haben sinnvolle Defaults und müssen nur gesetzt werden, wenn sie angepasst werden sollen. Der obige Block enthält alle unterstützten Variablen; dieselbe Liste steht auch in `.env.example`.

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

Damit wird der Bot-Kanal festgelegt und die Serversprache gesetzt. Ohne `/startup` funktionieren alle Befehle, aber in jedem Kanal. Ist ein Bot-Kanal gesetzt und ein Befehl wird woanders genutzt, wird er für normale Mitglieder mit einem (nur für sie sichtbaren) Hinweis abgelehnt; Mitglieder mit **Nachrichten-verwalten/Admin**-Recht bekommen stattdessen eine ephemere **Ja/Nein-Rückfrage** und können den Befehl bei Bestätigung trotzdem dort ausführen (Ausgabe dann normal/öffentlich).

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Betrieb als systemd-Dienst

Für den Dauerbetrieb liegen drei systemd-Unit-Vorlagen im Repo:

| Datei | Zweck |
|-------|-------|
| `aam-bot.service` | Startet den Bot (`main.py`) als Dienst, lädt `.env` und startet bei Absturz automatisch neu. |
| `aam-bot-update.service` | Oneshot-Auto-Deploy – führt `update.py` aus (siehe unten). |
| `aam-bot-update.timer` | Löst `aam-bot-update.service` alle 5 Minuten aus (`OnUnitActiveSec=5min`). |

**`update.py`** prüft `origin/main` auf neue Commits, zieht sie per Fast-Forward, installiert bei geänderter `requirements.txt` die Abhängigkeiten im venv nach und startet den `aam-bot`-Dienst neu. Gibt es nichts Neues, passiert nichts; **uncommittete lokale Änderungen brechen den Deploy ab** (werden nie überschrieben). Überschreibbar per Umgebungsvariable: `REPO_DIR`, `BRANCH`, `SERVICE`, `VENV` (Defaults: `/opt/discord-bot`, `main`, `aam-bot`, `<REPO_DIR>/.venv`).

Installation (einmalig, als root):

```bash
sudo cp aam-bot.service aam-bot-update.service aam-bot-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aam-bot.service        # Bot starten + Autostart beim Boot
sudo systemctl enable --now aam-bot-update.timer   # Auto-Deploy alle 5 Minuten
```

Damit der Update-Dienst den Bot neu starten darf, braucht der Service-User (`aam`) eine passwortlose sudo-Regel (eng auf genau diesen Befehl als `root` begrenzt):

```
# /etc/sudoers.d/aam-bot-restart
aam ALL=(root) NOPASSWD: /usr/bin/systemctl restart aam-bot
```

Nach jeder Änderung an einer `.service`- oder `.timer`-Datei einmal `sudo systemctl daemon-reload` ausführen. **Wichtig:** Der Auto-Deploy (`git pull`) aktualisiert die Unit-Vorlagen nur im Projektordner – die aktiv geladenen Kopien in `/etc/systemd/system/` müssen bei Änderungen erneut per `sudo cp … /etc/systemd/system/` + `daemon-reload` übernommen werden. Logs: `journalctl -u aam-bot -f` (Bot) bzw. `journalctl -u aam-bot-update -n 20` (letzter Deploy).

> Hinweis: Der Auto-Deploy zieht aus `main`; die Entwicklung läuft auf `beta` und wird erst durch Merge nach `main` produktiv ausgerollt.

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

**Löschen:** Löscht ein User (oder ein Mod) seine Bewertungs-Nachricht im Review-Kanal, leert der Bot automatisch die zugehörige Sheet-Zeile (Spalten A–I) und entfernt den DB-Tracking-Eintrag. Die Zeile wird nur **geleert**, nicht physisch entfernt – so bleiben alle übrigen Zeilennummern stabil; leere Zeilen werden beim Reconcile ohnehin übersprungen. Funktioniert auch bei Massen-/Bulk-Löschungen. Bei mehrteiligen (zusammengeführten) Bewertungen ist die **erste** Nachricht der Anker: Löschen der Anker-Nachricht leert die Zeile, das Löschen einer reinen Fortsetzungsnachricht nicht.

### Reaktionssystem

| Reaktion | Bedeutung |
|----------|-----------|
| 🟢 | Erfolgreich verarbeitet |
| 🟡 | Shop nicht erkannt oder Parse-Fehler |
| 🔴 | Retry fehlgeschlagen |

**Retry-Mechanismus:** Wenn eine Bewertung 🟡 bekommt, wird der unbekannte Shop-Identifier in `shop_mapping.csv` eingetragen (leer). Der Admin ordnet die korrekte URL per **`/shopmap set identifier:<Shop-Text> url:<domain>`** zu – das aktualisiert die CSV **und** den Live-Cache. Danach die 🟡-Reaktion anklicken (oder `/reprocess`), und die Bewertung wird verarbeitet. *(Alternativ die CSV direkt bearbeiten – das erfordert aber einen Bot-Neustart, da sie sonst nur beim Start bzw. über `/shopmap` neu eingelesen wird.)*

> **Hinweis:** `/shopmap` (Review-Auflösung, Shop-Text → URL, CSV) ist etwas anderes als `/shopmapping` (externer Name → interne AntCheck-Shop-ID, DB). Für ein 🟡 ist **`/shopmap`** das richtige.

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

Produkte werden nach AAM-Rating sortiert (beste zuerst, ohne Rating ganz unten). Preise werden in der Originalwährung des Shops angezeigt, inklusive automatischer EUR-Umrechnung via [Frankfurter API](https://www.frankfurter.app) (EZB, kostenlos, kein API-Key, 6-Stunden-Cache); für Währungen außerhalb der EZB (z. B. TWD) dient die offene [fawazahmed0/exchange-api](https://github.com/fawazahmed0/exchange-api) als Fallback. Hat ein Produkt einzelne **Varianten** (aus `shops_data.json`), werden diese zusätzlich mit Einzelpreis pro Variante aufgelistet (max. 8 pro Produkt, Rest als „… und X weitere") – die Produkt-Preisspanne bleibt als Übersicht erhalten:

```
34.49CAD (ca. 23.50€)
10.00-20.00CAD (ca. 6.80-13.60€)
59.99EUR
```

Die Benachrichtigung kommt als Discord-Embed; lange Ausgaben werden sicher auf mehrere Embeds aufgeteilt (jedes ≤ 4096 Zeichen, auch einzelne sehr lange Einträge werden zerlegt – kein Discord-Fehler mehr). Falls DMs blockiert sind, schreibt der Bot einen Ping in den Server-Kanal. Unter der DM erscheint ein Button **„📉 Preise beobachten"** – ein Klick öffnet direkt die `/track_price`-Auswahl (Shop → Produkte) für die gemeldete Art, ohne den Befehl tippen zu müssen.

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

3. **Variante wählen (optional)** – wird **genau ein** Produkt gewählt, das Varianten hat, erscheint ein zusätzlicher Auswahlschritt: „🔭 Ganzes Produkt (alle Varianten)" oder eine/mehrere konkrete Varianten. Bei Auswahl einer Variante wird deren Einzelpreis beobachtet (`variant_id`), sonst das ganze Produkt (Produkt-min/max, wie bisher). Bei Mehrfachauswahl von Produkten wird immer das ganze Produkt beobachtet.
4. **Bestätigen** – aktueller Preis als Baseline, öffentliche Ankündigung im Kanal

**Hintergrund-Check alle ~65 Minuten:** Preis gesunken → 📉-DM, gestiegen → 📈-DM. Bei Produkt-Tracking (ganzes Produkt) nennt die DM zusätzlich den **Grund** der Änderung, sofern erkennbar – z. B. „günstigste Variante ausverkauft“ (nur Spannen-Verschiebung, keine echte Erhöhung), „neue teurere/günstigere Variante hinzugekommen“ oder eine echte Preisänderung derselben Variante. Die Preisspanne wird nur aus **lagernden** Varianten gebildet (0 €/ausverkauft fließen nicht ein).

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

`/price_history` rendert für ein beobachtetes Produkt **oder eine beobachtete Variante** den Preisverlauf lokal als Diagramm (matplotlib, Step-Chart aus `price_history.db` bzw. `variant_price_history`) und markiert das historische Tief („Bestpreis seit Beobachtungsstart").

Mit `/set_target` legst du pro beobachtetem Produkt einen **Zielpreis** fest – Modus `zusätzlich` (weiter Änderungs-DMs plus 🎯-DM beim Erreichen), `ersetzt` (nur noch die 🎯-DM) oder `aus` (entfernen). Der Zielpreis gilt in der Shop-Währung und wird im laufenden Preis-Check (~65/67 Min.) ausgewertet.

### DM-Fallback

Falls DMs des Users blockiert sind, wird der Server-Kanal als Fallback genutzt.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Wochen-Digest

Optionaler wöchentlicher Überblick **per DM** – nur für User, die sich per **Opt-in** angemeldet haben (`/digest action:aktivieren`). Versand **montags 09:00 (Berliner Zeit)**; der Task feuert täglich, handelt aber nur montags.

**Inhalt:**
- **Größte Preisstürze der letzten 7 Tage** – aus `price_history.db` (Top 10, mit altem/neuem Preis und prozentualem Rückgang)
- **Neue Arten im Angebot** – Diff gegen die Baseline-Tabelle `known_species`, **nach Gattung gruppiert** und mit **klickbarem Produktlink** je Art (antcheck.info; Klartext, falls ausnahmsweise keine URL vorliegt). Es werden **alle** neuen Arten angezeigt – bei vielen Einträgen verteilt sich der Digest automatisch auf mehrere DMs (nichts wird mehr mit „… und N weitere" gekürzt)
- **Neue Shops** – Diff gegen die Baseline-Tabelle `known_shops` (ebenfalls vollständig, ohne Kürzung)

Die Baseline-Tabellen (`known_species`, `known_shops`) werden beim **ersten Lauf** befüllt – in diesem Lauf gibt es daher noch keine „neu"-Meldung; echte Neuzugänge werden erst ab dem zweiten Lauf erkannt. Gibt es in einer Woche nichts Neues, bekommen Abonnenten trotzdem eine kurze „nichts Neues"-DM.

An-/Abmelden und Status prüfen über `/digest` (`aktivieren` / `deaktivieren` / `status`).

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Rabattcode-Tracker

Liest in einem konfigurierten Kanal (`DISCOUNT_CHANNEL_ID`) Nachrichten, extrahiert per Claude Haiku Rabattcodes (Shop, Code, Rabatthöhe, Gültigkeitszeitraum, ggf. Mindestbestellwert) und speichert sie in der Datenbank. Codes werden dabei sowohl aus dem Text als auch – sofern `DISCOUNT_VISION_ENABLED` (Standard an) – aus geposteten **Bildern** (Screenshots, Flyer, Shop-Werbung) per Vision erkannt. Ist kein Kanal gesetzt, bleibt das Feature inaktiv.

### Funktionsweise

- **Einmal pro Nachricht:** Jede verarbeitete `message_id` wird in `discount_scanned` festgehalten, damit dieselbe Nachricht nie zweimal an Haiku geschickt wird.
- **Backfill beim Start:** Beim ersten `on_ready` wird der gesamte Kanal (älteste zuerst) durchgegangen; bereits gescannte Nachrichten werden übersprungen. Mehrfaches `on_ready` (Reconnects) löst keinen erneuten Scan aus.
- **Live:** Neue Posts im Kanal werden sofort verarbeitet (Reaktion 🏷️ bei gefundenem Code).
- **Kein Keyword-Vorfilter:** Jede Nachricht mit Text und/oder Bild-Anhang geht an Haiku, das im Zweifel selbst entscheidet (kein Code → leeres Ergebnis). Nur Nachrichten ganz ohne Text und ohne verwertbares Bild werden ohne API-Aufruf übersprungen und nur als gescannt markiert.
- **Bild-Analyse (`DISCOUNT_VISION_ENABLED`, Standard an):** Datei-Anhänge (jpg, jpeg, png, gif, webp) werden per Vision mitgeschickt – so werden auch Codes erkannt, die nur im Bild stehen. Max. `DISCOUNT_VISION_MAX_IMAGES` Bilder pro Nachricht (Standard 4), jeweils ≤ `DISCOUNT_VISION_MAX_BYTES` (Standard 4 MB); größere/andere Anhänge werden übersprungen. Text und Bilder einer Nachricht gehen gemeinsam in **einen** Haiku-Aufruf. Nur Datei-Anhänge, keine verlinkten Bilder/Embeds.
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
| Bilder (Vision) | jpg, jpeg, png, gif, webp | 4 MB |
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

**Modell:** Standard `claude-haiku-4-5-20251001`, konfigurierbar per `AI_CHAT_MODEL` – aktuell `claude-sonnet-4-6` (unterstützt Text und Vision). Die Stufe-2-Klassifikation (Shop-Relevanz) läuft separat über `AI_CHAT_CLASSIFY_MODEL` (Standard Haiku), der Review-Parser über `REVIEW_PARSER_MODEL`.

**Kosten:** Die Preistabelle kennt u. a. `claude-sonnet-5` zum Standardtarif ($3/Mio. Input, $15/Mio. Output, ohne Einführungsrabatt). Adaptives Denken muss nicht separat berechnet werden – Denk-Tokens werden als Output-Tokens abgerechnet und sind über `response.usage.output_tokens` bereits in den Kosten enthalten.

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

Nach jeweils `INAT_SNAPSHOT_EVERY` (Standard: 15) neu eingetragenen Beobachtungen liest der Bot den Tab `INAT_UEBERSICHT` (Standard: `Übersicht`, Spalten **A = Rang, B = Name, C = Anzahl Arten**, Kopfzeile in Zeile 1) und rendert daraus **lokal mit matplotlib** eine farbige Treppchen-Grafik (Top 3 in Gold/Silber/Bronze, Platz 4+ als Tabelle), die er im Channel postet. Es wird **kein** Google-PNG-Export mehr verwendet – das Bild entsteht komplett im Bot, daher keine flakigen Export-Fehler. Bei **Gleichstand** (gleiche Artenzahl) teilen sich mehrere Personen denselben Rang und dieselbe Treppchen-Stufe (Competition-Ranking: 1, 1, 3, …). Nach dem Erreichen der Schwelle wartet der Bot zunächst `INAT_SNAPSHOT_DEBOUNCE` Sekunden (Standard: 300 = 5 Min) auf weitere Links – **jeder** weitere Link setzt diesen Timer zurück, sodass kurz aufeinanderfolgende Einträge gebündelt werden und kein Link mitten im Prozess verloren geht. **Sobald der Post tatsächlich startet, wird er nicht mehr abgebrochen** – Links, die genau während des Postens eingehen, lösen stattdessen direkt danach einen weiteren (Follow-up-)Snapshot aus.

Ablauf:
1. **Debounce:** Ab Erreichen der Schwelle `INAT_SNAPSHOT_DEBOUNCE` Sekunden (Standard: 300) auf weitere Links warten; jeder neue Link setzt den Timer zurück. Erst nach dieser Ruhezeit geht es weiter. (Ein manueller `Rangliste`-Trigger überspringt diesen Schritt.)
2. Warten bis Spalte Z2 im Übersicht-Tab leer ist (evtl. läuft noch ein anderer Job)
3. Apps Script via Web App triggern (falls `INAT_WEBAPP_URL` konfiguriert)
4. 5 Sekunden warten damit das Script Z2 auf `block` setzen kann
5. Warten bis Z2 **stabil leer** ist (mehrfach hintereinander leer, nicht nur einmal) – max. `INAT_Z2_TIMEOUT` Sekunden (Standard: 600). Damit wird **nie** während einer laufenden Validierung gerendert.
6. Daten `A1:C` lesen, lokal als Treppchen-PNG (matplotlib) rendern und im Channel posten. Die Bild-Caption enthält den **Datenschnitt-Zeitstempel** (`🕒 Stand: TT.MM.JJJJ HH:MM:SS`) – so ist erkennbar, dass Links, die **nach** diesem Zeitpunkt gepostet wurden, in diesem Bild noch nicht enthalten sind. Schlägt das Rendern fehl, wird das Ranking als **Text-Tabelle** (bzw. als `ranking.txt`, falls zu lang) mit demselben Zeitstempel gepostet – die Rangliste geht also nie verloren.

Das Z2-Flag (`block`) wird vom Apps Script gesetzt solange es rechnet und gelöscht wenn es fertig ist – der Bot wartet geduldig.

**Manueller Trigger:** Schreibt jemand im iNat-Channel exakt `Rangliste` (nur dieses Wort), wird der Snapshot-Prozess sofort ausgelöst – unabhängig vom Eintrags-Zähler, aber nur **innerhalb des konfigurierten Zeitfensters** (`INAT_START`–`INAT_END`). Cooldown: 3 Stunden (⏱️-Reaktion wenn zu früh). Ein laufender Debounce-Puffer wird dabei abgebrochen und sofort gepostet; läuft bereits ein Post, wird der manuelle Trigger ignoriert (kein doppelter Post).

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
INAT_SNAPSHOT_EVERY  = 15                       # Snapshot nach jeweils N Einträgen
INAT_SNAPSHOT_DEBOUNCE = 300                    # Nach Schwelle N Sek. auf weitere Links warten (Debounce)
INAT_Z2_TIMEOUT      = 600                      # Max. Wartezeit auf Z2-Freigabe (Sekunden)
```

Über `.env` optional:

```env
INAT_WEBAPP_URL=https://script.google.com/macros/s/.../exec   # Apps Script Web App URL
INAT_WEBAPP_SECRET=dein-secret                                  # Muss mit BOT_TRIGGER_SECRET im Script übereinstimmen
```

Der Service Account (`service_account.json`) muss auch für das iNat-Sheet als Bearbeiter eingetragen sein (der Bot schreibt in den Rohdaten-Tab und liest den Übersicht-Tab). Die benötigten Scopes (`spreadsheets` und `drive.readonly`) sind in `cogs/inat_tracker.py` hinterlegt.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Erfolge

Sammelbare Achievements – **rein persönlich, ohne Rollen**. Abrufbar per `/achievements`: freigeschaltete (✅ mit Datum), in Arbeit (Fortschrittsbalken) und die **Existenz** versteckter Erfolge (🔒 `???`). Pro neu freigeschaltetem Erfolg schickt der Bot eine dezente DM (sind DMs gesperrt, bleibt die Freischaltung trotzdem erhalten).

**Prüfung:** event-getrieben, kein periodischer Job. Ausgewertet wird nach jedem Slash-Command (Completion-Listener in `cogs/achievements.py`), beim Öffnen von `/achievements` sowie an gezielten Stellen (u. a. Zielpreis gesetzt/getroffen, Rabattcode gepostet, KI-Chat genutzt, Tracking/Beobachtung bestätigt). Alle Kennzahlen werden bei der Abfrage frisch aus den vorhandenen Tabellen + `user_events` berechnet; Freischaltungen werden in der Tabelle `achievements` persistiert.

### Sichtbare Erfolge

| Emoji | Titel | Bedingung |
|-------|-------|-----------|
| 🔔 | Erste Suche | Erste Verfügbarkeitsbenachrichtigung eingerichtet |
| 📋 | Sammler | 10 Benachrichtigungen eingerichtet |
| 🛒 | Endlich! | Erste Benachrichtigung als gekauft markiert |
| 🌈 | Artenvielfalt | 10 verschiedene Arten gesucht |
| 📉 | Preisfuchs | Erstes Produkt im Preis-Tracking |
| 📊 | Beobachter | 10 Produkte im Preis-Tracking |
| 🎯 | Zielsicher | Ersten Zielpreis gesetzt |
| 🔭 | Weitblick | Erste Arten-Beobachtung (alle Shops) |
| 📬 | Immer informiert | Wochen-Digest abonniert |
| 🏷️ | Code-Bringer | Ersten Rabattcode gepostet |
| 🏷️ | Code-Sammler | 5 Rabattcodes gepostet |
| 🏷️ | Code-Meister | 15 Rabattcodes gepostet |
| 🤖 | KI-Neugier | Den KI-Chat einmal genutzt |
| 📅 | Stammgast | An 7 verschiedenen Tagen Befehle genutzt |
| 🏃 | Marathon | 15 Befehle an einem einzigen Tag |
| 💪 | Power-User | Insgesamt 100 Befehle ausgeführt |
| 🧰 | Werkzeugkasten | Befehle aus allen vier Bereichen (Verfügbarkeit, Preis, Stöbern, Community) genutzt |
| 🏆 | Komplettist | 12 verschiedene der wichtigsten Befehle eingesetzt |

Die Reihe **Code-Bringer / Code-Sammler / Code-Meister** ist derselbe Erfolg in drei Stufen (1 / 5 / 15 gepostete Rabattcodes).

Zusätzlich gibt es **versteckte Erfolge**, die erst beim Freischalten in `/achievements` sichtbar werden – bis dahin erscheinen sie nur als 🔒 `???`. Titel und Bedingungen werden hier bewusst nicht verraten.

**Metamorphose-Ränge:** Je nach Anzahl freigeschalteter Erfolge steigst du auf: 🥚 Ei → 🐛 Larve → ⚪ Puppe → 🐜 Arbeiterin → 🪖 Soldatin → 👑 Königin. Der aktuelle Rang steht oben in `/achievements`; beim Aufstieg kommt eine dezente DM.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Slash Commands

> Alle Slash-Befehle sind **guild-only** – sie funktionieren nur auf einem Server, nicht in der Bot-DM. Durchgesetzt wird das auf zwei Ebenen: (1) `main.py` setzt zentral `walk_application_commands().guild_only = True`, (2) die Laufzeit-Checks `allowed_channel()` und `admin_or_manage_messages()` (in `cogs/server_settings.py`) geben in DMs zusätzlich `False` zurück – das ist die eigentlich zuverlässige Sperre. Das Senden/Empfangen von DMs durch den Bot (Benachrichtigungen, Preis-DMs, Feedback-Reaktionen) läuft über Events und ist davon unberührt.

### Für alle User (nur im Bot-Kanal)

| Befehl | Parameter | Beschreibung | Beispiel |
|--------|-----------|--------------|----------|
| `/notification` | `species` oder `genus` (Pflicht, nicht beides), `regions` (z.B. `de,at` oder `eu`), `swiss_only`, `exclude_species`, `force` | Verfügbarkeitsbenachrichtigung einrichten. `regions: eu` wird automatisch auf alle EU-Ländercodes aufgelöst. `exclude_species` schließt bestimmte Arten innerhalb einer Gattungs-Suche aus. `force: True` überspringt die Prüfung ob die Art in der DB vorkommt. Legt man dieselbe Kombination (Taxon + Regionen) erneut an, entsteht **kein Duplikat** – der bestehende Eintrag wird aktualisiert und man erhält den Hinweis „bereits aktiv – ich prüfe erneut". | `/notification species:Messor barbarus regions:de,at swiss_only:true` |
| `/delete_notifications` | `ids` (komma- oder leerzeichengetrennte Benachrichtigungs-IDs) | Eigene Benachrichtigungen löschen. Die IDs sind aus `/history` ersichtlich. | `/delete_notifications ids:12 15` |
| `/history` | – | Zeigt (als Embed) die letzten 20 eigenen Benachrichtigungen mit ID, Art, Region und Status (active / completed / expired / failed). Als zweites Embed: Übersicht über aktive Preis-Tracking-Einträge (Einzelprodukte mit Shops und ältestem Eintrag, Arten-Beobachtungen mit Datum). | `/history` |
| `/testnotification` | – | Schickt eine Test-DM an sich selbst, um zu prüfen ob DMs vom Bot empfangen werden. | `/testnotification` |
| `/track_price` | `species` (Art oder Gattung, Pflicht) | Startet die interaktive Preis-Tracking-Einrichtung. Erste Option im Shop-Dropdown ist **Alle Shops beobachten** (Arten-Beobachtung: Preisänderungen + Neuerscheinungen shopübergreifend). Alternativ: spezifischer Shop mit Produkt-Auswahl (Mehrfachauswahl). Bei genau einem gewählten Produkt mit **Varianten** folgt ein optionaler Varianten-Auswahlschritt (ganzes Produkt oder konkrete Variante). Aktueller Preis als Baseline. Bereits beobachtete Produkte/Arten erzeugen kein Duplikat und werden als „bereits beobachtet" gemeldet. | `/track_price species:Camponotus` |
| `/my_price_tracking` | – | Listet alle aktiven Preis-Beobachtungen: oben Arten-Beobachtungen (🔭, alle Shops) mit Startdatum, darunter Einzelprodukte/**Varianten** mit aktuellem Preis (Variantenname wird mit angezeigt). | `/my_price_tracking` |
| `/untrack_price` | – | Zeigt Einzelprodukte/Varianten und Arten-Beobachtungen gemeinsam im Multi-Select-Dropdown und entfernt die ausgewählten (Produkt und einzelne Variante getrennt entfernbar). | `/untrack_price` |
| `/price_history` | – | Zeigt für eines deiner beobachteten Produkte/**Varianten** den Preisverlauf als Diagramm (Step-Chart; bei Varianten der Einzelpreis) mit markiertem historischem Tief („Bestpreis"). Auswahl per Dropdown. | `/price_history` |
| `/set_target` | `mode` (`zusätzlich`/`ersetzt`/`aus`), `target_price` (optional, Shop-Währung) | Setzt für ein beobachtetes Produkt **oder eine beobachtete Variante** (Auswahl per Dropdown) einen Zielpreis. `zusätzlich` = weiter Änderungs-DMs + 🎯-DM bei Erreichen; `ersetzt` = nur die 🎯-DM; `aus` = Zielpreis entfernen. | `/set_target mode:ersetzt target_price:12.50` |
| `/usersetting language` | `language` (`de` / `en` / `eo`) | Eigene Sprache setzen. Wirkt auf alle Bot-Antworten – Slash-Command-Ausgaben, DMs und KI-Antworten. | `/usersetting language language:de` |
| `/usersetting blacklist_add` | `shop` (Name oder Teile davon, Fuzzy-Match) | Shop dauerhaft von Verfügbarkeits-DMs ausschließen. Der Bot sucht den besten Treffer im Shop-Verzeichnis. | `/usersetting blacklist_add shop:Antstore` |
| `/usersetting blacklist_remove` | `shop` | Shop wieder in Benachrichtigungen einschließen. | `/usersetting blacklist_remove shop:Antstore` |
| `/usersetting blacklist_list` | – | Eigene Blacklist anzeigen (Shop-Name + ID). | `/usersetting blacklist_list` |
| `/usersetting shop_list` | `country` (optional, z.B. `de`) | Alle bekannten Shops anzeigen. **Ohne** `country`-Filter nach Ländern gruppiert (Überschrift z.B. `🇦🇹 Österreich (AT)` – Ländernamen in deiner Sprache (de/en/eo, via Babel/CLDR), Gruppen alphabetisch nach Ländername, Shop-Reihenfolge innerhalb wie im Ranking). **Mit** Filter flache Liste. Zeigt Name, URL und AAM-Rating. **Community-Warnhinweise** (aus dem `Übersicht`-Tab: Stufe 1–3, Hinweis, Shop, Datum) erscheinen oben als ⚠️-Block und als ⚠️-Marker beim betroffenen Shop. Ausgabe als Embed. | `/usersetting shop_list country:ch` |
| `/ch_delivery add` | `shop` (Name, Fuzzy-Match) | Shop manuell zur CH-Lieferliste hinzufügen (für `swiss_only`-Benachrichtigungen). Automatische CH-Shops (aus `country=ch` in der API) werden immer einbezogen. | `/ch_delivery add shop:Antstore` |
| `/ch_delivery remove` | `shop` (Name, Fuzzy-Match) | Shop aus der CH-Lieferliste entfernen. Angegeben wird der Shop-**Name** (nicht die ID). Jeder User kann eigene Einträge entfernen; Admins können alle entfernen. | `/ch_delivery remove shop:Antstore` |
| `/ch_delivery list` | – | CH-Lieferliste anzeigen: automatisch erkannte Shops (aus API) und manuell hinzugefügte. | `/ch_delivery list` |
| `/ai_status` | – | Eigenen KI-Chat Budget-Status anzeigen: aktuell verbrauchte Kosten, verbleibendes persönliches und globales Tagesbudget sowie Uhrzeit des nächsten Resets. | `/ai_status` |
| `/codes` | `show_expired` (optional) | Aktuell gültige Rabattcodes anzeigen (permanente, ohne Enddatum, noch nicht abgelaufene sowie manuell gültig markierte). Pro Shop+Code nur ein Eintrag. Mit `show_expired:true` werden auch abgelaufene (⌛) und manuell deaktivierte (🚫) Codes mit angezeigt. | `/codes show_expired:true` |
| `/digest` | `action` (`aktivieren`/`deaktivieren`/`status`) | Meldet dich für den **wöchentlichen Digest per DM** an oder ab: größte Preisstürze der Woche, neue Arten, neue Shops. Nur angemeldete User bekommen die DM (montags). | `/digest action:aktivieren` |
| `/achievements` | – | Zeigt deine Erfolge: freigeschaltete (✅ mit Datum), in Arbeit (Fortschrittsbalken) und versteckte (🔒 `???`, bis freigeschaltet). Beim Freischalten kommt eine dezente DM. Keine Rollen, nur für dich sichtbar. | `/achievements` |
| `/offers` | `shop` (Shopname, auch Teilname; Pflicht) | Listet **alle lagernden Angebote eines Shops** (Quelle: antcheck.info). Öffentliche Ausgabe, pro Produkt die Varianten-Einzelpreise (Original + EUR), Länderflagge, **AAM-Bewertung** (falls vorhanden), Shop-Link + **Produktlink** je Angebot. Bei mehreren Treffern werden die Shops zum Eingrenzen gelistet. Angebote mit **0 €/unbekanntem Preis** werden ausgeblendet; bei einem **Community-Warnhinweis** erscheint ein ⚠️ beim Shop. Ausgabe als Embed(s). | `/offers shop:Antstore` |
| `/sells` | `species` (Art/Gattung, auch Teilname; Pflicht), `country` (optional, Ländercode) | Vergleicht **lagernde Angebote** einer Art/Gattung über alle Shops (Quelle: antcheck.info). Öffentliche Ausgabe, gruppiert nach Art → Shop mit Länderflagge und **AAM-Bewertung** (falls vorhanden), **pro Variante** der Preis in Originalwährung + EUR-Umrechnung (Fallback auf Produkt-Preisspanne, falls keine Varianten vorliegen), plus **Produktlink** je Angebot. Bei mehreren Treffern Hinweis, für welche Arten es Angebote gibt. Optional per Ländercode filterbar. Angebote mit **0 €/unbekanntem Preis** werden ausgeblendet; bei einem **Community-Warnhinweis** erscheint ein ⚠️ beim Shop. Ausgabe als Embed(s). | `/sells species:aethiops` |
| `/help` | – | Befehlsübersicht (lokalisiert in der eingestellten Sprache). Antwort ist **öffentlich** sichtbar im Kanal. | `/help` |

### Nur Admin / Nachrichten verwalten

| Befehl | Parameter | Beschreibung | Beispiel |
|--------|-----------|--------------|----------|
| `/startup` | `language` (`de`/`en`/`eo`), `channel` (optional) | Bot-Kanal und Sprache für diesen Server festlegen. Muss einmalig pro Server aufgerufen werden. Ohne `channel` sind Befehle in allen Kanälen erlaubt. | `/startup language:de channel:#ameisen-bot` |
| `/status` | – | Zeigt die Anzahl der Bewertungen im Google Sheet, die Zahl der verarbeiteten Reviews und die ausstehenden (🟡) Nachrichten. | `/status` |
| `/pending` | – | Listet alle ausstehenden Nachrichten mit Message-ID, Grund und kurzem Nachrichtenausschnitt. | `/pending` |
| `/test` | `text` | KI-Parser mit einem frei eingegebenen Bewertungstext testen (ohne Sheet-Eintrag). Zeigt das von der KI extrahierte JSON. | `/test text:🛒 Shop: Antstore, Messor barbarus, 9/10` |
| `/rescan` | – | Gleicht die letzten 90 Tage Discord-History manuell mit dem Google Sheet ab. Nützlich nach manuellen Sheet-Korrekturen oder Bot-Ausfällen. | `/rescan` |
| `/reprocess` | `ids` (Leerzeichen- oder kommagetrennte Message-IDs) | Bewertungsnachricht(en) neu verarbeiten. Mehrere IDs werden zu einem einzigen Sheet-Eintrag zusammengeführt (für geteilte Nachrichten). | `/reprocess ids:1176542880 1176542995` |
| `/export` | `user_id` (optional) | Ohne Parameter: alle DB-Tabellen als JSON-Datei (Admin-Debug, max. 500 Zeilen/Tabelle). Mit `user_id`: alle gespeicherten Daten des Users als JSON per DM (DSGVO-Auskunft). | `/export user_id:123456789012345678` |
| `/stats` | – | Benachrichtigungsstatistiken: aktive, abgeschlossene, abgelaufene und gelöschte Benachrichtigungen sowie die Top-5-gesuchten Arten. | `/stats` |
| `/system` | – | Systemstatus: **laufende Bot-Version**, Uptime, Server-/Nutzerzahl, DB-Integrität, Gesamtzahl Benachrichtigungen, Alter der `shops_data.json`, Latenz, CPU- und RAM-Auslastung, Betriebssystem. | `/system` |
| `/reloadshops` | – | `shops_data.json` sofort neu einlesen und DB aktualisieren (ohne `average_rating` und `url_override` zu überschreiben). | `/reloadshops` |
| `/shopmapping add` | `external`, `shop_id` | Externen Shopnamen (z.B. aus Discord-Review) dauerhaft einer internen Shop-ID zuordnen. | `/shopmapping add external:Antstore.de shop_id:2` |
| `/shopmapping show` | – | Alle gespeicherten Shop-Name-Mappings anzeigen. | `/shopmapping show` |
| `/shopmapping remove` | `external` | Mapping löschen. | `/shopmapping remove external:Antstore.de` |
| `/shopurl set` | `shop_id`, `url` | Manuelle URL für einen Shop setzen. Überschreibt die API-URL dauerhaft und überlebt stündliche Shop-Reloads. Nützlich wenn die API eine falsche Domain liefert. | `/shopurl set shop_id:2 url:https://antstore.net` |
| `/shopurl clear` | `shop_id` | Manuelle URL-Override entfernen – API-URL wird wieder genutzt. | `/shopurl clear shop_id:2` |
| `/shopurl list` | – | Alle aktiven URL-Overrides anzeigen. | `/shopurl list` |
| `/ai_reset` | `user` (optional) | KI-Chat Budget für einen bestimmten User oder global (alle User) zurücksetzen. Ohne `user`-Angabe wird das globale Budget zurückgesetzt. | `/ai_reset user:@Mitglied` |
| `/ai_prompt` | – | Aktuell geladenen System-Prompt des KI-Chats anzeigen – in der eingestellten Sprache des ausführenden Users. | `/ai_prompt` |
| `/codes_set` | `code`, `status` (`valid` / `invalid` / `auto`), `shop` (optional) | Einen Rabattcode manuell als **immer gültig**, **ungültig** oder zurück auf **automatisch** (Datumslogik) setzen. Ohne `shop` werden alle Einträge mit diesem Code aktualisiert, sonst nur die des angegebenen Shops. | `/codes_set code:ANT10 status:valid shop:Antstore` |
| `/codes_rescan` | – | Rabattcode-Kanal nach noch nicht gescannten Nachrichten durchsuchen (z. B. nachdem der Bot offline war). Bereits gescannte Nachrichten werden übersprungen. | `/codes_rescan` |
| `/command_log` | `user_id` (Pflicht), `period` (optional: `1m`/`1h`/`1d`/`1w`) | Befehls-Nutzungsprotokoll eines Users aus der `command_log`-DB anzeigen (jüngste zuerst, max. 100, ephemeral). Ohne `period` alle vorhandenen Einträge (im Rahmen der 12-Monats-Retention), sonst nur das Zeitfenster. Sensible Parameter bleiben ausgeblendet. | `/command_log user_id:123456789012345678 period:1d` |
| `/shopmap set` | `identifier`, `url` | Ordnet einen Shop-Text aus einer Bewertung einer Shop-URL zu (schreibt `shop_mapping.csv`, aktualisiert den Live-Cache) → löst ein 🟡 auf. | `/shopmap set identifier:Home of Insects url:home-of-insects.com` |
| `/shopmap list` | – | Alle Shop-Zuordnungen anzeigen (inkl. noch offener). | `/shopmap list` |
| `/shopmap remove` | `identifier` | Eine Shop-Zuordnung entfernen. | `/shopmap remove identifier:Home of Insects` |

### Beispiele für umfangreiche Befehle

Die Befehle mit vielen Optionen hier mit mehreren typischen Aufrufen und der jeweiligen Wirkung.

**`/notification` – Verfügbarkeitsbenachrichtigung**

```text
/notification species:Messor barbarus
→ Meldet per DM, sobald Messor barbarus irgendwo lieferbar ist.

/notification species:Messor barbarus regions:de,at
→ Wie oben, aber nur Shops aus Deutschland und Österreich.

/notification genus:Camponotus regions:eu
→ ALLE Camponotus-Arten; regions:eu wird automatisch auf alle EU-Ländercodes aufgelöst.

/notification genus:Camponotus exclude_species:Camponotus ligniperda
→ Ganze Gattung beobachten, aber C. ligniperda ausnehmen (exclude_species wirkt nur bei genus).

/notification species:Lasius niger swiss_only:true
→ Nur Shops, die in die Schweiz liefern (automatische CH-Shops + manuelle CH-Liste).

/notification species:Atta sexdens force:true
→ Legt die Benachrichtigung auch an, wenn die Art aktuell in keiner Shop-Liste vorkommt.
```

**`/track_price` – Preisbeobachtung** *(interaktiv über Dropdowns)*

```text
/track_price species:Oecophylla smaragdina
→ Öffnet das Shop-Dropdown. Erste Option „Alle Shops beobachten" = shopübergreifende
  Arten-Beobachtung (Preisänderungen + Neuerscheinungen). Alternativ ein einzelner Shop
  mit Mehrfach-Produktauswahl. Der aktuelle Preis wird als Baseline gespeichert.

/track_price species:Camponotus
→ Gattung statt Einzelart: Beobachtung greift für alle Camponotus-Produkte.
```

**`/codes` & `/codes_set` – Rabattcodes**

```text
/codes
→ Zeigt nur aktuell gültige Codes (ein Eintrag pro Shop+Code).

/codes show_expired:true
→ Zusätzlich abgelaufene (⌛) und manuell deaktivierte (🚫) Codes.

/codes_set code:ANT10 status:valid
→ Markiert ANT10 in ALLEN Shops als dauerhaft gültig (ohne shop = alle Einträge).

/codes_set code:ANT10 status:invalid shop:Antstore
→ Nur den Eintrag bei Antstore ungültig setzen.

/codes_set code:ANT10 status:auto
→ Zurück auf automatische Datumslogik (Gültigkeit nach Enddatum).
```

**`/export` – Daten-Export**

```text
/export
→ Alle DB-Tabellen als JSON-Datei (Admin-Debug, max. 500 Zeilen pro Tabelle).

/export user_id:123456789012345678
→ DSGVO-Auskunft: alle zu diesem User gespeicherten Daten als JSON per DM.
```

**`/startup` – Server-Einrichtung**

```text
/startup language:de
→ Sprache Deutsch; Befehle sind in allen Kanälen erlaubt.

/startup language:en channel:#ant-bot
→ Sprache Englisch; Befehle nur noch im Kanal #ant-bot nutzbar.
```

**`/shopurl` – URL-Overrides**

```text
/shopurl set shop_id:2 url:https://antstore.net
→ Setzt eine feste URL für Shop 2; überlebt die stündlichen Shop-Reloads.

/shopurl clear shop_id:2
→ Entfernt den Override – die API-URL wird wieder verwendet.
```

**`/sells` – Angebote einer Art/Gattung vergleichen**

```text
/sells species:aethiops
→ Findet alle Arten, deren Name „aethiops" enthält. Gibt es Treffer für mehrere
  Arten, aber nur für manche Angebote, wird das genannt. Pro Art: Quelle
  (antcheck.info), Versand-Disclaimer, dann je Shop (mit Länderflagge) Produkttitel
  + Preis in Originalwährung, bei Nicht-EUR zusätzlich die EUR-Umrechnung.

/sells species:Lasius flavus
→ Konkrete Art statt Teilname.

/sells species:Camponotus
→ Ganze Gattung: alle lagernden Camponotus-Angebote über alle Shops.

/sells species:Messor barbarus country:de
→ Nur Angebote aus Shops mit Ländercode „de".
```

> Es werden ausschließlich **lebende Ameisen** angezeigt. Merchandise/Präparate
> (Sticker, Aufkleber, Poster, Präparate usw.), die manche Shops in AntCheck unter
> derselben „ants"-Kategorie führen, werden herausgefiltert – analog zur
> Benachrichtigungs-Logik, die ebenfalls nur echte Kolonien berücksichtigt. Das
> Gleiche gilt für `/offers`.

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
| Bot-Status | alle 2 Minuten | Rotierender Discord-Status mit Ameisen-Sprüchen (20 Quotes), jeweils mit der Bot-Version davor (z. B. `v0.1.0 · …`) |
| AI-Chat Konversations-Cleanup | alle 6 Stunden | Löscht abgelaufene Konversationshistorien (>24h TTL) |
| AI-Chat Shop-Daten-Refresh | alle 6 Stunden | Liest Tabs „Übersicht" + „Händler A-Z" aus Google Sheet und aktualisiert den System-Prompt-Anhang |
| Wochen-Digest | montags 09:00 (Berliner Zeit) | DM an Opt-in-Abonnenten: Preisstürze (7 Tage), neue Arten & neue Shops |

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Grabber

Eigenständiges Skript, das **nicht** Teil des Bots ist und separat läuft. Lädt Shops, Produkte und Varianten von der AntCheck API v2 in drei Schritten:

1. `GET /api/v2/ecommerce/shops?online=true&crawler_active=true` → alle aktiven Shops
2. `GET /api/v2/ecommerce/products?shop_id={id}&product_type=ants` → Produkte pro Shop
3. `GET /api/v2/ecommerce/variants?limit=-1` → **alle Varianten global**, nach `product_id` gruppiert und dem jeweiligen Produkt zugeordnet

Ergebnis wird atomar als `shops_data.json` geschrieben (`.json.tmp` → rename). Jedes Produkt trägt zusätzlich ein Feld `variants` (Liste mit `title`, `description`, `price`, `currency_iso`, `url`, `in_stock`, `is_active`) – dadurch stehen die Einzelpreise pro Variante **allen** Bot-Funktionen zur Verfügung (aktuell genutzt von `/sells`; `min_price`/`max_price` pro Produkt bleiben als Zusammenfassung erhalten). Fällt der Varianten-Endpoint aus, bleibt `variants` leer und alle Funktionen arbeiten wie bisher auf Produkt-Ebene weiter.

Außerdem schreibt der Grabber aktuelle Preisdaten in `price_history.db` – Tabelle `product_price_history` (Produkt-min/max) und `variant_price_history` (Einzelpreis pro Variante). Diese Datei wird vom Bot für das Preis-Tracking gelesen (read-only).

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
| `server_info` | Server-Metadaten: Name, Mitgliederzahl, Erstelldatum, Icon-/Splash-/Banner-URL, Beschreibung (beim Join/Update aktualisiert) |
| `user_shop_blacklist` | Blacklisted Shops pro User |
| `shop_name_mappings` | Externer Shopname → interne Shop-ID (für Review-Bot) |
| `ch_delivery_shops` | Shops die nach CH liefern (manuell hinzugefügt) |
| `server_user_mappings` | User → Server-Zuordnung (für DM-Fallback) |
| `user_seen_products` | Bereits gemeldete Produkt-IDs (Deduplizierung) |
| `user_price_tracking` | Preis-Tracking: User → beobachtete Produkte/**Varianten** mit Baseline-Preis und letzter Benachrichtigung. `variant_id=0` = ganzes Produkt (Default, abwärtskompatibel), `variant_id>0` = konkrete Variante; PK `(user_id, product_id, variant_id)` |
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
| `digest_subscribers` | Opt-in-Abonnenten des Wochen-Digests (nur User-ID) |
| `known_species` | Baseline bekannter Arten (Diff für „neue Arten" im Digest) |
| `known_shops` | Baseline bekannter Shops (Diff für „neue Shops" im Digest) |
| `achievements` | Freigeschaltete Erfolge pro User (user_id, achievement_id, Datum) |
| `user_events` | Leichtes Event-Log (Befehlsnutzung, Zielpreis-Treffer) für Aktions-/Versteckt-Erfolge |
| `command_log` | Befehls-Nutzungsprotokoll (User, Befehl, Parameter, Kanal, Zeit, Erfolg/Fehler) für Moderation; sensible Parameter ausgeblendet, DB-Retention 12 Monate |

### `price_history.db` (Grabber-Datenbank, read-only für den Bot)

Wird vom Grabber geschrieben und vom Bot nur gelesen. Enthält `product_price_history` (Produkt-Preisverlauf), `variant_price_history` (Varianten-Preisverlauf), `variant_snapshot` (letzter lagernder Varianten-Stand je Produkt – Basis für die Grund-Erkennung) und `product_price_reason` (erkannter Grund der letzten Spannen-Änderung je Produkt).

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Projektstruktur

```
.
├── main.py                  # Einstiegspunkt – lädt alle Cogs (inkl. Guild-Lock)
├── config.py                # Zentrale Konfiguration + Umgebungsvariablen
├── grabber.py               # AntCheck API → shops_data.json + price_history.db
├── update.py                # Auto-Deploy (git pull + venv-pip + Dienst-Neustart)
├── aam-bot.service          # systemd: Bot-Dienst (main.py)
├── aam-bot-update.service   # systemd: Oneshot-Auto-Deploy (update.py)
├── aam-bot-update.timer     # systemd: löst den Auto-Deploy alle 5 Min aus
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
│   ├── price_history.py     # /price_history: Preisverlauf-Chart (matplotlib) + Bestpreis-Marker
│   ├── price_targets.py     # /set_target: Zielpreis-Alerts (pro Tracking wählbar)
│   ├── stats.py             # /stats /system /help
│   ├── shop_admin.py        # /reloadshops /shopmapping /shopurl /ch_delivery
│   ├── shop_mapping.py      # /shopmap: Review-CSV Shop-Text → URL (löst 🟡)
│   ├── tasks.py             # Alle Hintergrundaufgaben
│   ├── ai_chat.py           # KI-Chat-Bot: on_message, /ai_status, /ai_reset, /ai_prompt
│   ├── inat_tracker.py      # iNat-Tracker: iNaturalist-Links → Google Sheets
│   ├── discount_codes.py    # Rabattcode-Tracker: Haiku-Parsing + /codes /codes_rescan
│   ├── digest.py            # /digest + wöchentlicher DM-Digest (Preisstürze, neue Arten/Shops)
│   ├── achievements.py      # /achievements + Erfolge-Freischaltung (Listener, DM-Ping)
│   ├── command_log.py       # Befehls-Nutzungsprotokoll (Mod-Kanal + DB)
│   ├── sells.py             # /sells: Preisvergleich einer Art/Gattung über alle Shops
│   └── offers.py            # /offers: alle lagernden Angebote eines Shops
│
├── utils/
│   ├── db.py                # SQLite-Helfer (execute_db, init_db, Schema)
│   ├── availability.py      # Verfügbarkeitsprüfung gegen shops_data.json
│   ├── currency.py          # Währungsumrechnung: Frankfurter (EZB) + fawazahmed0-Fallback (6h)
│   ├── sheet.py             # Google Sheets Cache (SheetCache) + Rating-Sync
│   ├── shop.py              # Shop-Auflösung + CSV-Mapping (Review-Bot)
│   ├── ai_parser.py         # Claude Haiku Parser (Review-Bot)
│   ├── discount_parser.py   # Claude Haiku Parser (Rabattcodes)
│   ├── ai_chat.py           # KI-Chat-Backend: Budget, History, API-Call
│   ├── sheets_shop_data.py  # Shop-Daten aus Google Sheets für KI-System-Prompt
│   ├── tracking.py          # Review-Tracking (Discord-ID → Sheet-Zeile)
│   ├── achievements.py      # Erfolge-Registry + Auswertung (evaluate, gather_stats)
│   ├── countries.py         # Ländercode → Flaggen-Emoji + lokalisierter Name (Babel/CLDR)
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

Zusätzlich sind im Discord-Befehlsmenü für **de/en** lokalisiert: die **Befehls- und Gruppenbeschreibungen** selbst (Basistext Englisch als Fallback für andere Client-Sprachen, `de` als deutsche Anzeige), die **Parameterbeschreibungen** sowie die wichtigsten **Auswahl-Optionen** (Choices, z. B. bei `/set_target`, `/digest`, `/codes_set`). Diese Texte richten sich nach der **Discord-App-Sprache** des Users – nicht nach `/usersetting language`, da Discord sie selbst rendert. Esperanto ist als Discord-Client-Sprache nicht verfügbar; die eigentlichen Bot-Ausgaben bleiben aber vollständig auch auf eo.

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

**Neue Sprache hinzufügen** (drei Schritte):

1. **Texte:** eine weitere `locales/<code>.json` mit denselben Keys anlegen – sie wird beim Start automatisch eingelesen.
2. **Auswählbar machen:** die `choices`-Listen von `/usersetting language` (in `cogs/user_settings.py`) und `/startup` (in `cogs/server_settings.py`) um den neuen Sprachcode ergänzen – aktuell stehen dort `de`, `en` und `eo`.
3. **KI-Chat:** einen System-Prompt in der neuen Sprache als `ai_chat_system_prompt_<code>.txt` anlegen **und** den Sprachcode in `config.py` in die Lade-Schleife von `AI_CHAT_SYSTEM_PROMPTS` (aktuell `for _lang in ("de", "en", "eo")`) aufnehmen. Fehlt einer der beiden Schritte, wird der Prompt nicht geladen und die KI antwortet in dieser Sprache über den englischen Fallback-Prompt (`ai_chat_system_prompt_en.txt`). Der Platzhalter `{model}` im Prompt wird automatisch durch das konfigurierte Modell ersetzt.

Die übrigen Bot-Ausgaben (Slash-Commands, DMs, Rabattcodes) funktionieren dagegen sofort über die neue `locales/<code>.json` – nur der KI-Chat braucht zusätzlich die eigene Prompt-Datei.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)

---

## Credits & Danksagung

Dieser Bot steht auf den Schultern anderer – vielen Dank an:

- **[Antony-Bot](https://github.com/deso85/Antony) von deso85** – ein großartiger Community-Bot für die Ameisenhaltung. Die Idee, Shop-Angebote **pro Variante** aufzuschlüsseln (`/sells`, `/offers`) und variantengenau zu tracken, ist von seinem `!sells`/`!offers` inspiriert. Dickes Lob und Danke dafür! 🐜👑
- **[antcheck.info](https://antcheck.info)** – Datenquelle für Shops, Produkte, Varianten und Preise (Grabber + Preis-Tracking).
- **[Frankfurter API](https://www.frankfurter.app)** – kostenlose Währungsumrechnung (EUR-Hinweise, EZB-Kurse).
- **[fawazahmed0/exchange-api](https://github.com/fawazahmed0/exchange-api)** – offene, key-lose Wechselkurse als Fallback (150+ Währungen inkl. TWD).
- **[iNaturalist](https://www.inaturalist.org)** – Taxon-Prüfung für den iNat-Tracker.

[↑ Zum Inhaltsverzeichnis](#inhaltsverzeichnis)
