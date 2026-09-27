"""Avito through a faked Chrome tab: one navigation per call, the first refusal stops Avito, no second try."""

from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from avito_connector import server
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.error_handling import RetryMiddleware
from mcp_core.domtest import JsdomUnavailable, run_extractor
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport.chrome_cdp import NavBlocked, NavigationPolicyError

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_JSON = (FIXTURES / "js_items_live.json").read_text(encoding="utf-8")
HOSTS = frozenset({"avito.ru", "www.avito.ru"})
CARD_URL = "https://www.avito.ru/moskva/telefony/iphone_13_128_gb_1234567890"
CARD = {
    "page_title": "iPhone 13 128 ГБ купить в Москве | Авито",
    "firewall": False,
    "captcha": False,
    "title": "iPhone 13 128 ГБ",
    "price": "52000",
    "description": "Телефон в хорошем состоянии, царапины на рамке.",
    "params": ["Состояние: Б/у", "Встроенная память: 128 ГБ", "Цвет: синий"],
    "posted": "· 21 сентября в 14:02",
    "views": "1 204 просмотра",
    "photos": 4,
    "delivery": True,
    "seller_kind": "Частное лицо",
    "seller_rating": "4,9",
    "seller_reviews": "37 отзывов",
    "seller_since": "На Авито с марта 2019",
}
FIREWALL_CARD = {**CARD, "title": None, "firewall": True, "page_title": "Доступ ограничен"}


def json_doc(text: str, title: str = "") -> dict:
    return {"page_title": title, "size": len(text), "text": text}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="avito", gate=StopGate(None)))
    monkeypatch.setattr(server, "_min_gap", 0.0)
    monkeypatch.setattr(server, "_POLL_S", 0.0)
    monkeypatch.setattr(server, "_RENDER_WAIT_S", 0.0)
    yield
    server._cache.clear()


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


class FakePage:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def evaluate(self, expression, arg=None):
        return json.dumps(self.payload, ensure_ascii=False)


def fake_tab(monkeypatch, *outcomes) -> list[dict]:
    """Each outcome is a payload the tab returns, or an exception raised on navigation."""
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


# ------------------------------------------------------------ transport ----


def test_there_is_no_retry_middleware_and_no_http_tier():
    assert not any(isinstance(m, RetryMiddleware) for m in server.mcp.middleware)
    assert not hasattr(server, "_sync_curl_get")


async def test_search_opens_js_items_once_and_parses_the_live_payload(monkeypatch):
    calls = fake_tab(monkeypatch, json_doc(LIVE_JSON))

    result = await server.avito_search("ноутбук")

    assert calls == [
        {
            "url": "https://www.avito.ru/web/1/js/items?q=%D0%BD%D0%BE%D1%83%D1%82%D0%B1%D1%83%D0%BA&p=1"
            "&locationId=637640&context=&updateListOnly=true",
            "allowed_hosts": HOSTS,
        }
    ]
    assert [item.price_rub for item in result.items] == [69900.0, 17990.0, 14000.0]
    assert result.items[0].title == "Ноутбук Lenovo ThinkBook 16 G6 Новый"
    assert (result.items[0].seller_rating, result.items[0].seller_reviews) == (5.0, 721)
    assert result.items[0].category_id == 98
    assert (result.tier_used, result.meta.warnings) == ("cdp", [])


async def test_a_repeated_search_is_served_from_cache(monkeypatch):
    calls = fake_tab(monkeypatch, json_doc(LIVE_JSON))

    await server.avito_search("ноутбук")
    again = await server.avito_search("ноутбук")

    assert (len(calls), again.tier_used) == (1, "cache")


async def test_filters_go_into_the_url_and_price_bounds_are_reapplied(monkeypatch):
    calls = fake_tab(monkeypatch, json_doc(LIVE_JSON))

    result = await server.avito_search(
        "ноутбук", price_min=15000, price_max=70000, sort="price_asc", delivery_only=True, seller_type="private"
    )

    url = calls[0]["url"]
    for part in ("pmin=15000", "pmax=70000", "s=1", "d=1", "user=1"):
        assert part in url.split("?")[1].split("&")
    assert [item.price_rub for item in result.items] == [17990.0, 69900.0]
    assert any(w.startswith("filters_not_verified_live") for w in result.meta.warnings)


@pytest.mark.parametrize(("sort", "code"), [("price_desc", "2"), ("date", "104")])
async def test_sort_codes(monkeypatch, sort, code):
    calls = fake_tab(monkeypatch, json_doc(LIVE_JSON))

    await server.avito_search("ноутбук", sort=sort)

    assert f"s={code}" in calls[0]["url"]


async def test_a_page_without_a_matching_title_is_dropped_as_no_match(monkeypatch):
    fake_tab(monkeypatch, json_doc(LIVE_JSON))

    result = await server.avito_search("холодильник")

    assert result.items == []
    assert result.meta.warnings[0].startswith("no_match")


async def test_an_empty_page_with_a_nonzero_total_is_warned(monkeypatch):
    fake_tab(monkeypatch, json_doc(json.dumps({"catalog": {"items": []}, "totalCount": 12})))

    result = await server.avito_search("ноутбук")

    assert "empty_items_with_nonzero_total" in result.meta.warnings


@pytest.mark.parametrize(
    "outcome",
    [
        NavBlocked(401, "https://www.avito.ru/web/1/js/items"),
        NavBlocked(403, "https://www.avito.ru/web/1/js/items"),
        NavBlocked(418, "https://www.avito.ru/web/1/js/items"),
        NavBlocked(429, "https://www.avito.ru/web/1/js/items"),
        NavigationPolicyError("https://www.avito.ru.evil/", HOSTS),
        json_doc("Доступ ограничен: проблема с IP", title="Доступ ограничен"),
        json_doc('{"firewall": {"captcha": true}}'),
        json_doc('{"status": "too-many-requests"}'),
    ],
    ids=["401", "403", "418", "429", "off-site", "block-page", "firewall-json", "too-many"],
)
async def test_the_first_refusal_stops_avito_without_a_second_request(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome, json_doc(LIVE_JSON))

    with pytest.raises(ToolError) as first:
        await server.avito_search("ноутбук")
    with pytest.raises(ToolError) as second:
        await server.avito_search("ноутбук б/у")

    assert error_payload(first.value)["error"] in {"transport_down", "challenge_required", "rate_limited"}
    assert error_payload(second.value)["source_stopped"] is True
    assert len(calls) == 1


async def test_a_server_error_is_transport_down_not_a_stop(monkeypatch):
    calls = fake_tab(monkeypatch, NavBlocked(502, "https://www.avito.ru/web/1/js/items"), json_doc(LIVE_JSON))

    with pytest.raises(ToolError) as excinfo:
        await server.avito_search("ноутбук")

    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.avito_search("ноутбук")).count == 3
    assert len(calls) == 2


async def test_an_oversized_body_is_transport_down(monkeypatch):
    monkeypatch.setattr(server, "MAX_BODY_CHARS", 100)
    fake_tab(monkeypatch, {"page_title": "", "size": 101, "text": ""})

    with pytest.raises(ToolError) as excinfo:
        await server.avito_search("ноутбук")

    assert error_payload(excinfo.value)["error"] == "transport_down"


@pytest.mark.parametrize(
    "outcome",
    [json_doc(""), json_doc('{"catalog": {"items": [{"id": 1, "title": "Ноу')],
    ids=["empty", "truncated"],
)
async def test_an_unfinished_answer_is_transport_down_not_a_stop(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome, json_doc(LIVE_JSON))

    with pytest.raises(ToolError) as excinfo:
        await server.avito_search("ноутбук")

    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.avito_search("ноутбук")).count == 3
    assert len(calls) == 2


async def test_a_listing_that_mentions_a_firewall_is_not_a_refusal(monkeypatch):
    payload = json.loads(LIVE_JSON)
    payload = {"catalog": payload["catalog"], "totalCount": 3}
    payload["catalog"]["items"][0]["title"] = "Ноутбук с firewall и too-many-requests в названии"
    fake_tab(monkeypatch, json_doc(json.dumps(payload, ensure_ascii=False)))

    result = await server.avito_search("ноутбук")

    assert result.count == 3


async def test_search_rows_never_carry_a_seller_name(monkeypatch):
    payload = json.loads(LIVE_JSON)
    payload["catalog"]["items"][0]["seller"] = {"name": "Продавец Тестовый", "id": 42, "isCompany": False}
    fake_tab(monkeypatch, json_doc(json.dumps(payload, ensure_ascii=False)))

    result = await server.avito_search("ноутбук")

    row = result.items[0].model_dump()
    assert "seller_name" not in row and "seller_id" not in row
    assert "Продавец Тестовый" not in json.dumps(result.model_dump(), ensure_ascii=False)
    assert row["is_company"] is False


async def test_json_that_is_not_an_object_is_parser_drift(monkeypatch):
    fake_tab(monkeypatch, json_doc("[1, 2]"))

    with pytest.raises(ToolError) as excinfo:
        await server.avito_search("ноутбук")

    assert error_payload(excinfo.value)["error"] == "parser_drift"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"location_id": "moscow"}, "location_id"),
        ({"category_id": "phones"}, "category_id"),
        ({"price_min": 5000, "price_max": 1000}, "price_min"),
        ({"sort": "cheapest"}, "sort"),
        ({"seller_type": "shop"}, "seller_type"),
    ],
)
async def test_search_rejects_bad_arguments_before_any_request(monkeypatch, kwargs, match):
    calls = fake_tab(monkeypatch)

    with pytest.raises(ToolError, match=match):
        await server.avito_search("ноутбук", **kwargs)
    assert calls == []


def test_the_json_reader_takes_the_text_of_chrome_s_json_document(tmp_path):
    page = tmp_path / "js_items.html"
    page.write_text(f"<html><head></head><body><pre>{html.escape(LIVE_JSON)}</pre></body></html>", encoding="utf-8")
    try:
        raw = run_extractor(server._JSON_READ_JS, page.resolve(), page_url="https://www.avito.ru/web/1/js/items?q=x")
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))

    payload = raw if isinstance(raw, dict) else json.loads(raw)
    assert json.loads(payload["text"]) == json.loads(LIVE_JSON)
    assert payload["size"] == len(payload["text"])


# ----------------------------------------------------------------- card ----


async def test_card_reads_the_item_page(monkeypatch):
    calls = fake_tab(monkeypatch, CARD)

    card = await server.avito_card(CARD_URL + "?context=abc")

    assert calls == [{"url": CARD_URL, "allowed_hosts": HOSTS}]
    assert (card.item_id, card.title, card.price_rub, card.photos, card.delivery) == (
        "1234567890",
        "iPhone 13 128 ГБ",
        52000.0,
        4,
        True,
    )
    assert card.params == ["Состояние: Б/у", "Встроенная память: 128 ГБ", "Цвет: синий"]
    assert card.views == 1204
    assert (card.seller.is_company, card.seller.rating_score, card.seller.rating_count, card.seller.since_year) == (
        False,
        4.9,
        37,
        2019,
    )
    assert card.meta.warnings[0].startswith("card_not_verified_live")


@pytest.mark.parametrize(
    "bad",
    [
        "1234567890",
        "https://www.avito.ru/moskva/telefony",
        "https://evil.example/moskva/telefony/iphone_1234567890",
        "https://user@www.avito.ru/moskva/telefony/iphone_1234567890",
        "ftp://www.avito.ru/moskva/telefony/iphone_1234567890",
    ],
)
async def test_card_takes_only_an_avito_item_url(monkeypatch, bad):
    calls = fake_tab(monkeypatch)

    with pytest.raises(ToolError, match="bad_request"):
        await server.avito_card(bad)
    assert calls == []


async def test_a_missing_item_is_not_found_not_a_stop(monkeypatch):
    fake_tab(monkeypatch, NavBlocked(404, CARD_URL), CARD)

    with pytest.raises(ToolError) as excinfo:
        await server.avito_card(CARD_URL)

    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.avito_card(CARD_URL)).title == "iPhone 13 128 ГБ"


@pytest.mark.parametrize(
    "outcome",
    [NavBlocked(429, CARD_URL), FIREWALL_CARD, {**CARD, "title": None, "captcha": True}],
    ids=["429", "firewall", "captcha"],
)
async def test_a_refused_card_stops_avito(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome, CARD)

    with pytest.raises(ToolError):
        await server.avito_card(CARD_URL)
    with pytest.raises(ToolError) as excinfo:
        await server.avito_card(CARD_URL)

    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(calls) == 1


async def test_a_card_page_without_a_title_is_drift(monkeypatch):
    fake_tab(monkeypatch, {**CARD, "title": None, "price": None})

    with pytest.raises(ToolError) as excinfo:
        await server.avito_card(CARD_URL)

    assert error_payload(excinfo.value)["error"] == "parser_drift"


def test_the_card_extractor_on_the_synthetic_page():
    try:
        payload = run_extractor(
            server._CARD_EXTRACT_JS, (FIXTURES / "card_synthetic.html").resolve(), page_url=CARD_URL
        )
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))

    payload = payload if isinstance(payload, dict) else json.loads(payload)
    assert {k: payload[k] for k in ("title", "price", "photos", "delivery", "firewall", "captcha")} == {
        "title": "iPhone 13 128 ГБ",
        "price": "52000",
        "photos": 4,
        "delivery": True,
        "firewall": False,
        "captcha": False,
    }
    assert payload["params"] == ["Состояние: Б/у", "Встроенная память: 128 ГБ", "Цвет: синий"]
    assert (payload["seller_kind"], payload["seller_rating"], payload["seller_reviews"]) == (
        "Частное лицо",
        "4,9",
        "37 отзывов",
    )
    assert payload["seller_since"] == "На Авито с марта 2019"
    assert payload["description"].startswith("Телефон в хорошем состоянии")


# ------------------------------------------------------------ selfcheck ----


async def test_selfcheck_is_healthy_on_the_live_payload(monkeypatch):
    fake_tab(monkeypatch, json_doc(LIVE_JSON))

    result = await server.avito_selfcheck()

    assert result.checks["search"].state == "healthy"


async def test_selfcheck_maps_a_refusal_to_inconclusive_never_drift(monkeypatch):
    fake_tab(monkeypatch, NavBlocked(429, "https://www.avito.ru/web/1/js/items"))

    result = await server.avito_selfcheck()

    assert result.checks["search"].state == "inconclusive"


async def test_selfcheck_flags_drift_when_a_key_family_vanishes(monkeypatch):
    payload = json.loads(LIVE_JSON)
    for item in payload["catalog"]["items"]:
        item["idRenamed"] = item.pop("id")
    fake_tab(monkeypatch, json_doc(json.dumps(payload, ensure_ascii=False)))

    result = await server.avito_selfcheck()

    assert result.checks["search"].state == "drift"
    assert any("items[].id" in note for note in result.checks["search"].notes)
