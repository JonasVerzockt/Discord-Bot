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
utils/ai_chat.py – Backend-Logik für den KI-Chat-Bot.

Verwaltet:
  - Budget-Tracking (global + per User, täglich um 00:00 UTC zurückgesetzt)
  - Konversations-Historie (für Thread-Antworten via Discord-Reply)
  - Eingabe-Validierung und Kostenabschaetzung (Pre-Check)
  - 3-stufige Shop-Daten-Vorqualifizierung (Keyword → Haiku → Sonnet)
  - Anthropic-API-Call (ohne Prompt Caching)

SHOP-DATEN-VORQUALIFIZIERUNG (3 Stufen):
  Stage 1 – Keyword-Check (kostenlos, sofort):
    Enthält die Nachricht shop-relevante Woerter? → Ja: Shop-Daten rein,
    fertig. Nein: weiter zu Stage 2.
  Stage 2 – Haiku-Klassifikation (~$0.00025, ~200ms):
    Haiku bewertet ob die Nachricht indirekt shop-relevant ist
    (z.B. "wo kaufe ich günstig?"). Nur bei "JA" werden Shop-Daten geladen.
    Kosten werden immer zum Gesamtbetrag addiert und im Disclaimer angezeigt.
  Stage 3 – Sonnet-Hauptaufruf:
    Mit oder ohne Shop-Daten im System-Prompt je nach Stage-1/2-Ergebnis.

WARUM KEIN PROMPT CACHING:
  Claude Haiku 4.5 erfordert mindestens 4.096 Tokens um einen Cache-Eintrag
  anzulegen. Ein typischer System-Prompt hat ~50-200 Tokens – das Minimum wird
  nie erreicht. cache_control wird von der API kommentarlos ignoriert, kostet
  aber dennoch den 1.25x Cache-Write-Preis sobald die Schwelle erreichbar waere.
  Für diesen Use Case (sporadischer Chat, kurzer System-Prompt) ist kein
  Caching die ehrlichste und guenstigste Loesung.

HINWEIS zur Batch API:
  Die Anthropic Batch API bietet 50 % Rabatt, hat aber eine Latenz von bis
  zu 24 Stunden. Sie ist damit für interaktive Discord-Bots NICHT geeignet.

Quellen Preise:
  https://www.anthropic.com/claude/haiku
  https://www.anthropic.com/news/claude-haiku-4-5
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic

import config as cfg
from utils.localization import l10n

logger = logging.getLogger(__name__)

# ── Preise pro Modell (Stand: Juni 2026) ──────────────────────────────────────
# Quelle: https://docs.anthropic.com/en/docs/about-claude/pricing
# Format: (input_usd_per_token, output_usd_per_token)
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    # Haiku
    "claude-haiku-4-5-20251001": (1.00 / 1_000_000,  5.00 / 1_000_000),
    "claude-haiku-4-5":          (1.00 / 1_000_000,  5.00 / 1_000_000),
    # Sonnet
    "claude-sonnet-4-5":         (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-sonnet-4-6":         (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-sonnet-5":           (3.00 / 1_000_000, 15.00 / 1_000_000),
    # Opus 4.5+ (neue Preisstruktur: $5/$25)
    "claude-opus-4-5":           (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-opus-4-6":           (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-opus-4-7":           (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-opus-4-8":           (5.00 / 1_000_000, 25.00 / 1_000_000),
    # Fable 5 (Top-Tier, $10/$50 lt. offizieller Preisliste)
    "claude-fable-5":            (10.00 / 1_000_000, 50.00 / 1_000_000),
    # Opus 4.1 (deprecated) + Opus 4 (retired) – alte Preisstruktur: $15/$75
    "claude-opus-4-1":           (15.00 / 1_000_000, 75.00 / 1_000_000),
    "claude-opus-4":             (15.00 / 1_000_000, 75.00 / 1_000_000),
}


def prices_for(model: str) -> tuple[float, float]:
    """Preis (Input, Output) pro Token für ein Modell. Fallback ueber Namensfamilie
    (konservativ = teureres Niveau), damit der Budget-Check nie zu niedrig schaetzt."""
    m = (model or "").lower()
    if m in _MODEL_PRICES:
        return _MODEL_PRICES[m]
    if "fable" in m or "mythos" in m:
        return (10.00 / 1_000_000, 50.00 / 1_000_000)
    if "opus" in m:
        return (5.00 / 1_000_000, 25.00 / 1_000_000)
    if "sonnet" in m:
        return (3.00 / 1_000_000, 15.00 / 1_000_000)
    return (1.00 / 1_000_000, 5.00 / 1_000_000)


# Preis des in der .env konfigurierten Standardmodells (Default, wenn kein Modell
# explizit uebergeben wird). Pro-Anfrage wird immer prices_for(model) genutzt.
PRICE_INPUT,  PRICE_OUTPUT = prices_for(cfg.AI_CHAT_MODEL)

# ── Anthropic-Client (Singleton) ──────────────────────────────────────────────
_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not cfg.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY nicht gesetzt!")
        _client = anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)
    return _client


# ── 3-stufige Shop-Daten-Vorqualifizierung ────────────────────────────────────

# Stage 1: Generische Shop-Begriffe (statisch, Shop-Namen kommen dynamisch aus Sheet)
_SHOP_KEYWORDS: frozenset[str] = frozenset({
    "shop", "händler", "haendler", "kaufen", "kauf", "bestellen", "bestellung",
    "warnung", "warnhinweis", "scam", "betrug", "abzocke",
    "bewertung", "bewertungen", "empfehlung", "empfehlen",
    "preis", "preise", "versand", "lieferung", "liefern",
    "erfahrung", "erfahrungen", "anbieter", "store", "webshop",
})


def _needs_shop_data(message: str) -> bool:
    """
    Stage 1: Schneller Keyword-Check ohne API-Kosten.
    Prüft statische generische Begriffe UND dynamisch geladene Shop-Namen
    aus dem Google Sheet (wird alle 6 Stunden aktualisiert).
    """
    from utils.sheets_shop_data import get_cached_shop_names
    msg = message.lower()
    return (
        any(kw in msg for kw in _SHOP_KEYWORDS)
        or any(name in msg for name in get_cached_shop_names())
    )


# Stage 2: Haiku-Klassifikation
_HAIKU_CLASSIFY_MODEL = cfg.AI_CHAT_CLASSIFY_MODEL
_HAIKU_PRICE_IN  = 1.00 / 1_000_000
_HAIKU_PRICE_OUT = 5.00 / 1_000_000

_CLASSIFY_SYSTEM = (
    "Du klassifizierst Discord-Nachrichten für einen Ameisen-Community-Bot. "
    "Antworte NUR mit 'JA' wenn die Nachricht eine Frage oder Aussage zu "
    "Ameisen-Shops, Haendlern, Kaufempfehlungen, Warnhinweisen, Scams oder "
    "Online-Bestellungen enthält. "
    "Antworte NUR mit 'NEIN' in allen anderen Faellen. Kein weiterer Text."
)


async def _classify_shop_haiku(message: str) -> dict:
    """
    Stage 2: Haiku klassifiziert ob Shop-Daten benötigt werden.
    Wird nur aufgerufen wenn Stage 1 (Keyword) keinen Treffer hatte.

    Returns:
        {"needs_shop": bool, "cost": float}
    Fehlerfall: Fallback auf needs_shop=True (sicherer als ohne Daten antworten).
    """
    try:
        client = _get_client()
        response = await client.messages.create(
            model=_HAIKU_CLASSIFY_MODEL,
            max_tokens=5,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": message}],
        )
        answer = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip().upper()
        cost = (
            response.usage.input_tokens  * _HAIKU_PRICE_IN
            + response.usage.output_tokens * _HAIKU_PRICE_OUT
        )
        needs_shop = answer.startswith("JA")
        logger.debug(
            f"[AI-Chat] Stage 2 Haiku: '{answer}' → shop={needs_shop} "
            f"(in={response.usage.input_tokens}t out={response.usage.output_tokens}t "
            f"cost=${cost:.6f})"
        )
        return {"needs_shop": needs_shop, "cost": cost}

    except Exception as e:
        logger.warning(
            f"[AI-Chat] Stage 2 Haiku-Klassifikation fehlgeschlagen: {e} "
            f"– Fallback: shop=True"
        )
        return {"needs_shop": True, "cost": 0.0}


# ── DB-Initialisierung ────────────────────────────────────────────────────────
# Die Tabellen ai_chat_budget und ai_chat_history werden zentral in
# utils/db.py:init_db() angelegt (siehe dortiges _SCHEMA).


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _today() -> str:
    """Gibt das aktuelle UTC-Datum als YYYY-MM-DD zurück."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(str(cfg.DB_FILE))
    # Konsistent zu utils/db.py: bis zu 5s warten statt sofort "database is locked".
    con.execute("PRAGMA busy_timeout=5000")
    return con


def calculate_cost(usage, model: str | None = None) -> float:
    """
    Berechnet die tatsächlichen Kosten aus dem Anthropic-Usage-Objekt.
    Kein explizites Caching aktiv – nur regulaere Input- und Output-Tokens.
    Cache-Felder werden defensiv mitgelesen falls Anthropic intern etwas cachet.
    ``model`` bestimmt den Preis (Default: konfiguriertes Modell).
    """
    price_in, price_out = prices_for(model or cfg.AI_CHAT_MODEL)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    cache_hit   = getattr(usage, "cache_read_input_tokens", 0)
    regular_in  = max(0, usage.input_tokens - cache_write - cache_hit)
    # Alle Input-Varianten zum gleichen Preis (kein Cache aktiv)
    return (
        (regular_in + cache_write + cache_hit) * price_in
        + usage.output_tokens * price_out
    )


def estimate_cost(input_chars: int, history_chars: int = 0, num_images: int = 0,
                  model: str | None = None) -> float:
    """
    Grobe Kostenschaetzung VOR dem API-Call.
    Wird für den Pre-Budget-Check verwendet (konservativ: 3.5 Zeichen = 1 Token).
    Kein Caching – System-Prompt wird als normaler Input gezaehlt.
    Bilder: ~1500 Token pro Bild (konservativer Schaetzwert).
    ``model`` bestimmt den Preis (Default: konfiguriertes Modell).
    """
    price_in, price_out = prices_for(model or cfg.AI_CHAT_MODEL)
    # Konservative Schaetzung: laengsten verfügbaren Prompt nehmen
    system_tokens = max((len(p) for p in cfg.AI_CHAT_SYSTEM_PROMPTS.values()), default=0) / 3.5
    input_tokens  = (input_chars + history_chars) / 3.5
    image_tokens  = num_images * 1500
    return (
        (system_tokens + input_tokens + image_tokens) * price_in
        + cfg.AI_CHAT_MAX_OUTPUT_TOKENS               * price_out
    )


async def count_input_tokens(model: str, system: str, messages: list) -> Optional[int]:
    """Exakte Input-Token-Anzahl via Anthropic count_tokens-Endpoint (kostenlos).
    Gibt None zurück, wenn der Aufruf fehlschlaegt (dann Heuristik als Fallback)."""
    try:
        client = _get_client()
        r = await client.messages.count_tokens(model=model, system=system, messages=messages)
        return int(r.input_tokens)
    except Exception as e:
        logger.debug(f"[AI-Chat] count_tokens fehlgeschlagen ({e}) – nutze Heuristik")
        return None


async def list_available_models() -> Optional[set[str]]:
    """Menge der fuer diesen API-Key verfuegbaren Modell-IDs (GET /v1/models).
    None bei Fehler -> Aufrufer soll 'fail open' behandeln (alle zeigen)."""
    try:
        client = _get_client()
        ids: set[str] = set()
        page = await client.models.list(limit=1000)
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", None)
            if mid:
                ids.add(mid)
        return ids or None
    except Exception as e:
        logger.warning(f"[AI-Chat] Modell-Liste konnte nicht geladen werden: {e}")
        return None


# ── Budget-Tracking ───────────────────────────────────────────────────────────

def get_global_cost_today() -> float:
    """Gibt die bisherigen globalen Kosten für heute (UTC) zurück."""
    with _db() as con:
        row = con.execute(
            "SELECT cost_usd FROM ai_chat_budget WHERE date=? AND user_id=0",
            (_today(),)
        ).fetchone()
    return row[0] if row else 0.0


def get_user_cost_today(user_id: int) -> float:
    """Gibt die bisherigen Kosten eines Users für heute (UTC) zurück."""
    with _db() as con:
        row = con.execute(
            "SELECT cost_usd FROM ai_chat_budget WHERE date=? AND user_id=?",
            (_today(), user_id)
        ).fetchone()
    return row[0] if row else 0.0


def add_cost(user_id: int, cost: float) -> None:
    """
    Addiert Kosten zum globalen Budget (user_id=0) und zum User-Budget.
    Wird NACH dem API-Call mit den tatsächlichen Kosten aufgerufen.
    """
    today = _today()
    now   = datetime.now(timezone.utc)
    with _db() as con:
        for uid in (0, user_id):
            con.execute(
                """INSERT INTO ai_chat_budget (date, user_id, cost_usd)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date, user_id)
                   DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd""",
                (today, uid, cost),
            )
        # Historisches Ausgaben-Ledger pro User (Tag/Woche/Monat/Jahr).
        # Nur echte User (nicht der globale Sammel-Eintrag user_id=0).
        if user_id and user_id != 0:
            periods = (
                ("day",   now.strftime("%Y-%m-%d")),
                ("week",  now.strftime("%G-W%V")),   # ISO-Jahr + ISO-Kalenderwoche
                ("month", now.strftime("%Y-%m")),
                ("year",  now.strftime("%Y")),
            )
            for ptype, pkey in periods:
                con.execute(
                    """INSERT INTO ai_chat_user_spend
                           (user_id, period_type, period_key, cost_usd, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, period_type, period_key)
                       DO UPDATE SET cost_usd  = cost_usd + excluded.cost_usd,
                                     updated_at = excluded.updated_at""",
                    (user_id, ptype, pkey, cost, now.isoformat()),
                )


def _reset_time_str() -> str:
    """Gibt den nächsten Reset-Zeitpunkt als lesbaren String zurück.
    Zeigt UTC + MEZ (UTC+1) bzw. MESZ (UTC+2) je nach Jahreszeit."""
    import datetime as _dt
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    # Naechster Reset = naechster Tag 00:00 UTC
    reset_utc = (now_utc + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Sommerzeit: letzter Sonntag im Maerz bis letzter Sonntag im Oktober
    year = reset_utc.year
    # Letzter Sonntag im Maerz
    dst_start = max(
        _dt.datetime(year, 3, d, 2, tzinfo=_dt.timezone.utc)
        for d in range(25, 32)
        if _dt.datetime(year, 3, d).weekday() == 6
    )
    # Letzter Sonntag im Oktober
    dst_end = max(
        _dt.datetime(year, 10, d, 1, tzinfo=_dt.timezone.utc)
        for d in range(25, 32)
        if _dt.datetime(year, 10, d).weekday() == 6
    )
    if dst_start <= reset_utc < dst_end:
        offset, tz_name = 2, "MESZ"
    else:
        offset, tz_name = 1, "MEZ"
    reset_local = reset_utc + _dt.timedelta(hours=offset)
    return (
        f"00:00 UTC / {reset_local.strftime('%H:%M')} {tz_name} "
        f"({reset_utc.strftime('%d.%m.%Y')})"
    )


def check_budget(user_id: int, estimated_cost: float, lang: str = "en") -> tuple[bool, str]:
    """
    Prüft ob globales und User-Budget ausreichen.

    Returns:
        (True, "")                falls Budget OK
        (False, Fehlermeldung)    falls Budget erschoepft
    """
    global_used = get_global_cost_today()
    user_used   = get_user_cost_today(user_id)
    reset_str   = _reset_time_str()

    if global_used + estimated_cost > cfg.AI_CHAT_DAILY_BUDGET_USD:
        remaining = max(0.0, cfg.AI_CHAT_DAILY_BUDGET_USD - global_used)
        return False, l10n.get(
            "ai_budget_global_exhausted", lang,
            limit=cfg.AI_CHAT_DAILY_BUDGET_USD, remaining=remaining,
            estimated=estimated_cost, reset=reset_str,
        )

    if user_used + estimated_cost > cfg.AI_CHAT_USER_DAILY_BUDGET_USD:
        remaining = max(0.0, cfg.AI_CHAT_USER_DAILY_BUDGET_USD - user_used)
        return False, l10n.get(
            "ai_budget_user_exhausted", lang,
            limit=cfg.AI_CHAT_USER_DAILY_BUDGET_USD, remaining=remaining,
            estimated=estimated_cost, reset=reset_str,
        )

    return True, ""


def get_budget_status(user_id: int) -> dict:
    """Gibt eine Zusammenfassung des aktuellen Budget-Status zurück."""
    global_used = get_global_cost_today()
    user_used   = get_user_cost_today(user_id)
    return {
        "global_used":  global_used,
        "global_limit": cfg.AI_CHAT_DAILY_BUDGET_USD,
        "global_pct":   (global_used / cfg.AI_CHAT_DAILY_BUDGET_USD * 100)
                         if cfg.AI_CHAT_DAILY_BUDGET_USD else 0.0,
        "user_used":    user_used,
        "user_limit":   cfg.AI_CHAT_USER_DAILY_BUDGET_USD,
        "user_pct":     (user_used / cfg.AI_CHAT_USER_DAILY_BUDGET_USD * 100)
                         if cfg.AI_CHAT_USER_DAILY_BUDGET_USD else 0.0,
    }


# ── Konversations-Historie ────────────────────────────────────────────────────

def save_conversation(
    bot_message_id: int,
    user_id: int,
    channel_id: int,
    history: list,
    model: str = "",
) -> None:
    """
    Speichert die Konversations-Historie für eine Bot-Message-ID.
    Wird NACH dem Senden der Bot-Antwort aufgerufen (dann kennen wir die ID).
    ``model`` = das für diese Antwort genutzte Modell (fuer Reply-Fortsetzung).
    """
    expires = (
        datetime.now(timezone.utc)
        + timedelta(hours=cfg.AI_CHAT_CONVERSATION_TTL_HOURS)
    ).isoformat()
    with _db() as con:
        con.execute(
            """INSERT OR REPLACE INTO ai_chat_history
               (message_id, user_id, channel_id, history_json, created_at, expires_at, model)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                bot_message_id,
                user_id,
                channel_id,
                json.dumps(history, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                expires,
                model or "",
            ),
        )


def load_conversation(bot_message_id: int) -> Optional[list]:
    """
    Lädt die gespeicherte Konversations-Historie für eine Bot-Message-ID.
    Gibt None zurück wenn keine oder abgelaufene Historie vorhanden.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        row = con.execute(
            """SELECT history_json FROM ai_chat_history
               WHERE message_id=? AND expires_at > ?""",
            (bot_message_id, now),
        ).fetchone()
    return json.loads(row[0]) if row else None


def load_conversation_model(bot_message_id: int) -> Optional[str]:
    """
    Gibt das für eine Bot-Antwort verwendete Modell zurück (fuer Reply-Fortsetzung
    mit demselben Modell). None, wenn keine/abgelaufene Historie oder kein Modell.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        row = con.execute(
            """SELECT model FROM ai_chat_history
               WHERE message_id=? AND expires_at > ?""",
            (bot_message_id, now),
        ).fetchone()
    return (row[0] or None) if row else None


def get_user_model(user_id: int) -> Optional[str]:
    """Zuletzt vom User gewaehltes Modell (fuer die Dropdown-Vorauswahl)."""
    with _db() as con:
        row = con.execute(
            "SELECT model FROM ai_chat_user_model WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return (row[0] or None) if row else None


def set_user_model(user_id: int, model: str) -> None:
    """Merkt sich das zuletzt gewaehlte Modell eines Users."""
    with _db() as con:
        con.execute(
            """INSERT INTO ai_chat_user_model (user_id, model, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   model=excluded.model, updated_at=excluded.updated_at""",
            (user_id, model, datetime.now(timezone.utc).isoformat()),
        )


def cleanup_expired_conversations() -> int:
    """
    Loescht abgelaufene Konversationen aus der DB.
    Wird regelmaessig vom Cleanup-Task aufgerufen.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        cur = con.execute(
            "DELETE FROM ai_chat_history WHERE expires_at <= ?", (now,)
        )
        return cur.rowcount


# ── Eingabe-Validierung ───────────────────────────────────────────────────────

def validate_input(text: str) -> tuple[bool, str]:
    """
    Prüft den User-Input auf Inhalt.
    Längen-Check für getippte Nachrichten erfolgt bereits im Cog (vor Datei-Anhang).
    Hier wird nur noch auf leere Eingabe geprüft.
    """
    text = text.strip()
    if not text:
        return False, "❌ Leere Nachricht – bitte schreib etwas."
    return True, ""


def trim_history(history: list) -> list:
    """
    Begrenzt die Konversations-Historie auf AI_CHAT_MAX_HISTORY_TURNS Runden.
    Entfernt immer das aelteste user+assistant-Paar vom Anfang.
    Schuetzt vor ballonierendem Kontext (= teure Requests).
    """
    max_msgs = cfg.AI_CHAT_MAX_HISTORY_TURNS * 2  # je 1× user + 1× assistant
    while len(history) > max_msgs:
        history = history[2:]
    return history


def chunk_discord(text: str, max_len: int = 1990) -> list[str]:
    """
    Teilt langen Text in Discord-kompatible Stuecke (max. 2000 Zeichen).
    Versucht an Zeilenumbruechen zu trennen.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip("\n")
    return chunks


# ── Hauptfunktion: API-Call ───────────────────────────────────────────────────

async def chat(
    user_id: int,
    user_message: str,
    prev_bot_message_id: Optional[int] = None,
    channel_id: int = 0,
    images: Optional[list[tuple[bytes, str]]] = None,
    user_lang: str = "en",
    model: Optional[str] = None,
) -> dict:
    """
    Sendet eine Nachricht an das konfigurierte Claude-Modell.
    Standard: claude-haiku-4-5 – aktuell: claude-sonnet-4-6 (siehe .env / config.py).

    Ablauf:
      1. Eingabe validieren (Laenge)
      2. Konversations-Historie laden (falls Reply auf Bot-Nachricht)
      3. Pre-Budget-Check (Kostenschaetzung)
      4. API-Call (einfacher String-System-Prompt, kein Caching)
      5. Tatsächliche Kosten tracken
      6. Aktualisierte History zurückgeben (zum Speichern nach bot.reply)

    Args:
        user_id:             Discord User-ID (für Budget-Tracking)
        user_message:        Nachrichtentext vom User
        prev_bot_message_id: Discord-Message-ID der vorherigen Bot-Antwort
                             (für Konversations-Fortsetzung via Reply)
        channel_id:          Discord-Channel-ID (wird mit History gespeichert)

    Returns dict:
        ok       (bool)  – True = Antwort vorhanden
        answer   (str)   – Antworttext oder Fehlermeldung
        cost     (float) – Tatsächliche Kosten in USD (0.0 bei Fehler)
        history  (list)  – Aktualisierte History ([] bei Fehler)
        is_error (bool)  – True bei technischem Fehler (nicht Budget-Problem)
    """

    # 0. Modell bestimmen (Default: konfiguriertes .env-Modell)
    model = model or cfg.AI_CHAT_MODEL

    # 1. Eingabe validieren
    ok, _reason = validate_input(user_message)
    if not ok:
        return {"ok": False, "answer": l10n.get("ai_err_empty", user_lang), "cost": 0.0,
                "history": [], "is_error": False, "model": model}

    user_message = user_message.strip()

    # 2. Konversations-Historie laden
    history: list = []
    if prev_bot_message_id:
        loaded = load_conversation(prev_bot_message_id)
        if loaded:
            history = trim_history(loaded)
            logger.debug(
                f"[AI-Chat] History geladen für msg_id={prev_bot_message_id} "
                f"({len(history)} Einträge)"
            )

    history_chars = sum(len(m.get("content", "") if isinstance(m.get("content"), str) else "") for m in history)
    num_images    = len(images) if images else 0

    # 3. Nachrichten für API zusammenstellen
    import base64 as _b64
    if images:
        user_content: list = []
        for img_bytes, media_type in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": _b64.b64encode(img_bytes).decode(),
                },
            })
        if user_message:
            user_content.append({"type": "text", "text": user_message})
        messages = history + [{"role": "user", "content": user_content}]
    else:
        messages = history + [{"role": "user", "content": user_message}]

    # 5. Shop-Daten 3-stufig bestimmen
    #    Stage 1: Keyword-Check (kostenlos)
    #    Stage 2: Haiku-Klassifikation (nur wenn kein Keyword gefunden)
    #    Stage 3: Shop-Block in System-Prompt einbetten (oder weglassen)
    from utils.sheets_shop_data import get_cached_block as _shop_block

    precheck_cost = 0.0
    if _needs_shop_data(user_message):
        shop_data = _shop_block()
        logger.debug("[AI-Chat] Stage 1: Keyword-Treffer → Shop-Daten eingeschlossen")
    else:
        classify    = await _classify_shop_haiku(user_message)
        precheck_cost = classify["cost"]
        shop_data   = _shop_block() if classify["needs_shop"] else None
        logger.debug(
            f"[AI-Chat] Stage 2: Haiku → shop={classify['needs_shop']} "
            f"(precheck_cost=${precheck_cost:.6f})"
        )

    # Sprachspezifischen System-Prompt auswaehlen (Fallback: User-Sprache → en)
    base_prompt = (
        cfg.AI_CHAT_SYSTEM_PROMPTS.get(user_lang)
        or cfg.AI_CHAT_SYSTEM_PROMPTS.get("en")
    )
    if not base_prompt:
        logger.error(
            "[AI-Chat] Kein System-Prompt verfügbar – ai_chat_system_prompt_en.txt fehlt"
        )
        return {
            "ok": False,
            "answer": l10n.get("ai_err_no_prompt", user_lang),
            "cost": 0.0, "history": [], "is_error": True,
        }
    system_prompt = (
        base_prompt + "\n\n" + shop_data
        if shop_data else
        base_prompt
    )

    # 5. Pre-Budget-Check mit EXAKTER Token-Zählung (count_tokens); Fallback auf
    #    die Zeichen-Heuristik, falls der Endpoint nicht erreichbar ist. Output
    #    wird konservativ mit dem Maximum angesetzt.
    price_in, price_out = prices_for(model)
    input_tokens = await count_input_tokens(model, system_prompt, messages)
    if input_tokens is not None:
        estimated = (
            input_tokens * price_in
            + cfg.AI_CHAT_MAX_OUTPUT_TOKENS * price_out
            + precheck_cost
        )
    else:
        estimated = estimate_cost(len(user_message), history_chars, num_images, model) + precheck_cost
    budget_ok, budget_msg = check_budget(user_id, estimated, user_lang)
    if not budget_ok:
        if precheck_cost:
            add_cost(user_id, precheck_cost)   # bereits verbrauchte Haiku-Vorklassifikation abrechnen
        return {"ok": False, "answer": budget_msg, "cost": precheck_cost,
                "history": [], "is_error": False,
                "budget_exceeded": True, "estimated": estimated, "model": model}

    # 6. API-Call (kein Prompt Caching: bei geringer Nutzung teurer als ohne)
    try:
        client   = _get_client()
        response = await client.messages.create(
            model=model,
            max_tokens=cfg.AI_CHAT_MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=messages,
        )

    except anthropic.RateLimitError:
        logger.warning("[AI-Chat] Rate-Limit erreicht")
        return {
            "ok": False,
            "answer": l10n.get("ai_err_ratelimit", user_lang),
            "cost": 0.0, "history": [], "is_error": True,
        }
    except anthropic.APIStatusError as e:
        logger.error(f"[AI-Chat] API-Statusfehler {e.status_code}: {e.message}")
        return {
            "ok": False,
            "answer": l10n.get("ai_err_api", user_lang, status=e.status_code),
            "cost": 0.0, "history": [], "is_error": True,
        }
    except Exception as e:
        logger.error(f"[AI-Chat] Unbekannter Fehler: {e}", exc_info=True)
        return {
            "ok": False,
            "answer": l10n.get("ai_err_unknown", user_lang),
            "cost": 0.0, "history": [], "is_error": True,
        }

    # 7. Antwort extrahieren
    answer = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ) or l10n.get("ai_no_answer", user_lang)

    # 8. Tatsächliche Kosten tracken (gewaehltes Modell + Haiku-Precheck falls vorhanden)
    actual_cost = calculate_cost(response.usage, model) + precheck_cost
    add_cost(user_id, actual_cost)

    logger.info(
        f"[AI-Chat] user={user_id} "
        f"in={response.usage.input_tokens}t "
        f"out={response.usage.output_tokens}t "
        f"cache_w={getattr(response.usage, 'cache_creation_input_tokens', 0)}t "
        f"cache_h={getattr(response.usage, 'cache_read_input_tokens', 0)}t "
        f"cost=${actual_cost:.6f}"
        + (f" (inkl. Haiku-Precheck ${precheck_cost:.6f})" if precheck_cost else "")
    )

    # 9. Aktualisierte History (wird vom Cog nach bot.reply() gespeichert)
    new_history = trim_history(
        messages + [{"role": "assistant", "content": answer}]
    )

    return {
        "ok":       True,
        "answer":   answer,
        "cost":     actual_cost,
        "history":  new_history,
        "is_error": False,
        "model":    model,
    }
