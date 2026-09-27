"""Holodilnik (holodilnik.ru) MCP connector: search and product pages over HTTP.

Both are server-rendered windows-1251 pages read with curl_cffi as Chrome. Search
rows are ``<div class="catalog-item">`` blocks: link, title, price, crossed-out
price, rating, buy button or «Нет в наличии». The row id is the site's article
number («артикул», ``data-icon-link-favorite``), the same number the card carries
as schema.org ``sku``. Cards are read from schema.org microdata. Badges such as
«Скидка 10%» (an app promo code) and «Списывайте бонусы» are not prices.

Verified live 2026-09-27 from a home address: search and card 200 without cookies,
city by IP (Санкт-Петербург), a nonsense query still lists one unrelated product,
a missing product 301 to its category. Any 401/403/418/429, foreign redirect or
anti-bot page stops Holodilnik for 30 minutes.

NEVER write to stdout in a stdio MCP server — it corrupts JSON-RPC.
"""

from __future__ import annotations

import asyncio
import datetime
import html as htmllib
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
from mcp_core.query_match import names_query
from mcp_core.redact import redact_error_text as _redact
from mcp_core.transport import proxy_from_env
from mcp_core.transport.impersonated import Reply, shop_request
from pydantic import Field

from holodilnik_connector.models_output import (
    HolodilnikCardResponse,
    HolodilnikSearchItemOut,
    HolodilnikSearchResponse,
    HolodilnikSelfcheckResponse,
    MetaOut,
)
from holodilnik_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.holodilnik.ru"
_HOSTS = frozenset({"holodilnik.ru", "www.holodilnik.ru"})
PAGE_SIZE = 24

_CARD_PATH_RE = re.compile(r"^/[a-z0-9_-]+/[a-z0-9_-]+/[a-z0-9_-]+/[a-z0-9_.-]+/$")
_BLOCK_START = '<div class="catalog-item">'
_ROW_LINK_RE = re.compile(r'<div class="catalog-item__name">\s*<a href="([^"]+)"')
_ROW_TITLE_RE = re.compile(r'<span class="catalog-item__title">([^<]*)</span>')
_ROW_ID_RE = re.compile(r"data-icon-link-favorite=['\"](\d{1,12})['\"]")
_ROW_PRICE_RE = re.compile(r'<span class="price-value__actual">([^<]*)</span>')
_ROW_OLD_RE = re.compile(r'<s class="price-value__old">([^<]*)</s>')
_CARD_PRICES_RE = re.compile(
    r'<div class="product-price__values">\s*<div class="product-price__current">([^<]*)</div>'
    r'\s*(?:<div class="product-price__old">([^<]*)</div>)?'
)
_ROW_RATING_RE = re.compile(r'class="rating-value"[^>]*>(.*?)</a>', re.S)
_ROW_TERM_RE = re.compile(r'<div class="catalog-item__add-info-delivery-item">(.*?)</div>', re.S)
_RATING_TEXT_RE = re.compile(r"^([\d.]+)\s*•\s*(\d+)")
_TOTAL_RE = re.compile(r"Найдено товаров:\s*<b>\s*(\d+)\s*</b>")
_CITY_RE = re.compile(r'data-region="current-region"[^>]*>([^<]*)<')
_PRODUCT_RE = re.compile(r'itemtype="https?://schema\.org/Product"')
_OFFER_RE = re.compile(r'itemtype="https?://schema\.org/Offer"')
_SCOPE_SPAN = 4000
_TAG_RE = re.compile(r"<[^>]+>")
_WALL_RE = re.compile(r"captcha|ddos-guard|qrator|servicepipe|доступ ограничен|не робот", re.IGNORECASE)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_GONE = frozenset({404, 410})
_OFFER_STOCK = {"InStock": True, "OutOfStock": False, "SoldOut": False, "Discontinued": False}
_OUT_OF_STOCK = "Нет в наличии"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

mcp = FastMCP(
    name="holodilnik-connector",
    version=SERVER_VERSION,
    instructions=(
        "Holodilnik (holodilnik.ru, home appliances) over plain HTTP: holodilnik_search reads the "
        "site's search page, holodilnik_card a product page by its URL. Prices are for the city "
        "the site picks by IP, returned as city."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_settings.min_gap, source="holodilnik")


def _proxy() -> str | None:
    return (_settings.proxy.get_secret_value() or "").strip() or proxy_from_env("HOLODILNIK_PROXY")


def _text(raw: str | None) -> str | None:
    if raw is None:
        return None
    return " ".join(htmllib.unescape(_TAG_RE.sub("", raw)).split()) or None


def _price(raw: str | None) -> float | None:
    return R.coerce_price(htmllib.unescape(raw)) if raw else None


def _card_path(url: str) -> str | None:
    """The canonical product URL, or None when ``url`` is not a holodilnik.ru product page."""
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, url))
    path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _HOSTS or not _CARD_PATH_RE.match(path):
        return None
    return f"{SITE_BASE}{path}"


async def _get(url: str, label: str) -> Reply:
    try:
        return await shop_request(
            url,
            pacer=_pacer,
            provider="holodilnik",
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
            f"{label}: {detail}; Holodilnik is stopped for 30 min, do not retry",
            provider="holodilnik",
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


def _page_text(reply: Reply, label: str) -> str:
    if reply.status in _GONE:
        _pacer.record_success()
        raise_tool_error(NotFoundError(f"{label}: Holodilnik has no such page", provider="holodilnik"))
    if reply.status != 200:
        raise_tool_error(
            TransportDownError(
                f"{label}: unexpected HTTP {reply.status}", provider="holodilnik", status_code=reply.status
            )
        )
    return reply.text or ""


def _city(page: str) -> str | None:
    match = _CITY_RE.search(page)
    return _text(match.group(1)) if match else None


def _rating(block: str) -> tuple[float | None, int | None]:
    match = _ROW_RATING_RE.search(block)
    found = _RATING_TEXT_RE.match(_text(match.group(1)) or "") if match else None
    if found is None:
        return None, None
    return R.coerce_rating(found.group(1)), R.coerce_int(found.group(2))


def _search_item(block: str) -> HolodilnikSearchItemOut:
    link = _ROW_LINK_RE.search(block)
    title = _ROW_TITLE_RE.search(block)
    sku = _ROW_ID_RE.search(block)
    price_match = _ROW_PRICE_RE.search(block)
    old_match = _ROW_OLD_RE.search(block)
    price = _price(price_match.group(1)) if price_match else None
    old = _price(old_match.group(1)) if old_match else None
    rating, reviews = _rating(block)
    in_stock: bool | None
    term: str | None
    if _OUT_OF_STOCK in block:
        in_stock, term = False, _OUT_OF_STOCK
    else:
        term_match = _ROW_TERM_RE.search(block)
        term = _text(term_match.group(1)) if term_match else None
        in_stock = True if 'data-smoke="AddToCartListing"' in block else None
    return HolodilnikSearchItemOut(
        product_id=sku.group(1) if sku else None,
        title=_text(title.group(1)) if title else None,
        price_rub=price,
        old_price_rub=old if old is not None and price is not None and old > price else None,
        rating=rating,
        review_count=reviews,
        availability=term,
        in_stock=in_stock,
        url=_card_path(htmllib.unescape(link.group(1))) if link else None,
    )


def _blocks(page: str) -> list[str]:
    return [chunk.split("<script", 1)[0] for chunk in page.split(_BLOCK_START)[1:]]


async def _search_page(url: str) -> dict[str, Any]:
    """The page and the category the site sent the query to, if it did."""
    reply = await _get(url, "holodilnik_search")
    category = None
    if reply.status in _REDIRECTS:
        target = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, reply.location))
        if target.scheme not in ("http", "https") or target.hostname not in _HOSTS:
            _refuse_redirect("holodilnik_search", reply)
        category = target.path
        reply = await _get(
            urllib.parse.urlunsplit(("https", "www.holodilnik.ru", target.path, target.query, "")), "holodilnik_search"
        )
        if reply.status in _REDIRECTS:
            _refuse_redirect("holodilnik_search", reply)
    page = _page_text(reply, "holodilnik_search")
    if 'id="search_res_info"' not in page and _BLOCK_START not in page:
        if _WALL_RE.search(page):
            _refuse(
                "holodilnik_search",
                "anti-bot page",
                "anti-bot page instead of the search",
                status=200,
                challenge_type="antibot_page",
            )
        raise_tool_error(
            ParserDriftError(
                "holodilnik_search: neither search info nor product rows on the page", provider="holodilnik"
            )
        )
    _pacer.record_success()
    return {"page": page, "category": category}


@mcp.tool(
    name="holodilnik_search",
    annotations=ToolAnnotations(
        title="Holodilnik Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def holodilnik_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'холодильник atlant'")],
    limit: Annotated[
        int, Field(default=20, ge=1, le=PAGE_SIZE, description="Items to return from the first page.")
    ] = 20,
    ctx: Context | None = None,
) -> HolodilnikSearchResponse:
    """Search Holodilnik the way the site does, first page by popularity.

    The site answers a nonsense query with a few unrelated products; when no row names
    the query those rows are dropped with a ``no_match`` warning.

    ## Return Format

    HolodilnikSearchResponse: {status, query, tier_used, city, total_available, count,
    items[], meta}. Items carry product_id (the article number), title, price_rub (None
    when absent — never 0), old_price_rub, rating, review_count, availability, in_stock, url.

    ## Error Format

    ToolError: challenge_required on a foreign redirect or anti-bot page, transport_down with
    source_stopped on 401/403/429 (Holodilnik stops for 30 min); parser_drift when the page
    layout moved.
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    url = f"{SITE_BASE}/search/?search={urllib.parse.quote_plus(text)}"
    log_event("holodilnik_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"holodilnik_search: {text!r}")
    cached = _cache.get(url)
    fetched = cached if isinstance(cached, dict) else await _search_page(url)
    page = fetched["page"]
    parsed = [_search_item(block) for block in _blocks(page)]
    if cached is None:
        _cache.set(url, fetched)
    total_match = _TOTAL_RE.search(page)
    total = R.coerce_int(total_match.group(1)) if total_match else None
    warnings: list[str] = []
    items: list[HolodilnikSearchItemOut] = []
    if parsed and not names_query(text, [it.title or "" for it in parsed]):
        warnings.append(
            f"no_match: Holodilnik has no match for {text!r}; its fuzzy results were unrelated and left out"
        )
        total = 0
    else:
        for item in parsed[:limit]:
            if item.product_id is None or item.url is None:
                warnings.append("row_skipped: a search row came without an article number or product link")
                continue
            if item.price_rub is None:
                warnings.append(f"{item.product_id}: no_price")
            items.append(item)
        if not parsed:
            warnings.append(f"empty: Holodilnik found nothing for {text!r}")
            total = 0
        elif items and all(it.price_rub is None for it in items):
            warnings.insert(0, "no_prices_on_page: search rows came back without prices")
    if fetched["category"]:
        warnings.insert(0, f"category: Holodilnik sent the query to {fetched['category']}")
    log_event("holodilnik_search.done", query=text[:60], returned=len(items))
    result = HolodilnikSearchResponse(
        query=text,
        tier_used="cache" if cached is not None else "http",
        city=_city(page),
        total_available=total,
        count=len(items),
        items=items,
    )
    result.meta = MetaOut(source="holodilnik_search", healthy=not warnings, warnings=warnings)
    return result


def _scope(page: str, match: re.Match[str] | None) -> str:
    """Microdata from the tag carrying an itemtype up to the next nested itemscope."""
    if match is None:
        return ""
    start = page.find(">", match.end()) + 1
    return page[start : start + _SCOPE_SPAN].split("itemscope", 1)[0] if start else ""


def _meta(scope: str, prop: str) -> str | None:
    match = re.search(rf'itemprop="{prop}" content="([^"]*)"', scope)
    return _text(match.group(1)) if match else None


def _single(page: str, pattern: str, what: str) -> re.Match[str] | None:
    """The one match of ``pattern``; two would mean another product's microdata on the page, so drift."""
    matches = list(re.finditer(pattern, page))
    if len(matches) > 1:
        _drift(f"{len(matches)} {what} on the page, expected one")
    return matches[0] if matches else None


def _required(page: str, pattern: str, what: str) -> re.Match[str]:
    match = _single(page, pattern, what)
    if match is None:
        _drift(f"no {what} on the page")
    return match


def _drift(detail: str) -> NoReturn:
    raise_tool_error(ParserDriftError(f"holodilnik_card: {detail}", provider="holodilnik"))


def _card_fields(page: str) -> dict[str, Any]:
    product = _required(page, _PRODUCT_RE.pattern, "schema.org Product")
    offer = _scope(page, _required(page, _OFFER_RE.pattern, "schema.org Offer"))
    sku = _required(page, r'itemprop="sku" content="(\d{1,12})"', "article number (sku)")
    rating = _scope(page, _single(page, r'itemtype="https?://schema\.org/AggregateRating"', "rating block"))
    brand = _scope(page, _single(page, r'itemtype="https?://schema\.org/Brand"', "brand block"))
    gtin = _single(page, r'itemprop="gtin\d*" content="([^"]*)"', "barcode (gtin)")
    stock = re.search(r'itemprop="availability" (?:content|href)="https?://schema\.org/(\w+)"', offer)
    reviews = R.coerce_int(_meta(rating, "reviewCount"))
    currency = _meta(offer, "priceCurrency")
    price = _price(_meta(offer, "price")) if currency in (None, "RUB") else None
    return {
        "product_id": sku.group(1),
        "title": _meta(_scope(page, product), "name"),
        "brand": _meta(brand, "name"),
        "gtin": _text(gtin.group(1)) if gtin else None,
        "price": price,
        "old_price": _old_price(page, price),
        "rating": R.coerce_rating(_meta(rating, "ratingValue")) if reviews else None,
        "reviews": reviews,
        "is_available": _OFFER_STOCK.get(stock.group(1)) if stock else None,
        "city": _city(page),
    }


def _old_price(page: str, price: float | None) -> float | None:
    """The crossed-out price of the visible price block, only when its current price is the microdata one."""
    block = _single(page, _CARD_PRICES_RE.pattern, "price blocks")
    if block is None or price is None or _price(block.group(1)) != price:
        return None
    old = _price(block.group(2)) if block.group(2) else None
    return old if old is not None and old > price else None


def _is_ancestor(path: str, of: str) -> bool:
    return path.endswith("/") and of.startswith(path) and path != of


@mcp.tool(
    name="holodilnik_card",
    annotations=ToolAnnotations(
        title="Holodilnik Product Card",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def holodilnik_card(
    url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description="Product URL from holodilnik_search (/<section>/<kind>/<brand>/<model>/)",
        ),
    ],
    ctx: Context | None = None,
) -> HolodilnikCardResponse:
    """Fetch one Holodilnik product page over HTTP.

    ## Return Format

    HolodilnikCardResponse: {status, product_id, title, brand, gtin, price_rub, rating,
    review_count, is_available, city, url, tier_used, meta}. product_id is the article
    number, the same one holodilnik_search returns.

    ## Error Format

    ToolError: bad_request for a foreign URL; not_found for a product the site no longer
    has (it redirects to the category); challenge_required on a foreign redirect or
    anti-bot page; transport_down with source_stopped on 401/403/429; parser_drift when
    the microdata moved.
    """
    log_event("holodilnik_card.start", input=url[:80])
    raw = url.strip()
    page_url = _card_path(raw if "://" in raw or raw.startswith("/") else f"https://{raw}")
    if page_url is None:
        raise_tool_error(
            BadRequestError(f"not a holodilnik.ru product page: {url[:120]!r}; pass the URL from holodilnik_search")
        )
    cached = _cache.get(f"card:{page_url}")
    if isinstance(cached, dict):
        fetched, tier = cached, "cache"
    else:
        reply = await _get(page_url, "holodilnik_card")
        if reply.status in _REDIRECTS:
            target = urllib.parse.urlsplit(urllib.parse.urljoin(page_url, reply.location))
            moved = _card_path(reply.location)
            requested = urllib.parse.urlsplit(page_url).path
            if target.hostname in _HOSTS and _is_ancestor(target.path, requested):
                _pacer.record_success()
                raise_tool_error(
                    NotFoundError(
                        f"holodilnik_card: Holodilnik no longer has this product (sent to {target.path})",
                        provider="holodilnik",
                    )
                )
            if moved is None:
                _refuse_redirect("holodilnik_card", reply)
            page_url = moved
            reply = await _get(page_url, "holodilnik_card")
            if reply.status in _REDIRECTS:
                _refuse_redirect("holodilnik_card", reply)
        page = _page_text(reply, "holodilnik_card")
        if not _PRODUCT_RE.search(page) and _WALL_RE.search(page):
            _refuse(
                "holodilnik_card",
                "anti-bot page",
                "anti-bot page instead of the product",
                status=200,
                challenge_type="antibot_page",
            )
        fetched, tier = {**_card_fields(page), "url": page_url}, "http"
        _pacer.record_success()
        _cache.set(f"card:{page_url}", fetched)
    warnings: list[str] = []
    if fetched["price"] is None and fetched["is_available"] is not False:
        warnings.append("no_price: the product page carries no price")
    log_event("holodilnik_card.done", product_id=fetched["product_id"], price=fetched["price"], warnings=len(warnings))
    card = HolodilnikCardResponse(
        product_id=fetched["product_id"],
        title=fetched["title"],
        brand=fetched["brand"],
        gtin=fetched["gtin"],
        price_rub=fetched["price"],
        old_price_rub=fetched["old_price"],
        rating=fetched["rating"],
        review_count=fetched["reviews"],
        is_available=fetched["is_available"],
        city=fetched["city"],
        url=fetched["url"],
        tier_used=tier,
    )
    card.meta = MetaOut(source="holodilnik_card", healthy=not warnings, warnings=warnings)
    return card


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def holodilnik_selfcheck(ctx: Context | None = None) -> HolodilnikSelfcheckResponse:
    """Drift canary: one search, then the card of its first row (two paced requests).

    A refusal or network fault is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("holodilnik_selfcheck.start")
    try:
        result = await _holodilnik_selfcheck_impl(ctx)
        log_event("holodilnik_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("holodilnik_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"holodilnik_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _holodilnik_selfcheck_impl(ctx: Context | None) -> HolodilnikSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(60):
            found = await holodilnik_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="catalog-item-search-v1", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="catalog-item-search-v1", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(
            state, baseline="catalog-item-search-v1", reason=reason, notes=[str(exc)[:160]]
        )

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="microdata-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await holodilnik_card(first_url, ctx=ctx)
            state = "healthy" if card.price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state,
                baseline="microdata-card-v1",
                reason=None if state == "healthy" else "parse_smoke_failed",
                notes=[f"price {card.price_rub}"],
            )
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(
                state, baseline="microdata-card-v1", reason=reason, notes=[str(exc)[:160]]
            )

    result_dict = R.selfcheck_result(
        "holodilnik",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return HolodilnikSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
