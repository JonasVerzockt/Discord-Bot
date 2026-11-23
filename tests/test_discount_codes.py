# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rabattcode-Auswertung (utils/discount_parser): Plausibilitätsprüfung der KI-Antwort und
feste Datumsregeln. Der KI-Aufruf ist durch eine feste Antwort ersetzt – es wird nur die
Logik getestet, die erfundene/falsche Werte aussortiert.
"""
import json
from datetime import date
from types import SimpleNamespace

import pytest

REF = date(2026, 11, 23)


def _item(**kw):
    d = {"shop": "Antstore", "shop_url": "", "code": "ANTS10", "discount": "10%",
         "valid_from": None, "valid_until": None, "permanent": False, "min_order": None}
    d.update(kw)
    return d


@pytest.mark.parametrize("text, expected", [
    ("Code gilt vom 1.12. bis 24.12.", ("2026-12-01", "2026-12-24")),
    ("Aktion vom 28.12.2026 bis 3.1.2027", ("2026-12-28", "2027-01-03")),
    ("Aktion vom 28.12. bis 3.1.", ("2026-12-28", "2027-01-03")),       # Jahreswechsel
    ("Rabatt gültig bis Ende Dezember", (None, "2026-12-31")),
    ("bis Ende Februar", (None, "2027-02-28")),                         # Monat liegt im nächsten Jahr
    ("einfach so", (None, None)),
])
def test_rule_dates(text, expected):
    from utils.discount_parser import _rule_dates
    assert _rule_dates(text, REF) == expected


def test_order_deadline_is_not_validity():
    from utils.discount_parser import _only_order_deadline
    assert _only_order_deadline("Bestellungen bis 20.12. werden noch vor Weihnachten versendet", "2026-12-20")
    assert not _only_order_deadline("Code gültig bis 20.12.", "2026-12-20")


def test_validate_rejects_invented_values():
    from utils.discount_parser import _validate
    text = "Mit ANTS10 gibt es 10% auf alles bei Antstore"
    assert _validate(_item(), text, False, REF)["discount"] == "10%"
    assert _validate(_item(code="ERFUNDEN"), text, False, REF) is None          # Code steht nicht im Text
    assert _validate(_item(code="🇩🇪"), text, False, REF) is None              # nur Emoji
    assert _validate(_item(discount="20%"), text, False, REF)["discount"] == ""  # Rabatt erfunden
    assert _validate(_item(permanent=True), text, False, REF)["permanent"] is False
    assert _validate(_item(shop="unbekannt"), text, False, REF)["shop"] == ""
    note = "Schreib in die Kundenanmerkung ANTS10 dann gibt es ein Geschenk"
    assert _validate(_item(), note, False, REF) is None                         # nur Kundenanmerkung


def test_validate_with_image_skips_text_checks():
    from utils.discount_parser import _validate
    assert _validate(_item(code="BILDCODE"), "siehe Bild", True, REF)["code"] == "BILDCODE"


def test_parse_codes_end_to_end(monkeypatch):
    import utils.discount_parser as P
    answer = {"codes": [
        {"shop": "Antstore", "code": "ANTS10", "discount": "10%", "valid_until": "2026-12-24"},
        {"shop": "?", "code": "ERFUNDEN", "discount": "50%"},
        {"shop": "AntMeUp", "code": "", "discount": "5%"},
    ]}
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="```json\n" + json.dumps(answer) + "\n```")])))
    monkeypatch.setattr(P, "_ai", fake)
    out = P.parse_codes("Mit ANTS10 gibt es 10% bei Antstore, gültig vom 1.12. bis 24.12.", "2026-11-23")
    assert [c["code"] for c in out] == ["ANTS10"]
    assert out[0]["valid_from"] == "2026-12-01" and out[0]["valid_until"] == "2026-12-24"
