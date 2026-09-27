"""Citilink MCP connector: search and product cards, read in the plugin's Chrome tab.

A tab without Citilink's ``_pcl`` cookie gets a 429 «Загрузка...» page that reloads
itself into 200 (live, 2026-09-27); plain HTTP never gets past it. Search reads the
rendered result tiles; the card reads the product from the page's ``__NEXT_DATA__``.

NEVER write to stdout in a stdio MCP server — it corrupts JSON-RPC.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import urllib.parse
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from mcp_core import resilience as R
from mcp_core.cache import TTLCache
from mcp_core.errors import (
    BadRequestError,
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
from mcp_core.stopgate import SourceStoppedError
from mcp_core.transport.chrome_cdp import NavBlocked, open_page
from pydantic import Field

from citilink_connector.models_output import (
    CitilinkCardResponse,
    CitilinkSearchItemOut,
    CitilinkSearchResponse,
    CitilinkSelfcheckResponse,
    MetaOut,
)
from citilink_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.citilink.ru"
SEARCH_URL = f"{SITE_BASE}/search/"

MAX_BODY_BYTES = _settings.max_body_bytes
_min_gap = _settings.min_gap

# The id is interpolated back into a URL opened in the plugin's Chrome, so `?`, `#`,
# `%` and `.` stay out: they would let a caller append a query or walk the path.
_ID_CHARS = r"[A-Za-z0-9_-]"
_PRODUCT_ID_RE = re.compile(rf"/product/({_ID_CHARS}+)")
_BARE_ID_RE = re.compile(rf"{_ID_CHARS}{{4,}}")
_TRAILING_NUMBER_RE = re.compile(r"(\d+)$")

_REFUSAL_STATUSES = frozenset({401, 403, 429})
# The 429 «Загрузка...» page reloads itself into 200 in ~1.5 s (live, 2026-09-27).
_CHALLENGE_STATUSES = frozenset({401, 403, 429})
_CHALLENGE_GRACE_S = 12.0
_RENDER_WAIT_MS = 8000

mcp = FastMCP(
    name="citilink-connector",
    version=SERVER_VERSION,
    instructions=(
        "Citilink electronics catalog: search and product cards, read in the plugin's "
        "Chrome tab. Start with citilink_search; citilink_card takes a product id or "
        "citilink.ru product URL."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap, source="citilink")
_cdp_lock = asyncio.Lock()


def _extract_product_id(raw: str) -> str | None:
    """The product slug from a citilink.ru product URL or a bare id; None for anything off-site."""
    raw = raw.strip()
    if not raw:
        return None
    if _BARE_ID_RE.fullmatch(raw):
        return raw.lower()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme:
        if parts.scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").rstrip(".").lower()
        if host != "citilink.ru" and not host.endswith(".citilink.ru"):
            return None
        raw = parts.path
    elif raw.startswith("//"):
        return None
    match = _PRODUCT_ID_RE.search(raw)
    return match.group(1).lower() if match else None


# Only ProductListLayout holds real results: on «ничего не найдено» the page shows
# priced ProductsCompilation recommendations instead (live, 2026-09-27).
_SEARCH_EXTRACT_JS = """
() => {
    const text = el => el ? ((el.textContent || '').replace(/\\s+/g, ' ').trim() || null) : null;
    const h1 = text(document.querySelector('h1'));
    // July 2026 saw a layout with the grid inside a same-origin iframe.
    const docs = [document];
    for (const frame of document.querySelectorAll('iframe')) {
        try { if (frame.contentDocument) docs.push(frame.contentDocument); } catch (e) {}
    }
    let list = null;
    for (const doc of docs) { list = list || doc.querySelector('[data-meta-name="ProductListLayout"]'); }
    const tiles = list
        ? list.querySelectorAll('[data-meta-name="ProductVerticalSnippet"], [data-meta-name="ProductHorizontalSnippet"]')
        : [];
    const items = [];
    for (const tile of tiles) {
        const titleEl = tile.querySelector('[data-meta-name="Snippet__title"]');
        let href = titleEl ? titleEl.getAttribute('href') : null;
        if (!href) {
            for (const a of tile.querySelectorAll('a[href^="/product/"]')) {
                const candidate = a.getAttribute('href');
                if (/^\\/product\\/[A-Za-z0-9_-]+\\/$/.test(candidate)) { href = candidate; break; }
            }
        }
        const img = tile.querySelector('img[alt]');
        const price = tile.querySelector('[data-meta-name="Snippet__price"] [data-meta-price]');
        items.push({
            url: href ? new URL(href, document.baseURI).href : null,
            title: (titleEl && titleEl.getAttribute('title')) || text(titleEl) || (img ? img.getAttribute('alt') : null),
            price: price ? price.getAttribute('data-meta-price') : null,
            old_price_text: text(tile.querySelector('[data-meta-name="Snippet__old-price"] > div')),
            rating: text(tile.querySelector('[data-meta-name="MetaInfo_rating"]')),
            reviews: text(tile.querySelector('[data-meta-name="MetaInfo_opinionsCount"]')),
            buyable: !!tile.querySelector('[data-meta-name="Snippet__cart-button"]'),
            awaited: !!tile.querySelector('[data-meta-name="Snippet__arrival-button"]'),
        });
    }
    return JSON.stringify({
        title: document.title || '',
        city: text(document.querySelector('[data-meta-name="CityChangeButton"]')),
        nothing: !list && /ничего не найдено/i.test(h1 || ''),
        items: items,
    });
}
"""

# Only the listed product fields leave the page: __NEXT_DATA__ also carries cookies,
# the visitor's address and session tokens.
_CARD_EXTRACT_JS = """
() => {
    let state = null;
    try {
        const node = document.getElementById('__NEXT_DATA__');
        state = node ? JSON.parse(node.textContent) : null;
    } catch (e) {}
    const s = (state && state.props && state.props.initialState) || {};
    const pick = (obj, path) => path.reduce((o, k) => (o && typeof o === 'object' ? o[k] : undefined), obj);
    const pb = pick(s, ['productPage', 'productHeader', 'payload', 'productBase']);
    return JSON.stringify({
        title: document.title || '',
        not_found: pick(s, ['system', 'appState', 'isNotFound']) === true,
        city: pick(s, ['layoutMain', 'cityList', 'payload', 'city', 'name']) || null,
        product: pb ? {
            id: pb.id, name: pb.name, isAvailable: pb.isAvailable, price: pb.price,
            brand: pb.brand, rating: pb.rating, counters: pb.counters,
        } : null,
    });
}
"""


def _parse_extract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BODY_BYTES:
        raise_tool_error(TransportDownError("extracted page data missing or over the body cap", provider="citilink"))
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise_tool_error(ParserDriftError("page extractor returned a non-object payload", provider="citilink"))
    return data


def _ready(data: dict[str, Any]) -> bool:
    return bool(data.get("items") or data.get("nothing") or data.get("product") or data.get("not_found"))


def _is_wall(data: dict[str, Any]) -> bool:
    """Every Citilink page title names the shop; the «Загрузка...» and block pages do not."""
    return not _ready(data) and "ситилинк" not in str(data.get("title") or "").lower()


def _blocked(status: int) -> TransportDownError:
    return TransportDownError(
        f"Citilink refused the browser tab (HTTP {status}); Citilink is stopped for 30 min, do not retry. "
        "If the profile window shows a check, it may be passed by hand, then `marketplace-mcp resume citilink`.",
        provider="citilink",
        status_code=status,
    )


async def _cdp_render(url: str, extract_js: str) -> dict[str, Any]:
    """Open ``url`` in the plugin's Chrome, let the 429 page reload itself, extract once the data shows."""
    async with _cdp_lock:
        try:
            await _pacer.wait(min_gap=_min_gap)
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(
                url,
                wait_ms=0,
                challenge_grace_s=_CHALLENGE_GRACE_S,
                challenge_statuses=_CHALLENGE_STATUSES,
            ) as page:
                deadline = asyncio.get_running_loop().time() + _RENDER_WAIT_MS / 1000
                while True:
                    data = _parse_extract(await asyncio.wait_for(page.evaluate(extract_js), timeout=30.0))
                    if _ready(data) or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.5)
        except NavBlocked as exc:
            if exc.status in _REFUSAL_STATUSES:
                _pacer.record_refusal(f"http {exc.status}")
                raise_tool_error(_blocked(exc.status))
            raise_tool_error(
                TransportDownError(
                    f"Citilink page failed (HTTP {exc.status})", provider="citilink", status_code=exc.status
                )
            )
    if _is_wall(data):
        _pacer.record_refusal("loading or block page")
        raise_tool_error(_blocked(429))
    _pacer.record_success()
    return data


def _number(text: Any) -> float | None:
    digits = re.sub(r"[^\d.,]", "", str(text or "")).replace(",", ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _clean(value: Any) -> str | None:
    return " ".join(str(value).split()) or None if isinstance(value, str) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_price(value: Any) -> float | None:
    price = _number(value)
    return price if price is not None and price > 0 else None


def _search_item(tile: dict[str, Any]) -> CitilinkSearchItemOut:
    price = _positive_price(tile.get("price"))
    old = _positive_price(tile.get("old_price_text"))
    reviews = _number(tile.get("reviews"))
    review_count = int(reviews) if reviews is not None else None
    in_stock = True if tile.get("buyable") else (False if tile.get("awaited") else None)
    url = tile.get("url") if isinstance(tile.get("url"), str) else None
    return CitilinkSearchItemOut(
        product_id=_extract_product_id(url) if url else None,
        title=_clean(tile.get("title")),
        price_rub=price,
        old_price_rub=old if old is not None and price is not None and old > price else None,
        rating=R.coerce_rating(_number(tile.get("rating"))) if review_count else None,
        review_count=review_count,
        in_stock=in_stock,
        url=url,
    )


@mcp.tool(
    name="citilink_search",
    annotations=ToolAnnotations(
        title="Citilink Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def citilink_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'rtx 5070'")],
    limit: Annotated[int, Field(default=20, ge=1, le=48, description="Items to return from the first page.")] = 20,
    ctx: Context | None = None,
) -> CitilinkSearchResponse:
    """Search Citilink the way the site does, first page by relevance.

    ## Return Format

    CitilinkSearchResponse: {status, query, tier_used, city, count, items[], meta}.
    Items carry product_id, title, price_rub (None when absent — never 0; the club
    price is not used), old_price_rub, rating, review_count, in_stock, url.

    ## Error Format

    ToolError: transport_down when Citilink refused the tab (Citilink stops for 30 min);
    parser_drift when a rendered page has neither results nor «ничего не найдено».
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    log_event("citilink_search.start", query=text[:60])
    try:
        url = f"{SEARCH_URL}?text={urllib.parse.quote(text)}"
        cached = _cache.get(url)
        if cached is not None:
            payload, tier = cached, "cache"
        else:
            payload = await _cdp_render(url, _SEARCH_EXTRACT_JS)
            tier = "cdp"
        tiles = [t for t in payload.get("items") or [] if isinstance(t, dict)]
        if not tiles and not payload.get("nothing"):
            raise_tool_error(
                ParserDriftError(
                    "rendered search page has neither result tiles nor «ничего не найдено»", provider="citilink"
                )
            )
        if tier == "cdp":
            _cache.set(url, payload)
        items = [_search_item(t) for t in tiles[:limit]]
        warnings: list[str] = []
        if not items:
            warnings.append(f"empty: Citilink found nothing for {text!r}")
        elif all(it.price_rub is None for it in items):
            warnings.append("no_prices_on_page")
        else:
            warnings.extend(f"{it.product_id}: no_price" for it in items if it.in_stock and it.price_rub is None)
        result = CitilinkSearchResponse(
            query=text, tier_used=tier, city=R.flatten_text(payload.get("city")), count=len(items), items=items
        )
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="citilink_search")
        result.meta = MetaOut(**attached["_meta"])
        log_event("citilink_search.done", query=text[:60], returned=len(items))
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("citilink_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"citilink_search failed: {exc}"), provider="citilink"))


@mcp.tool(
    name="citilink_card",
    annotations=ToolAnnotations(
        title="Citilink Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def citilink_card(
    product_url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=400,
            description="citilink.ru product URL or its slug, e.g. /product/noutbuk-lenovo-2169270/",
        ),
    ],
    ctx: Context | None = None,
) -> CitilinkCardResponse:
    """Fetch one Citilink product card.

    ## Return Format

    CitilinkCardResponse: {status, product_id, title, brand, price_rub, old_price_rub,
    is_available, rating, review_count, city, url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request when the input carries no citilink.ru product id; not_found
    for an unknown product; transport_down when Citilink refused the tab (stops for
    30 min); parser_drift when the page has no product data.
    """
    log_event("citilink_card.start", input=product_url[:80])
    try:
        product_id = _extract_product_id(product_url)
        if product_id is None:
            raise_tool_error(
                BadRequestError(
                    f"could not extract a product id from {product_url!r}; pass a citilink.ru URL with "
                    "/product/<slug>/ (the slug ends in a numeric id, e.g. /product/noutbuk-lenovo-2169270/)"
                )
            )
        url = f"{SITE_BASE}/product/{product_id}/"
        cached = _cache.get(url)
        if cached is not None:
            payload, tier = cached, "cache"
        else:
            payload = await _cdp_render(url, _CARD_EXTRACT_JS)
            tier = "cdp"
        product = payload.get("product")
        if not isinstance(product, dict):
            if payload.get("not_found"):
                raise_tool_error(NotFoundError(f"Citilink has no product {product_id!r}", provider="citilink"))
            raise_tool_error(ParserDriftError("card page has no product in __NEXT_DATA__", provider="citilink"))
        wanted = _TRAILING_NUMBER_RE.search(product_id)
        if wanted and str(product.get("id")) != wanted.group(1):
            raise_tool_error(
                NotFoundError(
                    f"Citilink opened product {product.get('id')!r} instead of {wanted.group(1)!r}", provider="citilink"
                )
            )
        if tier == "cdp":
            _cache.set(url, payload)
        price_block = _mapping(product.get("price"))
        price = _positive_price(price_block.get("current"))
        old = _positive_price(price_block.get("old"))
        counters = _mapping(product.get("counters"))
        reviews = R.coerce_int(counters.get("opinions"))
        brand = _mapping(product.get("brand"))
        available = product.get("isAvailable")
        warnings: list[str] = []
        if available is True and price is None:
            warnings.append("in_stock_no_price: the card is available but carries no price")
        result = CitilinkCardResponse(
            product_id=product_id,
            title=_clean(product.get("name")),
            brand=R.flatten_text(brand.get("name")),
            price_rub=price,
            old_price_rub=old if old is not None and price is not None and old > price else None,
            is_available=available if isinstance(available, bool) else None,
            rating=R.coerce_rating(product.get("rating")) if reviews else None,
            review_count=reviews,
            city=R.flatten_text(payload.get("city")),
            url=url,
            tier_used=tier,
        )
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="citilink_card")
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("citilink_card.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"citilink_card failed: {exc}"), provider="citilink"))


# CLI-only drift canary for ``marketplace-mcp doctor``; not an MCP tool.
async def citilink_selfcheck(ctx: Context | None = None) -> CitilinkSelfcheckResponse:
    """Drift canary: one search, then the card of its first priced row (two paced tabs).

    A refusal or a dead Chrome is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("citilink_selfcheck.start")
    try:
        result = await _citilink_selfcheck_impl(ctx)
        log_event("citilink_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("citilink_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"citilink_selfcheck failed: {exc}"), provider="citilink"))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _citilink_selfcheck_impl(ctx: Context | None) -> CitilinkSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(90):
            found = await citilink_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="dom-search-v2", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="dom-search-v2", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except TimeoutError:
        checks["search"] = R.selfcheck_entry("inconclusive", baseline="dom-search-v2", reason="timeout")
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(state, baseline="dom-search-v2", reason=reason, notes=[str(exc)[:160]])

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="next-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(90):
                card = await citilink_card(first_url, ctx=ctx)
            state = "healthy" if card.price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state, baseline="next-card-v1", reason=None if state == "healthy" else "parse_smoke_failed"
            )
        except TimeoutError:
            checks["card"] = R.selfcheck_entry("inconclusive", baseline="next-card-v1", reason="timeout")
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(state, baseline="next-card-v1", reason=reason, notes=[str(exc)[:160]])

    result_dict = R.selfcheck_result(
        "citilink",
        checks,
        required=("search",),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return CitilinkSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
