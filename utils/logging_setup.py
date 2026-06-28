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
utils/logging_setup.py – Zentrales Logging mit rotierenden Logdateien.

Ersetzt alle print()-Aufrufe im Bot schrittweise.
Logdateien: bot_log_YYYYMMDD.log (max 1 MB, 5 Backups).

Verwendung:
    from utils.logging_setup import setup_logging
    setup_logging()   # einmalig in main.py aufrufen

    import logging
    logging.info("Bot gestartet")
    logging.warning("Warnung!")
    logging.error("Fehler!")
"""
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from config import BASE_DIR


def setup_logging(level: int = logging.INFO) -> None:
    """Konfiguriert Root-Logger mit Datei- und Console-Handler."""
    log_file = BASE_DIR / f"bot_log_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger()
    logger.setLevel(level)

    # Verhindert doppelte Handler bei Hot-Reload
    if logger.handlers:
        logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Datei (rotierend) ─────────────────────────────────────────────────────
    fh = RotatingFileHandler(
        log_file, maxBytes=1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # ── Konsole ───────────────────────────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # discord.py-interne Logs nur auf WARNING reduzieren
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
