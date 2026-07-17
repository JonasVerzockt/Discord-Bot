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
utils/embeds.py – Einheitliche Embed-Ausgaben.

Zentraler Stil (eine Standard-Farbe) + automatisches Aufteilen langer Texte auf
mehrere Embeds (Beschreibung max. 4096 Zeichen). Genutzt für die inhaltlichen
Ausgaben (Listen/Reports); kurze Bestätigungen/Fehler bleiben bewusst Plain-Text.
"""
import discord

from utils.text_chunks import chunk_lines, chunk_paragraphs

# Einheitliche Farben: Türkis für normale (User-)Ausgaben, Weinrot für Admin/Mod.
EMBED_COLOR = discord.Colour(0x00BFA5)   # Türkis – Standard/User
ADMIN_COLOR = discord.Colour(0x800020)   # Weinrot/Bordeaux – Admin/Mod

_ZWSP = "​"  # Zero-Width-Space: verhindert leere Embed-Beschreibung.


def build_embeds(text: str, *, title: str | None = None,
                 color: discord.Color | None = None, max_len: int = 4000) -> list[discord.Embed]:
    """Zerlegt Text in eine Liste von Embeds (jeweils <= max_len Zeichen)."""
    color = color or EMBED_COLOR
    parts = chunk_paragraphs(text if (text and text.strip()) else _ZWSP, max_len)
    embeds: list[discord.Embed] = []
    for i, chunk in enumerate(parts):
        e = discord.Embed(description=chunk, color=color)
        if i == 0 and title:
            e.title = title
        embeds.append(e)
    return embeds


async def send_embeds(ctx, text: str, *, title: str | None = None, ephemeral: bool = False,
                      color: discord.Color | None = None, max_len: int = 4000) -> None:
    """Sendet Text als eine/mehrere Embeds über eine Interaktion (Slash-Befehl).
    Erste Nachricht via ctx.respond (auch nach ctx.defer), weitere via followup."""
    embeds = build_embeds(text, title=title, color=color, max_len=max_len)
    await ctx.respond(embed=embeds[0], ephemeral=ephemeral)
    for e in embeds[1:]:
        await ctx.followup.send(embed=e, ephemeral=ephemeral)


async def send_embeds_to(dest, text: str, *, title: str | None = None,
                         color: discord.Color | None = None, max_len: int = 4000) -> None:
    """Sendet Text als eine/mehrere Embeds an ein Ziel mit .send (User-DM oder Kanal)."""
    for e in build_embeds(text, title=title, color=color, max_len=max_len):
        await dest.send(embed=e)
