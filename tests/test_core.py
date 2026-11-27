# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit-Tests für Kernlogik ohne Discord/Web: Arten-Matching, Merch, Varianten, Nachrichten-
Aufteilung, Termine (RRULE), Übersetzungen, Währungen, Sicherheit."""
from datetime import datetime, timedelta, timezone

import pytest


# ── Arten-Matching (/sells, /notification) ─────────────────────────────────────
@pytest.mark.parametrize("field, query, hit", [
    ("Odontomachus cf. monticola (Schnappkiefer)", "Odontomachus monticola", True),
    ("Ant colony odontomachus monticola", "Odontomachus monticola", True),
    ("Ant colony Camponotus singularis", "Camponotus", True),
    ("Camponotus nicobarensis", "Camponotus", True),
    ("Camponotusx nicobarensis", "Camponotus", False),       # kein zufälliges Teilwort
    ("Lasius niger", "Lasius flavus", False),
    ("Lasius niger Poster", "Lasius niger", False),           # Merch wird ausgeschlossen
])
def test_matches_species_query(field, query, hit):
    from utils.availability import matches_species_query
    assert matches_species_query(field, query) is hit


@pytest.mark.parametrize("name, out", [
    ("Lasius cf. niger", "lasius niger"),
    ("Formica  aff.  rufa", "formica rufa"),
    ("Pheidole affinis", "pheidole affinis"),                 # „affinis“ bleibt ganz
])
def test_normalize_species_name(name, out):
    from utils.availability import normalize_species_name
    assert normalize_species_name(name) == out


def test_merch_product_detection():
    from utils.availability import is_merch_product
    assert is_merch_product({"species": "Myrmecocystus mexicanus", "title": "Poster – Myrmecocystus mexicanus – 70×50"})
    assert not is_merch_product({"species": "Lasius niger", "title": "Lasius niger Kolonie (Set)"})


def test_available_variants_only_in_stock_and_active():
    from utils.availability import available_variants
    p = {"variants": [{"title": "a", "in_stock": True, "is_active": True},
                      {"title": "b", "in_stock": False, "is_active": True},
                      {"title": "c", "in_stock": True, "is_active": False}]}
    assert [v["title"] for v in available_variants(p)] == ["a"]
    assert available_variants({}) == []


def test_split_messages_respect_discord_limit():
    from utils.availability import split_availability_messages
    entries = ["x" * 1500, "y" * 900, "q" * 4100, "kurz"]
    chunks = split_availability_messages(entries, 2000)
    assert all(len(c) <= 2000 for c in chunks)
    joined = "".join(chunks)
    assert joined.count("x") == 1500 and joined.count("q") == 4100 and "kurz" in joined


def test_chunk_paragraphs_limit():
    from utils.text_chunks import chunk_paragraphs
    text = "\n\n".join(["Absatz " + str(i) + " " + "w" * 300 for i in range(30)])
    chunks = chunk_paragraphs(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).count("w") == 30 * 300


# ── Termine der Karte (utils/map_events) ──────────────────────────────────────
class _Row(dict):
    def keys(self):
        return super().keys()


def test_rrule_roundtrip():
    from utils.map_events import _recur_code, _rrule_from
    d = datetime(2027, 3, 6, 18, 0)           # erster Samstag im März
    assert _rrule_from("weekly", d) == "FREQ=WEEKLY"
    assert _rrule_from("monthly_day", d) == "FREQ=MONTHLY;BYMONTHDAY=6"
    assert _rrule_from("monthly_weekday", d) == "FREQ=MONTHLY;BYDAY=1SA"
    assert _rrule_from("none", d) is None
    for kind in ("weekly", "monthly_day", "monthly_weekday", "yearly"):
        assert _recur_code(_rrule_from(kind, d)) == kind


def test_event_next_single_and_series():
    from utils.map_events import _event_next
    now = datetime.now(timezone.utc)
    future = _Row(start_at="2099-05-01T18:00", end_at="2099-05-01T20:00", all_day=0, rrule=None, exdates=None)
    assert _event_next(future).year == 2099
    past = _Row(start_at="2001-05-01T18:00", end_at="2001-05-01T20:00", all_day=0, rrule=None, exdates=None)
    assert _event_next(past) is None
    weekly = _Row(start_at="2020-01-06T19:00", end_at="2020-01-06T21:00", all_day=0,
                  rrule="FREQ=WEEKLY", exdates=None)
    nxt = _event_next(weekly)
    assert nxt is not None and nxt.weekday() == 0 and nxt > now - timedelta(hours=3)


def test_event_exdate_skips_occurrence():
    from utils.map_events import _event_next
    base = dict(start_at="2099-01-10T00:00", end_at=None, all_day=1, rrule="FREQ=MONTHLY;COUNT=3")
    first = _event_next(_Row(base, exdates=None))
    skipped = _event_next(_Row(base, exdates="2099-01-10T00:00"))
    assert first.month == 1 and skipped.month == 2


# ── Übersetzungen der Web-Seiten ───────────────────────────────────────────────
def test_translate_fallbacks():
    from utils.board_i18n import T, translate, flash_text
    key = next(iter(T))
    assert translate("de", key) == T[key]["de"]
    assert translate("xx", key) == T[key]["de"]              # unbekannte Sprache -> Deutsch
    assert translate("de", "gibt_es_nicht") == "gibt_es_nicht"
    assert flash_text("de", "unbekannter Volltext") == "unbekannter Volltext"
    assert flash_text("de", "") == ""


# ── Währungen ─────────────────────────────────────────────────────────────────
def test_currency_conversion_and_format():
    from utils.currency import format_price, to_eur
    assert to_eur(10, "EUR") == 10
    assert to_eur(10, "CHF") == 10.5
    assert to_eur(10, "XYZ") is None
    assert format_price(59.99, 59.99, "EUR") == "59.99EUR"
    assert format_price(10, 20, "EUR") == "10.00-20.00EUR"
    assert "CAD" in format_price(34.49, 34.49, "CAD") and "€" in format_price(34.49, 34.49, "CAD")


# ── Sicherheit (Board) ────────────────────────────────────────────────────────
def test_salt_problem():
    from web.core.security import salt_problem
    assert salt_problem(b"change-me-board-salt")
    assert salt_problem(b"kurz")
    assert salt_problem(b"")
    assert salt_problem(b"x" * 16) is None


def test_hmac_and_captcha():
    from web.core.security import _captcha_ok, _hmac, _make_captcha
    assert _hmac("a", "b") == _hmac("a", "b") != _hmac("a", "c")
    q, sig = _make_captcha()
    a, op, b = q.split()
    ans = {"+": int(a) + int(b), "-": int(a) - int(b), "×": int(a) * int(b)}[op]
    assert _captcha_ok({"captcha": str(ans), "captcha_sig": sig})
    assert not _captcha_ok({"captcha": str(ans + 1), "captcha_sig": sig})
    assert not _captcha_ok({"captcha": "abc", "captcha_sig": sig})
    assert not _captcha_ok({"captcha": str(ans), "captcha_sig": ""})


def test_utc_naive_matches_old_utcnow():
    from datetime import datetime, timezone, timedelta
    from utils.timez import utc_naive
    a = utc_naive()
    b = datetime.now(timezone.utc).replace(tzinfo=None)
    assert a.tzinfo is None and abs(b - a) < timedelta(seconds=2)
    assert utc_naive().isoformat().count("+") == 0                  # Export bleibt „…Z“ ohne „+00:00“
