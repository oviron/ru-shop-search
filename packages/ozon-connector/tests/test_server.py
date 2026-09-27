import asyncio
import json
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from mcp_core.stopgate import SourceStoppedError
from ozon_connector import server


def test_server_version_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == server.SERVER_VERSION


@pytest.fixture(autouse=True)
def clear_cache():
    """Composer reads are cached, so scenarios must not inherit each other's bodies."""
    server._cache.clear()
    server._pacer.reset()
    yield
    server._cache.clear()
    server._pacer.reset()


def _run(coro):
    return asyncio.run(coro)


def _reviews_body(*, reviews=None, next_button=None):
    paging = {"total": 5}
    if next_button:
        paging["nextButton"] = next_button
    return json.dumps(
        {
            "widgetStates": {
                "webListReviews-1": json.dumps(
                    {
                        "paging": paging,
                        "reviews": reviews or [],
                    }
                ),
                "webReviewProductScore-1": json.dumps(
                    {
                        "totalScore": 4.7,
                        "reviewsCount": 5,
                        "score": [{"title": "5 звезд", "value": 4}],
                    }
                ),
            },
        }
    )


def test_canonical_composer_path_rejects_unsafe_search_query_keys():
    assert server._canonical_composer_path("/search/?text=abc&page=1") == "/search/?text=abc&page=1"

    for path in (
        "/search/?text=abc&redirect=https://attacker.example",
        "/search/?url=//attacker.example",
        "https://www.ozon.ru/search/?text=abc",
    ):
        try:
            server._canonical_composer_path(path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path accepted: {path}")


def test_parse_search_tile_reads_textds_tile_name():
    item = {
        "sku": 1576326039,
        "action": {"link": "/product/example-1576326039/"},
        "mainState": [
            {"type": "priceV2", "priceV2": {"price": [{"text": "2 890 ₽"}]}},
            {
                "type": "textDS",
                "id": "name",
                "textDS": {
                    "text": "Network filter AGNI 5 sockets",
                    "testInfo": {"automatizationId": "tile-name"},
                },
            },
            {
                "type": "textDS",
                "textDS": {
                    "text": "1577 шт осталось",
                    "testInfo": {"automatizationId": "tile-stock"},
                },
            },
            {
                "type": "labelListV2",
                "labelListV2": {
                    "testInfo": {"automatizationId": "tile-list-rating"},
                    "items": [
                        {"type": "text", "text": {"text": "4.9"}},
                        {"type": "text", "text": {"text": "19189 reviews"}},
                    ],
                },
            },
        ],
    }

    parsed = server._parse_search_tile(item)

    assert parsed["title"] == "Network filter AGNI 5 sockets"
    assert parsed["price"] == "2 890 ₽"
    assert parsed["rating"] == "4.9"
    assert parsed["stock"] == "1577 шт осталось"


def test_parse_search_tile_exposes_canonical_card_input_for_slug_link():
    item = {
        "sku": 1576326039,
        "action": {"link": "/product/example-name-1576326039/"},
        "mainState": [
            {
                "type": "textDS",
                "id": "name",
                "textDS": {
                    "text": "Network filter AGNI 5 sockets",
                    "testInfo": {"automatizationId": "tile-name"},
                },
            },
        ],
    }

    parsed = server._parse_search_tile(item)

    assert parsed["url"] == "https://www.ozon.ru/product/example-name-1576326039/"
    assert parsed["canonical_path"] == "/product/1576326039/"
    assert parsed["card_input"] == "/product/1576326039/"


def test_canonical_product_path_ignores_relative_query_digits():
    canonical, error = server._canonical_product_path_from_input("/product/real-123?ref=-456")

    assert error is None
    assert canonical == "/product/123/"


def test_parse_search_tile_canonicalizes_absolute_ozon_product_link():
    parsed = server._parse_search_tile(
        {
            "sku": 1576326039,
            "action": {"link": "https://m.ozon.ru/product/example-name-1576326039/?from=search"},
            "mainState": [],
        }
    )

    assert parsed["url"] == "https://www.ozon.ru/product/1576326039/"
    assert parsed["canonical_path"] == "/product/1576326039/"
    assert parsed["card_input"] == "/product/1576326039/"


def test_parse_search_tile_omits_unsafe_or_non_product_links():
    for link in (
        "javascript:alert(1)",
        "https://example.com/product/example-name-1576326039/",
        "https://user:pass@www.ozon.ru/product/example-name-1576326039/",
        "/category/electronics/",
    ):
        parsed = server._parse_search_tile(
            {
                "sku": 1576326039,
                "action": {"link": link},
                "mainState": [],
            }
        )

        assert "url" not in parsed
        assert "canonical_path" not in parsed
        assert "card_input" not in parsed


def test_parse_search_tile_ignores_hostile_nested_shapes():
    item = {
        "sku": {"object": "not hashable"},
        "id": ["not", "scalar"],
        "action": {"link": {"not": "a string"}},
        "mainState": [
            "not an atom",
            {
                "type": "priceV2",
                "priceV2": {"price": [{"text": {"rich": "object"}}]},
            },
            {
                "type": "textDS",
                "id": "name",
                "textDS": {
                    "text": "safe title",
                    "testInfo": ["not", "a", "dict"],
                },
            },
            {
                "type": "labelList",
                "labelList": {"items": [{"title": {"rich": "object"}}]},
            },
            {
                "type": "labelListV2",
                "labelListV2": {
                    "testInfo": {"automatizationId": "tile-list-rating"},
                    "items": [{"type": "text", "text": {"text": {"rich": "object"}}}],
                },
            },
        ],
    }

    parsed = server._parse_search_tile(item)

    assert "sku" not in parsed
    assert "url" not in parsed
    assert parsed["title"] == "safe title"
    assert parsed["price"] is None
    assert parsed["rating"] is None
    assert parsed["stock"] is None


def test_parse_search_tile_non_dict_returns_empty():
    assert server._parse_search_tile(["not", "a", "tile"]) == {}


def test_parse_search_tile_non_list_main_state_no_crash():
    parsed = server._parse_search_tile({"sku": 1, "mainState": 123})

    assert parsed["sku"] == 1
    assert parsed["title"] is None


def test_safe_review_page_path_rejects_non_string_next_button():
    assert server._safe_review_page_path("123", {"page": 2}) is None
    assert server._safe_review_page_path("123", ["?page=2"]) is None


def test_cdp_fetch_json_times_out_open_page_and_releases_lock(monkeypatch):
    class HangingOpenPage:
        async def __aenter__(self):
            await asyncio.sleep(10)

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def scenario():
        lock = asyncio.Lock()
        monkeypatch.setattr(server, "_cdp_lock", lock)
        monkeypatch.setattr(server, "TIMEOUT", 0.01)
        monkeypatch.setattr(server, "open_page", lambda *args, **kwargs: HangingOpenPage())

        status, body = await asyncio.wait_for(
            server._cdp_fetch_json("https://www.ozon.ru/api/test", None),
            timeout=0.2,
        )

        assert status == 0
        assert "timeout" in body.lower()
        assert not lock.locked()

    _run(scenario())


def test_ozon_search_reports_actionable_cdp_block(monkeypatch):
    async def blocked_fetch(path, ctx):
        return 403, "navigation blocked: HTTP 403", "cdp_blocked"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", blocked_fetch)

        with pytest.raises(ToolError) as excinfo:
            await server.ozon_search("сетевой фильтр")
        msg = str(excinfo.value)

        assert "transport_down" in msg
        assert "Ozon is stopped, do not retry" in msg
        assert "resume ozon" in msg
        assert "cdp_blocked" in msg
        assert "403" in msg

    _run(scenario())


def test_ozon_search_skips_unhashable_sku_values(monkeypatch):
    widgets = {
        "tileGridDesktop-1": json.dumps(
            {
                "items": [
                    {"sku": [], "mainState": [{"type": "textDS", "id": "name", "textDS": {"text": "Bad list"}}]},
                    {"sku": {}, "mainState": [{"type": "textDS", "id": "name", "textDS": {"text": "Bad dict"}}]},
                    {"sku": False, "mainState": [{"type": "textDS", "id": "name", "textDS": {"text": "Bad bool"}}]},
                    {"sku": 123, "mainState": [{"type": "textDS", "id": "name", "textDS": {"text": "Good int"}}]},
                    {
                        "sku": "123",
                        "mainState": [{"type": "textDS", "id": "name", "textDS": {"text": "Duplicate string"}}],
                    },
                ],
            }
        )
    }

    async def fake_fetch(path, ctx):
        return 200, json.dumps({"widgetStates": widgets}), "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)
        result = await server.ozon_search("x")
        data = result.model_dump()
        assert data["status"] == "success"
        assert data["count"] == 1
        assert data["items"][0]["sku"] == 123
        assert data["items"][0]["title"] == "Good int"

    _run(scenario())


def test_ozon_selfcheck_includes_runtime_identity(monkeypatch):
    search_body = json.dumps(
        {
            "widgetStates": {
                "tileGridDesktop-1": json.dumps(
                    {
                        "items": [
                            {
                                "sku": 123,
                                "mainState": [
                                    {
                                        "type": "textDS",
                                        "id": "name",
                                        "textDS": {"text": "Good filter"},
                                    },
                                ],
                            },
                        ],
                    }
                ),
            },
        }
    )
    card_body = json.dumps(
        {
            "widgetStates": {
                "webPrice-1": json.dumps(
                    {
                        "price": "1 000 ₽",
                        "cardPrice": "900 ₽",
                        "isAvailable": True,
                    }
                ),
                "webReviewProductScore-1": json.dumps(
                    {
                        "totalScore": 4.7,
                        "reviewsCount": 5,
                    }
                ),
            },
        }
    )
    reviews_body = _reviews_body(reviews=[{"content": {"comment": "ok"}}])

    async def fake_fetch(path, ctx):
        if path.startswith("/search/"):
            return 200, search_body, "fake"
        if "/reviews/" in path:
            return 200, reviews_body, "fake"
        return 200, card_body, "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)
        result = await server.ozon_selfcheck()
        data = result.model_dump()
        assert data["status"] == "success"
        assert data["server_version"] == server.SERVER_VERSION
        assert isinstance(data["server_started_at"], str)
        assert data["server_started_at"].endswith("Z")
        datetime.fromisoformat(data["server_started_at"].replace("Z", "+00:00"))
        assert isinstance(data["process_id"], int)
        assert data["process_id"] > 0

    _run(scenario())


def test_card_and_reviews_accept_search_slug_product_url(monkeypatch):
    seen_paths = []

    async def fake_fetch(path, ctx):
        seen_paths.append(path)
        if path.endswith("/reviews/"):
            return 200, _reviews_body(), "fake"
        return 200, json.dumps({"widgetStates": {}}), "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)

        card = (await server.ozon_card("https://www.ozon.ru/product/example-name-1576326039/")).model_dump()
        reviews = (
            await server.ozon_reviews(
                "https://www.ozon.ru/product/example-name-1576326039/",
                limit=1,
            )
        ).model_dump()

        assert card["status"] == "success"
        assert card["url"] == "https://www.ozon.ru/product/1576326039/"
        assert reviews["status"] == "success"
        assert reviews["url"] == "https://www.ozon.ru/product/1576326039/reviews/"
        assert seen_paths == ["/product/1576326039/", "/product/1576326039/reviews/"]

    _run(scenario())


def test_ozon_tools_reject_non_object_payloads(monkeypatch):
    async def fake_fetch(path, ctx):
        return 200, "[]", "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)

        for call in (
            server.ozon_card("123"),
            server.ozon_reviews("123", limit=1),
            server.ozon_search("сетевой фильтр"),
        ):
            with pytest.raises(ToolError) as excinfo:
                await call
            assert "parser_drift" in str(excinfo.value)

    _run(scenario())


def test_reviews_marks_partial_when_later_page_fails(monkeypatch):
    calls = []
    first_page_review = {
        "uuid": "r1",
        "content": {"score": 5, "comment": "good"},
        "author": {"firstName": "Ann"},
        "usefulness": {"useful": 1},
        "publishedAt": 0,
    }

    async def fake_fetch(path, ctx):
        calls.append(path)
        if len(calls) == 1:
            return (
                200,
                _reviews_body(
                    reviews=[first_page_review],
                    next_button="?page=2&page_key=abc&sort=published_at_desc",
                ),
                "fake",
            )
        return 500, "upstream failed", "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)

        result = await server.ozon_reviews("123", limit=5)
        data = result.model_dump()

        assert data["status"] == "success"
        assert data["returned"] == 1
        assert data["partial"] is True
        assert data["requested_limit"] == 5
        assert data["stop_reason"] == "http"
        assert data["last_error"]["code"] == 500

    _run(scenario())


def test_reviews_tag_each_item_with_the_variant_it_describes(monkeypatch):
    """A pooled review must be attributable to the SKU it is actually about.

    Ozon serves one review pool per card family, and the neighbours are often
    other brands entirely — a live card for a 1500 W Huter returned 100 reviews
    of which zero were about the Huter. Without item_id a caller reads those as
    the product's own and repeats a rating that belongs to someone else.
    """
    body = json.dumps(
        {
            "widgetStates": {
                "webListReviews-1": json.dumps(
                    {
                        "paging": {"total": 3},
                        "products": {
                            "123": {"name": "Asked-for product"},
                            "456": {"name": "Neighbour in the same pool"},
                        },
                        "reviews": [
                            {"uuid": "a", "itemId": 123, "content": {"score": 5, "comment": "mine"}},
                            {"uuid": "b", "itemId": 456, "content": {"score": 1, "comment": "not mine"}},
                            {"uuid": "c", "content": {"score": 4, "comment": "no item id"}},
                        ],
                    }
                ),
                "webReviewProductScore-1": json.dumps({"totalScore": 4.7, "reviewsCount": 3, "score": []}),
            },
        }
    )

    async def fake_fetch(path, ctx):
        return 200, body, "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)
        data = (await server.ozon_reviews("123", limit=5)).model_dump()

        assert data["requested_item_id"] == 123
        assert [r["item_id"] for r in data["reviews"]] == [123, 456, None]
        assert data["own_reviews"] == 1
        assert data["pool_variants"] == {
            "123": "Asked-for product",
            "456": "Neighbour in the same pool",
        }

    _run(scenario())


def test_reviews_report_zero_own_reviews_when_the_pool_is_all_neighbours(monkeypatch):
    """own_reviews=0 is the honest answer, not an error and not an empty result."""
    body = json.dumps(
        {
            "widgetStates": {
                "webListReviews-1": json.dumps(
                    {
                        "paging": {"total": 2},
                        "products": {"456": {"name": "Neighbour"}},
                        "reviews": [
                            {"uuid": "a", "itemId": 456, "content": {"score": 5, "comment": "theirs"}},
                            {"uuid": "b", "itemId": 456, "content": {"score": 5, "comment": "theirs too"}},
                        ],
                    }
                ),
                "webReviewProductScore-1": json.dumps({"totalScore": 4.8, "reviewsCount": 2, "score": []}),
            },
        }
    )

    async def fake_fetch(path, ctx):
        return 200, body, "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)
        data = (await server.ozon_reviews("123", limit=5)).model_dump()

        assert data["status"] == "success"
        assert data["returned"] == 2
        assert data["own_reviews"] == 0
        assert data["rating_score"] == 4.8  # pool-wide, and now visibly so

    _run(scenario())


def test_reviews_tolerate_drifted_item_id_and_products_shapes(monkeypatch):
    """A drifted itemId/products must degrade to None/{}, never crash the tool."""
    body = json.dumps(
        {
            "widgetStates": {
                "webListReviews-1": json.dumps(
                    {
                        "paging": {"total": 2},
                        "products": ["not", "a", "mapping"],
                        "reviews": [
                            {"uuid": "a", "itemId": "123", "content": {"score": 5, "comment": "string id"}},
                            {"uuid": "b", "itemId": {"bad": "shape"}, "content": {"score": 5, "comment": "dict id"}},
                        ],
                    }
                ),
                "webReviewProductScore-1": json.dumps({"totalScore": 4.7, "reviewsCount": 2, "score": []}),
            },
        }
    )

    async def fake_fetch(path, ctx):
        return 200, body, "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)
        data = (await server.ozon_reviews("123", limit=5)).model_dump()

        assert data["returned"] == 2
        assert [r["item_id"] for r in data["reviews"]] == [None, None]
        assert data["own_reviews"] == 0
        assert data["pool_variants"] == {}

    _run(scenario())


def test_reviews_tolerates_malformed_uuid_next_button_and_score(monkeypatch):
    body = json.dumps(
        {
            "widgetStates": {
                "webListReviews-1": json.dumps(
                    {
                        "paging": {"total": 1, "nextButton": {"bad": "shape"}},
                        "reviews": [
                            {"uuid": ["not-hashable"], "content": {"score": 5, "comment": "ok"}},
                        ],
                    }
                ),
                "webReviewProductScore-1": json.dumps(
                    {
                        "totalScore": 4.7,
                        "reviewsCount": 1,
                        "score": 1,
                    }
                ),
            },
        }
    )

    async def fake_fetch(path, ctx):
        return 200, body, "fake"

    async def scenario():
        monkeypatch.setattr(server, "_fetch_composer", fake_fetch)
        result = await server.ozon_reviews("123", limit=5)
        data = result.model_dump()
        assert data["status"] == "success"
        assert data["returned"] == 1
        assert data["partial"] is True
        assert data["stop_reason"] == "invalid_next_button"
        assert data["distribution"] == {}

    _run(scenario())


def _patch_tab(monkeypatch, impl):
    """Route the tab fetch through ``impl``; returns the call counter."""
    calls = {"n": 0}

    async def fake_cdp(api_url, ctx):
        calls["n"] += 1
        result = impl(calls["n"])
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(server, "_min_gap", 0)
    monkeypatch.setattr(server, "_cdp_fetch_json", fake_cdp)
    return calls


def test_fetch_composer_reads_through_the_tab_only(monkeypatch):
    calls = _patch_tab(monkeypatch, lambda n: (200, '{"widgetStates": {}}'))

    status, _, tier = _run(server._fetch_composer("/product/123/", None))

    assert (status, tier, calls["n"]) == (200, "cdp", 1)


def test_fetch_composer_serves_a_repeat_read_from_cache(monkeypatch):
    """A cache hit skips a whole browser round-trip."""
    calls = _patch_tab(monkeypatch, lambda n: (200, '{"widgetStates": {}}'))

    async def scenario():
        await server._fetch_composer("/product/123/", None)
        return await server._fetch_composer("/product/123/", None)

    assert _run(scenario())[2] == "cache"
    assert calls["n"] == 1


def test_cache_is_keyed_by_canonical_path_not_raw_input(monkeypatch):
    """Two spellings of the same product must share one cache entry."""
    calls = _patch_tab(monkeypatch, lambda n: (200, '{"widgetStates": {}}'))

    async def scenario():
        await server._fetch_composer("/product/123/", None)
        return await server._fetch_composer("/product/123", None)

    assert _run(scenario())[2] == "cache"
    assert calls["n"] == 1


def test_fetch_composer_reports_a_navigation_block(monkeypatch):
    _patch_tab(monkeypatch, lambda n: server.NavBlocked(403, "https://www.ozon.ru/"))

    status, body, tier = _run(server._fetch_composer("/product/123/", None))

    assert (status, tier) == (403, "cdp_blocked")
    assert "navigation blocked" in body


@pytest.mark.parametrize(
    "refusal",
    [(403, "<html>Antibot Challenge Page</html>"), (429, "slow down"), server.NavBlocked(403, "https://www.ozon.ru/")],
)
def test_a_refusal_stops_ozon_and_is_not_cached(monkeypatch, refusal):
    calls = _patch_tab(monkeypatch, lambda n: refusal)

    async def scenario():
        await server._fetch_composer("/product/123/", None)
        with pytest.raises(SourceStoppedError):
            await server._fetch_composer("/product/123/", None)

    _run(scenario())
    assert calls["n"] == 1


def test_a_dead_browser_does_not_stop_ozon(monkeypatch):
    calls = _patch_tab(monkeypatch, lambda n: (0, "CDP timeout after 20s"))

    async def scenario():
        await server._fetch_composer("/product/123/", None)
        await server._fetch_composer("/product/123/", None)

    _run(scenario())
    assert calls["n"] == 2
