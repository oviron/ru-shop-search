"""Offline tests for the Nix connector: the Chrome tab is faked at ``open_page``, HTTP at ``impersonated_get``."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport import impersonated
from mcp_core.transport.chrome_cdp import NavBlocked
from mcp_core.transport.impersonated import Reply
from nix_connector import server

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "search_rtx5070_live.json").read_text(encoding="utf-8"))
UNRELATED = json.loads((FIXTURES / "search_unrelated_live.json").read_text(encoding="utf-8"))
GENERIC = json.loads((FIXTURES / "search_generic_live.json").read_text(encoding="utf-8"))
NONSENSE = json.loads((FIXTURES / "search_cyrillic_nonsense_live.json").read_text(encoding="utf-8"))
SPB_SEARCH = json.loads((FIXTURES / "search_rtx5070_spb_live.json").read_text(encoding="utf-8"))
CARD_HTML = (FIXTURES / "card_gigabyte_live.html").read_text(encoding="utf-8")
CARD_URL = (
    "https://www.nix.ru/autocatalog/gigabyte/video/"
    "12Gb-PCI-E-GDDR7-GIGABYTE-GV-N5070GAMING-OC-12GD-RTL-HDMI-plus-3xDP-GeForce-RTX5070_860397.html"
)
SPB_CARD_URL = CARD_URL.replace("https://www.", "https://spb.")
NOTHING = {"title": SEARCH["title"], "city": "Москва", "answered": True, "total": None, "nothing": True, "rows": []}


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


def stub_http(monkeypatch, *replies: Reply) -> list[str]:
    urls: list[str] = []
    queue = list(replies)

    async def fake_get(url, **kwargs):
        urls.append(url)
        return queue.pop(0)

    monkeypatch.setattr(impersonated, "impersonated_get", fake_get)
    return urls


def ok(text: str) -> Reply:
    return Reply(200, text, None)


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


def with_rows(payload: dict, rows: list[str]) -> dict:
    return {**payload, "rows": rows}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="nix", gate=StopGate(None)))
    monkeypatch.setattr(server, "_min_gap", 0.0)
    monkeypatch.setattr(server, "_RENDER_WAIT_MS", 0)
    yield
    server._cache.clear()


# ------------------------------------------------------------------ search ----


async def test_search_opens_the_sites_search_page_with_the_query_in_the_hash(monkeypatch):
    calls = fake_tab(monkeypatch, SEARCH)

    result = await server.nix_search("rtx  5070")

    assert calls[0]["url"] == "https://spb.nix.ru/price/search_panel_ajax.html#keywords=rtx%205070"
    assert (result.tier_used, result.city, result.total_available, result.count) == ("cdp", "Москва", 287, 6)


async def test_a_query_with_hash_syntax_stays_one_keyword_value(monkeypatch):
    calls = fake_tab(monkeypatch, SEARCH)

    await server.nix_search("rtx&store=1#x видеокарта")

    assert calls[0]["url"].endswith(
        "#keywords=rtx%26store%3D1%23x%20%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE%D0%BA%D0%B0%D1%80%D1%82%D0%B0"
    )


async def test_search_rows_carry_the_price_not_the_superprice(monkeypatch):
    fake_tab(monkeypatch, SEARCH)

    items = (await server.nix_search("rtx 5070")).items

    assert [(it.product_id, it.price_rub, it.availability, it.in_stock) for it in items] == [
        ("907082", 128042.0, "01.10", True),
        ("860397", 112683.0, "07.10", True),
        ("860399", 104290.0, "30.09", True),
        ("957068", 101686.0, "30.09", True),
        ("860396", 109782.0, "01.10", True),
        ("860395", 108480.0, "30.09", True),
    ]
    assert items[1].title == (
        "GIGABYTE Gaming GV-N5070GAMING OC-12GD < GeForce® RTX 5070 > "
        "(12 Гб, GDDR7, 192 бита, PCI Express, 3xDP, 1xHDMI)"
    )
    assert items[1].url == SPB_CARD_URL


async def test_saint_petersburg_rows_carry_their_single_price(monkeypatch):
    """On spb.nix.ru a row has one «cell-price» cell and no СУПЕРЦЕНА."""
    fake_tab(monkeypatch, SPB_SEARCH)

    result = await server.nix_search("rtx 5070")

    assert result.city == "Санкт-Петербург"
    assert [(it.product_id, it.price_rub, it.superprice_rub, it.availability, it.in_stock) for it in result.items] == [
        ("907082", 128042.0, None, "08.10", True),
        ("860397", 114931.0, None, "12.10", True),
        ("860399", 104290.0, None, "05.10", True),
        ("957068", 101686.0, None, "05.10", True),
        ("860396", 109781.0, None, "08.10", True),
        ("860395", 108480.0, None, "05.10", True),
    ]
    assert result.meta.warnings == []


async def test_search_rows_carry_the_superprice_beside_the_price(monkeypatch):
    fake_tab(monkeypatch, with_rows(SEARCH, SEARCH["rows"][:2] + UNRELATED["rows"][2:3]))

    items = (await server.nix_search("rtx 5070")).items

    assert [(it.price_rub, it.superprice_rub) for it in items] == [
        (128042.0, 124193.0),
        (112683.0, 109296.0),
        (5569.0, None),
    ]


async def test_a_row_with_one_price_takes_it_as_the_price(monkeypatch):
    fake_tab(monkeypatch, with_rows(SEARCH, UNRELATED["rows"][2:3]))

    items = (await server.nix_search("dewalt")).items

    assert [(it.product_id, it.price_rub) for it in items] == [("999368", 5569.0)]


async def test_limit_cuts_the_rows(monkeypatch):
    fake_tab(monkeypatch, SEARCH)

    result = await server.nix_search("rtx 5070", limit=2)

    assert [it.product_id for it in result.items] == ["907082", "860397"]
    assert result.meta.warnings == []


@pytest.mark.parametrize("term", ["ЕСТЬ", "ЗАВТРА", "ПОСЛЕЗАВТРА", "12.10"])
async def test_a_named_term_means_in_stock(monkeypatch, term):
    fake_tab(monkeypatch, with_rows(SEARCH, [SEARCH["rows"][0].replace(">01.10<", f">{term}<")]))

    item = (await server.nix_search("rtx 5070")).items[0]

    assert (item.availability, item.in_stock) == (term, True)


@pytest.mark.parametrize("term", ["", "ОЖИДАЕТСЯ", "01.10.2026"])
async def test_an_unknown_term_leaves_stock_unknown(monkeypatch, term):
    fake_tab(monkeypatch, with_rows(SEARCH, [SEARCH["rows"][0].replace(">01.10<", f">{term}<")]))

    item = (await server.nix_search("rtx 5070")).items[0]

    assert item.in_stock is None


async def test_fuzzy_rows_that_do_not_name_the_query_are_no_match(monkeypatch):
    fake_tab(monkeypatch, UNRELATED)

    result = await server.nix_search("qzxwvkjh")

    assert (result.count, result.total_available, result.items) == (0, 0, [])
    assert result.meta.warnings[0].startswith("no_match:")


async def test_a_cyrillic_nonsense_word_is_no_match_too(monkeypatch):
    fake_tab(monkeypatch, NONSENSE)

    result = await server.nix_search("абырвалг")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings[0].startswith("no_match:")


async def test_a_generic_word_the_site_files_under_a_category_keeps_its_rows(monkeypatch):
    fake_tab(monkeypatch, GENERIC)

    result = await server.nix_search("видеокарта")

    assert (result.count, result.total_available) == (4, 5000)
    assert not any(w.startswith("no_match:") for w in result.meta.warnings)


async def test_nothing_found_is_an_empty_answer(monkeypatch):
    fake_tab(monkeypatch, NOTHING)

    result = await server.nix_search("rtx 5070")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: Nix found nothing for 'rtx 5070'"]


async def test_a_row_without_a_price_is_flagged(monkeypatch):
    row = SEARCH["rows"][0].replace("124&nbsp;193", "").replace("128&nbsp;042", "")
    fake_tab(monkeypatch, with_rows(SEARCH, [row, SEARCH["rows"][1]]))

    result = await server.nix_search("rtx 5070")

    assert result.items[0].price_rub is None
    assert result.meta.warnings == ["907082: no_price"]


async def test_a_row_whose_link_leaves_the_catalog_is_skipped(monkeypatch):
    row = SEARCH["rows"][0].replace('href="/autocatalog/zotac/', 'href="https://evil.example/autocatalog/zotac/')
    fake_tab(monkeypatch, with_rows(SEARCH, [row, SEARCH["rows"][1]]))

    result = await server.nix_search("rtx 5070")

    assert [it.product_id for it in result.items] == ["860397"]
    assert "row_skipped: a search row came without a product link" in result.meta.warnings


async def test_a_repeat_search_is_served_from_cache(monkeypatch):
    calls = fake_tab(monkeypatch, SEARCH)

    await server.nix_search("rtx 5070")
    again = await server.nix_search("rtx 5070", limit=3)

    assert (len(calls), again.tier_used, again.count) == (1, "cache", 3)


async def test_a_rendered_page_without_rows_is_drift_not_a_stop(monkeypatch):
    fake_tab(monkeypatch, {**NOTHING, "nothing": False}, SEARCH)

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.nix_search("rtx 5070")).count == 6


@pytest.mark.parametrize("answered", [False, None])
async def test_a_page_that_never_answered_the_query_is_drift_not_empty(monkeypatch, answered):
    fake_tab(monkeypatch, {**NOTHING, "answered": answered})

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_an_extractor_payload_that_is_not_json_is_drift(monkeypatch):
    class BrokenPage:
        async def evaluate(self, expression, arg=None):
            return "{not json"

    @asynccontextmanager
    async def open_page(url, wait_ms=5000, **kwargs):
        yield BrokenPage()

    monkeypatch.setattr(server, "open_page", open_page)

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


@pytest.mark.parametrize("status", [401, 403, 418, 429])
async def test_a_refused_tab_stops_nix_for_search_and_card(monkeypatch, status):
    fake_tab(monkeypatch, NavBlocked(status, "blocked"))
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["status_code"] == status
    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value).get("source_stopped") is True
    assert urls == []


async def test_a_block_page_in_the_tab_stops_nix(monkeypatch):
    fake_tab(
        monkeypatch,
        {"title": "Access denied", "city": None, "answered": False, "total": None, "nothing": False, "rows": []},
    )

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value).get("source_stopped") is True


async def test_a_page_without_a_title_by_the_deadline_is_a_timeout_not_a_stop(monkeypatch):
    blank = {"title": "", "city": None, "answered": False, "total": None, "nothing": False, "rows": []}
    fake_tab(monkeypatch, blank, SEARCH)

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.nix_search("rtx 5070")).count == 6


async def test_a_server_error_in_the_tab_is_not_a_stop(monkeypatch):
    fake_tab(monkeypatch, NavBlocked(502, "bad gateway"), SEARCH)

    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.nix_search("rtx 5070")).count == 6


# -------------------------------------------------------------------- card ----


async def test_card_reads_price_stock_and_brand_from_microdata(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.nix_card(CARD_URL)

    assert urls == [SPB_CARD_URL]
    assert (card.product_id, card.price_rub, card.is_available, card.city) == ("860397", 112683.0, True, "Москва")
    assert (card.title, card.brand, card.part_number) == (
        "GIGABYTE Gaming GV-N5070GAMING OC-12GD",
        "GIGABYTE",
        "GV-N5070GAMING OC-12GD",
    )
    assert (card.url, card.tier_used, card.meta.warnings) == (SPB_CARD_URL, "http", [])


@pytest.mark.parametrize(
    "given",
    [
        CARD_URL.removeprefix("https://www."),
        CARD_URL.removeprefix("https://www.nix.ru"),
        CARD_URL.replace("https://www.", "http://"),
    ],
)
async def test_card_accepts_the_url_without_scheme_or_host(monkeypatch, given):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    assert (await server.nix_card(given)).price_rub == 112683.0
    assert urls == [SPB_CARD_URL]


@pytest.mark.parametrize(
    "bad",
    [
        "860397",
        "https://www.regard.ru/autocatalog/gigabyte/x_860397.html",
        "https://nix.ru.evil.example/autocatalog/gigabyte/x_860397.html",
        "https://www.nix.ru/price/price_list.html?section=video_cards_all",
        "https://www.nix.ru/autocatalog/../../x_860397.html",
        "javascript:alert(1)//www.nix.ru/autocatalog/a/x_860397.html",
        "//evil.example/autocatalog/a/x_860397.html",
        "видеокарта",
    ],
)
async def test_card_refuses_input_that_is_not_a_product(monkeypatch, bad):
    urls = stub_http(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(bad)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert urls == []


async def test_card_offer_survives_a_nested_block(monkeypatch):
    opening = 'itemtype="http://schema.org/Offer" class="all-delivery-prices">'
    assert opening in CARD_HTML
    stub_http(monkeypatch, ok(CARD_HTML.replace(opening, opening + '<div class="delivery">Доставка</div>')))

    card = await server.nix_card(CARD_URL)

    assert (card.price_rub, card.is_available) == (112683.0, True)


@pytest.mark.parametrize(
    ("content", "expected"),
    [('content="5569.00"', 5569.0), ('content="0"', None), ('content=""', None)],
)
async def test_card_price_is_read_as_money_not_digits(monkeypatch, content, expected):
    stub_http(monkeypatch, ok(CARD_HTML.replace('content="112683"', content)))

    card = await server.nix_card(CARD_URL)

    assert card.price_rub == expected
    assert card.meta.warnings == ([] if expected else ["no_price: the product page carries no price"])


async def test_a_price_in_another_currency_is_not_taken(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('content="RUB"', 'content="USD"')))

    assert (await server.nix_card(CARD_URL)).price_rub is None


@pytest.mark.parametrize(("stock", "expected"), [("OutOfStock", False), ("PreOrder", None)])
async def test_card_availability_follows_schema_org(monkeypatch, stock, expected):
    stub_http(monkeypatch, ok(CARD_HTML.replace("schema.org/InStock", f"schema.org/{stock}")))

    card = await server.nix_card(CARD_URL)

    assert card.is_available is expected


async def test_the_superprice_line_is_not_the_price(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('content="112683"', 'content=""')))

    card = await server.nix_card(CARD_URL)

    assert card.price_rub is None


async def test_the_card_carries_the_superprice_beside_the_price(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML), ok(CARD_HTML.replace('<span class="superprice ">', "<span>")))

    card = await server.nix_card(CARD_URL)
    server._cache.clear()
    plain = await server.nix_card(CARD_URL)

    assert (card.price_rub, card.superprice_rub) == (112683.0, 109296.0)
    assert (plain.price_rub, plain.superprice_rub) == (112683.0, None)


@pytest.mark.parametrize("status", [404, 410])
async def test_a_missing_product_is_not_found_not_a_stop(monkeypatch, status):
    urls = stub_http(monkeypatch, Reply(status, "<title>Страница не найдена!</title>", None), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL.replace("_860397", "_999999997"))
    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.nix_card(CARD_URL)).price_rub == 112683.0
    assert len(urls) == 2


async def test_a_redirect_to_the_same_product_is_followed_once(monkeypatch):
    moved = CARD_URL.replace("/gigabyte/video/", "/gigabyte/")
    urls = stub_http(
        monkeypatch, Reply(301, "", None, location=moved.removeprefix("https://www.nix.ru")), ok(CARD_HTML)
    )

    card = await server.nix_card(CARD_URL)

    assert urls == [SPB_CARD_URL, moved.replace("https://www.", "https://spb.")]
    assert card.url == moved.replace("https://www.", "https://spb.")


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/autocatalog/gigabyte/x_860397.html",
        "/autocatalog/gigabyte/x_860398.html",
        "/price/index.html",
    ],
)
async def test_a_redirect_elsewhere_stops_nix(monkeypatch, location):
    urls = stub_http(monkeypatch, Reply(302, "", None, location=location))

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "challenge_required"
    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value).get("source_stopped") is True
    assert len(urls) == 1


async def test_a_second_redirect_is_not_followed(monkeypatch):
    moved = CARD_URL.replace("/gigabyte/video/", "/gigabyte/")
    urls = stub_http(
        monkeypatch, Reply(301, "", None, location=moved), Reply(301, "", None, location=CARD_URL), ok(CARD_HTML)
    )

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "challenge_required"
    assert len(urls) == 2


@pytest.mark.parametrize("status", [401, 403, 418, 429])
async def test_a_refused_card_stops_nix_after_one_request(monkeypatch, status):
    urls = stub_http(monkeypatch, Reply(status, "", None), ok(CARD_HTML))
    fake_tab(monkeypatch, SEARCH)

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value)["status_code"] == status
    with pytest.raises(ToolError) as excinfo:
        await server.nix_search("rtx 5070")
    assert error_payload(excinfo.value).get("source_stopped") is True
    assert len(urls) == 1


async def test_an_anti_bot_page_on_200_stops_nix(monkeypatch):
    stub_http(monkeypatch, ok("<html><title>Доступ ограничен</title><div>captcha</div></html>"))

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "challenge_required"


async def test_a_page_without_microdata_is_drift_not_a_stop(monkeypatch):
    stub_http(monkeypatch, ok("<html><title>НИКС</title><h1>x</h1></html>"), ok(CARD_HTML))

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.nix_card(CARD_URL)).price_rub == 112683.0


async def test_an_offer_for_another_product_is_drift(monkeypatch):
    stub_http(monkeypatch, ok(CARD_HTML.replace('RTX5070_860397.html" />\n</div>', 'RTX5070_860398.html" />\n</div>')))

    with pytest.raises(ToolError) as excinfo:
        await server.nix_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_a_repeat_card_is_served_from_cache(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    await server.nix_card(CARD_URL)
    again = await server.nix_card(CARD_URL)

    assert (len(urls), again.tier_used, again.price_rub) == (1, "cache", 112683.0)


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_is_healthy_on_a_priced_search_and_card(monkeypatch):
    fake_tab(monkeypatch, SEARCH)
    urls = stub_http(monkeypatch, ok(CARD_HTML.replace("_860397", "_907082")))

    result = await server.nix_selfcheck()

    assert result.checks["search"].state == "healthy"
    assert result.checks["card"].state == "healthy"
    assert urls == [
        "https://spb.nix.ru/autocatalog/zotac/"
        "12Gb-PCI-E-GDDR7-ZOTAC-GeForce-RTX5070-SOLID-OC-RTL-HDMI-plus-3xDP-ZT-B50700J-10P_907082.html"
    ]


async def test_selfcheck_calls_a_refusal_inconclusive(monkeypatch):
    fake_tab(monkeypatch, NavBlocked(403, "blocked"))

    result = await server.nix_selfcheck()

    assert result.checks["search"].state == "inconclusive"
    assert result.checks["card"].state == "inconclusive"


async def test_a_www_link_is_read_from_the_saint_petersburg_site(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML.replace("https://www.nix.ru/", "https://spb.nix.ru/")))

    card = await server.nix_card(CARD_URL)

    assert urls == [CARD_URL.replace("https://www.nix.ru", "https://spb.nix.ru")]
    assert card.url.startswith("https://spb.nix.ru/autocatalog/")
