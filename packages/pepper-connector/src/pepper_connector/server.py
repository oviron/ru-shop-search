"""Pepper (pepper.ru) MCP connector: community deals, read in the plugin's Chrome tab.

A deal is a user's post about a price at some shop, not a shop offer: its price is
what the poster saw, often with conditions in the title (subscription, wallet,
bank card, import), and old deals stay listed. Plain HTTP meets a Yandex
SmartCaptcha redirect (307 ``?_ycch=1``); the tab opens the site's own pages.

Search: ``/selective_search?keyword=…&search_list=deals``, deals are ``article``
blocks. Card: the deal page's schema.org JSON-LD (Product with an Offer, Article
with ``datePublished``). Any 401/403/418/429, a captcha or a redirect off
pepper.ru stops Pepper for 30 minutes.

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
from mcp_core.query_match import names_query
from mcp_core.redact import redact_error_text as _redact
from mcp_core.stopgate import SourceStoppedError
from mcp_core.transport.chrome_cdp import NavBlocked, NavigationPolicyError, open_page
from pydantic import Field

from pepper_connector.models_output import (
    MetaOut,
    PepperCardResponse,
    PepperDealOut,
    PepperSearchResponse,
    PepperSelfcheckResponse,
)
from pepper_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.pepper.ru"
SEARCH_URL = f"{SITE_BASE}/selective_search"
_HOSTS = frozenset({"pepper.ru", "www.pepper.ru"})
MAX_DEALS = 30

_min_gap = _settings.min_gap
_DEAL_PATH_RE = re.compile(r"^/deals/[a-z0-9-]*[a-z0-9]-(\d{3,9})/?$")
_TEMPERATURE_RE = re.compile(r"^(-?\d+)\s*°$")
_TOTAL_RE = re.compile(r"\((\d+)\)")
_REFUSAL_STATUSES = frozenset({401, 403, 418, 429})
_GONE = frozenset({404, 410})
_RENDER_WAIT_S = 10.0
_POLL_S = 0.5

mcp = FastMCP(
    name="pepper-connector",
    version=SERVER_VERSION,
    instructions=(
        "Pepper (pepper.ru, community deals): pepper_search lists deals for a query, pepper_card "
        "reads one deal page. A deal price is what a user reported for a shop when posting — "
        "check it at the shop before quoting; it is never a shop price."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap, source="pepper")
_cdp_lock = asyncio.Lock()

# `_ycch` is SmartCaptcha's JS check, which the tab passes by itself; a captcha page or form is not.
_CAPTCHA_JS = """
    const checking = /[?&]_ycch=/.test(document.location.search);
    const captcha = /captcha/i.test(document.location.pathname)
        || !!document.querySelector('#checkbox-captcha-form, .CheckboxCaptcha, .AdvancedCaptcha, iframe[src*="captcha"]');
"""

# Author names and avatars stay on the page: only deal fields leave the tab.
_SEARCH_EXTRACT_JS = (
    """
() => {
    const text = el => el ? ((el.textContent || '').replace(/\\s+/g, ' ').trim() || null) : null;
"""
    + _CAPTCHA_JS
    + """
    const content = document.getElementById('search-results-content');
    const deals = content ? [...content.querySelectorAll('article[data-gtm-deal-id]')].slice(0, MAX_DEALS) : [];
    return JSON.stringify({
        title: document.title || '',
        captcha,
        checking,
        results: !!content,
        nothing: !!(content && content.querySelector('.empty_state')),
        total: text(document.querySelector('#results-count .loading-filters-complete')),
        deals: deals.map(a => {
            const link = a.querySelector('.custom-card-title a');
            const row = link ? link.closest('.custom-card-title').nextElementSibling : null;
            const pick = sel => row ? text(row.querySelector(':scope > ' + sel)) : null;
            return {
                deal_id: a.getAttribute('data-gtm-deal-id'),
                store: a.getAttribute('data-gtm-store'),
                href: link ? link.getAttribute('href') : null,
                title: text(link),
                price: pick('div.text-primary'),
                old: pick('div.line-through'),
                stamp: text(a.querySelector('span.text-xs')),
                hot: text(a.querySelector('[class*="hotness_value_"]')),
            };
        }),
    });
}
""".replace("MAX_DEALS", str(MAX_DEALS))
)

_CARD_EXTRACT_JS = (
    """
() => {
    const text = el => el ? ((el.textContent || '').replace(/\\s+/g, ' ').trim() || null) : null;
"""
    + _CAPTCHA_JS
    + """
    const docs = [...document.querySelectorAll('script[type="application/ld+json"]')]
        .map(s => { try { return JSON.parse(s.textContent); } catch (e) { return null; } })
        .filter(d => d && typeof d === 'object');
    const products = docs.filter(d => d['@type'] === 'Product');
    const articles = docs.filter(d => d['@type'] === 'Article');
    const product = products.length === 1 ? products[0] : {};
    const offer = product.offers && typeof product.offers === 'object' ? product.offers : {};
    return JSON.stringify({
        title: document.title || '',
        captcha,
        checking,
        products: products.length,
        name: product.name || null,
        product_url: product.url || null,
        merchant: product.brand && product.brand.name ? product.brand.name : null,
        price: offer.price == null ? null : String(offer.price),
        currency: offer.priceCurrency || null,
        valid_until: offer.priceValidUntil || null,
        availability: offer.availability || null,
        published: articles.length === 1 ? (articles[0].datePublished || null) : null,
        h1: text(document.querySelector('h1')),
        hot: text(document.querySelector('[class*="hotness_value_"]')),
        expired_notice: /срок действия этой скидки истек/i.test(document.body ? document.body.textContent : ''),
    });
}
"""
)


def _deal_path(url: str) -> tuple[str, str] | None:
    """(deal id, canonical URL) for a pepper.ru deal page, else None."""
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, url))
    match = _DEAL_PATH_RE.match(parsed.path)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in _HOSTS
        or parsed.username is not None
        or parsed.port is not None
        or match is None
    ):
        return None
    return match.group(1), f"{SITE_BASE}{parsed.path.rstrip('/')}"


def _temperature(raw: Any) -> int | None:
    match = _TEMPERATURE_RE.match(str(raw or "").strip())
    return int(match.group(1)) if match else None


def _parse_extract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > _settings.max_body_bytes:
        raise_tool_error(TransportDownError("extracted page data missing or over the body cap", provider="pepper"))
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise_tool_error(ParserDriftError("page extractor returned a non-object payload", provider="pepper"))
    return data


def _refuse(reason: str, error: ConnectorError) -> NoReturn:
    _pacer.record_refusal(reason)
    raise_tool_error(error)


def _captcha(what: str = "showed a captcha") -> ChallengeRequiredError:
    return ChallengeRequiredError(
        f"Pepper {what} in the plugin's tab; Pepper is stopped for 30 min, do not retry. "
        "If the profile window shows it, it may be passed by hand, then `marketplace-mcp resume pepper`.",
        provider="pepper",
        challenge_type="smartcaptcha",
        status_code=403,
    )


def _blocked(status: int) -> TransportDownError:
    return TransportDownError(
        f"Pepper refused the browser tab (HTTP {status}); Pepper is stopped for 30 min, do not retry.",
        provider="pepper",
        status_code=status,
    )


def _search_ready(data: dict[str, Any]) -> bool:
    return bool(data.get("captcha") or (not data.get("checking") and (data.get("deals") or data.get("nothing"))))


def _card_ready(data: dict[str, Any]) -> bool:
    return bool(data.get("captcha") or (not data.get("checking") and data.get("products")))


async def _cdp_render(url: str, ready: Any, js: str) -> dict[str, Any]:
    """Open ``url`` in the plugin's Chrome and extract once ``ready`` holds or the deadline passes."""
    async with _cdp_lock:
        try:
            await _pacer.wait(min_gap=_min_gap)
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(url, wait_ms=0, allowed_hosts=_HOSTS) as page:
                deadline = asyncio.get_running_loop().time() + _RENDER_WAIT_S
                while True:
                    data = _parse_extract(await asyncio.wait_for(page.evaluate(js), timeout=30.0))
                    if ready(data) or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(_POLL_S)
        except NavBlocked as exc:
            if exc.status in _REFUSAL_STATUSES:
                _refuse(f"http {exc.status}", _blocked(exc.status))
            if exc.status in _GONE:
                _pacer.record_success()
                raise_tool_error(NotFoundError("Pepper has no such page", provider="pepper"))
            raise_tool_error(
                TransportDownError(f"Pepper page failed (HTTP {exc.status})", provider="pepper", status_code=exc.status)
            )
        except NavigationPolicyError:
            _refuse("redirect off pepper.ru", _blocked(403))
    if data.get("captcha"):
        _refuse("captcha", _captcha())
    if data.get("checking"):
        _refuse("js check did not pass", _captcha("kept its SmartCaptcha JS check"))
    title = str(data.get("title") or "")
    if not ready(data) and title and "pepper" not in title.lower():
        _refuse("block page in the tab", _blocked(403))
    if not ready(data) and not title:
        raise_tool_error(TransportDownError("Pepper page did not load in time", provider="pepper"))
    _pacer.record_success()
    return data


def _deal_from(raw: dict[str, Any]) -> tuple[PepperDealOut | None, str | None]:
    found = _deal_path(str(raw.get("href") or ""))
    if found is None or found[0] != str(raw.get("deal_id") or ""):
        return None, "row_skipped: a deal came without a link to its own page"
    stamp = str(raw.get("stamp") or "")
    expired = stamp == "Истекло"
    price = R.coerce_price(raw.get("price"))
    old = R.coerce_price(raw.get("old"))
    return (
        PepperDealOut(
            deal_id=found[0],
            title=raw.get("title") or None,
            merchant=raw.get("store") or None,
            deal_price_rub=price,
            old_price_rub=old if old is not None and price is not None and old > price else None,
            posted=None if expired or not stamp else stamp,
            expired=expired,
            temperature=_temperature(raw.get("hot")),
            url=found[1],
        ),
        None,
    )


@mcp.tool(
    name="pepper_search",
    annotations=ToolAnnotations(
        title="Pepper Deals Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def pepper_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'rtx 5070'")],
    limit: Annotated[int, Field(default=20, ge=1, le=MAX_DEALS, description="Deals to return.")] = 20,
    ctx: Context | None = None,
) -> PepperSearchResponse:
    """Search Pepper deals the way the site does, in the site's order.

    Deals are community posts about prices at other shops, old and expired ones
    included: deal_price_rub is what the poster saw, never a shop price. When no
    deal title names the query the deals are dropped with a ``no_match`` warning.

    ## Return Format

    PepperSearchResponse: {status, query, tier_used, total_available, count, items[],
    meta}. Items carry deal_id, title, merchant, deal_price_rub (None when absent —
    never 0), old_price_rub, posted, expired, temperature, url.

    ## Error Format

    ToolError: challenge_required on a captcha and transport_down with source_stopped
    on 401/403/418/429 or a redirect off pepper.ru (Pepper stops for 30 min);
    parser_drift when a rendered page has neither deals nor «ничего не нашлось».
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    log_event("pepper_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"pepper_search: {text!r}")
    try:
        url = f"{SEARCH_URL}?{urllib.parse.urlencode({'keyword': text, 'search_list': 'deals'})}"
        cached = _cache.get(url)
        if isinstance(cached, dict):
            payload, tier = cached, "cache"
        else:
            payload, tier = await _cdp_render(url, _search_ready, _SEARCH_EXTRACT_JS), "cdp"
        rows = [row for row in payload.get("deals") or [] if isinstance(row, dict)]
        if not rows and not payload.get("nothing"):
            raise_tool_error(
                ParserDriftError("rendered search page has neither deals nor «ничего не нашлось»", provider="pepper")
            )
        if tier == "cdp":
            _cache.set(url, payload)
        warnings: list[str] = []
        parsed: list[PepperDealOut] = []
        for row in rows:
            deal, warning = _deal_from(row)
            if deal is not None:
                parsed.append(deal)
            elif warning is not None:
                warnings.append(warning)
        total_match = _TOTAL_RE.search(str(payload.get("total") or ""))
        total = int(total_match.group(1)) if total_match else None
        items: list[PepperDealOut] = []
        if not rows:
            warnings.append(f"empty: Pepper has no deals for {text!r}")
            total = 0
        elif parsed and not names_query(text, [deal.title or "" for deal in parsed]):
            warnings.append(f"no_match: Pepper listed {len(parsed)} deals, none names {text!r} — dropped as unrelated")
            total = 0
        else:
            items = parsed[:limit]
        result = PepperSearchResponse(query=text, tier_used=tier, total_available=total, count=len(items), items=items)
        result.meta = MetaOut(source="pepper_search", healthy=not warnings, warnings=warnings)
        log_event("pepper_search.done", query=text[:60], returned=len(items))
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("pepper_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"pepper_search failed: {exc}"), provider="pepper"))


def _card_fields(data: dict[str, Any], deal_id: str) -> dict[str, Any]:
    if data.get("products") != 1:
        raise_tool_error(
            ParserDriftError(
                f"pepper_card: expected one schema.org Product, got {data.get('products')}", provider="pepper"
            )
        )
    found = _deal_path(str(data.get("product_url") or ""))
    if found is None or found[0] != deal_id:
        raise_tool_error(ParserDriftError(f"pepper_card: the Product is not deal {deal_id}", provider="pepper"))
    availability = str(data.get("availability") or "")
    return {
        "title": data.get("h1") or data.get("name"),
        "merchant": data.get("merchant"),
        "price": R.coerce_price(data.get("price")) if data.get("currency") in (None, "RUB") else None,
        "published_at": data.get("published"),
        "valid_until": data.get("valid_until"),
        "expired": bool(data.get("expired_notice")) or availability.endswith("/OutOfStock"),
        "temperature": _temperature(data.get("hot")),
        "url": found[1],
    }


@mcp.tool(
    name="pepper_card",
    annotations=ToolAnnotations(
        title="Pepper Deal Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def pepper_card(
    url: Annotated[
        str,
        Field(min_length=1, max_length=400, description="Deal URL from pepper_search (/deals/<slug>-<id>)"),
    ],
    ctx: Context | None = None,
) -> PepperCardResponse:
    """Read one Pepper deal page in the plugin's Chrome tab.

    ## Return Format

    PepperCardResponse: {status, deal_id, title, merchant, deal_price_rub,
    published_at, valid_until, expired, temperature, url, tier_used, meta}.
    deal_price_rub is what the poster saw at the merchant, never a shop price.

    ## Error Format

    ToolError: bad_request for a foreign URL; not_found for a deal the site does not
    have; challenge_required on a captcha and transport_down with source_stopped on
    401/403/418/429 or a redirect off pepper.ru; parser_drift when the JSON-LD moved.
    """
    log_event("pepper_card.start", input=url[:80])
    try:
        raw = url.strip()
        target = _deal_path(raw if "://" in raw or raw.startswith("/") else f"https://{raw}")
        if target is None:
            raise_tool_error(
                BadRequestError(f"not a pepper.ru deal page: {url[:120]!r}; pass the URL from pepper_search")
            )
        deal_id, page_url = target
        cached = _cache.get(f"card:{deal_id}")
        if isinstance(cached, dict):
            fetched, tier = cached, "cache"
        else:
            fetched, tier = _card_fields(await _cdp_render(page_url, _card_ready, _CARD_EXTRACT_JS), deal_id), "cdp"
            _cache.set(f"card:{deal_id}", fetched)
        warnings: list[str] = []
        if fetched["price"] is None:
            warnings.append("no_price: the deal page names no price")
        if fetched["expired"]:
            warnings.append("expired: the site says this deal has ended")
        log_event("pepper_card.done", deal_id=deal_id, price=fetched["price"], warnings=len(warnings))
        card = PepperCardResponse(
            deal_id=deal_id,
            title=fetched["title"],
            merchant=fetched["merchant"],
            deal_price_rub=fetched["price"],
            published_at=fetched["published_at"],
            valid_until=fetched["valid_until"],
            expired=fetched["expired"],
            temperature=fetched["temperature"],
            url=fetched["url"],
            tier_used=tier,
        )
        card.meta = MetaOut(source="pepper_card", healthy=not warnings, warnings=warnings)
        return card
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("pepper_card.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"pepper_card failed: {exc}"), provider="pepper"))


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def pepper_selfcheck(ctx: Context | None = None) -> PepperSelfcheckResponse:
    """Drift canary: one tab search, then the card of its first deal.

    A refusal or network fault is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("pepper_selfcheck.start")
    try:
        result = await _pepper_selfcheck_impl(ctx)
        log_event("pepper_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("pepper_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"pepper_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _pepper_selfcheck_impl(ctx: Context | None) -> PepperSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(90):
            found = await pepper_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        if found.items:
            first_url = found.items[0].url
            checks["search"] = R.selfcheck_entry("healthy", baseline="tab-deals-v1", notes=[f"{found.count} deals"])
        else:
            checks["search"] = R.selfcheck_entry(
                "inconclusive", baseline="tab-deals-v1", reason="skipped", notes=["no deals for the query"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(state, baseline="tab-deals-v1", reason=reason, notes=[str(exc)[:160]])

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="jsonld-deal-v1", reason="skipped", notes=["no deal to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await pepper_card(first_url, ctx=ctx)
            state = "healthy" if card.deal_price_rub is not None else "drift"
            checks["card"] = R.selfcheck_entry(
                state,
                baseline="jsonld-deal-v1",
                reason=None if state == "healthy" else "parse_smoke_failed",
                notes=[f"price {card.deal_price_rub}"],
            )
        except ToolError as exc:
            state, reason = _probe_verdict(exc)
            checks["card"] = R.selfcheck_entry(state, baseline="jsonld-deal-v1", reason=reason, notes=[str(exc)[:160]])

    result_dict = R.selfcheck_result(
        "pepper",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return PepperSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
