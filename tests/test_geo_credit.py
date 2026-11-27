# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Quellenangabe der Karten-Umrisse (utils/map_geodata.py):
  • jedes Land mit Bereitsteller, Lizenz und Link; vereinfachte Daten als geändert markiert
  • nicht mehr pauschal „geoBoundaries (CC BY 4.0)“
  • Abgleich mit den geoBoundaries-Metadaten meldet geänderte Lizenzen (neuer Pin)
  • Karte und Schwarmflug-Seite zeigen die Angabe in allen Sprachen
"""
import asyncio
import json

import pytest

from utils import map_geodata as M


@pytest.mark.parametrize("lang,changed,osm", [("de", "vereinfacht", "OpenStreetMap-Mitwirkende"),
                                              ("en", "simplified", "OpenStreetMap contributors"),
                                              ("eo", "simpligita", "kontribuantoj de OpenStreetMap")])
def test_credit_names_every_country(lang, changed, osm):
    h = M.boundary_credit_html(lang)
    for part in ("DE © ", "AT © ", "CH © ", "LI © ", ">BKG<", ">BEV<", ">swisstopo<", f">{osm}<",
                 "https://www.govdata.de/dl-de/by-2-0", ">dl-de/by-2-0<", ">CC BY-SA 2.0<", ">ODbL<",
                 "https://www.openstreetmap.org/copyright", ">geoBoundaries<"):
        assert part in h, part
    assert h.count(changed) == 2                       # DE und AT: vereinfacht = geändert (Lizenzpflicht)
    assert "CC BY 4.0" not in h
    assert h.count("LI ©") == 1                         # zweite LI-Datei nur für den Lizenzabgleich


def test_credit_table_matches_downloaded_files():
    metas = {c["meta"] for c in M.BOUNDARY_CREDITS}
    for url, _dest in M.SOURCES:
        if "geoBoundaries-" in url:
            iso, adm = url.split("/gbOpen/")[1].split("/")[:2]
            assert f"{iso}/{adm}" in metas, f"{iso}/{adm} ohne Quellenangabe"


def test_check_licenses(tmp_path):
    def meta(key, lic):
        (tmp_path / (key.replace("/", "_") + ".json")).write_text(json.dumps({"boundaryLicense": lic}))
    assert M.check_licenses(tmp_path) == []                                  # keine Metadaten: nichts zu melden
    for c in M.BOUNDARY_CREDITS:
        meta(c["meta"], c["meta_license"])
    assert M.check_licenses(tmp_path) == []
    meta("AUT/ADM1", "Creative Commons Attribution 4.0 International")
    errs = M.check_licenses(tmp_path)
    assert len(errs) == 1 and errs[0].startswith("AUT/ADM1") and "Attribution 4.0" in errs[0]


def test_pages_show_credit():
    import board_snapshot as BS
    from aiohttp.test_utils import TestClient, TestServer
    from web import build_app

    async def run():
        bot = BS.FakeBot(asyncio.get_running_loop())
        from utils.db import init_db
        await init_db(bot)                              # nur Tabellen; Testdaten befüllt test_web
        out = {}
        async with TestClient(TestServer(build_app(bot))) as c:
            for path in ("/map", "/schwarmflug"):
                for lang in ("de", "en", "eo"):
                    out[(path, lang)] = await (await c.get(f"{path}?lang={lang}")).text()
        return out
    pages = asyncio.run(run())
    for (path, lang), html in pages.items():
        assert "https://www.govdata.de/dl-de/by-2-0" in html and ">swisstopo<" in html, (path, lang)
        assert "geoBoundaries</a> (CC BY 4.0)" not in html and "© geoBoundaries (CC BY 4.0)" not in html
        assert "{bounds}" not in html
