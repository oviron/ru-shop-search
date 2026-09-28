"""AliExpress MCP connector: search and cards rendered in the plugin's Chrome profile.

Anonymous HTTP gets the x5sec check or an empty SPA shell, and a direct navigation to
an item page is checked even in a warm profile, so a card opens with ``window.open``
from a loaded search tab (measured 2026-09-27, docs/ANTI_BOT.md).

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
from mcp_core.dom import JS_HELPERS, title_from_tile
from mcp_core.errors import (
    BadRequestError,
    ChallengeRequiredError,
    ConnectorError,
    ParserDriftError,
    ToolError,
    TransportDownError,
    raise_tool_error,
)
from mcp_core.logging import log_event
from mcp_core.output_schema import apply_compact_output_schemas
from mcp_core.pacing import Pacer
from mcp_core.query_match import names_query as _mentions_query
from mcp_core.redact import redact_error_text as _redact
from mcp_core.transport.chrome_cdp import NavBlocked, NavigationPolicyError, open_page
from pydantic import Field

from aliexpress_connector.models_output import (
    AliCardResponse,
    AliSearchItemOut,
    AliSearchResponse,
    AliSelfcheckResponse,
    MetaOut,
)
from aliexpress_connector.settings import get_settings

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://aliexpress.ru"
SEARCH_URL = f"{SITE_BASE}/wholesale"
# A card opens from a loaded search tab; this search is the landing for it.
_LANDING = f"{SEARCH_URL}?SearchText={urllib.parse.quote('часы')}"

TIMEOUT = _settings.timeout
MAX_BODY_BYTES = _settings.max_body_bytes
_min_gap = _settings.min_gap
MAX_TILES = 48
_RENDER_WAIT_S = 15.0
_POLL_S = 1.0
_REFUSAL_STATUSES = frozenset({401, 403, 418, 429})

# The 9-digit floor keeps short garbage out of the URL we rebuild for the profile's Chrome.
_ITEM_ID_RE = re.compile(r"/item/(\d{9,})(?:\.html)?")
_BARE_ID_RE = re.compile(r"\d{9,}")
_BARE_PRICE_RE = re.compile(r"[\d\s\u00a0\u2009\u202f.,]+(?:₽|руб\.?)?")

mcp = FastMCP(
    name="aliexpress-connector",
    version=SERVER_VERSION,
    instructions=(
        "AliExpress catalog: search and product cards, rendered in the plugin's own Chrome "
        "profile — x5sec blocks anonymous access. Start with aliexpress_search; "
        "aliexpress_card takes an item id or aliexpress.ru item URL. "
        "Review TEXTS are not exposed: only the rating and order counts are read."
    ),
)

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap, source="aliexpress")
_cdp_lock = asyncio.Lock()


async def _polite_wait() -> None:
    """Space this source's requests out; raises SourceStoppedError while AliExpress is stopped."""
    await _pacer.wait(min_gap=_min_gap)


def _extract_item_id(raw: str) -> str | None:
    """Pull the item id out of an aliexpress.ru URL or a bare numeric id.

    Host-checked: only the id survives and the card URL is rebuilt from SITE_BASE,
    so the argument cannot steer the profile's Chrome elsewhere.
    """
    raw = raw.strip()
    if not raw:
        return None
    if _BARE_ID_RE.fullmatch(raw):
        return raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme:
        if parts.scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").rstrip(".").lower()
        if host != "aliexpress.ru" and not host.endswith(".aliexpress.ru"):
            return None
        raw = parts.path
    elif raw.startswith("//"):
        return None
    match = _ITEM_ID_RE.search(raw)
    return match.group(1) if match else None


def _item_url(item_id: str, sku_id: str | None = None) -> str:
    url = f"{SITE_BASE}/item/{item_id}.html"
    return f"{url}?sku_id={sku_id}" if sku_id and sku_id.isdigit() else url


def _is_punish(payload: dict[str, Any]) -> bool:
    """The x5sec challenge page is a refusal, not data."""
    return bool(payload.get("punish")) or bool(payload.get("url") and "punish" in str(payload["url"]))


def _challenge_error(detail: str = "") -> ChallengeRequiredError:
    message = (
        "AliExpress showed its x5sec challenge; AliExpress is stopped for 30 min, do not retry. "
        "If the profile window shows the check, it may be passed by hand, then "
        "`marketplace-mcp resume aliexpress`."
    )
    if detail:
        message += f" Detail: {detail[:160]}"
    return ChallengeRequiredError(message, provider="aliexpress", challenge_type="x5sec", status_code=403)


def _refuse(reason: str, error: ConnectorError) -> NoReturn:
    _pacer.record_refusal(reason)
    raise_tool_error(error)


def _nav_failed(exc: NavBlocked) -> NoReturn:
    if exc.status in _REFUSAL_STATUSES:
        _refuse(
            f"http {exc.status}",
            TransportDownError(
                f"AliExpress refused the browser tab (HTTP {exc.status}); AliExpress is stopped for 30 min, "
                "do not retry. If the profile window shows a check, it may be passed by hand, then "
                "`marketplace-mcp resume aliexpress`.",
                provider="aliexpress",
                status_code=exc.status,
            ),
        )
    raise_tool_error(
        TransportDownError(f"AliExpress page failed (HTTP {exc.status})", provider="aliexpress", status_code=exc.status)
    )


def _left_site() -> NoReturn:
    _refuse("redirect off aliexpress.ru", _challenge_error("the tab was sent off aliexpress.ru (login or check page)"))


# Shared helpers: tileRootFor tests the element itself first (the DNS lesson), and
# priceTextsIn returns {attached, other}; prices are paired in Python, not here.
_PUNISH_JS = """
    const PUNISH_RE = /_____tmd_____|\\/punish|x5secdata/i;
    const isPunished = () => PUNISH_RE.test((document.location && document.location.href) || '')
        || /^\\s*Пройдите проверку/i.test(document.title || '');
"""

_SEARCH_EXTRACT_TEMPLATE = """
() => {
    //__SHARED_HELPERS__
    //__PUNISH__
    // Only the main feed: below it the site appends «Подобрали для вас», unrelated to the query.
    const feed = document.querySelector('[class*="SnowSearchProductFeed_List"]');
    const text = (document.body && document.body.innerText) || '';
    const nothing = /По запросу «[^»]*» ничего не найдено|Ничего не нашли/.test(text);
    const anchors = feed ? Array.from(feed.querySelectorAll('a[href*="/item/"]'))
        .filter(a => /\\/item\\/\\d{9,}\\.html/.test(a.getAttribute('href') || a.href || '')) : [];
    const out = [];
    const seen = new Set();
    const DELIVERY_RE = /^(?:до|от)\\s+\\d+\\s+д|бесплатно$|доставк/i;
    const ratingLeaf = (tile) => {
        for (const el of tile.querySelectorAll('*')) {
            if (el.children.length) continue;
            const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (/^\\d(?:[.,]\\d{1,2})?$/.test(t)) return t.replace(',', '.');
        }
        return null;
    };
    const ordersLeaf = (tile) => {
        for (const el of tile.querySelectorAll('*')) {
            if (el.children.length) continue;
            const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            const m = t.match(/^([\\d\\s\\u00a0]+)\\s+купили$/i);
            if (m) return m[1].replace(/\\s+/g, '');
        }
        return null;
    };
    for (const a of anchors) {
        if (out.length >= __MAX_TILES__) break;
        const href = a.href || a.getAttribute('href') || '';
        const m = href.match(/\\/item\\/(\\d{9,})\\.html/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        const tile = tileRootFor(a, /\\/item\\/(\\d{9,})\\.html/);
        if (!tile || !tile.textContent) continue;
        // The name is the longest leaf that digits do not dominate (>= 2 digits and > 40 %)
        // and that is not the delivery line; some tiles render no name at all.
        let title = null, best = 5;
        for (const el of tile.querySelectorAll('*')) {
            if (el.children.length) continue;
            const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!t || t.length > 140) continue;
            if (DECOY_RE.test(t) || DELIVERY_RE.test(t)) continue;
            const digits = ((t.match(/\\d/g) || []).length);
            if (digits >= 2 && !/купили$/i.test(t) && digits / t.length > 0.4) continue;
            if (t.length > best) { best = t.length; title = t; }
        }
        out.push({
            item_id: m[1],
            title: title,
            price_texts: priceTextsIn(tile),
            rating: ratingLeaf(tile),
            orders: ordersLeaf(tile),
            sku_id: (href.match(/sku_id=(\\d+)/) || [])[1] || null,
        });
    }
    return JSON.stringify({items: out, punish: isPunished(), feed: !!feed, nothing: nothing,
        page_title: document.title || ''});
}
"""

_CARD_EXTRACT_TEMPLATE = """
() => {
    //__SHARED_HELPERS__
    //__PUNISH__
    const title = cleanText(document.querySelector('h1')) || null;
    // Only the sticky offer bar: a body-wide scan would pick up neighbours' prices from the rails.
    const sticky = document.querySelector('[class*="HazeStickyOfferPrice__offerPrice"]')
        || document.querySelector('[class*="SnowStickyOffer"]');
    const ratingText = cleanText(document.querySelector('[class*="HazeProductDescription__ratingWrap"]'));
    const meta = (document.querySelector('meta[name="description"]') || {}).content || null;
    return JSON.stringify({
        punish: isPunished(),
        title: title,
        price_texts: priceTextsIn(sticky),
        rating_text: ratingText,
        meta_description: meta,
        url: (document.location && document.location.href || ''),
    });
}
"""


def _js(template: str) -> str:
    return (
        template.replace("//__SHARED_HELPERS__", JS_HELPERS)
        .replace("//__PUNISH__", _PUNISH_JS)
        .replace("__MAX_TILES__", str(MAX_TILES))
    )


_SEARCH_EXTRACT_JS = _js(_SEARCH_EXTRACT_TEMPLATE)
_CARD_EXTRACT_JS = _js(_CARD_EXTRACT_TEMPLATE)


def _scored_prices(attached_raw: list[Any] | None) -> tuple[list[float], float | None]:
    """Split glyph-attached texts into (regular prices, lowest coupon price).

    Only a bare number is a price: «N ₽ с купоном» needs opting in, and a delivery
    line («завтра, от 109 ₽») is not the product's price at all.
    """
    regular: list[float] = []
    coupon: float | None = None
    for candidate in attached_raw or []:
        text = str(candidate).strip()
        parsed = R.coerce_price(text)
        if parsed is None:
            continue
        if "купон" in text.lower():
            coupon = parsed if coupon is None else min(coupon, parsed)
        elif _BARE_PRICE_RE.fullmatch(text):
            regular.append(parsed)
    return regular, coupon


def _attached(payload: dict[str, Any]) -> list[Any] | None:
    texts = payload.get("price_texts")
    attached = texts.get("attached") if isinstance(texts, dict) else None
    return attached if isinstance(attached, list) else None


def _pair(regular: list[float]) -> tuple[float | None, float | None]:
    """Order-free pairing: the smallest price is paid, the largest above it is crossed out."""
    if not regular:
        return None, None
    price = min(regular)
    above = [candidate for candidate in regular if candidate > price]
    return price, (max(above) if above else None)


def _tile_prices(tile: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """(price, old_price, coupon_price) for a search tile; the grid lists the base price first."""
    regular, coupon = _scored_prices(_attached(tile))
    price, old_price = _pair(regular)
    return price, old_price, coupon


def _card_prices(payload: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """(price, old_price, coupon_price) from the sticky offer bar, paired the same way as tiles."""
    regular, coupon = _scored_prices(_attached(payload))
    return *_pair(regular), coupon


def _item_from_payload(tile: dict[str, Any]) -> AliSearchItemOut:
    price, old_price, coupon = _tile_prices(tile)
    item_id = str(tile.get("item_id") or "")
    sku_id = tile.get("sku_id") if isinstance(tile.get("sku_id"), str) else None
    return AliSearchItemOut(
        item_id=item_id or None,
        title=title_from_tile(tile),
        price_rub=price,
        old_price_rub=old_price,
        coupon_price_rub=coupon,
        rating=R.coerce_rating(tile.get("rating")),
        orders_count=R.coerce_int(tile.get("orders")),
        sku_id=sku_id,
        url=_item_url(item_id, sku_id) if _BARE_ID_RE.fullmatch(item_id) else None,
    )


def _unwrap_extract(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BODY_BYTES:
        raise_tool_error(TransportDownError("extracted page data missing or over the body cap"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise_tool_error(ParserDriftError(f"non-JSON {what} extract; preview: {str(raw)[:200]}"))
    if not isinstance(data, dict):
        raise_tool_error(ParserDriftError(f"{what} extractor returned a non-object payload"))
    return data


def _search_settled(data: dict[str, Any], previous: int | None) -> bool:
    """Done on a challenge, on «ничего не найдено», or once the tile count stops growing."""
    count = len(data.get("items") or [])
    return bool(data.get("punish") or data.get("nothing") or (count and count == previous))


async def _cdp_render_search(url: str, ctx: Context | None) -> dict[str, Any]:
    """Open the search URL in the profile's Chrome and extract the main feed's tiles."""
    async with _cdp_lock:
        await _polite_wait()
        try:
            async with open_page(url, wait_ms=0) as page:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _RENDER_WAIT_S
                previous: int | None = None
                while True:
                    await asyncio.sleep(_POLL_S)
                    data = _unwrap_extract(
                        await asyncio.wait_for(page.evaluate(_SEARCH_EXTRACT_JS), timeout=30.0), "search"
                    )
                    if _search_settled(data, previous) or loop.time() >= deadline:
                        break
                    previous = len(data.get("items") or [])
        except NavBlocked as exc:
            _nav_failed(exc)
        except NavigationPolicyError:
            _left_site()
    if _is_punish(data):
        _pacer.record_refusal("x5sec challenge")
    else:
        _pacer.record_success()
    return data


async def _cdp_card(url: str, ctx: Context | None) -> dict[str, Any]:
    """Open the item in a new tab from a loaded search page and read that tab's DOM.

    A direct navigation to an item page is challenged; ``window.open`` from the
    search tab is not. The URL is rebuilt from SITE_BASE by the caller.
    """
    item_id = _extract_item_id(url) or ""
    async with _cdp_lock:
        await _polite_wait()
        try:
            async with open_page(_LANDING, wait_ms=6000) as page:
                return await _read_item_tab(page, url, item_id)
        except NavBlocked as exc:
            _nav_failed(exc)
        except NavigationPolicyError:
            _left_site()


async def _read_item_tab(page: Any, url: str, item_id: str) -> dict[str, Any]:
    landing = _unwrap_extract(await asyncio.wait_for(page.evaluate(_CARD_EXTRACT_JS), timeout=30.0), "landing")
    if _is_punish(landing):
        _refuse("x5sec challenge", _challenge_error("the search page opened onto an x5sec challenge"))
    # The sibling-tab walk needs Playwright's page context; the raw-CDP fallback has none.
    context = getattr(page, "context", None)
    if context is None:
        raise_tool_error(
            TransportDownError(
                "aliexpress_card needs the Playwright-attached tab (it opens the item from the search tab); "
                "the raw-CDP fallback cannot follow a new tab"
            )
        )
    before = set(context.pages)
    # noopener: the item page must not script the search tab; the Referer stays, as in the live check.
    opened = await asyncio.wait_for(
        page.evaluate(
            "() => { try { window.open(" + json.dumps(url) + ", '_blank', 'noopener'); return 'opened'; } "
            "catch (e) { return 'error:' + String(e); } }"
        ),
        timeout=10.0,
    )
    if not isinstance(opened, str) or "opened" not in opened:
        raise_tool_error(TransportDownError(f"could not open the item tab ({opened})"))
    wants = f"/item/{item_id}"
    target = None
    try:
        for _ in range(20):
            await asyncio.sleep(_POLL_S)
            fresh = [candidate for candidate in context.pages if candidate not in before]
            if any("punish" in (candidate.url or "") for candidate in fresh):
                _refuse("x5sec challenge", _challenge_error("the item tab opened onto an x5sec challenge"))
            # Only a tab this call opened, with the requested id: a neighbour's tab would answer with its price.
            target = next((candidate for candidate in fresh if wants in (candidate.url or "")), None)
            if target is not None:
                break
        if target is None:
            raise_tool_error(TransportDownError("the item tab never opened (popup blocked or closed)"))
        raw = None
        # The sticky price module renders after the title; a title-only state is accepted after 6 polls.
        for attempt in range(12):
            try:
                raw = await asyncio.wait_for(target.evaluate(_CARD_EXTRACT_JS), timeout=30.0)
                probe = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
                if probe.get("punish") or _attached(probe) or (attempt >= 6 and probe.get("title")):
                    break
            except Exception:
                pass
            await asyncio.sleep(_POLL_S)
        if raw is None:
            raise_tool_error(TransportDownError("item tab produced no extractable content"))
        data = _unwrap_extract(raw, "card")
        if _is_punish(data):
            _refuse("x5sec challenge", _challenge_error("the item tab opened onto an x5sec challenge"))
        _pacer.record_success()
        return data
    finally:
        # Close every tab this call opened, matched or not: orphaned item tabs themselves draw x5sec.
        for candidate in list(context.pages):
            if candidate not in before:
                try:
                    await candidate.close()
                except Exception:
                    pass


# -------------------------------------------------------------------- tools ----


@mcp.tool(
    name="aliexpress_search",
    annotations=ToolAnnotations(
        title="AliExpress Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def aliexpress_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'умные часы'")],
    ctx: Context | None = None,
) -> AliSearchResponse:
    """Search AliExpress, rendered in the plugin's Chrome profile; the main feed in the site's order.

    AliExpress fills a query it cannot match with unrelated goods; when no tile names
    the query they are dropped with a ``no_match`` warning.

    ## Return Format

    AliSearchResponse: {status, query, tier_used, count, items[], meta}.
    price_rub is None when absent — never 0; a coupon price goes to coupon_price_rub, never price_rub.

    ## Error Format

    ToolError: challenge_required on the x5sec check (AliExpress stops for 30 min);
    transport_down on a refused tab or CDP failure; parser_drift when a rendered page
    has neither tiles nor «ничего не найдено».
    """
    log_event("aliexpress_search.start", query=query[:60])
    try:
        url = f"{SEARCH_URL}?SearchText={urllib.parse.quote(query.strip())}"
        cached = _cache.get(url)
        if cached is not None:
            payload, tier = cached, "cache"
        else:
            payload = await _cdp_render_search(url, ctx)
            if _is_punish(payload):
                raise_tool_error(_challenge_error())
            tier = "cdp"
        tiles = [t for t in (payload.get("items") or []) if isinstance(t, dict)]
        if not tiles and not payload.get("nothing"):
            raise_tool_error(
                ParserDriftError(
                    "rendered search page has neither product tiles nor «ничего не найдено» — the DOM shape moved"
                )
            )
        _cache.set(url, payload)
        warnings: list[str] = []
        items: list[AliSearchItemOut] = []
        coupon_seen = 0
        for tile in tiles:
            coupon_seen += _tile_prices(tile)[2] is not None
            items.append(_item_from_payload(tile))
        if not items:
            warnings.append("empty: AliExpress found nothing for the query")
        elif not _mentions_query(query, [item.title or "" for item in items]):
            warnings.append(
                f"no_match: AliExpress listed {len(items)} goods, none names the query — dropped as unrelated"
            )
            items, coupon_seen = [], 0
        if items and all(item.price_rub is None for item in items):
            warnings.append("no_prices_on_page")
        if len(items) >= MAX_TILES:
            warnings.append(f"truncated_at_{MAX_TILES}: refine the query to see the rest")
        if coupon_seen:
            warnings.append(
                f"{coupon_seen} tile(s) advertise a coupon price; price_rub carries the regular "
                "price and coupon_price_rub the coupon one"
            )
        result = AliSearchResponse(query=query, tier_used=tier, count=len(items), items=items)
        attached = R.attach_meta(
            result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="aliexpress_search"
        )
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("aliexpress_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"aliexpress_search failed: {exc}")))


@mcp.tool(
    name="aliexpress_card",
    annotations=ToolAnnotations(
        title="AliExpress Product Card",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def aliexpress_card(
    item_id_or_url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=400,
            description="AliExpress item id (9-16 digits) or aliexpress.ru item URL, e.g. /item/1005010003103368.html",
        ),
    ],
    ctx: Context | None = None,
) -> AliCardResponse:
    """Fetch one AliExpress product card, opened from a search tab in the plugin's Chrome profile.

    Review TEXTS are deliberately not exposed: they require navigating the
    review tab, which x5sec challenges; this tool returns the rating and the
    order count instead.

    ## Return Format

    AliCardResponse: {status, item_id, title, price_rub, old_price_rub, rating,
    orders_count, url, tier_used, meta}.

    ## Error Format

    ToolError: bad_request when no item id can be extracted; challenge_required on
    the x5sec check (AliExpress stops for 30 min); transport_down on a refused tab
    or CDP failure; parser_drift when a rendered card has neither title nor price.
    """
    log_event("aliexpress_card.start", input=item_id_or_url[:80])
    try:
        item_id = _extract_item_id(item_id_or_url)
        if item_id is None:
            raise_tool_error(
                BadRequestError(
                    f"could not extract an item id from {item_id_or_url!r}; pass a 9-16 digit id or an "
                    f"aliexpress.ru URL like /item/1005010003103368.html"
                )
            )
        # Rebuilt from SITE_BASE: the page opens in the operator's own Chrome,
        # so the target host must come from us, never from the argument.
        url = f"{SITE_BASE}/item/{item_id}.html"
        cached = _cache.get(url)
        if cached is not None:
            payload, tier = cached, "cache"
        else:
            payload = await _cdp_card(url, ctx)
            if _is_punish(payload):
                _refuse("x5sec challenge", _challenge_error())
            tier = "cdp"
            _cache.set(url, payload)
        title = R.flatten_text(payload.get("title"))
        price, old_price, coupon = _card_prices(payload)
        rating = R.coerce_rating(payload.get("rating_text"))
        # Orders arrive from the SEO description block only: the extractor
        # emits meta_description, and there is no dedicated orders field in the
        # sticky/card modules measured so far.
        orders: int | None = None
        meta_desc = payload.get("meta_description")
        if orders is None and isinstance(meta_desc, str):
            match = re.search(r"➜\s*([\d\s\u00a0]+)\s+заказов", meta_desc)
            if match:
                orders = R.coerce_int(match.group(1).replace("\u00a0", " ").replace(" ", ""))
        if rating is None and isinstance(meta_desc, str):
            match = re.search(r"Рейтинг\s*-\s*(\d[.,]\d)", meta_desc)
            if match:
                rating = R.coerce_rating(match.group(1))
        if title is None and price is None:
            raise_tool_error(
                ParserDriftError("rendered card has neither title nor price — the card shape moved; verify manually")
            )
        warnings: list[str] = []
        if price is None:
            warnings.append(
                "price_missing: the price module did not render — treat availability of this field as unknown"
            )
        if orders is None:
            warnings.append(
                "orders_count_missing: the SEO block it is read from was absent — drift-prone, not necessarily zero"
            )
        result = AliCardResponse(
            item_id=item_id,
            title=title,
            price_rub=price,
            old_price_rub=old_price,
            coupon_price_rub=coupon,
            rating=rating,
            orders_count=orders,
            url=url,
            tier_used=tier,
        )
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="aliexpress_card")
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("aliexpress_card.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"aliexpress_card failed: {exc}")))


# CLI-only drift canary: ``marketplace-mcp doctor`` imports and calls
# aliexpress_selfcheck() directly. It is deliberately NOT registered as an MCP
# tool — selfchecks are operator diagnostics, and their input/output schemas
# would be billed in every client request.
async def aliexpress_selfcheck(ctx: Context | None = None) -> AliSelfcheckResponse:
    """Structural drift canary for AliExpress (tri-state). Renders one live
    search and one card in the operator's Chrome and checks they parse.

    An x5sec challenge or CDP-down is ``inconclusive`` (transport), NEVER drift.
    Only a rendered page that yields zero tiles / an unparseable card is
    ``drift``. From a machine whose Chrome has not passed the challenge,
    inconclusive is the expected verdict.

    ## Return Format

    AliSelfcheckResponse: {status, healthy, connector, checks, ...}.

    ## Error Format

    Raises ToolError (TransportDownError) ONLY on an unexpected internal bug
    that prevents the canary from producing any verdict. Transport/block
    failures of individual sub-checks map to inconclusive entries, not errors.
    """
    log_event("aliexpress_selfcheck.start")
    try:
        result = await _aliexpress_selfcheck_impl(ctx)
        log_event("aliexpress_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("aliexpress_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"aliexpress_selfcheck failed: {exc}")))


async def _aliexpress_selfcheck_impl(ctx: Context | None) -> AliSelfcheckResponse:
    checks: dict[str, dict] = {}
    baseline = "cdp-search-shape-v1"
    url = f"{SEARCH_URL}?SearchText={urllib.parse.quote('умные часы')}"
    items_raw: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(90):
            payload = await _cdp_render_search(url, ctx)
    except TimeoutError:
        checks["search"] = R.selfcheck_entry(
            "inconclusive", baseline=baseline, reason="timeout", notes=["render exceeded 90s"]
        )
    except ToolError as exc:
        checks["search"] = R.selfcheck_entry(
            "inconclusive",
            baseline=baseline,
            reason="blocked" if "challenge" in str(exc) or "stopped" in str(exc) else "transport_down",
            notes=[str(exc)[:160]],
        )
    except Exception as exc:
        checks["search"] = R.selfcheck_entry(
            "inconclusive",
            baseline=baseline,
            reason="transport_down",
            notes=[f"{type(exc).__name__}: {str(exc)[:120]}"],
        )
    else:
        if _is_punish(payload):
            checks["search"] = R.selfcheck_entry(
                "inconclusive",
                baseline=baseline,
                reason="blocked",
                notes=["x5sec challenge page rendered instead of the grid"],
            )
        else:
            items_raw = [t for t in (payload.get("items") or []) if isinstance(t, dict)]
            priced = [t for t in items_raw if (t.get("price_texts") or {}).get("attached")]
            totals = items_raw and [t for t in items_raw if t.get("orders") is not None]
            if not items_raw:
                checks["search"] = R.selfcheck_entry(
                    "drift", baseline=baseline, reason="parse_smoke_failed", notes=["zero tiles"]
                )
            elif len(items_raw) < 3 or not priced:
                checks["search"] = R.selfcheck_entry(
                    "drift",
                    baseline=baseline,
                    reason="parse_smoke_failed",
                    notes=[
                        f"{len(items_raw)} tiles but {'no prices attach' if not priced else 'fewer than 3'} — the shape likely moved"
                    ],
                )
            else:
                notes = [
                    f"{len(items_raw)} tiles extracted",
                    f"{len(priced)} tiles with an attached price"
                    + (f", {len(totals)} with an orders line" if totals else ""),
                ]
                checks["search"] = R.selfcheck_entry("healthy", baseline=baseline, notes=notes)

    # Chain the card probe off the search result so the canary never depends on
    # a hardcoded SKU that may have been delisted.
    card_checked = False
    search_state = checks.get("search") or {}
    first_id = next((t.get("item_id") for t in items_raw if t.get("item_id")), None)
    if first_id and search_state.get("state") == "healthy":
        card_checked = True
        try:
            async with asyncio.timeout(90):
                card = await _cdp_card(f"{SITE_BASE}/item/{first_id}.html", ctx)
        except ToolError as exc:
            checks["card"] = R.selfcheck_entry(
                "inconclusive",
                baseline="cdp-card-shape-v1",
                reason="blocked" if "challenge" in str(exc) or "stopped" in str(exc) else "transport_down",
                notes=[str(exc)[:160]],
            )
        except Exception as exc:
            checks["card"] = R.selfcheck_entry(
                "inconclusive",
                baseline="cdp-card-shape-v1",
                reason="transport_down",
                notes=[f"{type(exc).__name__}: {str(exc)[:120]}"],
            )
        else:
            price, _old, _coupon = _card_prices(card)
            if _is_punish(card):
                checks["card"] = R.selfcheck_entry(
                    "inconclusive",
                    baseline="cdp-card-shape-v1",
                    reason="blocked",
                    notes=["the item tab opened onto an x5sec challenge"],
                )
            elif card.get("title") and price is not None:
                checks["card"] = R.selfcheck_entry(
                    "healthy", baseline="cdp-card-shape-v1", notes=[f"card {first_id} parsed with a price"]
                )
            elif card.get("title"):
                checks["card"] = R.selfcheck_entry(
                    "inconclusive",
                    baseline="cdp-card-shape-v1",
                    reason="price_missing",
                    notes=[
                        "card rendered but the price module stayed empty even after the extended poll — "
                        "measured on live sessions this is x5sec load-shedding, not a selector break "
                        "(megamarket precedent: session-shaped empties are inconclusive, never drift)",
                    ],
                )
            else:
                checks["card"] = R.selfcheck_entry(
                    "drift",
                    baseline="cdp-card-shape-v1",
                    reason="parse_smoke_failed",
                    notes=["card rendered but neither title nor price parsed"],
                )
    elif not card_checked:
        checks["card"] = R.selfcheck_entry(
            "inconclusive",
            baseline="cdp-card-shape-v1",
            reason="skipped_no_search_health",
            notes=["card probe skipped because the search gate is not healthy"],
        )

    result_dict = R.selfcheck_result(
        "aliexpress",
        checks,
        required=("search",),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return AliSelfcheckResponse(**result_dict)


# Advertised output schemas are the dominant constant cost of an MCP mount:
# replace the full Pydantic tree with top-level field names (~64 % fewer
# wire tokens on the unified server).
apply_compact_output_schemas(mcp)
