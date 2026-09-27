"""Lamoda MCP connector.

Search and cards read the site's own pages over HTTP (curl_cffi as Chrome). Both
pages carry their data as JSON behind the second ``payload:`` key of
``window.__NUXT__``: ``products`` on a search page, ``product`` on a card.

Prices come as a chain: the original price, then the site discount (``onsite``),
then a time-limited site promo (``onsite_promo``) with an end time. Every buyer
gets both without a code, so ``price_rub`` is the last step of the chain; a step
that needs a promo code, or of an unknown kind, ends the chain with a warning.

Verified live 2026-09-27 from a home address: search 200, card 200, a bare
``/p/<sku>/`` answers 301 to the canonical address, a nonsense query returns an
honest empty page. A 401/403/418/429, any other redirect or an anti-bot page
stops Lamoda for 30 minutes.

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

from lamoda_connector.models_output import (
    LamodaCardResponse,
    LamodaSearchItemOut,
    LamodaSearchResponse,
    LamodaSelfcheckResponse,
    LamodaSizeOut,
    MetaOut,
)
from lamoda_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.lamoda.ru"
_HOSTS = frozenset({"lamoda.ru", "www.lamoda.ru"})

# SKUs look like MP002XM1RMM3: two letters, alphanumerics, 8-20 chars. URLs
# carry them lowercased, so match case-insensitively and normalise to upper.
_SKU_RE = re.compile(r"\b([a-zA-Z]{2}[a-zA-Z0-9]{6,18})\b")
_CARD_PATH_RE = re.compile(r"^/p/([a-zA-Z]{2}[a-zA-Z0-9]{6,18})(/[a-z0-9-]*)?/?$")
_PAYLOAD_RE = re.compile(r'\bpayload:\s*(?=\{")')
_WALL_RE = re.compile(r"servicepipe|captcha|ddos-guard|qrator|доступ ограничен|не робот", re.IGNORECASE)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

_APPLIED_DISCOUNTS = frozenset({"onsite", "onsite_promo"})
_CARD_STEPS = ("original", "onsite", "action")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

mcp = FastMCP(
    name="lamoda-connector",
    version=SERVER_VERSION,
    instructions=(
        "Lamoda fashion catalog over plain HTTP: lamoda_search reads the site's search page, "
        "lamoda_card a product page by SKU or URL. price_rub already includes the site promo "
        "anyone gets; promo_until says when it ends."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_settings.min_gap, source="lamoda")


def _proxy() -> str | None:
    return (_settings.proxy.get_secret_value() or "").strip() or proxy_from_env("LAMODA_PROXY")


def _extract_sku(raw: str) -> str | None:
    """Pull a SKU out of a lamoda.ru URL or a bare SKU string, uppercased."""
    match = _SKU_RE.search(raw.strip())
    return match.group(1).upper() if match else None


def _card_url(sku_or_url: str) -> tuple[str, str]:
    """The SKU and the page to fetch: the URL's own path when given, else ``/p/<sku>/``."""
    raw = sku_or_url.strip()
    if "/" not in raw:
        sku = _extract_sku(raw)
        if sku is None or sku != raw.upper():
            raise_tool_error(BadRequestError(f"could not read a SKU from {raw!r}; pass MP… or a lamoda.ru product URL"))
        return sku, f"{SITE_BASE}/p/{sku.lower()}/"
    parsed = urllib.parse.urlsplit(raw if "://" in raw else f"https://{raw.lstrip('/')}")
    match = _CARD_PATH_RE.match(parsed.path)
    if parsed.hostname not in _HOSTS or match is None:
        raise_tool_error(BadRequestError(f"not a lamoda.ru product URL: {raw!r}; pass MP… or /p/<sku>/…"))
    return match.group(1).upper(), f"{SITE_BASE}{parsed.path}"


def _product_url(sku: str, seo_tail: Any) -> str:
    tail = str(seo_tail or "").strip("/")
    return f"{SITE_BASE}/p/{sku.lower()}/{tail}/" if tail else f"{SITE_BASE}/p/{sku.lower()}/"


async def _get(url: str, label: str) -> Reply:
    try:
        return await shop_request(
            url,
            pacer=_pacer,
            provider="lamoda",
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
            f"{label}: {detail}; Lamoda is stopped for 30 min, do not retry",
            provider="lamoda",
            challenge_type=challenge_type,
            status_code=status,
        )
    )


def _nuxt_payload(html: str) -> dict[str, Any] | None:
    match = _PAYLOAD_RE.search(html)
    if match is None:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _page_payload(reply: Reply, label: str) -> dict[str, Any]:
    if reply.status in _REDIRECTS:
        _refuse(
            label,
            f"http {reply.status}",
            f"redirect to {reply.location[:80]!r} instead of the page",
            status=reply.status,
            challenge_type="redirect",
        )
    if reply.status == 404:
        raise_tool_error(NotFoundError(f"{label}: Lamoda has no such page", provider="lamoda"))
    if reply.status != 200:
        raise_tool_error(
            TransportDownError(f"{label}: unexpected HTTP {reply.status}", provider="lamoda", status_code=reply.status)
        )
    html = reply.text or ""
    data = _nuxt_payload(html)
    if data is None:
        if _WALL_RE.search(html):
            _refuse(
                label,
                "anti-bot page",
                "anti-bot page instead of the catalog",
                status=200,
                challenge_type="antibot_page",
            )
        raise_tool_error(ParserDriftError(f"{label}: no __NUXT__ payload on the page", provider="lamoda"))
    _pacer.record_success()
    return data


def _price_chain(prices: Any) -> tuple[float | None, float | None, str | None, list[str]]:
    """``(price, price before discounts, promo end, warnings)`` from a search list or a card dict."""
    warnings: list[str] = []
    if isinstance(prices, dict):
        unknown = sorted(set(prices) - set(_CARD_STEPS))
        if unknown:
            warnings.append(f"price_step_skipped: unknown price kinds {unknown}")
        steps = [prices[k] for k in _CARD_STEPS if isinstance(prices.get(k), dict)]
    else:
        steps = [s for s in prices if isinstance(s, dict)] if isinstance(prices, list) else []
    base = price = None
    until: str | None = None
    for step in steps:
        value = R.coerce_price(step.get("price"))
        discount = step.get("discount")
        if not isinstance(discount, dict):
            if base is None:
                base = price = value
            continue
        kind = str(discount.get("type") or "")
        if kind not in _APPLIED_DISCOUNTS or discount.get("promocode"):
            warnings.append(f"price_step_skipped: {kind or 'untyped'} discount needs a code or is unknown")
            break
        price = value
        until = str(discount["active_to"]) if discount.get("active_to") else until
    old = base if base is not None and price is not None and base > price else None
    return price, old, until, warnings


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _title(name: Any, model: Any) -> str | None:
    return " ".join(str(part).strip() for part in (name, model) if part and str(part).strip()) or None


def _search_item(raw: dict[str, Any]) -> tuple[LamodaSearchItemOut, list[str]]:
    price, old, until, warnings = _price_chain(raw.get("prices"))
    rating = _as_dict(raw.get("rating"))
    reviews = R.coerce_int(rating.get("reviews_count"))
    average = R.coerce_price(rating.get("average_rating"))
    sizes = [s for s in raw.get("sizes") or [] if isinstance(s, dict)]
    sellable = raw.get("is_sellable")
    if not isinstance(sellable, bool):
        in_stock = None
    elif sizes:
        in_stock = sellable and any(s.get("is_available") for s in sizes)
    else:
        in_stock = sellable
    sku = str(raw.get("sku") or "")
    item = LamodaSearchItemOut(
        sku=sku or None,
        title=_title(raw.get("name"), raw.get("model_name")),
        brand=_as_dict(raw.get("brand")).get("name"),
        price_rub=price,
        old_price_rub=old,
        promo_until=until,
        # Search ratings are on a 0..100 scale; the card gives the same value 0..5.
        rating=R.coerce_rating(round(average / 20, 2)) if average and reviews else None,
        review_count=reviews,
        in_stock=in_stock,
        url=_product_url(sku, raw.get("seo_tail")) if sku else None,
    )
    return item, [f"{sku}: {w}" for w in warnings]


@mcp.tool(
    name="lamoda_search",
    annotations=ToolAnnotations(
        title="Lamoda Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def lamoda_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'кроссовки nike'")],
    limit: Annotated[int, Field(default=20, ge=1, le=60, description="Items to return from the first page.")] = 20,
    ctx: Context | None = None,
) -> LamodaSearchResponse:
    """Search Lamoda the way the site does, first page by relevance.

    ## Return Format

    LamodaSearchResponse: {status, query, tier_used, total_available, count, items[], meta}.
    Items carry sku, title, brand, price_rub (site promo included, None when absent —
    never 0), old_price_rub, promo_until, rating, review_count, in_stock, url.

    ## Error Format

    ToolError: challenge_required on a redirect or anti-bot page (Lamoda stops for
    30 min); transport_down on other failures; parser_drift when the page payload moved.
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    url = f"{SITE_BASE}/catalogsearch/result/?q={urllib.parse.quote(text)}"
    log_event("lamoda_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"lamoda_search: {text!r}")

    async def fetch() -> dict[str, Any]:
        return _page_payload(await _get(url, "lamoda_search"), "lamoda_search")

    data = await _cache.get_or_fetch(url, fetch)
    products = data.get("products")
    if not isinstance(products, list):
        raise_tool_error(ParserDriftError("lamoda_search: payload.products is missing", provider="lamoda"))
    warnings: list[str] = []
    items: list[LamodaSearchItemOut] = []
    for raw in products[:limit]:
        if not isinstance(raw, dict):
            continue
        item, item_warnings = _search_item(raw)
        if item.sku is None:
            warnings.append("row_skipped: a search row came without a sku")
            continue
        if item.price_rub is None and not item_warnings:
            item_warnings.append(f"{item.sku}: no_price")
        items.append(item)
        warnings.extend(item_warnings)
    meta = _as_dict(data.get("products_meta"))
    if meta.get("is_predicted_term_corrected"):
        warnings.insert(0, f"query_corrected: Lamoda searched for {meta.get('predicted_term')!r} instead")
    if not items:
        warnings.insert(0, f"empty: Lamoda found nothing for {text!r}")
    elif all(it.price_rub is None for it in items):
        warnings.insert(0, "no_price: search rows came back without prices")
    log_event("lamoda_search.done", query=text[:60], returned=len(items))
    result = LamodaSearchResponse(
        query=text,
        tier_used="http",
        total_available=R.coerce_int(_as_dict(data.get("pagination")).get("found")),
        count=len(items),
        items=items,
    )
    result.meta = MetaOut(source="lamoda_search", healthy=not warnings, warnings=warnings)
    return result


@mcp.tool(
    name="lamoda_card",
    annotations=ToolAnnotations(
        title="Lamoda Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def lamoda_card(
    sku_or_url: Annotated[
        str, Field(min_length=1, max_length=300, description="SKU (MP002XM1RMM3) or lamoda.ru product URL")
    ],
    ctx: Context | None = None,
) -> LamodaCardResponse:
    """Fetch one Lamoda product page. A bare SKU costs one extra request (301 to the canonical URL).

    ## Return Format

    LamodaCardResponse: {status, sku, title, brand, price_rub, old_price_rub, promo_until,
    rating, review_count, seller, is_available, sizes[], url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request when no SKU can be read; not_found for an unknown SKU;
    challenge_required on a foreign redirect or anti-bot page; parser_drift when the
    page payload moved.
    """
    log_event("lamoda_card.start", input=sku_or_url[:80])
    sku, url = _card_url(sku_or_url)

    async def fetch() -> dict[str, Any]:
        reply = await _get(url, "lamoda_card")
        if reply.status in _REDIRECTS:
            target = urllib.parse.urljoin(SITE_BASE, reply.location)
            parsed = urllib.parse.urlsplit(target)
            match = _CARD_PATH_RE.match(parsed.path)
            if parsed.hostname in _HOSTS and match is not None and match.group(1).upper() == sku:
                reply = await _get(f"{SITE_BASE}{parsed.path}", "lamoda_card")
        return _page_payload(reply, "lamoda_card")

    data = await _cache.get_or_fetch(f"card:{sku}", fetch)
    product = data.get("product")
    if not isinstance(product, dict) or str(product.get("sku") or "").upper() != sku:
        raise_tool_error(ParserDriftError(f"lamoda_card: payload.product for {sku} is missing", provider="lamoda"))
    price, old, until, warnings = _price_chain(product.get("prices"))
    reviews = R.coerce_int(_as_dict(product.get("counters")).get("reviews"))
    sizes = [
        LamodaSizeOut(
            size=str(s.get("title") or "") or None, is_available=(R.coerce_int(s.get("stock_quantity")) or 0) > 0
        )
        for s in product.get("sizes") or []
        if isinstance(s, dict)
    ]
    in_stock = product.get("is_in_stock")
    if price is None:
        warnings.insert(0, "no_price: the product page carries no price")
    log_event("lamoda_card.done", sku=sku, price=price, warnings=len(warnings))
    card = LamodaCardResponse(
        sku=sku,
        title=_title(product.get("title"), product.get("model_title")),
        brand=_as_dict(product.get("brand")).get("title"),
        price_rub=price,
        old_price_rub=old,
        promo_until=until,
        rating=R.coerce_rating(product.get("average_rating")) if reviews else None,
        review_count=reviews,
        seller=_as_dict(product.get("seller")).get("title"),
        is_available=in_stock if isinstance(in_stock, bool) else None,
        sizes=sizes,
        url=_product_url(sku, product.get("seo_tail")),
        tier_used="http",
    )
    card.meta = MetaOut(source="lamoda_card", healthy=not warnings, warnings=warnings)
    return card


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def lamoda_selfcheck(ctx: Context | None = None) -> LamodaSelfcheckResponse:
    """Drift canary: one search, then the card of its first row (two paced requests).

    A refusal or network fault is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("lamoda_selfcheck.start")
    try:
        result = await _lamoda_selfcheck_impl(ctx)
        log_event("lamoda_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("lamoda_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"lamoda_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _lamoda_selfcheck_impl(ctx: Context | None) -> LamodaSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(60):
            found = await lamoda_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="nuxt-search-v1", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="nuxt-search-v1", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(state, baseline="nuxt-search-v1", reason=reason, notes=[str(exc)[:160]])

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="nuxt-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await lamoda_card(first_url, ctx=ctx)
            state = "healthy" if card.price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state,
                baseline="nuxt-card-v1",
                reason=None if state == "healthy" else "parse_smoke_failed",
                notes=[f"price {card.price_rub}"],
            )
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(state, baseline="nuxt-card-v1", reason=reason, notes=[str(exc)[:160]])

    result_dict = R.selfcheck_result(
        "lamoda",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return LamodaSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
