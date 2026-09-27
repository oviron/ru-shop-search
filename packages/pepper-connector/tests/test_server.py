"""Offline tests for the Pepper connector through a faked Chrome tab: refusals stop Pepper, deals are never shop prices."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastmcp.exceptions import ToolError
from mcp_core.pacing import Pacer
from mcp_core.stopgate import StopGate
from mcp_core.transport.chrome_cdp import NavBlocked, NavigationPolicyError
from pepper_connector import server

TITLE = "Pepper.ru — лучшие скидки и промокоды в России"
CARD_URL = "https://www.pepper.ru/deals/5070-694148"


def deal(deal_id: str, slug: str, title: str, price: str | None, **extra) -> dict:
    return {
        "deal_id": deal_id,
        "store": "Wildberries",
        "href": f"/deals/{slug}-{deal_id}",
        "title": title,
        "price": price,
        "old": None,
        "stamp": "10 дней назад",
        "hot": "964°",
        **extra,
    }


# Live 2026-09-27 22:42:44, «rtx 5070» (search_rtx5070_live.html): two of the four deals.
RTX = {
    "title": TITLE,
    "captcha": False,
    "results": True,
    "nothing": False,
    "total": "Показать все (4)",
    "deals": [
        deal("694148", "5070", "Видеокарта MSI RTX 5070 SHADOW 2X OC 12 ГБ (с ВБ кошельком)", "75 000₽"),
        deal(
            "689146",
            "mechrevo-yaoshi16-utral-noutbuk-igrovoy",
            'Игровой ноутбук MECHREVO YaoShi16 Utral: 16", GeForce RTX 5070 Ti (12 Гб)',
            "219 404₽",
            store="OZON",
            stamp="Истекло",
            hot="-64°",
        ),
    ],
}
NOTHING = {"title": TITLE, "captcha": False, "results": True, "nothing": True, "total": "Показать все (0)", "deals": []}
CARD = {
    "title": "Видеокарта MSI RTX 5070 SHADOW 2X OC 12 ГБ (с ВБ кошельком) | Pepper",
    "captcha": False,
    "products": 1,
    "name": "Видеокарта MSI RTX 5070 SHADOW 2X OC 12 ГБ (с ВБ кошельком)",
    "product_url": CARD_URL,
    "merchant": "Wildberries",
    "price": "75000.0",
    "currency": "RUB",
    "valid_until": "2026-09-30 00:59:00 +0300",
    "availability": "https://schema.org/InStock",
    "published": "2026-09-17T14:43:13+03:00",
    "h1": "Видеокарта MSI RTX 5070 SHADOW 2X OC 12 ГБ (с ВБ кошельком)",
    "hot": "964°",
    "expired_notice": False,
}
CAPTCHA = {
    "title": "",
    "captcha": True,
    "checking": False,
    "results": False,
    "nothing": False,
    "deals": [],
    "products": 0,
}
CHECKING = {**CAPTCHA, "captcha": False, "checking": True}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server._cache.clear()
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="pepper", gate=StopGate(None)))
    monkeypatch.setattr(server, "_POLL_S", 0.0)
    monkeypatch.setattr(server, "_min_gap", 0.0)
    monkeypatch.setattr(server, "_RENDER_WAIT_S", 0.0)
    yield
    server._cache.clear()


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


class FakePage:
    """Answers each poll with the next payload; the last one repeats."""

    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)

    async def evaluate(self, expression, arg=None):
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return json.dumps(payload, ensure_ascii=False)


def fake_tab(monkeypatch, *outcomes) -> list[dict]:
    """Each outcome is a page (FakePage or payload) or an exception raised on navigation."""
    calls: list[dict] = []
    queue = list(outcomes)

    @asynccontextmanager
    async def open_page(url, wait_ms=5000, **kwargs):
        calls.append({"url": url, **kwargs})
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome if isinstance(outcome, FakePage) else FakePage(outcome)

    monkeypatch.setattr(server, "open_page", open_page)
    return calls


# ----------------------------------------------------------------- search ----


async def test_search_opens_the_site_search_and_returns_deals(monkeypatch):
    calls = fake_tab(monkeypatch, RTX)

    result = await server.pepper_search("rtx 5070")

    assert calls == [
        {
            "url": "https://www.pepper.ru/selective_search?keyword=rtx+5070&search_list=deals",
            "allowed_hosts": frozenset({"pepper.ru", "www.pepper.ru"}),
        }
    ]
    assert (result.total_available, result.count, result.tier_used) == (4, 2, "cdp")
    fresh, expired = result.items
    assert (fresh.deal_id, fresh.merchant, fresh.deal_price_rub, fresh.posted, fresh.expired, fresh.temperature) == (
        "694148",
        "Wildberries",
        75000.0,
        "10 дней назад",
        False,
        964,
    )
    assert fresh.url == CARD_URL
    assert (expired.expired, expired.posted, expired.temperature) == (True, None, -64)
    assert result.meta.warnings == []


async def test_a_repeated_search_is_served_from_cache(monkeypatch):
    calls = fake_tab(monkeypatch, RTX)

    await server.pepper_search("rtx 5070")
    again = await server.pepper_search("rtx  5070")

    assert (len(calls), again.tier_used, again.count) == (1, "cache", 2)


async def test_old_price_is_kept_only_above_the_deal_price(monkeypatch):
    rows = [
        deal("694666", "asus-tuf", "Ноутбук ASUS TUF RTX 5070", "142 883₽", old="182 418₽"),
        deal("100001", "rtx-5070-a", "RTX 5070 A", "50 000₽", old="40 000₽"),
    ]
    fake_tab(monkeypatch, {**RTX, "deals": rows})

    result = await server.pepper_search("rtx 5070")

    assert [(i.deal_price_rub, i.old_price_rub) for i in result.items] == [(142883.0, 182418.0), (50000.0, None)]


async def test_a_deal_without_a_price_keeps_none_never_zero(monkeypatch):
    fake_tab(monkeypatch, {**RTX, "deals": [deal("100002", "rtx-5070", "RTX 5070 промокод", None)]})

    result = await server.pepper_search("rtx 5070")

    assert result.items[0].deal_price_rub is None


async def test_a_row_whose_link_is_not_its_deal_is_skipped(monkeypatch):
    rows = [RTX["deals"][0], {**deal("100003", "rtx-5070", "RTX 5070", "1₽"), "href": "/visit/deals/100003"}]
    fake_tab(monkeypatch, {**RTX, "deals": rows})

    result = await server.pepper_search("rtx 5070")

    assert [i.deal_id for i in result.items] == ["694148"]
    assert result.meta.warnings == ["row_skipped: a deal came without a link to its own page"]


async def test_limit_cuts_the_list(monkeypatch):
    fake_tab(monkeypatch, RTX)

    result = await server.pepper_search("rtx 5070", limit=1)

    assert [i.deal_id for i in result.items] == ["694148"]


async def test_deals_that_do_not_name_the_query_are_no_match(monkeypatch):
    fake_tab(monkeypatch, RTX)

    result = await server.pepper_search("холодильник")

    assert (result.count, result.items, result.total_available) == (0, [], 0)
    assert result.meta.warnings[0].startswith("no_match")


async def test_nothing_found_is_empty_not_drift(monkeypatch):
    fake_tab(monkeypatch, NOTHING)

    result = await server.pepper_search("абырвалгщзх")

    assert (result.count, result.total_available) == (0, 0)
    assert result.meta.warnings == ["empty: Pepper has no deals for 'абырвалгщзх'"]


async def test_a_pepper_page_with_neither_deals_nor_empty_state_is_drift_not_a_stop(monkeypatch):
    calls = fake_tab(monkeypatch, {**NOTHING, "nothing": False}, RTX)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "parser_drift"
    assert (await server.pepper_search("rtx 5070")).count == 2
    assert len(calls) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        NavBlocked(401, "https://www.pepper.ru/selective_search"),
        NavBlocked(403, "https://www.pepper.ru/selective_search"),
        NavBlocked(418, "https://www.pepper.ru/selective_search"),
        NavBlocked(429, "https://www.pepper.ru/selective_search"),
        NavigationPolicyError("https://sso.example.ru/", {"pepper.ru", "www.pepper.ru"}),
        CAPTCHA,
        {**NOTHING, "title": "Доступ ограничен", "nothing": False},
    ],
)
async def test_a_refused_tab_stops_pepper(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome, RTX)

    with pytest.raises(ToolError):
        await server.pepper_search("rtx 5070")
    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070 ti")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(calls) == 1


async def test_the_js_check_is_waited_out_not_a_stop(monkeypatch):
    monkeypatch.setattr(server, "_RENDER_WAIT_S", 5.0)
    calls = fake_tab(monkeypatch, FakePage(CHECKING, CHECKING, RTX), RTX)

    assert (await server.pepper_search("rtx 5070")).count == 2
    assert (await server.pepper_search("rtx 5070 ti")).count == 2
    assert len(calls) == 2


async def test_a_js_check_that_never_passes_stops_pepper(monkeypatch):
    calls = fake_tab(monkeypatch, CHECKING, RTX)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "challenge_required"
    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070 ti")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(calls) == 1


async def test_a_captcha_is_challenge_required(monkeypatch):
    fake_tab(monkeypatch, CAPTCHA)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "challenge_required"


async def test_a_failing_page_is_transport_down_not_a_stop(monkeypatch):
    calls = fake_tab(monkeypatch, NavBlocked(502, ""), RTX)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.pepper_search("rtx 5070")).count == 2
    assert len(calls) == 2


async def test_a_page_that_never_loaded_is_transport_down_not_a_stop(monkeypatch):
    calls = fake_tab(monkeypatch, {**NOTHING, "title": "", "nothing": False}, RTX)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070")
    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.pepper_search("rtx 5070")).count == 2
    assert len(calls) == 2


# ------------------------------------------------------------------- card ----


async def test_card_reads_the_deal(monkeypatch):
    calls = fake_tab(monkeypatch, CARD)

    card = await server.pepper_card("pepper.ru/deals/5070-694148?utm_source=x")

    assert [c["url"] for c in calls] == [CARD_URL]
    assert (card.deal_id, card.merchant, card.deal_price_rub, card.expired, card.temperature, card.url) == (
        "694148",
        "Wildberries",
        75000.0,
        False,
        964,
        CARD_URL,
    )
    assert (card.published_at, card.valid_until) == ("2026-09-17T14:43:13+03:00", "2026-09-30 00:59:00 +0300")
    assert card.meta.warnings == []


@pytest.mark.parametrize(
    "change",
    [{"availability": "https://schema.org/OutOfStock"}, {"expired_notice": True}],
)
async def test_an_ended_deal_is_expired(monkeypatch, change):
    fake_tab(monkeypatch, {**CARD, **change})

    card = await server.pepper_card(CARD_URL)

    assert card.expired is True
    assert card.meta.warnings == ["expired: the site says this deal has ended"]


async def test_a_foreign_currency_is_not_a_ruble_price(monkeypatch):
    fake_tab(monkeypatch, {**CARD, "currency": "USD"})

    card = await server.pepper_card(CARD_URL)

    assert card.deal_price_rub is None
    assert card.meta.warnings == ["no_price: the deal page names no price"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/deals/5070-694148",
        "https://www.pepper.ru/coupons/wildberries.ru",
        "https://www.pepper.ru/visit/deals/694148",
        "https://user@www.pepper.ru/deals/5070-694148",
        "ftp://www.pepper.ru/deals/5070-694148",
    ],
)
async def test_card_refuses_anything_but_a_pepper_deal_page(monkeypatch, url):
    calls = fake_tab(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_card(url)
    assert error_payload(excinfo.value)["error"] == "bad_request"
    assert calls == []


async def test_a_missing_deal_is_not_found_not_a_stop(monkeypatch):
    calls = fake_tab(monkeypatch, NavBlocked(404, CARD_URL), CARD)

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "not_found"
    assert (await server.pepper_card(CARD_URL)).deal_price_rub == 75000.0
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [{"product_url": "https://www.pepper.ru/deals/other-100004"}, {"products": 2}],
)
async def test_a_page_for_another_deal_is_drift(monkeypatch, change):
    fake_tab(monkeypatch, {**CARD, **change})

    with pytest.raises(ToolError) as excinfo:
        await server.pepper_card(CARD_URL)
    assert error_payload(excinfo.value)["error"] == "parser_drift"


@pytest.mark.parametrize("outcome", [NavBlocked(403, CARD_URL), CAPTCHA])
async def test_a_refused_card_stops_pepper(monkeypatch, outcome):
    calls = fake_tab(monkeypatch, outcome, CARD)

    with pytest.raises(ToolError):
        await server.pepper_card(CARD_URL)
    with pytest.raises(ToolError) as excinfo:
        await server.pepper_search("rtx 5070")
    assert error_payload(excinfo.value)["source_stopped"] is True
    assert len(calls) == 1


async def test_a_repeated_card_is_served_from_cache(monkeypatch):
    calls = fake_tab(monkeypatch, CARD)

    await server.pepper_card(CARD_URL)
    again = await server.pepper_card(CARD_URL)

    assert (len(calls), again.tier_used) == (1, "cache")


@pytest.mark.parametrize("tool", ["search", "card"])
async def test_an_unexpected_tab_failure_is_transport_down_not_a_stop(monkeypatch, tool):
    calls = fake_tab(monkeypatch, RuntimeError("CDP autostart failed"), TimeoutError(), RTX)
    call = (lambda: server.pepper_search("rtx 5070")) if tool == "search" else (lambda: server.pepper_card(CARD_URL))

    for _ in range(2):
        with pytest.raises(ToolError) as excinfo:
            await call()
        assert error_payload(excinfo.value)["error"] == "transport_down"
    assert (await server.pepper_search("rtx 5070")).count == 2
    assert len(calls) == 3
