# Nutzungsbedingungen & Datenschutzerklärung

**AAM Discord Bot** – bereitgestellt für die Community *Ameisen an die Macht (AAM)*

---

## Nutzungsbedingungen

### Zweck

Der Bot kombiniert zwei Funktionen für die AAM-Community:

- **Bewertungs-Bot** – erkennt Shop-Bewertungen im dafür vorgesehenen Kanal, wertet sie automatisch mit KI aus und trägt sie in eine gemeinschaftliche Bewertungsübersicht ein.
- **AntCheck-Bot** – überwacht die Verfügbarkeit von Ameisenarten bei Online-Shops und benachrichtigt Mitglieder per Direktnachricht, sobald eine gesuchte Art verfügbar ist.
- **AI-Chat-Bot** – beantwortet Fragen im dafür vorgesehenen Kanal mit KI (Claude Sonnet) auf @-Erwähnung. Alle Nachrichten in diesem Kanal werden an die Anthropic API weitergeleitet. Jede Antwort enthält automatisch einen Disclaimer mit Hinweis auf die Unverbindlichkeit der KI-Aussagen sowie die tatsächlichen Anfragekosten. Die KI antwortet in der eingestellten Sprache des Users (Deutsch, Englisch oder Esperanto). *(aktuell nicht öffentlich verfügbar im AAM Discord)*
- **iNat-Tracker** – erkennt iNaturalist-Beobachtungslinks im dafür vorgesehenen Kanal innerhalb eines definierten Zeitfensters und trägt sie automatisch in ein Google Sheet ein. Dabei werden Discord User-ID, Servername und der Link erfasst.

### Nutzung

- Der Bot steht allen Mitgliedern des AAM-Discord-Servers kostenlos zur Verfügung.
- Es dürfen ausschließlich echte, selbst erlebte Einkaufserfahrungen bewertet werden.
- Verfügbarkeitsbenachrichtigungen dienen dem persönlichen Gebrauch und dürfen nicht automatisiert abgefragt werden.
- Missbrauch (gefälschte Bewertungen, Spam, Umgehung von Einschränkungen) kann zum Ausschluss vom Server führen.
- Der AI-Chat-Bot ist im AAM Discord aktuell nicht öffentlich zugänglich. Sobald er aktiviert wird, gilt: Im KI-Chat-Kanal werden **alle** Nachrichten an die Anthropic API übermittelt. Bitte keine sensiblen personenbezogenen Daten in diesem Kanal teilen.

### Haftung

Der Bot wird ohne Gewähr betrieben. Für die Richtigkeit der eingetragenen Bewertungen oder der angezeigten Verfügbarkeitsdaten übernimmt der Betreiber keine Haftung. Technische Ausfälle oder Fehler bei der Datenerfassung begründen keine Ansprüche.

### Änderungen

Diese Bedingungen können jederzeit angepasst werden. Wesentliche Änderungen werden im Server angekündigt.

---

## Datenschutzerklärung

### Verantwortlicher

Jonas Beier  
📧 aam-bot@proton.me

### Welche Daten werden verarbeitet?

#### Bewertungs-Bot

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Inhalte von Bewertungsnachrichten | Automatische Auswertung per KI | Google Sheets (DE) |
| Discord-IDs von Shop-Accounts | Zuordnung Shop-Name zu Discord-Account | Lokale CSV-Datei auf dem Server |
| Discord Message-IDs | Vermeidung von Doppeleinträgen | Lokale SQLite-Datenbank auf dem Server |

Bewertungen werden **anonym** gespeichert – Benutzernamen der bewertenden Mitglieder werden nicht erfasst.

#### AntCheck-Bot

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID | Zuordnung von Benachrichtigungen und Einstellungen zur Person | Lokale SQLite-Datenbank auf dem Server |
| Benachrichtigungseinstellungen (Art, Gattung, Region, Ausschlüsse) | Durchführung der Verfügbarkeitsprüfung | Lokale SQLite-Datenbank auf dem Server |
| Gesehene Produkt-IDs pro Nutzer | Vermeidung von Doppelbenachrichtigungen | Lokale SQLite-Datenbank auf dem Server |
| Persönliche Shop-Blacklist | Ausblenden unerwünschter Shops aus Benachrichtigungen | Lokale SQLite-Datenbank auf dem Server |
| Spracheinstellung | Ausgabe in der bevorzugten Sprache (de/en/eo) | Lokale SQLite-Datenbank auf dem Server |
| Discord User-ID beim Hinzufügen zur CH-Lieferliste | Nachweis der Urheberschaft für eigene Einträge | Lokale SQLite-Datenbank auf dem Server |
| Server-Zuordnung | Fallback für Direktnachrichten bei gesperrten DMs | Lokale SQLite-Datenbank auf dem Server |

**Nicht** gespeichert werden: Nutzernamen, Profilbilder, Rollen oder sonstige Metadaten.

#### AI-Chat-Bot

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID | Budget-Tracking (Tagesbudget pro User) | Lokale SQLite-Datenbank auf dem Server |
| Gesprächsverlauf (Nachrichten und KI-Antworten) | Konversationsgedächtnis für Anschlussfragen (per Discord-Reply) | Lokale SQLite-Datenbank auf dem Server, automatisch gelöscht nach 24 Stunden |
| Bildanhänge (jpg, png, gif, webp, max. 1 MB) | Bildanalyse durch die KI | Werden einmalig an Anthropic API übermittelt, nicht lokal gespeichert |
| Textdateianhänge (txt, md, csv, log, max. 10 KB) | Analyse durch die KI | Inhalt wird einmalig an Anthropic API übermittelt, nicht lokal gespeichert |
| Shop-Bewertungsdaten aus Google Sheets (Tabs „Übersicht" + „Händler A-Z") | Als Kontextwissen im System-Prompt – ermöglicht Shop-Fragen | Werden alle 6 Stunden geladen; nur bei shop-relevanten Anfragen an die API übermittelt (3-stufige Vorqualifizierung); enthalten keine personenbezogenen Daten |

> **Hinweis:** **Alle** Nachrichten im AI-Chat-Kanal werden zur Verarbeitung an die Anthropic API (USA) übermittelt. Im AI-Chat-Kanal sollten daher keine sensiblen personenbezogenen Daten geteilt werden. Die KI-Antworten sind unverbindlich – jede Antwort enthält automatisch einen entsprechenden Disclaimer.

**Nicht** gespeichert werden: Nutzernamen oder der Nachrichtentext selbst außerhalb des Gesprächsverlaufs.

#### iNat-Tracker

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID | Zuordnung der Beobachtung zur einsendenden Person | Google Sheets (separates Sheet) |
| Anzeigename auf dem Server (display_name) | Leserliche Zuordnung im Sheet | Google Sheets (separates Sheet) |
| iNaturalist-Beobachtungslink | Kern-Inhalt der Erfassung | Google Sheets (separates Sheet) |
| Datum der Nachricht (Berliner Zeit) | Zeitliche Zuordnung | Google Sheets (separates Sheet) |

Links werden nur innerhalb des konfigurierten Zeitfensters erfasst. Die Daten werden ausschließlich im Google Sheet gespeichert – lokal auf dem Server werden keine iNat-Tracker-Daten abgelegt.

### Drittanbieter

#### Anthropic (KI-Verarbeitung)

Nachrichteninhalte werden zur automatischen Auswertung an die Anthropic API (USA) übermittelt und dort **nicht dauerhaft gespeichert**. Dies betrifft sowohl den Bewertungs-Bot (Shop-Bewertungen) als auch den AI-Chat-Bot (alle Nachrichten im AI-Kanal). Anthropic verarbeitet Daten auf Basis von Standardvertragsklauseln (SCC) gemäß Art. 46 DSGVO.  
→ Datenschutz: https://www.anthropic.com/privacy

Gesprächsverläufe werden lokal für **maximal 24 Stunden** zwischengespeichert (für Konversationsgedächtnis) und danach automatisch gelöscht.

#### Google (Tabellenspeicherung)

Ausgewertete Bewertungen werden in Google Sheets (Deutschland) gespeichert.  
→ Datenschutz: https://policies.google.com/privacy

#### AntCheck API

Zur Verfügbarkeitsprüfung werden Shop- und Produktdaten von der AntCheck API abgerufen. Es werden dabei keine personenbezogenen Daten übermittelt.  
→ https://antcheck.info

#### Discord

Der Bot operiert innerhalb der Discord-Plattform und unterliegt zusätzlich den Discord-Datenschutzbestimmungen.  
→ Datenschutz: https://discord.com/privacy

### Serverstandort

Der Bot läuft auf einem Server in **Deutschland** (Strato AG, Berlin).

### Speicherdauer

- **Bewertungsdaten** werden so lange gespeichert wie die Community besteht.
- **Benachrichtigungseinstellungen** werden auf Wunsch des Nutzers per `/delete_notifications` jederzeit gelöscht.
- **Gesehene Produkte und Blacklist** werden zusammen mit den zugehörigen Benachrichtigungen entfernt.
- **AI-Chat-Konversationsverläufe** werden automatisch nach **24 Stunden** gelöscht (oder sofort wenn du nicht auf eine Bot-Antwort antwortest).
- **AI-Chat-Budgetdaten** (User-ID + Tageskosten) werden nach dem jeweiligen Tag automatisch nicht mehr genutzt; eine manuelle Bereinigung erfolgt bei Bedarf.
- **Technische Hilfsdaten** (Message-IDs, Shop-Zuordnungen) werden bei Bedarf manuell bereinigt.

### Deine Rechte

Du hast jederzeit das Recht auf **Auskunft, Berichtigung oder Löschung** deiner Daten.

- Benachrichtigungen und zugehörige Einstellungen kannst du selbst per `/delete_notifications` löschen.
- Für alle weiteren Anfragen (Auskunft, manuelle Löschung) wende dich an:  
  📧 aam-bot@proton.me

Da Bewertungen anonym erfasst werden, ist eine Zuordnung zu einzelnen Personen dort in der Regel nicht möglich.

### Rechtsgrundlage

Die Verarbeitung erfolgt auf Grundlage des berechtigten Interesses (Art. 6 Abs. 1 lit. f DSGVO) – nämlich den Betrieb einer gemeinschaftlichen, anonymen Shop-Bewertungsplattform sowie eines personalisierten Verfügbarkeitsdienstes für die AAM-Community.

---

*Stand: Juni 2026*
