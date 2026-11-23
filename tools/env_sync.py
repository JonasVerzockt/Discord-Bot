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
tools/env_sync.py – bringt die eigene .env in Aufbau und Reihenfolge von .env.example,
mit den EIGENEN Werten. Läuft lokal auf dem Server; Werte werden nie ausgegeben.

  python3 tools/env_sync.py              # schreibt .env.new (zum Ansehen/Vergleichen)
  python3 tools/env_sync.py --write      # ersetzt .env (vorher Backup .env.bak-<Zeit>)

Regeln:
  • Abschnitte, Kommentare und Reihenfolge kommen aus .env.example.
  • Ist eine Variable in der .env gesetzt, wird sie AKTIV mit ihrem Wert (zeichengenau)
    übernommen – auch wenn sie in .env.example nur auskommentiert steht.
  • Ist sie NICHT gesetzt, bleibt die Zeile aus der Vorlage auskommentiert – auch wenn sie
    dort aktiv ist. So ändert sich an der wirksamen Konfiguration nichts.
  • Variablen aus der .env, die die Vorlage nicht kennt, landen am Ende in einem
    Prüf-Abschnitt (nichts geht verloren).
  • Zum Schluss wird geprüft, dass alte und neue Datei exakt dieselben wirksamen Werte
    ergeben – sonst wird nichts geschrieben.

Auf dem Bildschirm erscheinen nur Variablennamen, nie Werte.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ACTIVE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_TEMPLATE = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")


def parse_active(text: str) -> tuple[dict[str, str], list[str]]:
    """Wirksame Werte (letzter gewinnt) + Liste doppelt gesetzter Namen."""
    values, seen, dups = {}, set(), []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ACTIVE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key in seen and key not in dups:
            dups.append(key)
        seen.add(key)
        values[key] = raw
    return values, dups


def build(example: str, env: str) -> tuple[str, dict]:
    values, dups = parse_active(env)
    out, filled, template_only = [], set(), []
    for line in example.splitlines():
        m = _TEMPLATE.match(line)
        if not m:
            out.append(line)
            continue
        commented, key = bool(m.group(1)), m.group(2)
        if key in values and key not in filled:
            out.append(f"{key}={values[key]}")
            filled.add(key)
        elif key in filled:
            out.append(line if commented else f"# {line}")        # Dublette in der Vorlage
        else:
            out.append(line if commented else f"# {line}")        # nicht gesetzt → bleibt aus
            if key not in template_only:
                template_only.append(key)
    extra = [k for k in values if k not in filled]
    if extra:
        out += ["", "# ── Nicht in .env.example – bitte prüfen (übernommen, damit nichts verloren geht) ──"]
        out += [f"{k}={values[k]}" for k in extra]
    text = "\n".join(out).rstrip("\n") + "\n"
    suspicious = sorted(k for k, v in values.items() if re.search(r"\s#", v) and not v.strip().startswith(("'", '"')))
    report = {"übernommen": len(filled), "nur_vorlage": template_only, "unbekannt": extra,
              "doppelt": dups, "inline_kommentar_verdacht": suspicious}
    return text, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--example", default=str(ROOT / ".env.example"))
    ap.add_argument("--out", default=None, help="Zieldatei (Standard: .env.new)")
    ap.add_argument("--write", action="store_true", help=".env ersetzen (mit Backup)")
    a = ap.parse_args(argv)
    env_p, ex_p = Path(a.env), Path(a.example)
    env_text, ex_text = env_p.read_text(encoding="utf-8"), ex_p.read_text(encoding="utf-8")
    new_text, rep = build(ex_text, env_text)

    # Sicherheitsprüfung: wirksame Werte müssen identisch sein
    old_vals, _ = parse_active(env_text)
    new_vals, _ = parse_active(new_text)
    if old_vals != new_vals:
        diff = sorted(set(old_vals) ^ set(new_vals) | {k for k in old_vals if old_vals.get(k) != new_vals.get(k)})
        print("ABBRUCH: wirksame Werte würden sich ändern bei:", ", ".join(diff))
        return 1

    target = env_p if a.write else Path(a.out or str(env_p) + ".new")
    if a.write:
        backup = env_p.with_name(f"{env_p.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_text(env_text, encoding="utf-8")
        os.chmod(backup, 0o600)
        print(f"Backup: {backup.name}")
    target.write_text(new_text, encoding="utf-8")
    os.chmod(target, 0o600)

    print(f"Geschrieben: {target.name}  (Werte übernommen: {rep['übernommen']}, wirksame Konfiguration unverändert)")
    if rep["unbekannt"]:
        print("Nicht in .env.example (ans Ende übernommen, bitte prüfen):", ", ".join(rep["unbekannt"]))
    if rep["doppelt"]:
        print("Mehrfach gesetzt (es gilt der letzte Wert):", ", ".join(rep["doppelt"]))
    if rep["inline_kommentar_verdacht"]:
        print("ACHTUNG – Wert enthält ' #' (Kommentar hinter dem Wert? systemd liest ihn MIT):",
              ", ".join(rep["inline_kommentar_verdacht"]))
    print(f"Nur in der Vorlage, bei dir nicht gesetzt (Standardwert gilt): {len(rep['nur_vorlage'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
