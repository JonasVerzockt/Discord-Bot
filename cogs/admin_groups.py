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
cogs/admin_groups.py – fasst Admin-Befehle zu Gruppen zusammen.

Wird als LETZTES Cog geladen. Die eigentlichen Befehle bleiben in ihren Cogs
(Code, Optionen, Checks unverändert); hier werden sie nur aus der
Top-Level-Liste genommen und als Unterbefehl in eine Admin-Gruppe gehängt:

  /bot      status | system | stats | nutzer | protokoll | export | angebote
  /review   offen | test | neu_verarbeiten
  /codes_admin  setzen | datum | bearbeiten
  /info_admin   anlegen | bearbeiten | löschen | quelltext
  /event_admin  offen | freigeben | ablehnen | absagen | bearbeiten   (nur MAP_ENABLED)
  /wartung aktion:<jobs | shops | karte | abgleich | codes>

Alle Gruppen: default_member_permissions = Nachrichten verwalten (für andere
unsichtbar) + admin_or_manage_messages()-Check auf Gruppenebene.
Fehlt ein Quellbefehl (z.B. Karte deaktiviert), wird er übersprungen.
"""
import logging

import discord
from discord.ext import commands

from cogs.server_settings import admin_or_manage_messages

logger = logging.getLogger(__name__)

LOCK = "🔒 [Admin] "
ADMIN_PERMS = discord.Permissions(manage_messages=True)

# Gruppe → (Beschreibung EN, Beschreibung DE, [(alter Name, neuer Unterbefehl), …])
GROUPS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "bot": (
        "Bot status, statistics, users, logs and export",
        "Bot-Status, Statistiken, Nutzer, Protokolle und Export",
        [("status", "status"), ("system", "system"), ("stats", "stats"),
         ("known_users", "nutzer"), ("command_log", "protokoll"),
         ("export", "export"), ("offer_alert check", "angebote")],
    ),
    "review": (
        "Review bot: pending messages, parser test, reprocess",
        "Review-Bot: offene Nachrichten, Parser-Test, neu verarbeiten",
        [("pending", "offen"), ("test", "test"), ("reprocess", "neu_verarbeiten")],
    ),
    "codes_admin": (
        "Manage discount codes",
        "Rabattcodes verwalten",
        [("codes_set", "setzen"), ("codes_date", "datum"), ("codes_edit", "bearbeiten")],
    ),
    "info_admin": (
        "Manage info entries",
        "Info-Einträge verwalten",
        [("info_add", "anlegen"), ("info_edit", "bearbeiten"),
         ("info_remove", "löschen"), ("info_raw", "quelltext")],
    ),
    "event_admin": (
        "Manage map events",
        "Karten-Events verwalten",
        [("event_pending", "offen"), ("event_approve", "freigeben"),
         ("event_reject", "ablehnen"), ("event_exclude", "absagen"),
         ("event_edit", "bearbeiten")],
    ),
}

# /wartung aktion:<wert> → alter Befehl, Anzeigename EN, Anzeigename DE
MAINTENANCE: dict[str, tuple[str, str, str]] = {
    "jobs":     ("run_jobs",     "Background jobs",      "Hintergrund-Jobs"),
    "shops":    ("reloadshops",  "Shop data",            "Shop-Daten"),
    "karte":    ("map_refresh",  "Map",                  "Karte"),
    "abgleich": ("rescan",       "Re-reconcile reviews", "Bewertungen abgleichen"),
    "codes":    ("codes_rescan", "Scan discount codes",  "Rabattcodes scannen"),
    "schwarmflug": ("swarm_rescan", "Read swarm flight channel", "Schwarmflug-Kanal einlesen"),
    "prognose": ("swarm_forecast", "Swarm flight forecast", "Schwarmflug-Prognose"),
    "saisonstart": ("swarm_season_reset", "Swarm season start", "Schwarmflug-Saisonstart"),
}


def _strip(text: str | None) -> str:
    """Entfernt alte Präfixe ('🔒 [Admin] ', '(Admin) ')."""
    t = (text or "").strip()
    for p in ("🔒 [Admin]", "(Admin)", "🔒"):
        if t.startswith(p):
            t = t[len(p):].strip()
    return t


def _find(bot: discord.Bot, qualified: str):
    """Sucht einen Befehl (auch 'gruppe unterbefehl') in den ausstehenden Befehlen."""
    parts = qualified.split()
    for cmd in bot.pending_application_commands:
        if not isinstance(cmd, (discord.SlashCommand, discord.SlashCommandGroup)):
            continue
        if cmd.name != parts[0]:
            continue
        if len(parts) == 1:
            return cmd, None
        for sub in getattr(cmd, "subcommands", []):
            if sub.name == parts[1]:
                return sub, cmd
    return None, None


def _detach(bot: discord.Bot, cmd, parent) -> None:
    """Nimmt einen Befehl aus seiner bisherigen Stelle heraus."""
    if parent is None:
        bot.remove_application_command(cmd)
    else:
        parent.subcommands.remove(cmd)


def _move(bot: discord.Bot, group: discord.SlashCommandGroup, cmd, new_name: str) -> None:
    """Hängt einen bestehenden SlashCommand als Unterbefehl in group."""
    de_desc = ""
    if isinstance(cmd.description_localizations, dict):
        de_desc = cmd.description_localizations.get("de", "")
    en = _strip(cmd.description)
    de = _strip(de_desc) or en
    cmd.name = new_name
    cmd.name_localizations = discord.MISSING
    cmd.description = (LOCK + en)[:100]
    cmd.description_localizations = {"de": (LOCK + de)[:100]}
    cmd.parent = group
    cmd.default_member_permissions = None   # gilt bei Discord nur auf oberster Ebene
    cmd.nsfw = None
    group.add_command(cmd)


class MaintenanceCog(commands.Cog):
    """/wartung aktion:<…> – führt einen der früheren Einzel-Wartungsbefehle aus."""

    def __init__(self, bot: discord.Bot, targets: dict):
        self.bot = bot
        self.targets = targets   # wert → SlashCommand

    @discord.slash_command(
        name="wartung",
        description="🔒 [Admin] Maintenance: jobs, shop data, map, reviews, codes, swarm flights",
        description_localizations={"de": "🔒 [Admin] Wartung: Jobs, Shop-Daten, Karte, Bewertungen, Rabattcodes, Schwarmflug"},
    )
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    async def wartung(
        self,
        ctx: discord.ApplicationContext,
        aktion: discord.Option(
            str,
            "What should be run?",
            description_localizations={"de": "Was soll ausgeführt werden?"},
            choices=[
                discord.OptionChoice(en, value, name_localizations={"de": de})
                for value, (_, en, de) in MAINTENANCE.items()
            ],
        ),
    ):
        target = self.targets.get(aktion)
        if target is None:
            await ctx.respond("❌ Diese Aktion ist auf diesem Bot nicht aktiv.", ephemeral=True)
            return
        # Das Ziel bringt seine eigene Kanal-Abfrage (allowed_channel-Wrapper) mit;
        # /wartung selbst fragt deshalb nicht noch einmal nach.
        await target(ctx)


def setup(bot: discord.Bot):
    moved = 0
    for gname, (en, de, items) in GROUPS.items():
        found = []
        for old, new in items:
            cmd, parent = _find(bot, old)
            if cmd is None:
                logger.info(f"🔒 admin_groups: /{old} nicht vorhanden – übersprungen")
                continue
            found.append((cmd, parent, new))
        if not found:
            continue
        group = bot.create_group(
            gname,
            (LOCK + en)[:100],
            description_localizations={"de": (LOCK + de)[:100]},
            default_member_permissions=ADMIN_PERMS,
            checks=[admin_or_manage_messages().predicate],
        )
        for cmd, parent, new in found:
            _detach(bot, cmd, parent)
            _move(bot, group, cmd, new)
            moved += 1

    targets = {}
    for value, (old, _, _) in MAINTENANCE.items():
        cmd, parent = _find(bot, old)
        if cmd is None:
            logger.info(f"🔒 admin_groups: /{old} nicht vorhanden – Wartungsaktion '{value}' inaktiv")
            continue
        _detach(bot, cmd, parent)
        targets[value] = cmd
    bot.add_cog(MaintenanceCog(bot, targets))
    # Nur Auswahlpunkte anbieten, deren Befehl es auf diesem Bot gibt (z.B. Karte aus).
    wartung, _ = _find(bot, "wartung")
    if wartung is not None:
        opt = wartung.options[0]
        opt.choices = [c for c in opt.choices if c.value in targets]
    logger.info(f"🔒 admin_groups: {moved} Befehle gruppiert, {len(targets)} Wartungsaktionen")
