# SPDX-License-Identifier: AGPL-3.0-or-later
"""
utils/web_keys.py – typisierte Schlüssel der aiohttp-Anwendung (web.AppKey).

aiohttp empfiehlt AppKey-Objekte statt Text-Schlüsseln wie app["bot"]
(sonst NotAppKeyWarning). Liegt bewusst in utils/ statt web/, damit auch Module
außerhalb des Web-Pakets ihn ohne Zirkelimport nutzen können.
"""
from aiohttp import web

# Der laufende discord.Bot – gesetzt in web.build_app(), gelesen von allen Handlern.
BOT_KEY: web.AppKey = web.AppKey("bot", object)
