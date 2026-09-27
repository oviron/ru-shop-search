"""Avito MCP connector: listings search and item pages, read in the plugin's Chrome tab.

Avito bans by IP reputation and request rate, so there is one transport and no second
try: the tab opens the ``/web/1/js/items`` search API as a document (one navigation,
the JSON is the page text) or an item page. Any 401/403/418/429, a block page, a
firewall JSON or a redirect off avito.ru stops Avito for 30 minutes.

A missing price is None, never 0: exchange and price-on-request ads would rank cheapest.

NEVER write to stdout in a stdio MCP server — it corrupts JSON-RPC.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import urllib.parse
from typing import Annotated, Any, Literal, NoReturn

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

from avito_connector.models_output import (
    AvitoCardResponse,
    AvitoCardSellerOut,
    AvitoSearchItemOut,
    AvitoSearchResponse,
    AvitoSelfcheckResponse,
    MetaOut,
)
from avito_connector.settings import get_settings
from avito_connector.shape_reference import missing_required_families

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.avito.ru"
ITEMS_API = f"{SITE_BASE}/web/1/js/items"
_HOSTS = frozenset({"avito.ru", "www.avito.ru"})
MAX_BODY_CHARS = _settings.max_body_chars

_min_gap = _settings.min_gap
_LOCATION_ID_RE = re.compile(r"^\d{2,10}$")
_ITEM_PATH_RE = re.compile(r"^/[a-z0-9_-]+/[a-z0-9_-]+/[a-z0-9_-]*_(\d{6,})/?$")
_REFUSAL_STATUSES = frozenset({401, 403, 418, 429})
_GONE = frozenset({404, 410})
_FIREWALL_MARKERS = ("firewall", "too-many-requests")
_RENDER_WAIT_S = 10.0
_POLL_S = 0.5
_SORT = {"default": None, "price_asc": "1", "price_desc": "2", "date": "104"}
_SELLER_TYPE = {"any": None, "private": "1", "company": "2"}

mcp = FastMCP(
    name="avito-connector",
    version=SERVER_VERSION,
    instructions=(
        "Avito listings (used and new, from private sellers and shops): avito_search lists "
        "ads for a query with price/sort/delivery/seller-type filters, avito_card reads one "
        "ad page. Read-only, no login. A refusal stops Avito for 30 minutes."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap, source="avito")
_cdp_lock = asyncio.Lock()

# Chrome shows a JSON document as <body><pre>…</pre>, plus a viewer the pre excludes.
_JSON_READ_JS = """
() => {
    const pre = document.querySelector('body > pre');
    const raw = (pre || document.body || document.documentElement).textContent || '';
    return JSON.stringify({
        page_title: document.title || '',
        size: raw.length,
        text: raw.length > MAX_BODY_CHARS ? '' : raw,
    });
}
""".replace("MAX_BODY_CHARS", str(MAX_BODY_CHARS))

# Seller names stay on the page: only reputation leaves the tab.
_CARD_EXTRACT_JS = """
() => {
    const text = el => el ? ((el.textContent || '').replace(/\\s+/g, ' ').trim() || null) : null;
    const q = sel => document.querySelector(sel);
    const title = text(q('[data-marker="item-view/title-info"]')) || text(q('h1[itemprop="name"]'));
    const priceEl = q('[data-marker="item-view/item-price"]') || q('[itemprop="price"]');
    const seller = q('[data-marker="seller-info"]');
    const since = seller ? (seller.textContent.match(/На Авито с[^\\n]*?\\d{4}/) || [null])[0] : null;
    const head = document.body ? document.body.textContent.slice(0, 3000) : '';
    return JSON.stringify({
        page_title: document.title || '',
        title,
        firewall: !title && /Доступ ограничен|проблема с IP/i.test(head),
        captcha: /captcha|blocked/i.test(document.location.pathname)
            || !!q('iframe[src*="captcha"], form[action*="captcha"], .g-recaptcha, .h-captcha'),
        price: priceEl ? (priceEl.getAttribute('content') || text(priceEl)) : null,
        description: text(q('[data-marker="item-view/item-description"]')),
        params: [...document.querySelectorAll('[data-marker="item-view/item-params"] li')].map(text).filter(Boolean).slice(0, 40),
        posted: text(q('[data-marker="item-view/item-date"]')),
        views: text(q('[data-marker="item-view/total-views"]')),
        photos: document.querySelectorAll('[data-marker="image-preview/item"]').length,
        delivery: !!q('[data-marker*="delivery"]'),
        seller_kind: seller ? text(seller.querySelector('[data-marker="seller-info/label"]')) : null,
        seller_rating: seller ? text(seller.querySelector('[data-marker="seller-info/score"]')) : null,
        seller_reviews: seller ? text(seller.querySelector('[data-marker="seller-info/summary"]')) : null,
        seller_since: since ? since.replace(/\\s+/g, ' ').trim() : null,
    });
}
"""


def _refuse(reason: str, error: ConnectorError) -> NoReturn:
    _pacer.record_refusal(reason)
    raise_tool_error(error)


def _blocked(status: int, what: str = "refused the browser tab") -> TransportDownError:
    return TransportDownError(
        f"Avito {what} (HTTP {status}); Avito is stopped for 30 min, do not retry.",
        provider="avito",
        status_code=status,
    )


def _captcha() -> ChallengeRequiredError:
    return ChallengeRequiredError(
        "Avito showed a captcha in the plugin's tab; Avito is stopped for 30 min, do not retry.",
        provider="avito",
        challenge_type="avito_firewall",
        status_code=403,
    )


async def _open(url: str, js: str, ready: Any) -> dict[str, Any]:
    """One navigation to ``url``; poll ``js`` until ``ready`` or the deadline. Refusals stop Avito."""
    async with _cdp_lock:
        try:
            await _pacer.wait(min_gap=_min_gap)
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(url, wait_ms=0, allowed_hosts=_HOSTS) as page:
                deadline = asyncio.get_running_loop().time() + _RENDER_WAIT_S
                while True:
                    raw = await asyncio.wait_for(page.evaluate(js), timeout=30.0)
                    data = json.loads(raw) if isinstance(raw, str) else None
                    if not isinstance(data, dict):
                        raise_tool_error(ParserDriftError("tab reader returned a non-object", provider="avito"))
                    if ready(data) or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(_POLL_S)
        except NavBlocked as exc:
            if exc.status in _REFUSAL_STATUSES:
                _refuse(f"http {exc.status}", _blocked(exc.status))
            if exc.status in _GONE:
                _pacer.record_success()
                raise_tool_error(NotFoundError("Avito has no such page", provider="avito"))
            raise_tool_error(
                TransportDownError(f"Avito page failed (HTTP {exc.status})", provider="avito", status_code=exc.status)
            )
        except NavigationPolicyError:
            _refuse("redirect off avito.ru", _blocked(403, "redirected the tab off avito.ru"))
    return data


async def _open_json(url: str) -> dict[str, Any]:
    data = await _open(url, _JSON_READ_JS, lambda d: bool(d.get("text")))
    size = data.get("size")
    if not isinstance(size, int) or size > MAX_BODY_CHARS:
        raise_tool_error(TransportDownError("Avito answer missing or over the body cap", provider="avito"))
    text = str(data.get("text") or "").strip()
    if not text:
        raise_tool_error(TransportDownError("Avito answer did not load in time", provider="avito"))
    if text[0] not in "{[":
        _refuse("a page instead of JSON", _blocked(200, "answered with a page instead of JSON"))
    try:
        payload = json.loads(text)
    except ValueError:
        raise_tool_error(TransportDownError("Avito answer is cut-off JSON", provider="avito"))
    if not isinstance(payload, dict):
        raise_tool_error(ParserDriftError("js/items payload is not a JSON object", provider="avito"))
    if not _parse_search_items(payload)[0] and any(m in text[:300].lower() for m in _FIREWALL_MARKERS):
        _refuse("firewall answer", _blocked(403, "answered with a firewall block"))
    _pacer.record_success()
    return payload


def _valid_location_id(raw: str) -> bool:
    return bool(_LOCATION_ID_RE.match(raw.strip()))


def _posted_at(item: dict[str, Any]) -> str | None:
    """Publication time as an ISO-8601 string, or an honest None.

    The live payload carries no `date`/`time` string — it carries
    `sortTimeStamp`/`allowTimeStamp` in epoch milliseconds (verified 2026-07-28
    against a real response). Reading only the string aliases left `posted_at`
    null on every row, so the epoch form is handled here and rendered as ISO-8601
    UTC rather than passed through as a bare 13-digit number, which no caller
    could tell from an id.
    """
    text = R.flatten_text(R.first_present(item, "date", "publishedAt", "postedAt", "time"), "text", "value", "name")
    if text:
        return text
    stamp = R.first_present(item, "sortTimeStamp", "allowTimeStamp")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    try:
        # Milliseconds since epoch; guard against a seconds-based drift
        # upstream. The division stays inside the try: json.loads admits
        # arbitrary-precision ints, and a huge stamp overflows the float
        # arithmetic before fromtimestamp is ever reached.
        seconds = stamp / 1000.0 if stamp > 1e11 else float(stamp)
        moment = datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def _parse_search_items(payload: dict) -> tuple[list[dict[str, Any]], int | None]:
    """Best-effort extraction of items + total from a js/items payload.

    The endpoint has shipped several envelope shapes; tolerate the known ones
    and record drift loudly rather than guessing.
    """
    candidates = (
        payload.get("items"),
        payload.get("catalog", {}).get("items") if isinstance(payload.get("catalog"), dict) else None,
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), dict) else None,
    )
    items_raw = next((c for c in candidates if isinstance(c, list)), [])
    total = R.first_present(payload, "totalCount", "total", "count")
    if not isinstance(total, int):
        total = R.coerce_int(total)
    out: list[dict[str, Any]] = []
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        item_id = R.first_present(it, "id", "itemId", "item_id")
        item_id = R.coerce_int(item_id)
        title = R.flatten_text(R.first_present(it, "title", "name"))
        price = R.coerce_price(R.first_present(it, "price", "priceRub", "price_rub"))
        if price is None and isinstance(it.get("priceDetailed"), dict):
            price = R.coerce_price(R.first_present(it["priceDetailed"], "value", "price"))
        # The live js/items key is `urlPath`. The alias list used to read `uriPath`
        # — one letter out — so every search hit came back with url=None while the
        # rest of the row parsed fine. Verified 2026-07-28 against a real response
        # captured from a residential session: `urlPath` is the ONLY url-bearing
        # key on the item (there is no `uri`, `url` or `itemUrl`).
        uri = R.first_present(it, "uri", "url", "urlPath", "uriPath", "itemUrl") or ""
        if uri and uri.startswith("/"):
            uri = SITE_BASE + uri
        # location arrives as a nested object on the live endpoint, not a string:
        # {"id": 637640, "name": "Москва", ...}. flatten_text pulls the name out
        # (or yields None) so one extra level of nesting upstream can no longer
        # fail validation for the entire page of listings.
        location = R.flatten_text(
            R.first_present(it, "location", "locationName", "address"),
            "name",
            "title",
            "formattedAddress",
        )
        if location is None:
            # Real fallbacks present in the live payload, in order of specificity.
            # `addressDetailed.locationName` can be "" for an item Avito itself has
            # no place name for; flatten_text turns that into None rather than an
            # empty string masquerading as an answer. A bare `locationId` is NOT
            # resolved to a city — there is no lookup table here, and guessing one
            # would be inventing data.
            location = R.flatten_text(it.get("addressDetailed"), "locationName", "name") or R.flatten_text(
                it.get("geo"), "formattedAddress", "name", "title"
            )
        seller = it.get("seller") if isinstance(it.get("seller"), dict) else {}
        rating: dict[str, Any] = it["rating"] if isinstance(it.get("rating"), dict) else {}
        category: dict[str, Any] = it["category"] if isinstance(it.get("category"), dict) else {}
        images = it.get("images")
        images_count = (
            len(images)
            if isinstance(images, list)
            else R.coerce_int(R.first_present(it, "imagesCount", "images_count")) or 0
        )
        out.append(
            {
                "item_id": item_id,
                "title": title,
                "price_rub": price,
                "url": uri or None,
                "location": location,
                "category_id": R.coerce_int(category.get("id")),
                "seller_rating": R.coerce_rating(rating.get("score")),
                "seller_reviews": _first_int(rating.get("summary")),
                "is_company": R.first_present(seller, "isCompany", "is_company") if seller else None,
                "posted_at": _posted_at(it),
                "images": images_count,
            }
        )
    return out, total


def _search_url(query: str, page: int, location_id: str, category_id: str | None, extra: dict[str, str]) -> str:
    params: dict[str, str] = {
        "q": query,
        "p": str(page),
        "locationId": location_id,
        "context": "",
        "updateListOnly": "true",
    }
    if category_id:
        params["categoryId"] = category_id
    params.update(extra)
    return f"{ITEMS_API}?{urllib.parse.urlencode(params)}"


@mcp.tool(
    name="avito_search",
    annotations=ToolAnnotations(
        title="Avito Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def avito_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'thinkpad x1 carbon'")],
    page: Annotated[int, Field(ge=1, le=100, description="Result page (1-based)")] = 1,
    location_id: Annotated[
        str | None, Field(description="Avito location id; default AVITO_LOCATION_ID (637640 = Moscow)")
    ] = None,
    category_id: Annotated[str | None, Field(description="Optional Avito category id to narrow the search")] = None,
    price_min: Annotated[int | None, Field(ge=0, description="Lowest price, rubles")] = None,
    price_max: Annotated[int | None, Field(ge=0, description="Highest price, rubles")] = None,
    sort: Annotated[
        Literal["default", "price_asc", "price_desc", "date"], Field(description="Avito's order of results")
    ] = "default",
    delivery_only: Annotated[bool, Field(description="Only ads with Avito delivery")] = False,
    seller_type: Annotated[Literal["any", "private", "company"], Field(description="Private sellers or shops")] = "any",
    ctx: Context | None = None,
) -> AvitoSearchResponse:
    """Search Avito listings through the site's js/items API, opened in the plugin's tab.

    Filters go to Avito as the site's own URL parameters; price bounds are also
    re-applied here. When no title names the query the page is dropped as ``no_match``.

    ## Return Format

    AvitoSearchResponse: {status, query, page, location_id, tier_used, count,
    total_count, items[], meta}. Items carry item_id, title, price_rub (None when the
    ad has no price — never 0), url, location, category_id, seller_rating, seller_reviews.

    ## Error Format

    ToolError: bad_request on malformed arguments; transport_down with source_stopped
    on 401/403/418/429, a block page, a firewall answer or a redirect off avito.ru
    (Avito stops for 30 min); parser_drift when the answer is JSON of another shape.
    """
    text = " ".join(query.split())
    log_event("avito_search.start", query=text[:60], page=page)
    try:
        if not text:
            raise_tool_error(BadRequestError("empty search query"))
        if location_id is not None and not location_id.strip():
            raise_tool_error(BadRequestError("location_id must not be empty"))
        loc = (location_id or _settings.location_id).strip()
        if not _valid_location_id(loc):
            raise_tool_error(BadRequestError(f"location_id must be digits (e.g. 637640), got {loc!r}"))
        if category_id is not None and not category_id.strip().isdigit():
            raise_tool_error(BadRequestError(f"category_id must be digits, got {category_id!r}"))
        if price_min is not None and price_max is not None and price_min > price_max:
            raise_tool_error(BadRequestError(f"price_min {price_min} is above price_max {price_max}"))
        if sort not in _SORT:
            raise_tool_error(BadRequestError(f"sort must be one of {sorted(_SORT)}, got {sort!r}"))
        if seller_type not in _SELLER_TYPE:
            raise_tool_error(BadRequestError(f"seller_type must be one of {sorted(_SELLER_TYPE)}, got {seller_type!r}"))

        extra = {
            key: value
            for key, value in (
                ("pmin", None if price_min is None else str(price_min)),
                ("pmax", None if price_max is None else str(price_max)),
                ("s", _SORT[sort]),
                ("d", "1" if delivery_only else None),
                ("user", _SELLER_TYPE[seller_type]),
            )
            if value is not None
        }
        url = _search_url(text, page, loc, category_id.strip() if category_id else None, extra)
        cached = _cache.get(url)
        if isinstance(cached, dict):
            payload, tier = cached, "cache"
        else:
            payload, tier = await _open_json(url), "cdp"
            _cache.set(url, payload)

        rows, total = _parse_search_items(payload)
        warnings: list[str] = []
        if extra:
            warnings.append(
                "filters_not_verified_live: Avito has not yet been seen honouring "
                f"{', '.join(sorted(extra))} on js/items; price bounds are re-applied here"
            )
        if not rows and total not in (0, None):
            warnings.append("empty_items_with_nonzero_total")
        if price_min is not None or price_max is not None:
            low, high = price_min or 0, price_max if price_max is not None else float("inf")
            rows = [r for r in rows if r["price_rub"] is not None and low <= r["price_rub"] <= high]
        if sort in ("price_asc", "price_desc"):
            priced = sorted((r for r in rows if r["price_rub"] is not None), key=lambda r: r["price_rub"])
            rows = (priced if sort == "price_asc" else priced[::-1]) + [r for r in rows if r["price_rub"] is None]
        if rows and not names_query(text, [r["title"] or "" for r in rows]):
            warnings.insert(0, f"no_match: Avito listed {len(rows)} ads, none names {text!r} — dropped as unrelated")
            rows = []
        items = [AvitoSearchItemOut(**row) for row in rows]
        result = AvitoSearchResponse(
            query=text, page=page, location_id=loc, tier_used=tier, count=len(items), total_count=total, items=items
        )
        result.meta = MetaOut(source="avito_search", healthy=not warnings, warnings=warnings)
        log_event("avito_search.done", query=text[:60], returned=len(items))
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("avito_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"avito_search failed: {exc}"), provider="avito"))


def item_page(url: str) -> tuple[str, str] | None:
    """(item id, canonical URL) for an avito.ru item page, else None."""
    parsed = urllib.parse.urlsplit(url)
    match = _ITEM_PATH_RE.match(parsed.path)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in _HOSTS
        or parsed.username is not None
        or parsed.port is not None
        or match is None
    ):
        return None
    return match.group(1), f"{SITE_BASE}{parsed.path.rstrip('/')}"


def _first_int(raw: Any) -> int | None:
    match = re.search(r"\d[\d\s  ]*", str(raw or ""))
    return int(re.sub(r"\D", "", match.group(0))) if match else None


def _card_ready(data: dict[str, Any]) -> bool:
    return bool(data.get("title") or data.get("firewall") or data.get("captcha"))


@mcp.tool(
    name="avito_card",
    annotations=ToolAnnotations(
        title="Avito Item Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def avito_card(
    url: Annotated[str, Field(min_length=1, max_length=400, description="Ad URL from avito_search or the site")],
    ctx: Context | None = None,
) -> AvitoCardResponse:
    """Read one Avito ad page in the plugin's tab: price, description, params, seller reputation.

    The seller's name is not returned. The page reader has not been checked on a live
    page yet (the meta carries ``card_not_verified_live``).

    ## Return Format

    AvitoCardResponse: {status, item_id, title, price_rub, description, params, posted,
    views, photos, delivery, seller{is_company, rating_score, rating_count, since_year},
    url, tier_used, meta}. price_rub is None when the ad has no price — never 0.

    ## Error Format

    ToolError: bad_request for anything but an avito.ru ad URL; not_found for a removed
    ad; challenge_required on a captcha and transport_down with source_stopped on
    401/403/418/429, a block page or a redirect off avito.ru; parser_drift when the page
    has no title.
    """
    log_event("avito_card.start", input=url[:80])
    try:
        target = item_page(url.strip())
        if target is None:
            raise_tool_error(
                BadRequestError(f"not an avito.ru ad page: {url[:120]!r}; pass the ad URL from avito_search")
            )
        item_id, page_url = target
        cached = _cache.get(f"card:{item_id}")
        if isinstance(cached, dict):
            data, tier = cached, "cache"
        else:
            data, tier = await _open(page_url, _CARD_EXTRACT_JS, _card_ready), "cdp"
            if data.get("captcha"):
                _refuse("captcha", _captcha())
            if data.get("firewall"):
                _refuse("block page in the tab", _blocked(403, "showed «Доступ ограничен»"))
            if not data.get("title"):
                raise_tool_error(ParserDriftError("avito_card: the ad page has no title", provider="avito"))
            _pacer.record_success()
            _cache.set(f"card:{item_id}", data)
        kind = str(data.get("seller_kind") or "").lower()
        since = re.search(r"(?:19|20)\d\d", str(data.get("seller_since") or ""))
        card = AvitoCardResponse(
            item_id=item_id,
            title=data.get("title"),
            price_rub=R.coerce_price(data.get("price")),
            description=data.get("description"),
            params=[str(p) for p in data.get("params") or []],
            posted=data.get("posted"),
            views=_first_int(data.get("views")),
            photos=int(data.get("photos") or 0),
            delivery=data.get("delivery") if isinstance(data.get("delivery"), bool) else None,
            seller=AvitoCardSellerOut(
                is_company=True if "компан" in kind else False if "частн" in kind else None,
                rating_score=R.coerce_rating(data.get("seller_rating")),
                rating_count=_first_int(data.get("seller_reviews")),
                since_year=int(since.group(0)) if since else None,
            ),
            url=page_url,
            tier_used=tier,
        )
        warnings = ["card_not_verified_live: the ad page reader has not been checked on a live Avito page"]
        if card.price_rub is None:
            warnings.append("no_price: the ad page names no price")
        card.meta = MetaOut(source="avito_card", healthy=False, warnings=warnings)
        return card
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("avito_card.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"avito_card failed: {exc}"), provider="avito"))


# CLI-only drift canary: ``marketplace-mcp doctor`` calls it directly; it is not an MCP tool.
async def avito_selfcheck(ctx: Context | None = None) -> AvitoSelfcheckResponse:
    """Drift canary: one search through the tab. A refusal is inconclusive; only a reached answer that fails to parse is drift."""
    log_event("avito_selfcheck.start")
    try:
        checks: dict[str, dict] = {}
        try:
            async with asyncio.timeout(60):
                url = _search_url(_settings.selfcheck_query, 1, _settings.location_id, None, {})
                payload = await _open_json(url)
            items, _ = _parse_search_items(payload)
            missing = missing_required_families(R.shape_signature(payload))
            if not items or missing:
                lost = "; ".join("/".join(family) for family in missing) or "no items parsed"
                checks["search"] = R.selfcheck_entry(
                    "drift", baseline="js-items-search-v1", reason="parse_smoke_failed", notes=[lost[:160]]
                )
            else:
                checks["search"] = R.selfcheck_entry(
                    "healthy", baseline="js-items-search-v1", notes=[f"{len(items)} items parsed"]
                )
        except ToolError as exc:
            try:
                code = json.loads(str(exc)).get("error")
            except (ValueError, AttributeError):
                code = None
            state, reason = ("drift", "parse_smoke_failed") if code == "parser_drift" else ("inconclusive", code)
            checks["search"] = R.selfcheck_entry(
                state, baseline="js-items-search-v1", reason=reason or "transport_down", notes=[str(exc)[:160]]
            )
        result = AvitoSelfcheckResponse(
            **R.selfcheck_result(
                "avito",
                checks,
                required=("search",),
                server_version=SERVER_VERSION,
                server_started_at=SERVER_STARTED_AT,
                process_id=None,
            )
        )
        log_event("avito_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("avito_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"avito_selfcheck failed: {exc}")))


apply_compact_output_schemas(mcp)
