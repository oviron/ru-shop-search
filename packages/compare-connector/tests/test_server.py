"""Offline tests for cross-marketplace comparison.

Each marketplace's search is stubbed, so these tests exercise the part that only
this connector owns: merging, ranking, and reporting partial failures honestly.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

import pytest
from compare_connector import server
from compare_connector.models_output import MarketOffer
from fastmcp.exceptions import ToolError
from mcp_core import resilience as R
from mcp_core.errors import ChallengeRequiredError, ParserDriftError, RateLimitedError, raise_tool_error
from pydantic import ValidationError


def offer(source: str, price: float | None, title: str = "товар", **kwargs) -> MarketOffer:
    return MarketOffer(source=source, title=title, price_rub=price, **kwargs)


def stub_sources(monkeypatch, impls: dict[str, object]):
    """Replace the per-marketplace search implementations.

    ``SOURCES`` is patched alongside so availability checks agree with the stubs.
    """
    monkeypatch.setattr(server, "_SEARCH_IMPLS", impls)
    monkeypatch.setattr(server, "SOURCES", dict.fromkeys(impls, object()))


def error_payload(err: ToolError) -> dict:
    return json.loads(str(err))


# ------------------------------------------------------------------ basics ----


def test_server_version_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == server.SERVER_VERSION


async def test_registered_tools_are_stable():
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert names == {"compare_prices", "compare_sources", "compare_verify_offer"}


async def test_compare_verify_offer_dispatches_to_source_card(monkeypatch):
    class FakeCard:
        async def __call__(self, *, nm_ids):
            return {"items": [{"nm_id": nm_ids[0], "price_rub": 1234}]}

    class FakeSource:
        wb_card = FakeCard()

    monkeypatch.setattr(server, "SOURCES", {"wildberries": FakeSource()})

    result = await server.compare_verify_offer(
        "wildberries", "https://www.wildberries.ru/catalog/123/detail.aspx", expected_price_rub=1234
    )

    assert result["source"] == "wildberries"
    assert result["card"]["items"][0]["nm_id"] == 123
    assert result["price_verification"]["matches"] is True


async def test_default_comparison_omits_deselected_sources_but_explicit_request_is_rejected(monkeypatch):
    async def search(query, limit):
        return [offer("wildberries", 100)]

    monkeypatch.setenv("MARKETPLACE_SOURCES", "wildberries,compare")
    monkeypatch.setattr(server, "_SEARCH_IMPLS", {"wildberries": search, "ozon": search})
    monkeypatch.setattr(server, "SOURCES", {"wildberries": object(), "ozon": object()})

    result = await server.compare_prices("тест")
    assert result.sources_queried == ["wildberries"]
    assert result.complete is True
    with pytest.raises(ToolError, match="deselected"):
        await server.compare_prices("тест", sources=["ozon"])


async def test_selected_missing_connector_still_makes_default_comparison_partial(monkeypatch):
    async def search(query, limit):
        return [offer("wildberries", 100)]

    monkeypatch.setenv("MARKETPLACE_SOURCES", "wildberries,ozon,compare")
    monkeypatch.setattr(server, "_SEARCH_IMPLS", {"wildberries": search, "ozon": search, "dns": search})
    monkeypatch.setattr(server, "SOURCES", {"wildberries": object()})

    result = await server.compare_prices("тест")
    assert result.complete is False
    assert {outcome.source: outcome.status for outcome in result.source_outcomes} == {
        "wildberries": "ok",
        "ozon": "not_installed",
    }
    report = await server.compare_sources()
    assert report["not_installed"] == ["ozon"]
    assert "dns" in report["deselected"]
    assert "ozon" not in report["deselected"]


async def test_detsky_mir_search_rows_become_offers(monkeypatch):
    from detmir_connector import server as detmir
    from detmir_connector.models_output import DetmirListResponse, MetaOut

    fixture = Path(__file__).parents[2] / "detmir-connector/tests/fixtures/search_lego_technic_live.html"
    raw = detmir._app_data(fixture.read_text(encoding="utf-8"))["catalog"]["data"]["items"]
    products = [detmir._parse_product(item) for item in raw]

    async def detmir_search(query, limit):
        return DetmirListResponse(
            query=query, mode="search", returned=len(products), items=products, meta=MetaOut(source="detmir_search")
        )

    monkeypatch.setattr(
        server, "SOURCES", {"detsky_mir": type("M", (), {"detmir_search": staticmethod(detmir_search)})}
    )

    offers = await server._search_detmir("lego technic", 2)

    assert "detsky_mir" in server.SEARCHABLE
    assert [(o.product_id, o.price_rub, o.brand, o.in_stock) for o in offers] == [
        ("6750060", 7199.0, "LEGO", True),
        ("6750058", 6083.0, "LEGO", True),
    ]
    assert offers[0].url == "https://www.detmir.ru/product/index/id/6750060/"


async def test_lamoda_search_rows_become_offers(monkeypatch):
    from lamoda_connector import server as lamoda
    from mcp_core.transport import impersonated
    from mcp_core.transport.impersonated import Reply

    html = (Path(__file__).parents[2] / "lamoda-connector/tests/fixtures/search_live.html").read_text(encoding="utf-8")

    async def fake_get(url, **kwargs):
        return Reply(200, html, None)

    monkeypatch.setattr(impersonated, "impersonated_get", fake_get)
    monkeypatch.setattr(lamoda._pacer, "min_gap", 0.0)
    lamoda._cache.clear()
    monkeypatch.setattr(server, "SOURCES", {"lamoda": lamoda})

    offers = await server._search_lamoda("кроссовки adidas", 2)

    assert [(o.product_id, o.price_rub, o.brand, o.rating, o.rating_count, o.in_stock) for o in offers] == [
        ("RTLAEU059301", 8063.0, "adidas Originals", 4.75, 95, True),
        ("RTLAES035701", 6299.0, "adidas", 4.82, 58, True),
    ]


async def test_citilink_search_rows_become_offers(monkeypatch):
    import json as _json
    from contextlib import asynccontextmanager

    from citilink_connector import server as citilink
    from mcp_core.domtest import JsdomUnavailable, run_extractor

    fixture = Path(__file__).parents[2] / "citilink-connector/tests/fixtures/search_rtx5070_live.html"
    try:
        payload = run_extractor(
            citilink._SEARCH_EXTRACT_JS, fixture, page_url="https://www.citilink.ru/search/?text=rtx"
        )
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))

    class Page:
        async def evaluate(self, expression, arg=None):
            return _json.dumps(payload, ensure_ascii=False)

    @asynccontextmanager
    async def open_page(url, wait_ms=5000, **kwargs):
        yield Page()

    monkeypatch.setattr(citilink, "open_page", open_page)
    monkeypatch.setattr(citilink, "_min_gap", 0.0)
    citilink._cache.clear()
    monkeypatch.setattr(server, "SOURCES", {"citilink": citilink})

    offers = await server._search_citilink("rtx 5070", 4)

    assert [(o.price_rub, o.rating, o.rating_count, o.in_stock) for o in offers] == [
        (96690.0, 4.8, 13, True),
        (94560.0, 4.9, 18, True),
        (105500.0, 4.7, 7, True),
        (None, 5.0, 2, False),
    ]


async def test_mvideo_search_rows_become_offers(monkeypatch):
    from mcp_core.pacing import Pacer
    from mcp_core.stopgate import StopGate
    from mcp_core.transport import impersonated
    from mcp_core.transport.impersonated import Reply
    from mvideo_connector import server as mvideo

    fixture = Path(__file__).parents[2] / "mvideo-connector/tests/fixtures/search_rtx5070_live.json"

    async def fake_post(url, body, **kwargs):
        return Reply(200, fixture.read_text(encoding="utf-8"), None)

    monkeypatch.setattr(impersonated, "impersonated_post", fake_post)
    monkeypatch.setattr(mvideo, "_pacer", Pacer(0.0, source="mvideo", gate=StopGate(None)))
    mvideo._cache.clear()
    monkeypatch.setattr(server, "SOURCES", {"mvideo": mvideo})

    offers = await server._search_mvideo("rtx 5070", 12)

    assert [(o.product_id, o.price_rub, o.seller, o.rating_count, o.in_stock) for o in offers[:2]] == [
        ("400678996", 84999.0, "М.Видео", 1, True),
        ("400535386", 126350.0, "М.Видео", 28, True),
    ]
    assert [(o.product_id, o.seller) for o in offers if o.seller != "М.Видео"] == [("402590260", "")]


async def test_vseinstrumenti_search_rows_become_offers(monkeypatch):
    from mcp_core.pacing import Pacer
    from mcp_core.stopgate import StopGate
    from mcp_core.transport import impersonated
    from mcp_core.transport.impersonated import Reply
    from vseinstrumenti_connector import server as vseinstrumenti

    fixture = Path(__file__).parents[2] / "vseinstrumenti-connector/tests/fixtures/search_makita_live.html"

    async def fake_get(url, **kwargs):
        return Reply(200, fixture.read_text(encoding="utf-8"), None)

    monkeypatch.setattr(impersonated, "impersonated_get", fake_get)
    monkeypatch.setattr(vseinstrumenti, "_pacer", Pacer(0.0, source="vseinstrumenti", gate=StopGate(None)))
    vseinstrumenti._cache.clear()
    monkeypatch.setattr(server, "SOURCES", {"vseinstrumenti": vseinstrumenti})

    offers = await server._search_vseinstrumenti("перфоратор makita", 2)

    assert [(o.product_id, o.price_rub, o.seller, o.rating_count, o.in_stock, o.url) for o in offers] == [
        (
            "5195",
            12500.0,
            "ВсеИнструменты",
            2475,
            True,
            "https://www.vseinstrumenti.ru/product/perforator-makita-hr-2470-5195/",
        ),
        (
            "814840",
            14490.0,
            "ВсеИнструменты",
            245,
            True,
            "https://www.vseinstrumenti.ru/product/perforator-makita-hr2470x15-814840/",
        ),
    ]


# ----------------------------------------------------------------- ranking ----


async def test_offers_are_ranked_cheapest_first(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 1500.0), offer("wildberries", 700.0)]

    async def ya(query, limit):
        return [offer("yandex_market", 1000.0)]

    stub_sources(monkeypatch, {"wildberries": wb, "yandex_market": ya})

    result = await server.compare_prices(query="тест")

    assert [o.price_rub for o in result.offers] == [700.0, 1000.0, 1500.0]
    assert result.cheapest.price_rub == 700.0
    assert result.cheapest.source == "wildberries"
    assert result.price_spread_rub == 800.0
    assert result.complete is True


async def test_subscription_prices_never_win_the_ranking(monkeypatch):
    """A Plus-only price must not be presented as the cheapest available.

    Yandex's subscriber price runs 25-30% below its everyday price; ranking on it
    would fabricate a bargain that non-subscribers cannot buy.
    """

    async def wb(query, limit):
        return [offer("wildberries", 1000.0)]

    async def ya(query, limit):
        return [offer("yandex_market", 1200.0, price_with_subscription_rub=800.0)]

    stub_sources(monkeypatch, {"wildberries": wb, "yandex_market": ya})

    result = await server.compare_prices(query="тест")

    assert result.cheapest.source == "wildberries"
    assert result.cheapest.price_rub == 1000.0
    # The subscriber price is still reported, just not ranked on.
    yandex_offer = next(o for o in result.offers if o.source == "yandex_market")
    assert yandex_offer.price_with_subscription_rub == 800.0


async def test_unpriced_offers_are_kept_at_the_end(monkeypatch):
    """'Found but no price' is information; dropping it would hide stock reality."""

    async def wb(query, limit):
        return [offer("wildberries", None, title="без цены"), offer("wildberries", 500.0)]

    stub_sources(monkeypatch, {"wildberries": wb})

    result = await server.compare_prices(query="тест", sources=["wildberries"])

    assert result.total_offers == 2
    assert result.offers[0].price_rub == 500.0
    assert result.offers[-1].price_rub is None
    assert result.cheapest.price_rub == 500.0


async def test_spread_is_none_with_a_single_priced_offer(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 500.0)]

    stub_sources(monkeypatch, {"wildberries": wb})

    result = await server.compare_prices(query="тест", sources=["wildberries"])

    assert result.price_spread_rub is None


# ------------------------------------------------------- partial failures ----


async def test_one_source_failing_does_not_sink_the_comparison(monkeypatch):
    """The core resilience promise: three sources answering still beats nothing."""

    async def wb(query, limit):
        return [offer("wildberries", 500.0)]

    async def ozon(query, limit):
        raise RuntimeError('{"error": "transport_down", "message": "Cloudflare"}')

    stub_sources(monkeypatch, {"wildberries": wb, "ozon": ozon})

    result = await server.compare_prices(query="тест")

    assert result.total_offers == 1
    assert result.complete is False
    assert result.sources_ok == ["wildberries"]
    outcomes = {o.source: o for o in result.source_outcomes}
    assert outcomes["ozon"].status == "blocked"
    assert any("partial" in w for w in result.warnings)


async def test_a_timeout_is_reported_as_a_timeout(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 500.0)]

    async def slow(query, limit):
        await asyncio.sleep(5)
        return []

    stub_sources(monkeypatch, {"wildberries": wb, "yandex_market": slow})
    monkeypatch.setattr(server, "SOURCE_TIMEOUT_S", 0.05)

    result = await server.compare_prices(query="тест")

    outcomes = {o.source: o for o in result.source_outcomes}
    assert outcomes["yandex_market"].status == "timeout"
    assert result.complete is False
    assert result.total_offers == 1
    assert outcomes["yandex_market"].retryable is True


@pytest.mark.parametrize("wire_error", [False, True])
async def test_challenge_keeps_partial_results_and_stops_only_the_failed_source(monkeypatch, wire_error):
    calls = []

    async def wb(query, limit):
        calls.append("wildberries")
        return [offer("wildberries", 500.0)]

    async def aliexpress(query, limit):
        calls.append("aliexpress")
        error = ChallengeRequiredError("visible challenge " * 40, challenge_type="login_or_captcha")
        if wire_error:
            raise_tool_error(error)
        raise error

    stub_sources(monkeypatch, {"wildberries": wb, "aliexpress": aliexpress})
    result = await server.compare_prices(query="тест")
    outcome = next(o for o in result.source_outcomes if o.source == "aliexpress")
    assert result.complete is False
    assert result.cheapest.source == "wildberries"
    assert outcome.status == "blocked"
    assert outcome.error_code == "challenge_required"
    assert outcome.challenge_type == "login_or_captcha"
    assert len(outcome.detail) <= 200

    again = await server.compare_prices(query="тест")
    stopped = next(o for o in again.source_outcomes if o.source == "aliexpress")
    assert calls.count("aliexpress") == 1
    assert calls.count("wildberries") == 2
    assert stopped.status == "blocked"
    assert stopped.retry_after_s is not None and stopped.retry_after_s > 0


@pytest.mark.parametrize(
    ("error", "status", "code", "retryable"),
    [
        (RateLimitedError("ozon"), "blocked", "rate_limited", True),
        (ParserDriftError("field 403 missing"), "error", "parser_drift", False),
    ],
)
def test_structured_error_code_takes_precedence_over_message(error, status, code, retryable):
    result = server._source_error(ToolError(json.dumps(error.to_dict())))
    assert result["status"] == status
    assert result["error_code"] == code
    assert result["retryable"] is retryable
    assert result["retry_after_s"] is None


@pytest.mark.parametrize("message", ["broken", "[]", "null", '{"error": []}', '{"error": "new_code"}'])
def test_malformed_error_envelope_does_not_break_comparison(message):
    result = server._source_error(ToolError(message))
    assert result["status"] == "error"


def test_challenge_metadata_does_not_leak_credentials_or_upstream_instructions():
    payload = {
        "error": "challenge_required",
        "message": "failed at https://user:secret@example.com/?token=private",
        "challenge_type": "visit https://untrusted.invalid/?token=private",
    }
    result = server._source_error(ToolError(json.dumps(payload)))
    assert "secret" not in result["detail"]
    assert "private" not in result["detail"]
    assert result["challenge_type"] is None


async def test_a_generic_failure_is_reported_as_error_not_blocked(monkeypatch):
    """Anti-bot blocks and ordinary bugs need different responses, so they differ."""

    async def wb(query, limit):
        raise ValueError("unexpected shape in response")

    stub_sources(monkeypatch, {"wildberries": wb})

    result = await server.compare_prices(query="тест", sources=["wildberries"])

    assert result.source_outcomes[0].status == "error"
    assert result.total_offers == 0
    assert any("no_prices" in w for w in result.warnings)


async def test_sources_report_their_elapsed_time(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 100.0)]

    stub_sources(monkeypatch, {"wildberries": wb})

    result = await server.compare_prices(query="тест", sources=["wildberries"])

    assert result.source_outcomes[0].elapsed_ms >= 0
    assert result.source_outcomes[0].offers_returned == 1


async def test_a_missing_connector_is_distinguished_from_a_block(monkeypatch):
    """'Not installed' needs a different fix than 'refused us', so they differ."""

    async def wb(query, limit):
        return [offer("wildberries", 100.0)]

    monkeypatch.setattr(server, "_SEARCH_IMPLS", {"wildberries": wb, "ozon": wb})
    monkeypatch.setattr(server, "SOURCES", {"wildberries": object()})  # Ozon absent

    result = await server.compare_prices(query="тест", sources=["wildberries", "ozon"])

    outcomes = {o.source: o for o in result.source_outcomes}
    assert outcomes["ozon"].status == "not_installed"
    assert result.complete is False


async def test_degraded_yandex_rows_never_rank_with_a_subscription_price(monkeypatch):
    """A Plus price must not masquerade as the everyday price in the ranking.

    Live fact 2026-09-13: with the SSR widget state gone, yandex_search answers
    through its schema.org fallback, whose offers.price is the Plus subscriber
    price (Realme Note 60x: ld+json 11102 == the yaBank additional price, while
    the everyday price was 11329 and the base 12450). Such rows carry
    price_rub=None and may only ride in price_with_subscription_rub, keeping
    the offer unranked instead of letting a subscription price win.
    """

    async def degraded_yandex(query, limit):
        return [
            offer(
                "yandex_market",
                None,
                title="Смартфон Realme Note 60х 4/128GB Зеленый",
                product_id="4315891968",
                variant_id="103796664836",
                price_with_subscription_rub=11102.0,
            )
        ]

    stub_sources(monkeypatch, {"yandex_market": degraded_yandex})

    result = await server.compare_prices(query="телефон", sources=["yandex_market"])

    assert result.cheapest is None
    assert result.total_offers == 1
    row = result.offers[0]
    assert row.price_rub is None
    assert row.price_with_subscription_rub == 11102.0
    assert any("no_prices" in warning for warning in result.warnings)


async def test_sources_run_concurrently(monkeypatch):
    """Serial queries would make a four-source comparison unusably slow."""
    started: list[float] = []

    async def make(delay: float):
        async def impl(query, limit):
            started.append(asyncio.get_running_loop().time())
            await asyncio.sleep(delay)
            return [offer("x", 100.0)]

        return impl

    impls = {
        "wildberries": await make(0.1),
        "yandex_market": await make(0.1),
        "ozon": await make(0.1),
    }
    stub_sources(monkeypatch, impls)

    loop_start = asyncio.get_running_loop().time()
    await server.compare_prices(query="тест")
    elapsed = asyncio.get_running_loop().time() - loop_start

    assert len(started) == 3
    # Concurrent: ~0.1s total rather than ~0.3s serial.
    assert elapsed < 0.25


# -------------------------------------------------------------- validation ----


@pytest.mark.parametrize("bad_query", ["", " ", "x"])
async def test_short_queries_are_rejected(monkeypatch, bad_query):
    """Rejected either by the tool's own check or by pydantic's min_length.

    Both are correct outcomes; what matters is that no marketplace is queried.
    """

    async def fail(query, limit):
        raise AssertionError("a rejected query must never reach a marketplace")

    stub_sources(monkeypatch, {"wildberries": fail})

    with pytest.raises((ToolError, ValidationError)):
        await server.compare_prices(query=bad_query, sources=["wildberries"])


async def test_unknown_source_names_are_rejected(monkeypatch):
    async def wb(query, limit):
        return []

    stub_sources(monkeypatch, {"wildberries": wb})

    with pytest.raises(ToolError) as excinfo:
        await server.compare_prices(query="тест", sources=["aliexpress"])

    payload = error_payload(excinfo.value)
    assert payload["error"] == "bad_request"
    assert "aliexpress" in payload["message"]


async def test_all_requested_sources_missing_is_an_error(monkeypatch):
    """No installed source means the answer would be empty and misleading."""

    async def wb(query, limit):
        return []

    monkeypatch.setattr(server, "_SEARCH_IMPLS", {"wildberries": wb})
    monkeypatch.setattr(server, "SOURCES", {})

    with pytest.raises(ToolError) as excinfo:
        await server.compare_prices(query="тест", sources=["wildberries"])

    assert error_payload(excinfo.value)["error"] == "bad_request"


async def test_per_source_limit_is_passed_through(monkeypatch):
    seen: dict[str, int] = {}

    async def wb(query, limit):
        seen["limit"] = limit
        return []

    stub_sources(monkeypatch, {"wildberries": wb})

    await server.compare_prices(query="тест", per_source_limit=7, sources=["wildberries"])

    assert seen["limit"] == 7


# ----------------------------------------------------------- source report ----


async def test_compare_sources_reports_installed_and_missing(monkeypatch):
    monkeypatch.setattr(server, "SOURCES", {"wildberries": object(), "yandex_market": object()})

    report = await server.compare_sources()

    assert "wildberries" in report["installed"]
    assert "ozon" in report["not_installed"]
    assert report["server_version"] == server.SERVER_VERSION
    assert "source_timeout_s" in report


# --------------------------------------------------------- typed adapters ----


class _FakeOzonItem:
    """Mirrors OzonSearchItemOut: every value arrives as display text."""

    def __init__(self, **kw):
        self.sku = kw.get("sku")
        self.url = kw.get("url")
        self.canonical_path = kw.get("canonical_path")
        self.card_input = kw.get("card_input")
        self.title = kw.get("title")
        self.price = kw.get("price")
        self.price_original = kw.get("price_original")
        self.rating = kw.get("rating")
        self.rating_count = kw.get("rating_count")
        self.stock = kw.get("stock")


class _FakeResponse:
    def __init__(self, items):
        self.items = items


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234 ₽", 1234.0),
        ("1 234 ₽", 1234.0),  # non-breaking space
        ("1 234,50 ₽", 1234.5),  # narrow no-break space, comma decimal
        ("999 ₽", 999.0),
        (1500, 1500.0),
        (1500.5, 1500.5),
        ("0 ₽", None),  # a zero price is not a bargain, it is no offer
        (0, None),
        ("", None),
        (None, None),
        ("нет цены", None),
        (True, None),  # a bool is not a price
    ],
)
def test_price_coercion_handles_ozon_display_strings(raw, expected):
    """Ozon reports prices as text; MarketOffer.price_rub is a float.

    Passing the raw string through raised a pydantic ValidationError, which would
    have taken down the entire Ozon source rather than one offer.
    """
    assert server._as_price(raw) == expected


def test_price_coercion_never_substitutes_zero():
    """A 0.0 would rank a dead listing as the cheapest option."""
    assert server._as_price("0 ₽") is None
    assert server._as_price(0) is None
    assert server._as_price(0.0) is None


def test_price_coercion_never_returns_a_negative():
    """A negative is not a price. Ranking one would crown it the cheapest
    offer, so it must degrade to no-offer exactly like a zero does."""
    assert server._as_price(-500) is None
    assert server._as_price(-500.0) is None
    assert server._as_price("-500 ₽") is None
    assert server._as_price("-1 234") is None


def test_price_coercion_never_returns_a_non_finite_value():
    """inf is not a price either: ``10**400`` blows up ``float()`` with
    OverflowError, a float inf compares as greater than every real price
    (so it would never win the ranking, but it also never belongs in an
    offer), and mcp_core.coerce_price already guarantees finiteness —
    the compare connector must not diverge from that doctrine."""
    assert server._as_price(float("inf")) is None
    assert server._as_price(float("-inf")) is None
    assert server._as_price(float("nan")) is None
    assert server._as_price(10**400) is None
    assert server._as_price("9" * 400) is None


def test_price_coercion_never_concatenates_a_price_range():
    """Live-captured Ozon filter strings (rendered search page, 2026-08-07,
    the captured pages kept outside the repository, ozon_search_page_raw.html) carry TWO numbers:
    '1 000–2 000 ₽'. The old tolerant parser concatenated them into
    10002000.0 — a plausible-looking fabricated price. The shared doctrine
    is: more than one number means ambiguous, which means None."""
    assert server._as_price("1\u2009000–2\u2009000\u2009₽") is None
    assert server._as_price("2\u2009000–30\u2009000\u2009₽") is None


@pytest.mark.parametrize(
    "raw",
    [
        # Thin-space grouped integers exactly as captured from Ozon's rendered
        # pages on 2026-08-07 (the captured pages kept outside the repository, ozon_*_page_raw.html); the two
        # comma-decimal forms were not on those pages but are the kopeck
        # display shape coerce_price is documented to handle — pinned here so
        # the delegation's decimal parity is explicit, not implied.
        "158\u2009370\u2009₽",
        "84\u2009817\u2009₽",
        "3\u2009266\u2009₽",
        "1\u2009000\u2009₽",
        "79\u2009990\u2009₽",
        "99,50\u2009₽",
        "1\u2009234,50\u2009₽",
    ],
)
def test_price_coercion_keeps_coerce_price_parity_on_live_ozon_formats(raw):
    assert server._as_price(raw) == R.coerce_price(raw)
    assert server._as_price(raw) is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("24 086 отзывов", 24086),
        ("(15 374)", 15374),
        ("5", 5),
        (42, 42),
        (None, None),
        ("нет", None),
    ],
)
def test_count_coercion_handles_russian_review_labels(raw, expected):
    assert server._as_count(raw) == expected


def test_count_coercion_never_raises_on_non_finite_floats():
    """json.loads admits NaN/Infinity by default, and int() raises on both.
    coerce_int guards them with isfinite, and the compare duplicate must keep
    parity — otherwise one poisoned cell aborts the whole compare_prices tool
    instead of degrading that field to no-data."""
    assert server._as_count(float("nan")) is None
    assert server._as_count(float("inf")) is None
    assert server._as_count(float("-inf")) is None


def test_count_coercion_never_drops_a_sign():
    """A signed count is ambiguous: dropping the sign and concatenating digits
    fabricates a plausible-but-wrong number ('-3' -> 3). That is the doctrine
    coerce_int established — signed means None, not a manufactured count."""
    assert server._as_count("-3") is None
    assert server._as_count("+5") is None


def test_count_coercion_rejects_unicode_signs_and_ranges():
    """The sign guard must cover the unicode minus/en-dash/em-dash, not only
    ASCII -/+: a marketplace renders a range as '1 000–2 000', and concatenating
    those digits fabricates 10002000 — the same hole coerce_int had and the
    compare duplicate inherits. '−5' must not read as 5 either."""
    assert server._as_count("1 000–2 000") is None
    assert server._as_count("5–10") is None
    assert server._as_count("−5") is None
    # the tolerant review-label form must still parse — letters are not the hole
    assert server._as_count("24 086 отзывов") == 24086


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("осталось 3 шт", True),
        ("осталось 0 шт", False),
        ("много шт", True),
        ("477\u2009шт осталось", True),
        ("осталось 1\u202f000 шт.", True),
        ("В наличии", True),
        ("есть в наличии", True),
        ("Не осталось", False),
        ("нет в наличии", False),
        ("нет на складе", False),
        ("товар закончился", False),
        ("распродано", False),
        ("0 шт осталось", False),
        ("2 шт", None),  # pack size is not a stock statement
        ("купили 3 шт", None),
        ("в упаковке 5 шт", None),
        ("скидка 30%", None),
        ("осталось -3 шт", None),
        ("осталось +3 шт", None),
        ("осталось −3 шт", None),
        ("осталось 3–5 шт", None),
        ("осталось 3-5 шт", None),
        ("осталось 3.5 шт", None),
        ("осталось 1234 567 шт", None),
        ("осталось 3 шт, купили 20", None),
        ("осталось", None),
        (5, True),
        (0, False),
        (None, None),  # unknown is not the same as out of stock
        ("", None),
    ],
)
def test_stock_label_coercion(raw, expected):
    assert server._stock_from_label(raw) is expected


def test_stock_label_never_claims_stock_from_a_non_finite_value():
    """Only a positive statement counts as in stock. NaN compared > 0 is
    False and inf compared > 0 is True, so without a finiteness guard a
    poisoned cell asserts OUT OF STOCK (NaN) or IN STOCK (inf) — two lies
    where the docstring promises "unknown", because json.loads admits
    NaN/Infinity by default."""
    assert server._stock_from_label(float("nan")) is None
    assert server._stock_from_label(float("inf")) is None
    assert server._stock_from_label(float("-inf")) is None


async def test_ozon_adapter_reads_the_real_model_fields(monkeypatch):
    """The old adapter guessed keys OzonSearchItemOut does not declare.

    It read price_rub, reviews_count, feedbacks, name, id and brand — none exist,
    so each silently became None. This pins the real field names.
    """
    item = _FakeOzonItem(
        sku=123456,
        title="товар",
        price="1 999 ₽",
        rating="4,8",
        rating_count="24 086 отзывов",
        stock="осталось 3 шт",
        url="https://www.ozon.ru/product/123456/",
    )

    class FakeOzonServer:
        async def ozon_search(self, query):
            return _FakeResponse([item])

    monkeypatch.setitem(server.SOURCES, "ozon", FakeOzonServer())

    offers = await server._search_ozon("товар", 10)

    assert len(offers) == 1
    got = offers[0]
    assert got.product_id == "123456"
    assert (got.price_rub, got.price_with_subscription_rub) == (None, 1999.0)
    assert got.rating == 4.8
    assert got.rating_count == 24086, "review count was always None before"
    assert got.in_stock is True, "stock was ignored entirely before"
    assert got.url == "https://www.ozon.ru/product/123456/"
    # Ozon search rows genuinely carry neither, so they are empty by definition.
    assert got.brand == ""
    assert got.seller == ""


async def test_ozon_adapter_survives_a_priceless_row(monkeypatch):
    class FakeOzonServer:
        async def ozon_search(self, query):
            return _FakeResponse([_FakeOzonItem(sku=1, title="без цены")])

    monkeypatch.setitem(server.SOURCES, "ozon", FakeOzonServer())

    offers = await server._search_ozon("тест", 10)

    assert offers[0].price_rub is None
    assert offers[0].in_stock is None


async def test_wildberries_adapter_reads_typed_attributes(monkeypatch):
    from wb_connector.models_output import WbCardItem

    class FakeWbServer:
        async def wb_search(self, query, page):
            return _FakeResponse(
                [
                    WbCardItem(
                        nm_id=5535522,
                        name="фильтр",
                        brand="DEFENDER",
                        supplier="Продавец",
                        review_rating=4.7,
                        feedbacks=120,
                        # Native WB requires a reported quantity for known stock.
                        total_quantity=3,
                        in_stock=True,
                        price_rub=1500.0,
                    )
                ]
            )

    monkeypatch.setitem(server.SOURCES, "wildberries", FakeWbServer())

    offers = await server._search_wildberries("фильтр", 10)

    got = offers[0]
    assert got.product_id == "5535522"
    assert got.price_rub == 1500.0
    assert got.rating == 4.7
    assert got.rating_count == 120
    assert got.in_stock is True
    assert got.url == "https://www.wildberries.ru/catalog/5535522/detail.aspx"


async def test_wildberries_adapter_tolerates_a_no_results_response(monkeypatch):
    """wb_search can return a distinct no-results model with no items at all."""
    from wb_connector.models_output import WbNoResultsResponse

    class FakeWbServer:
        async def wb_search(self, query, page):
            return WbNoResultsResponse(query="ничего")

    monkeypatch.setitem(server.SOURCES, "wildberries", FakeWbServer())

    assert await server._search_wildberries("ничего", 10) == []


async def test_yandex_adapter_keeps_the_subscriber_price_out_of_ranking(monkeypatch):
    from yandex_connector.models_output import YandexProduct

    class FakeYandexServer:
        async def yandex_search(self, query, page, limit):
            return _FakeResponse(
                [
                    YandexProduct(
                        product_id="777",
                        title="телефон",
                        brand="Бренд",
                        seller="Магазин",
                        price_rub=30000.0,
                        price_with_plus=21000.0,
                        rating=4.5,
                        rating_count=88,
                        in_stock=True,
                        url="https://market.yandex.ru/product/777",
                    )
                ]
            )

    monkeypatch.setitem(server.SOURCES, "yandex_market", FakeYandexServer())

    got = (await server._search_yandex("телефон", 10))[0]

    assert got.price_rub == 30000.0, "ranking must use the everyday price"
    assert got.price_with_subscription_rub == 21000.0


async def test_avito_adapter_maps_classified_fields(monkeypatch):
    from avito_connector.models_output import AvitoSearchItemOut

    class FakeAvitoServer:
        async def avito_search(self, query, page):
            return _FakeResponse(
                [
                    AvitoSearchItemOut(
                        item_id=4612345678,
                        title="Ноутбук ThinkPad",
                        price_rub=45500.0,
                        url="https://www.avito.ru/moskva/noutbuki/x_4612345678",
                        seller_name="ИП Сидоров",
                    ),
                    AvitoSearchItemOut(item_id=4612349999, title="Отдам даром", price_rub=None, url=""),
                ]
            )

    monkeypatch.setitem(server.SOURCES, "avito", FakeAvitoServer())

    got = await server._search_avito("ноутбук", 10)

    assert got[0].price_rub == 45500.0
    assert got[0].seller == "ИП Сидоров"
    assert got[1].price_rub is None, "a priceless ad stays priceless — never 0"


async def test_a_yuan_offer_never_becomes_cheapest(monkeypatch):
    """End-to-end: a cheap yuan number must not outrank a dearer rouble one.

    9999 ¥ is worth far more than 45500 ₽, but a naive comparator sees the
    smaller integer and calls Taobao the bargain. The ranking must exclude it
    on currency, and the response must still tell the user the yuan price.
    """
    offers = [
        MarketOffer(source="wildberries", product_id="1", title="iPhone", price_rub=45500.0),
        MarketOffer(
            source="taobao", product_id="2", title="iPhone", price_rub=None, currency="cny", price_native=9999.0
        ),
    ]

    priced = [offer for offer in offers if server._ranks_in_rubles(offer)]

    assert [offer.source for offer in priced] == ["wildberries"]
    assert offers[1].price_native == 9999.0, "the yuan price is reported, just not ranked"
    assert offers[0].price_native == 45500.0, "rouble sources mirror price_rub into price_native"


async def test_a_foreign_currency_offer_cannot_be_smuggled_into_the_ranking():
    """currency is checked independently of price_rub, on purpose.

    If an adapter regressed and filled price_rub with a yuan figure, the
    price_rub-is-not-None test alone would let it win. The currency guard is
    the backstop.
    """
    smuggled = MarketOffer(source="taobao", product_id="9", title="x", price_rub=300.0, currency="cny")

    assert not server._ranks_in_rubles(smuggled)


async def test_cdp_source_adapters_map_their_fields(monkeypatch):
    """Every CDP adapter must map its connector's typed model onto MarketOffer."""
    from aliexpress_connector.models_output import AliSearchItemOut

    class FakeAli:
        async def aliexpress_search(self, query):
            return _FakeResponse(
                [
                    AliSearchItemOut(
                        item_id="1005001234567",
                        title="Часы",
                        price_rub=3290.0,
                        rating=4.7,
                        url="https://aliexpress.ru/item/1005001234567.html",
                    )
                ]
            )

    monkeypatch.setitem(server.SOURCES, "aliexpress", FakeAli())
    got = await server._search_aliexpress("часы", 10)
    assert got[0].price_rub == 3290.0
    assert got[0].rating == 4.7


async def test_every_searchable_source_has_an_impl_and_a_search_tool():
    """SEARCHABLE must never name a source that lacks a _SEARCH_IMPLS entry —
    that is the mismatch this audit caught."""
    for name in server.SEARCHABLE:
        assert name in server._SEARCH_IMPLS, f"{name} in SEARCHABLE but not in _SEARCH_IMPLS"
    for name in server._SEARCH_IMPLS:
        assert name in server.SEARCHABLE, f"{name} has an impl but is excluded from SEARCHABLE"


# ------------------------------------------------------------------ deduplication ----


def test_duplicate_listings_are_collapsed():
    """One listing must not occupy two ranking slots.

    A marketplace returning the same product twice (colour variants sharing an
    id, an overlapping page) used to produce two offers, so a single listing
    could be both cheapest and runner-up and read as two independent
    confirmations of the same price.
    """
    offers = [
        MarketOffer(source="wildberries", product_id="1", title="Ноутбук", price_rub=52999.0),
        MarketOffer(source="wildberries", product_id="1", title="Ноутбук", price_rub=52999.0),
        MarketOffer(source="ozon", product_id="1", title="Ноутбук", price_rub=51000.0),
    ]

    out = server._dedupe(offers)

    assert len(out) == 2, "same id on the same source is one listing"
    assert [(o.source, o.product_id) for o in out] == [("wildberries", "1"), ("ozon", "1")]


def test_dedupe_keeps_source_order():
    offers = [
        MarketOffer(source="wildberries", product_id="a", title="A", price_rub=3.0),
        MarketOffer(source="wildberries", product_id="b", title="B", price_rub=1.0),
        MarketOffer(source="wildberries", product_id="a", title="A", price_rub=3.0),
    ]

    assert [o.product_id for o in server._dedupe(offers)] == ["a", "b"]


def test_offers_without_an_id_are_never_merged():
    """A blank id is unknown, not shared — merging those would lose real offers."""
    offers = [
        MarketOffer(source="dns", product_id="", title="Первый", price_rub=100.0),
        MarketOffer(source="dns", product_id="", title="Второй", price_rub=200.0),
    ]

    assert len(server._dedupe(offers)) == 2


# ------------------------------------------------------------------- relevance ----
#
# Observed live in July 2026: a query for "iphone 15" returned a 34 224 ₽ offer
# ranked above genuine 52 049 ₽ ones. The tool trusts each marketplace's own
# relevance completely, so an accessory or a different configuration can become
# the headline "cheapest". These warn; they never drop a row, because no
# threshold tuned without live data should be allowed to hide a real bargain.


def test_an_accessory_as_cheapest_is_flagged():
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="Чехол для iPhone 15 силиконовый", price_rub=990.0),
        MarketOffer(source="ozon", product_id="2", title="Apple iPhone 15 128GB", price_rub=52049.0),
    ]

    warnings = server._relevance_warnings("iphone 15", priced)

    assert any(w.startswith("relevance:") for w in warnings), warnings


def test_searching_for_the_accessory_itself_is_not_flagged():
    """Asking for a case and getting cases is the correct answer."""
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="Чехол для iPhone 15", price_rub=990.0),
        MarketOffer(source="ozon", product_id="2", title="Чехол для iPhone 15 кожаный", price_rub=1990.0),
    ]

    assert server._relevance_warnings("чехол для iphone 15", priced) == []


def test_a_genuine_cheapest_is_left_alone():
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="Apple iPhone 15 128GB", price_rub=49900.0),
        MarketOffer(source="ozon", product_id="2", title="Apple iPhone 15 128GB", price_rub=52049.0),
        MarketOffer(source="yandex_market", product_id="3", title="Apple iPhone 15", price_rub=60229.0),
    ]

    assert server._relevance_warnings("iphone 15", priced) == []


def test_a_price_far_below_the_median_is_flagged():
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="iPhone 15 запчасть", price_rub=4000.0),
        MarketOffer(source="ozon", product_id="2", title="Apple iPhone 15 128GB", price_rub=52049.0),
        MarketOffer(source="yandex_market", product_id="3", title="Apple iPhone 15", price_rub=60229.0),
    ]

    warnings = server._relevance_warnings("iphone 15", priced)

    assert any(w.startswith("price_outlier:") for w in warnings), warnings


def test_the_outlier_check_needs_three_offers_to_have_a_median():
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="iPhone 15", price_rub=1000.0),
        MarketOffer(source="ozon", product_id="2", title="iPhone 15", price_rub=52049.0),
    ]

    assert not any(w.startswith("price_outlier:") for w in server._relevance_warnings("iphone 15", priced))


async def test_relevance_warnings_reach_the_response(monkeypatch):
    """The warning is useless if it does not travel with the comparison."""

    async def wb(query, limit):
        return [offer("wildberries", 990.0, title="Чехол для iPhone 15")]

    async def ozon(query, limit):
        return [offer("ozon", 52049.0, title="Apple iPhone 15 128GB")]

    stub_sources(monkeypatch, {"wildberries": wb, "ozon": ozon})

    result = await server.compare_prices(query="iphone 15")

    assert any(w.startswith("relevance:") for w in result.warnings), result.warnings
    # Flagged, not removed: the offer is still ranked and still cheapest.
    assert result.cheapest.price_rub == 990.0


async def test_comparison_exposes_like_for_like_candidate(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 990.0, title="Чехол для iPhone 15")]

    async def ozon(query, limit):
        return [offer("ozon", 52049.0, title="Apple iPhone 15 128GB")]

    stub_sources(monkeypatch, {"wildberries": wb, "ozon": ozon})

    result = await server.compare_prices(query="iphone 15")

    assert result.cheapest.source == "wildberries"
    assert result.cheapest_comparable.source == "ozon"
    assert any(w.startswith("comparable_price:") for w in result.warnings)


async def test_comparable_candidate_is_none_when_everything_is_suspicious(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 990.0, title="Чехол для iPhone 15")]

    stub_sources(monkeypatch, {"wildberries": wb})

    result = await server.compare_prices(query="iphone 15")

    assert result.cheapest_comparable is None
    assert "comparable_price: every priced listing" in result.warnings[-1]


async def test_stock_filter_ranks_only_confirmed_available_offers(monkeypatch):
    async def wb(query, limit):
        return [offer("wildberries", 1000.0, title="Product", in_stock=False)]

    async def ozon(query, limit):
        return [offer("ozon", 1200.0, title="Product", in_stock=True)]

    stub_sources(monkeypatch, {"wildberries": wb, "ozon": ozon})

    result = await server.compare_prices(query="product", in_stock_only=True)

    assert result.cheapest.source == "ozon"
    assert len(result.offers) == 2
    assert any(w.startswith("stock_filter:") for w in result.warnings)


# ------------------------------------------------------------------- condition ----
#
# Checked live on wildberries.ru in July 2026 for the query "iphone 15": the
# cheap rows were "Смартфон iPhone 15, 256ГБ, Восстановленный" at 35 206 ₽ and
# "iPhone 15 256GB Витринный образец" — a refurbished phone and a display unit,
# ranked against new phones from other marketplaces. This is the case the
# accessory list misses: nothing in the title looks like a case, and 34 224 ₽
# against a 52 049 ₽ median is not low enough to trip the outlier check.


def test_a_refurbished_cheapest_is_flagged():
    priced = [
        MarketOffer(
            source="wildberries",
            product_id="1",
            title="Смартфон iPhone 15, 256ГБ, Восстановленный Apple",
            price_rub=34224.0,
        ),
        MarketOffer(source="ozon", product_id="2", title="Apple iPhone 15 128GB", price_rub=52049.0),
        MarketOffer(source="yandex_market", product_id="3", title="Apple iPhone 15", price_rub=60229.0),
    ]

    warnings = server._relevance_warnings("iphone 15", priced)

    assert any(w.startswith("condition:") for w in warnings), warnings
    assert not any(w.startswith("price_outlier:") for w in warnings), (
        "the real case does not trip the median check — that is why the condition check exists"
    )


def test_a_display_unit_is_flagged():
    priced = [
        MarketOffer(
            source="wildberries", product_id="1", title="iPhone 15 256GB Витринный образец esim", price_rub=40000.0
        ),
        MarketOffer(source="ozon", product_id="2", title="Apple iPhone 15 256GB", price_rub=52049.0),
    ]

    assert any(w.startswith("condition:") for w in server._relevance_warnings("iphone 15", priced))


def test_asking_for_a_refurbished_phone_is_not_flagged():
    """Searching for refurbished and getting refurbished is the right answer."""
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="iPhone 15 Восстановленный", price_rub=34224.0),
        MarketOffer(source="ozon", product_id="2", title="iPhone 15 восстановленный", price_rub=39000.0),
    ]

    assert not any(w.startswith("condition:") for w in server._relevance_warnings("iphone 15 восстановленный", priced))


def test_a_new_product_carries_no_condition_warning():
    priced = [
        MarketOffer(source="wildberries", product_id="1", title="Apple iPhone 15 128GB черный", price_rub=49900.0),
        MarketOffer(source="ozon", product_id="2", title="Apple iPhone 15 128GB", price_rub=52049.0),
    ]

    assert server._relevance_warnings("iphone 15", priced) == []


# ------------------------------------------------------------- stop gate ----


async def test_a_refused_source_is_stopped_and_not_called_again(monkeypatch):
    calls = {"ozon": 0}

    async def refused(query, limit):
        calls["ozon"] += 1
        raise_tool_error(ChallengeRequiredError("captcha wall", provider="ozon"))

    async def fine(query, limit):
        return [offer("wildberries", 100.0)]

    stub_sources(monkeypatch, {"ozon": refused, "wildberries": fine})

    first = await server.compare_prices(query="rtx 5070")
    second = await server.compare_prices(query="rtx 5070")

    assert calls["ozon"] == 1
    outcome = next(o for o in second.source_outcomes if o.source == "ozon")
    assert outcome.status == "blocked"
    assert "stopped after a refusal" in outcome.detail
    assert [o.status for o in first.source_outcomes if o.source == "wildberries"] == ["ok"]
    assert [o.status for o in second.source_outcomes if o.source == "wildberries"] == ["ok"]


async def test_a_parse_failure_does_not_stop_the_source(monkeypatch):
    calls = {"ozon": 0}

    async def drifted(query, limit):
        calls["ozon"] += 1
        raise_tool_error(ParserDriftError("shape moved", provider="ozon"))

    stub_sources(monkeypatch, {"ozon": drifted})

    await server.compare_prices(query="rtx 5070")
    await server.compare_prices(query="rtx 5070")

    assert calls["ozon"] == 2


async def test_a_card_for_a_stopped_source_sends_no_request(monkeypatch):
    from mcp_core.stopgate import default_gate

    class FakeCard:
        called = False

        async def __call__(self, **kwargs):
            FakeCard.called = True

    class FakeSource:
        ozon_card = FakeCard()

    monkeypatch.setattr(server, "SOURCES", {"ozon": FakeSource()})
    default_gate().trip("ozon", "403")

    with pytest.raises(ToolError) as caught:
        await server.fetch_card("ozon", "123")

    assert error_payload(caught.value)["source_stopped"] is True
    assert FakeCard.called is False


async def test_a_refused_card_stops_the_source(monkeypatch):
    from mcp_core.stopgate import default_gate

    class FakeCard:
        async def __call__(self, **kwargs):
            raise_tool_error(RateLimitedError("ozon"))

    class FakeSource:
        ozon_card = FakeCard()

    monkeypatch.setattr(server, "SOURCES", {"ozon": FakeSource()})

    with pytest.raises(ToolError):
        await server.fetch_card("ozon", "123")

    assert "ozon" in default_gate().status()
