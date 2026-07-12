# Nutzungsbedingungen & Datenschutzerklärung

**AAM Discord Bot** – bereitgestellt für die Community *Ameisen an die Macht (AAM)*

---

## Nutzungsbedingungen

### Zweck

Der Bot kombiniert mehrere Funktionen für die AAM-Community:

- **Bewertungs-Bot** – erkennt Shop-Bewertungen im dafür vorgesehenen Kanal, wertet sie automatisch mit KI aus und trägt sie in eine gemeinschaftliche Bewertungsübersicht ein.
- **AntCheck-Bot** – überwacht die Verfügbarkeit von Ameisenarten bei Online-Shops und benachrichtigt Mitglieder per Direktnachricht, sobald eine gesuchte Art verfügbar ist. Preise werden in der Originalwährung des Shops angezeigt, inklusive automatischer EUR-Umrechnung.
- **Preis-Tracking** – beobachtet auf Wunsch einzelne Produkte dauerhaft und informiert per Direktnachricht, sobald sich deren Preis verändert. Die Auswahl erfolgt interaktiv über Shop- und Produkt-Menüs im Discord. Preisdaten stammen aus der lokalen `price_history.db`, die vom Grabber-Skript befüllt wird. Alternativ kann über "Alle Shops beobachten" eine gesamte Art oder Gattung shopübergreifend beobachtet werden – der Bot benachrichtigt dann bei Preisänderungen. Neue Produkte werden automatisch in die Beobachtung aufgenommen, lösen aber keine separate DM aus (dafür gibt es die Verfügbarkeitsbenachrichtigung via `/notification`).
- **AI-Chat-Bot** – beantwortet Fragen im dafür vorgesehenen Kanal mit KI (Claude Sonnet) auf @-Erwähnung. Alle Nachrichten in diesem Kanal werden an die Anthropic API weitergeleitet. Jede Antwort enthält automatisch einen Disclaimer mit Hinweis auf die Unverbindlichkeit der KI-Aussagen sowie die tatsächlichen Anfragekosten. Die KI antwortet in der eingestellten Sprache des Users (Deutsch, Englisch oder Esperanto). *(aktuell nicht öffentlich verfügbar im AAM Discord)*
- **iNat-Tracker** – erkennt iNaturalist-Beobachtungslinks im dafür vorgesehenen Kanal innerhalb eines definierten Zeitfensters. Vor dem Eintragen wird geprüft ob der Link bereits vorhanden ist und ob die Beobachtung zur Überfamilie Formicoidea (Ameisen) gehört – nur dann wird sie in ein Google Sheet eingetragen. Dabei werden Discord-Username, Anzeigename auf dem Server, der Link und das Datum erfasst. Bei nicht erreichbarer API wird automatisch alle 5 Minuten erneut versucht. Nach jeweils 15 eingetragenen Beobachtungen postet der Bot – nach einer kurzen Wartezeit von 5 Minuten, um kurz aufeinanderfolgende Einträge zu bündeln – automatisch ein aktuelles Ranking-Bild (aus dem Übersicht-Tab des Sheets) im Channel; das Bild trägt einen Zeitstempel des Datenstands, damit erkennbar ist, welche danach geposteten Links noch nicht enthalten sind. Durch das Schreiben von `Rangliste` im Channel kann das Ranking-Bild jederzeit manuell abgerufen werden (maximal einmal alle 3 Stunden).
- **Rabattcode-Tracker** – liest im dafür vorgesehenen Kanal alle Nachrichten und extrahiert per KI (Claude Haiku) Rabattcodes inkl. Shop, Rabatthöhe und Gültigkeitszeitraum. Codes werden auch in geposteten **Bildern** (Screenshots, Flyer, Shop-Werbung) erkannt. Jede Nachricht wird dabei nur einmal an die KI übermittelt; beim Start wird der gesamte Kanalverlauf einmalig verarbeitet. Über `/codes` rufen Mitglieder die aktuell gültigen Codes ab. Abgelaufene Codes werden automatisch ausgeblendet. **Alle** Textnachrichten sowie Bild-Anhänge dieses Kanals werden zur Auswertung an die Anthropic API übermittelt.

### Nutzung

- Der Bot steht allen Mitgliedern des AAM-Discord-Servers kostenlos zur Verfügung.
- Es dürfen ausschließlich echte, selbst erlebte Einkaufserfahrungen bewertet werden.
- Verfügbarkeitsbenachrichtigungen und Preis-Tracking dienen dem persönlichen Gebrauch und dürfen nicht automatisiert abgefragt werden.
- Missbrauch (gefälschte Bewertungen, Spam, Umgehung von Einschränkungen) kann zum Ausschluss vom Server führen.
- Der AI-Chat-Bot ist im AAM Discord aktuell nicht öffentlich zugänglich. Sobald er aktiviert wird, gilt: Im KI-Chat-Kanal werden **alle** Nachrichten an die Anthropic API übermittelt. Bitte keine sensiblen personenbezogenen Daten in diesem Kanal teilen.
- Im Rabattcode-Kanal werden **alle** Textnachrichten sowie Bild-Anhänge zur automatischen Code-Erkennung an die Anthropic API übermittelt. Bitte auch dort keine sensiblen personenbezogenen Daten teilen.

### Haftung

Der Bot wird ohne Gewähr betrieben. Für die Richtigkeit der eingetragenen Bewertungen, der angezeigten Verfügbarkeitsdaten oder der angezeigten Preise übernimmt der Betreiber keine Haftung. Preisangaben (inkl. EUR-Umrechnung via Frankfurter API) sind unverbindlich und können von tatsächlichen Preisen abweichen. Technische Ausfälle oder Fehler bei der Datenerfassung begründen keine Ansprüche.

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

#### Preis-Tracking

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID | Zuordnung der Tracking-Einträge zur Person | Lokale SQLite-Datenbank auf dem Server (`antcheckbot.db`) |
| Produkt-ID, Produkttitel, Produkt-URL | Identifikation des beobachteten Produkts (Einzelprodukt-Tracking) | Lokale SQLite-Datenbank auf dem Server |
| Shop-Name, Shop-ID | Zuordnung zum jeweiligen Shop | Lokale SQLite-Datenbank auf dem Server |
| Währungskürzel (ISO) | Darstellung und Umrechnung der Preise | Lokale SQLite-Datenbank auf dem Server |
| Zuletzt gemeldeter Preis (min/max) | Erkennung von Preisänderungen (Vergleichswert) | Lokale SQLite-Datenbank auf dem Server |
| Datum des Tracking-Starts | Transparenz für den Nutzer | Lokale SQLite-Datenbank auf dem Server |
| Zielpreis + Modus (optional) | Benachrichtigung bei Erreichen eines Wunschpreises | Lokale SQLite-Datenbank auf dem Server |
| Beobachtete Art/Gattung (normalisierter Name) | Arten-Beobachtung shopübergreifend | Lokale SQLite-Datenbank auf dem Server |
| Bekannte Produkt-IDs je Art-Beobachtung + letzter Preis | Erkennung von Neuerscheinungen und Preisänderungen | Lokale SQLite-Datenbank auf dem Server |

Aktuelle Preisdaten werden aus `price_history.db` gelesen – einer separaten Datenbank, die vom Grabber-Skript befüllt wird. Diese Daten enthalten keine personenbezogenen Informationen. Wechselkurse für die EUR-Umrechnung werden von der [Frankfurter API](https://www.frankfurter.app) abgerufen (keine personenbezogenen Daten übermittelt, 6-Stunden-Cache).

#### AI-Chat-Bot

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID | Budget-Tracking (Tagesbudget pro User) | Lokale SQLite-Datenbank auf dem Server |
| Gesprächsverlauf (Nachrichten und KI-Antworten) | Konversationsgedächtnis für Anschlussfragen (per Discord-Reply) | Lokale SQLite-Datenbank auf dem Server, automatisch gelöscht nach 24 Stunden |
| Bildanhänge (jpg, png, gif, webp, max. 4 MB) | Bildanalyse durch die KI | Werden einmalig an Anthropic API übermittelt, nicht lokal gespeichert |
| Textdateianhänge (txt, md, csv, log, max. 10 KB) | Analyse durch die KI | Inhalt wird einmalig an Anthropic API übermittelt, nicht lokal gespeichert |
| Shop-Bewertungsdaten aus Google Sheets (Tabs „Übersicht" + „Händler A-Z") | Als Kontextwissen im System-Prompt – ermöglicht Shop-Fragen | Werden alle 6 Stunden geladen; nur bei shop-relevanten Anfragen an die API übermittelt (3-stufige Vorqualifizierung); enthalten keine personenbezogenen Daten |

> **Hinweis:** **Alle** Nachrichten im AI-Chat-Kanal werden zur Verarbeitung an die Anthropic API (USA) übermittelt. Im AI-Chat-Kanal sollten daher keine sensiblen personenbezogenen Daten geteilt werden. Die KI-Antworten sind unverbindlich – jede Antwort enthält automatisch einen entsprechenden Disclaimer.

**Nicht** gespeichert werden: Nutzernamen oder der Nachrichtentext selbst außerhalb des Gesprächsverlaufs.

#### iNat-Tracker

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord-Username (z.B. jonasverzockt) | Zuordnung der Beobachtung zur einsendenden Person | Google Sheets (separates Sheet) |
| Anzeigename auf dem Server (display_name) | Leserliche Zuordnung im Sheet | Google Sheets (separates Sheet) |
| iNaturalist-Beobachtungslink | Kern-Inhalt der Erfassung | Google Sheets (separates Sheet) |
| Datum der Nachricht (Berliner Zeit) | Zeitliche Zuordnung | Google Sheets (separates Sheet) |

Links werden nur innerhalb des konfigurierten Zeitfensters erfasst. Die Daten werden ausschließlich im Google Sheet gespeichert – lokal auf dem Server werden keine iNat-Tracker-Daten abgelegt.

#### Rabattcode-Tracker

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Nachrichteninhalte des Rabattcode-Kanals | KI-Extraktion von Rabattcodes | Einmalig an Anthropic API übermittelt, dort nicht dauerhaft gespeichert |
| Bild-Anhänge des Rabattcode-Kanals (jpg, png, gif, webp, max. 4 MB, bis 4 pro Nachricht) | Erkennung von Codes in Screenshots/Flyern per Vision | Einmalig an Anthropic API übermittelt, nicht lokal gespeichert |
| Discord Message-IDs | Vermeidung doppelter KI-Auswertung (jede Nachricht nur einmal) | Lokale SQLite-Datenbank auf dem Server |
| Extrahierte Codes (Shop, Code, Rabatthöhe, Gültigkeit, ggf. Mindestbestellwert) | Bereitstellung über `/codes` | Lokale SQLite-Datenbank auf dem Server |
| Discord-Username des Verfassers der Code-Nachricht | Nachvollziehbarkeit der Quelle | Lokale SQLite-Datenbank auf dem Server |
| Datum der Quellnachricht | Berechnung von Gültigkeit/Alter eines Codes | Lokale SQLite-Datenbank auf dem Server |

> **Hinweis:** **Alle** Textnachrichten und Bild-Anhänge im Rabattcode-Kanal werden zur Verarbeitung an die Anthropic API (USA) übermittelt. Teile dort daher keine sensiblen personenbezogenen Daten. Anders als bei Bewertungen wird hier der Discord-Username des Verfassers gespeichert (zur Quellenangabe der Codes).

#### Wochen-Digest

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID (nur bei Anmeldung) | Versand des wöchentlichen Digests per Direktnachricht | Lokale SQLite-Datenbank auf dem Server |

Die Anmeldung erfolgt freiwillig per `/digest` und ist jederzeit per `/digest action:deaktivieren` widerrufbar. Der Digest-Inhalt (Preisstürze, neue Arten/Shops) enthält keine personenbezogenen Daten.

#### Erfolge (Achievements)

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID + freigeschaltete Erfolge | Persönliche Erfolgsübersicht (`/achievements`) | Lokale SQLite-Datenbank auf dem Server |
| Leichtes Event-Log (genutzte Befehle, Zielpreis-Treffer, Zeitpunkt) | Auswertung von Aktions- und versteckten Erfolgen | Lokale SQLite-Datenbank auf dem Server |

Erfolge sind rein persönlich (nur per `/achievements` für dich selbst sichtbar), es werden **keine Rollen** vergeben und nichts öffentlich angezeigt. Es werden keine Nachrichteninhalte gespeichert, nur Befehlsnamen und Zeitstempel.

#### Befehls-Log (Moderation)

| Daten | Zweck | Speicherort |
|-------|-------|-------------|
| Discord User-ID + Anzeigename, genutzter Befehl (Slash-Befehle + bekannte Text-Trigger wie `!hilfe`), Parameter, Kanal, Server, Zeitstempel, Erfolg/Fehler | Moderations- und Missbrauchskontrolle (nachvollziehen, wer welche Bot-Funktionen nutzt) | Lokale SQLite-Datenbank + optional ein Mod-only-Discord-Kanal |

Es werden **nur Befehlsnutzungen** protokolliert, **keine** beliebigen Nachrichtsinhalte. **Sensible Parameterwerte** (z. B. `user_id` bei `/export`) werden **ausgeblendet**. Der Mod-Kanal ist nur für Moderator:innen sichtbar; die dort geposteten Übersichten bleiben zur Nachvollziehbarkeit dauerhaft bestehen.

### Drittanbieter

#### Anthropic (KI-Verarbeitung)

Nachrichteninhalte werden zur automatischen Auswertung an die Anthropic API (USA) übermittelt und dort **nicht dauerhaft gespeichert**. Dies betrifft den Bewertungs-Bot (Shop-Bewertungen), den AI-Chat-Bot (alle Nachrichten im AI-Kanal) sowie den Rabattcode-Tracker (alle Textnachrichten im Rabattcode-Kanal). Anthropic verarbeitet Daten auf Basis von Standardvertragsklauseln (SCC) gemäß Art. 46 DSGVO.  
→ Datenschutz: https://www.anthropic.com/privacy

Gesprächsverläufe werden lokal für **maximal 24 Stunden** zwischengespeichert (für Konversationsgedächtnis) und danach automatisch gelöscht.

#### Google (Tabellenspeicherung)

Ausgewertete Bewertungen und iNat-Daten werden in Google Sheets (Deutschland) gespeichert.  
→ Datenschutz: https://policies.google.com/privacy

#### Frankfurter API (Währungskurse)

Zur EUR-Umrechnung von Preisen werden aktuelle Wechselkurse von `api.frankfurter.app` abgerufen. Es werden dabei **keine personenbezogenen Daten** übermittelt – die Anfrage enthält nur den Basiswährungscode (EUR). Kurse werden 6 Stunden im Speicher gecacht.  
→ https://www.frankfurter.app

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
- **Preis-Tracking-Einstellungen** (beobachtete Produkte, Arten-Beobachtungen, Baseline-Preise) werden auf Wunsch per `/untrack_price` jederzeit entfernt.
- **AI-Chat-Konversationsverläufe** werden automatisch nach **24 Stunden** gelöscht (oder sofort wenn du nicht auf eine Bot-Antwort antwortest).
- **AI-Chat-Budgetdaten** (User-ID + Tageskosten) werden nach dem jeweiligen Tag automatisch nicht mehr genutzt; eine manuelle Bereinigung erfolgt bei Bedarf.
- **Rabattcodes** (extrahierte Codes inkl. Quell-Username und Scan-Historie) werden gespeichert, solange sie für die Community relevant sind; eine Bereinigung erfolgt bei Bedarf manuell.
- **Digest-Anmeldung** (nur User-ID) wird gespeichert, bis du dich per `/digest action:deaktivieren` abmeldest.
- **Befehls-Nutzungsprotokoll** wird **12 Monate** in der lokalen Datenbank vorgehalten und danach automatisch gelöscht. Diese Dauer ist zweckgebunden für die Moderations- und Missbrauchskontrolle (ein voller Jahreszyklus erlaubt das Nachvollziehen wiederkehrender/saisonaler Muster, Ban-Evasion und später auftauchender Streitfälle) und an gängige Praxis für Moderations-/Audit-Logs angelehnt – sie ist **keine** gesetzlich vorgeschriebene Höchstfrist (die DSGVO nennt keine feste Frist; maßgeblich ist die Speicherbegrenzung nach Art. 5 Abs. 1 lit. e). Im Mod-Kanal gepostete Übersichten bleiben bestehen.
- **Technische Hilfsdaten** (Message-IDs, Shop-Zuordnungen) werden bei Bedarf manuell bereinigt.

### Deine Rechte

Du hast jederzeit das Recht auf **Auskunft, Berichtigung oder Löschung** deiner Daten.

- Benachrichtigungen und zugehörige Einstellungen kannst du selbst per `/delete_notifications` löschen.
- Eine Übersicht deiner aktiven Benachrichtigungen und Preis-Tracking-Einträge erhältst du per `/history`.
- Preis-Tracking-Einträge kannst du selbst per `/untrack_price` entfernen.
- Für alle weiteren Anfragen (Auskunft, manuelle Löschung) wende dich an:  
  📧 aam-bot@proton.me

Da Bewertungen anonym erfasst werden, ist eine Zuordnung zu einzelnen Personen dort in der Regel nicht möglich.

### Rechtsgrundlage

Die Verarbeitung erfolgt auf Grundlage des berechtigten Interesses (Art. 6 Abs. 1 lit. f DSGVO) – nämlich den Betrieb einer gemeinschaftlichen, anonymen Shop-Bewertungsplattform sowie eines personalisierten Verfügbarkeits- und Preis-Tracking-Dienstes für die AAM-Community.

---

*Stand: Juli 2026*