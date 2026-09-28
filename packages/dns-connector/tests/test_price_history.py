"""История цен DNS: одна вкладка товара, GUID со страницы, один POST истории с паузой перед ним."""

from __future__ import annotations

import contextlib
import datetime
import json
from pathlib import Path

import pytest
from dns_connector import server
from fastmcp.exceptions import ToolError
from mcp_core.pacing import Pacer
from mcp_core.transport.chrome_cdp import NavBlocked

HISTORY = (Path(__file__).parent / "fixtures" / "price_history_palit_live.json").read_text(encoding="utf-8")
URL = "https://www.dns-shop.ru/product/b58aaa7e00a9d582/videokarta-palit/"
GUID = "b58aaa7e-00a9-11f0-92fe-0050569d5825"
PAGE = {"guid": GUID, "title": "Купить Видеокарта Palit GeForce RTX 5070 Infinity 3"}


class _Tab:
    def __init__(self, page, answer):
        self.page, self.answer, self.fetched = page, answer, []

    async def evaluate(self, js, arg=None):
        if arg is None:
            return json.dumps(self.page)
        self.fetched.append(arg)
        return json.dumps(self.answer)


@pytest.fixture
def tab(monkeypatch):
    state = {"opened": [], "waits": 0, "page": PAGE, "answer": {"status": 200, "text": HISTORY}, "blocked": None}

    @contextlib.asynccontextmanager
    async def fake_open_page(url, wait_ms, challenge_grace_s=0.0):
        state["opened"].append(url)
        if state["blocked"] is not None:
            raise NavBlocked(state["blocked"], url)
        state["tab"] = _Tab(state["page"], state["answer"])
        yield state["tab"]

    async def counted_wait():
        state["waits"] += 1
        await server._pacer.wait(min_gap=0.0)

    monkeypatch.setattr(server, "open_page", fake_open_page)
    monkeypatch.setattr(server, "_polite_wait", counted_wait)
    monkeypatch.setattr(server, "_pacer", Pacer(0.0, source="dns"))
    monkeypatch.setattr(server, "_PRICE_WAIT_MS", 0)
    monkeypatch.setattr(server, "_today", lambda: datetime.date(2026, 9, 28))
    return state


async def test_weekly_points_come_from_the_product_page_with_a_pause_before_the_request(tab):
    result = await server.dns_price_history(URL)

    assert tab["opened"] == ["https://www.dns-shop.ru/product/b58aaa7e00a9d582/"]
    assert tab["tab"].fetched == [GUID]
    assert tab["waits"] == 2
    assert len(result.points) == 24
    first, last = result.points[0], result.points[-1]
    assert (first.week, first.label, first.price_rub) == ("2026-04-13", "13.04-19.04", 57999.0)
    assert (last.week, last.label, last.price_rub) == ("2026-09-21", "21.09-27.09", 90299.0)
    assert result.price_range == "от 57 999 ₽ до 90 299 ₽"
    assert result.product_id == "b58aaa7e00a9d582"


def test_week_labels_get_their_year_across_new_year():
    labels = ["22.12-28.12", "29.12-04.01", "05.01-11.01"]

    assert server._iso_weeks(labels, datetime.date(2026, 1, 7)) == ["2025-12-22", "2025-12-29", "2026-01-05"]


def test_an_unreadable_label_keeps_no_date():
    assert server._iso_weeks(["вчера", "05.01-11.01"], datetime.date(2026, 1, 7)) == ["", "2026-01-05"]


async def test_a_url_without_a_product_id_opens_nothing(tab):
    with pytest.raises(ToolError, match="bad_request"):
        await server.dns_price_history("https://www.dns-shop.ru/catalog/")
    assert tab["opened"] == []


async def test_an_access_restricted_page_stops_dns_before_any_request(tab):
    tab["page"] = {"guid": None, "title": "Доступ ограничен"}

    with pytest.raises(ToolError):
        await server.dns_price_history(URL)
    with pytest.raises(ToolError) as excinfo:
        await server.dns_price_history(URL)

    assert json.loads(str(excinfo.value))["source_stopped"] is True
    assert len(tab["opened"]) == 1


async def test_a_page_without_the_guid_is_drift_not_a_stop(tab):
    tab["page"] = {"guid": None, "title": "Видеокарта Palit"}

    with pytest.raises(ToolError, match="parser_drift"):
        await server.dns_price_history(URL)
    assert tab["tab"].fetched == []
    await server._pacer.wait(min_gap=0.0)


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_a_refused_history_request_stops_dns(tab, status):
    tab["answer"] = {"status": status, "text": ""}

    with pytest.raises(ToolError) as excinfo:
        await server.dns_price_history(URL)

    assert json.loads(str(excinfo.value))["status_code"] == status
    with pytest.raises(ToolError, match="source_stopped"):
        await server.dns_price_history(URL)


async def test_a_server_error_is_transport_down_without_a_stop(tab):
    tab["answer"] = {"status": 500, "text": ""}

    with pytest.raises(ToolError, match="transport_down"):
        await server.dns_price_history(URL)
    await server._pacer.wait(min_gap=0.0)


@pytest.mark.parametrize("text", ["<html>", json.dumps({"result": False, "data": None}), json.dumps({"result": True})])
async def test_an_unexpected_body_is_drift(tab, text):
    tab["answer"] = {"status": 200, "text": text}

    with pytest.raises(ToolError, match="parser_drift"):
        await server.dns_price_history(URL)


async def test_a_refused_tab_stops_dns(tab):
    tab["blocked"] = 403

    with pytest.raises(ToolError):
        await server.dns_price_history(URL)
    with pytest.raises(ToolError, match="source_stopped"):
        await server.dns_price_history(URL)
    assert len(tab["opened"]) == 1


def test_the_page_script_reads_the_guid_from_the_live_card():
    from mcp_core.domtest import JsdomUnavailable, run_extractor

    fixture = Path(__file__).parent / "fixtures" / "card_palit_live.html"
    try:
        payload = run_extractor(
            server._HISTORY_PAGE_JS, fixture, page_url="https://www.dns-shop.ru/product/b58aaa7e00a9d582/"
        )
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))

    assert payload["guid"] == GUID
    assert payload["h1"] == "Видеокарта Palit GeForce RTX 5070 Infinity 3 [NE75070019K9-GB2050S]"
