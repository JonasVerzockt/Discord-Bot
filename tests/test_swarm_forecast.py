# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schwarmflug-Radar und Prognose ohne Netz:
  • Meldungen lesen (utils/swarm.parse_message): Vorlage, Freitext, Gespräch, Haltung,
    Ausland, Regionsrolle, Datum/Uhrzeit/Temperatur, Arten mit cf./sp./Gruppe/Untergattung
  • Kommentar statt neuer Meldung, Kennzahlen/Steckbrief (summarize), Saisonstart
  • Prognose-Rechnung (utils/forecast): Entfernung, Punkt-in-Region, Tagesmerkmale aus
    einer Stundenreihe, logistische Regression, AUC, Quantile, Stufen, Flugfenster,
    Zusammenfassung je Taxon, Alarm-Abgleich, Open-Meteo-Kosten
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from utils import forecast as F
from utils import swarm as S
from utils import swarm_resolve as SR

NOW = datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc)           # 18 Uhr Berlin


def _p(text, roles=None):
    return S.parse_message(text, roles or {}, NOW)


# ── Meldungen lesen ───────────────────────────────────────────────────────────

def test_template_report_full():
    p = _p("🐜 Art: Lasius niger\n🕑 Datum u. Uhrzeit: 12.07. 17:30\n🌡️ Temperatur: 26,5°C\n"
           "🌬️ Wind: schwach\n🗺️ Ort: Augsburg <@&111>\n🌧️ Wetter: sonnig, schwül", {"111": "Bayern"})
    assert p["taxa"] == [{"genus": "Lasius", "species": "niger", "qualifier": ""}]
    assert (p["date"], p["time"], p["temp"]) == ("2026-07-12", "17:30", 26.5)
    assert (p["place"], p["country"], p["region"]) == ("Augsburg", "de", "BY")
    assert p["wind"] == "schwach" and p["weather"] == "sonnig, schwül"
    assert p["templated"] and p["rule_ok"] and not p["indoor"] and p["abroad"] is None


def test_template_relative_date_and_several_taxa():
    p = _p("🐜 Art: L. flavus, Myrmica cf. rubra\n🕑 Datum: gestern 19 Uhr\n🌡️ Temperatur: 22\n🗺️ Ort: 47167 Duisburg")
    assert [S.taxon_label(t) for t in p["taxa"]] == ["Lasius flavus", "Myrmica cf. rubra"]
    assert (p["date"], p["time"], p["temp"], p["place"]) == ("2026-07-11", "19:00", 22.0, "47167 Duisburg")


def test_free_text_report_and_chat():
    p = _p("Gerade Lasius niger Schwarmflug in Düsseldorf")
    assert p["free"] and p["rule_ok"] and p["place"] == "Düsseldorf"
    chat = _p("Hat jemand eine Idee wie lange Lasius niger braucht bis die ersten Arbeiterinnen schlüpfen, "
              "bei mir ist es jetzt schon sechs Wochen her")
    assert chat["taxa"] and not chat["rule_ok"]                     # langes Gespräch → KI entscheidet


def test_indoor_abroad_canton():
    indoor = _p("🐜 Art: Camponotus ligniperda/herculeanus\n🗺️ Ort: im Becken zuhause, Wien")
    assert indoor["indoor"] and (indoor["country"], indoor["region"]) == ("at", "9")
    assert [t["species"] for t in indoor["taxa"]] == ["ligniperda", "herculeanus"]
    assert _p("🐜 Art: Messor barbarus\n🗺️ Ort: Mallorca, Spanien")["abroad"] == "es"
    ch = _p("🐜 Art: Formica sp.\n🗺️ Ort: Zürich")
    assert (ch["country"], ch["region"], ch["place"]) == ("ch", "ZH", "")   # nur Region, kein Ort


def test_date_outside_window_falls_back_to_post_date():
    p = _p("🐜 Art: Lasius niger\n🕑 Datum: 01.03.2026 18:00")
    assert p["date"] == "2026-07-12"


@pytest.mark.parametrize("text,labels", [
    ("Formica rufa Gruppe", ["Formica rufa Gruppe"]),
    ("Lasius s. str.", ["Lasius s. str."]),
    ("Chthonolasius sp.", ["Lasius (Chthonolasius)"]),
    ("Myrmica cf. rubra", ["Myrmica cf. rubra"]),
    ("Formica.polyctena", ["Formica polyctena"]),
    ("Lasius ist geschwärmt", ["Lasius sp."]),                     # Füllwort ist kein Artname
    ("heute viele Ameisen", []),
])
def test_parse_taxa(text, labels):
    assert [S.taxon_label(t) for t in S.parse_taxa(text)] == labels


def test_field_helpers():
    ref = date(2026, 7, 1)
    assert S._date("3. Mai", ref) == date(2026, 5, 3)
    assert S._date("31.02.", ref) is None
    assert S._date("12.07.25", ref) == date(2025, 7, 12)
    assert S._time("ca 18.30 Uhr") == "18:30" and S._time("um 9h") == "09:00" and S._time("30.03. 12:00") == "12:00"
    assert S._time("abends") is None
    assert S._temp("50°") is None and S._temp("-3") == -3.0 and S._temp("23,5 °C") == 23.5
    assert S._place("???") == "" and S._place("k.A.") == "" and S._place("Bayern") == ""


def test_region_fuzzy_and_abbreviation():
    assert SR.region_fuzzy("Rehinland-Pfalz") == ("de", "RP")
    assert S.region_in_text("irgendwo in NRW") == ("de", "NW")
    assert S.region_in_text("Bern") is None                          # kurze CH-Namen nur mit „Kanton“
    assert S.region_in_text("Kanton Bern") == ("ch", "BE")


def test_epithet_plausible():
    assert SR.epithet_plausible("niger") and not SR.epithet_plausible("ist")
    assert not SR.epithet_plausible("schwärmt") and not SR.epithet_plausible("")


def test_comment_vs_new_report():
    prev = {"genus": "Lasius", "region": "BY", "place": "Augsburg"}
    short = {"taxa": [{"genus": "Lasius"}], "place": "", "region": "", "time": None, "temp": None}
    assert S.is_comment(short, prev, True)
    assert not S.is_comment({**short, "time": "18:00"}, prev, True)          # eigene Uhrzeit
    assert not S.is_comment({**short, "taxa": [{"genus": "Messor"}]}, prev, True)
    assert not S.is_comment(short, prev, False)                              # Vorlage = eigene Meldung
    assert S.is_comment({**short, "region": "BY", "place": "augsburg"}, prev, True)
    assert not S.is_comment({**short, "region": "BW", "place": "Ulm"}, prev, True)
    assert S.comment_of(short, [{"genus": "Messor"}, prev], True) is prev


def _r(d, genus="Lasius", species="niger", t="18:00", temp=24, country="de", region="BY", mid=None, author="a"):
    return {"flight_date": d, "flight_time": t, "genus": genus, "species": species, "temp_c": temp, "w_temp": None,
            "country": country, "region": region, "message_id": mid or d + genus, "author_id": author}


def test_summarize_profile():
    rs = [_r("2025-07-10"), _r("2025-07-12", t="19:00", temp=26), _r("2026-07-11", t="17:00", temp=22, author="b"),
          _r("2026-07-14", t="18:00", temp=25, region="BW"), _r("2026-05-01", genus="Messor", species=None)]
    s = S.summarize(rs)
    assert s["total"] == 5 and s["genera"] == 2 and s["years"] == ["2025", "2026"] and s["reporters"] == 2
    ln = next(t for t in s["taxa"] if t["name"] == "Lasius niger")
    assert ln["n"] == 4 and ln["years"] == 2 and ln["months"][6] == 4 and ln["hour"] == 18
    assert ln["temp"] == 24 and ln["last"] == "2026-07-14"
    assert ln["regions"][0][1] == 3                                          # Bayern 3×
    assert all(t["name"] != "Messor" for t in s["taxa"])                     # < 3 Meldungen → kein Steckbrief
    assert s["regions"] == {"de-BY": 4, "de-BW": 1}
    assert S.temp_of({"temp_c": None, "w_temp": 19.5}) == 19.5


def test_week_label_and_season_banner():
    assert S.week_label(27) == "Anfang Juli" and S.week_label(27, "en") == "early July" and S.week_label(None) == "?"
    S.SEASON.clear()
    S.SEASON[2026] = {"created_at": "2026-04-02 10:00:00"}
    try:
        assert S.season_now(date(2026, 4, 10)) is not None
        assert S.season_now(date(2026, 4, 20)) is None                       # > 14 Tage
        assert S.season_now(date(2027, 4, 3)) is None
    finally:
        S.SEASON.clear()


# ── Prognose-Rechnung ─────────────────────────────────────────────────────────

def _box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def test_km_and_point_in_region(tmp_path, monkeypatch):
    import json
    assert F.km(48.137, 11.575, 48.137, 11.575) == 0
    assert 500 < F.km(48.137, 11.575, 52.52, 13.405) < 510                  # München–Berlin ≈ 504 km
    assert F._inside(5, 5, _box(0, 0, 10, 10)) and not F._inside(11, 5, _box(0, 0, 10, 10))
    # Die echten Umrisse (static/*.geojson) liegen nicht im Repo → vereinfachte Rechtecke
    feats = lambda *fs: {"features": [{"properties": pr, "geometry": g} for pr, g in fs]}
    (tmp_path / "de_bundeslaender.geojson").write_text(json.dumps(feats(
        ({"shapeISO": "DE-BY"}, {"type": "Polygon", "coordinates": [_box(9, 47.3, 13.8, 50.5)]}),
        ({"shapeISO": "DE-BE"}, {"type": "MultiPolygon", "coordinates": [[_box(13.1, 52.3, 13.7, 52.7)]]}))))
    (tmp_path / "ch_kantone.geojson").write_text(json.dumps(feats(
        ({"shapeISO": "CH-ZH"}, {"type": "Polygon", "coordinates": [_box(8.36, 47.16, 8.98, 47.69)]}))))
    (tmp_path / "li_land.geojson").write_text(json.dumps(feats(
        ({"shapeName": "Liechtenstein"}, {"type": "Polygon", "coordinates": [_box(9.47, 47.05, 9.64, 47.27)]}),
        ({"shapeISO": "XX"}, {"type": "Polygon", "coordinates": [_box(0, 0, 1, 1)]}))))
    monkeypatch.setattr(F, "STATIC", tmp_path)
    polys = F._polys()
    assert len(polys) == 4                                                   # „XX“ ohne Land-Kürzel entfällt
    assert F.region_of(48.137, 11.575, polys) == ("de", "BY")               # München
    assert F.region_of(52.52, 13.405, polys) == ("de", "BE")                # Berlin (MultiPolygon)
    assert F.region_of(47.377, 8.541, polys) == ("ch", "ZH")                # Zürich
    assert F.region_of(47.14, 9.52, polys) == ("li", "LI")                  # Vaduz
    assert F.region_of(40.4, -3.7, polys) is None                           # Madrid


def _hourly(day="2026-07-12", temp=25.0, rain_hour=None):
    start = datetime.fromisoformat(day) - timedelta(days=3)
    times, t, pr, pp, ps = [], [], [], [], []
    for i in range(4 * 24):
        ts = start + timedelta(hours=i)
        times.append(ts.strftime("%Y-%m-%dT%H:00"))
        t.append(temp - 6 + (6 if 12 <= ts.hour <= 19 else 0))
        pr.append(2.0 if rain_hour is not None and ts.strftime("%Y-%m-%d") == day and ts.hour == rain_hour else 0.0)
        pp.append(80 if rain_hour is not None and ts.hour == rain_hour else 10)
        ps.append(1015.0 if ts.hour < 8 else 1012.0)
    return {"time": times, "temperature_2m": t, "precipitation": pr, "precipitation_probability": pp,
            "pressure_msl": ps, "relative_humidity_2m": [60] * len(times), "wind_speed_10m": [8] * len(times),
            "shortwave_radiation": [500] * len(times)}


def test_day_features_and_hours():
    h = _hourly()
    f = F.day_features(h, "2026-07-12")
    assert f["temp"] == 25.0 and f["hum"] == 60 and f["wind"] == 8 and f["rad"] == 500
    assert f["rain24"] == 0.0 and f["rain72"] == 0.0 and f["dp12"] == -3.0 and f["tmax"] == 25.0
    assert f["soil_t"] is None                                               # fehlt in der Reihe → None
    assert F.day_features(h, "2030-01-01") is None
    hrs = F.hours_of(_hourly(rain_hour=15), "2026-07-12")
    assert set(hrs) == set(range(10, 22)) and hrs[15][1] == 2.0 and hrs[15][2] == 80


def test_logistic_fit_separates_and_auc():
    X = [[t] for t in range(10, 31)]
    y = [1 if t >= 22 else 0 for t in range(10, 31)]
    m = F._fit(X, y, lam=0.1)
    assert F._prob(m, [28]) > 0.9 and F._prob(m, [12]) < 0.1
    assert F._prob(m, [None]) == pytest.approx(F._prob(m, [sum(range(10, 31)) / 21]))   # fehlend → Mittelwert
    scores = [F._prob(m, x) for x in X]
    assert F._auc(scores, y) == 1.0
    assert F._auc([0.5, 0.5], [1, 0]) == 0.5                                # Gleichstand
    assert F._auc([0.1, 0.2], [0, 0]) is None


def test_weighted_quantile_and_season_curve():
    assert F._wquant([1, 2, 3, 4], [1, 1, 1, 1], 0.5) == 2
    assert F._wquant([1, 2, 3, 4], [0, 0, 0, 10], 0.5) == 4
    c = F._season_curve([27, 27, 28])
    assert c[27] == 1.0 and c[28] < 1.0 and c[10] == 0.01 and len(c) == 54


def _rule_mod(levels=None):
    return {"kind": "rule", "params": {"curve": [1.0] * 54, "t": [18, None, 24], "h": [16, 20]}}


def test_rule_score_capped_at_good():
    f = {"temp": 24, "rain24": 0}
    p, lv = F.score(_rule_mod(), f, 27, 5)
    assert p == pytest.approx(1.0) and lv == 2                             # Regel nie „sehr gut“
    _, lv_wet = F.score(_rule_mod(), {"temp": 24, "rain24": 12}, 27, 5)
    _, lv_cold = F.score(_rule_mod(), {"temp": 10, "rain24": 0}, 27, 5)
    assert lv_wet == 1 and lv_cold == 0


@pytest.mark.filterwarnings("ignore:Mean of empty slice")   # leere Spalten sind gewollt
def test_model_levels():
    X = [[0, t] + [None] * 14 for t in range(10, 31)]
    y = [1 if t >= 22 else 0 for t in range(10, 31)]
    m = F._fit([[x[0]] + x[1:] for x in X], y, lam=0.1)
    mod = {"kind": "model", "params": {**m, "curve": [1.0] * 54, "levels": [0.3, 0.6, 0.9]}}
    _, hot = F.score(mod, {"temp": 29}, 27, 0)
    _, cold = F.score(mod, {"temp": 11}, 27, 0)
    assert hot == 3 and cold == 0


def test_flight_window():
    par = {"h": [16, 19], "t": [18, None, 24]}
    hrs = {h: (24.0, 0.0, 10) for h in range(10, 22)}
    assert F.window(par, hrs) == (15, 20)
    hrs[15] = (24.0, 1.0, 10)                                               # Regen um 15 Uhr
    hrs[20] = (24.0, 0.0, 70)                                               # hohe Regenwahrscheinlichkeit
    assert F.window(par, hrs) == (16, 19)
    cold = {h: (12.0, 0.0, 10) for h in range(10, 22)}
    assert F.window(par, cold) == (None, None)
    assert F.window({"h": [None, None], "t": [None, None, None]}, {h: (20.0, 0, 0) for h in range(10, 22)}) == (13, 21)


def test_condense_drops_genus_when_species_present():
    rows = [{"taxon": "Lasius", "level": 2, "p": .5, "country": "de", "region": "BY", "h_from": 16, "h_to": 19, "temp": 24},
            {"taxon": "Lasius niger", "level": 3, "p": .9, "country": "de", "region": "BY", "h_from": 17, "h_to": 20, "temp": 25},
            {"taxon": "Lasius niger", "level": 1, "p": .4, "country": "de", "region": "BW", "h_from": 15, "h_to": 18, "temp": 21},
            {"taxon": "Messor", "level": 1, "p": .3, "country": "at", "region": "9", "h_from": None, "h_to": None, "temp": None}]
    out = F.condense(rows)
    assert [e["taxon"] for e in out] == ["Lasius niger", "Messor"]
    ln = out[0]
    assert ln["level"] == 3 and ln["regions"][0] == "Bayern" and len(ln["regions"]) == 2
    assert out[1]["h_from"] is None and out[1]["temp"] is None
    assert len(F.condense(rows, max_taxa=1)) == 1


def test_taxon_matches():
    assert F.taxon_matches("Lasius", "Lasius niger") and F.taxon_matches("Lasius niger", "Lasius")
    assert F.taxon_matches("Lasius niger", "Lasius niger") and not F.taxon_matches("Lasius niger", "Lasius flavus")
    assert not F.taxon_matches("Messor", "Lasius")


def test_open_meteo_cost():
    assert F.cost(1, 1, 5) == 1
    assert F.cost(100, 8, 25) == 100 * 2 * 2.5                               # Orte × ⌈Tage/7⌉ × Variablen/10


# ── Cog: Meldung einlesen → speichern → Alarm-PN ──────────────────────────────

import asyncio                                                              # noqa: E402
from types import SimpleNamespace                                           # noqa: E402

import board_snapshot as BS                                                 # noqa: E402

REPORTER = 850000000000000001
FAN = "850000000000000002"
FAN_BW = "850000000000000003"


class _Msg:
    _n = 9300000000000000

    def __init__(self, content, author=REPORTER, roles=(), minutes_ago=60, bot=False, reference=None):
        _Msg._n += 1
        self.id = _Msg._n
        self.content = content
        self.author = SimpleNamespace(id=author, bot=bot)
        self.webhook_id = None
        self.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self.role_mentions = [SimpleNamespace(id=i, name=n) for i, n in roles]
        self.reference = reference
        self.channel = SimpleNamespace(history=self._history)

    def _history(self, **kw):
        async def gen():
            if False:
                yield None
        return gen()


class _User:
    def __init__(self, uid):
        self.id = int(uid)
        self.embeds = []

    async def send(self, embed=None, **kw):
        self.embeds.append(embed)


class _SwBot(BS.FakeBot):
    def __init__(self, loop):
        super().__init__(loop)
        self.u = {}

    def get_user(self, uid):
        return self.u.setdefault(int(uid), _User(uid))


@pytest.fixture()
def sw(monkeypatch):
    import cogs.swarm as C
    quiet = set()
    monkeypatch.setattr(C, "is_quiet", lambda uid: str(uid) in quiet)
    S.SEASON.clear()

    def run(coro_fn):
        async def go():
            from utils.db import init_db, execute_db
            bot = _SwBot(asyncio.get_running_loop())
            await init_db(bot)
            for t in ("swarm_reports", "swarm_alerts", "swarm_alert_sent"):
                await execute_db(bot, f"DELETE FROM {t}", commit=True)
            cog = C.Swarm.__new__(C.Swarm)                                   # ohne Hintergrund-Schleifen
            cog.bot, cog._scan_running = bot, False
            return await coro_fn(cog, bot, execute_db)
        return asyncio.run(go())
    return SimpleNamespace(run=run, quiet=quiet)


async def _alert(execute_db, bot, uid, taxon, country="", region=""):
    await execute_db(bot, "INSERT INTO swarm_alerts (user_id, taxon, country, region) VALUES (?,?,?,?)",
                     (uid, taxon, country, region), commit=True)


def test_process_stores_report(sw):
    async def c(cog, bot, db):
        m = _Msg("🐜 Art: Lasius niger\n🕑 Datum u. Uhrzeit: heute 18:00\n🌡️ Temperatur: 25\n🗺️ Ort: Augsburg <@&7>",
                 roles=[(7, "Bayern")])
        n = await cog.process(m, use_ai=False, geo=False)
        rows = await db(bot, "SELECT genus, species, country, region, place, temp_c, source FROM swarm_reports "
                             "WHERE message_id=?", (str(m.id),), fetch=True)
        return n, [dict(r) for r in rows]
    n, rows = sw.run(c)
    assert n == 1
    assert rows == [{"genus": "Lasius", "species": "niger", "country": "de", "region": "BY",
                     "place": "Augsburg", "temp_c": 25.0, "source": "rule"}]


def test_process_ignores_chat_bot_and_comment(sw):
    async def c(cog, bot, db):
        first = _Msg("🐜 Art: Messor barbarus\n🗺️ Ort: Wien", minutes_ago=90)
        await cog.process(first, use_ai=False, geo=False)
        chat = _Msg("Hat jemand eine Idee wie lange Lasius niger braucht bis die ersten Arbeiterinnen schlüpfen, "
                    "bei mir ist es jetzt schon sechs Wochen her")
        botm = _Msg("🐜 Art: Lasius niger\n🗺️ Ort: Köln", bot=True)
        comment = _Msg("Mein erster Messor Flug!", minutes_ago=80)
        res = [await cog.process(x, use_ai=False, geo=False) for x in (chat, botm, comment)]
        n = await db(bot, "SELECT COUNT(*) AS n FROM swarm_reports WHERE genus<>''", fetch=True)
        return res, n[0]["n"]
    res, n = sw.run(c)
    assert res == [0, 0, 0] and n == 1


def test_report_without_place_inherits_previous(sw):
    async def c(cog, bot, db):
        await cog.process(_Msg("🐜 Art: Lasius niger\n🗺️ Ort: Augsburg <@&7>", roles=[(7, "Bayern")], minutes_ago=50),
                          use_ai=False, geo=False)
        m = _Msg("🐜 Art: Lasius flavus\n🕑 Datum u. Uhrzeit: heute 18:30", minutes_ago=30)
        await cog.process(m, use_ai=False, geo=False)
        r = await db(bot, "SELECT country, region, place FROM swarm_reports WHERE message_id=?", (str(m.id),), fetch=True)
        return dict(r[0])
    assert sw.run(c) == {"country": "de", "region": "BY", "place": "Augsburg"}


def test_alert_loop_sends_matching_dms_once(sw):
    async def c(cog, bot, db):
        await _alert(db, bot, FAN, "Lasius")                                # Gattung, überall
        await _alert(db, bot, FAN_BW, "Lasius niger", "de", "BW")           # nur Baden-Württemberg
        await _alert(db, bot, str(REPORTER), "Lasius")                     # eigene Meldung → nie
        await _alert(db, bot, "850000000000000004", "Lasius flavus")       # andere Art
        await cog.process(_Msg("🐜 Art: Lasius niger\n🌡️ Temperatur: 24\n🗺️ Ort: Augsburg <@&7>",
                               roles=[(7, "Bayern")]), use_ai=False, geo=False)
        await cog.alert_loop()
        await cog.alert_loop()                                              # zweiter Lauf: keine Doppel-PN
        return {uid: len(u.embeds) for uid, u in bot.u.items()}, bot.u[int(FAN)].embeds[0]
    counts, e = sw.run(c)
    assert counts == {int(FAN): 1}
    assert "Lasius niger" in e.description and "Bayern" in e.description and "24 °C" in e.description


def test_alert_loop_respects_quiet_and_indoor(sw):
    async def c(cog, bot, db):
        await _alert(db, bot, FAN, "Lasius")
        sw.quiet.add(FAN)
        await cog.process(_Msg("🐜 Art: Lasius niger\n🗺️ Ort: Augsburg <@&7>", roles=[(7, "Bayern")]),
                          use_ai=False, geo=False)
        await cog.process(_Msg("🐜 Art: Lasius flavus\n🗺️ Ort: im Becken zuhause <@&7>", roles=[(7, "Bayern")]),
                          use_ai=False, geo=False)
        await cog.alert_loop()
        n_quiet = len(bot.get_user(FAN).embeds)
        sw.quiet.clear()
        await cog.alert_loop()
        return n_quiet, bot.get_user(FAN).embeds
    n_quiet, embeds = sw.run(c)
    assert n_quiet == 0 and len(embeds) == 1
    assert "Lasius niger" in embeds[0].description and "flavus" not in embeds[0].description   # Haltung zählt nicht
