# AAM Discord Bot

Modularer Discord-Bot für die AAM-Community, der zwei Funktionen vereint:

- **Review-Bot** – liest Shopbewertungen aus einem Discord-Kanal, parst sie mit Claude AI (Haiku) und schreibt sie automatisch in ein Google Sheet
- **AntCheck-Bot** – überwacht die Verfügbarkeit von Ameisenarten bei Online-Shops via AntCheck API und benachrichtigt User per DM

---

## Voraussetzungen

- Python 3.11+
- Ein Discord-Bot-Token ([discord.com/developers](https://discord.com/developers/applications))
- Google Service Account JSON für Sheets-Zugriff
- Claude API Key (Anthropic) für den KI-Parser

---

## Installation

```bash
git clone <repo-url>
cd discord-bot
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Wichtig:** Der Bot nutzt `py-cord` für Slash Commands. `discord.py` und `py-cord` sind nicht kompatibel – nur eines darf installiert sein. Falls `discord.py` bereits installiert ist:
> ```bash
> pip uninstall discord.py -y
> pip install "py-cord>=2.4.0"
> ```

### Abhängigkeiten (requirements.txt)

```
py-cord>=2.4.0        # Discord (Slash Commands, ApplicationContext)
anthropic>=0.25.0     # Claude Haiku KI-Parser
gspread>=6.0.0        # Google Sheets
google-auth>=2.0.0    # Google Auth
python-dotenv>=1.0.0  # .env Dateien
requests>=2.31.0      # HTTP (Grabber)
rapidfuzz>=3.0.0      # Fuzzy Shop-Matching
psutil>=5.9.0         # System-Stats (/system Befehl)
```

---

## Konfiguration

Erstelle eine `.env`-Datei im Projektordner:

```env
# Discord
DISCORD_TOKEN=dein_token_hier
REVIEW_CHANNEL_ID=123456789012345678
BOT_OWNER_ID=123456789012345678
SERVER_IDS=123456789012345678,987654321098765432

# Anthropic (KI-Parser)
ANTHROPIC_API_KEY=sk-ant-...

# Google Sheets
GOOGLE_SPREADSHEET_ID=deine_spreadsheet_id

# Pfade (optional, Standard: Projektordner)
DATA_DIRECTORY=/pfad/zu/daten

# AntCheck API (optional, hat Standardwert)
ANTCHECK_API_URL=https://api.antcheck.info

# Python
PYTHONUNBUFFERED=1
```

Eine vollständige Vorlage liegt als `.env.example` im Repo.

Lege außerdem die Google Service Account Datei als `service_account.json` im Projektordner ab (wird in `.gitignore` ignoriert).

---

## Starten

```bash
# Bot starten
python main.py

# Shopdaten aktualisieren (manuell oder als Cron-Job)
python grabber.py
```

### Empfohlener Cron-Job für den Grabber

```cron
0 * * * * cd /pfad/zum/bot && .venv/bin/python grabber.py
```

---

## Projektstruktur

```
.
├── main.py                  # Einstiegspunkt – lädt alle Cogs
├── config.py                # Zentrale Konfiguration
├── grabber.py               # AntCheck API → shops_data.json
│
├── cogs/
│   ├── server_settings.py   # /startup – Servereinrichtung
│   ├── reviews.py           # Review-Verarbeitung (on_message etc.)
│   ├── admin.py             # /status /pending /test /rescan /export
│   ├── user_settings.py     # /usersetting – Sprache, Blacklist, Shops
│   ├── notifications.py     # /notification /history /delete_notifications
│   ├── stats.py             # /stats /system /help
│   ├── shop_admin.py        # /reloadshops /shopmapping /ch_delivery
│   └── tasks.py             # Hintergrundaufgaben (Verfügbarkeit, DB-Pflege)
│
├── utils/
│   ├── db.py                # SQLite-Helfer + Schema (init_db, execute_db)
│   ├── localization.py      # l10n-System (de/en/eo)
│   ├── availability.py      # AntCheck-Verfügbarkeitsprüfung
│   ├── tracking.py          # Review-Tracking (Discord-ID → Sheet-Zeile)
│   ├── sheet.py             # Google Sheets Zugriff (SheetCache)
│   ├── shop.py              # Shop-Auflösung + CSV-Mapping
│   ├── ai_parser.py         # Claude Haiku Parser für Bewertungen
│   └── logging_setup.py     # Rotating File Handler
│
└── locales/
    ├── de.json              # Deutsch
    ├── en.json              # English
    └── eo.json              # Esperanto
```

---

## Slash Commands

### Für alle User

| Befehl | Beschreibung |
|--------|-------------|
| `/notification` | Benachrichtigung für eine Art oder Gattung einrichten |
| `/delete_notifications` | Eigene Benachrichtigungen löschen |
| `/history` | Benachrichtigungshistorie anzeigen |
| `/testnotification` | Test-DM senden |
| `/usersetting language` | Eigene Sprache setzen (de/en/eo) |
| `/usersetting blacklist_add` | Shop von Benachrichtigungen ausschließen |
| `/usersetting blacklist_remove` | Shop wieder einschließen |
| `/usersetting blacklist_list` | Eigene Blacklist anzeigen |
| `/usersetting shop_list` | Alle verfügbaren Shops anzeigen |
| `/help` | Befehlsübersicht |

### Nur Admins / Manage Messages

| Befehl | Beschreibung |
|--------|-------------|
| `/startup` | Bot-Kanal für diesen Server festlegen |
| `/status` | Bewertungsanzahl / verarbeitet / ausstehend |
| `/pending` | Liste der ausstehenden Nachrichten (🟡) |
| `/test` | KI-Parser testen ohne Sheet-Eintrag |
| `/rescan` | Letzte 90 Tage manuell neu abgleichen |
| `/export` | Sheet-Rohdaten als JSON anzeigen |
| `/stats` | Benachrichtigungsstatistiken |
| `/system` | Systemstatus (Uptime, CPU, RAM, DB) |
| `/reloadshops` | Shop-Daten neu laden |
| `/shopmapping add/show/remove` | Shopname-Mappings verwalten |
| `/ch_delivery add/remove/list` | CH-Lieferliste verwalten |

---

## Review-Bot: Ablauf

1. User postet eine Bewertung im konfigurierten Kanal
2. Bot erkennt sie anhand von Stichwörtern (`looks_like_review`)
3. Claude Haiku parst Shop, Bewertung, Produktart, Preis usw.
4. Zeile wird ins Google Sheet geschrieben → Reaktion 🟢
5. Bei unbekanntem Shop → 🟡 + Eintrag in `shop_mapping.csv`
6. Admin füllt CSV aus → klickt 🟡 → Bot versucht erneut

---

## Datenbank

SQLite-Datei `antcheckbot.db` (nicht im Git). Wird beim Start automatisch angelegt.

Wichtige Tabellen:

| Tabelle | Inhalt |
|---------|--------|
| `notifications` | Aktive/abgeschlossene Benachrichtigungen |
| `user_settings` | Spracheinstellung pro User |
| `server_settings` | Kanal + Sprache pro Server |
| `user_shop_blacklist` | Blacklisteinträge |
| `review_tracking` | Discord-Msg-ID → Sheet-Zeilennummer |
| `review_pending` | Ausstehende Nachrichten (unaufgelöst/Fehler) |
| `user_seen_products` | Bereits gemeldete Produkte (Deduplizierung) |

---

## Lokalisierung

Texte befinden sich in `locales/{de,en,eo}.json`. Neue Schlüssel einfach in alle drei Dateien eintragen. Der Bot wählt die Sprache nach: User-Einstellung → Server-Einstellung → Englisch.

---

## Hintergrundaufgaben (`cogs/tasks.py`)

| Task | Intervall |
|------|-----------|
| Verfügbarkeitsprüfung | alle 5 Minuten |
| Shop-Daten-Reload | stündlich |
| Shop-Ratings aus Google Sheet | alle 48 Stunden |
| Abgelaufene Benachrichtigungen | täglich (nach 1 Jahr) |
| DB VACUUM + ANALYZE | wöchentlich |
| Bot-Status (Uptime/Server) | jede Minute |

---

## Lizenz

Dieses Projekt steht unter der **GNU Affero General Public License v3.0 oder später (AGPL-3.0-or-later)**.

Copyright (C) 2026 Jonas Beier

Jede Person, die eine modifizierte Version dieses Bots als Netzwerkdienst betreibt, ist verpflichtet, den Quellcode ihrer Änderungen öffentlich zugänglich zu machen.

Weitere Details: [LICENSE](LICENSE) · [gnu.org/licenses/agpl-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
