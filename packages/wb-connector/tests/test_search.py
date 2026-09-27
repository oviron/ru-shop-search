"""Поиск WB v18 по токену из профиля: один перевыпуск на 498, стоп на первый отказ."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp.exceptions import ToolError
from mcp_core import stopgate
from mcp_core.transport.chrome_cdp import NavBlocked
from wb_connector import server
from wb_connector import session as wb_session

TOKEN = "cached-token-" + "x" * 40
FRESH = "fresh-token-" + "y" * 40
UA = "Mozilla/5.0 (Macintosh) Chrome/153.0.0.0 Safari/537.36"


def _product(nm: int, *, price: int | None = 1_018_120, qty: object = 5) -> dict:
    sizes = [{"price": {"basic": 1_429_950, "product": price}}] if price is not None else [{"price": None}]
    return {"id": nm, "name": f"RTX 5070 {nm}", "brand": "b", "supplier": "s", "totalQuantity": qty, "sizes": sizes}


def _body(*products: dict, total: int = 42) -> str:
    return json.dumps({"total": total, "products": list(products)})


def _session(token: str = TOKEN) -> wb_session.WbSession:
    return wb_session.WbSession(token=token, device_id="site_" + "b" * 32, user_agent=UA, minted_at=time.time())


class _Wire:
    """Stands in for curl_cffi; records every search request with its headers and cookies."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[tuple[str, dict, dict]] = []

    async def __call__(self, url, *, headers=None, cookies=None):
        self.requests.append((url, headers or {}, cookies or {}))
        return self.answers.pop(0)


class _Mint:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, timeout_s: float = 15.0):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    server._cache.clear()
    server._page1_fingerprints.clear()
    server._pacer.reset()
    monkeypatch.setattr(server, "_min_gap", 0.0)
    monkeypatch.setattr(server._pacer, "error_delay", 0.0)
    yield
    server._cache.clear()
    server._pacer.reset()


def _wire(monkeypatch, *answers) -> _Wire:
    wire = _Wire(*answers)
    monkeypatch.setattr(server, "_impersonated_get_text", wire)
    return wire


def _mint(monkeypatch, *outcomes) -> _Mint:
    mint = _Mint(*outcomes)
    monkeypatch.setattr(wb_session, "mint", mint)
    return mint


def _stopped() -> bool:
    return "wildberries" in stopgate.default_gate().status()


def _error(excinfo) -> dict:
    return json.loads(str(excinfo.value))


def test_search_reads_v18_with_the_cached_token(monkeypatch):
    wb_session.save(_session())
    mint = _mint(monkeypatch)
    wire = _wire(monkeypatch, (200, _body(_product(355329855)), None))

    data = asyncio.run(server.wb_search("rtx 5070")).model_dump()

    assert mint.calls == 0
    [(url, headers, cookies)] = wire.requests
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (parts.hostname, parts.path) == (
        "www.wildberries.ru",
        "/__internal/u-search/exactmatch/ru/common/v18/search",
    )
    assert query["query"] == ["rtx 5070"]
    assert query["dest"] == [server.WB_DEFAULT_DEST]
    assert query["spp"] == ["30"]
    assert "page" not in query
    assert cookies == {"x_wbaas_token": TOKEN}
    assert headers["deviceid"] == "site_" + "b" * 32
    assert headers["User-Agent"] == UA
    assert data["items"][0]["nm_id"] == 355329855
    assert data["items"][0]["price_rub"] == 10181.2
    assert data["items"][0]["price_original_rub"] == 14299.5
    assert data["items"][0]["in_stock"] is True
    assert data["total_ids"] == 42
    assert data["page_size"] == 100


def test_the_first_search_mints_a_token_in_the_profile_and_keeps_it(monkeypatch):
    mint = _mint(monkeypatch, _session(FRESH))
    wire = _wire(monkeypatch, (200, _body(_product(1)), None), (200, _body(_product(2)), None))

    asyncio.run(server.wb_search("a"))
    asyncio.run(server.wb_search("b"))

    assert mint.calls == 1
    assert [r[2]["x_wbaas_token"] for r in wire.requests] == [FRESH, FRESH]
    assert wb_session.load().token == FRESH


def test_an_expired_token_is_reissued_once(monkeypatch):
    wb_session.save(_session())
    mint = _mint(monkeypatch, _session(FRESH))
    wire = _wire(monkeypatch, (498, "", None), (200, _body(_product(7)), None))

    data = asyncio.run(server.wb_search("x")).model_dump()

    assert mint.calls == 1
    assert [r[2]["x_wbaas_token"] for r in wire.requests] == [TOKEN, FRESH]
    assert data["count"] == 1
    assert not _stopped()


def test_a_498_on_a_fresh_token_stops_wb_without_another_mint(monkeypatch):
    wb_session.save(_session())
    mint = _mint(monkeypatch, _session(FRESH))
    wire = _wire(monkeypatch, (498, "", None), (498, "", None))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_search("x"))

    assert "WB is stopped" in _error(excinfo)["message"]
    assert (mint.calls, len(wire.requests)) == (1, 2)
    assert _stopped()


@pytest.mark.parametrize(
    "answer",
    [(403, "", None), (429, "", None), (401, "", None), (200, "<html><center>Angie</center></html>", None)],
)
def test_a_refusal_stops_wb_on_the_first_request(monkeypatch, answer):
    wb_session.save(_session())
    mint = _mint(monkeypatch)
    wire = _wire(monkeypatch, answer)

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_search("x"))
    assert "WB is stopped" in _error(excinfo)["message"]

    with pytest.raises(ToolError) as again:
        asyncio.run(server.wb_search("y"))
    assert _error(again)["error"] == "rate_limited"
    assert (mint.calls, len(wire.requests)) == (0, 1)
    assert server._cache.get(wire.requests[0][0]) is None


def test_no_token_in_the_tab_stops_wb_before_any_search(monkeypatch):
    mint = _mint(monkeypatch, wb_session.TokenNotIssued("no cookie"))
    wire = _wire(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_search("x"))

    assert "WB is stopped" in _error(excinfo)["message"]
    assert (mint.calls, wire.requests) == (1, [])
    assert _stopped()


@pytest.mark.parametrize(("status", "stops"), [(403, True), (429, True), (502, False)])
def test_a_blocked_home_page_stops_wb_only_on_a_refusal(monkeypatch, status, stops):
    _mint(monkeypatch, NavBlocked(status, "https://www.wildberries.ru/"))
    wire = _wire(monkeypatch)

    with pytest.raises(ToolError):
        asyncio.run(server.wb_search("x"))

    assert wire.requests == []
    assert _stopped() is stops


def test_a_dead_browser_is_not_a_refusal(monkeypatch):
    _mint(monkeypatch, RuntimeError("Chrome/Chromium not found"))
    _wire(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_search("x"))

    payload = _error(excinfo)
    assert payload["error"] == "transport_down"
    assert "marketplace-mcp browser" in payload["message"]
    assert not _stopped()


def test_a_network_fault_is_an_error_not_an_empty_answer(monkeypatch):
    wb_session.save(_session())
    _wire(monkeypatch, (0, None, "timeout: no response within 45s"))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_search("x"))

    assert _error(excinfo)["error"] == "transport_down"
    assert not _stopped()


def test_a_changed_shape_is_parser_drift(monkeypatch):
    wb_session.save(_session())
    _wire(monkeypatch, (200, json.dumps({"unexpected": "shape"}), None))

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_search("x"))

    assert _error(excinfo)["error"] == "parser_drift"


def test_no_matches_is_no_results(monkeypatch):
    wb_session.save(_session())
    _wire(monkeypatch, (200, _body(total=0), None))

    result = asyncio.run(server.wb_search("zzz"))

    assert isinstance(result, server.WbNoResultsResponse)
    assert result.query == "zzz"


def test_unpriced_and_sold_out_items_are_flagged(monkeypatch):
    wb_session.save(_session())
    _wire(monkeypatch, (200, _body(_product(1, price=None, qty=0), _product(2, qty="0")), None))

    data = asyncio.run(server.wb_search("x")).model_dump()

    assert [i["in_stock"] for i in data["items"]] == [False, False]
    assert data["items"][0]["price_rub"] is None


def test_a_page_of_only_unpriced_items_warns(monkeypatch):
    wb_session.save(_session())
    _wire(monkeypatch, (200, _body(_product(1, price=None, qty=0)), None))

    data = asyncio.run(server.wb_search("x")).model_dump()

    assert any("no_prices" in w for w in data["meta"]["warnings"])


def test_a_repeated_search_is_served_from_the_cache(monkeypatch):
    wb_session.save(_session())
    wire = _wire(monkeypatch, (200, _body(_product(1)), None))

    asyncio.run(server.wb_search("x"))
    asyncio.run(server.wb_search("x"))

    assert len(wire.requests) == 1


def test_later_pages_are_requested_by_number(monkeypatch):
    wb_session.save(_session())
    wire = _wire(monkeypatch, (200, _body(_product(1), _product(2), _product(3)), None))

    asyncio.run(server.wb_search("x", page=3))

    assert parse_qs(urlsplit(wire.requests[0][0]).query)["page"] == ["3"]


def _selfcheck_other_probes(monkeypatch):
    async def fake_safe_get_text(client, url):
        return 500, "", "not under test"

    monkeypatch.setattr(server, "_safe_get_text", fake_safe_get_text)


def test_the_canary_never_mints_a_token(monkeypatch):
    _selfcheck_other_probes(monkeypatch)
    mint = _mint(monkeypatch)
    wire = _wire(monkeypatch)

    check = asyncio.run(server.wb_selfcheck()).model_dump()["checks"]["search"]

    assert (check["state"], check["reason"]) == ("inconclusive", "no_session")
    assert (mint.calls, wire.requests) == (0, [])


@pytest.mark.parametrize(
    ("answer", "state", "reason", "stops"),
    [
        ((200, _body(_product(1)), None), "healthy", None, False),
        ((200, _body(_product(1, price=None)), None), "drift", None, False),
        ((200, "{}", None), "drift", "schema_drift", False),
        ((498, "", None), "inconclusive", "token_expired", False),
        ((403, "", None), "inconclusive", "refused", True),
    ],
)
def test_the_canary_reads_search_with_the_cached_token(monkeypatch, answer, state, reason, stops):
    wb_session.save(_session())
    _selfcheck_other_probes(monkeypatch)
    mint = _mint(monkeypatch)
    _wire(monkeypatch, answer)

    check = asyncio.run(server.wb_selfcheck()).model_dump()["checks"]["search"]

    assert check["state"] == state
    if reason:
        assert check["reason"] == reason
    assert mint.calls == 0
    assert _stopped() is stops


def test_a_refusal_on_one_review_host_is_not_followed_by_the_mirror(monkeypatch):
    hosts = []

    async def fake_budgeted(client, url, **kwargs):
        hosts.append(urlsplit(url).hostname)
        return 403, "", None

    monkeypatch.setattr(server, "get_text_budgeted", fake_budgeted)

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.wb_reviews(server._SELFCHECK_IMT))

    assert hosts == ["feedbacks2.wb.ru"]
    assert _error(excinfo)["error"] == "rate_limited"
    assert _stopped()
