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
web/features/chat.py – KI-Chat im Board (schwebendes Fenster, nur eingeloggte AAM-Mitglieder).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import asyncio
import hmac
import secrets
from aiohttp import web
from datetime import datetime, timezone
from utils.board_i18n import pick_lang, translate
from config import BOARD_AI_CHAT_PER_HOUR, BOARD_AI_CHAT_PER_MIN

from web.core.security import _chat_uid, _member_csrf, _rate

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


_WEB_CHAT_CHANNEL = -1          # Kennung für Web-Verläufe in ai_chat_history


def _chat_models(bot, uid: str, lang: str) -> tuple[list, str]:
    import config as cfg
    from cogs.ai_chat import AI_MODELS, _resolve_model_meta
    from utils.ai_chat import get_user_model
    from utils.localization import l10n
    cog = bot.cogs.get("AiChatCog")
    avail = getattr(cog, "_available_models", None)
    shown = [m for m in AI_MODELS if not avail or m["id"] in avail] or AI_MODELS
    pre = get_user_model(int(uid)) or cfg.AI_CHAT_MODEL
    pre = _resolve_model_meta(pre)["id"]
    if pre not in {m["id"] for m in shown}:
        pre = shown[0]["id"]
    out = [{"id": m["id"], "label": m["label"], "emoji": m["emoji"],
            "tier": l10n.get(m["tier_key"], lang), "desc": l10n.get(m["desc_key"], lang),
            "rec": m["id"] == cfg.AI_CHAT_RECOMMENDED_MODEL} for m in shown]
    return out, pre


async def h_chat_models(req):
    uid = _chat_uid(req)
    if not uid:
        return web.json_response({"error": "login"}, status=403)
    models, pre = await asyncio.to_thread(_chat_models, req.app["bot"], uid, pick_lang(req))
    return web.json_response({"models": models, "preselect": pre},
                             headers={"Cache-Control": "private, no-store"})


def _owned_conv(uid: str, conv) -> int | None:
    """Nur eigene, nicht abgelaufene Web-Verläufe fortsetzen."""
    from utils.ai_chat import _db
    try:
        cid = int(conv)
    except (TypeError, ValueError):
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        row = con.execute("SELECT 1 FROM ai_chat_history WHERE message_id=? AND user_id=? "
                          "AND channel_id=? AND expires_at > ?",
                          (cid, int(uid), _WEB_CHAT_CHANNEL, now)).fetchone()
    return cid if row else None


async def h_chat_send(req):
    import config as cfg
    from utils.ai_chat import chat, save_conversation, set_user_model
    from utils.localization import l10n
    lang = pick_lang(req)
    uid = _chat_uid(req)
    if not uid:
        return web.json_response({"answer": translate(lang, "chat_login")}, status=403)
    if not hmac.compare_digest(req.headers.get("X-CSRF", ""), _member_csrf(uid)):
        return web.json_response({"answer": translate(lang, "chat_error")}, status=403)
    if not (_rate("aichat-m:" + uid, BOARD_AI_CHAT_PER_MIN, 60)
            and _rate("aichat-h:" + uid, BOARD_AI_CHAT_PER_HOUR, 3600)):
        return web.json_response({"answer": translate(lang, "chat_rate")}, status=429)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"answer": translate(lang, "chat_error")}, status=400)
    msg = str(body.get("msg") or "").strip()
    if not msg:
        return web.json_response({"answer": l10n.get("ai_err_empty", lang)})
    if len(msg) > cfg.AI_CHAT_MAX_INPUT_CHARS:
        return web.json_response({"answer": l10n.get(
            "ai_msg_too_long", lang, length=f"{len(msg):,}", max=f"{cfg.AI_CHAT_MAX_INPUT_CHARS:,}")})
    models, pre = await asyncio.to_thread(_chat_models, req.app["bot"], uid, lang)
    prev = await asyncio.to_thread(_owned_conv, uid, body.get("conv"))
    model = body.get("model") if body.get("model") in {m["id"] for m in models} else pre
    if prev:                                       # Fortsetzung: Modell des Verlaufs
        from utils.ai_chat import load_conversation_model
        model = load_conversation_model(prev) or model
    try:
        set_user_model(int(uid), model)
    except Exception:
        pass
    result = await chat(user_id=int(uid), user_message=msg, prev_bot_message_id=prev,
                        channel_id=_WEB_CHAT_CHANNEL, user_lang=lang, model=model)
    used = result.get("model") or model
    out = {"answer": result.get("answer") or translate(lang, "chat_error"), "conv": None}
    if result.get("budget_exceeded"):
        from cogs.ai_chat import _model_price_overview
        out["answer"] += "\n\n" + _model_price_overview(lang) + "\n\n" + l10n.get("ai_budget_retry_hint", lang)
    else:
        from cogs.ai_chat import _model_footer
        out["footer"] = _model_footer(used, lang) + (f" · 💰 ${result['cost']:.5f}" if result.get("cost") else "")
    if result.get("ok") and result.get("history"):
        new_id = -secrets.randbits(52) - 1        # negativ: kollidiert nie mit Discord-IDs
        await asyncio.to_thread(save_conversation, new_id, int(uid), _WEB_CHAT_CHANNEL,
                                result["history"], used)
        out["conv"] = str(new_id)
    logger.info("🤖 Web-KI-Chat: Nutzer %s, Modell %s, $%.5f", uid, used, result.get("cost") or 0)
    return web.json_response(out, headers={"Cache-Control": "private, no-store"})
