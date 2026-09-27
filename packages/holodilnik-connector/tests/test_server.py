"""Offline tests for the Holodilnik connector: live pages from fixtures, the network stubbed at curl_cffi."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from holodilnik_connector import server
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport import impersonated
from mcp_core.transport.impersonated import Reply

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "search_atlant_live.html").read_text(encoding="utf-8")
FRIDGE_HTML = (FIXTURES / "search_refrigerator_live.html").read_text(encoding="utf-8")
UNRELATED_HTML = (FIXTURES / "search_unrelated_live.html").read_text(encoding="utf-8")
CARD_HTML = (FIXTURES / "card_atlant_live.html").read_text(encoding="utf-8")
CARD_URL = "https://www.holodilnik.ru/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/"
BEKO_HTML = (FIXTURES / "card_beko_live.html").read_text(encoding="utf-8")
BEKO_URL = "https://www.holodilnik.ru/refrigerator/two_chambered_refrigerators/beko/b1rcsk332w_belyy/"
EMPTY_HTML = SEARCH_HTML[: SEARCH_HTML.index('<div class="catalog-item">')] + "</div>\n</body></html>\n"
QRATOR_PAGE = "<html><head><title>Qrator</title></head><body>Доступ ограничен</body></html>"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="holodilnik", gate=StopGate(None)))
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


# ----------------------------------------------------------------- search ----


async def test_search_reads_the_sites_own_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.holodilnik_search("холодильник  atlant")

    assert urls == [
        "https://www.holodilnik.ru/search/?search=%D1%85%D0%BE%D0%BB%D0%BE%D0%B4%D0%B8%D0%BB%D1%8C%D0%BD%D0%B8%D0%BA+atlant"
    ]
    assert (result.tier_used, result.city, result.total_available, result.count) == (
        "http",
        "Санкт-Петербург",
        186,
        20,
    )
    assert result.meta.warnings == []


async def test_search_rows_carry_article_price_rating_and_term(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    items = (await server.holodilnik_search("холодильник atlant")).items

    first = items[0]
    assert (first.product_id, first.title, first.price_rub, first.old_price_rub) == (
        "107818",
        "Двухкамерный холодильник Atlant ХМ 4012-022",
        37199.0,
        None,
    )
    assert (first.rating, first.review_count, first.in_stock, first.availability, first.url) == (
        4.5,
        74,
        True,
        "послезавтра",
        CARD_URL,
    )
    assert [(it.product_id, it.price_rub, it.availability) for it in items[1:4]] == [
        ("171749", 33499.0, "послезавтра"),
        ("723822", 58799.0, "послезавтра"),
        ("358829", 50499.0, "c 1 октября"),
    ]


async def test_a_row_without_reviews_has_no_rating(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    items = (await server.holodilnik_search("холодильник atlant", limit=24)).items

    unrated = next(it for it in items if it.product_id == "819428")
    assert (unrated.rating, unrated.review_count, unrated.price_rub) == (None, None, 77599.0)


async def test_the_crossed_out_price_is_kept_only_above_the_price(monkeypatch):
    stub_http(monkeypatch, ok(FRIDGE_HTML), ok(FRIDGE_HTML.replace("41&nbsp;490", "40&nbsp;000")))

    beko = (await server.holodilnik_search("холодильник")).items[1]
    assert (beko.product_id, beko.price_rub, beko.old_price_rub, beko.rating, beko.review_count) == (
        "652459",
        40899.0,
        41490.0,
        5.0,
        15,
    )
    server._cache.clear()
    assert (await server.holodilnik_search("холодильник")).items[1].old_price_rub is None


async def test_promo_badges_are_not_prices(monkeypatch):
    assert "Скидка 10%" in SEARCH_HTML and "Списывайте бонусы" in SEARCH_HTML
    stub_http(monkeypatch, ok(SEARCH_HTML))

    items = (await server.holodilnik_search("холодильник atlant", limit=24)).items

    assert all(it.price_rub is not None and it.price_rub > 1000 for it in items)
    assert all(it.old_price_rub is None for it in items)


async def test_a_row_marked_out_of_stock_is_not_in_stock(monkeypatch):
    stub_http(monkeypatch, ok(UNRELATED_HTML))

    items = (await server.holodilnik_search("aqara")).items

    assert [(it.product_id, it.price_rub, it.in_stock, it.availability) for it in items] == [
        ("761849", 5499.0, False, "Нет в наличии")
    ]


async def test_stock_is_unknown_without_a_buy_button(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML.replace('data-smoke="AddToCartListing"', 'data-smoke="Other"')))

    first = (await server.holodilnik_search("холодильник atlant")).items[0]

    assert (first.in_stock, first.availability) == (None, "послезавтра")


async def test_fuzzy_rows_that_do_not_name_the_query_are_no_match(monkeypatch):
    stub_http(monkeypatch, ok(UNRELATED_HTML))

    result = await server.holodilnik_search("qzxwvkjh plmnbq")

    assert (result.count, result.total_available, result.items) == (0, 0, [])
    assert result.meta.warnings[0].startswith("no_match:")


async def test_a_page_with_search_info_and_no_rows_is_an_honest_empty_result(monkeypatch):
    stub_http(monkeypatch, ok(EMPTY_HTML))

    result = await server.holodilnik_search("холодильник atlant")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: Holodilnik found nothing for 'холодильник atlant'"]


@pytest.mark.parametrize(
    "change",
    [
        ("data-icon-link-favorite='107818'", "data-icon-link-favorite=''"),
        (
            '<a href="/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/" class="link link--inherit">',
            '<a href="https://evil.example/x/y/z/w/" class="link link--inherit">',
        ),
    ],
)
async def test_a_row_without_article_or_product_link_is_skipped(monkeypatch, change):
    assert SEARCH_HTML.count(change[0]) == 1
    stub_http(monkeypatch, ok(SEARCH_HTML.replace(*change)))

    result = await server.holodilnik_search("холодильник atlant")

    assert result.items[0].product_id == "171749"
    assert result.meta.warnings[0].startswith("row_skipped:")


async def test_a_row_without_a_price_is_flagged(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML.replace("37&nbsp;199&nbsp;&#8381;", "", 1)))

    result = await server.holodilnik_search("холодильник atlant")

    assert result.items[0].price_rub is None
    assert "107818: no_price" in result.meta.warnings


async def test_limit_cuts_the_first_page(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    assert (await server.holodilnik_search("холодильник atlant", limit=3)).count == 3


async def test_a_second_search_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    await server.holodilnik_search("холодильник atlant")
    again = await server.holodilnik_search("холодильник atlant")

    assert (again.tier_used, again.count, len(urls)) == ("cache", 20, 1)


async def test_a_same_site_redirect_is_followed_once_and_named(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(301, "", None, location="/refrigerator/two_chambered_refrigerators/atlant/"),
        ok(SEARCH_HTML),
    )

    result = await server.holodilnik_search("атлант")

    assert urls[1] == "https://www.holodilnik.ru/refrigerator/two_chambered_refrigerators/atlant/"
    assert result.meta.warnings[0] == (
        "category: Holodilnik sent the query to /refrigerator/two_chambered_refrigerators/atlant/"
    )


@pytest.mark.parametrize(
    "html",
    ["<html><body>Главная</body></html>", SEARCH_HTML.replace('id="search_res_info"', "").replace("catalog-item", "x")],
)
async def test_a_page_without_search_info_or_rows_is_drift_not_a_stop(monkeypatch, html):
    urls = stub_http(monkeypatch, ok(html), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_search("холодильник atlant")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.holodilnik_search("холодильник atlant")).count == 20
    assert len(urls) == 2


@pytest.mark.parametrize(
    "replies",
    [
        (Reply(403, QRATOR_PAGE, None),),
        (Reply(401, "", None),),
        (Reply(418, "", None),),
        (Reply(429, "", None),),
        (ok(QRATOR_PAGE),),
        (Reply(301, "", None, location="https://evil.example/search/"),),
        (Reply(302, "", None, location="/x/"), Reply(302, "", None, location="/y/")),
    ],
)
async def test_a_refusal_or_foreign_redirect_stops_holodilnik(monkeypatch, replies):
    urls = stub_http(monkeypatch, *replies, ok(SEARCH_HTML))

    with pytest.raises(ToolError):
        await server.holodilnik_search("холодильник atlant")
    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_card(CARD_URL)
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == len(replies)


async def test_an_antibot_page_on_200_is_named(monkeypatch):
    stub_http(monkeypatch, ok(QRATOR_PAGE))

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_search("холодильник atlant")
    payload = error_payload(excinfo.value)
    assert (payload["error"], payload["status_code"], payload["challenge_type"]) == (
        "challenge_required",
        200,
        "antibot_page",
    )


async def test_a_server_error_is_a_transport_error_not_a_stop(monkeypatch):
    stub_http(monkeypatch, Reply(500, "", None), ok(SEARCH_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_search("холодильник atlant")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.holodilnik_search("холодильник atlant")).count == 20


async def test_an_empty_query_is_a_bad_request(monkeypatch):
    urls = stub_http(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_search("   ")
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert urls == []


# ------------------------------------------------------------------- card ----


async def test_card_reads_article_price_stock_and_identity_from_microdata(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.holodilnik_card(CARD_URL)

    assert urls == [CARD_URL]
    assert (card.product_id, card.price_rub, card.is_available, card.city) == (
        "107818",
        37199.0,
        True,
        "Санкт-Петербург",
    )
    assert (card.title, card.brand, card.gtin, card.rating, card.review_count) == (
        "Двухкамерный холодильник Atlant ХМ 4012-022",
        "Atlant",
        "4811159026935",
        4.51,
        73,
    )
    assert (card.url, card.tier_used, card.meta.warnings) == (CARD_URL, "http", [])


@pytest.mark.parametrize(
    "given",
    [
        CARD_URL.removeprefix("https://"),
        CARD_URL.removeprefix("https://www.holodilnik.ru"),
        CARD_URL.rstrip("/"),
        CARD_URL.replace("https://www.", "http://"),
    ],
)
async def test_card_accepts_the_url_without_scheme_host_or_slash(monkeypatch, given):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    assert (await server.holodilnik_card(given)).product_id == "107818"
    assert urls == [CARD_URL]


@pytest.mark.parametrize(
    "bad",
    [
        "107818",
        "холодильник atlant",
        "https://www.holodilnik.ru/refrigerator/two_chambered_refrigerators/atlant/",
        "https://www.holodilnik.ru/a/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/",
        "https://holodilnik.ru.evil.example/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/",
        "https://www.regard.ru/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/",
        "//evil.example/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/",
        "https://www.holodilnik.ru/refrigerator/../../atlant/xm_4012_022/",
        "javascript:alert(1)//www.holodilnik.ru/a/b/c/d/",
        "https://www.holodilnik.ru@evil.example/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/",
        "https://www.holodilnik.ru:443@evil.example/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022/",
        "https:\\\\evil.example\\refrigerator\\two_chambered_refrigerators\\atlant\\xm_4012_022\\",
    ],
)
async def test_card_refuses_input_that_is_not_a_product(monkeypatch, bad):
    urls = stub_http(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_card(bad)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert urls == []


async def test_a_product_the_site_dropped_is_not_found_not_a_stop(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(301, "", None, location="/refrigerator/two_chambered_refrigerators/atlant/"),
        ok(CARD_HTML),
    )
    gone = CARD_URL.replace("xm_4012_022", "zq_9999_999")

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_card(gone)
    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.holodilnik_card(CARD_URL)).price_rub == 37199.0
    assert len(urls) == 2


@pytest.mark.parametrize("status", [404, 410])
async def test_a_gone_page_is_not_found(monkeypatch, status):
    stub_http(monkeypatch, Reply(status, "", None))

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "not_found"


async def test_a_redirect_to_another_product_page_is_followed_once(monkeypatch):
    moved = "/refrigerator/two_chambered_refrigerators/atlant/xm_4012_022_new/"
    urls = stub_http(monkeypatch, Reply(301, "", None, location=moved), ok(CARD_HTML))

    card = await server.holodilnik_card(CARD_URL)

    assert urls == [CARD_URL, f"https://www.holodilnik.ru{moved}"]
    assert card.url == f"https://www.holodilnik.ru{moved}"


@pytest.mark.parametrize(
    "replies",
    [
        (Reply(301, "", None, location="https://evil.example/a/b/c/d/"),),
        (Reply(302, "", None, location="/search/?search=x"),),
        (
            Reply(301, "", None, location="/refrigerator/two_chambered_refrigerators/atlant/xm_1/"),
            Reply(301, "", None, location="/refrigerator/two_chambered_refrigerators/atlant/xm_2/"),
        ),
        (ok(QRATOR_PAGE),),
        (Reply(429, "", None),),
    ],
)
async def test_a_card_refusal_or_foreign_redirect_stops_holodilnik(monkeypatch, replies):
    urls = stub_http(monkeypatch, *replies, ok(SEARCH_HTML))

    with pytest.raises(ToolError):
        await server.holodilnik_card(CARD_URL)
    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_search("холодильник atlant")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == len(replies)


@pytest.mark.parametrize(
    "change",
    [
        ('<meta itemprop="sku" content="107818"/>', ""),
        ('itemtype="https://schema.org/Offer"', 'itemtype="https://schema.org/Thing"'),
        ('itemtype="http://schema.org/Product"', 'itemtype="http://schema.org/Thing"'),
    ],
)
async def test_a_card_without_its_microdata_is_drift_not_a_stop(monkeypatch, change):
    assert CARD_HTML.count(change[0]) == 1
    urls = stub_http(monkeypatch, ok(CARD_HTML.replace(*change)), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.holodilnik_card(CARD_URL)).product_id == "107818"
    assert len(urls) == 2


async def test_a_discounted_card_carries_the_crossed_out_price(monkeypatch):
    stub_http(monkeypatch, ok(BEKO_HTML))

    card = await server.holodilnik_card(BEKO_URL)

    assert (card.product_id, card.price_rub, card.old_price_rub) == ("652459", 40899.0, 41490.0)
    assert (card.rating, card.review_count, card.gtin) == (5.0, 12, "4680037178420")


async def test_a_card_without_a_discount_has_no_old_price(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML))

    assert (await server.holodilnik_card(CARD_URL)).old_price_rub is None


@pytest.mark.parametrize(
    "change",
    [
        ('<div class="product-price__current">40&nbsp;899', '<div class="product-price__current">39&nbsp;999'),
        ('<div class="product-price__old">41&nbsp;490', '<div class="product-price__old">40&nbsp;000'),
    ],
)
async def test_an_old_price_that_does_not_fit_the_offer_is_dropped(monkeypatch, change):
    assert BEKO_HTML.count(change[0]) == 1
    stub_http(monkeypatch, ok(BEKO_HTML.replace(*change)))

    card = await server.holodilnik_card(BEKO_URL)

    assert (card.price_rub, card.old_price_rub) == (40899.0, None)


RECOMMENDED = (
    '<div itemtype="http://schema.org/Product" itemscope><meta itemprop="name" content="Другой товар" />'
    '<meta itemprop="sku" content="999999"/><div itemprop="offers" itemtype="https://schema.org/Offer" itemscope>'
    '<meta itemprop="price" content="1"/><meta itemprop="priceCurrency" content="RUB"/></div></div>'
)


async def test_another_products_microdata_on_the_page_is_drift_not_a_mixed_card(monkeypatch):
    anchor = '<div class="layout card-product-page catalog"'
    assert CARD_HTML.count(anchor) == 1
    stub_http(monkeypatch, ok(CARD_HTML.replace(anchor, RECOMMENDED + anchor)))

    with pytest.raises(ToolError) as excinfo:
        await server.holodilnik_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_an_out_of_stock_card_needs_no_price(monkeypatch):
    page = CARD_HTML.replace("schema.org/InStock", "schema.org/OutOfStock").replace('content="37199"', 'content=""')
    stub_http(monkeypatch, ok(page))

    card = await server.holodilnik_card(CARD_URL)

    assert (card.is_available, card.price_rub, card.meta.warnings) == (False, None, [])


async def test_a_price_in_another_currency_is_not_taken(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('content="RUB"', 'content="USD"')))

    card = await server.holodilnik_card(CARD_URL)

    assert card.price_rub is None
    assert card.meta.warnings == ["no_price: the product page carries no price"]


async def test_a_card_without_reviews_has_no_rating(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('content="73"', 'content="0"')))

    card = await server.holodilnik_card(CARD_URL)

    assert (card.rating, card.review_count) == (None, 0)


async def test_the_offer_survives_a_nested_block(monkeypatch):
    opening = 'itemtype="https://schema.org/Offer" itemscope>'
    assert opening in CARD_HTML
    stub_http(monkeypatch, ok(CARD_HTML.replace(opening, opening + '<div class="x">Доставка</div>')))

    assert (await server.holodilnik_card(CARD_URL)).price_rub == 37199.0


async def test_a_second_card_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    await server.holodilnik_card(CARD_URL)
    again = await server.holodilnik_card(CARD_URL.rstrip("/"))

    assert (again.tier_used, again.price_rub, len(urls)) == ("cache", 37199.0, 1)


# -------------------------------------------------------------- selfcheck ----


async def test_selfcheck_healthy_after_search_and_card(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML), ok(CARD_HTML))

    result = await server.holodilnik_selfcheck()

    assert result.status == "success"
    assert {name: check.state for name, check in result.checks.items()} == {"search": "healthy", "card": "healthy"}
    assert urls[1] == CARD_URL


async def test_selfcheck_refusal_is_inconclusive_and_skips_the_card(monkeypatch):
    urls = stub_http(monkeypatch, Reply(403, QRATOR_PAGE, None))

    result = await server.holodilnik_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {
        "search": "inconclusive",
        "card": "inconclusive",
    }
    assert len(urls) == 1


async def test_selfcheck_drift_when_the_rows_move(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML.replace("price-value__actual", "price-now")))

    result = await server.holodilnik_selfcheck()

    assert result.checks["search"].state == "drift"
