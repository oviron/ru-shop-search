"""Offline tests for the Citilink connector: the Chrome tab is faked at ``open_page``."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from citilink_connector import server
from fastmcp.exceptions import ToolError
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport.chrome_cdp import NavBlocked

SLUG = "videokarta-palit-pci-e-rtx5070-infinity-3-nv-rtx5070-12gb-192bit-gddr7-2092141"
CARD_URL = f"https://www.citilink.ru/product/{SLUG}/"
TITLE = "rtx 5070 - купить по низкой цене в Ситилинк"

SEARCH_PAYLOAD = {
    "title": TITLE,
    "city": "Город по IP",
    "nothing": False,
    "items": [
        {
            "url": CARD_URL,
            "title": "Видеокарта Palit NVIDIA  GeForce RTX 5070 PA-RTX5070 Infinity 3",
            "price": "94560",
            "old_price_text": None,
            "rating": "4.9",
            "reviews": "18",
            "buyable": True,
            "awaited": False,
        },
        {
            "url": "https://www.citilink.ru/product/videokarta-asus-tuf-rtx5070-2088716/",
            "title": "Видеокарта ASUS TUF RTX 5070",
            "price": "105500",
            "old_price_text": "114 000",
            "rating": "4.7",
            "reviews": "7",
            "buyable": True,
            "awaited": False,
        },
        {
            "url": "https://www.citilink.ru/product/videokarta-asus-prime-rtx5070ti-2078988/",
            "title": "Видеокарта ASUS PRIME RTX 5070 Ti",
            "price": None,
            "old_price_text": None,
            "rating": "5",
            "reviews": "2",
            "buyable": False,
            "awaited": True,
        },
    ],
}
NOTHING_PAYLOAD = {
    "title": "zxqvwj - купить по низкой цене в Ситилинк",
    "city": "Город по IP",
    "nothing": True,
    "items": [],
}
CARD_PAYLOAD = {
    "title": "Видеокарта Palit … – купить в Ситилинк | 2092141",
    "not_found": False,
    "city": "Город по IP",
    "product": {
        "id": "2092141",
        "name": "Видеокарта Palit NVIDIA  GeForce RTX 5070 PA-RTX5070 Infinity 3 12ГБ",
        "isAvailable": True,
        "price": {"current": "94560", "old": "99990", "club": "93000"},
        "brand": {"id": "PALIT", "name": "PALIT"},
        "rating": 4.9,
        "counters": {"opinions": 18, "reviews": 0},
    },
}


class FakePage:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def evaluate(self, expression, arg=None):
        return json.dumps(self.payload, ensure_ascii=False)


def fake_tab(monkeypatch, *outcomes) -> list[dict]:
    """Each outcome is a payload dict (the extractor's JSON) or a NavBlocked to raise."""
    calls: list[dict] = []
    queue = list(outcomes)

    @asynccontextmanager
    async def open_page(url, wait_ms=5000, **kwargs):
        calls.append({"url": url, **kwargs})
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield FakePage(outcome)

    monkeypatch.setattr(server, "open_page", open_page)
    return calls


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="citilink", gate=StopGate(None)))
    monkeypatch.setattr(server, "_min_gap", 0.0)
    monkeypatch.setattr(server, "_RENDER_WAIT_MS", 0)
    yield
    server._cache.clear()


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


# ------------------------------------------------------------------ search ----


async def test_search_opens_the_sites_search_with_the_429_grace(monkeypatch):
    calls = fake_tab(monkeypatch, SEARCH_PAYLOAD)

    result = await server.citilink_search("rtx  5070")

    assert calls[0]["url"] == "https://www.citilink.ru/search/?text=rtx%205070"
    assert 429 in calls[0]["challenge_statuses"]
    assert calls[0]["challenge_grace_s"] > 0
    assert (result.tier_used, result.city, result.count) == ("cdp", "Город по IP", 3)


async def test_search_rows_carry_price_discount_rating_and_stock(monkeypatch):
    fake_tab(monkeypatch, SEARCH_PAYLOAD)

    items = (await server.citilink_search("rtx 5070")).items

    assert [(i.price_rub, i.old_price_rub, i.rating, i.review_count, i.in_stock) for i in items] == [
        (94560.0, None, 4.9, 18, True),
        (105500.0, 114000.0, 4.7, 7, True),
        (None, None, 5.0, 2, False),
    ]
    assert (items[0].product_id, items[0].url) == (SLUG, CARD_URL)
    assert items[0].title == "Видеокарта Palit NVIDIA GeForce RTX 5070 PA-RTX5070 Infinity 3"


async def test_a_sold_out_row_without_price_is_not_a_warning(monkeypatch):
    fake_tab(monkeypatch, SEARCH_PAYLOAD)

    assert (await server.citilink_search("rtx 5070")).meta.warnings == []


async def test_a_buyable_row_without_price_is_reported(monkeypatch):
    payload = json.loads(json.dumps(SEARCH_PAYLOAD))
    payload["items"][0]["price"] = None
    fake_tab(monkeypatch, payload)

    assert (await server.citilink_search("rtx 5070")).meta.warnings == [f"{SLUG}: no_price"]


async def test_limit_cuts_the_first_page(monkeypatch):
    fake_tab(monkeypatch, SEARCH_PAYLOAD)

    assert (await server.citilink_search("rtx 5070", limit=1)).count == 1


async def test_nothing_found_is_an_honest_empty_result(monkeypatch):
    fake_tab(monkeypatch, NOTHING_PAYLOAD)

    result = await server.citilink_search("zxqvwj")

    assert result.count == 0
    assert result.meta.warnings == ["empty: Citilink found nothing for 'zxqvwj'"]


async def test_a_page_with_neither_results_nor_nothing_found_is_drift(monkeypatch):
    fake_tab(monkeypatch, {"title": TITLE, "city": None, "nothing": False, "items": []})

    with pytest.raises(ToolError) as excinfo:
        await server.citilink_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_a_second_search_comes_from_the_cache(monkeypatch):
    calls = fake_tab(monkeypatch, SEARCH_PAYLOAD)

    await server.citilink_search("rtx 5070")
    result = await server.citilink_search("rtx 5070")

    assert (result.tier_used, len(calls)) == ("cache", 1)


@pytest.mark.parametrize(
    "outcome",
    [NavBlocked(429), NavBlocked(403), {"title": "Загрузка...", "items": [], "nothing": False}],
    ids=["429 that stayed", "403", "loading page"],
)
async def test_a_refusal_stops_citilink_after_one_tab(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome, SEARCH_PAYLOAD)

    with pytest.raises(ToolError):
        await server.citilink_search("rtx 5070")
    with pytest.raises(ToolError) as excinfo:
        await server.citilink_search("rtx 5070")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(calls) == 1


async def test_a_server_error_is_not_a_stop(monkeypatch):
    calls = fake_tab(monkeypatch, NavBlocked(502), SEARCH_PAYLOAD)

    with pytest.raises(ToolError) as excinfo:
        await server.citilink_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.citilink_search("rtx 5070")).count == 3
    assert len(calls) == 2


# -------------------------------------------------------------------- card ----


async def test_card_reads_the_product_and_ignores_the_club_price(monkeypatch):
    calls = fake_tab(monkeypatch, CARD_PAYLOAD)

    card = await server.citilink_card(CARD_URL + "?utm_source=x#frag")

    assert calls[0]["url"] == CARD_URL
    assert (card.product_id, card.brand, card.city) == (SLUG, "PALIT", "Город по IP")
    assert (card.price_rub, card.old_price_rub, card.is_available) == (94560.0, 99990.0, True)
    assert (card.rating, card.review_count) == (4.9, 18)
    assert card.title == "Видеокарта Palit NVIDIA GeForce RTX 5070 PA-RTX5070 Infinity 3 12ГБ"
    assert card.meta.warnings == []


async def test_card_without_reviews_has_no_rating(monkeypatch):
    payload = json.loads(json.dumps(CARD_PAYLOAD))
    payload["product"]["counters"]["opinions"] = 0
    fake_tab(monkeypatch, payload)

    card = await server.citilink_card(CARD_URL)

    assert (card.rating, card.review_count) == (None, 0)


async def test_an_available_card_without_price_warns(monkeypatch):
    payload = json.loads(json.dumps(CARD_PAYLOAD))
    payload["product"]["price"] = {"current": "0", "old": "0"}
    fake_tab(monkeypatch, payload)

    card = await server.citilink_card(CARD_URL)

    assert card.price_rub is None
    assert card.meta.warnings == ["in_stock_no_price: the card is available but carries no price"]


async def test_an_unknown_product_is_not_found(monkeypatch):
    fake_tab(monkeypatch, {"title": "Страница не найдена – Ситилинк", "not_found": True, "city": None, "product": None})

    with pytest.raises(ToolError) as excinfo:
        await server.citilink_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "not_found"


async def test_a_card_that_opened_another_product_is_not_found(monkeypatch):
    payload = json.loads(json.dumps(CARD_PAYLOAD))
    payload["product"]["id"] = "2092508"
    fake_tab(monkeypatch, payload)

    with pytest.raises(ToolError) as excinfo:
        await server.citilink_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "not_found"


async def test_a_card_without_product_data_is_drift(monkeypatch):
    fake_tab(
        monkeypatch, {"title": "Видеокарта – купить в Ситилинк", "not_found": False, "city": None, "product": None}
    )

    with pytest.raises(ToolError) as excinfo:
        await server.citilink_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_card_accepts_a_bare_slug(monkeypatch):
    calls = fake_tab(monkeypatch, CARD_PAYLOAD)

    await server.citilink_card(SLUG)

    assert calls[0]["url"] == CARD_URL


# ------------------------------------------------------------- input guard ----

_OFF_HOST_URLS = [
    "https://evil.example/product/noutbuk-lenovo-2169270/",
    "https://citilink.ru.evil.example/product/noutbuk-lenovo-2169270/",
    "http://127.0.0.1:9222/product/noutbuk-lenovo-2169270/",
    "//evil.example/product/noutbuk-lenovo-2169270/",
    "file:///etc/passwd#/product/noutbuk-lenovo-2169270",
    "javascript:fetch('/product/noutbuk-lenovo-2169270')",
    "https://www.citilink.ru/catalog/noutbuki/",
]


@pytest.mark.parametrize("hostile", _OFF_HOST_URLS)
async def test_card_refuses_input_without_a_citilink_product(monkeypatch, hostile):
    calls = fake_tab(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.citilink_card(hostile)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert calls == []


@pytest.mark.parametrize(
    "hostile",
    ["/product/x?utm=1&redirect=evil", "/product/..", "/product/%2e%2e%2f%2e%2e", "/product/x#@evil.example"],
)
def test_an_id_never_carries_query_fragment_or_traversal(hostile):
    got = server._extract_product_id(hostile)
    assert got is None or all(ch not in got for ch in "?#%./\\")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.citilink.ru/product/noutbuk-lenovo-2169270/", "noutbuk-lenovo-2169270"),
        ("https://www.citilink.ru/product/noutbuk-lenovo-2169270/otzyvy/", "noutbuk-lenovo-2169270"),
        ("noutbuk-lenovo-2169270", "noutbuk-lenovo-2169270"),
    ],
)
def test_extract_product_id(raw, expected):
    assert server._extract_product_id(raw) == expected


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_healthy_after_search_and_card(monkeypatch):
    calls = fake_tab(monkeypatch, SEARCH_PAYLOAD, CARD_PAYLOAD)

    result = await server.citilink_selfcheck()

    assert result.status == "success"
    assert {name: check.state for name, check in result.checks.items()} == {"search": "healthy", "card": "healthy"}
    assert calls[1]["url"] == CARD_URL


async def test_selfcheck_refusal_is_inconclusive_and_skips_the_card(monkeypatch):
    calls = fake_tab(monkeypatch, NavBlocked(403))

    result = await server.citilink_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {
        "search": "inconclusive",
        "card": "inconclusive",
    }
    assert len(calls) == 1


async def test_selfcheck_drift_when_the_page_shape_moves(monkeypatch):
    fake_tab(monkeypatch, {"title": TITLE, "city": None, "nothing": False, "items": []})

    result = await server.citilink_selfcheck()

    assert result.checks["search"].state == "drift"
