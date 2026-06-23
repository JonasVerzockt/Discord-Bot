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
utils/ai_chat.py – Backend-Logik fuer den KI-Chat-Bot.

Verwaltet:
  - Budget-Tracking (global + per User, taeglich um 00:00 UTC zurueckgesetzt)
  - Konversations-Historie (fuer Thread-Antworten via Discord-Reply)
  - Eingabe-Validierung und Kostenabschaetzung (Pre-Check)
  - Anthropic-API-Call (ohne Prompt Caching)

WARUM KEIN PROMPT CACHING:
  Claude Haiku 4.5 erfordert mindestens 4.096 Tokens um einen Cache-Eintrag
  anzulegen. Ein typischer System-Prompt hat ~50-200 Tokens – das Minimum wird
  nie erreicht. cache_control wird von der API kommentarlos ignoriert, kostet
  aber dennoch den 1.25x Cache-Write-Preis sobald die Schwelle erreichbar waere.
  Fuer diesen Use Case (sporadischer Chat, kurzer System-Prompt) ist kein
  Caching die ehrlichste und guenstigste Loesung.

HINWEIS zur Batch API:
  Die Anthropic Batch API bietet 50 % Rabatt, hat aber eine Latenz von bis
  zu 24 Stunden. Sie ist damit fuer interaktive Discord-Bots NICHT geeignet.

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

logger = logging.getLogger(__name__)

# ── Preise pro Modell (Stand: Juni 2026) ─────────────────────────────────────
# Quelle: https://www.anthropic.com/pricing
# Format: (input_usd_per_token, output_usd_per_token)
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00 / 1_000_000, 5.00  / 1_000_000),
    "claude-haiku-4-5":          (1.00 / 1_000_000, 5.00  / 1_000_000),
    "claude-sonnet-4-5":         (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-sonnet-4-6":         (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-opus-4-8":           (15.00 / 1_000_000, 75.00 / 1_000_000),
    "claude-opus-4":             (15.00 / 1_000_000, 75.00 / 1_000_000),
}
# Fallback: passendes Preisniveau anhand Modellname erraten
def _get_prices() -> tuple[float, float]:
    model = cfg.AI_CHAT_MODEL.lower()
    if model in _MODEL_PRICES:
        return _MODEL_PRICES[model]
    if "opus" in model:
        return (15.00 / 1_000_000, 75.00 / 1_000_000)
    if "sonnet" in model:
        return (3.00 / 1_000_000, 15.00 / 1_000_000)
    # Default: Haiku-Preise
    return (1.00 / 1_000_000, 5.00 / 1_000_000)

PRICE_INPUT,  PRICE_OUTPUT = _get_prices()

# ── Anthropic-Client (Singleton) ──────────────────────────────────────────────
_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not cfg.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY nicht gesetzt!")
        _client = anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)
    return _client


# ── DB-Initialisierung (selbstaendig, kein Eingriff in db.py noetig) ─────────

def init_ai_chat_tables() -> None:
    """Legt die benotigten Tabellen an, falls sie noch nicht existieren."""
    with sqlite3.connect(str(cfg.DB_FILE)) as con:
        con.executescript("""
            -- Tagesbudget-Tracking (user_id = 0 → global)
            CREATE TABLE IF NOT EXISTS ai_chat_budget (
                date     TEXT    NOT NULL,
                user_id  INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL    NOT NULL DEFAULT 0.0,
                PRIMARY KEY (date, user_id)
            );

            -- Konversations-Historie (Key: Discord-Message-ID der Bot-Antwort)
            CREATE TABLE IF NOT EXISTS ai_chat_history (
                message_id   INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                channel_id   INTEGER NOT NULL,
                history_json TEXT    NOT NULL,
                created_at   TEXT    NOT NULL,
                expires_at   TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ai_history_expires
                ON ai_chat_history (expires_at);
        """)
    logger.debug("[AI-Chat] Tabellen initialisiert")


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _today() -> str:
    """Gibt das aktuelle UTC-Datum als YYYY-MM-DD zurueck."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(cfg.DB_FILE))


def calculate_cost(usage) -> float:
    """
    Berechnet die tatsaechlichen Kosten aus dem Anthropic-Usage-Objekt.
    Kein explizites Caching aktiv – nur regulaere Input- und Output-Tokens.
    Cache-Felder werden defensiv mitgelesen falls Anthropic intern etwas cachet.
    """
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    cache_hit   = getattr(usage, "cache_read_input_tokens", 0)
    regular_in  = max(0, usage.input_tokens - cache_write - cache_hit)
    # Alle Input-Varianten zum gleichen Preis (kein Cache aktiv)
    return (
        (regular_in + cache_write + cache_hit) * PRICE_INPUT
        + usage.output_tokens * PRICE_OUTPUT
    )


def estimate_cost(input_chars: int, history_chars: int = 0) -> float:
    """
    Grobe Kostenschaetzung VOR dem API-Call.
    Wird fuer den Pre-Budget-Check verwendet (konservativ: 3.5 Zeichen = 1 Token).
    Kein Caching – System-Prompt wird als normaler Input gezaehlt.
    """
    system_tokens = len(cfg.AI_CHAT_SYSTEM_PROMPT) / 3.5
    input_tokens  = (input_chars + history_chars) / 3.5
    return (
        (system_tokens + input_tokens)    * PRICE_INPUT
        + cfg.AI_CHAT_MAX_OUTPUT_TOKENS   * PRICE_OUTPUT
    )


# ── Budget-Tracking ───────────────────────────────────────────────────────────

def get_global_cost_today() -> float:
    """Gibt die bisherigen globalen Kosten fuer heute (UTC) zurueck."""
    with _db() as con:
        row = con.execute(
            "SELECT cost_usd FROM ai_chat_budget WHERE date=? AND user_id=0",
            (_today(),)
        ).fetchone()
    return row[0] if row else 0.0


def get_user_cost_today(user_id: int) -> float:
    """Gibt die bisherigen Kosten eines Users fuer heute (UTC) zurueck."""
    with _db() as con:
        row = con.execute(
            "SELECT cost_usd FROM ai_chat_budget WHERE date=? AND user_id=?",
            (_today(), user_id)
        ).fetchone()
    return row[0] if row else 0.0


def add_cost(user_id: int, cost: float) -> None:
    """
    Addiert Kosten zum globalen Budget (user_id=0) und zum User-Budget.
    Wird NACH dem API-Call mit den tatsaechlichen Kosten aufgerufen.
    """
    today = _today()
    with _db() as con:
        for uid in (0, user_id):
            con.execute(
                """INSERT INTO ai_chat_budget (date, user_id, cost_usd)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date, user_id)
                   DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd""",
                (today, uid, cost),
            )


def _reset_time_str() -> str:
    """Gibt den naechsten Reset-Zeitpunkt als lesbaren String zurueck.
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


def check_budget(user_id: int, estimated_cost: float) -> tuple[bool, str]:
    """
    Prueft ob globales und User-Budget ausreichen.

    Returns:
        (True, "")                falls Budget OK
        (False, Fehlermeldung)    falls Budget erschoepft
    """
    global_used = get_global_cost_today()
    user_used   = get_user_cost_today(user_id)
    reset_str   = _reset_time_str()

    if global_used + estimated_cost > cfg.AI_CHAT_DAILY_BUDGET_USD:
        remaining = max(0.0, cfg.AI_CHAT_DAILY_BUDGET_USD - global_used)
        return False, (
            f"⚠️ Das globale Tagesbudget ist erschoepft "
            f"(${cfg.AI_CHAT_DAILY_BUDGET_USD:.2f}/Tag, "
            f"noch ${remaining:.4f} uebrig). "
            f"Reset um {reset_str}."
        )

    if user_used + estimated_cost > cfg.AI_CHAT_USER_DAILY_BUDGET_USD:
        remaining = max(0.0, cfg.AI_CHAT_USER_DAILY_BUDGET_USD - user_used)
        return False, (
            f"⚠️ Dein persoenliches Tagesbudget ist erschoepft "
            f"(${cfg.AI_CHAT_USER_DAILY_BUDGET_USD:.2f}/Tag, "
            f"noch ${remaining:.4f} uebrig). "
            f"Reset um {reset_str}."
        )

    return True, ""


def get_budget_status(user_id: int) -> dict:
    """Gibt eine Zusammenfassung des aktuellen Budget-Status zurueck."""
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
) -> None:
    """
    Speichert die Konversations-Historie fuer eine Bot-Message-ID.
    Wird NACH dem Senden der Bot-Antwort aufgerufen (dann kennen wir die ID).
    """
    expires = (
        datetime.now(timezone.utc)
        + timedelta(hours=cfg.AI_CHAT_CONVERSATION_TTL_HOURS)
    ).isoformat()
    with _db() as con:
        con.execute(
            """INSERT OR REPLACE INTO ai_chat_history
               (message_id, user_id, channel_id, history_json, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                bot_message_id,
                user_id,
                channel_id,
                json.dumps(history, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                expires,
            ),
        )


def load_conversation(bot_message_id: int) -> Optional[list]:
    """
    Laedt die gespeicherte Konversations-Historie fuer eine Bot-Message-ID.
    Gibt None zurueck wenn keine oder abgelaufene Historie vorhanden.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        row = con.execute(
            """SELECT history_json FROM ai_chat_history
               WHERE message_id=? AND expires_at > ?""",
            (bot_message_id, now),
        ).fetchone()
    return json.loads(row[0]) if row else None


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
    Prueft den User-Input auf Laenge und Inhalt.
    Schutzt vor teuren Anfragen durch Zeichen-Limit.
    """
    text = text.strip()
    if not text:
        return False, "❌ Leere Nachricht – bitte schreib etwas."
    if len(text) > cfg.AI_CHAT_MAX_INPUT_CHARS:
        return False, (
            f"❌ Deine Nachricht ist zu lang "
            f"({len(text):,}/{cfg.AI_CHAT_MAX_INPUT_CHARS:,} Zeichen). "
            f"Bitte kueze sie."
        )
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
) -> dict:
    """
    Sendet eine Nachricht an Claude Haiku.

    Ablauf:
      1. Eingabe validieren (Laenge)
      2. Konversations-Historie laden (falls Reply auf Bot-Nachricht)
      3. Pre-Budget-Check (Kostenschaetzung)
      4. API-Call (einfacher String-System-Prompt, kein Caching)
      5. Tatsaechliche Kosten tracken
      6. Aktualisierte History zurueckgeben (zum Speichern nach bot.reply)

    Args:
        user_id:             Discord User-ID (fuer Budget-Tracking)
        user_message:        Nachrichtentext vom User
        prev_bot_message_id: Discord-Message-ID der vorherigen Bot-Antwort
                             (fuer Konversations-Fortsetzung via Reply)
        channel_id:          Discord-Channel-ID (wird mit History gespeichert)

    Returns dict:
        ok       (bool)  – True = Antwort vorhanden
        answer   (str)   – Antworttext oder Fehlermeldung
        cost     (float) – Tatsaechliche Kosten in USD (0.0 bei Fehler)
        history  (list)  – Aktualisierte History ([] bei Fehler)
        is_error (bool)  – True bei technischem Fehler (nicht Budget-Problem)
    """

    # 1. Eingabe validieren
    ok, reason = validate_input(user_message)
    if not ok:
        return {"ok": False, "answer": reason, "cost": 0.0,
                "history": [], "is_error": False}

    user_message = user_message.strip()

    # 2. Konversations-Historie laden
    history: list = []
    if prev_bot_message_id:
        loaded = load_conversation(prev_bot_message_id)
        if loaded:
            history = trim_history(loaded)
            logger.debug(
                f"[AI-Chat] History geladen fuer msg_id={prev_bot_message_id} "
                f"({len(history)} Eintraege)"
            )

    # 3. Pre-Budget-Check
    history_chars = sum(len(m.get("content", "")) for m in history)
    estimated     = estimate_cost(len(user_message), history_chars)
    budget_ok, budget_msg = check_budget(user_id, estimated)
    if not budget_ok:
        return {"ok": False, "answer": budget_msg, "cost": 0.0,
                "history": [], "is_error": False}

    # 4. Nachrichten fuer API zusammenstellen
    messages = history + [{"role": "user", "content": user_message}]

    # 5. API-Call (kein Prompt Caching: Haiku 4.5 benoetigt min. 4.096 Tokens,
    #    ein typischer System-Prompt hat ~50-200 Tokens – Minimum nie erreicht)
    try:
        client   = _get_client()
        response = await client.messages.create(
            model=cfg.AI_CHAT_MODEL,
            max_tokens=cfg.AI_CHAT_MAX_OUTPUT_TOKENS,
            system=cfg.AI_CHAT_SYSTEM_PROMPT,
            messages=messages,
        )

    except anthropic.RateLimitError:
        logger.warning("[AI-Chat] Rate-Limit erreicht")
        return {
            "ok": False,
            "answer": "⚠️ Rate-Limit erreicht. Bitte kurz warten und erneut versuchen.",
            "cost": 0.0, "history": [], "is_error": True,
        }
    except anthropic.APIStatusError as e:
        logger.error(f"[AI-Chat] API-Statusfehler {e.status_code}: {e.message}")
        return {
            "ok": False,
            "answer": f"❌ API-Fehler ({e.status_code}). Bitte spaeter erneut versuchen.",
            "cost": 0.0, "history": [], "is_error": True,
        }
    except Exception as e:
        logger.error(f"[AI-Chat] Unbekannter Fehler: {e}", exc_info=True)
        return {
            "ok": False,
            "answer": "❌ Unbekannter Fehler. Bitte Jonas Bescheid geben.",
            "cost": 0.0, "history": [], "is_error": True,
        }

    # 6. Antwort extrahieren
    answer = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ) or "(Keine Antwort erhalten)"

    # 7. Tatsaechliche Kosten tracken (nach erfolgreichem Call)
    actual_cost = calculate_cost(response.usage)
    add_cost(user_id, actual_cost)

    logger.info(
        f"[AI-Chat] user={user_id} "
        f"in={response.usage.input_tokens}t "
        f"out={response.usage.output_tokens}t "
        f"cache_w={getattr(response.usage, 'cache_creation_input_tokens', 0)}t "
        f"cache_h={getattr(response.usage, 'cache_read_input_tokens', 0)}t "
        f"cost=${actual_cost:.6f}"
    )

    # 8. Aktualisierte History (wird vom Cog nach bot.reply() gespeichert)
    new_history = trim_history(
        messages + [{"role": "assistant", "content": answer}]
    )

    return {
        "ok":       True,
        "answer":   answer,
        "cost":     actual_cost,
        "history":  new_history,
        "is_error": False,
    }
