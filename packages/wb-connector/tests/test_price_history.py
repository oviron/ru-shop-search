"""История цен WB из basket: хост по живой карте CDN, недельные точки в рублях, отказ — стоп."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from wb_connector import server

FIXTURES = Path(__file__).parent / "fixtures"
ROUTES = (FIXTURES / "upstreams_live.json").read_text(encoding="utf-8")
HISTORY = (FIXTURES / "price_history_live.json").read_text(encoding="utf-8")
NGINX_404 = (
    "<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n<center><h1>404 Not Found</h1></center>\r\n"
)


@pytest.fixture
def wb(monkeypatch):
    state = {"urls": [], "answers": {}}

    async def fake_get(client, url):
        state["urls"].append(url)
        for part, answer in state["answers"].items():
            if part in url:
                return answer
        return 404, NGINX_404, None

    async def no_wait():
        return None

    monkeypatch.setattr(server, "_safe_get_text", fake_get)
    monkeypatch.setattr(server, "_polite_wait", no_wait)
    monkeypatch.setattr(server, "_routes", None)
    return state


async def test_the_host_comes_from_the_live_route_map_beyond_the_old_table(wb):
    wb["answers"]["upstreams"] = (200, ROUTES, None)

    assert await server._basket_host(445317378) == "basket-25.wbbasket.ru"
    assert await server._basket_host(2880500000) == "basket-65.wbbasket.ru"
    assert [u for u in wb["urls"] if "upstreams" in u] == ["https://cdn.wbbasket.ru/api/v3/upstreams"]


async def test_without_the_route_map_the_static_table_answers(wb):
    wb["answers"]["upstreams"] = (503, None, "http_status: 503")

    assert await server._basket_host(445317378) == "basket-25.wbbasket.ru"


async def test_price_history_is_weekly_roubles_from_kopecks(wb):
    wb["answers"]["upstreams"] = (200, ROUTES, None)
    wb["answers"]["price-history.json"] = (200, HISTORY, None)

    result = await server.wb_price_history(445317378)

    assert wb["urls"][-1] == "https://basket-25.wbbasket.ru/vol4453/part445317/445317378/info/price-history.json"
    assert len(result.points) == 8
    assert (result.points[0].week, result.points[0].price_rub) == ("2026-06-28", 31617.0)
    assert (result.points[-1].week, result.points[-1].price_rub) == ("2026-09-13", 67089.0)


async def test_points_without_a_rouble_price_are_skipped(wb):
    wb["answers"]["upstreams"] = (200, ROUTES, None)
    body = [{"dt": 1783209600, "price": {"RUB": 31700}}, {"dt": 1783814400, "price": {}}, {"dt": "x"}]
    wb["answers"]["price-history.json"] = (200, json.dumps(body), None)

    result = await server.wb_price_history(445317378)

    assert [(p.week, p.price_rub) for p in result.points] == [("2026-07-05", 317.0)]


async def test_no_history_file_is_not_found(wb):
    wb["answers"]["upstreams"] = (200, ROUTES, None)

    with pytest.raises(ToolError, match="not_found"):
        await server.wb_price_history(445317378)


async def test_a_non_list_body_is_parser_drift(wb):
    wb["answers"]["upstreams"] = (200, ROUTES, None)
    wb["answers"]["price-history.json"] = (200, "{}", None)

    with pytest.raises(ToolError, match="parser_drift"):
        await server.wb_price_history(445317378)


async def test_a_refusal_is_a_transport_error_with_its_status(wb):
    wb["answers"]["upstreams"] = (200, ROUTES, None)
    wb["answers"]["price-history.json"] = (429, None, "http_status: 429")

    with pytest.raises(ToolError) as excinfo:
        await server.wb_price_history(445317378)

    assert json.loads(str(excinfo.value))["status_code"] == 429


@pytest.mark.parametrize(("status", "stopped"), [(404, False), (200, True), (429, True)])
async def test_only_a_wall_or_a_refusal_stops_wb_not_an_html_404(monkeypatch, status, stopped):
    from mcp_core.stopgate import default_gate

    async def transport(client, url):
        return status, NGINX_404, None

    monkeypatch.setattr(server, "_budgeted_get_text", transport)

    await server._safe_get_text(None, "https://basket-43.wbbasket.ru/vol11192/part1119275/1119275475/info/x.json")

    assert ("wildberries" in default_gate().status()) is stopped
