"""Nix (nix.ru) MCP connector: search in the plugin's Chrome tab, product cards over HTTP.

Search results come from an XHR (``FastSearch/goods``) signed with the session and
the page's own JS, so plain HTTP only gets an unfiltered category list. The tab
renders ``/price/search_panel_ajax.html#keywords=…`` and the extractor hands back
the result rows' markup, parsed here. Cards are ordinary windows-1251 pages with
schema.org microdata. The price is the «до» column / ``itemprop="price"``;
СУПЕРЦЕНА (the «от» column) needs prepayment and store pickup and is left out.

Verified live 2026-09-27 from a home address: tab search and card 200 without
cookies, a missing product 404. Any 401/403/418/429, foreign redirect or anti-bot
page stops Nix for 30 minutes.

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
from mcp_core.stopgate import SourceStoppedError
from mcp_core.transport import proxy_from_env
from mcp_core.transport.chrome_cdp import NavBlocked, open_page
from mcp_core.transport.impersonated import Reply, shop_request
from pydantic import Field

from nix_connector.models_output import (
    MetaOut,
    NixCardResponse,
    NixSearchItemOut,
    NixSearchResponse,
    NixSelfcheckResponse,
)
from nix_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.nix.ru"
SEARCH_URL = f"{SITE_BASE}/price/search_panel_ajax.html"
_HOSTS = frozenset({"nix.ru", "www.nix.ru"})
MAX_ROWS = 50

_min_gap = _settings.min_gap
_CARD_PATH_RE = re.compile(r"^/autocatalog/(?:[A-Za-z0-9_+-]+/){1,4}[A-Za-z0-9_.+-]*_(\d{3,9})\.html$")
_ROW_LINK_RE = re.compile(r'<a class="t" href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_ROW_TERM_RE = re.compile(r'id="sup_\d+">([^<]*)<')
_ROW_PRICE_RE = re.compile(r'class="d tar cell-half-price"[^>]*>\s*<span[^>]*>([^<]*)</span>')
_SUPERPRICE_RE = re.compile(r'class="superprice[^"]*">.*?<span class="nowrap">([^<]*)</span>', re.S)
_TERM_RE = re.compile(r"^(?:ЕСТЬ|ЗАВТРА|ПОСЛЕЗАВТРА|\d{2}\.\d{2})$")
_TAG_RE = re.compile(r"<[^>]+>")
_PRODUCT_RE = re.compile(r'itemtype="https?://schema\.org/Product"')
_OFFER_RE = re.compile(r'itemtype="https?://schema\.org/Offer"')
_OFFER_SPAN = 4000
_WALL_RE = re.compile(r"captcha|ddos-guard|qrator|servicepipe|доступ ограничен|не робот", re.IGNORECASE)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_GONE = frozenset({404, 410})
_OFFER_STOCK = {"InStock": True, "OutOfStock": False, "SoldOut": False, "Discontinued": False}
_REFUSAL_STATUSES = frozenset({401, 403, 418, 429})
_RENDER_WAIT_MS = 10000

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

mcp = FastMCP(
    name="nix-connector",
    version=SERVER_VERSION,
    instructions=(
        "Nix (nix.ru, computers and electronics): nix_search reads the site's search in the "
        "plugin's Chrome tab, nix_card a product page by its URL over HTTP. Prices are for the "
        "city the site shows, returned as city."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap, source="nix")
_cdp_lock = asyncio.Lock()

# Rows leave the page as markup (compare icons cut). ``answered``: .fts-keywords gets the
# query only once the XHR is in, while «ничего не найдено» shows before it (live, 2026-09-27).
# ``groups`` are the site's category chips for the query («Видеокарты» for «видеокарта»).
_SEARCH_EXTRACT_JS = """
() => {
    const text = el => el ? ((el.textContent || '').replace(/\\s+/g, ' ').trim() || null) : null;
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const none = document.querySelector('.fs-fts-total-label-none');
    const rows = [...document.querySelectorAll('#search_results tr.search-result-row')].slice(0, MAX_ROWS);
    const asked = new URLSearchParams(location.hash.slice(1)).get('keywords');
    return JSON.stringify({
        title: document.title || '',
        city: text(document.getElementById('toolbar-city-change-name')),
        answered: !!asked && norm(text(document.querySelector('.fts-keywords'))) === norm(asked),
        total: text(document.querySelector('.fs-fts-total-label .fts-total')),
        nothing: !!none && !none.classList.contains('order-hide'),
        groups: [...document.querySelectorAll('.fts-header-group[id^="fts-cat-"]')].map(text).filter(g => g && g !== 'Все категории'),
        rows: rows.map(r => { const c = r.cloneNode(true); c.querySelectorAll('svg').forEach(s => s.remove()); return c.outerHTML; }),
    });
}
""".replace("MAX_ROWS", str(MAX_ROWS))


def _proxy() -> str | None:
    return (_settings.proxy.get_secret_value() or "").strip() or proxy_from_env("NIX_PROXY")


def _text(raw: str | None) -> str | None:
    if raw is None:
        return None
    return " ".join(htmllib.unescape(_TAG_RE.sub("", raw)).split()) or None


def _price(raw: str | None) -> float | None:
    return R.coerce_price(htmllib.unescape(raw)) if raw else None


def _card_path(url: str) -> tuple[str, str] | None:
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(SITE_BASE, url))
    match = _CARD_PATH_RE.match(parsed.path)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _HOSTS or match is None:
        return None
    return match.group(1), f"{SITE_BASE}{parsed.path}"


def _search_item(row: str) -> NixSearchItemOut:
    link = _ROW_LINK_RE.search(row)
    card = _card_path(link.group(1)) if link else None
    term_match = _ROW_TERM_RE.search(row)
    term = _text(term_match.group(1)) if term_match else None
    cells = [_text(c) for c in _ROW_PRICE_RE.findall(row)]
    # Two cells are «от» (СУПЕРЦЕНА) and «до» (the price); a single price fills only «от».
    price = (_price(cells[1]) if cells[1] else _price(cells[0])) if len(cells) == 2 else None
    superprice = _price(cells[0]) if len(cells) == 2 and cells[1] else None
    return NixSearchItemOut(
        product_id=card[0] if card else None,
        title=_text(link.group(2)) if link else None,
        price_rub=price,
        superprice_rub=superprice,
        availability=term,
        in_stock=True if term and _TERM_RE.match(term) else None,
        url=card[1] if card else None,
    )


def _parse_extract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > _settings.max_body_bytes:
        raise_tool_error(TransportDownError("extracted page data missing or over the body cap", provider="nix"))
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise_tool_error(ParserDriftError("page extractor returned a non-object payload", provider="nix"))
    return data


def _ready(data: dict[str, Any]) -> bool:
    return data.get("answered") is True and bool(data.get("rows") or data.get("nothing"))


def _settled(data: dict[str, Any]) -> bool:
    return _ready(data) and bool(data.get("groups") or not data.get("rows"))


def _is_wall(data: dict[str, Any]) -> bool:
    """Every Nix page title names the shop; a block page would not. No title yet is not a block."""
    title = str(data.get("title") or "").lower()
    return not _ready(data) and bool(title) and "никс" not in title


def _blocked(status: int) -> TransportDownError:
    return TransportDownError(
        f"Nix refused the browser tab (HTTP {status}); Nix is stopped for 30 min, do not retry. "
        "If the profile window shows a check, it may be passed by hand, then `marketplace-mcp resume nix`.",
        provider="nix",
        status_code=status,
    )


async def _cdp_render(url: str) -> dict[str, Any]:
    """Open ``url`` in the plugin's Chrome and extract once the rows or «ничего не найдено» show."""
    async with _cdp_lock:
        try:
            await _pacer.wait(min_gap=_min_gap)
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(url, wait_ms=0) as page:
                deadline = asyncio.get_running_loop().time() + _RENDER_WAIT_MS / 1000
                while True:
                    data = _parse_extract(await asyncio.wait_for(page.evaluate(_SEARCH_EXTRACT_JS), timeout=30.0))
                    if _settled(data) or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.5)
        except NavBlocked as exc:
            if exc.status in _REFUSAL_STATUSES:
                _pacer.record_refusal(f"http {exc.status}")
                raise_tool_error(_blocked(exc.status))
            raise_tool_error(
                TransportDownError(f"Nix page failed (HTTP {exc.status})", provider="nix", status_code=exc.status)
            )
    if _is_wall(data):
        _pacer.record_refusal("block page in the tab")
        raise_tool_error(_blocked(403))
    if not _ready(data) and not data.get("title"):
        raise_tool_error(TransportDownError("Nix search page did not load in time", provider="nix"))
    _pacer.record_success()
    return data


@mcp.tool(
    name="nix_search",
    annotations=ToolAnnotations(
        title="Nix Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def nix_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'rtx 5070'")],
    limit: Annotated[
        int, Field(default=20, ge=1, le=MAX_ROWS, description="Items to return from the first page.")
    ] = 20,
    ctx: Context | None = None,
) -> NixSearchResponse:
    """Search Nix the way the site does, first page in the site's order.

    Nix cuts unknown words down to a prefix and still lists goods; when neither a row
    nor a category chip names the query those rows are dropped with a ``no_match`` warning.

    ## Return Format

    NixSearchResponse: {status, query, tier_used, city, total_available, count, items[],
    meta}. Items carry product_id, title, price_rub (None when absent — never 0;
    СУПЕРЦЕНА is not used), availability, in_stock, url.

    ## Error Format

    ToolError: transport_down with source_stopped when Nix refused the tab (Nix stops for
    30 min); parser_drift when a rendered page has neither rows nor «ничего не найдено».
    """
    text = " ".join(query.split())
    if not text:
        raise_tool_error(BadRequestError("empty search query"))
    log_event("nix_search.start", query=text[:60])
    if ctx is not None:
        await ctx.info(f"nix_search: {text!r}")
    try:
        url = f"{SEARCH_URL}#keywords={urllib.parse.quote(text, safe='')}"
        cached = _cache.get(url)
        if isinstance(cached, dict):
            payload, tier = cached, "cache"
        else:
            payload, tier = await _cdp_render(url), "cdp"
        rows = [r for r in payload.get("rows") or [] if isinstance(r, str)]
        if not _ready(payload):
            raise_tool_error(
                ParserDriftError(
                    "rendered search page never answered the query with rows or «ничего не найдено»", provider="nix"
                )
            )
        if tier == "cdp":
            _cache.set(url, payload)
        parsed = [_search_item(r) for r in rows]
        total = R.coerce_int(re.sub(r"\D", "", str(payload.get("total") or ""))) if rows else 0
        warnings: list[str] = []
        items: list[NixSearchItemOut] = []
        groups = [g for g in payload.get("groups") or [] if isinstance(g, str)]
        if parsed and not names_query(text, [it.title or "" for it in parsed] + groups):
            warnings.append(f"no_match: Nix has no match for {text!r}; its fuzzy results were unrelated and left out")
            total = 0
        else:
            for item in parsed[:limit]:
                if item.product_id is None or item.url is None:
                    warnings.append("row_skipped: a search row came without a product link")
                    continue
                if item.price_rub is None:
                    warnings.append(f"{item.product_id}: no_price")
                items.append(item)
            if not parsed:
                warnings.append(f"empty: Nix found nothing for {text!r}")
            elif items and all(it.price_rub is None for it in items):
                warnings.insert(0, "no_prices_on_page: search rows came back without prices")
        result = NixSearchResponse(
            query=text,
            tier_used=tier,
            city=_text(payload.get("city")) if isinstance(payload.get("city"), str) else None,
            total_available=total,
            count=len(items),
            items=items,
        )
        result.meta = MetaOut(source="nix_search", healthy=not warnings, warnings=warnings)
        log_event("nix_search.done", query=text[:60], returned=len(items))
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("nix_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"nix_search failed: {exc}"), provider="nix"))


async def _get(url: str) -> Reply:
    try:
        return await shop_request(
            url,
            pacer=_pacer,
            provider="nix",
            label="nix_card",
            headers=HEADERS,
            timeout_s=_settings.timeout,
            max_bytes=_settings.max_body_bytes,
            retries=1,
            proxy=_proxy(),
        )
    except ConnectorError as exc:
        raise_tool_error(exc)


def _refuse(reason: str, detail: str, *, status: int, challenge_type: str) -> NoReturn:
    _pacer.record_refusal(reason)
    raise_tool_error(
        ChallengeRequiredError(
            f"nix_card: {detail}; Nix is stopped for 30 min, do not retry",
            provider="nix",
            challenge_type=challenge_type,
            status_code=status,
        )
    )


def _meta(page: str, prop: str) -> str | None:
    match = re.search(rf'itemprop="{prop}" content="([^"]*)"', page)
    return _text(match.group(1)) if match else None


def _card_fields(page: str, product_id: str) -> dict[str, Any]:
    offer_match = _OFFER_RE.search(page)
    if not _PRODUCT_RE.search(page) or offer_match is None:
        raise_tool_error(ParserDriftError("nix_card: no schema.org Product with an Offer on the page", provider="nix"))
    offer = page[offer_match.end() : offer_match.end() + _OFFER_SPAN].split("itemscope", 1)[0]
    offer_url = re.search(r'itemprop="url" href="([^"]+)"', offer)
    found = _card_path(htmllib.unescape(offer_url.group(1))) if offer_url else None
    if found is None or found[0] != product_id:
        raise_tool_error(ParserDriftError(f"nix_card: the offer is not for product {product_id}", provider="nix"))
    currency = _meta(offer, "priceCurrency")
    stock = re.search(r'itemprop="availability" href="https?://schema\.org/(\w+)"', offer)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    city = re.search(r'id="toolbar-city-change-name">([^<]*)<', page)
    superprice = _SUPERPRICE_RE.search(page[: offer_match.start()])
    return {
        "title": _text(h1.group(1)) if h1 else _meta(page, "name"),
        "brand": _meta(page, "brand"),
        "part_number": _meta(page, "sku"),
        "price": _price(_meta(offer, "price")) if currency in (None, "RUB") else None,
        "superprice": _price(_text(superprice.group(1))) if superprice else None,
        "is_available": _OFFER_STOCK.get(stock.group(1)) if stock else None,
        "city": _text(city.group(1)) if city else None,
    }


@mcp.tool(
    name="nix_card",
    annotations=ToolAnnotations(
        title="Nix Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def nix_card(
    url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=400,
            description="Product URL from nix_search (/autocatalog/…/<name>_<id>.html)",
        ),
    ],
    ctx: Context | None = None,
) -> NixCardResponse:
    """Fetch one Nix product page over HTTP.

    ## Return Format

    NixCardResponse: {status, product_id, title, brand, part_number, price_rub,
    is_available, city, url, tier_used, meta}. The price is ``itemprop="price"``;
    СУПЕРЦЕНА is not used.

    ## Error Format

    ToolError: bad_request for a foreign URL; not_found for a product the site does not
    have; challenge_required on a foreign redirect or anti-bot page; transport_down with
    source_stopped on 401/403/429; parser_drift when the microdata moved.
    """
    log_event("nix_card.start", input=url[:80])
    raw = url.strip()
    target = _card_path(raw if "://" in raw or raw.startswith("/") else f"https://{raw}")
    if target is None:
        raise_tool_error(BadRequestError(f"not a nix.ru product page: {url[:120]!r}; pass the URL from nix_search"))
    product_id, page_url = target
    cached = _cache.get(f"card:{product_id}")
    if isinstance(cached, dict):
        fetched, tier = cached, "cache"
    else:
        reply = await _get(page_url)
        if reply.status in _REDIRECTS:
            moved = _card_path(reply.location)
            if moved is None or moved[0] != product_id:
                _refuse(
                    f"http {reply.status}",
                    f"redirect to {reply.location[:80]!r} instead of the product",
                    status=reply.status,
                    challenge_type="redirect",
                )
            page_url = moved[1]
            reply = await _get(page_url)
            if reply.status in _REDIRECTS:
                _refuse(f"http {reply.status}", "a second redirect", status=reply.status, challenge_type="redirect")
        if reply.status in _GONE:
            _pacer.record_success()
            raise_tool_error(NotFoundError(f"nix_card: Nix has no product {product_id}", provider="nix"))
        if reply.status != 200:
            raise_tool_error(
                TransportDownError(
                    f"nix_card: unexpected HTTP {reply.status}", provider="nix", status_code=reply.status
                )
            )
        page = reply.text or ""
        if not _PRODUCT_RE.search(page) and _WALL_RE.search(page):
            _refuse("anti-bot page", "anti-bot page instead of the product", status=200, challenge_type="antibot_page")
        fetched, tier = {**_card_fields(page, product_id), "url": page_url}, "http"
        _pacer.record_success()
        _cache.set(f"card:{product_id}", fetched)
    warnings: list[str] = []
    if fetched["price"] is None and fetched["is_available"] is not False:
        warnings.append("no_price: the product page carries no price")
    log_event("nix_card.done", product_id=product_id, price=fetched["price"], warnings=len(warnings))
    card = NixCardResponse(
        product_id=product_id,
        title=fetched["title"],
        brand=fetched["brand"],
        part_number=fetched["part_number"],
        price_rub=fetched["price"],
        superprice_rub=fetched["superprice"],
        is_available=fetched["is_available"],
        city=fetched["city"],
        url=fetched["url"],
        tier_used=tier,
    )
    card.meta = MetaOut(source="nix_card", healthy=not warnings, warnings=warnings)
    return card


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def nix_selfcheck(ctx: Context | None = None) -> NixSelfcheckResponse:
    """Drift canary: one tab search, then the card of its first priced row.

    A refusal or network fault is inconclusive; only a reached page that fails to
    parse is drift.
    """
    log_event("nix_selfcheck.start")
    try:
        result = await _nix_selfcheck_impl(ctx)
        log_event("nix_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("nix_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"nix_selfcheck failed: {exc}")))


def _probe_verdict(exc: ToolError) -> tuple[str, str]:
    try:
        code = json.loads(str(exc)).get("error")
    except (ValueError, AttributeError):
        code = None
    return ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", "transport_down")


async def _nix_selfcheck_impl(ctx: Context | None) -> NixSelfcheckResponse:
    checks: dict[str, dict] = {}
    first_url: str | None = None
    try:
        async with asyncio.timeout(90):
            found = await nix_search(_settings.selfcheck_query, limit=5, ctx=ctx)
        priced = [it for it in found.items if it.price_rub is not None and it.url]
        if priced:
            first_url = priced[0].url
            checks["search"] = R.selfcheck_entry(
                "healthy", baseline="tab-rows-v1", notes=[f"{len(priced)} priced rows"]
            )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline="tab-rows-v1", reason="parse_smoke_failed", notes=["no priced rows"]
            )
    except ToolError as exc:
        state, reason = _probe_verdict(exc)
        checks["search"] = R.selfcheck_entry(state, baseline="tab-rows-v1", reason=reason, notes=[str(exc)[:160]])

    if first_url is None:
        checks["card"] = R.selfcheck_entry(
            "inconclusive", baseline="microdata-card-v1", reason="skipped", notes=["no search row to open"]
        )
    else:
        try:
            async with asyncio.timeout(60):
                card = await nix_card(first_url, ctx=ctx)
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
        "nix",
        checks,
        required=("search", "card"),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return NixSelfcheckResponse(**result_dict)


apply_compact_output_schemas(mcp)
