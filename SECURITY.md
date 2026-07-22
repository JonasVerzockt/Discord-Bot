# Sicherheitsrichtlinie · Security Policy

> **English (TL;DR):** Please report security vulnerabilities **privately** to
> **webmaster@jonasants.de** – *not* via public GitHub issues. This is an
> open-source (AGPL-3.0) hobby/community project; we handle reports on a
> best-effort basis and ask for coordinated disclosure. Details below (in German).

---

## Unterstützte Versionen

Sicherheitsupdates gibt es nur für den **jeweils aktuellen Stand** des Projekts
(die aktuelle `VERSION` steht in `config.py` bzw. im README-Kopf). Ältere Stände
werden **nicht** rückwirkend gepflegt – bitte immer auf die neueste Version
aktualisieren.

| Version | Unterstützt |
|---------|-------------|
| aktuelle `VERSION` (Branch `main`/`beta`) | ✅ |
| alle älteren Stände | ❌ |

## Eine Sicherheitslücke melden

**Bitte NICHT über öffentliche GitHub-Issues/Pull-Requests melden.**

- **Kontakt:** 📧 **webmaster@jonasants.de**
- **Bitte angeben:**
  - Beschreibung der Schwachstelle und **betroffene Komponente/Datei**
    (z. B. `cogs/board.py`, `utils/db.py`, Grabber, Auth …),
  - **Schritte zur Reproduktion** bzw. ein minimaler Proof-of-Concept,
  - **möglicher Impact** (was kann ein Angreifer erreichen?),
  - betroffene Version/Commit und Umgebung, falls relevant.
- **Reaktion:** Dies ist ein ehrenamtliches Community-Projekt – Bearbeitung nach
  bestem Bemühen. Ziel ist eine **erste Rückmeldung innerhalb weniger Tage**.
- **Coordinated Disclosure:** Bitte gib uns angemessene Zeit, das Problem zu
  beheben, **bevor** Details öffentlich gemacht werden. Danke für verantwortungs-
  volles Vorgehen – auf Wunsch nennen wir dich in den Release-Notes/Danksagungen.

## Geltungsbereich (Scope)

**Im Geltungsbereich** (bitte melden):

- Der Bot-Code (`main.py`, `cogs/`, `utils/`, `grabber.py`) und dessen Umgang mit
  Eingaben, Berechtigungen und Daten.
- Das **Feedback-Board** (`cogs/board.py`, Webdienst): z. B. Authentifizierung
  (Admin-Login), CSRF, XSS, Injection, unzureichende Zugriffskontrolle, Umgehung
  der Moderations-Queue, IP-/Daten-Leaks.
- Umgang mit **Secrets/Konfiguration** (versehentliches Loggen/Ausgeben von Tokens,
  Schlüsseln, Roh-IPs o. Ä.).
- Umgehung der **Server-Bindung (Guild-Lock)** oder der Rechte-/Kanal-Checks.

**Außerhalb des Geltungsbereichs** (bitte anderswo/anders behandeln):

- Schwachstellen in **Drittanbietern** (Discord, Anthropic, Google, AntCheck,
  Strato, Let's Encrypt, PyPI-Pakete) → bitte direkt beim jeweiligen Anbieter melden
  (Dependency-Lücken siehe unten).
- Die **individuelle Serverkonfiguration** eines Selbst-Hosters (nginx, Proxmox,
  Firewall, TLS-Setup) – das liegt in dessen Verantwortung.
- Reine **Lastspitzen/DoS** ohne konkreten Implementierungsfehler, Social
  Engineering, physischer Zugriff.

## Bereits umgesetzte Schutzmaßnahmen (Kurzüberblick)

- **Secrets nie im Repo:** `.env`, `service_account.json`, `token.json` u. a. sind
  in `.gitignore`; `.env.example` enthält nur Platzhalter. Die Laufzeit-Datenbanken
  (`antcheckbot.db`, `price_history.db`, `board.db`) sind ebenfalls gitignored.
- **Server-Bindung (Guild-Lock):** Der Bot arbeitet ausschließlich auf dem fest
  hinterlegten Server; fremde Server werden automatisch verlassen und Befehle dort
  blockiert (siehe `main.py`).
- **Feedback-Board** (falls aktiviert): Bindung nur an `127.0.0.1` (Reverse-Proxy +
  HTTPS davor), **Moderations-Queue** (nichts öffentlich ohne Freigabe),
  **Honeypot** und **Rate-Limits**, **CSRF-Schutz** auf Admin-Aktionen,
  **Jinja2-Autoescape** gegen XSS, **HMAC-SHA3-512-gehashte IPs** (keine Roh-IP;
  geheimes Salt via `BOARD_HASH_SALT`).
- **Moderations-Log:** sensible Parameterwerte werden im Befehls-Log ausgeblendet.

## Hinweise für Selbst-Hoster (Härtung)

Der Bot ist **AGPL-3.0** – jede/r darf ihn forken und selbst betreiben (dann mit
**eigenem** Bot-Token und **eigener** Instanz). Wer selbst hostet, sollte:

- **Eigene, starke Zufalls-Secrets** setzen und **nie committen**: `DISCORD_TOKEN`,
  `ANTHROPIC_API_KEY`, `ANTCHECK_API_KEY`, `BOARD_ADMIN_TOKEN`, `BOARD_HASH_SALT`
  (z. B. `openssl rand -hex 32` bzw. `python3 -c "import secrets;print(secrets.token_hex(32))"`).
- **Dateirechte** eng setzen: `.env` und `service_account.json` auf `600`
  (`-rw-------`).
- Das **Board nie direkt exponieren**: `BOARD_BIND=127.0.0.1`, davor Reverse-Proxy
  (nginx/Caddy) mit gültigem TLS-Zertifikat.
- **Abhängigkeiten aktuell halten** (py-cord, aiohttp, Jinja2, gspread, anthropic …)
  und bekannte CVEs zeitnah einspielen.
- **Least Privilege:** dedizierter Service-User; die passwortlose `sudo`-Regel eng
  auf genau `systemctl restart <dienst>` begrenzen.
- Regelmäßige **Backups** der Datenbanken; Zugriff auf den Server absichern.

## Abhängigkeiten

Bekannte Schwachstellen in verwendeten Bibliotheken bitte ebenfalls an obige
Adresse melden – wir aktualisieren dann die betroffene Abhängigkeit bzw. passen die
Nutzung an.

---

*Danke, dass du hilfst, den Bot und die Daten der AAM-Community sicher zu halten.*
