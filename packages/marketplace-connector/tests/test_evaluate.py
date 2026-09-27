"""avito_evaluate: лот из карточки или из аргументов, ориентиры из выдачи Авито и розницы, без повторов после стопа."""

from __future__ import annotations

import json

import pytest
from avito_connector.models_output import AvitoSearchItemOut, AvitoSearchResponse
from compare_connector.models_output import CompareResponse, MarketOffer
from fastmcp.exceptions import ToolError
from marketplace_connector import server

LOT_URL = "https://www.avito.ru/moskva/telefony/iphone_13_128_gb_1234567890"
STOPPED = ToolError(json.dumps({"error": "rate_limited", "source_stopped": True, "message": "avito stopped"}))
CARD = {
    "item_id": "1234567890",
    "title": "iPhone 13 128 ГБ",
    "price_rub": 40000.0,
    "description": "Телефон в хорошем состоянии, всё работает. " * 6,
    "params": ["Состояние: Б/у", "Память: 128 ГБ", "Цвет: синий"],
    "photos": 4,
    "delivery": True,
    "seller": {"is_company": False, "rating_score": 5.0, "rating_count": 40, "since_year": 2019},
    "url": LOT_URL,
}


def peer(item_id: int, title: str, price: float | None) -> AvitoSearchItemOut:
    return AvitoSearchItemOut(item_id=item_id, title=title, price_rub=price)


PEERS = [
    peer(1234567890, "iPhone 13 128 ГБ", 40000.0),
    *(peer(1000 + n, f"iPhone 13 128GB вариант {n}", 50000.0) for n in range(5)),
    peer(2000, "iPhone 13 на запчасти", 5000.0),
]


class FakeAvito:
    def __init__(self, items=None, error: ToolError | None = None) -> None:
        self.items, self.error, self.calls = items or [], error, []

    async def avito_search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return AvitoSearchResponse(query=kwargs["query"], items=self.items, count=len(self.items))


@pytest.fixture
def wired(monkeypatch):
    state = {"cards": [], "compare": []}

    async def fetch_card(name, product):
        state["cards"].append((name, product))
        if isinstance(state.get("card"), Exception):
            raise state["card"]
        return state.get("card", CARD), ""

    async def compare_prices(**kwargs):
        state["compare"].append(kwargs)
        offers = [
            MarketOffer(source="dns", title="Смартфон Apple iPhone 13 128 ГБ", price_rub=60000.0),
            MarketOffer(source="citilink", title="Apple iPhone 13 128GB", price_rub=62000.0),
            MarketOffer(source="mvideo", title="iPhone 13 128 ГБ синий", price_rub=64000.0),
            MarketOffer(source="ozon", title="Чехол для iPhone 13 128", price_rub=500.0),
            MarketOffer(source="wildberries", title="iPhone 13 128", price_rub=None),
        ]
        return CompareResponse(query=kwargs["query"], offers=offers)

    avito = FakeAvito(PEERS)
    monkeypatch.setattr(server.compare, "fetch_card", fetch_card)
    monkeypatch.setattr(server.compare, "compare_prices", compare_prices)
    monkeypatch.setitem(server.compare.SOURCES, "avito", avito)
    state["avito"] = avito
    return state


async def test_a_lot_by_url_is_scored_against_independent_avito_and_retail_medians(wired):
    result = await server.avito_evaluate(query="iphone 13 128", listing_url=LOT_URL)

    assert wired["cards"] == [("avito", LOT_URL)]
    assert (result.title, result.price_rub, result.url) == ("iPhone 13 128 ГБ", 40000.0, LOT_URL)
    assert (result.avito_sample, result.avito_median_rub) == (5, 50000.0)
    assert (result.retail_sample, result.retail_median_rub) == (3, 62000.0)
    assert result.verdict == "good_deal"
    assert result.notes == []


async def test_retail_is_asked_only_of_new_goods_shops(wired):
    await server.avito_evaluate(query="iphone 13 128", listing_url=LOT_URL)

    asked = set(wired["compare"][0]["sources"])
    assert asked and not asked & {"avito", "pepper", "aliexpress"}


async def test_retail_can_be_skipped(wired):
    result = await server.avito_evaluate(query="iphone 13 128", listing_url=LOT_URL, check_retail=False)

    assert wired["compare"] == []
    assert result.retail_sample == 0


async def test_explicit_fields_override_the_card(wired):
    result = await server.avito_evaluate(query="iphone 13 128", listing_url=LOT_URL, price_rub=45000.0)

    assert result.price_rub == 45000.0


async def test_a_stopped_avito_is_not_retried_and_the_callers_fields_and_peers_are_used(wired):
    wired["card"] = STOPPED
    wired["avito"].error = STOPPED

    result = await server.avito_evaluate(
        query="iphone 13 128",
        listing_url=LOT_URL,
        title="iPhone 13 128 ГБ",
        price_rub=40000.0,
        peer_prices=[50000.0, 51000.0, 49000.0, 50000.0, 52000.0],
        check_retail=False,
    )

    assert wired["avito"].calls == []
    assert (result.avito_sample, result.avito_median_rub) == (5, 50000.0)
    assert any(note.startswith("listing_not_fetched") and "rate_limited" in note for note in result.notes)
    assert "peer_prices_from_caller" in result.notes
    assert result.verdict != "insufficient_sample"


async def test_a_stopped_card_means_no_avito_search_either(wired):
    wired["card"] = STOPPED

    result = await server.avito_evaluate(
        query="iphone 13 128", listing_url=LOT_URL, title="iPhone 13", price_rub=40000.0, check_retail=False
    )

    assert wired["avito"].calls == []
    assert result.verdict == "insufficient_sample"
    assert any(note.startswith("avito_peers_unavailable") for note in result.notes)


async def test_a_stopped_avito_without_peers_gives_no_verdict(wired):
    wired["avito"].error = STOPPED

    result = await server.avito_evaluate(query="iphone 13 128", listing_url=LOT_URL, check_retail=False)

    assert result.verdict == "insufficient_sample"
    assert any(note.startswith("avito_peers_unavailable") for note in result.notes)


async def test_without_a_card_or_a_title_there_is_nothing_to_evaluate(wired):
    wired["card"] = STOPPED

    with pytest.raises(ToolError, match="bad_request"):
        await server.avito_evaluate(query="iphone 13 128", listing_url=LOT_URL)


async def test_a_listing_url_must_be_avito(wired):
    with pytest.raises(ToolError, match="bad_request"):
        await server.avito_evaluate(query="iphone 13 128", listing_url="https://evil.example/x_1234567890")
    assert wired["cards"] == []
