"""Offline tests for AliExpress through a faked Chrome tab: refusals stop the shop, the card opens from a search tab."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from aliexpress_connector import server
from fastmcp.exceptions import ToolError
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport.chrome_cdp import NavBlocked, NavigationPolicyError

ITEM = "1005010003103368"
ITEM_URL = f"https://aliexpress.ru/item/{ITEM}.html"


def tile(item_id: str, title: str, *prices: str, sku: str | None = None) -> dict:
    return {
        "item_id": item_id,
        "title": title,
        "price_texts": {"attached": list(prices), "other": []},
        "rating": "4.7",
        "orders": "2578",
        "sku_id": sku,
    }


REALME = {
    "punish": False,
    "feed": True,
    "nothing": False,
    "page_title": "realme watch с бесплатной доставкой на AliExpress",
    "items": [
        tile(ITEM, "Realme Watch 5 смарт-часы", "7 064 ₽", "3 049 ₽", sku="12000050856992499"),
        tile("1005012078314868", "realme Watch S5 умные часы", "5 348 ₽ с купоном", "5 781 ₽"),
    ],
}
# Live 2026-09-27 22:09:05, «абырвалгщзх»: the feed held these goods, none naming the query.
NONSENSE = {
    "punish": False,
    "feed": True,
    "nothing": False,
    "page_title": "абырвалгщзх с бесплатной доставкой на AliExpress",
    "items": [
        tile("1005012833320889", "ARZOPA M1RC игровой монитор 27 дюймов 2K QHD", "24 281 ₽", "10 279 ₽"),
        tile("1005010804172553", "Наращивание волос MORESOO Butterfly Weft", "21 776 ₽ с купоном", "22 469 ₽"),
    ],
}
CARD = {
    "punish": False,
    "title": "Realme Watch 5 смарт-часы",
    "price_texts": {"attached": ["3 049 ₽", "7 064 ₽"], "other": []},
    "rating_text": "4,7",
    "meta_description": "Realme Watch 5 смарт-часы. ➜ 2578 заказов. ⭐ Рейтинг- 4.7",
    "url": ITEM_URL,
}
PUNISH = {"punish": True, "items": [], "feed": False, "nothing": False, "page_title": "Пройдите проверку"}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="aliexpress", gate=StopGate(None)))
    monkeypatch.setattr(server, "_POLL_S", 0.0)
    monkeypatch.setattr(server, "_min_gap", 0.0)
    monkeypatch.setattr(server, "_RENDER_WAIT_S", 0.0)
    yield
    server._cache.clear()


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


class FakeTab:
    def __init__(self, context: FakeContext | None, url: str, payload: dict, opens: tuple[str, dict] | None = None):
        self.context = context
        self.url = url
        self.payload = payload
        self.opens = opens
        self.closed = False
        self.scripts: list[str] = []

    async def evaluate(self, expression, arg=None):
        self.scripts.append(expression)
        if "window.open" in expression:
            assert self.context is not None and self.opens is not None
            self.context.pages.append(FakeTab(self.context, *self.opens))
            return "opened"
        return json.dumps(self.payload, ensure_ascii=False)

    async def close(self):
        self.closed = True
        if self.context is not None and self in self.context.pages:
            self.context.pages.remove(self)


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakeTab] = []


def fake_tabs(monkeypatch, *outcomes) -> list[str]:
    """Each outcome is a tab (FakeTab or payload dict) or an exception to raise on navigation."""
    urls: list[str] = []
    queue = list(outcomes)

    @asynccontextmanager
    async def open_page(url, wait_ms=5000, **kwargs):
        urls.append(url)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome if isinstance(outcome, FakeTab) else FakeTab(None, url, outcome)

    monkeypatch.setattr(server, "open_page", open_page)
    return urls


def search_tab(opens: tuple[str, dict] | None) -> tuple[FakeTab, FakeContext]:
    context = FakeContext()
    landing = FakeTab(context, "https://aliexpress.ru/wholesale?SearchText=x", REALME, opens)
    context.pages.append(landing)
    return landing, context


# ----------------------------------------------------------------- search ----


async def test_search_through_the_tab_returns_current_prices_and_canonical_urls(monkeypatch):
    urls = fake_tabs(monkeypatch, REALME)

    result = await server.aliexpress_search("realme watch")

    assert urls == ["https://aliexpress.ru/wholesale?SearchText=realme%20watch"]
    assert [(i.item_id, i.price_rub, i.old_price_rub, i.url) for i in result.items] == [
        (ITEM, 3049.0, 7064.0, f"{ITEM_URL}?sku_id=12000050856992499"),
        ("1005012078314868", 5781.0, None, "https://aliexpress.ru/item/1005012078314868.html"),
    ]
    assert any("coupon" in w for w in result.meta.warnings)


async def test_goods_that_do_not_name_the_query_are_no_match(monkeypatch):
    fake_tabs(monkeypatch, NONSENSE)

    result = await server.aliexpress_search("абырвалгщзх")

    assert (result.count, result.items) == (0, [])
    assert result.meta.warnings[0].startswith("no_match")


async def test_nothing_found_is_an_empty_answer_not_drift(monkeypatch):
    fake_tabs(monkeypatch, {"punish": False, "feed": False, "nothing": True, "items": [], "page_title": ""})

    result = await server.aliexpress_search("qqq")

    assert result.count == 0
    assert result.meta.warnings == ["empty: AliExpress found nothing for the query"]


async def test_a_page_with_neither_tiles_nor_nothing_is_drift_not_a_stop(monkeypatch):
    urls = fake_tabs(
        monkeypatch, {"punish": False, "feed": False, "nothing": False, "items": [], "page_title": ""}, REALME
    )

    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_search("realme watch")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.aliexpress_search("realme watch")).count == 2
    assert len(urls) == 2


@pytest.mark.parametrize(
    "texts",
    [
        ["510 ₽", "309 ₽", "завтра, от 109 ₽"],
        ["510 ₽", "309 ₽", "от 109 ₽ доставка"],
        ["510 ₽", "309 ₽", "до 14 дней, от 109 ₽"],
    ],
)
def test_a_delivery_line_is_never_the_price(texts):
    assert server._tile_prices({"price_texts": {"attached": texts}}) == (309.0, 510.0, None)


@pytest.mark.parametrize(
    "outcome",
    [
        NavBlocked(403, "https://aliexpress.ru/wholesale"),
        NavBlocked(429, "https://aliexpress.ru/wholesale"),
        NavigationPolicyError("https://login.aliexpress.ru/bx/", {"aliexpress.ru"}),
        PUNISH,
    ],
)
async def test_a_refused_search_tab_stops_aliexpress(monkeypatch, outcome):
    urls = fake_tabs(monkeypatch, outcome, REALME)

    with pytest.raises(ToolError):
        await server.aliexpress_search("realme watch")
    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_search("realme watch 5")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == 1


async def test_a_failing_page_is_transport_down_not_a_stop(monkeypatch):
    urls = fake_tabs(monkeypatch, NavBlocked(500, ""), REALME)

    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_search("realme watch")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.aliexpress_search("realme watch")).count == 2
    assert len(urls) == 2


# ------------------------------------------------------------------- card ----


async def test_the_card_opens_from_a_search_tab_and_its_tab_is_closed(monkeypatch):
    landing, context = search_tab((ITEM_URL, CARD))
    urls = fake_tabs(monkeypatch, landing)

    card = await server.aliexpress_card(ITEM)

    assert urls == [server._LANDING]
    assert any("'noopener'" in script for script in landing.scripts if "window.open" in script)
    assert (card.item_id, card.price_rub, card.old_price_rub, card.orders_count) == (ITEM, 3049.0, 7064.0, 2578)
    assert context.pages == [landing]


async def test_a_tab_for_another_item_is_not_the_card(monkeypatch):
    landing, context = search_tab(("https://aliexpress.ru/item/1005000000000001.html", CARD))
    fake_tabs(monkeypatch, landing)

    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_card(ITEM)
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert context.pages == [landing]
    server._pacer.check_stopped()


@pytest.mark.parametrize(
    "opens",
    [
        ("https://aliexpress.ru/_____tmd_____/punish?x5secdata=x", PUNISH),
        (ITEM_URL, {**CARD, "punish": True}),
    ],
)
async def test_x5sec_on_the_item_tab_stops_aliexpress(monkeypatch, opens):
    landing, context = search_tab(opens)
    urls = fake_tabs(monkeypatch, landing)

    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_card(ITEM)
    assert error_payload(excinfo.value)["error"] == "challenge_required"
    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_card(ITEM)
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert (len(urls), context.pages) == (1, [landing])


async def test_a_raw_cdp_tab_cannot_open_the_card(monkeypatch):
    fake_tabs(monkeypatch, REALME)

    with pytest.raises(ToolError) as excinfo:
        await server.aliexpress_card(ITEM)
    assert error_payload(excinfo.value)["error"] == "transport_down"
