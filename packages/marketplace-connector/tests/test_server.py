"""Сервер плагина отдаёт только свои инструменты, а не 32 инструмента коннекторов."""

from __future__ import annotations

import pytest
from compare_connector.models_output import CompareResponse, MarketOffer
from fastmcp.exceptions import ToolError
from marketplace_connector import server
from mcp_core.stopgate import default_gate


async def test_only_plugin_tools_are_exposed():
    names = {tool.name for tool in await server.mcp.list_tools()}

    assert names == {"search", "compare", "card", "price_history", "shops", "avito_evaluate"}


async def test_search_delegates_to_the_fan_out(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return CompareResponse(query=kwargs["query"])

    monkeypatch.setattr(server.compare, "compare_prices", fake)

    result = await server.search(query="rtx 5070", shops=["dns"], per_shop=3)

    assert result.query == "rtx 5070"
    assert seen["sources"] == ["dns"] and seen["per_source_limit"] == 3


async def test_compare_groups_the_same_model_from_different_shops(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        offers = [
            MarketOffer(source="regard", title="Palit RTX 5070 Infinity 3 (NE75070019K9-GB2050S)", price_rub=61500.0),
            MarketOffer(
                source="dns", title="Palit GeForce RTX 5070 Infinity 3 [NE75070019K9-GB2050S]", price_rub=62999.0
            ),
            MarketOffer(
                source="mvideo", title="Palit RTX 5070 INFINITY 3 OC [ne75070s19k9-gb2050s]", price_rub=64999.0
            ),
        ]
        return CompareResponse(query=kwargs["query"], offers=offers, total_offers=3)

    monkeypatch.setattr(server.compare, "compare_prices", fake)

    result = await server.compare_models(query="rtx 5070", shops=["dns", "regard", "mvideo"], per_shop=3)

    assert seen["sources"] == ["dns", "regard", "mvideo"] and seen["per_source_limit"] == 3
    assert result.total_offers == 3
    assert [(m.confidence, m.sources) for m in result.models] == [("likely", ["dns", "regard"])]


async def test_compare_says_when_no_model_matched(monkeypatch):
    async def fake(**kwargs):
        return CompareResponse(query=kwargs["query"], offers=[MarketOffer(source="dns", title="x", price_rub=1.0)])

    monkeypatch.setattr(server.compare, "compare_prices", fake)

    result = await server.compare_models(query="rtx 5070")

    assert result.models == []
    assert any(w.startswith("no_model_match") for w in result.warnings)


async def test_card_rejects_an_unknown_shop():
    with pytest.raises(ToolError, match="bad_request"):
        await server.card(shop="nowhere", product="1")


async def test_shops_lists_all_fifteen_with_stop_state(monkeypatch):
    default_gate().trip("ozon", "captcha")

    result = await server.shops()

    by_key = {shop.key: shop for shop in result.shops}
    assert len(by_key) == 15
    assert by_key["wildberries"].installed is True
    assert by_key["pepper"].installed is True
    assert by_key["ozon"].stopped["reason"] == "captcha"
    assert by_key["wildberries"].stopped is None

    monkeypatch.setattr(server.compare, "SOURCES", {k: v for k, v in server.compare.SOURCES.items() if k != "pepper"})
    by_key = {shop.key: shop for shop in (await server.shops()).shops}
    assert by_key["pepper"].installed is False


@pytest.mark.parametrize("tool", ["search", "compare_models"])
async def test_avito_is_searched_only_when_named(monkeypatch, tool):
    seen = []

    async def fake(**kwargs):
        seen.append(kwargs["sources"])
        return CompareResponse(query=kwargs["query"])

    monkeypatch.setattr(server.compare, "compare_prices", fake)
    monkeypatch.delenv("MARKETPLACE_SOURCES", raising=False)

    await getattr(server, tool)(query="rtx 5070")
    await getattr(server, tool)(query="rtx 5070", shops=["avito"])
    monkeypatch.setenv("MARKETPLACE_SOURCES", "wildberries,avito")
    await getattr(server, tool)(query="rtx 5070")

    assert "avito" not in seen[0] and {"wildberries", "pepper", "dns"} <= set(seen[0])
    assert seen[1] == ["avito"]
    assert seen[2] == ["wildberries"]
