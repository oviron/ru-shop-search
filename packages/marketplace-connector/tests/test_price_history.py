"""price_history: история магазина (WB, DNS) и свои снимки цен из search и compare."""

from __future__ import annotations

import datetime
import json

import pytest
from compare_connector.models_output import CompareResponse, MarketOffer
from dns_connector.models_output import DnsPriceHistoryResponse, DnsPricePoint
from fastmcp.exceptions import ToolError
from marketplace_connector import server, snapshots
from mcp_core.stopgate import default_gate
from wb_connector.models_output import WbPriceHistoryResponse, WbPricePoint

WB_URL = "https://www.wildberries.ru/catalog/445317378/detail.aspx?targetUrl=GP"
DNS_URL = "https://www.dns-shop.ru/product/b58aaa7e00a9d582/videokarta-palit/"
DAY1 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.UTC)
DAY2 = datetime.datetime(2026, 9, 27, 10, 0, tzinfo=datetime.UTC)


def wb_offer(price: float) -> MarketOffer:
    return MarketOffer(source="wildberries", product_id="445317378", title="RTX 5070", price_rub=price, url=WB_URL)


def test_a_snapshot_is_kept_per_day_and_price():
    store = snapshots.Snapshots()
    store.record([wb_offer(92104.0)], now=DAY1)
    store.record([wb_offer(92104.0)], now=DAY1 + datetime.timedelta(hours=3))
    store.record([wb_offer(90000.0)], now=DAY1 + datetime.timedelta(hours=5))
    store.record([wb_offer(90000.0)], now=DAY2)

    rows = store.history("wildberries", product_id="445317378")

    assert [(r.observed_at[:10], r.price_rub) for r in rows] == [
        ("2026-09-26", 92104.0),
        ("2026-09-26", 90000.0),
        ("2026-09-27", 90000.0),
    ]


def test_rows_without_an_id_or_a_rouble_price_are_not_kept():
    store = snapshots.Snapshots()
    store.record(
        [
            MarketOffer(source="dns", title="без id", price_rub=1.0),
            MarketOffer(source="dns", product_id="a", title="без цены", price_rub=None),
            MarketOffer(source="aliexpress", product_id="b", price_rub=None, currency="cny", price_native=3.0),
        ],
        now=DAY1,
    )

    assert store.history("dns", product_id="a") == []
    assert store.history("aliexpress", product_id="b") == []


def test_a_conditional_price_is_kept_as_the_price_without_the_condition():
    store = snapshots.Snapshots()
    store.record(
        [
            MarketOffer(
                source="nix",
                product_id="1",
                price_rub=109296.0,
                price_condition="superprice",
                price_regular_rub=112683.0,
            ),
            MarketOffer(source="ozon", product_id="2", price_rub=106011.0, price_condition="ozon_card"),
        ],
        now=DAY1,
    )

    assert [r.price_rub for r in store.history("nix", product_id="1")] == [112683.0]
    assert store.history("ozon", product_id="2") == []


def test_a_snapshot_is_found_by_its_url_without_the_query():
    store = snapshots.Snapshots()
    store.record([wb_offer(92104.0)], now=DAY1)

    rows = store.history("wildberries", url="https://www.wildberries.ru/catalog/445317378/detail.aspx/")

    assert [r.price_rub for r in rows] == [92104.0]


def test_snapshots_live_in_the_plugin_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    snapshots.Snapshots().record([wb_offer(1.0)], now=DAY1)

    assert (tmp_path / "price_snapshots.sqlite").exists()


@pytest.fixture
def shops(monkeypatch):
    state = {"calls": []}

    class Wb:
        async def wb_price_history(self, nm_id):
            state["calls"].append(("wb", nm_id))
            if "wb_error" in state:
                raise state["wb_error"]
            points = [
                WbPricePoint(week="2026-09-06", price_rub=67542.6),
                WbPricePoint(week="2026-09-13", price_rub=67089.0),
            ]
            return WbPriceHistoryResponse(nm_id=nm_id, points=points)

    class Dns:
        async def dns_price_history(self, product_url):
            state["calls"].append(("dns", product_url))
            points = [
                DnsPricePoint(week="2026-04-13", label="13.04-19.04", price_rub=57999.0),
                DnsPricePoint(week="2026-09-14", label="14.09-20.09", price_rub=89999.0),
                DnsPricePoint(week="2026-09-21", label="21.09-27.09", price_rub=90299.0),
            ]
            return DnsPriceHistoryResponse(product_id="b58aaa7e00a9d582", points=points, price_range="от 57 999 ₽")

    monkeypatch.setitem(server.compare.SOURCES, "wildberries", Wb())
    monkeypatch.setitem(server.compare.SOURCES, "dns", Dns())
    return state


async def test_wb_history_and_own_snapshots_come_together(shops):
    snapshots.Snapshots().record([wb_offer(92104.0)], now=DAY2)

    result = await server.price_history(shop="wildberries", product=WB_URL)

    assert shops["calls"] == [("wb", 445317378)]
    assert [(p.week, p.price_rub) for p in result.shop_history] == [("2026-09-06", 67542.6), ("2026-09-13", 67089.0)]
    assert [s.price_rub for s in result.snapshots] == [92104.0]
    assert (result.summary.basis, result.summary.min_rub, result.summary.max_rub) == ("shop_history", 67089.0, 67542.6)
    assert result.summary.median_rub == 67315.8


async def test_dns_history_keeps_the_sites_range_and_labels(shops):
    result = await server.price_history(shop="dns", product=DNS_URL)

    assert shops["calls"] == [("dns", DNS_URL)]
    assert result.shop_history[-1].label == "21.09-27.09"
    assert result.summary.last_rub == 90299.0
    assert any("от 57 999 ₽" in note for note in result.notes)


async def test_a_shop_without_published_history_answers_from_snapshots(shops):
    offer = MarketOffer(
        source="citilink", product_id="2092141", title="Palit", price_rub=63990.0, url="https://x/2092141/"
    )
    snapshots.Snapshots().record([offer], now=DAY1)

    result = await server.price_history(shop="citilink", product="2092141")

    assert shops["calls"] == []
    assert result.shop_history == []
    assert result.summary.basis == "snapshots" and result.summary.last_rub == 63990.0
    assert any(note.startswith("no_shop_history") for note in result.notes)


async def test_a_stopped_shop_is_not_asked_and_snapshots_still_answer(shops):
    snapshots.Snapshots().record([wb_offer(92104.0)], now=DAY2)
    default_gate().trip("wildberries", "http 429")

    result = await server.price_history(shop="wildberries", product="445317378")

    assert shops["calls"] == []
    assert [s.price_rub for s in result.snapshots] == [92104.0]
    assert any(note.startswith("shop_history_unavailable") and "rate_limited" in note for note in result.notes)


async def test_a_refusal_stops_the_shop(shops):
    shops["wb_error"] = ToolError(json.dumps({"error": "transport_down", "status_code": 429, "message": "429"}))

    first = await server.price_history(shop="wildberries", product="445317378")
    await server.price_history(shop="wildberries", product="445317378")

    assert len(shops["calls"]) == 1
    assert any(note.startswith("shop_history_unavailable") for note in first.notes)


async def test_nothing_known_is_an_empty_answer_not_an_error(shops):
    result = await server.price_history(shop="citilink", product="1")

    assert (result.shop_history, result.snapshots, result.summary) == ([], [], None)


async def test_an_unknown_shop_or_a_bad_wb_id_is_a_bad_request(shops):
    with pytest.raises(ToolError, match="bad_request"):
        await server.price_history(shop="nowhere", product="1")
    with pytest.raises(ToolError, match="bad_request"):
        await server.price_history(shop="wildberries", product="https://evil.example/1")


async def test_search_and_compare_record_their_priced_rows(monkeypatch):
    async def fake(**kwargs):
        return CompareResponse(query=kwargs["query"], offers=[wb_offer(92104.0)])

    monkeypatch.setattr(server.compare, "compare_prices", fake)

    await server.search(query="rtx 5070")
    await server.compare_models(query="rtx 5070")

    assert [r.price_rub for r in snapshots.Snapshots().history("wildberries", product_id="445317378")] == [92104.0]


async def test_a_failed_snapshot_write_does_not_sink_the_search(monkeypatch):
    import sqlite3

    async def fake(**kwargs):
        return CompareResponse(query=kwargs["query"], offers=[wb_offer(92104.0)])

    def broken(self, offers, *, now):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.compare, "compare_prices", fake)
    monkeypatch.setattr(snapshots.Snapshots, "record", broken)

    assert [o.price_rub for o in (await server.search(query="rtx 5070")).offers] == [92104.0]
    assert (await server.compare_models(query="rtx 5070")).offers
