#!/usr/bin/env bash
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

# update.sh – Auto-Deploy für den AAM Discord Bot
# Prüft origin/main auf neue Commits, zieht sie und startet den Bot neu.
set -euo pipefail

REPO_DIR="/opt/discord-bot"
BRANCH="main"
SERVICE="aam-bot"
VENV="$REPO_DIR/.venv"

cd "$REPO_DIR"

git fetch origin "$BRANCH" --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0   # Nichts zu tun
fi

echo "Update gefunden: $LOCAL -> $REMOTE"

# Merken ob sich requirements.txt ändert
REQ_CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" -- requirements.txt || true)

# Lokale Änderungen niemals überschreiben – dann lieber abbrechen
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "FEHLER: Lokale, uncommittete Änderungen in $REPO_DIR – Deploy abgebrochen." >&2
    exit 1
fi

git merge --ff-only "origin/$BRANCH"

if [ -n "$REQ_CHANGED" ]; then
    echo "requirements.txt geändert – installiere Abhängigkeiten..."
    "$VENV/bin/pip" install --quiet -r requirements.txt
fi

echo "Starte $SERVICE neu..."
sudo /usr/bin/systemctl restart "$SERVICE"
echo "Deploy abgeschlossen: $(git rev-parse --short HEAD)"
