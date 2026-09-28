"""Regard MCP connector.

Search and cards read the site's own pages over HTTP (curl_cffi as Chrome). Both
are Next.js pages: the data sits in ``<script id="__NEXT_DATA__">`` under
``props.initialState``. Search rows are ``listing.data.bySearch.pages["0"].data``;
a query the site maps to a category is redirected to ``/catalog/<group>/<slug>``
with rows under ``listing.data["<group>"]``. A card is ``product.data``. The city is the site's own (``town.cookieTownId``
named in ``town.townsHits``) and is returned as ``city``.

Verified live 2026-09-27 from a home address: search and card 200 without
cookies, a one-word query answers 301 to its category, a bare ``/product/<id>``
301 to its slug URL, a missing product 410. Any 401/403/418/429, foreign
redirect or anti-bot page stops Regard for 30 minutes.

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

from regard_connector.models_output import (
    MetaOut,
    RegardCardResponse,
    RegardSearchItemOut,
    RegardSearchResponse,
    RegardSelfcheckResponse,
)
from regard_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.regard.ru"
_HOSTS = frozenset({"regard.ru", "www.regard.ru"})
# townsHits: 44 Москва, 137 Санкт-Петербург; without the cookie an HTTP request gets Moscow.
CITY_COOKIES = {"city_id": "137"}
PAGE_SIZE = 24

_CARD_PATH_RE = re.compile(r"^/product/(\d{3,12})(?:/[a-z0-9-]+)?/?$")
_CATEGORY_PATH_RE = re.compile(r"^/catalog/(\d{1,9})(?:/[a-z0-9-]+)?/?$")
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_NEXT_DATA_RE = re.compile(r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LD_JSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_WALL_RE = re.compile(r"captcha|ddos-guard|qrator|servicepipe|доступ ограничен|не робот", re.IGNORECASE)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_GONE = frozenset({404, 410})
_OFFER_STOCK = {
    "https://schema.org/InStock": True,
    "https://schema.org/OutOfStock": False,
    "https://schema.org/SoldOut": False,
    "https://schema.org/Discontinued": False,
}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

mcp = FastMCP(
    name="regard-connector",
    version=SERVER_VERSION,
    instructions=(
        "Regard (computer parts and electronics) over plain HTTP: regard_search reads the site's "
        "search page, regard_card a product page by its URL or id. Prices are for the city the "
        "site shows, returned as city."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_settings.min_gap, source="regard")


def _proxy() -> str | None:
    return (_settings.proxy.get_secret_value() or "").strip() or proxy_from_env("REGARD_PROXY")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _state(html: str) -> dict[str, Any] | None:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (ValueError, RecursionError):
        return None
    state = _as_dict(_as_dict(data).get("props")).get("initialState")
    return state if isinstance(state, dict) else None


def _city(state: dict[str, Any]) -> str | None:
    town = _as_dict(state.get("town"))
    town_id = town.get("cookieTownId")
    for hit in town.get("townsHits") or []:
        if isinstance(hit, dict) and town_id is not None and hit.get("id") == town_id and hit.get("name"):
            return str(hit["name"])
    return None


def _card_url(product_id: str, slug: Any) -> str | None:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        return None
    return f"{SITE_BASE}/product/{product_id}/{slug}"


def _card_target(url_or_id: str) -> tuple[str, str]:
    """The product id and the page to fetch, from a product URL, its path or a bare id."""
    raw = url_or_id.strip()
    if raw.isascii() and raw.isdigit() and 3 <= len(raw) <= 12:
        return raw, f"{SITE_BASE}/product/{raw}"
    if raw.startswith("/"):
        raw = f"{SITE_BASE}{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    match = _CARD_PATH_RE.match(parsed.path)
    if parsed.hostname not in _HOSTS or match is None:
        raise_tool_error(
            BadRequestError(f"not a regard.ru product: {url_or_id!r}; pass /product/<id>/<slug> or the id")
        )
    return match.group(1), f"{SITE_BASE}{parsed.path.rstrip('/')}"


async def _get(url: str, label: str) -> Reply:
    try:
        return await shop_request(
            url,
            pacer=_pacer,
            provider="regard",
            label=label,
            headers=HEADERS,
            cookies=CITY_COOKIES,
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
            f"{label}: {detail}; Regard is stopped for 30 min, do not retry",
            provider="regard",
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


def _page(reply: Reply, label: str) -> tuple[dict[str, Any], str]:
    """``props.initialState`` and the raw page, or the right error."""
    if reply.status in _REDIRECTS:
        _refuse_redirect(label, reply)
    if reply.status in _GONE:
        _pacer.record_success()
        raise_tool_error(NotFoundError(f"{label}: Regard has no such page", provider="regard"))
    if reply.status != 200:
        raise_tool_error(
            TransportDownError(f"{label}: unexpected HTTP {reply.status}", provider="regard", status_code=reply.status)
        )
    html = reply.text or ""
    state = _state(html)
    if state is None:
        if _WALL_RE.search(html):
            _refuse(
                label,
                "anti-bot page",
                "anti-bot page instead of the catalog",
                status=200,
                challenge_type="antibot_page",
            )
        raise_tool_error(ParserDriftError(f"{label}: no __NEXT_DATA__ state on the page", provider="regard"))
    _pacer.record_success()
    return state, html


def _prices(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    price = R.coerce_price(raw.get("price"))
    old = R.coerce_price(raw.get("price_old"))
    return price, old if old is not None and price is not None and old > price else None


def _rating(raw: dict[str, Any]) -> tuple[float | None, int | None]:
    count = R.coerce_int(raw.get("reviews_count"))
    stars = raw.get("reviews_stars")
    if not count or isinstance(stars, bool) or not isinstance(stars, int | float):
        return None, count
    return R.coerce_rating(round(float(stars), 2)), count


def _text(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _product_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("id")
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _search_item(raw: dict[str, Any]) -> RegardSearchItemOut:
    price, old = _prices(raw)
    rating, reviews = _rating(raw)
    product_id = _product_id(raw)
    days = raw.get("max_day")
    known_term = isinstance(days, int) and not isinstance(days, bool) and days >= 0
    return RegardSearchItemOut(
        product_id=product_id,
        title=_text(raw.get("full_title")) or _text(raw.get("title")),
        brand=_text(raw.get("vendor")),
        part_number=_text(raw.get("vendorcode")),
        price_rub=price,
        old_price_rub=old,
        rating=rating,
        review_count=reviews,
        in_stock=True if known_term and raw.get("preorder") == 0 else None,
        url=_card_url(product_id, raw.get("seo_url")) if product_id else None,
    )


async def _search_page(url: str) -> dict[str, Any]:
    """The page state, the listing key it keeps rows under and the category it was sent to."""
    reply = await _get(url, "regard_search")
    key, category = "bySearch", None
    if reply.status in _REDIRECTS:
        target = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, reply.location))
        match = _CATEGORY_PATH_RE.match(target.path) if target.hostname in _HOSTS else None
        if match is None:
            _refuse_redirect("regard_search", reply)
        key, category = match.group(1), target.path
        reply = await _get(f"{SITE_BASE}{target.path}?{target.query}".rstrip("?"), "regard_search")
    state, _ = _page(reply, "regard_search")
    return {"state": state, "key": key, "category": category}


@mcp.tool(
    name="regard_search",
    annotations=ToolAnnotations(
        title="Regard Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def regard_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'rtx 5070'")],
    limit: Annotated[
        int, Field(default=20, ge=1, le=PAGE_SIZE, description="Items to return from the first page.")
    ] = 20,
    ctx: Context | None = None,
) -> RegardSearchResponse:
    """Search Regard the way the site does, first page by relevance.

    A query the site maps to a whole category (e.g. 'видеокарта') is redirected there;
    the connector follows once and says so in the warnings.

    ## Return Format

    RegardSearchResponse: {status, query, tier_used, city, total_available, count, items[],
    meta}. Items carry product_id, title, brand, part_number, price_rub (None when absent —
    never 0), old_price_rub, rating, review_count, in_stock, url.

    ## Error Format

    ToolError: challenge_required on a foreign redirect or anti-bot page, transport_down with
    source_stopped on 401/403/429 (Regard stops for 30 min); parser_drift when the page
    payload moved.
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    url = f"{SITE_BASE}/catalog?search={urllib.parse.quote(text)}"
    log_event("regard_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"regard_search: {text!r}")
    cached = _cache.get(f"search:{text}")
    fetched = cached if isinstance(cached, dict) else await _search_page(url)
    state = fetched["state"]
    listing = _as_dict(_as_dict(_as_dict(state.get("listing")).get("data")).get(fetched["key"]))
    rows = _as_dict(_as_dict(listing.get("pages")).get("0")).get("data")
    if not isinstance(rows, list):
        rows = [] if listing.get("recordsFiltered") == 0 else None
    if rows is None:
        raise_tool_error(
            ParserDriftError(f"regard_search: listing.data.{fetched['key']} has no page 0", provider="regard")
        )
    if cached is None:
        _cache.set(f"search:{text}", fetched)
    warnings: list[str] = []
    items: list[RegardSearchItemOut] = []
    for raw in rows[:limit]:
        if not isinstance(raw, dict):
            continue
        item = _search_item(raw)
        if item.product_id is None or item.url is None:
            warnings.append("row_skipped: a search row came without an id or product link")
            continue
        if item.price_rub is None:
            warnings.append(f"{item.product_id}: no_price")
        items.append(item)
    if fetched["category"]:
        warnings.insert(0, f"category: Regard sent the query to its category {fetched['category']}")
    if not items:
        warnings.insert(0, f"empty: Regard found nothing for {text!r}")
    elif all(it.price_rub is None for it in items):
        warnings.insert(0, "no_prices_on_page: search rows came back without prices")
    log_event("regard_search.done", query=text[:60], returned=len(items))
    result = RegardSearchResponse(
        query=text,
        tier_used="cache" if cached is not None else "http",
        city=_city(state),
        total_available=R.coerce_int(listing.get("recordsFiltered")),
        count=len(items),
        items=items,
    )
    result.meta = MetaOut(source="regard_search", healthy=not warnings, warnings=warnings)
    return result


def _ld_offer(html: str) -> dict[str, Any]:
    for match in _LD_JSON_RE.finditer(html):
        try:
            node = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(node, dict) and node.get("@type") == "Product":
            return _as_dict(node.get("offers"))
    return {}


def _ld_price(offer: dict[str, Any]) -> float | None:
    return R.coerce_price(offer.get("price")) if offer.get("priceCurrency") in (None, "RUB") else None


@mcp.tool(
    name="regard_card",
    annotations=ToolAnnotations(
        title="Regard Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def regard_card(
    url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description="Product URL from regard_search (/product/<id>/<slug>) or the bare product id",
        ),
    ],
    ctx: Context | None = None,
) -> RegardCardResponse:
    """Fetch one Regard product page.

    A bare id costs one extra request: the site redirects it to the full URL.

    ## Return Format

    RegardCardResponse: {status, product_id, title, brand, part_number, price_rub,
    old_price_rub, rating, review_count, is_available, city, url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request for a foreign URL; not_found for a product the site does not
    have; challenge_required on a foreign redirect or anti-bot page; parser_drift when
    the page payload moved.
    """
    log_event("regard_card.start", input=url[:80])
    product_id, page_url = _card_target(url)
    cached = _cache.get(f"card:{product_id}")
    if isinstance(cached, dict):
        fetched, tier = cached, "cache"
    else:
        reply = await _get(page_url, "regard_card")
        if reply.status in _REDIRECTS:
            parsed = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, reply.location))
            match = _CARD_PATH_RE.match(parsed.path) if parsed.hostname in _HOSTS else None
            if match is None or match.group(1) != product_id:
                _refuse_redirect("regard_card", reply)
            page_url = f"{SITE_BASE}{parsed.path.rstrip('/')}"
            reply = await _get(page_url, "regard_card")
        state, html = _page(reply, "regard_card")
        fetched, tier = {"state": state, "offer": _ld_offer(html), "url": page_url}, "http"
    product = _as_dict(_as_dict(fetched["state"].get("product")).get("data"))
    if _product_id(product) != product_id:
        raise_tool_error(ParserDriftError(f"regard_card: no product {product_id} on the page", provider="regard"))
    if tier == "http":
        _cache.set(f"card:{product_id}", fetched)
    price, old = _prices(product)
    rating, reviews = _rating(product)
    offer = fetched["offer"]
    warnings: list[str] = []
    ld_price = _ld_price(offer)
    if price is not None and ld_price is not None and price != ld_price:
        warnings.append(f"price_mismatch: the page data says {price:.0f} ₽, its schema.org offer {ld_price:.0f} ₽")
    is_available = _OFFER_STOCK.get(str(offer.get("availability") or ""))
    if price is None and is_available is not False:
        warnings.insert(0, "no_price: the product page carries no price")
    log_event("regard_card.done", product_id=product_id, price=price, warnings=len(warnings))
    card = RegardCardResponse(
        product_id=product_id,
        title=_text(product.get("full_title")) or _text(product.get("title")),
        brand=_text(product.get("vendor")),
        part_number=_text(product.get("vendorcode")),
        price_rub=price,
        old_price_rub=old,
        rating=rating,
        review_count=reviews,
        is_available=is_available,
        city=_city(fetched["state"]),
        url=_card_url(product_id, product.get("seo_url")) or fetched["url"],
        tier_used=tier,
    )
    card.meta = MetaOut(source="regard_card", healthy=not warnings, warnings=warnings)
    return card


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def regard_selfcheck(ctx: Context | None = None) -> RegardSelfcheckResponse:
    """Drift canary: one search, then the card of its first row (two paced requests).

    A refusal or network fault is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("regard_selfcheck.start")
    try:
        result = await _regard_selfcheck_impl(ctx)
        log_event("regard_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("regard_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"regard_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _regard_selfcheck_impl(ctx: Context | None) -> RegardSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(60):
            found = await regard_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="next-data-search-v1", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="next-data-search-v1", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(
            state, baseline="next-data-search-v1", reason=reason, notes=[str(exc)[:160]]
        )

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="next-data-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await regard_card(first_url, ctx=ctx)
            state = "healthy" if card.price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state,
                baseline="next-data-card-v1",
                reason=None if state == "healthy" else "parse_smoke_failed",
                notes=[f"price {card.price_rub}"],
            )
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(
                state, baseline="next-data-card-v1", reason=reason, notes=[str(exc)[:160]]
            )

    result_dict = R.selfcheck_result(
        "regard",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return RegardSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
