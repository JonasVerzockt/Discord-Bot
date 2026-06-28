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
utils/localization.py – Mehrsprachigkeitssystem für den AAM Discord Bot.

Unterstützte Sprachen: de (Deutsch), en (Englisch), eo (Esperanto).
Sprachpriorität: User-Einstellung → Server-Einstellung → 'en' (Fallback).

Verwendung:
    from utils.localization import l10n, get_user_lang
    lang = await get_user_lang(bot, user_id, server_id)
    text = l10n.get('notification_set', lang, species='Messor barbarus', regions='de')
"""
import logging
import json
from pathlib import Path
from config import LOCALES_DIR

logger = logging.getLogger(__name__)


class Localization:
    """Lädt alle Sprachdateien aus locales/ und stellt .get() bereit."""

    def __init__(self):
        self._langs: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        for f in Path(LOCALES_DIR).glob("*.json"):
            lang = f.stem
            try:
                with open(f, encoding="utf-8") as fh:
                    self._langs[lang] = json.load(fh)
                logger.debug(f"🔍 Sprache geladen: {lang}")
            except Exception as e:
                logger.error(f"❌ Fehler beim Laden von {f}: {e}")

    def get(self, key: str, lang: str = "en", **kwargs) -> str:
        """
        Gibt den übersetzten String zurück.
        Fällt bei fehlendem Key auf 'en' zurück, dann auf den Key selbst.
        """
        text = (
            self._langs.get(lang, {}).get(key)
            or self._langs.get("en", {}).get(key)
            or f"[{key}]"
        )
        try:
            return text.format(**kwargs)
        except KeyError as e:
            logger.error(f"❌ l10n: Fehlender Platzhalter {e} in Key '{key}'")
            return text


# Singleton
l10n = Localization()


async def get_user_lang(bot, user_id: int | str, server_id: int | str | None) -> str:
    """
    Ermittelt die Sprache für einen User.
    Reihenfolge: user_settings → server_settings → 'en'
    """
    from utils.db import execute_db

    try:
        user_id = int(user_id)
        rows = await execute_db(
            bot,
            "SELECT language FROM user_settings WHERE user_id=?",
            (user_id,),
            fetch=True,
        )
        if rows:
            return rows[0]["language"]

        if server_id is not None:
            server_id = int(server_id)
            rows = await execute_db(
                bot,
                "SELECT language FROM server_settings WHERE server_id=?",
                (server_id,),
                fetch=True,
            )
            if rows:
                return rows[0]["language"]
    except Exception as e:
        logger.error(f"❌ get_user_lang error (user={user_id}): {e}")

    return "en"
