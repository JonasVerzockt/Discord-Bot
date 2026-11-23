# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web-Oberfläche: Stylesheets, Auslieferung statischer Dateien, Konto-Aktionstabelle,
Start-Schutz beim Salt – alles offline mit dem aiohttp-Testclient."""
import asyncio
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r'<link rel="stylesheet" href="/static/([A-Za-z0-9_.-]+\.css)\?v=\{\{ css_v \}\}">')


def _linked_css() -> set[str]:
    names = set()
    for t in (ROOT / "web/templates").glob("*.html"):
        names |= set(LINK_RE.findall(t.read_text(encoding="utf-8")))
    return names


def test_every_linked_stylesheet_exists_and_is_whitelisted():
    from web.core.static import _STATIC_FILES
    linked = _linked_css()
    assert "board.css" in linked
    for name in linked:
        assert (ROOT / "static" / name).is_file(), f"static/{name} fehlt"
        assert _STATIC_FILES.get(name) == "text/css", f"{name} nicht für /static freigegeben"


def test_no_inline_style_blocks_left_except_small_ones():
    """Große Style-Blöcke gehören in static/*.css (kleine Einzeiler dürfen bleiben)."""
    for t in (ROOT / "web/templates").glob("*.html"):
        for css in re.findall(r"<style[^>]*>(.*?)</style>", t.read_text(encoding="utf-8"), re.S):
            assert css.count("\n") < 3, f"{t.name}: großer <style>-Block – nach static/ auslagern"


def test_stylesheets_are_served_with_cache_busting():
    import board_snapshot as BS
    from aiohttp.test_utils import TestClient, TestServer
    from web import build_app

    async def run():
        bot = BS.FakeBot(asyncio.get_running_loop())
        await BS._seed(bot)
        async with TestClient(TestServer(build_app(bot))) as c:
            page = await (await c.get("/?lang=de")).text()
            hrefs = re.findall(r'href="(/static/board[a-z-]*\.css\?v=[^"]+)"', page)
            assert hrefs, "Startseite bindet board.css nicht ein"
            for h in hrefs:
                r = await c.get(h)
                assert r.status == 200 and r.headers["Content-Type"].startswith("text/css")
                assert "max-age" in r.headers.get("Cache-Control", "")
            assert (await c.get("/static/../config.py")).status == 404
            assert (await c.get("/static/gibt-es-nicht.css")).status == 404
    asyncio.run(run())


def test_konto_action_table_complete():
    """Jede Konto-Aktion zeigt auf eine async-Funktion; gefährliche Aktionen sind abgedeckt."""
    import inspect
    from web.features import account as A
    assert len(A._KONTO_ACTIONS) >= 20
    for act, fn in A._KONTO_ACTIONS.items():
        assert inspect.iscoroutinefunction(fn), act
    for act in A._KONTO_DANGER:
        assert act in A._KONTO_ACTIONS, f"Sperre für unbekannte Aktion {act}"


def test_board_refuses_placeholder_salt(monkeypatch):
    import board_snapshot as BS
    import cogs.board as CB
    monkeypatch.setattr(CB, "salt_problem", lambda: "Platzhalter aktiv")

    class Bot(BS.FakeBot):
        async def wait_until_ready(self):
            pass

    async def run():
        cog = CB.BoardCog.__new__(CB.BoardCog)
        cog.bot, cog.runner = Bot(asyncio.get_running_loop()), None
        await cog._start()
        return cog.runner
    assert asyncio.run(run()) is None
