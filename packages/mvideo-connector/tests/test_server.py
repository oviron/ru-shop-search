"""Offline tests for the M.Video connector: HTTP stubbed at curl_cffi, the Chrome tab faked at ``open_page``."""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from mcp_core.domtest import JsdomUnavailable, run_extractor
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport import impersonated
from mcp_core.transport.chrome_cdp import NavBlocked
from mcp_core.transport.impersonated import Reply
from mvideo_connector import server

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "search_rtx5070_live.json").read_text(encoding="utf-8"))
CARD_URL = "https://www.mvideo.ru/products/400678996"
SALE_URL = "https://www.mvideo.ru/products/400535386"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="mvideo", gate=StopGate(None)))
    monkeypatch.setattr(server, "_RENDER_WAIT_MS", 0)
    yield
    server._cache.clear()


def stub_http(monkeypatch, *replies: Reply) -> list[dict]:
    calls: list[dict] = []
    queue = list(replies)

    async def fake_post(url, body, **kwargs):
        calls.append({"url": url, "body": json.loads(body), **kwargs})
        return queue.pop(0)

    monkeypatch.setattr(impersonated, "impersonated_post", fake_post)
    return calls


def ok(data: dict) -> Reply:
    return Reply(200, json.dumps(data, ensure_ascii=False), None)


def search_with(edit) -> dict:
    data = copy.deepcopy(SEARCH)
    edit(data["body"])
    return data


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


def page_payload(name: str, url: str) -> dict:
    try:
        return run_extractor(server._CARD_EXTRACT_JS, FIXTURES / name, page_url=url)
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))


class FakePage:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def evaluate(self, expression, arg=None):
        return json.dumps(self.payload, ensure_ascii=False)


def fake_tab(monkeypatch, *outcomes) -> list[dict]:
    """Each outcome is the extractor's JSON as a dict, or a NavBlocked to raise."""
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


# ------------------------------------------------------------------ search ----


async def test_search_posts_the_sites_own_query_with_moscow_cookies(monkeypatch):
    calls = stub_http(monkeypatch, ok(SEARCH))

    result = await server.mvideo_search("rtx  5070")

    assert calls[0]["url"] == "https://www.mvideo.ru/bff/products"
    assert calls[0]["body"] == {
        "limit": 60,
        "cursorId": "",
        "enrich": True,
        "filters": [],
        "sortBy": "popularity",
        "sortDirection": "desc",
        "isGettingBonusRoubles": True,
        "query": "rtx 5070",
    }
    assert calls[0]["cookies"]["MVID_CITY_ID"] == "CityCZ_975"
    assert calls[0]["headers"]["Referer"] == "https://www.mvideo.ru/search?q=rtx%205070"
    assert (result.tier_used, result.city, result.total_available, result.count) == ("http", "Москва", 12, 12)
    assert result.meta.warnings == []


async def test_search_rows_carry_sale_price_crossed_price_rating_and_stock(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH))

    items = (await server.mvideo_search("rtx 5070", limit=3)).items

    assert [(i.product_id, i.price_rub, i.old_price_rub, i.rating, i.review_count, i.in_stock) for i in items] == [
        ("400678996", 84999.0, None, 5.0, 1, True),
        ("400535386", 126350.0, 132999.0, 5.0, 28, True),
        ("401420937", 103000.0, 126355.0, None, 0, True),
    ]
    assert items[0].title == "Видеокарта Palit NVIDIA GeForce RTX 5070 INFINITY 3 OC 12GB [ne75070s19k9-gb2050s]"
    assert (items[0].brand, items[0].url, items[0].marketplace) == ("Palit", CARD_URL, False)


async def test_a_marketplace_row_is_marked(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH))

    items = (await server.mvideo_search("rtx 5070", limit=60)).items

    partner = [i for i in items if i.marketplace]
    assert [(i.product_id, i.title, i.price_rub) for i in partner] == [
        ("402590260", "(МП)GA GIGABYTE GeForce RTX 5070 GAMING", 139999.0)
    ]


async def test_sold_out_and_preorder_rows(monkeypatch):
    def edit(body):
        body["items"][0].update(soldOut=True, status="notAvailable", price={})
        body["items"][1].update(isPreorder=True)

    stub_http(monkeypatch, ok(search_with(edit)))

    result = await server.mvideo_search("rtx 5070", limit=2)

    assert [(i.in_stock, i.price_rub) for i in result.items] == [(False, None), (None, 126350.0)]
    assert result.meta.warnings == []


async def test_a_row_without_price_or_id_is_reported(monkeypatch):
    def edit(body):
        body["items"][0]["price"] = {"salePrice": 0}
        del body["items"][1]["productId"]

    stub_http(monkeypatch, ok(search_with(edit)))

    result = await server.mvideo_search("rtx 5070", limit=3)

    assert [i.product_id for i in result.items] == ["400678996", "401420937"]
    assert result.items[0].price_rub is None
    assert result.meta.warnings == ["400678996: no_price", "row_skipped: a search row came without a productId"]


async def test_nothing_found_is_an_honest_empty_result(monkeypatch):
    stub_http(monkeypatch, ok(search_with(lambda body: body.update(items=[], total=0))))

    result = await server.mvideo_search("zxqvwj")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: M.Video found nothing for 'zxqvwj'"]


async def test_an_empty_204_is_an_honest_empty_result(monkeypatch):
    stub_http(monkeypatch, Reply(204, "", None))

    result = await server.mvideo_search("zxqvwj pplkq")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: M.Video found nothing for 'zxqvwj pplkq'"]


async def test_a_second_search_is_served_from_cache(monkeypatch):
    calls = stub_http(monkeypatch, ok(SEARCH))

    await server.mvideo_search("rtx 5070")
    result = await server.mvideo_search("rtx 5070")

    assert (len(calls), result.tier_used) == (1, "cache")


@pytest.mark.parametrize(
    "reply",
    [
        Reply(200, json.dumps({"success": True, "body": {"total": 0}}), None),
        Reply(200, json.dumps({"success": True}), None),
        Reply(200, "not json", None),
    ],
    ids=["no items", "no body", "not json"],
)
async def test_a_moved_answer_is_drift(monkeypatch, reply):
    stub_http(monkeypatch, reply)

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_a_server_error_is_a_transport_error_not_a_stop(monkeypatch):
    calls = stub_http(monkeypatch, Reply(500, "", None), ok(SEARCH))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.mvideo_search("rtx 5070")).count == 12
    assert len(calls) == 2


async def test_a_page_of_rows_without_prices_is_flagged(monkeypatch):
    def edit(body):
        for row in body["items"]:
            row["price"] = {}

    stub_http(monkeypatch, ok(search_with(edit)))

    result = await server.mvideo_search("rtx 5070", limit=3)

    assert [i.price_rub for i in result.items] == [None, None, None]
    assert result.meta.warnings[0] == "no_prices_on_page: search rows came back without prices"


async def test_success_false_is_a_transport_error_not_a_stop(monkeypatch):
    calls = stub_http(monkeypatch, ok({"success": False, "messages": ["oops"], "body": {}}), ok(SEARCH))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.mvideo_search("rtx 5070")).count == 12
    assert len(calls) == 2


@pytest.mark.parametrize(
    "reply",
    [
        Reply(403, "<html><title>Главная</title></html>", None),
        Reply(429, "", None),
        Reply(302, "", None, location="/showcaptcha"),
        Reply(200, "<!DOCTYPE html><html><title>Главная</title></html>", None),
    ],
    ids=["403", "429", "redirect", "html page"],
)
async def test_a_refusal_stops_mvideo_after_one_request(monkeypatch, reply):
    calls = stub_http(monkeypatch, reply, ok(SEARCH))

    with pytest.raises(ToolError):
        await server.mvideo_search("rtx 5070")
    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_search("rtx 4070")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("reply", "status", "challenge_type"),
    [
        (Reply(302, "", None, location="/showcaptcha"), 302, "redirect"),
        (Reply(200, "<html></html>", None), 200, "antibot_page"),
    ],
)
async def test_a_refusal_names_what_mvideo_answered(monkeypatch, reply, status, challenge_type):
    stub_http(monkeypatch, reply)

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_search("rtx 5070")
    payload = error_payload(excinfo.value)
    assert (payload["error"], payload["status_code"], payload["challenge_type"]) == (
        "challenge_required",
        status,
        challenge_type,
    )


# -------------------------------------------------------------------- card ----


async def test_card_reads_the_product_page_in_the_tab(monkeypatch):
    calls = fake_tab(monkeypatch, page_payload("card_palit_live.html", CARD_URL))

    card = await server.mvideo_card("400678996")

    assert calls == [{"url": CARD_URL}]
    assert (card.product_id, card.price_rub, card.old_price_rub, card.is_available) == (
        "400678996",
        84999.0,
        None,
        True,
    )
    assert (card.brand, card.rating, card.review_count, card.marketplace) == ("Palit", 5.0, 1, False)
    assert card.title == "Видеокарта Palit NVIDIA GeForce RTX 5070 INFINITY 3 OC 12GB [ne75070s19k9-gb2050s]"
    assert (card.tier_used, card.url, card.meta.warnings) == ("cdp", CARD_URL, [])


async def test_card_reads_the_crossed_out_price(monkeypatch):
    fake_tab(monkeypatch, page_payload("card_palit_sale_live.html", SALE_URL))

    card = await server.mvideo_card(SALE_URL)

    assert (card.price_rub, card.old_price_rub, card.review_count) == (126350.0, 132999.0, 28)


def test_the_tab_hands_out_only_product_fields():
    payload = page_payload("card_palit_live.html", CARD_URL)

    assert set(payload) == {"title", "h1", "product", "price_text", "old_price_text"}
    assert set(payload["product"]) == {"sku", "name", "brand", "price", "currency", "availability", "rating", "reviews"}


@pytest.mark.parametrize(
    "product",
    [
        "https://www.mvideo.ru/products/videokarta-palit-geforce-rtx-5070-infinity-3-oc-12gb-400678996",
        "www.mvideo.ru/products/400678996/",
        "https://www.mvideo.ru/products/400678996?utm_source=share&next=/bff/profile",
        " 400678996 ",
    ],
)
async def test_card_accepts_a_url_or_an_id(monkeypatch, product):
    calls = fake_tab(monkeypatch, page_payload("card_palit_live.html", CARD_URL))

    await server.mvideo_card(product)

    assert calls[0]["url"] == CARD_URL


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/products/400678996",
        "https://www.mvideo.ru.evil.example/products/400678996",
        "https://www.mvideo.ru/bff/pdp/info?productId=400678996",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "4006",
        "palit",
    ],
)
async def test_card_refuses_input_without_an_mvideo_product(monkeypatch, bad):
    calls = fake_tab(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card(bad)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert calls == []


async def test_a_page_of_another_product_is_not_found(monkeypatch):
    fake_tab(monkeypatch, page_payload("card_palit_sale_live.html", SALE_URL))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400678996")
    assert error_payload(excinfo.value)["error"] == "not_found"


async def test_a_not_found_page_is_not_found_and_not_a_stop(monkeypatch):
    missing = {"title": "Страница не найдена", "h1": None, "product": None, "price_text": None}
    fake_tab(monkeypatch, missing, page_payload("card_palit_live.html", CARD_URL))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400000001")
    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.mvideo_card("400678996")).price_rub == 84999.0


async def test_a_shop_page_without_product_is_drift(monkeypatch):
    fake_tab(monkeypatch, {"title": "Купить в М.Видео", "h1": "Видеокарта", "product": None})

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400678996")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


@pytest.mark.parametrize(
    "outcome",
    [NavBlocked(403, CARD_URL), NavBlocked(429, CARD_URL), {"title": "Главная", "h1": None, "product": None}],
    ids=["403", "429", "block page"],
)
async def test_a_refused_tab_stops_mvideo_search_too(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome)
    http = stub_http(monkeypatch, ok(SEARCH))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400678996")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_search("rtx 5070")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert (len(calls), http) == (1, [])


async def test_a_server_error_in_the_tab_is_not_a_stop(monkeypatch):
    fake_tab(monkeypatch, NavBlocked(502, CARD_URL), page_payload("card_palit_live.html", CARD_URL))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400678996")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.mvideo_card("400678996")).price_rub == 84999.0


@pytest.mark.parametrize("fault", [RuntimeError("CDP Page.navigate: net::ERR_ABORTED"), TimeoutError()])
async def test_a_broken_tab_is_a_transport_error_not_a_stop(monkeypatch, fault):
    fake_tab(monkeypatch, fault, page_payload("card_palit_live.html", CARD_URL))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400678996")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.mvideo_card("400678996")).price_rub == 84999.0


async def test_a_stopped_mvideo_opens_no_tab(monkeypatch):
    server._pacer.record_refusal("http 403")
    calls = fake_tab(monkeypatch, page_payload("card_palit_live.html", CARD_URL))

    with pytest.raises(ToolError) as excinfo:
        await server.mvideo_card("400678996")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert calls == []


async def test_a_foreign_currency_offer_is_not_a_price(monkeypatch):
    payload = page_payload("card_palit_live.html", CARD_URL)
    payload["product"]["currency"] = "USD"
    payload["price_text"] = None
    fake_tab(monkeypatch, payload)

    card = await server.mvideo_card("400678996")

    assert card.price_rub is None
    assert card.meta.warnings[0] == "price_skipped: the page prices in 'USD'"


async def test_the_shown_price_wins_over_a_stale_offer_and_is_reported(monkeypatch):
    payload = page_payload("card_palit_live.html", CARD_URL)
    payload["price_text"] = "82 999 ₽"
    fake_tab(monkeypatch, payload)

    card = await server.mvideo_card("400678996")

    assert card.price_rub == 82999.0
    assert card.meta.warnings == ["price_mismatch: the page shows 82999 ₽, its schema.org offer 84999 ₽"]


async def test_a_sold_out_card_is_not_available(monkeypatch):
    payload = page_payload("card_palit_live.html", CARD_URL)
    payload["product"]["availability"] = "https://schema.org/OutOfStock"
    payload["price_text"] = None
    fake_tab(monkeypatch, payload)

    card = await server.mvideo_card("400678996")

    assert (card.is_available, card.price_rub, card.meta.warnings) == (False, 84999.0, [])


async def test_an_in_stock_card_without_price_is_reported(monkeypatch):
    payload = page_payload("card_palit_live.html", CARD_URL)
    payload["product"]["price"] = None
    payload["price_text"] = None
    fake_tab(monkeypatch, payload)

    card = await server.mvideo_card("400678996")

    assert card.price_rub is None
    assert card.meta.warnings == ["in_stock_no_price: the card is available but carries no price"]


async def test_a_second_card_is_served_from_cache(monkeypatch):
    calls = fake_tab(monkeypatch, page_payload("card_palit_live.html", CARD_URL))

    await server.mvideo_card("400678996")
    card = await server.mvideo_card(CARD_URL)

    assert (len(calls), card.tier_used) == (1, "cache")


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_healthy_after_search_and_card(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH))
    calls = fake_tab(monkeypatch, page_payload("card_palit_live.html", CARD_URL))

    result = await server.mvideo_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {"search": "healthy", "card": "healthy"}
    assert calls[0]["url"] == CARD_URL


async def test_selfcheck_refusal_is_inconclusive_and_skips_the_card(monkeypatch):
    http = stub_http(monkeypatch, Reply(403, "", None))
    calls = fake_tab(monkeypatch)

    result = await server.mvideo_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {
        "search": "inconclusive",
        "card": "inconclusive",
    }
    assert (len(http), calls) == (1, [])


async def test_selfcheck_drift_when_the_answer_moves(monkeypatch):
    stub_http(monkeypatch, ok({"success": True, "body": {}}))

    result = await server.mvideo_selfcheck()

    assert result.checks["search"].state == "drift"
