"""VseInstrumenti MCP connector.

Search and cards read the site's own pages over HTTP (curl_cffi as Chrome). Both
are Nuxt 3 pages: the data sits in ``<script id="__NUXT_DATA__">`` as a devalue
array, a flat list where objects point at other entries by index. Search rows are
``pinia.ListingStore.products``; a card is the object with ``pricesV2`` under one
of the ``data`` keys. The city is the site's own choice by IP
(``state.$sactiveRegion``) and is returned as ``city``.

Verified live 2026-09-27 from a home address: search and card 200 without
cookies, a bare ``/product/<id>/`` answers 404, a one-word query answers 302 to
its category. After five cookieless requests in seven minutes ServicePipe answered
403 with a JS check; any 401/403/418/429, foreign redirect or anti-bot page stops
VseInstrumenti for 30 minutes.

NEVER write to stdout in a stdio MCP server — it corrupts JSON-RPC.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import urllib.parse
from typing import Annotated, Any, NoReturn

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from mcp_core import resilience as R
from mcp_core.cache import TTLCache
from mcp_core.errors import (
    BadRequestError,
    ChallengeRequiredError,
    ConnectorError,
    NotFoundError,
    ParserDriftError,
    ToolError,
    TransportDownError,
    raise_tool_error,
)
from mcp_core.logging import log_event
from mcp_core.output_schema import apply_compact_output_schemas
from mcp_core.pacing import Pacer
from mcp_core.redact import redact_error_text as _redact
from mcp_core.transport import proxy_from_env
from mcp_core.transport.impersonated import Reply, shop_request
from pydantic import Field

from vseinstrumenti_connector.models_output import (
    MetaOut,
    VseInstrumentiCardResponse,
    VseInstrumentiSearchItemOut,
    VseInstrumentiSearchResponse,
    VseInstrumentiSelfcheckResponse,
)
from vseinstrumenti_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.vseinstrumenti.ru"
_HOSTS = frozenset({"vseinstrumenti.ru", "www.vseinstrumenti.ru"})

_CARD_PATH_RE = re.compile(r"^/product/[a-z0-9-]*?-(\d{3,12})/?$")
_CATEGORY_PATH_RE = re.compile(r"^/category/[a-z0-9-]+/?$")
_NUXT_DATA_RE = re.compile(r'<script[^>]*\bid="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LD_JSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_WALL_RE = re.compile(r"servicepipe|captcha|ddos-guard|qrator|доступ ограничен|не робот", re.IGNORECASE)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# devalue wraps Vue refs and reactive objects in a one-argument tag around the real value.
_WRAPPERS = frozenset({"Reactive", "ShallowReactive", "Ref", "ShallowRef", "JSONifiable"})

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

mcp = FastMCP(
    name="vseinstrumenti-connector",
    version=SERVER_VERSION,
    instructions=(
        "VseInstrumenti (tools and equipment) over plain HTTP: vseinstrumenti_search reads the "
        "site's search page, vseinstrumenti_card a product page by its URL. Prices are for the "
        "city the site picks by IP, returned as city."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_settings.min_gap, source="vseinstrumenti")


def _proxy() -> str | None:
    return (_settings.proxy.get_secret_value() or "").strip() or proxy_from_env("VSEINSTRUMENTI_PROXY")


def _unflatten(values: list[Any]) -> Any:
    """Rebuild a devalue array; unknown tags and negative (special) indices become None."""
    done: dict[int, Any] = {}

    def hydrate(ref: Any) -> Any:
        if isinstance(ref, bool) or not isinstance(ref, int) or not 0 <= ref < len(values):
            return None
        if ref in done:
            return done[ref]
        value = values[ref]
        if isinstance(value, dict):
            obj: dict[str, Any] = {}
            done[ref] = obj
            obj.update((key, hydrate(item)) for key, item in value.items())
            return obj
        if isinstance(value, list) and value and isinstance(value[0], str):
            done[ref] = None
            tag = value[0]
            if tag in _WRAPPERS and len(value) == 2:
                done[ref] = hydrate(value[1])
            elif tag == "Set":
                done[ref] = [hydrate(item) for item in value[1:]]
            elif tag == "null":
                done[ref] = {str(value[i]): hydrate(value[i + 1]) for i in range(1, len(value) - 1, 2)}
            return done[ref]
        if isinstance(value, list):
            items: list[Any] = []
            done[ref] = items
            items.extend(hydrate(item) for item in value)
            return items
        done[ref] = value
        return value

    return hydrate(0)


def _nuxt_data(html: str) -> dict[str, Any] | None:
    match = _NUXT_DATA_RE.search(html)
    if match is None:
        return None
    try:
        values = json.loads(match.group(1))
        data = _unflatten(values) if isinstance(values, list) else None
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _city(page: dict[str, Any]) -> str | None:
    name = _as_dict(_as_dict(page.get("state")).get("$sactiveRegion")).get("name")
    return str(name) if name else None


def _card_target(url_or_path: str) -> tuple[str, str]:
    """The product id and the page to fetch, from a product URL or its path."""
    raw = url_or_path.strip()
    if raw.isdigit():
        raise_tool_error(
            BadRequestError(
                f"a bare id {raw!r} does not open on VseInstrumenti; pass the product URL from vseinstrumenti_search"
            )
        )
    if raw.startswith("/"):
        raw = f"{SITE_BASE}{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    match = _CARD_PATH_RE.match(parsed.path)
    if parsed.hostname not in _HOSTS or match is None:
        raise_tool_error(
            BadRequestError(f"not a vseinstrumenti.ru product URL: {url_or_path!r}; pass /product/<slug>-<id>/")
        )
    path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    return match.group(1), f"{SITE_BASE}{path}"


async def _get(url: str, label: str) -> Reply:
    try:
        return await shop_request(
            url,
            pacer=_pacer,
            provider="vseinstrumenti",
            label=label,
            headers=HEADERS,
            timeout_s=_settings.timeout,
            max_bytes=_settings.max_body_bytes,
            retries=1,
            proxy=_proxy(),
        )
    except ConnectorError as exc:
        raise_tool_error(exc)


def _refuse(label: str, reason: str, detail: str, *, status: int, challenge_type: str) -> NoReturn:
    _pacer.record_refusal(reason)
    raise_tool_error(
        ChallengeRequiredError(
            f"{label}: {detail}; VseInstrumenti is stopped for 30 min, do not retry",
            provider="vseinstrumenti",
            challenge_type=challenge_type,
            status_code=status,
        )
    )


def _refuse_redirect(label: str, reply: Reply) -> NoReturn:
    _refuse(
        label,
        f"http {reply.status}",
        f"redirect to {reply.location[:80]!r} instead of the page",
        status=reply.status,
        challenge_type="redirect",
    )


def _same_site_path(location: str) -> str | None:
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, location))
    return parsed.path if parsed.hostname in _HOSTS else None


def _page(reply: Reply, label: str) -> tuple[dict[str, Any], str]:
    """The decoded ``__NUXT_DATA__`` and the raw page, or the right error."""
    if reply.status in _REDIRECTS:
        _refuse_redirect(label, reply)
    if reply.status == 404:
        _pacer.record_success()
        raise_tool_error(NotFoundError(f"{label}: VseInstrumenti has no such page", provider="vseinstrumenti"))
    if reply.status != 200:
        raise_tool_error(
            TransportDownError(
                f"{label}: unexpected HTTP {reply.status}", provider="vseinstrumenti", status_code=reply.status
            )
        )
    html = reply.text or ""
    data = _nuxt_data(html)
    if data is None:
        if _WALL_RE.search(html):
            _refuse(
                label,
                "anti-bot page",
                "anti-bot page instead of the catalog",
                status=200,
                challenge_type="antibot_page",
            )
        raise_tool_error(ParserDriftError(f"{label}: no __NUXT_DATA__ on the page", provider="vseinstrumenti"))
    _pacer.record_success()
    return data, html


def _prices(raw: Any) -> tuple[float | None, float | None]:
    """``(price, crossed-out price)`` from ``pricesV2``; B2B discount prices are ignored."""
    prices = _as_dict(raw)
    if isinstance(prices.get("default"), dict):
        prices = prices["default"]
    price = R.coerce_price(prices.get("current"))
    old = R.coerce_price(prices.get("old"))
    return price, old if old is not None and price is not None and old > price else None


def _rating(responses: Any) -> tuple[float | None, int | None]:
    block = _as_dict(responses)
    count = R.coerce_int(block.get("amount"))
    average = block.get("averageRatio")
    if not count or not isinstance(average, int | float):
        return None, count
    return R.coerce_rating(round(float(average), 2)), count


def _search_item(raw: dict[str, Any]) -> VseInstrumentiSearchItemOut:
    price, old = _prices(raw.get("pricesV2"))
    rating, reviews = _rating(raw.get("responses"))
    link = str(raw.get("link") or "")
    available = raw.get("isAvailable")
    product_id = raw.get("id")
    return VseInstrumentiSearchItemOut(
        product_id=str(product_id) if isinstance(product_id, int | str) and str(product_id) else None,
        title=str(raw.get("name") or "").strip() or None,
        price_rub=price,
        old_price_rub=old,
        rating=rating,
        review_count=reviews,
        in_stock=available if isinstance(available, bool) else None,
        url=f"{SITE_BASE}{link}" if _CARD_PATH_RE.match(link) else None,
    )


async def _search_page(url: str) -> dict[str, Any]:
    reply = await _get(url, "vseinstrumenti_search")
    category: str | None = None
    if reply.status in _REDIRECTS:
        category = _same_site_path(reply.location)
        if category is None or not _CATEGORY_PATH_RE.match(category):
            _refuse_redirect("vseinstrumenti_search", reply)
        reply = await _get(f"{SITE_BASE}{category}", "vseinstrumenti_search")
    data, _ = _page(reply, "vseinstrumenti_search")
    return {"page": data, "category": category}


@mcp.tool(
    name="vseinstrumenti_search",
    annotations=ToolAnnotations(
        title="VseInstrumenti Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def vseinstrumenti_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'перфоратор makita'")],
    limit: Annotated[int, Field(default=20, ge=1, le=40, description="Items to return from the first page.")] = 20,
    ctx: Context | None = None,
) -> VseInstrumentiSearchResponse:
    """Search VseInstrumenti the way the site does, first page by relevance.

    A query the site maps to a whole category (e.g. 'перфоратор') is redirected there;
    the connector follows once and says so in the warnings.

    ## Return Format

    VseInstrumentiSearchResponse: {status, query, tier_used, city, total_available, count,
    items[], meta}. Items carry product_id, title, price_rub (None when absent — never 0),
    old_price_rub, rating, review_count, in_stock, url.

    ## Error Format

    ToolError: challenge_required on a foreign redirect or anti-bot page, transport_down
    with source_stopped on 401/403/429 (VseInstrumenti stops for 30 min); parser_drift
    when the page payload moved.
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    url = f"{SITE_BASE}/search/?what={urllib.parse.quote(text)}"
    log_event("vseinstrumenti_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"vseinstrumenti_search: {text!r}")
    cached = _cache.get(f"search:{text}")
    fetched = cached if isinstance(cached, dict) else await _search_page(url)
    page = fetched["page"]
    listing = _as_dict(_as_dict(page.get("pinia")).get("ListingStore"))
    products = listing.get("products")
    rows = list(products.values()) if isinstance(products, dict) else products
    if not isinstance(rows, list):
        raise_tool_error(
            ParserDriftError("vseinstrumenti_search: ListingStore.products is missing", provider="vseinstrumenti")
        )
    if cached is None:
        _cache.set(f"search:{text}", fetched)
    warnings: list[str] = []
    items: list[VseInstrumentiSearchItemOut] = []
    for raw in rows[:limit]:
        if not isinstance(raw, dict):
            continue
        item = _search_item(raw)
        if item.product_id is None or item.url is None:
            warnings.append("row_skipped: a search row came without an id or product link")
            continue
        if item.price_rub is None and item.in_stock is not False:
            warnings.append(f"{item.product_id}: no_price")
        items.append(item)
    if fetched["category"]:
        warnings.insert(0, f"category: VseInstrumenti sent the query to its category {fetched['category']}")
    if not items:
        warnings.insert(0, f"empty: VseInstrumenti found nothing for {text!r}")
    elif all(it.price_rub is None for it in items):
        warnings.insert(0, "no_prices_on_page: search rows came back without prices")
    log_event("vseinstrumenti_search.done", query=text[:60], returned=len(items))
    result = VseInstrumentiSearchResponse(
        query=text,
        tier_used="cache" if cached is not None else "http",
        city=_city(page),
        total_available=R.coerce_int(listing.get("productsCount")),
        count=len(items),
        items=items,
    )
    result.meta = MetaOut(source="vseinstrumenti_search", healthy=not warnings, warnings=warnings)
    return result


def _card_product(page: dict[str, Any], product_id: str) -> dict[str, Any] | None:
    for block in _as_dict(page.get("data")).values():
        info = _as_dict(block).get("info")
        if isinstance(info, dict) and "pricesV2" in info and str(info.get("id")) == product_id:
            return info
    return None


def _ld_offer_price(html: str) -> float | None:
    for match in _LD_JSON_RE.finditer(html):
        try:
            node = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(node, dict) and node.get("@type") == "Product":
            offer = _as_dict(node.get("offers"))
            return R.coerce_price(offer.get("price")) if offer.get("priceCurrency") in (None, "RUB") else None
    return None


@mcp.tool(
    name="vseinstrumenti_card",
    annotations=ToolAnnotations(
        title="VseInstrumenti Product Card",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def vseinstrumenti_card(
    url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description="Product URL from vseinstrumenti_search (/product/<slug>-<id>/); a bare id does not open",
        ),
    ],
    ctx: Context | None = None,
) -> VseInstrumentiCardResponse:
    """Fetch one VseInstrumenti product page.

    ## Return Format

    VseInstrumentiCardResponse: {status, product_id, title, brand, price_rub, old_price_rub,
    rating, review_count, is_available, city, url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request for a bare id or a foreign URL; not_found for a page the site
    does not have; challenge_required on a foreign redirect or anti-bot page;
    parser_drift when the page payload moved.
    """
    log_event("vseinstrumenti_card.start", input=url[:80])
    product_id, page_url = _card_target(url)
    cached = _cache.get(f"card:{product_id}")
    if isinstance(cached, dict):
        fetched, tier = cached, "cache"
    else:
        reply = await _get(page_url, "vseinstrumenti_card")
        if reply.status in _REDIRECTS:
            path = _same_site_path(reply.location) or ""
            match = _CARD_PATH_RE.match(path)
            if match is None or match.group(1) != product_id:
                _refuse_redirect("vseinstrumenti_card", reply)
            page_url = f"{SITE_BASE}{path}"
            reply = await _get(page_url, "vseinstrumenti_card")
        page, html = _page(reply, "vseinstrumenti_card")
        fetched, tier = {"page": page, "ld_price": _ld_offer_price(html), "url": page_url}, "http"
    product = _card_product(fetched["page"], product_id)
    if product is None:
        raise_tool_error(
            ParserDriftError(f"vseinstrumenti_card: no product {product_id} on the page", provider="vseinstrumenti")
        )
    if tier == "http":
        _cache.set(f"card:{product_id}", fetched)
    price, old = _prices(product.get("pricesV2"))
    rating, reviews = _rating(product.get("responses"))
    warnings: list[str] = []
    ld_price = fetched["ld_price"]
    if price is not None and ld_price is not None and price != ld_price:
        warnings.append(f"price_mismatch: the page data says {price:.0f} ₽, its schema.org offer {ld_price:.0f} ₽")
    available = product.get("isAvailable")
    is_available = available if isinstance(available, bool) else None
    if product.get("productWithdrawnFromSale") is True:
        is_available = False
    if price is None and is_available is not False:
        warnings.insert(0, "no_price: the product page carries no price")
    log_event("vseinstrumenti_card.done", product_id=product_id, price=price, warnings=len(warnings))
    card = VseInstrumentiCardResponse(
        product_id=product_id,
        title=str(product.get("name") or "").strip() or None,
        brand=_as_dict(product.get("brand")).get("name"),
        price_rub=price,
        old_price_rub=old,
        rating=rating,
        review_count=reviews,
        is_available=is_available,
        city=_city(fetched["page"]),
        url=fetched["url"],
        tier_used=tier,
    )
    card.meta = MetaOut(source="vseinstrumenti_card", healthy=not warnings, warnings=warnings)
    return card


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def vseinstrumenti_selfcheck(ctx: Context | None = None) -> VseInstrumentiSelfcheckResponse:
    """Drift canary: one search, then the card of its first row (two paced requests).

    A refusal or network fault is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("vseinstrumenti_selfcheck.start")
    try:
        result = await _vseinstrumenti_selfcheck_impl(ctx)
        log_event("vseinstrumenti_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("vseinstrumenti_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"vseinstrumenti_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _vseinstrumenti_selfcheck_impl(ctx: Context | None) -> VseInstrumentiSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(60):
            found = await vseinstrumenti_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="nuxt-data-search-v1", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="nuxt-data-search-v1", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(
            state, baseline="nuxt-data-search-v1", reason=reason, notes=[str(exc)[:160]]
        )

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="nuxt-data-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await vseinstrumenti_card(first_url, ctx=ctx)
            state = "healthy" if card.price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state,
                baseline="nuxt-data-card-v1",
                reason=None if state == "healthy" else "parse_smoke_failed",
                notes=[f"price {card.price_rub}"],
            )
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(
                state, baseline="nuxt-data-card-v1", reason=reason, notes=[str(exc)[:160]]
            )

    result_dict = R.selfcheck_result(
        "vseinstrumenti",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return VseInstrumentiSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
