"""Offline tests for the Yandex Market connector's tool layer.

Page fetches are monkeypatched to serve the trimmed real fixtures, so the suite
needs no network and no Russian-friendly IP.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from mcp_core import stopgate
from mcp_core.transport.impersonated import Reply
from yandex_connector import server

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def clear_cache():
    server._cache.clear()
    yield
    server._cache.clear()


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    """Remove the politeness gap so tests do not actually wait 1.5s per call."""
    monkeypatch.setattr(server._limiter, "min_gap", 0.0)


def stub_html(monkeypatch, mapping: dict[str, str]):
    """Serve canned HTML from ``_fetch_html``, matched by URL substring."""

    async def fake_fetch(url: str, label: str, ctx=None) -> str:
        for needle, html in mapping.items():
            if needle in url:
                return html
        raise AssertionError(f"unexpected URL requested: {url}")

    monkeypatch.setattr(server, "_fetch_html", fake_fetch)


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


# ------------------------------------------------------------------ basics ----


def test_server_version_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == server.SERVER_VERSION


async def test_registered_tools_are_stable():
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert names == {"yandex_search", "yandex_card"}


# ------------------------------------------------------------------ search ----


async def test_search_returns_products_with_both_prices(monkeypatch):
    stub_html(monkeypatch, {"/search": load("search_washer.html")})

    result = await server.yandex_search(query="стиральная машина")

    assert result.returned == 3
    assert result.total_available == 1434
    assert result.meta.extraction == "ssr"
    first = result.items[0]
    assert first.brand == "Tuvio"
    # Re-read from this same Jul 2026 capture after the 2026-09-11 price
    # mapping fix: 16724 is the displayed cart price; the old pin (22600) was
    # the struck-through base price and now rides in price_old_rub.
    assert first.price_rub == 16724.0
    assert first.price_with_plus == 16222.0
    assert first.price_old_rub == 22600.0


async def test_search_quotes_the_cart_price_not_the_strike_through(monkeypatch):
    """End-to-end regression for the 2026-09-11 price-semantics fix.

    The SERP state carries the struck-through initialPrice in offer.price.value
    and baobabPayload.price (3698 for the Tuvio TKP2117S); the price any buyer
    pays is the snippet's cart price (2293), verified the same day against the
    product card. yandex_search must surface the latter as price_rub and keep
    the former in price_old_rub.
    """
    stub_html(monkeypatch, {"/search": load("search_kettle.html")})

    result = await server.yandex_search(query="чайник")

    tuvio = next(item for item in result.items if item.product_id == "5929806453")
    assert tuvio.price_rub == 2293.0
    assert tuvio.price_old_rub == 3698.0
    assert tuvio.price_with_plus == 2247.0
    assert tuvio.sku_id == "5929806452"


async def test_search_honours_the_limit(monkeypatch):
    stub_html(monkeypatch, {"/search": load("search_washer.html")})

    result = await server.yandex_search(query="стиральная машина", limit=2)

    assert result.returned == 2


async def test_search_builds_a_page_parameter_only_beyond_page_one(monkeypatch):
    seen: list[str] = []

    async def capture(url: str, label: str, ctx=None) -> str:
        seen.append(url)
        return load("search_washer.html")

    monkeypatch.setattr(server, "_fetch_html", capture)

    await server.yandex_search(query="тест", page=1)
    assert "page=" not in seen[-1]

    await server.yandex_search(query="тест", page=3)
    assert "page=3" in seen[-1]


async def test_search_percent_encodes_cyrillic_queries(monkeypatch):
    seen: list[str] = []

    async def capture(url: str, label: str, ctx=None) -> str:
        seen.append(url)
        return load("search_washer.html")

    monkeypatch.setattr(server, "_fetch_html", capture)

    await server.yandex_search(query="стиральная машина")

    assert "стиральная" not in seen[-1]
    assert "%D1%81" in seen[-1]


async def test_search_warns_on_empty_results(monkeypatch):
    stub_html(monkeypatch, {"/search": load("search_empty.html")})

    result = await server.yandex_search(query="йцукенгшщз")

    assert result.returned == 0
    assert result.meta.healthy is False
    assert any("no_results" in w for w in result.meta.warnings)


async def test_search_raises_drift_when_the_page_is_unrecognisable(monkeypatch):
    """Neither products nor an empty-result banner means the SSR shape moved."""
    stub_html(monkeypatch, {"/search": "<html><body>redesigned</body></html>"})

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_search(query="телефон")

    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_search_stops_the_market_on_a_captcha(monkeypatch):
    """Капча — стоп Маркета на 30 минут, без повторов и без попыток её решить."""
    stub_html(monkeypatch, {"/search": '<html><body><div id="SmartCaptcha"></div></body></html>'})

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_search(query="телефон")

    assert error_payload(excinfo.value)["error"] == "challenge_required"
    assert server.SOURCE in stopgate.default_gate().status()


async def test_search_flags_a_degraded_ldjson_fallback(monkeypatch):
    """Callers must know when fields are missing and the price is Plus-only."""
    html = """<html><body><script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[{"item":{"name":"X",
     "url":"https://market.yandex.ru/product/1","offers":{"price":100}}}]}
    </script></body></html>"""
    stub_html(monkeypatch, {"/search": html})

    result = await server.yandex_search(query="тест")

    assert result.meta.extraction == "ld+json"
    assert any("degraded" in w for w in result.meta.warnings)


@pytest.mark.parametrize("bad_query", ["", " ", "x"])
async def test_search_rejects_too_short_queries(monkeypatch, bad_query):
    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("a rejected query must never reach the network")

    monkeypatch.setattr(server, "_fetch_html", fail_fetch)

    with pytest.raises((ToolError, Exception)):
        await server.yandex_search(query=bad_query)


# -------------------------------------------------------------------- card ----


async def test_card_returns_prices_rating_breakdown_and_reviews(monkeypatch):
    stub_html(monkeypatch, {"/card/stiralnaya-mashina/763970960": load("card_washer.html")})

    card = await server.yandex_card(product_id="/card/stiralnaya-mashina/763970960")

    assert card.title.startswith("Стиральная машина")
    assert card.price_rub == 19377.0
    assert card.price_with_plus == 18796.0
    assert card.rating == 4.8
    assert card.rating_stars == {1: 10, 2: 3, 3: 10, 4: 19, 5: 502}
    assert card.reviews
    assert card.reviews[0].author


async def test_card_can_skip_reviews(monkeypatch):
    stub_html(monkeypatch, {"/card/": load("card_washer.html")})

    card = await server.yandex_card(product_id="/card/stiralnaya-mashina/763970960", include_reviews=False)

    assert card.reviews == []
    assert card.review_count is not None  # the count still comes through


async def test_card_warns_when_a_resale_offer_has_no_rating(monkeypatch):
    """Real and common — the warning explains it rather than implying 0 stars."""
    stub_html(monkeypatch, {"/card/": load("card_no_rating.html")})

    card = await server.yandex_card(product_id="/card/tovar/1912483624")

    assert card.rating is None
    assert card.meta.healthy is False
    assert any("no_rating" in w for w in card.meta.warnings)


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "123?foo=bar",
        "abc",
        "12 34",
        "1/2",
        "-5",
        "https://evil.test/card/a/1",
        "https://market.yandex.ru.evil.test/card/a/1",
        "https://market.yandex.ru/card/../1",
        "http://market.yandex.ru/card/a/1",
        "https://market.yandex.ru/product/1",
        "/card/a/1/../2",
        "4356725479",
    ],
)
async def test_card_rejects_anything_but_an_id_or_a_card_link(monkeypatch, bad_id):
    """Ссылка уходит в запрос, поэтому принимается только /card/<slug>/<id> на market.yandex.ru."""

    async def fail_fetch(*_args, **_kwargs):
        raise AssertionError("a rejected id must never reach the network")

    monkeypatch.setattr(server, "_fetch_html", fail_fetch)

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_card(product_id=bad_id)

    assert error_payload(excinfo.value)["error"] == "bad_request"


@pytest.mark.parametrize(
    "ref",
    [
        "https://market.yandex.ru/card/stiralnaya-mashina/763970960?sku=1&utm_source=x",
        "/card/stiralnaya-mashina/763970960",
    ],
)
async def test_card_reads_the_card_link_from_search(monkeypatch, ref):
    requested: list[str] = []

    async def fetch(url: str, label: str, ctx=None) -> str:
        requested.append(url)
        return load("card_washer.html")

    monkeypatch.setattr(server, "_fetch_html", fetch)

    card = await server.yandex_card(product_id=ref)

    assert requested == ["https://market.yandex.ru/card/stiralnaya-mashina/763970960"]
    assert card.url == requested[0]
    assert card.title


async def test_card_raises_drift_when_no_title_is_found(monkeypatch):
    stub_html(monkeypatch, {"/card/": '<html><body>"mainPrice" but no product</body></html>'})

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_card(product_id="/card/tovar/1")

    assert error_payload(excinfo.value)["error"] == "parser_drift"


async def test_card_empty_shell_is_not_reported_as_parser_drift(monkeypatch):
    """The live 2026-09-13 hollow frame: a retryable upstream condition.

    Reporting it as parser_drift would page a maintainer about parsers that
    are fine (and fail `marketplace-mcp doctor` with exit 1), while the real
    state of the world is Yandex serving the product frame with no product
    inside — degraded serving or a delisting, neither of which is a code bug.
    """
    stub_html(monkeypatch, {"/card/": load("card_empty_shell.html")})

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_card(product_id="/card/smartfon/4315891968")

    payload = error_payload(excinfo.value)
    assert payload["error"] == "transport_down"
    assert payload["retryable"] is True
    assert "empty_product_shell" in payload["message"]
    assert "4315891968" in payload["message"]


async def test_card_renamed_field_families_still_report_drift(monkeypatch):
    """Drift stays reserved for pages that reshaped known field families.

    Same missing title as the hollow frame, but the collections are populated:
    the state is there and no longer understood — a maintainer must look.
    """
    html = (
        '<noframes data-apiary="patch">{"collections":{"pageParams":{"current":'
        '{"id":"current","pageId":"market:product","params":{"productId":"42"}}}}}</noframes>'
        '<noframes data-apiary="patch">{"collections":{"titleV2":{"t1":{"titleRenamed":"Товар"}},'
        '"price":{"p1":{"mainPriceRenamed":{}}}}}</noframes>'
    )
    stub_html(monkeypatch, {"/card/": html})

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_card(product_id="/card/tovar/42")

    assert error_payload(excinfo.value)["error"] == "parser_drift"


# --------------------------------------------------------------- selfcheck ----


async def test_selfcheck_chains_search_into_card(monkeypatch):
    """The card probe uses an id from search, so no fixture SKU can go stale."""
    stub_html(
        monkeypatch,
        {"/search": load("search_telefon_zone.html"), "/card/": load("card_washer.html")},
    )

    result = await server.yandex_selfcheck()

    assert result.status == "success"
    assert result.checks["search"].state == "healthy"
    assert result.checks["card"].state == "healthy"
    assert result.tool_count == 2


async def test_selfcheck_is_inconclusive_when_transport_fails(monkeypatch):
    """A block says nothing about the parsers, so it must not read as drift."""
    from mcp_core.errors import TransportDownError, raise_tool_error

    async def always_down(url: str, label: str, ctx=None) -> str:
        raise_tool_error(TransportDownError("upstream unreachable", provider="yandex"))
        raise AssertionError("unreachable")

    monkeypatch.setattr(server, "_fetch_html", always_down)

    result = await server.yandex_selfcheck()

    assert result.status == "inconclusive"
    assert result.checks["search"].state == "inconclusive"


async def test_selfcheck_reports_drift_when_pages_stop_parsing(monkeypatch):
    stub_html(monkeypatch, {"/search": "<html><body>redesigned</body></html>"})

    result = await server.yandex_selfcheck()

    assert result.status == "drift_detected"
    assert result.checks["search"].state == "drift"


async def test_selfcheck_skips_card_when_search_yields_no_id(monkeypatch):
    stub_html(monkeypatch, {"/search": load("search_empty.html")})

    result = await server.yandex_selfcheck()

    assert result.checks["card"].state == "inconclusive"
    assert "skipped" in result.checks["card"].detail


async def test_selfcheck_card_shell_is_inconclusive_not_drift(monkeypatch):
    """Doctor must not page anyone (exit 1) over a hollow serving frame.

    The 2026-09-13 canary reported drift_detected on exactly this page; the
    product was alive in search the whole time and the parsers were fine.
    """
    stub_html(
        monkeypatch,
        {"/search": load("search_telefon_zone.html"), "/card/": load("card_empty_shell.html")},
    )

    result = await server.yandex_selfcheck()

    assert result.checks["search"].state == "healthy"
    assert result.checks["card"].state == "inconclusive"
    assert "empty_product_shell" in result.checks["card"].detail
    assert any("hollow" in note for note in result.checks["card"].notes)
    assert result.status == "inconclusive"


async def test_selfcheck_ldjson_only_search_is_weak_not_healthy(monkeypatch):
    """A search read only through the schema.org fallback proves nothing about
    the SSR parsers, and its card probe id inherits that weakness — report it
    as inconclusive(ok_ldjson_only), never healthy (2026-09-13 canary gap)."""
    html = """<html><body><script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[{"item":{"name":"X",
     "url":"https://market.yandex.ru/card/stiralnaya-mashina/763970960","offers":{"price":100}}}]}
    </script></body></html>"""
    stub_html(monkeypatch, {"/search": html, "/card/": load("card_washer.html")})

    result = await server.yandex_selfcheck()

    assert result.checks["search"].state == "inconclusive"
    assert "ok_ldjson_only" in result.checks["search"].detail
    assert result.checks["card"].state == "healthy"
    assert any("degraded" in note for note in result.checks["card"].notes)
    assert result.status == "inconclusive"


def _drifted_values_search_html() -> str:
    """The washer capture with its title/price nodes renamed in the SSR state.

    This is what a live re-layout looks like: items keep their product ids
    (collection keys) but lose every title and price (state values). The real
    parser returns them as title '' / price None — verified, not assumed.
    Since the 2026-09-11 price mapping fix the everyday price also lives in
    ``baobabPayload.additionalPrices`` (the withDiscount entry), so the
    simulated rename has to cover that node too — otherwise the fallback
    chain would mask the drift this test exists to detect.
    """
    html = load("search_washer.html")
    return (
        html.replace('"titles"', '"titlesZ"')
        .replace('"title"', '"titleZ"')
        .replace('"actualPrice"', '"actualPriceZ"')
        .replace('"initialPrice"', '"initialPriceZ"')
        .replace('"oldPrice"', '"oldPriceZ"')
        .replace('"additionalPrices"', '"additionalPricesZ"')
        .replace('"price"', '"priceZ"')
    )


async def test_search_flags_drift_when_items_lose_their_values(monkeypatch):
    """Items with ids but no title/price anywhere is a moved SSR state, not a
    result set — serve drift instead of silently degraded items."""
    stub_html(monkeypatch, {"/search": _drifted_values_search_html()})

    with pytest.raises(ToolError) as excinfo:
        await server.yandex_search(query="стиральная машина")

    assert "without any title or price" in str(excinfo.value)


async def test_selfcheck_reports_value_drift(monkeypatch):
    stub_html(monkeypatch, {"/search": _drifted_values_search_html()})

    result = await server.yandex_selfcheck()

    assert result.status == "drift_detected"
    assert result.checks["search"].state == "drift"


# --------------------------------------------------------------- transport ----


class _Wire:
    """Подменяет curl_cffi: отдаёт заготовленные ответы и пишет каждый запрос."""

    def __init__(self, *replies: Reply):
        self.replies = list(replies)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    async def __call__(self, url, **kwargs):
        self.urls.append(url)
        self.headers.append(kwargs.get("headers") or {})
        return self.replies.pop(0)


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setattr(server._settings, "net_backoff_s", 0.0)
    monkeypatch.setattr(server._limiter, "error_delay", 0.0)
    server._limiter.reset()

    def install(*replies: Reply) -> _Wire:
        fake = _Wire(*replies)
        monkeypatch.setattr(server, "impersonated_get", fake)
        return fake

    yield install
    server._limiter.reset()


def _ok(html: str = "<html>страница</html>") -> Reply:
    return Reply(200, html, None)


def _stopped() -> bool:
    return server.SOURCE in stopgate.default_gate().status()


async def _fetch(url: str = "https://market.yandex.ru/search?text=a") -> str:
    return await server._fetch_html(url, "test", None)


async def test_a_page_is_read_as_chrome_and_cached(wire):
    fake = wire(_ok())

    assert await _fetch() == "<html>страница</html>"
    assert await _fetch() == "<html>страница</html>"

    assert len(fake.urls) == 1
    assert "User-Agent" not in fake.headers[0]
    assert fake.headers[0]["Accept-Language"].startswith("ru-RU")


async def test_a_redirect_to_showcaptcha_stops_the_market_on_the_first_request(wire):
    fake = wire(Reply(302, "", None, "https://market.yandex.ru/showcaptcha?retpath=x"))

    with pytest.raises(ToolError) as excinfo:
        await _fetch()
    assert error_payload(excinfo.value)["error"] == "challenge_required"
    assert "do not retry" in error_payload(excinfo.value)["message"]

    with pytest.raises(ToolError) as again:
        await _fetch("https://market.yandex.ru/search?text=b")
    assert error_payload(again.value)["source_stopped"] is True
    assert len(fake.urls) == 1
    assert _stopped()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_a_refusal_status_stops_the_market_without_a_retry(wire, status):
    fake = wire(Reply(status, "нельзя", None))

    with pytest.raises(ToolError) as excinfo:
        await _fetch()

    assert error_payload(excinfo.value)["error"] == "rate_limited"
    assert len(fake.urls) == 1
    assert _stopped()


async def test_a_smartcaptcha_page_stops_the_market_and_is_not_cached(wire):
    fake = wire(_ok('<div id="SmartCaptcha"></div>'))

    with pytest.raises(ToolError) as excinfo:
        await _fetch()

    assert error_payload(excinfo.value)["error"] == "challenge_required"
    assert server._cache.get("https://market.yandex.ru/search?text=a") is None
    assert len(fake.urls) == 1
    assert _stopped()


async def test_the_word_captcha_alone_is_a_healthy_page(wire):
    wire(_ok('<div id="/content/captchaService"></div><p>товары</p>'))

    assert "товары" in await _fetch()
    assert not _stopped()


async def test_a_bare_302_is_retried_as_a_hiccup(wire):
    fake = wire(Reply(302, "", None), _ok())

    assert await _fetch() == "<html>страница</html>"
    assert len(fake.urls) == 2
    assert not _stopped()


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [(Reply(503, "", None), "HTTP 503"), (Reply(0, None, "timeout: no response within 45s"), "timeout")],
)
async def test_faults_are_retried_within_the_budget_then_reported(wire, reply, fragment):
    fake = wire(reply, reply, reply)

    with pytest.raises(ToolError) as excinfo:
        await _fetch()

    payload = error_payload(excinfo.value)
    assert payload["error"] == "transport_down"
    assert fragment in payload["message"]
    assert len(fake.urls) == 3
    assert not _stopped()


async def test_a_redirect_to_the_canonical_card_is_followed_once(wire):
    fake = wire(Reply(301, "", None, "/card/chaynik-redmond/42?sku=7&do-waremd5=abc"), _ok())

    await _fetch("https://market.yandex.ru/product/42")

    assert fake.urls == ["https://market.yandex.ru/product/42", "https://market.yandex.ru/card/chaynik-redmond/42"]


@pytest.mark.parametrize(
    "replies",
    [
        [Reply(302, "", None, "https://passport.yandex.ru/auth")],
        [Reply(301, "", None, "/card/a/1"), Reply(301, "", None, "/card/b/1")],
    ],
)
async def test_other_redirects_are_reported_not_followed(wire, replies):
    fake = wire(*replies)

    with pytest.raises(ToolError) as excinfo:
        await _fetch("https://market.yandex.ru/product/1")

    assert error_payload(excinfo.value)["error"] == "transport_down"
    assert len(fake.urls) == len(replies)
    assert not _stopped()


async def test_a_missing_page_is_not_found(wire):
    wire(Reply(404, "нет", None))

    with pytest.raises(ToolError) as excinfo:
        await _fetch()

    assert error_payload(excinfo.value)["error"] == "not_found"


async def test_an_empty_body_is_a_transport_failure(wire):
    wire(_ok("   "))

    with pytest.raises(ToolError) as excinfo:
        await _fetch()

    assert error_payload(excinfo.value)["error"] == "transport_down"


async def test_a_stopped_market_sends_nothing(wire):
    fake = wire()
    stopgate.default_gate().trip(server.SOURCE, "captcha")

    with pytest.raises(ToolError) as excinfo:
        await _fetch()

    assert error_payload(excinfo.value)["source_stopped"] is True
    assert fake.urls == []


# ------------------------------------------------------- pagination dedupe ----


def _stub_parsed_items(monkeypatch, raw_items):
    """Feed yandex_search a known item list, bypassing HTML and the SSR parser."""
    stub_html(monkeypatch, {"/search": "<html></html>"})

    def fake_parse_search(_html):
        return {
            "status": server.ssr.ParseStatus.OK,
            "items": raw_items,
            "query": "проверка",
            "page": 1,
            "page_count": 30,
            "total": 500,
            "has_next_page": True,
        }

    monkeypatch.setattr(server.ssr, "parse_search", fake_parse_search)


async def test_search_drops_repeated_product_ids(monkeypatch):
    """One product occupying several snippets must be reported once."""
    _stub_parsed_items(
        monkeypatch,
        [
            {"product_id": "111", "title": "первый"},
            {"product_id": "222", "title": "второй"},
            {"product_id": "111", "title": "первый снова"},
            {"product_id": "333", "title": "третий"},
        ],
    )

    result = await server.yandex_search(query="проверка")

    ids = [item.product_id for item in result.items]
    assert ids == ["111", "222", "333"]
    assert result.returned == 3


async def test_dedupe_runs_before_the_limit_is_applied(monkeypatch):
    """A duplicate must not eat part of the caller's budget.

    Slicing first would return two distinct products for limit=3 while claiming
    the page was full, with nothing to explain the shortfall.
    """
    _stub_parsed_items(
        monkeypatch,
        [
            {"product_id": "111", "title": "a"},
            {"product_id": "111", "title": "a duplicate"},
            {"product_id": "222", "title": "b"},
            {"product_id": "333", "title": "c"},
        ],
    )

    result = await server.yandex_search(query="проверка", limit=3)

    assert [item.product_id for item in result.items] == ["111", "222", "333"]
    assert result.returned == 3


async def test_dedupe_preserves_upstream_ranking_order(monkeypatch):
    """Yandex's ordering is the result of the search and must survive."""
    _stub_parsed_items(
        monkeypatch,
        [
            {"product_id": "999", "title": "ranked first"},
            {"product_id": "111", "title": "ranked second"},
            {"product_id": "999", "title": "repeat of first"},
            {"product_id": "555", "title": "ranked third"},
        ],
    )

    result = await server.yandex_search(query="проверка")

    assert [item.product_id for item in result.items] == ["999", "111", "555"]


async def test_products_without_an_id_are_never_collapsed(monkeypatch):
    """A blank id means "unknown", not "the same product"."""
    _stub_parsed_items(
        monkeypatch,
        [
            {"product_id": "", "title": "unidentified one"},
            {"product_id": "", "title": "unidentified two"},
            {"product_id": "111", "title": "identified"},
        ],
    )

    result = await server.yandex_search(query="проверка")

    assert result.returned == 3
    assert [item.title for item in result.items] == [
        "unidentified one",
        "unidentified two",
        "identified",
    ]


async def test_limit_still_caps_a_page_without_duplicates(monkeypatch):
    _stub_parsed_items(
        monkeypatch,
        [{"product_id": str(i), "title": f"item {i}"} for i in range(10)],
    )

    result = await server.yandex_search(query="проверка", limit=4)

    assert result.returned == 4
    assert [item.product_id for item in result.items] == ["0", "1", "2", "3"]


async def test_selfcheck_skips_the_card_when_search_gave_no_card_link(monkeypatch):
    """Без ссылки /card/ зонд карточки не идёт на /product/<id>: там капча."""
    stub_html(monkeypatch, {"/search": load("search_washer.html")})

    result = await server.yandex_selfcheck()

    assert result.checks["search"].state == "healthy"
    assert result.checks["card"].state == "inconclusive"
    assert "/card/" in result.checks["card"].detail
