# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Artnamen-Erkennung des Grabbers (grabber._canonical_species).

Die Erwartungen stammen aus der dokumentierten Logik (README, Abschnitt „Grabber“, und
Docstrings in grabber.py). Grundlage ist ein kleiner Test-Katalog im Format von
data/ant_species.json – unabhängig vom echten AntCat-Katalog.
"""
import json

import pytest

CATALOG = {
    "accepted": {k.lower(): k for k in [
        "Lasius niger", "Lasius flavus", "Lasius fuliginosus",
        "Monomorium chinense", "Monomorium chilense",
        "Tetramorium caespitum",
        "Camponotus decipiens", "Camponotus ligniperda", "Camponotus chilensis",
        "Camponotus fedtschenkoi", "Camponotus novaeboracensis", "Camponotus indianus",
        "Polyrhachis chinensis",
        "Ectomomyrmex astutus",
        "Pheidole megacephala", "Pheidole dolon",
        "Messor barbarus", "Messor structor",
        "Neoponera apicalis", "Linepithema humile",
        "Crematogaster torosa",
    ]},
    "synonyms": {"iridomyrmex humilis": "Linepithema humile",
                 "pachycondyla apicalis": "Neoponera apicalis"},
    "genera": {g.lower(): g for g in [
        "Lasius", "Monomorium", "Tetramorium", "Camponotus", "Polyrhachis", "Ectomomyrmex",
        "Pheidole", "Messor", "Neoponera", "Linepithema", "Crematogaster"]},
}


@pytest.fixture()
def grabber(tmp_path):
    import grabber as G
    f = tmp_path / "ant_species.json"
    f.write_text(json.dumps(CATALOG), encoding="utf-8")
    old = G._SPECIES_FILE
    G._SPECIES_FILE = str(f)
    G._SP_LOADED = False
    G._SP_EPITHETS.clear()
    yield G
    G._SPECIES_FILE = old
    G._SP_LOADED = False
    G._SP_EPITHETS.clear()


@pytest.mark.parametrize("raw, expected", [
    # Exakte Treffer, auch mit Beiwerk
    ("Lasius niger", "Lasius niger"),
    ("Lasius niger Kolonie mit Königin", "Lasius niger"),
    ("Messor barbarus queen + workers", "Messor barbarus"),
    # cf./aff. meinen eine konkrete Art -> normal auflösen
    ("Lasius cf. niger", "Lasius niger"),
    ("Lasius aff. flavus", "Lasius flavus"),
    # sp./spp. = Art unbestimmt -> nur Gattung, kein Epitheton raten
    ("Lasius sp.", "Lasius"),
    ("Crematogaster sp. (Tococa)", "Crematogaster"),
    # Echte Art in Klammern wird gefunden
    ("Lesser Red Carpenter Ant (Camponotus decipiens)", "Camponotus decipiens"),
    # Synonyme -> akzeptierter Name
    ("Iridomyrmex humilis", "Linepithema humile"),
    ("Pachycondyla apicalis", "Neoponera apicalis"),
    # Tippfehler im Epitheton (≤ 2 Schritte, eindeutig)
    ("Lasius nigar", "Lasius niger"),
    ("Lasius nigre", "Lasius niger"),
    # Tippfehler in der Gattung
    ("Tetramoirum caespitum", "Tetramorium caespitum"),
    # Commerce-Wörter sind kein Epitheton (kein Fuzzy auf „Pheidole dolon“)
    ("Pheidole colony", "Pheidole"),
    # Schutz vor Art-Verwechslung: „chinensis“ ist echtes Epitheton (andere Gattung)
    ("Camponotus chinensis", "Camponotus"),
    # Manuelle Overrides (erzwingen)
    ("Monomorium chiense", "Monomorium chinense"),
    ("Dendrolasius fuliginosus", "Lasius fuliginosus"),
    ("C. fedschenkoi", "Camponotus fedtschenkoi"),
    ("Camponotus nova", "Camponotus novaeboracensis"),
    # Manueller Override (blocken) -> bleibt bei der Gattung
    ("Camponotus andanus", "Camponotus"),
    # Nicht-Arten werden nicht zu Gattungen gebogen
    ("Formicarium", None),
    ("", None),
])
def test_canonical_species(grabber, raw, expected):
    assert grabber._canonical_species(raw) == expected


def test_without_catalog_returns_none(tmp_path):
    import grabber as G
    old = G._SPECIES_FILE
    try:
        G._SPECIES_FILE = str(tmp_path / "fehlt.json")
        G._SP_LOADED = False
        G._SP_ACCEPTED = {}
        assert G._canonical_species("Lasius niger") is None
    finally:
        G._SPECIES_FILE = old
        G._SP_LOADED = False


@pytest.mark.parametrize("a, b, dist", [
    ("niger", "niger", 0), ("nigar", "niger", 1), ("nigre", "niger", 1),
    ("chiense", "chinense", 1), ("abc", "xyz", 3),
])
def test_osa_distance(a, b, dist):
    import grabber as G
    assert G._osa(a, b) == dist


def test_ending_variant():
    import grabber as G
    assert G._is_ending_variant("niger", "nigra")
    assert not G._is_ending_variant("chinensis", "chilensis")


def test_strip_html():
    import grabber as G
    assert G._strip_html("<b>Lasius</b>&nbsp;niger <i>Kolonie</i>") == "Lasius niger Kolonie"
    assert G._strip_html(None) == ""
