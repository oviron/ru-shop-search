"""Сервер плагина отдаёт только свои инструменты, а не 32 инструмента коннекторов."""

from __future__ import annotations

import pytest
from compare_connector.models_output import CompareResponse
from fastmcp.exceptions import ToolError
from marketplace_connector import server
from mcp_core.stopgate import default_gate


async def test_only_plugin_tools_are_exposed():
    names = {tool.name for tool in await server.mcp.list_tools()}

    assert names == {"search", "card", "shops"}


async def test_search_delegates_to_the_fan_out(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return CompareResponse(query=kwargs["query"])

    monkeypatch.setattr(server.compare, "compare_prices", fake)

    result = await server.search(query="rtx 5070", shops=["dns"], per_shop=3)

    assert result.query == "rtx 5070"
    assert seen["sources"] == ["dns"] and seen["per_source_limit"] == 3


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
