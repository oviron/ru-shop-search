"""Offline tests for the Lamoda connector: live pages from fixtures, the network stubbed at curl_cffi."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lamoda_connector import server
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport import impersonated
from mcp_core.transport.impersonated import Reply

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "search_live.html").read_text(encoding="utf-8")
CARD_HTML = (FIXTURES / "card_live.html").read_text(encoding="utf-8")
CARD_URL = "https://www.lamoda.ru/p/rtlaeu059301/shoes-adidasoriginals-kedy/"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="lamoda", gate=StopGate(None)))
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


def edited_search(edit) -> str:
    match = server._PAYLOAD_RE.search(SEARCH_HTML)
    data, end = json.JSONDecoder().raw_decode(SEARCH_HTML, match.end())
    edit(data)
    return SEARCH_HTML[: match.end()] + json.dumps(data, ensure_ascii=False) + SEARCH_HTML[end:]


# ------------------------------------------------------------------ search ----


async def test_search_reads_the_sites_own_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML))

    result = await server.lamoda_search("кроссовки  adidas")

    assert urls == [
        "https://www.lamoda.ru/catalogsearch/result/?q=%D0%BA%D1%80%D0%BE%D1%81%D1%81%D0%BE%D0%B2%D0%BA%D0%B8%20adidas"
    ]
    assert (result.tier_used, result.total_available, result.count) == ("http", 3878, 5)
    assert [(i.sku, i.price_rub, i.old_price_rub) for i in result.items] == [
        ("RTLAEU059301", 8063.0, 15999.0),
        ("RTLAES035701", 6299.0, 8999.0),
        ("RTLAES219202", 8999.0, None),
        ("RTLADF666602", 7999.0, 16999.0),
        ("RTLAEN425302", 7999.0, None),
    ]
    assert result.meta.warnings == []


async def test_search_row_carries_title_brand_rating_stock_and_url(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    first = (await server.lamoda_search("кроссовки adidas")).items[0]

    assert (first.title, first.brand) == ("Кеды HANDBALL SPEZIAL W", "adidas Originals")
    assert (first.rating, first.review_count, first.in_stock) == (4.75, 95, True)
    assert first.promo_until == "2026-09-27T23:59:59.000+03:00"
    assert first.url == CARD_URL


async def test_a_row_without_reviews_has_no_rating(monkeypatch):
    stub_http(monkeypatch, ok(SEARCH_HTML))

    last = (await server.lamoda_search("кроссовки adidas")).items[4]

    assert (last.rating, last.review_count) == (None, None)


async def test_a_promo_code_step_is_not_the_price(monkeypatch):
    def needs_code(data):
        data["products"][0]["prices"][2]["discount"]["promocode"] = "AUTUMN"

    stub_http(monkeypatch, ok(edited_search(needs_code)))

    result = await server.lamoda_search("кроссовки adidas")

    assert (result.items[0].price_rub, result.items[0].promo_until) == (11199.0, None)
    assert result.meta.warnings == [
        "RTLAEU059301: price_step_skipped: onsite_promo discount needs a code or is unknown"
    ]


async def test_no_size_in_stock_means_out_of_stock(monkeypatch):
    def sold_out(data):
        for size in data["products"][0]["sizes"]:
            size["is_available"] = False

    stub_http(monkeypatch, ok(edited_search(sold_out)))

    assert (await server.lamoda_search("кроссовки adidas")).items[0].in_stock is False


async def test_unknown_sellability_is_unknown_stock(monkeypatch):
    def unknown(data):
        del data["products"][0]["is_sellable"]

    stub_http(monkeypatch, ok(edited_search(unknown)))

    assert (await server.lamoda_search("кроссовки adidas")).items[0].in_stock is None


async def test_a_row_without_sku_is_skipped_and_reported(monkeypatch):
    def no_sku(data):
        data["products"][1]["sku_id"] = data["products"][1].pop("sku")

    stub_http(monkeypatch, ok(edited_search(no_sku)))

    result = await server.lamoda_search("кроссовки adidas")

    assert [i.sku for i in result.items] == ["RTLAEU059301", "RTLAES219202", "RTLADF666602", "RTLAEN425302"]
    assert result.meta.warnings == ["row_skipped: a search row came without a sku"]


async def test_a_row_without_prices_is_reported(monkeypatch):
    def no_prices(data):
        data["products"][2]["prices"] = []

    stub_http(monkeypatch, ok(edited_search(no_prices)))

    result = await server.lamoda_search("кроссовки adidas")

    assert result.items[2].price_rub is None
    assert result.meta.warnings == ["RTLAES219202: no_price"]


async def test_an_empty_page_is_an_honest_empty_result(monkeypatch):
    def nothing(data):
        data["products"] = []
        data["pagination"]["found"] = 0

    stub_http(monkeypatch, ok(edited_search(nothing)))

    result = await server.lamoda_search("zxqvwj пплк")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: Lamoda found nothing for 'zxqvwj пплк'"]


async def test_a_corrected_query_is_reported(monkeypatch):
    def corrected(data):
        data["products_meta"].update(is_predicted_term_corrected=True, predicted_term="кроссовки adidas")

    stub_http(monkeypatch, ok(edited_search(corrected)))

    result = await server.lamoda_search("кросовки адидас")

    assert result.meta.warnings == ["query_corrected: Lamoda searched for 'кроссовки adidas' instead"]


async def test_a_page_without_payload_is_drift(monkeypatch):
    stub_http(monkeypatch, ok("<html><body>новый фронтенд</body></html>"))

    with pytest.raises(ToolError) as excinfo:
        await server.lamoda_search("кроссовки")
    assert error_payload(excinfo.value)["error"] == "parser_drift"


@pytest.mark.parametrize(
    "reply",
    [
        Reply(403, "<html>ServicePipe</html>", None),
        Reply(429, "", None),
        Reply(307, "", None, location="/catalogsearch/result/?q=a&__rr=1"),
        Reply(200, "<html><title>Проверка</title><script src='/servicepipe.js'></script></html>", None),
    ],
    ids=["403", "429", "redirect", "anti-bot page"],
)
async def test_a_refusal_stops_lamoda_after_one_request(monkeypatch, reply):
    urls = stub_http(monkeypatch, reply, ok(SEARCH_HTML))

    with pytest.raises(ToolError):
        await server.lamoda_search("кроссовки")
    with pytest.raises(ToolError) as excinfo:
        await server.lamoda_search("кроссовки")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(urls) == 1


@pytest.mark.parametrize(
    ("reply", "status", "challenge_type"),
    [
        (Reply(307, "", None, location="/?__rr=1"), 307, "redirect"),
        (Reply(200, "<html><script src='/servicepipe.js'></script></html>", None), 200, "antibot_page"),
    ],
)
async def test_a_refusal_names_what_lamoda_actually_answered(monkeypatch, reply, status, challenge_type):
    stub_http(monkeypatch, reply)

    with pytest.raises(ToolError) as excinfo:
        await server.lamoda_search("кроссовки")
    payload = error_payload(excinfo.value)
    assert (payload["error"], payload["status_code"], payload["challenge_type"]) == (
        "challenge_required",
        status,
        challenge_type,
    )


# -------------------------------------------------------------------- card ----


async def test_card_reads_the_product_page(monkeypatch):
    urls = stub_http(monkeypatch, ok(CARD_HTML))

    card = await server.lamoda_card(CARD_URL + "?utm=x")

    assert urls == [CARD_URL]
    assert (card.sku, card.title, card.brand) == ("RTLAEU059301", "Кеды HANDBALL SPEZIAL W", "adidas Originals")
    assert (card.price_rub, card.old_price_rub, card.promo_until) == (8063.0, 15999.0, "2026-09-27T23:59:59.000+03:00")
    assert (card.rating, card.review_count, card.seller, card.is_available) == (4.7, 95, "Lamoda", True)
    assert [s.size for s in card.sizes if s.is_available] == ["35,5", "36", "37", "37,5", "38,5", "40"]
    assert card.url == CARD_URL
    assert card.meta.warnings == []


async def test_a_bare_sku_follows_the_canonical_redirect_once(monkeypatch):
    urls = stub_http(
        monkeypatch,
        Reply(301, "", None, location="/p/rtlaeu059301/shoes-adidasoriginals-kedy/"),
        ok(CARD_HTML),
    )

    card = await server.lamoda_card("RTLAEU059301")

    assert urls == ["https://www.lamoda.ru/p/rtlaeu059301/", CARD_URL]
    assert card.price_rub == 8063.0


async def test_a_redirect_elsewhere_stops_lamoda(monkeypatch):
    urls = stub_http(monkeypatch, Reply(302, "", None, location="https://www.lamoda.ru/?__rr=1"))

    with pytest.raises(ToolError) as excinfo:
        await server.lamoda_card("RTLAEU059301")
    assert error_payload(excinfo.value)["error"] == "challenge_required"
    assert len(urls) == 1


async def test_an_unknown_sku_is_not_found(monkeypatch):
    stub_http(monkeypatch, Reply(404, "<html>404</html>", None))

    with pytest.raises(ToolError) as excinfo:
        await server.lamoda_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "not_found"


@pytest.mark.parametrize(
    "bad", ["", "   ", "not a sku", "https://evil.example/p/rtlaeu059301/", "https://www.lamoda.ru/c/17/shoes/"]
)
async def test_card_rejects_input_without_a_lamoda_sku(bad):
    with pytest.raises(ToolError) as excinfo:
        await server.lamoda_card(bad)
    assert error_payload(excinfo.value)["error"] == "bad_request"


def test_extract_sku():
    assert server._extract_sku("MP002XM1RMM3") == "MP002XM1RMM3"
    assert server._extract_sku("https://www.lamoda.ru/p/mp002xm1rmm3/shoes-nike/") == "MP002XM1RMM3"
    assert server._extract_sku("!!") is None


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_healthy_after_search_and_card(monkeypatch):
    urls = stub_http(monkeypatch, ok(SEARCH_HTML), ok(CARD_HTML))

    result = await server.lamoda_selfcheck()

    assert result.status == "success"
    assert {name: check.state for name, check in result.checks.items()} == {"search": "healthy", "card": "healthy"}
    assert urls[1] == CARD_URL


async def test_selfcheck_refusal_is_inconclusive_and_skips_the_card(monkeypatch):
    urls = stub_http(monkeypatch, Reply(403, "", None))

    result = await server.lamoda_selfcheck()

    assert {name: check.state for name, check in result.checks.items()} == {
        "search": "inconclusive",
        "card": "inconclusive",
    }
    assert len(urls) == 1


async def test_selfcheck_drift_when_the_payload_moves(monkeypatch):
    stub_http(monkeypatch, ok(re.sub(r"payload:", "data2:", SEARCH_HTML)))

    result = await server.lamoda_selfcheck()

    assert result.checks["search"].state == "drift"
