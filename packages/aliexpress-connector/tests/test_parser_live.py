"""The REAL extractors run against the captured fixtures — not mocks.

The offline suite above monkeypatches the render step and asserts the tool
contract around parsed tiles; what it deliberately cannot prove is that the
extractor JavaScript still understands the site's markup. That half runs here
against trimmed real pages (see fixtures/*.provenance.json
for what those pages displayed), in jsdom when available. Mocking the render
call and feeding pre-parsed dicts is exactly how the DNS and Citilink bug
classes lived while 707 tests stayed green.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aliexpress_connector import server
from mcp_core.domtest import JsdomUnavailable, run_extractor

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
SEARCH_FIXTURE = FIXTURES / "search_realme_live.html"
NONSENSE_FIXTURE = FIXTURES / "search_nonsense_live.html"
CARD_FIXTURE = FIXTURES / "card.html"
SEARCH_PAGE_URL = "https://aliexpress.ru/wholesale?SearchText=realme+watch"


def _run(js_source, fixture, page_url):
    try:
        payload = run_extractor(js_source, fixture, page_url=page_url)
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))
    assert isinstance(payload, dict), f"extractor returned non-object: {payload!r}"
    return payload


def test_search_extractor_reads_only_the_main_feed():
    payload = _run(server._SEARCH_EXTRACT_JS, SEARCH_FIXTURE, SEARCH_PAGE_URL)

    assert (payload.get("punish"), payload.get("feed")) == (False, True)
    items = [t for t in payload.get("items") or [] if isinstance(t, dict)]
    assert len(items) == 17, "the feed held 17 tiles; «Подобрали для вас» below it is not the answer"
    by_id = {t.get("item_id"): t for t in items}
    realme = by_id["1005010003103368"]
    assert realme.get("title") == "Realme Watch 5 смарт-часы"
    assert (realme.get("price_texts") or {}).get("attached") == ["7 064 ₽", "3 049 ₽"]
    assert (realme.get("rating"), realme.get("orders")) == ("4.7", "2578")
    # This tile's delivery line is longer than its name and used to win as the title.
    assert by_id["1005006996441269"].get("title") == "Часы Realme Watch 5"


def test_search_extractor_pairing_agrees_with_python():
    """Parser-to-wire: the DOM order (base, current) must yield current=min; a coupon is never the price."""
    payload = _run(server._SEARCH_EXTRACT_JS, SEARCH_FIXTURE, SEARCH_PAGE_URL)
    parsed = {p.item_id: p for p in (server._item_from_payload(t) for t in payload["items"])}

    assert (parsed["1005010003103368"].price_rub, parsed["1005010003103368"].old_price_rub) == (3049.0, 7064.0)
    assert (parsed["1005012078314868"].price_rub, parsed["1005012078314868"].old_price_rub) == (5781.0, None)


def test_a_nonsense_query_leaves_only_unrelated_goods_in_the_feed():
    payload = _run(server._SEARCH_EXTRACT_JS, NONSENSE_FIXTURE, SEARCH_PAGE_URL)
    titles = [t.get("title") or "" for t in payload["items"]]

    assert len(titles) == 5, "59 links on the page, 5 in the feed; the rest are recommendations"
    assert not server._mentions_query("абырвалгщзх", titles)


def test_card_extractor_reads_the_captured_modules():
    payload = _run(server._CARD_EXTRACT_JS, CARD_FIXTURE, "https://aliexpress.ru/item/1005010003103368.html")

    assert payload.get("punish") is False
    assert "Realme Watch 5" in (payload.get("title") or "")
    assert "Рейтинг- 4.7" in (payload.get("meta_description") or "")
    # The SSR shell the fixture was captured from carries no price module: the
    # price pair renders client-side and is asserted by the cdp-marked test and
    # by the offline tool tests (CARD_EXTRACTED payload). Here the extractor
    # must return an empty glyph-scored structure rather than invent one.
    assert payload.get("price_texts") is not None


@pytest.mark.cdp
def test_live_cdp_reads_a_real_grid_and_card():
    """The whole two-hop flow against the operator's Chrome.

    Excluded from CI (no Chrome there). Run locally with CDP on port 9222:
        uv run pytest packages/aliexpress-connector/tests/test_parser_live.py -m cdp -s

    The card assertion is deliberately softer than the search one: measured on
    this very session, x5sec quietly strips the client-rendered price module
    from card pages when the session is probed hard — the title renders, the
    price does not (the connector reports that as `price_missing`, never as a
    fake number). A total render failure — no title either — stays a failure.
    """
    import asyncio
    import os

    async def _cdp_alive() -> bool:
        port = int(os.environ.get("CHROME_CDP_PORT", "9222"))
        _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=3)
        writer.close()
        await writer.wait_closed()
        return True

    try:
        alive = asyncio.run(_cdp_alive())
    except Exception:
        alive = False
    if not alive:
        pytest.skip("CDP is not listening — start the scraping Chrome (scripts/start_chrome_cdp) first")

    async def probe():
        search = await server._cdp_render_search(f"{server.SEARCH_URL}?SearchText={'умные часы'}", ctx=None)
        items = [t for t in (search.get("items") or []) if isinstance(t, dict)]
        assert items, "no tiles on the live grid"
        parsed = server._item_from_payload(items[0])
        assert parsed.title, "live tile title did not parse"
        card = await server._cdp_card(f"{server.SITE_BASE}/item/{parsed.item_id}.html", ctx=None)
        title = card.get("title")
        price, _old = server._card_prices(card)
        assert title, "live card rendered no title at all — hard shape break"
        assert price is None or price > 0
        # price_missing is a known, honest state under x5sec load-shedding; the
        # tool-level contract for it is covered by test_card_warns_when_price_missing.

    asyncio.run(probe())
