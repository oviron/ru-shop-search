"""Offline tests for the Regard connector: live pages from fixtures, the network stubbed at curl_cffi."""

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
from regard_connector import server

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "search_rtx5070_live.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIXTURES / "search_empty_live.html").read_text(encoding="utf-8")
CARD_HTML = (FIXTURES / "card_palit_live.html").read_text(encoding="utf-8")
CATEGORY_HTML = (FIXTURES / "category_videokarty_live.html").read_text(encoding="utf-8")
CATEGORY_LOCATION = "/catalog/1013/videokarty?q=eyJzaG93SGlkZGVuIjp0cnVlfQ&search=%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE%D0%BA%D0%B0%D1%80%D1%82%D0%B0&searchStep=2"
SLUG = "videokarta-nvidia-geforce-rtx-5070-palit-infinity-3-12gb-ne75070019k9-gb2050s"
CARD_URL = f"https://www.regard.ru/product/737606/{SLUG}"
QRATOR_PAGE = "<html><head><title>Qrator</title></head><body>Доступ ограничен</body></html>"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="regard", gate=StopGate(None)))
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
    """The page with its __NEXT_DATA__ edited in place."""
    match = server._NEXT_DATA_RE.search(html)
    data = json.loads(match.group(1))
    edit(data["props"]["initialState"])
    return html[: match.start(1)] + json.dumps(data, ensure_ascii=False) + html[match.end(1) :]


def search_rows(state: dict) -> list[dict]:
    return state["listing"]["data"]["bySearch"]["pages"]["0"]["data"]


# ----------------------------------------------------------------- search ----


async def test_search_reads_the_sites_own_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.regard_search("  rtx   5070 ")

    assert urls == ["https://www.regard.ru/catalog?search=rtx%205070"]
    assert (result.query, result.city, result.total_available, result.count, result.tier_used) == (
        "rtx 5070",
        "Москва",
        37,
        6,
        "http",
    )
    assert result.meta.warnings == []


async def test_search_rows_carry_price_rating_brand_and_part_number(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.regard_search("rtx 5070")

    rows = {
        it.product_id: (it.price_rub, it.old_price_rub, it.rating, it.review_count, it.in_stock) for it in result.items
    }
    assert rows == {
        "737606": (89990.0, None, 5.0, 3, True),
        "730439": (146870.0, None, 5.0, 67, True),
        "733488": (107470.0, None, 4.8, 125, True),
        "739366": (91990.0, None, None, 0, True),
        "729177": (166560.0, None, 5.0, 67, True),
        "753863": (98930.0, None, 4.5, 2, True),
    }
    first = result.items[0]
    assert first.title == "Видеокарта NVIDIA GeForce RTX 5070 Palit Infinity 3 12GB (NE75070019K9-GB2050S)"
    assert (first.brand, first.part_number, first.url) == ("Palit", "NE75070019K9-GB2050S", CARD_URL)


async def test_the_crossed_out_price_counts_only_above_the_price(monkeypatch):
    def edit(state):
        rows = search_rows(state)
        rows[0]["price_old"] = 99990
        rows[1]["price_old"] = 100

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.regard_search("rtx 5070")

    assert [it.old_price_rub for it in result.items[:2]] == [99990.0, None]


async def test_a_bonus_or_markdown_price_is_not_the_price(monkeypatch):
    def edit(state):
        search_rows(state)[0].update(price=None, bonus=720, discounted_price=70000)

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.regard_search("rtx 5070")

    assert result.items[0].price_rub is None
    assert "737606: no_price" in result.meta.warnings


@pytest.mark.parametrize(("max_day", "preorder"), [(None, 0), (1, 1), (True, 0), (-1, 0)])
async def test_stock_is_unknown_without_a_plain_delivery_term(monkeypatch, max_day, preorder):
    def edit(state):
        search_rows(state)[0].update(max_day=max_day, preorder=preorder)

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.regard_search("rtx 5070")

    assert result.items[0].in_stock is None


@pytest.mark.parametrize("stars", [True, "4.5", None])
async def test_a_rating_that_is_not_a_number_is_dropped(monkeypatch, stars):
    def edit(state):
        search_rows(state)[0].update(reviews_stars=stars, reviews_count=3)

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.regard_search("rtx 5070")

    assert (result.items[0].rating, result.items[0].review_count) == (None, 3)


async def test_a_one_word_query_follows_the_sites_category_redirect(monkeypatch):
    urls = stub_http(monkeypatch, Reply(301, "", None, location=CATEGORY_LOCATION), ok(CATEGORY_HTML))

    result = await server.regard_search("видеокарта")

    assert urls == [
        "https://www.regard.ru/catalog?search=%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE%D0%BA%D0%B0%D1%80%D1%82%D0%B0",
        f"https://www.regard.ru{CATEGORY_LOCATION}",
    ]
    assert (result.total_available, result.city) == (4549, "Москва")
    assert [(it.product_id, it.price_rub, it.in_stock) for it in result.items] == [
        ("750211", 52990.0, True),
        ("737606", 89990.0, True),
        ("720863", 19680.0, True),
        ("488227", 48150.0, True),
    ]
    assert result.meta.warnings == ["category: Regard sent the query to its category /catalog/1013/videokarty"]


async def test_a_category_page_without_its_listing_is_drift(monkeypatch):
    stub_http(
        monkeypatch,
        Reply(301, "", None, location="/catalog/1014/protsessory?search=x"),
        ok(CATEGORY_HTML),
    )

    with pytest.raises(ToolError) as excinfo:
        await server.regard_search("процессор")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_a_category_that_redirects_again_stops_regard(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(301, "", None, location=CATEGORY_LOCATION),
        Reply(301, "", None, location="/catalog/1013/videokarty"),
    )

    with pytest.raises(ToolError) as excinfo:
        await server.regard_search("видеокарта")
    assert error_payload(excinfo.value)["challenge_type"] == "redirect"
    assert len(urls) == 2


async def test_nothing_found_is_an_honest_empty_result(monkeypatch):
    stub_http(monkeypatch, ok(EMPTY_HTML))

    result = await server.regard_search("qzxwvkjh plmnbq")

    assert (result.count, result.total_available, result.city) == (0, 0, "Москва")
    assert result.meta.warnings == ["empty: Regard found nothing for 'qzxwvkjh plmnbq'"]


@pytest.mark.parametrize(
    "change",
    [{"id": None}, {"id": "737606"}, {"seo_url": None}, {"seo_url": "../../evil"}],
)
async def test_a_row_without_id_or_link_is_skipped(monkeypatch, change):
    def edit(state):
        search_rows(state)[0].update(change)

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.regard_search("rtx 5070")

    assert result.count == 5
    assert "row_skipped: a search row came without an id or product link" in result.meta.warnings


async def test_a_page_of_rows_without_prices_is_flagged(monkeypatch):
    def edit(state):
        for row in search_rows(state):
            row["price"] = 0

    stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)))

    result = await server.regard_search("rtx 5070")

    assert result.meta.warnings[0] == "no_prices_on_page: search rows came back without prices"
    assert all(it.price_rub is None for it in result.items)


async def test_limit_cuts_the_first_page(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.regard_search("rtx 5070", limit=2)

    assert [it.product_id for it in result.items] == ["737606", "730439"]


async def test_a_second_search_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    await server.regard_search("rtx 5070")
    again = await server.regard_search("rtx  5070")

    assert (len(urls), again.tier_used, again.count) == (1, "cache", 6)


async def test_a_listing_that_moved_is_drift_and_not_cached(monkeypatch):
    def edit(state):
        state["listing"]["data"]["byQuery"] = state["listing"]["data"].pop("bySearch")

    urls = stub_http(monkeypatch, ok(edited(SEARCH_HTML, edit)), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.regard_search("rtx 5070")).count == 6
    assert len(urls) == 2


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>ok</body></html>",
        '<script id="__NEXT_DATA__" type="application/json">not json</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"props": []}</script>',
        '<script id="__NEXT_DATA__" type="application/json">' + "[" * 100_000 + "]" * 100_000 + "</script>",
    ],
    ids=["no script", "not json", "no state", "too deep"],
)
async def test_a_page_without_payload_is_drift_not_a_stop(monkeypatch, html):
    urls = stub_http(monkeypatch, ok(html), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.regard_search("rtx 5070")).count == 6
    assert len(urls) == 2


@pytest.mark.parametrize(
    "reply",
    [
        Reply(403, QRATOR_PAGE, None),
        Reply(429, "", None),
        ok(QRATOR_PAGE),
        Reply(301, "", None, location="https://evil.example/catalog/1013/videokarty"),
        Reply(301, "", None, location="/product/737606/x"),
        Reply(302, "", None, location="/catalog/videokarty"),
    ],
)
async def test_a_refusal_or_redirect_stops_regard_after_one_request(monkeypatch, reply):
    urls = stub_http(monkeypatch, reply, ok(SEARCH_HTML))

    with pytest.raises(ToolError):
        await server.regard_search("rtx 5070")
    with pytest.raises(ToolError) as excinfo:
        await server.regard_card(CARD_URL)
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == 1


async def test_an_antibot_page_on_200_is_named(monkeypatch):
    stub_http(monkeypatch, ok(QRATOR_PAGE))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_search("rtx 5070")
    payload = error_payload(excinfo.value)
    assert (payload["error"], payload["status_code"], payload["challenge_type"]) == (
        "challenge_required",
        200,
        "antibot_page",
    )


async def test_a_server_error_is_a_transport_error_not_a_stop(monkeypatch):
    urls = stub_http(monkeypatch, Reply(500, "", None), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.regard_search("rtx 5070")).count == 6
    assert len(urls) == 2


# ------------------------------------------------------------------- card ----


async def test_card_reads_the_product_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.regard_card(CARD_URL)

    assert urls == [CARD_URL]
    assert (
        card.product_id,
        card.brand,
        card.part_number,
        card.price_rub,
        card.old_price_rub,
        card.rating,
        card.review_count,
        card.is_available,
        card.city,
        card.url,
        card.tier_used,
    ) == ("737606", "Palit", "NE75070019K9-GB2050S", 89990.0, None, 5.0, 3, True, "Москва", CARD_URL, "http")
    assert card.title == "Видеокарта NVIDIA GeForce RTX 5070 Palit Infinity 3 12GB (NE75070019K9-GB2050S)"
    assert card.meta.warnings == []


@pytest.mark.parametrize(
    "given",
    [
        CARD_URL,
        f"{CARD_URL}/",
        f"regard.ru/product/737606/{SLUG}",
        f"/product/737606/{SLUG}",
        f"http://www.regard.ru/product/737606/{SLUG}?utm_source=x",
    ],
)
async def test_card_accepts_a_product_url_or_path(monkeypatch, given):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.regard_card(given)

    assert urls == [CARD_URL]
    assert card.product_id == "737606"


async def test_a_bare_id_follows_the_sites_redirect_once(monkeypatch):
    urls = stub_http(monkeypatch, Reply(301, "", None, location=f"/product/737606/{SLUG}"), ok(CARD_HTML))

    card = await server.regard_card("737606")

    assert urls == ["https://www.regard.ru/product/737606", CARD_URL]
    assert card.url == CARD_URL


@pytest.mark.parametrize(
    "location",
    [
        "/product/739366/videokarta-other",
        "https://evil.example/product/737606/x",
        "/catalog?search=737606",
    ],
)
async def test_a_card_redirect_elsewhere_stops_regard(monkeypatch, location):
    urls = stub_http(monkeypatch, Reply(301, "", None, location=location), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_card("737606")
    assert error_payload(excinfo.value)["challenge_type"] == "redirect"
    with pytest.raises(ToolError):
        await server.regard_search("rtx 5070")
    assert len(urls) == 1


async def test_a_second_redirect_is_not_followed(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(308, "", None, location="/product/737606"),
        Reply(301, "", None, location=f"/product/737606/{SLUG}"),
    )

    with pytest.raises(ToolError) as excinfo:
        await server.regard_card("737606")
    assert error_payload(excinfo.value)["challenge_type"] == "redirect"
    assert len(urls) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "https://www.dns-shop.ru/product/737606/x",
        "https://www.regard.ru/catalog/1013/videokarty",
        "https://www.regard.ru/product/abc/x",
        "https://regard.ru.evil.example/product/737606/x",
        "12",
        "٧٣٧٦٠٦",
        "видеокарта",
    ],
)
async def test_card_refuses_input_that_is_not_a_product(monkeypatch, bad):
    urls = stub_http(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.regard_card(bad)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert urls == []


@pytest.mark.parametrize("status", [404, 410])
async def test_a_gone_product_is_not_found_not_a_stop(monkeypatch, status):
    urls = stub_http(monkeypatch, Reply(status, "<title>410 - Регард</title>", None), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_card("999999999")
    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.regard_card(CARD_URL)).price_rub == 89990.0
    assert len(urls) == 2


async def test_a_page_about_another_product_is_drift(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.regard_card("/product/739366/videokarta-other")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_a_price_that_disagrees_with_the_offer_is_flagged(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('"price": "89990.00"', '"price": "92990.00"')))

    card = await server.regard_card(CARD_URL)

    assert card.price_rub == 89990.0
    assert card.meta.warnings == ["price_mismatch: the page data says 89990 ₽, its schema.org offer 92990 ₽"]


@pytest.mark.parametrize(
    ("availability", "expected"),
    [("https://schema.org/OutOfStock", False), ("https://schema.org/PreOrder", None)],
)
async def test_availability_comes_from_the_offer(monkeypatch, availability, expected):
    stub_http(monkeypatch, ok(CARD_HTML.replace("https://schema.org/InStock", availability)))

    card = await server.regard_card(CARD_URL)

    assert card.is_available is expected


async def test_a_card_without_price_says_so(monkeypatch):
    def edit(state):
        state["product"]["data"]["price"] = None

    stub_http(monkeypatch, ok(edited(CARD_HTML, edit)))

    card = await server.regard_card(CARD_URL)

    assert card.price_rub is None
    assert card.meta.warnings[0] == "no_price: the product page carries no price"


async def test_a_second_card_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    await server.regard_card(CARD_URL)
    again = await server.regard_card("737606")

    assert (len(urls), again.tier_used, again.price_rub, again.url) == (1, "cache", 89990.0, CARD_URL)


def test_fixtures_carry_no_visitor_or_author_data():
    for page in (SEARCH_HTML, EMPTY_HTML, CARD_HTML):
        assert not re.search(r"author|email|phone|token|userId|session", page)


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_healthy_after_search_and_card(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML), ok(CARD_HTML))

    result = await server.regard_selfcheck()

    assert result.status == "success"
    assert {name: check.state for name, check in result.checks.items()} == {"search": "healthy", "card": "healthy"}
    assert urls[1] == CARD_URL


async def test_selfcheck_refusal_is_inconclusive_and_skips_the_card(monkeypatch):
    urls = stub_http(monkeypatch, Reply(403, QRATOR_PAGE, None))

    result = await server.regard_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {
        "search": "inconclusive",
        "card": "inconclusive",
    }
    assert len(urls) == 1


async def test_selfcheck_drift_when_the_page_moves(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML.replace('"bySearch"', '"byQuery"')))

    result = await server.regard_selfcheck()

    assert result.checks["search"].state == "drift"
