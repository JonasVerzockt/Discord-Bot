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
utils/text_chunks.py – Hilfsfunktionen, um lange Ausgaben Discord-tauglich in
mehrere Nachrichten (<= 2000 Zeichen) aufzuteilen, statt sie still abzuschneiden
oder mit HTTP 400 ('content > 2000') abzustürzen.
"""


def chunk_lines(text: str, max_len: int = 2000) -> list[str]:
    """
    Teilt ``text`` an Zeilenumbrüchen in Stücke von höchstens ``max_len`` Zeichen.
    Einzelne Zeilen, die länger als ``max_len`` sind, werden hart nachgeteilt,
    damit garantiert kein Stück das Limit überschreitet.
    """
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        # Überlange Einzelzeile hart zerlegen (verhindert Rest > max_len).
        while len(line) > max_len:
            if cur:
                out.append(cur.rstrip("\n"))
                cur = ""
            out.append(line[:max_len])
            line = line[max_len:]
        if cur and len(cur) + len(line) + 1 > max_len:
            out.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out or [text]


async def send_chunked(ctx, text: str, *, ephemeral: bool = True, max_len: int = 2000) -> None:
    """
    Sendet ``text`` als eine oder mehrere Nachrichten über ``ctx``. Die erste geht
    über ``ctx.respond`` (funktioniert auch nach ``ctx.defer``), weitere über
    ``ctx.followup.send``. So bleibt jede Nachricht unter dem Discord-Limit.
    """
    parts = chunk_lines(text, max_len)
    await ctx.respond(parts[0], ephemeral=ephemeral)
    for part in parts[1:]:
        await ctx.followup.send(part, ephemeral=ephemeral)
