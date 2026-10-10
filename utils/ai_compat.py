# SPDX-License-Identifier: AGPL-3.0-or-later
"""
utils/ai_compat.py – Hilfen für neuere Claude-Modelle bei kurzen, strukturierten Aufrufen
(Review-Parser, Rabattcode-Parser, Shop-Klassifikation).

Hintergrund (Anthropic-Doku „Thinking"): Bei Claude Haiku 5.5, Sonnet 5.5, Opus 5.5 und
Fable 5.1 ist Thinking standardmäßig AN. Die Antwort beginnt dann mit einem ThinkingBlock
(ohne .text), und Thinking-Tokens zählen gegen max_tokens.
  • Haiku 5.5:  thinking={"type": "disabled"} erlaubt (bei effort high oder niedriger).
  • Sonnet 5.5: "disabled" wird abgelehnt; niedrigste Stufe ist {"type": "between_tools"}.
  • Opus 5.5 / Fable 5.1: Thinking lässt sich nicht abschalten.
Quelle: https://platform.claude.com/docs/en/build-with-claude/thinking
"""


def thinking_off(model: str) -> dict:
    """Zusätzliche messages.create-Argumente, um Thinking (wo möglich) abzuschalten."""
    m = (model or "").lower()
    if "haiku-5" in m:
        return {"thinking": {"type": "disabled"}}
    if "sonnet-5-5" in m:
        return {"thinking": {"type": "between_tools"}}
    return {}


def max_tokens_for(model: str, base: int) -> int:
    """Bei Modellen, deren Thinking sich nicht abschalten lässt, Luft für Denk-Tokens lassen
    (abgerechnet werden nur tatsächlich erzeugte Tokens)."""
    m = (model or "").lower()
    if ("opus-5" in m or "fable" in m or "mythos" in m) and not thinking_off(model):
        return base + 4000
    return base


def text_of(resp) -> str:
    """Alle Text-Blöcke einer Antwort zusammenfügen (Thinking-Blöcke überspringen)."""
    return "".join(getattr(b, "text", "") or "" for b in (resp.content or [])
                   if getattr(b, "type", "text") == "text")
