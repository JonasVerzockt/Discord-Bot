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
utils/paths.py – schlanke Pfad-/Migrationshilfen (keine Fremd-/Bot-Importe).

Zweck: Beim Wechsel des Datenlayouts (alle Laufzeitdaten in data/) werden
vorhandene Dateien aus ihrem alten Ort automatisch an den neuen verschoben –
einmalig, idempotent und ohne Datenverlust (nur verschieben, wenn Ziel fehlt).
Für SQLite-Dateien werden die -shm/-wal-Begleitdateien mitgezogen.
"""
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_SIDECARS = ("-shm", "-wal", "-journal")


def migrate_legacy_files(mapping: dict) -> list[str]:
    """
    Verschiebt Dateien gemäß {alter_pfad: neuer_pfad}. Überspringt Einträge, bei
    denen alt == neu, die Quelle fehlt oder das Ziel bereits existiert. Für .db-
    Dateien werden -shm/-wal/-journal mitverschoben. Rückgabe: Liste verschobener
    Zielnamen (für Logging).
    """
    moved: list[str] = []
    for old, new in mapping.items():
        old, new = Path(old), Path(new)
        try:
            if old.resolve() == new.resolve():
                continue
        except OSError:
            continue
        if not old.exists() or new.exists():
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(old), str(new))
            moved.append(new.name)
            if old.suffix == ".db":
                for sc in _DB_SIDECARS:
                    o2, n2 = Path(str(old) + sc), Path(str(new) + sc)
                    if o2.exists() and not n2.exists():
                        shutil.move(str(o2), str(n2))
        except Exception as e:
            logger.warning("⚠️ Daten-Migration '%s' → '%s' fehlgeschlagen: %s", old, new, e)
    if moved:
        logger.info("📁 Laufzeitdaten nach data/ migriert: %s", ", ".join(moved))
    return moved
