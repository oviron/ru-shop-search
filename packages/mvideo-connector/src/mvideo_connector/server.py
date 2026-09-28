"""M.Video MCP connector.

Search posts the query to the site's own ``/bff/products`` API over HTTP (curl_cffi as
Chrome) with the Saint Petersburg city cookies. The card opens the product page in the plugin's
Chrome tab and reads its schema.org ``Product`` plus the rendered price block: over plain
HTTP ``/bff/pdp/info`` and ``/bff/location`` answer 403 (live, 2026-09-27).

A 401/403/418/429, a redirect or an HTML page instead of JSON stops M.Video for 30 minutes.

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
from mcp_core.stopgate import SourceStoppedError
from mcp_core.transport import proxy_from_env
from mcp_core.transport.chrome_cdp import NavBlocked, open_page
from mcp_core.transport.impersonated import Reply, shop_request
from pydantic import Field

from mvideo_connector.models_output import (
    MetaOut,
    MVideoCardResponse,
    MVideoSearchItemOut,
    MVideoSearchResponse,
    MVideoSelfcheckResponse,
)
from mvideo_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.mvideo.ru"
PRODUCTS_API = f"{SITE_BASE}/bff/products"
_HOSTS = frozenset({"mvideo.ru", "www.mvideo.ru"})

_BARE_ID_RE = re.compile(r"\d{6,12}")
_CARD_PATH_RE = re.compile(r"^/products/(?:[a-z0-9-]*-)?(\d{6,12})/?$")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TAB_REFUSALS = frozenset({401, 403, 429})
_NOT_FOUND_RE = re.compile(r"не найден|404|не существует", re.IGNORECASE)
_MARKETPLACE_PREFIX = "(МП)"
_RENDER_WAIT_MS = 8000

# The site's own values for Saint Petersburg, read from its cookies after it switched city (2026-09-28).
CITY = "Санкт-Петербург"
CITY_COOKIES = {
    "MVID_CITY_ID": "CityCZ_1638",
    "MVID_REGION_ID": "6",
    "MVID_REGION_SHOP": "S904",
    "MVID_KLADR_ID": "7800000000000",
    "MVID_TIMEZONE_OFFSET": "3",
}

mcp = FastMCP(
    name="mvideo-connector",
    version=SERVER_VERSION,
    instructions=(
        "M.Video electronics catalog: mvideo_search asks the site's search API over HTTP, "
        "mvideo_card opens a product page in the plugin's Chrome tab. Prices are for Saint Petersburg; "
        "bonus roubles are never counted as price."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_settings.min_gap, source="mvideo")
_cdp_lock = asyncio.Lock()


def _proxy() -> str | None:
    return (_settings.proxy.get_secret_value() or "").strip() or proxy_from_env("MVIDEO_PROXY")


def _extract_product_id(raw: str) -> str | None:
    """The numeric id from an mvideo.ru product URL or a bare id; None for anything else."""
    raw = raw.strip()
    if _BARE_ID_RE.fullmatch(raw):
        return raw
    parts = urllib.parse.urlsplit(raw if "://" in raw else f"https://{raw.lstrip('/')}")
    if parts.scheme not in ("http", "https") or (parts.hostname or "").rstrip(".").lower() not in _HOSTS:
        return None
    match = _CARD_PATH_RE.match(parts.path)
    return match.group(1) if match else None


def _product_url(product_id: str) -> str:
    return f"{SITE_BASE}/products/{product_id}"


def _refuse(label: str, reason: str, detail: str, *, status: int, challenge_type: str) -> NoReturn:
    _pacer.record_refusal(reason)
    raise_tool_error(
        ChallengeRequiredError(
            f"{label}: {detail}; M.Video is stopped for 30 min, do not retry",
            provider="mvideo",
            challenge_type=challenge_type,
            status_code=status,
        )
    )


async def _post_json(url: str, payload: dict[str, Any], *, referer: str, label: str) -> Reply:
    try:
        return await shop_request(
            url,
            pacer=_pacer,
            provider="mvideo",
            label=label,
            body=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Content-Type": "application/json",
                "Origin": SITE_BASE,
                "Referer": referer,
            },
            cookies=CITY_COOKIES,
            timeout_s=_settings.timeout,
            max_bytes=_settings.max_body_bytes,
            retries=1,
            proxy=_proxy(),
        )
    except ConnectorError as exc:
        raise_tool_error(exc)


def _api_body(reply: Reply, label: str) -> dict[str, Any]:
    if reply.status in _REDIRECTS:
        _refuse(
            label,
            f"http {reply.status}",
            f"redirect to {reply.location[:80]!r} instead of JSON",
            status=reply.status,
            challenge_type="redirect",
        )
    if reply.status == 204:
        # The site answers a query with no matches with an empty 204 (live, 2026-09-27).
        _pacer.record_success()
        return {"items": [], "total": 0}
    if reply.status != 200:
        raise_tool_error(
            TransportDownError(f"{label}: unexpected HTTP {reply.status}", provider="mvideo", status_code=reply.status)
        )
    text = (reply.text or "").lstrip()
    if text.startswith("<"):
        _refuse(label, "html page", "an HTML page instead of JSON", status=200, challenge_type="antibot_page")
    try:
        data = json.loads(text)
    except ValueError:
        raise_tool_error(ParserDriftError(f"{label}: the answer is not JSON", provider="mvideo"))
    if not isinstance(data, dict) or not isinstance(data.get("body"), dict):
        raise_tool_error(ParserDriftError(f"{label}: the answer has no body object", provider="mvideo"))
    if data.get("success") is False:
        raise_tool_error(
            TransportDownError(f"{label}: the site answered success=false {str(data.get('messages'))[:120]}")
        )
    _pacer.record_success()
    return data["body"]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_price(value: Any) -> float | None:
    price = R.coerce_price(value)
    return price if price is not None and price > 0 else None


def _title(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _search_item(raw: dict[str, Any]) -> MVideoSearchItemOut:
    prices = _mapping(raw.get("price"))
    price = _positive_price(prices.get("salePrice"))
    old = _positive_price(prices.get("basePrice"))
    rating = _mapping(raw.get("rating"))
    reviews = R.coerce_int(rating.get("count"))
    if raw.get("soldOut") is True:
        in_stock: bool | None = False
    elif raw.get("isPreorder") is True:
        in_stock = None
    else:
        in_stock = True if raw.get("status") == "available" else None
    product_id = str(raw.get("productId") or "") or None
    title = _title(raw.get("name"))
    return MVideoSearchItemOut(
        product_id=product_id,
        title=title,
        brand=_title(raw.get("brandName")),
        price_rub=price,
        old_price_rub=old if old is not None and price is not None and old > price else None,
        rating=R.coerce_rating(rating.get("star")) if reviews else None,
        review_count=reviews,
        in_stock=in_stock,
        marketplace=bool(title and title.startswith(_MARKETPLACE_PREFIX)),
        url=_product_url(product_id) if product_id else None,
    )


@mcp.tool(
    name="mvideo_search",
    annotations=ToolAnnotations(
        title="M.Video Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def mvideo_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'rtx 5070'")],
    limit: Annotated[int, Field(default=20, ge=1, le=60, description="Items to return from the first page.")] = 20,
    ctx: Context | None = None,
) -> MVideoSearchResponse:
    """Search M.Video the way the site does, first page by popularity, Saint Petersburg prices.

    ## Return Format

    MVideoSearchResponse: {status, query, tier_used, city, total_available, count, items[], meta}.
    Items carry product_id, title, brand, price_rub (None when absent — never 0),
    old_price_rub, rating, review_count, in_stock, marketplace, url.

    ## Error Format

    ToolError: challenge_required on a redirect or an HTML page instead of JSON, a
    401/403/418/429 stops M.Video too (30 min); transport_down on other failures;
    parser_drift when the answer's shape moved.
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    log_event("mvideo_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"mvideo_search: {text!r}")
    payload = {
        "limit": 60,
        "cursorId": "",
        "enrich": True,
        "filters": [],
        "sortBy": "popularity",
        "sortDirection": "desc",
        "isGettingBonusRoubles": True,
        "query": text,
    }
    referer = f"{SITE_BASE}/search?q={urllib.parse.quote(text)}"
    cached = _cache.get(f"search:{text}")
    if cached is not None:
        body, tier = cached, "cache"
    else:
        body = _api_body(
            await _post_json(PRODUCTS_API, payload, referer=referer, label="mvideo_search"), "mvideo_search"
        )
        tier = "http"
    rows = body.get("items")
    if not isinstance(rows, list):
        raise_tool_error(ParserDriftError("mvideo_search: body.items is missing", provider="mvideo"))
    if tier == "http":
        _cache.set(f"search:{text}", body)
    warnings: list[str] = []
    items: list[MVideoSearchItemOut] = []
    for raw in rows[:limit]:
        if not isinstance(raw, dict):
            continue
        item = _search_item(raw)
        if item.product_id is None:
            warnings.append("row_skipped: a search row came without a productId")
            continue
        if item.price_rub is None and item.in_stock is not False:
            warnings.append(f"{item.product_id}: no_price")
        items.append(item)
    if not items:
        warnings.insert(0, f"empty: M.Video found nothing for {text!r}")
    elif all(it.price_rub is None for it in items):
        warnings.insert(0, "no_prices_on_page: search rows came back without prices")
    log_event("mvideo_search.done", query=text[:60], returned=len(items))
    result = MVideoSearchResponse(
        query=text,
        tier_used=tier,
        city=CITY,
        total_available=R.coerce_int(body.get("total")),
        count=len(items),
        items=items,
    )
    result.meta = MetaOut(source="mvideo_search", healthy=not warnings, warnings=warnings)
    return result


# Only the fields below leave the tab: the page's reviews carry buyers' names.
_CARD_EXTRACT_JS = """
() => {
  let product = null;
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    let data;
    try { data = JSON.parse(script.textContent || ''); } catch (e) { continue; }
    const nodes = Array.isArray(data) ? data : (Array.isArray(data && data['@graph']) ? data['@graph'] : [data]);
    product = nodes.find((node) => node && node['@type'] === 'Product') || null;
    if (product) break;
  }
  const block = document.querySelector('mvid-price-info-block');
  const text = (selector) => {
    const el = block && block.querySelector(selector);
    return el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : null;
  };
  const offer = product ? (Array.isArray(product.offers) ? product.offers[0] : product.offers) || {} : {};
  const rating = (product && product.aggregateRating) || {};
  const h1 = document.querySelector('h1');
  return JSON.stringify({
    title: document.title || '',
    h1: h1 ? (h1.textContent || '').trim().slice(0, 200) : null,
    product: product ? {
      sku: product.sku == null ? null : String(product.sku),
      name: typeof product.name === 'string' ? product.name : null,
      brand: product.brand && typeof product.brand.name === 'string' ? product.brand.name : null,
      price: offer.price == null ? null : offer.price,
      currency: offer.priceCurrency || null,
      availability: typeof offer.availability === 'string' ? offer.availability : null,
      rating: rating.ratingValue == null ? null : rating.ratingValue,
      reviews: rating.reviewCount == null ? null : rating.reviewCount,
    } : null,
    price_text: text('.prices-base'),
    old_price_text: text('.prices-discount'),
  });
}
"""


def _parse_extract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > _settings.max_body_bytes:
        raise_tool_error(TransportDownError("extracted page data missing or over the body cap", provider="mvideo"))
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise_tool_error(ParserDriftError("page extractor returned a non-object payload", provider="mvideo"))
    return data


def _in_stock(availability: Any) -> bool | None:
    kind = str(availability or "").rsplit("/", 1)[-1]
    if kind == "InStock":
        return True
    if kind in ("OutOfStock", "SoldOut", "Discontinued"):
        return False
    return None


def _ready(data: dict[str, Any]) -> bool:
    product = _mapping(data.get("product"))
    return bool(product) and (bool(data.get("price_text")) or _in_stock(product.get("availability")) is not True)


def _blocked(status: int) -> TransportDownError:
    return TransportDownError(
        f"M.Video refused the browser tab (HTTP {status}); M.Video is stopped for 30 min, do not retry. "
        "If the profile window shows a check, it may be passed by hand, then `marketplace-mcp resume mvideo`.",
        provider="mvideo",
        status_code=status,
    )


async def _cdp_render(url: str) -> dict[str, Any]:
    """Open the product page in the plugin's Chrome and extract once its price block shows."""
    async with _cdp_lock:
        try:
            await _pacer.wait()
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(url, wait_ms=0, cookies=CITY_COOKIES) as page:
                deadline = asyncio.get_running_loop().time() + _RENDER_WAIT_MS / 1000
                while True:
                    data = _parse_extract(await asyncio.wait_for(page.evaluate(_CARD_EXTRACT_JS), timeout=30.0))
                    if _ready(data) or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.5)
        except NavBlocked as exc:
            if exc.status in _TAB_REFUSALS:
                _pacer.record_refusal(f"http {exc.status}")
                raise_tool_error(_blocked(exc.status))
            raise_tool_error(
                TransportDownError(
                    f"M.Video page failed (HTTP {exc.status})", provider="mvideo", status_code=exc.status
                )
            )
        except (RuntimeError, TimeoutError) as exc:
            log_event("mvideo_card.tab_error", error=_redact(str(exc)), exc_type=type(exc).__name__)
            raise_tool_error(TransportDownError(_redact(f"the browser tab failed: {exc}"[:300]), provider="mvideo"))
    if not data.get("product"):
        page_text = f"{data.get('title') or ''} {data.get('h1') or ''}"
        if _NOT_FOUND_RE.search(page_text):
            _pacer.record_success()
            return data
        if "м.видео" not in page_text.lower():
            _pacer.record_refusal("page without the shop's title")
            raise_tool_error(_blocked(200))
        raise_tool_error(ParserDriftError("the product page has no schema.org Product", provider="mvideo"))
    _pacer.record_success()
    return data


@mcp.tool(
    name="mvideo_card",
    annotations=ToolAnnotations(
        title="M.Video Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def mvideo_card(
    product_id_or_url: Annotated[
        str, Field(min_length=1, max_length=300, description="Product id (400678996) or mvideo.ru product URL")
    ],
    ctx: Context | None = None,
) -> MVideoCardResponse:
    """Open one M.Video product page in the plugin's Chrome tab.

    ## Return Format

    MVideoCardResponse: {status, product_id, title, brand, price_rub, old_price_rub,
    rating, review_count, is_available, marketplace, url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request when no id can be read; not_found for an unknown id or a page
    of another product; transport_down when the tab is refused (M.Video stops for
    30 min); parser_drift when the page no longer carries a schema.org Product.
    """
    product_id = _extract_product_id(product_id_or_url)
    if product_id is None:
        raise_tool_error(
            BadRequestError(f"could not read an M.Video product id from {product_id_or_url[:80]!r}; pass 400678996")
        )
    url = _product_url(product_id)
    log_event("mvideo_card.start", product_id=product_id)
    if ctx is not None:
        await ctx.info(f"mvideo_card: {product_id}")
    cached = _cache.get(f"card:{product_id}")
    if cached is not None:
        data, tier = cached, "cache"
    else:
        data = await _cdp_render(url)
        tier = "cdp"
    product = _mapping(data.get("product"))
    if not product:
        raise_tool_error(NotFoundError(f"M.Video has no product {product_id}", provider="mvideo"))
    if str(product.get("sku") or "") != product_id:
        raise_tool_error(
            NotFoundError(f"M.Video opened product {product.get('sku')!r} instead of {product_id}", provider="mvideo")
        )
    if tier == "cdp":
        _cache.set(f"card:{product_id}", data)
    warnings: list[str] = []
    ld_price = _positive_price(product.get("price"))
    if product.get("currency") not in (None, "RUB"):
        warnings.append(f"price_skipped: the page prices in {product.get('currency')!r}")
        ld_price = None
    shown = _positive_price(data.get("price_text"))
    if shown is not None and ld_price is not None and shown != ld_price:
        warnings.append(f"price_mismatch: the page shows {shown:.0f} ₽, its schema.org offer {ld_price:.0f} ₽")
    price = shown if shown is not None else ld_price
    old = _positive_price(data.get("old_price_text"))
    reviews = R.coerce_int(product.get("reviews"))
    available = _in_stock(product.get("availability"))
    if available is True and price is None:
        warnings.append("in_stock_no_price: the card is available but carries no price")
    title = _title(product.get("name"))
    log_event("mvideo_card.done", product_id=product_id, price=price, warnings=len(warnings))
    card = MVideoCardResponse(
        product_id=product_id,
        title=title,
        brand=_title(product.get("brand")),
        price_rub=price,
        old_price_rub=old if old is not None and price is not None and old > price else None,
        rating=R.coerce_rating(product.get("rating")) if reviews else None,
        review_count=reviews,
        is_available=available,
        marketplace=bool(title and title.startswith(_MARKETPLACE_PREFIX)),
        url=url,
        tier_used=tier,
    )
    card.meta = MetaOut(source="mvideo_card", healthy=not warnings, warnings=warnings)
    return card


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def mvideo_selfcheck(ctx: Context | None = None) -> MVideoSelfcheckResponse:
    """Drift canary: one search, then the card of its first priced row (HTTP, then one tab).

    A refusal or network fault is inconclusive; only a reached answer that fails to
    parse is drift.
    """
    log_event("mvideo_selfcheck.start")
    try:
        result = await _mvideo_selfcheck_impl(ctx)
        log_event("mvideo_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("mvideo_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"mvideo_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _mvideo_selfcheck_impl(ctx: Context | None) -> MVideoSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(60):
            found = await mvideo_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="bff-products-v1", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="bff-products-v1", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(state, baseline="bff-products-v1", reason=reason, notes=[str(exc)[:160]])

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="ldjson-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await mvideo_card(first_url, ctx=ctx)
            state = "healthy" if card.price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state,
                baseline="ldjson-card-v1",
                reason=None if state == "healthy" else "parse_smoke_failed",
                notes=[f"price {card.price_rub}"],
            )
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(state, baseline="ldjson-card-v1", reason=reason, notes=[str(exc)[:160]])

    result_dict = R.selfcheck_result(
        "mvideo",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return MVideoSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
