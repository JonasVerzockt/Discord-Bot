# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/command_tree_snapshot.py – lädt alle Cogs offline (ohne Discord-Login) und
schreibt den kompletten Befehlsbaum als JSON: Slash-Befehle inkl. Gruppen,
Optionen, Choices, Beschreibungen/Übersetzungen sowie Listener und Task-Loops je Cog.

  python tests/command_tree_snapshot.py <ausgabe.json>

Vergleich vorher/nachher per diff zeigt, ob ein Umbau die Discord-Oberfläche verändert.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("BOARD_SNAPSHOT_ROOT") or Path(__file__).resolve().parent.parent).resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_snapshot as BS  # noqa: E402  (Env + Sheet-Ersatz wiederverwenden)


def _opt(o) -> dict:
    return {
        "name": o.name, "type": str(o.input_type), "required": o.required,
        "description": o.description,
        "desc_l10n": o.description_localizations or None,
        "choices": [(c.name, c.value) for c in (o.choices or [])],
        "autocomplete": bool(o.autocomplete),
        "min": o.min_value, "max": o.max_value,
    }


def _cmd(c) -> dict:
    import discord
    d = {"name": c.name, "description": getattr(c, "description", ""),
         "desc_l10n": getattr(c, "description_localizations", None) or None,
         "class": type(c).__name__}
    if isinstance(c, discord.SlashCommandGroup):
        d["subcommands"] = sorted((_cmd(s) for s in c.subcommands), key=lambda x: x["name"])
    elif isinstance(c, discord.SlashCommand):
        d["options"] = [_opt(o) for o in c.options]
        d["cog"] = type(c.cog).__name__ if c.cog else None
    return d


def main(out: Path) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cmdtree-"))
    BS._setup_env(tmp)
    import gspread
    gspread.service_account = lambda *a, **k: BS._OfflineSheets()
    import re
    import discord
    from discord.ext import tasks
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    block = src[src.index("INITIAL_COGS = ["):]
    block = block[:block.index("]")]
    cogs = re.findall(r'"(cogs\.[a-z_]+)"', block)
    bot = discord.Bot(intents=discord.Intents.default())
    failed = []
    for c in cogs:
        try:
            bot.load_extension(c)
        except Exception as e:
            failed.append(f"{c}: {e!r}"[:200])
    tree = sorted((_cmd(c) for c in bot.pending_application_commands), key=lambda x: x["name"])
    cog_info = {}
    for name, cog in sorted(bot.cogs.items()):
        loops = sorted(a for a in dir(cog) if isinstance(getattr(cog, a, None), tasks.Loop))
        listeners = sorted(f"{ev}:{fn.__name__}" for ev, fn in cog.get_listeners())
        cog_info[name] = {"loops": loops, "listeners": listeners}
    data = {"loaded": len(cogs) - len(failed), "total": len(cogs), "failed": failed,
            "commands": tree, "cogs": cog_info}
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True, default=str),
                   encoding="utf-8")
    print(f"Cogs {data['loaded']}/{data['total']}, Befehle {len(tree)}, Fehler {len(failed)} → {out}")
    for f in failed:
        print("  FEHLER", f)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
