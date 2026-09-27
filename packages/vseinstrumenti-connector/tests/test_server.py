"""Offline tests for the VseInstrumenti connector: live pages from fixtures, the network stubbed at curl_cffi."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport import impersonated
from mcp_core.transport.impersonated import Reply
from vseinstrumenti_connector import server

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "search_makita_live.html").read_text(encoding="utf-8")
CARD_HTML = (FIXTURES / "card_makita_live.html").read_text(encoding="utf-8")
NOT_FOUND_HTML = (FIXTURES / "card_not_found_live.html").read_text(encoding="utf-8")
CARD_URL = "https://www.vseinstrumenti.ru/product/perforator-makita-hr-2470-5195/"
SERVICEPIPE_403 = (
    '<html><head><script src="https://x.servicepipe.tech/loaders/a.js"></script></head>'
    '<body><div class="spinner-container"></div></body></html>'
)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="vseinstrumenti", gate=StopGate(None)))
    yield
    server._cache.clear()


def stub_http(monkeypatch, *replies: Reply) -> list[str]:
    urls: list[str] = []
    queue = list(replies)

    async def fake_get(url, **kwargs):
        urls.append(url)
        return queue.pop(0)

    monkeypatch.setattr(impersonated, "impersonated_get", fake_get)
    return urls


def ok(html: str) -> Reply:
    return Reply(200, html, None)


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


def edited(html: str, edit) -> str:
    """The page with its devalue array edited in place (entries, not the rebuilt tree)."""
    match = server._NUXT_DATA_RE.search(html)
    values = json.loads(match.group(1))
    edit(values)
    return html[: match.start(1)] + json.dumps(values, ensure_ascii=False) + html[match.end(1) :]


def entry(values: list, **fields) -> dict:
    """The first devalue object whose listed fields point at the given literal values."""
    for value in values:
        if isinstance(value, dict) and all(k in value and values[value[k]] == v for k, v in fields.items()):
            return value
    raise AssertionError(f"no entry with {fields}")


# ---------------------------------------------------------------- devalue ----


def test_devalue_follows_refs_tags_and_cycles():
    values = [
        ["ShallowReactive", 1],
        {"name": 2, "tags": 3, "self": 1, "error": 5, "gone": -1, "odd": 7},
        "Перфоратор",
        ["Set", 4],
        "sale",
        ["null", "url", 6],
        "/product/x-1/",
        ["RegExp", "a", "g"],
    ]

    data = server._unflatten(values)

    assert data["name"] == "Перфоратор"
    assert data["tags"] == ["sale"]
    assert data["self"] is data
    assert data["error"] == {"url": "/product/x-1/"}
    assert (data["gone"], data["odd"]) == (None, None)


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no payload</body></html>",
        '<script id="__NUXT_DATA__" type="application/json">not json</script>',
        '<script id="__NUXT_DATA__" type="application/json">{"a": 1}</script>',
    ],
    ids=["no script", "not json", "not an array"],
)
def test_a_page_without_a_readable_payload_has_no_data(html):
    assert server._nuxt_data(html) is None


@pytest.mark.parametrize(
    "payload",
    [[["Ref", i + 1] for i in range(3000)] + ["leaf"], "[" * 100_000 + "]" * 100_000],
    ids=["chain of refs", "nested arrays"],
)
async def test_a_payload_too_deep_to_read_is_drift_not_a_crash(monkeypatch, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    html = f'<script id="__NUXT_DATA__" type="application/json">{text}</script>'
    urls = stub_http(monkeypatch, ok(html), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("перфоратор makita")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.vseinstrumenti_search("перфоратор makita")).count == 6
    assert len(urls) == 2


# ------------------------------------------------------------------ search ----


async def test_search_reads_the_sites_own_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.vseinstrumenti_search("перфоратор  makita")

    assert urls == [
        "https://www.vseinstrumenti.ru/search/?what=%D0%BF%D0%B5%D1%80%D1%84%D0%BE%D1%80%D0%B0%D1%82%D0%BE%D1%80%20makita"
    ]
    assert (result.tier_used, result.city, result.total_available, result.count) == (
        "http",
        "Санкт-Петербург",
        138,
        6,
    )
    assert result.meta.warnings == []


async def test_search_rows_carry_price_crossed_price_rating_and_stock(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.vseinstrumenti_search("перфоратор makita")

    assert [
        (i.product_id, i.price_rub, i.old_price_rub, i.rating, i.review_count, i.in_stock) for i in result.items
    ] == [
        ("5195", 12500.0, 15240.0, 4.7, 2475, True),
        ("814840", 14490.0, 17875.0, 4.7, 245, True),
        ("22025396", 10290.0, None, 3.7, 6, True),
        ("713544", 32835.0, None, 4.7, 65, True),
        ("18915549", 5440.0, 6240.0, 4.9, 22, True),
        ("772527", 69537.0, 77590.0, 4.7, 28, True),
    ]
    assert (result.items[0].title, result.items[0].url) == ("Перфоратор Makita HR2470", CARD_URL)


async def test_a_b2b_discount_price_is_not_the_price(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.vseinstrumenti_search("перфоратор makita")

    bort = next(i for i in result.items if i.product_id == "18915549")
    assert (bort.price_rub, bort.old_price_rub) == (5440.0, 6240.0)


async def test_a_one_word_query_follows_the_sites_category_redirect(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(
            302,
            "",
            None,
            location="https://www.vseinstrumenti.ru/category/perforatory-32/#searchQuery=%D0%BF&searchType=redirect",
        ),
        ok(SEARCH_HTML),
    )

    result = await server.vseinstrumenti_search("перфоратор")

    assert urls[1] == "https://www.vseinstrumenti.ru/category/perforatory-32/"
    assert result.count == 6
    assert result.meta.warnings == ["category: VseInstrumenti sent the query to its category /category/perforatory-32/"]


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/category/perforatory-32/", "/lk/login/", "https://www.vseinstrumenti.ru/product/x-1/"],
    ids=["foreign host", "not a category", "a product"],
)
async def test_any_other_search_redirect_stops_vseinstrumenti(monkeypatch, location):
    urls = stub_http(monkeypatch, Reply(302, "", None, location=location), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("перфоратор")
    payload = error_payload(excinfo.value)
    assert (payload["error"], payload["challenge_type"]) == ("challenge_required", "redirect")
    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("дрель")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == 1


async def test_nothing_found_is_an_honest_empty_result(monkeypatch):
    def drop_products(values):
        store = next(v for v in values if isinstance(v, dict) and "productsCount" in v)
        values.extend([{}, 0])
        store["products"], store["productsCount"] = len(values) - 2, len(values) - 1

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, drop_products)))

    result = await server.vseinstrumenti_search("zxqvwj")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: VseInstrumenti found nothing for 'zxqvwj'"]


async def test_a_row_without_price_or_link_is_reported(monkeypatch):
    def edit(values):
        first = entry(values, name="Перфоратор Makita HR2470")
        values[first["pricesV2"]] = {}
        second = entry(values, name="Перфоратор Makita HR2470x15")
        del second["link"]

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.vseinstrumenti_search("перфоратор makita", limit=3)

    assert [(i.product_id, i.price_rub) for i in result.items] == [("5195", None), ("22025396", 10290.0)]
    assert result.meta.warnings == ["5195: no_price", "row_skipped: a search row came without an id or product link"]


async def test_a_page_of_rows_without_prices_is_flagged(monkeypatch):
    def edit(values):
        for value in values:
            if isinstance(value, dict) and "pricesV2" in value:
                values[value["pricesV2"]] = {}

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.vseinstrumenti_search("перфоратор makita", limit=2)

    assert result.meta.warnings[0] == "no_prices_on_page: search rows came back without prices"


async def test_a_second_search_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    await server.vseinstrumenti_search("перфоратор makita")
    result = await server.vseinstrumenti_search("перфоратор makita")

    assert (len(urls), result.tier_used, result.count) == (1, "cache", 6)


async def test_a_listing_that_moved_is_drift(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML.replace('"ListingStore"', '"CatalogStore"')))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("перфоратор makita")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_a_page_without_payload_is_drift_not_a_stop(monkeypatch):
    urls = stub_http(monkeypatch, ok("<html><title>ВсеИнструменты</title></html>"), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("перфоратор makita")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.vseinstrumenti_search("перфоратор makita")).count == 6
    assert len(urls) == 2


@pytest.mark.parametrize(
    "reply",
    [Reply(403, SERVICEPIPE_403, None), Reply(429, "", None), ok(SERVICEPIPE_403)],
    ids=["servicepipe 403", "429", "servicepipe page on 200"],
)
async def test_a_refusal_stops_vseinstrumenti_after_one_request(monkeypatch, reply):
    urls = stub_http(monkeypatch, reply, ok(SEARCH_HTML))

    with pytest.raises(ToolError):
        await server.vseinstrumenti_search("перфоратор makita")
    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_card(CARD_URL)
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == 1


async def test_a_servicepipe_page_on_200_is_named(monkeypatch):
    stub_http(monkeypatch, ok(SERVICEPIPE_403))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("перфоратор makita")
    payload = error_payload(excinfo.value)
    assert (payload["error"], payload["status_code"], payload["challenge_type"]) == (
        "challenge_required",
        200,
        "antibot_page",
    )


async def test_a_server_error_is_a_transport_error_not_a_stop(monkeypatch):
    urls = stub_http(monkeypatch, Reply(500, "", None), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_search("перфоратор makita")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.vseinstrumenti_search("перфоратор makita")).count == 6
    assert len(urls) == 2


# -------------------------------------------------------------------- card ----


async def test_card_reads_the_product_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.vseinstrumenti_card(CARD_URL)

    assert urls == [CARD_URL]
    assert (card.product_id, card.title, card.brand) == ("5195", "Перфоратор Makita HR2470", "Makita")
    assert (card.price_rub, card.old_price_rub, card.rating, card.review_count) == (12500.0, 15240.0, 4.7, 2475)
    assert (card.is_available, card.city, card.url, card.tier_used) == (True, "Санкт-Петербург", CARD_URL, "http")
    assert card.meta.warnings == []


@pytest.mark.parametrize(
    "given",
    [
        CARD_URL,
        "https://www.vseinstrumenti.ru/product/perforator-makita-hr-2470-5195",
        "https://www.vseinstrumenti.ru/product/perforator-makita-hr-2470-5195/?utm_source=x#reviews",
        "http://vseinstrumenti.ru/product/perforator-makita-hr-2470-5195/",
        "www.vseinstrumenti.ru/product/perforator-makita-hr-2470-5195/",
        "/product/perforator-makita-hr-2470-5195/",
    ],
    ids=["canonical", "no slash", "query and fragment", "bare host", "no scheme", "path"],
)
async def test_card_accepts_a_product_url_or_path(monkeypatch, given):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.vseinstrumenti_card(given)

    assert (urls, card.product_id) == ([CARD_URL], "5195")


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/product/perforator-makita-hr-2470-5195/",
        "https://www.vseinstrumenti.ru.evil.example/product/perforator-makita-hr-2470-5195/",
        "https://www.vseinstrumenti.ru/category/perforatory-32/",
        "https://www.vseinstrumenti.ru/product/perforator-makita/",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "перфоратор makita",
    ],
)
async def test_card_refuses_input_that_is_not_a_product_page(monkeypatch, bad):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_card(bad)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert urls == []


async def test_a_bare_id_is_refused_with_a_hint(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_card("5195")
    payload = error_payload(excinfo.value)
    assert payload["error"] == "bad_request"
    assert "product URL" in payload["message"]
    assert urls == []


async def test_a_missing_page_is_not_found_and_not_a_stop(monkeypatch):
    urls = stub_http(monkeypatch, Reply(404, NOT_FOUND_HTML, None), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_card("https://www.vseinstrumenti.ru/product/nothing-here-999999/")
    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.vseinstrumenti_card(CARD_URL)).price_rub == 12500.0
    assert len(urls) == 2


async def test_a_redirect_to_the_same_product_is_followed_once(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(301, "", None, location="/product/perforator-makita-hr2470-5195/"),
        ok(CARD_HTML),
    )

    card = await server.vseinstrumenti_card(CARD_URL)

    assert urls == [CARD_URL, "https://www.vseinstrumenti.ru/product/perforator-makita-hr2470-5195/"]
    assert (card.product_id, card.url) == ("5195", urls[1])


@pytest.mark.parametrize(
    "location",
    ["/product/perforator-makita-hr2470x15-814840/", "https://evil.example/product/x-5195/", "/showcaptcha"],
    ids=["another product", "foreign host", "not a product"],
)
async def test_any_other_card_redirect_stops_vseinstrumenti(monkeypatch, location):
    urls = stub_http(monkeypatch, Reply(302, "", None, location=location), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_card(CARD_URL)
    assert error_payload(excinfo.value)["challenge_type"] == "redirect"
    assert len(urls) == 1


async def test_a_page_of_another_product_is_drift(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.vseinstrumenti_card("https://www.vseinstrumenti.ru/product/perforator-makita-hr2470x15-814840/")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_the_page_data_price_is_checked_against_the_schema_offer(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('"price": 12500', '"price": 12900')))

    card = await server.vseinstrumenti_card(CARD_URL)

    assert card.price_rub == 12500.0
    assert card.meta.warnings == ["price_mismatch: the page data says 12500 ₽, its schema.org offer 12900 ₽"]


async def test_a_withdrawn_product_is_not_available(monkeypatch):
    def edit(values):
        info = entry(values, name="Перфоратор Makita HR2470")
        values.append(True)
        info["productWithdrawnFromSale"] = len(values) - 1
        prices = values[values[info["pricesV2"]]["default"]]
        values.append(None)
        prices["current"] = len(values) - 1

    stub_http(monkeypatch, ok(edited(CARD_HTML, edit)))

    card = await server.vseinstrumenti_card(CARD_URL)

    assert (card.is_available, card.price_rub, card.old_price_rub, card.meta.warnings) == (False, None, None, [])


async def test_an_available_card_without_price_is_reported(monkeypatch):
    def edit(values):
        info = entry(values, name="Перфоратор Makita HR2470")
        values[values[info["pricesV2"]]["default"]] = {}

    stub_http(monkeypatch, ok(edited(CARD_HTML, edit)))

    card = await server.vseinstrumenti_card(CARD_URL)

    assert card.price_rub is None
    assert card.meta.warnings == ["no_price: the product page carries no price"]


async def test_a_second_card_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    await server.vseinstrumenti_card(CARD_URL)
    card = await server.vseinstrumenti_card("/product/perforator-makita-hr-2470-5195/")

    assert (len(urls), card.tier_used, card.price_rub) == (1, "cache", 12500.0)


def test_fixtures_carry_no_visitor_or_author_data():
    for page in (SEARCH_HTML, CARD_HTML, NOT_FOUND_HTML):
        assert not re.search(r"author|userId|mkId|person-name|SigmaUserData|spid", page)


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_healthy_after_search_and_card(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML), ok(CARD_HTML))

    result = await server.vseinstrumenti_selfcheck()

    assert result.status == "success"
    assert {name: check.state for name, check in result.checks.items()} == {"search": "healthy", "card": "healthy"}
    assert urls[1] == CARD_URL


async def test_selfcheck_refusal_is_inconclusive_and_skips_the_card(monkeypatch):
    urls = stub_http(monkeypatch, Reply(403, SERVICEPIPE_403, None))

    result = await server.vseinstrumenti_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {
        "search": "inconclusive",
        "card": "inconclusive",
    }
    assert len(urls) == 1


async def test_selfcheck_drift_when_the_page_moves(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML.replace('"ListingStore"', '"CatalogStore"')))

    result = await server.vseinstrumenti_selfcheck()

    assert result.checks["search"].state == "drift"
