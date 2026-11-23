# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/static_check.py – statische Prüfung des gesamten Bot-Codes mit pyflakes.

  python tests/static_check.py

Schlägt fehl bei: Syntaxfehlern, undefinierten Namen, unbenutzten Importen/Variablen u. Ä.

Bewusst ignoriert wird genau EIN bekannter Fehlalarm: py-cord-Slash-Optionen stehen als
Parameter-Annotation (``x: discord.Option(str, "Beschreibung", choices=[...])``). pyflakes hält
die Texte darin für Typ-Hinweise („forward annotation“) und meldet z. B. „undefined name 'de'“.
Meldungen INNERHALB solcher Parameter-Annotationen werden deshalb übergangen – alles andere zählt.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from pyflakes import api, messages, reporter as _rep

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["cogs", "utils", "web", "tools", "tests", "main.py", "grabber.py", "update.py", "config.py"]


def _annotation_spans(tree: ast.AST) -> list[tuple[int, int, int, int]]:
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    an = arg.annotation
                    spans.append((an.lineno, an.col_offset, an.end_lineno, an.end_col_offset))
    return spans


def _inside(spans, line: int, col: int) -> bool:
    for l1, c1, l2, c2 in spans:
        if (line, col) >= (l1, c1) and (line, col) <= (l2, c2):
            return True
    return False


class _Collect(_rep.Reporter):
    def __init__(self):
        self.items = []

    def unexpectedError(self, filename, msg):
        self.items.append((filename, 0, 0, f"FEHLER: {msg}", None))

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.items.append((filename, lineno, offset or 0, f"Syntaxfehler: {msg}", None))

    def flake(self, message):
        self.items.append((message.filename, message.lineno, message.col,
                           message.message % message.message_args, message))


def main() -> int:
    files = []
    for t in TARGETS:
        p = ROOT / t
        files += sorted(p.rglob("*.py")) if p.is_dir() else ([p] if p.exists() else [])
    problems = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        rep = _Collect()
        api.check(src, str(f.relative_to(ROOT)), rep)
        try:
            spans = _annotation_spans(ast.parse(src))
        except SyntaxError:
            spans = []
        for fn, line, col, text, msg in rep.items:
            if isinstance(msg, (messages.UndefinedName, messages.ForwardAnnotationSyntaxError)) \
                    and _inside(spans, line, col):
                continue
            problems.append(f"{fn}:{line}:{col + 1}: {text}")
    for p in problems:
        print(p)
    print(f"{len(files)} Dateien geprüft, {len(problems)} Befund(e).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
