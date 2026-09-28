"""DNS-Shop MCP connector.

DNS sits behind Qrator's JavaScript proof-of-work: every dynamic page answers
HTTP 401 with a ~349 KB challenge script, and the one route not behind it
(``/ajax-state/product-buy/``) wants a CSRF token you can only get from a page
that *is* behind it. No header trick clears a proof-of-work — but a real
Chrome executes it natively, so every read here runs in the operator's browser
over CDP and parses the rendered DOM.

Verified live July 2026 from a datacenter IP (docs/ANTI_BOT.md):
  - every dynamic page — 401 + Qrator challenge
  - ``restapi.dns-shop.ru`` — 403 even from residential (internal SSR address)
  - anonymously reachable: ``robots.txt``, ``sitemap.xml`` (~600k product URLs)

The CDP route needs verification from the operator's machine: the DOM tile
selectors below are written against the public catalog layout, and only a
challenge-passed session can confirm them. Run dns_selfcheck from your Chrome
first — its verdict tells you which case you are in.

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
from fastmcp.server.middleware.error_handling import RetryMiddleware
from mcp.types import ToolAnnotations
from mcp_core import resilience as R
from mcp_core.cache import TTLCache
from mcp_core.dom import JS_HELPERS
from mcp_core.errors import (
    BadRequestError,
    ConnectorError,
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

from dns_connector.models_output import (
    DnsCardResponse,
    DnsPriceHistoryResponse,
    DnsPricePoint,
    DnsSearchItemOut,
    DnsSearchResponse,
    DnsSelfcheckResponse,
    MetaOut,
)
from dns_connector.settings import get_settings
from dns_connector.shape_reference import SEARCH_REQUIRED_KEYS, SEARCH_SHAPE_REFERENCE

_settings = get_settings()

SERVER_VERSION = "2.4.2"
SERVER_STARTED_AT = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")

SITE_BASE = "https://www.dns-shop.ru"
SEARCH_URL = f"{SITE_BASE}/search/"

TIMEOUT = _settings.timeout
MAX_BODY_BYTES = _settings.max_body_bytes
_min_gap = _settings.min_gap

# Real product routes, confirmed live July 2026 from a challenge-passed session:
#   citilink  /product/noutbuk-lenovo-2169270/   slug ending in a numeric id
#   dns       /product/b7a1667f9b19ed20/         16-hex id
# The original pattern demanded 24 hex — a MongoDB ObjectId shape neither site
# uses — so every search returned zero tiles and reported drift.
#
# The charset is deliberately narrower than "anything but a slash". This id is
# interpolated straight back into a URL that opens in the operator's logged-in
# Chrome, so `?`, `#`, `%` and `.` stay out: they would let a caller append a
# query string or walk the path on a page we then render with their cookies.
_ID_CHARS = r"[A-Za-z0-9_-]"
_PRODUCT_ID_RE = re.compile(rf"/product/({_ID_CHARS}+)")
_BARE_ID_RE = re.compile(rf"{_ID_CHARS}{{4,}}")

mcp = FastMCP(
    name="dns-connector",
    version=SERVER_VERSION,
    instructions=(
        "DNS-Shop electronics catalog: search and product cards. Every read "
        "runs in the operator's own Chrome over CDP — Qrator's proof-of-work "
        "blocks all anonymous access. Start with dns_search; dns_card takes a "
        "product id or dns-shop.ru product URL."
    ),
)
mcp.add_middleware(RetryMiddleware())

_REFUSAL_STATUSES = frozenset({401, 403, 429})
# Qrator's 401 reloads itself into 200 in ~2 s (live, 2026-09-27).
_CHALLENGE_GRACE_S = 12.0
_PRICE_WAIT_MS = 8000

_cache: TTLCache = TTLCache(ttl_s=_settings.cache_ttl, max_entries=128)
_pacer = Pacer(_min_gap, source="dns")
_cdp_lock = asyncio.Lock()


async def _polite_wait() -> None:
    """Space this source's requests out, and back off if it refused us.

    Reads ``_min_gap`` at call time so an operator or a test can retune the
    pace without rebuilding the pacer.
    """
    await _pacer.wait(min_gap=_min_gap)


def _extract_product_id(raw: str) -> str | None:
    """Pull the product id out of a dns-shop.ru product URL or a bare id.

    Host-checked on purpose. The id is the only thing that survives this
    function, and the card URL is rebuilt from SITE_BASE, so a caller cannot
    smuggle ``/product/<id>`` into a path on someone else's domain and
    steer the operator's logged-in Chrome there. Anything that is not
    dns-shop.ru, or carries a scheme we do not navigate, returns None.
    """
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
        if host != "dns-shop.ru" and not host.endswith(".dns-shop.ru"):
            return None
        raw = parts.path
    elif raw.startswith("//"):
        # Scheme-relative: the authority is someone else's, so treat it as off-host.
        return None
    match = _PRODUCT_ID_RE.search(raw)
    return match.group(1).lower() if match else None


# Why this extractor returns TEXT and not numbers
# ------------------------------------------------
# Audit 2026-07-28, against the live grid (query «ноутбук», Moscow):
#
#   1. It keyed on `a[href*="/product/"]` and walked up with
#      `closest('[class*="catalog-product"], ..., li, div')`. `closest()` tests
#      THE ELEMENT ITSELF before any ancestor, and the first product anchor in a
#      DNS tile is the image link, whose own class is
#      `catalog-product__image-link` — which matches `[class*="catalog-product"]`.
#      So the "card" resolved to that anchor, not to the tile. Measured on the
#      captured grid: the resolved node has textContent length 0, the real tile
#      root has 402. Every tile therefore produced title=null and price=null
#      while the page showed 48 perfectly good products.
#      The tile ROOT is the single BEM block `.catalog-product`; the inner nodes
#      are `catalog-product__*`, which is why an exact class match finds the root
#      and a substring match cannot distinguish them. Tiles are now selected
#      directly and never inferred by walking up from an anchor.
#
#   2. Price was `Math.min` over lines matching /руб|₽/. DNS tiles carry
#      "от 5 751 ₽/ мес." next to a 58 999 ₽ price, so the minimum is the
#      monthly instalment. That is worse than null: it validates, it looks
#      plausible, and it is wrong. Confirmed on the live tile for HUAWEI
#      MateBook D 16 (58 999 ₽ shown, 5 751 ₽/мес instalment).
#
#   3. The strikethrough price lives in `.product-buy__prev` and carries NO
#      currency glyph ("54 999"), so the /руб|₽/ filter could never see it.
#
# So the JS no longer does arithmetic. It returns the raw display strings from
# named elements and lets mcp_core.resilience.coerce_price parse them, because
# that helper already handles non-breaking spaces, a missing glyph and comma
# decimals — and refuses an ambiguous multi-number blob instead of concatenating
# it into nonsense.
#
# It also reads textContent rather than innerText. innerText depends on layout
# and CSS visibility, which is exactly the kind of thing that differs between a
# warm tab and a freshly navigated one; textContent is stable and is what the
# fixture test can assert against.
_SEARCH_EXTRACT_TEMPLATE = """
() => {
    //__SHARED_HELPERS__
    // Search results render inside an iframe in at least one of this site's
    // layouts — confirmed live in July 2026, where the top-level document held
    // zero product anchors while the grid sat in a same-origin frame. Reading
    // only `document` makes that layout look like a dead parser. Collect the
    // frames we are allowed to read and treat them as one page.
    const docs = [document];
    for (const frame of document.querySelectorAll('iframe')) {
        try {
            if (frame.contentDocument) docs.push(frame.contentDocument);
        } catch (e) {
            // Cross-origin frame: not ours to read, and not where our tiles are.
        }
    }

    // The tile root, resolved without closest(). Exact-match the BEM block so
    // `catalog-product__image` cannot masquerade as the tile.
    const tiles = [];
    for (const doc of docs) {
        for (const t of doc.querySelectorAll('.catalog-product')) tiles.push(t);
    }
    // Fallback for a renamed grid: take each product anchor's OUTERMOST ancestor
    // that still holds exactly one product link, which is the tile by definition.
    if (!tiles.length) {
        const seenTile = new Set();
        for (const doc of docs) {
            for (const a of doc.querySelectorAll('a[href*="/product/"]')) {
                let node = a, best = a;
                for (let hop = 0; hop < 8 && node.parentElement; hop++) {
                    node = node.parentElement;
                    const links = node.querySelectorAll('a[href*="/product/"]');
                    const ids = new Set();
                    for (const l of links) {
                        const mm = (l.href || '').match(/\\/product\\/([A-Za-z0-9_-]+)/);
                        if (mm) ids.add(mm[1]);
                    }
                    if (ids.size > 1) break;
                    best = node;
                }
                if (!seenTile.has(best)) { seenTile.add(best); tiles.push(best); }
            }
        }
    }

    const out = [];
    const seen = new Set();
    for (const tile of tiles) {
        // Prefer the NAME link: the image link carries no text.
        const nameEl = tile.querySelector('a.catalog-product__name')
            || tile.querySelector('a[href*="/product/"][title]')
            || tile.querySelector('a[href*="/product/"]');
        if (!nameEl) continue;
        const href = nameEl.href || '';
        const m = href.match(/\\/product\\/([A-Za-z0-9_-]+)/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);

        // Drop the bracketed short-spec blob from the title; keep the product name.
        const title = cleanTextWithout(nameEl, ['.catalog-product__short-specs'])
            || nameEl.getAttribute('title')
            || null;

        // Current price: the price node minus the strikethrough child and minus
        // the instalment sub-line. `.product-buy__sub` is the "от N ₽/ мес." decoy.
        const priceEl = tile.querySelector('.product-buy__price')
            || tile.querySelector('[class*="product-buy__price"]');
        const priceText = cleanTextWithout(priceEl, ['.product-buy__prev', '.product-buy__sub', '.product-buy__hint']);
        const oldPriceText = cleanText(tile.querySelector('.product-buy__prev'));
        const availText = cleanText(tile.querySelector('.available')) || cleanText(tile.querySelector('[class*="available"]'));

        out.push({
            product_id: m[1],
            title: title,
            price_text: priceText,
            old_price_text: oldPriceText,
            availability_text: availText,
            url: href
        });
        if (out.length >= 48) break;
    }
    const city = cleanText(document.querySelector('[class^="city-select__text"]'));
    return JSON.stringify({items: out, title: document.title || '', city: city});
}
"""

_CARD_EXTRACT_TEMPLATE = """
() => {
    //__SHARED_HELPERS__
    // h1 is the product name on a card page; the generic [class*="title"] fallback
    // could otherwise pick a section heading.
    const titleEl = document.querySelector('h1')
        || document.querySelector('[class*="product-card-top__title"]');
    const title = cleanText(titleEl) || document.title || null;

    // Named price nodes, not a scan of body text: the body carries recommended
    // products, instalment offers and bonus amounts, any of which is smaller than
    // the real price and would win a Math.min.
    const priceEl = document.querySelector('.product-buy__price')
        || document.querySelector('[class*="product-buy__price"]');
    const priceText = cleanTextWithout(priceEl, ['.product-buy__prev', '.product-buy__sub', '.product-buy__hint']);
    const oldPriceText = cleanText(document.querySelector('.product-buy__prev'));

    const availEl = document.querySelector('.order-avail-wrap .available')
        || document.querySelector('.available');
    const availText = cleanText(availEl);
    // Only decide availability from an explicit signal. A page whose stock block
    // has not rendered must read "unknown" (null), not "in stock".
    let available = null;
    if (availText) {
        available = !/Нет в наличии|Под заказ|Закончился/i.test(availText);
    } else if (document.body) {
        // Fallback scan of body text. textContent, not innerText: innerText
        // depends on layout visibility, is unavailable in jsdom, and this
        // fallback exists so the DOM fixture can assert on it.
        const body = (document.body.textContent || '').replace(/\\s+/g, ' ');


        if (/Нет в наличии|Под заказ|Закончился/i.test(body)) available = false;
        else if (/В наличии/i.test(body)) available = true;
    }




    return JSON.stringify({
        title: title,
        price_text: priceText,
        old_price_text: oldPriceText,
        availability_text: availText,
        is_available: available,
        city: cleanText(document.querySelector('[class^="city-select__text"]')),
        page_title: document.title || ''
    });
}
"""

# Shared with every other CDP source through mcp_core.dom, so a fix to tile
# resolution or price selection lands on all of them at once instead of being
# copied four times and drifting three ways.
_SEARCH_EXTRACT_JS = _SEARCH_EXTRACT_TEMPLATE.replace("//__SHARED_HELPERS__", JS_HELPERS)
_CARD_EXTRACT_JS = _CARD_EXTRACT_TEMPLATE.replace("//__SHARED_HELPERS__", JS_HELPERS)


def _search_item_from_tile(tile: dict[str, Any]) -> DnsSearchItemOut:
    """Map one extracted tile onto the wire shape, parsing prices in Python.

    Accepts BOTH shapes on purpose. The extractor now returns ``price_text`` /
    ``old_price_text``, but a payload cached by an older build (or a fixture
    written against it) still carries numeric ``price_rub``, and a cache entry
    outliving a deploy must not start answering with nulls. The wire shape is
    unchanged either way: ``price_rub`` is a float or None, never 0.
    """
    price = R.price_from_texts(tile.get("price_text"), tile.get("price_rub"))
    old_price = R.price_from_texts(tile.get("old_price_text"), tile.get("old_price_rub"))
    # A strikethrough below the current price is a drifted read, not a discount:
    # report the pair only when it is internally consistent.
    if price is not None and old_price is not None and old_price <= price:
        old_price = None
    availability = R.flatten_text(tile.get("availability_text"))
    return DnsSearchItemOut(
        product_id=tile.get("product_id"),
        title=R.flatten_text(tile.get("title")),
        price_rub=price,
        old_price_rub=old_price,
        availability=availability,
        in_stock=_in_stock(availability),
        url=tile.get("url"),
    )


_ABSENT_RE = re.compile(r"нет в наличии|закончил|нет на складе|снят с продажи", re.IGNORECASE)


def _in_stock(label: str | None) -> bool | None:
    """«При заказе» means DNS brings it in later: not explicitly in stock, so unknown."""
    if not label:
        return None
    if _ABSENT_RE.search(label):
        return False
    return True if re.match(r"\s*в наличии", label, re.IGNORECASE) else None


def _parse_extract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BODY_BYTES:
        raise ToolError(TransportDownError("extracted page data missing or over the body cap"))
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ToolError(ParserDriftError("page extractor returned a non-object payload"))
    return data


def _has_prices(data: dict[str, Any]) -> bool:
    items = data.get("items")
    if isinstance(items, list):
        return any(isinstance(it, dict) and it.get("price_text") for it in items)
    return bool(data.get("price_text"))


def _blocked(status: int) -> TransportDownError:
    return TransportDownError(
        f"DNS refused the browser tab (HTTP {status}); DNS is stopped for 30 min, do not retry. "
        "If the profile window shows a check, it may be passed by hand, then `marketplace-mcp resume dns`.",
        status_code=status,
    )


async def _cdp_render(url: str, extract_js: str, wait_ms: int, ctx: Context | None) -> dict[str, Any]:
    """Open ``url`` in the plugin's Chrome, let Qrator pass, extract once prices show.

    Qrator answers a tab without its cookie with 401 and a script that reloads the
    page into 200 by itself; ``open_page`` waits for that. A 401/403/429 that stays
    stops DNS. ``wait_ms`` caps how long to wait for prices to render.
    """
    async with _cdp_lock:
        try:
            await _polite_wait()
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(url, wait_ms=0, challenge_grace_s=_CHALLENGE_GRACE_S) as page:
                deadline = asyncio.get_running_loop().time() + wait_ms / 1000
                while True:
                    data = _parse_extract(await asyncio.wait_for(page.evaluate(extract_js), timeout=30.0))
                    if _has_prices(data) or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.5)
        except NavBlocked as exc:
            if exc.status in _REFUSAL_STATUSES:
                _pacer.record_refusal(f"http {exc.status}")
                raise_tool_error(_blocked(exc.status))
            raise_tool_error(TransportDownError(f"DNS page failed (HTTP {exc.status})", status_code=exc.status))
    if _is_qrator_wall(data):
        _pacer.record_refusal("access-restricted page")
        raise_tool_error(_blocked(403))
    _pacer.record_success()
    return data


def _is_qrator_wall(data: dict[str, Any]) -> bool:
    title = str(data.get("title") or data.get("page_title") or "").lower()
    return not _has_prices(data) and ("доступ ограничен" in title or "qrator" in title)


@mcp.tool(
    name="dns_search",
    annotations=ToolAnnotations(
        title="DNS-Shop Search", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def dns_search(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Search text, e.g. 'ноутбук lenovo'")],
    ctx: Context | None = None,
) -> DnsSearchResponse:
    """Search DNS-Shop, rendered in the operator's Chrome.

    ## Return Format

    DnsSearchResponse: {status, query, tier_used, count, items[], meta}.
    price_rub is None when absent — never 0.

    ## Error Format

    ToolError: TransportDownError on CDP/Qrator failures; ParserDriftError when
    a rendered page yields zero product tiles.
    """
    log_event("dns_search.start", query=query[:60])
    try:
        url = f"{SEARCH_URL}?q={urllib.parse.quote(query.strip())}"
        cached = _cache.get(url)
        if cached is not None:
            payload, tier = cached, "cache"
        else:
            payload = await _cdp_render(url, _SEARCH_EXTRACT_JS, wait_ms=_PRICE_WAIT_MS, ctx=ctx)
            tier = "cdp"
            _cache.set(url, payload)
        items_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        if not items_raw:
            raise_tool_error(
                ParserDriftError(
                    "rendered search page yielded zero product tiles — either the query matched nothing or the DOM shape moved; verify manually"
                )
            )
        items = [_search_item_from_tile(it) for it in items_raw if isinstance(it, dict)]
        warnings: list[str] = []
        if items and all(it.price_rub is None for it in items):
            warnings.append("no_prices_on_page")
        result = DnsSearchResponse(
            query=query, tier_used=tier, city=R.flatten_text(payload.get("city")), count=len(items), items=items
        )
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), warnings, source="dns_search")
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("dns_search.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"dns_search failed: {exc}")))


@mcp.tool(
    name="dns_card",
    annotations=ToolAnnotations(
        title="DNS-Shop Product Card", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
)
async def dns_card(
    product_url: Annotated[
        str,
        Field(
            min_length=1,
            max_length=400,
            description="dns-shop.ru product URL containing /product/<id>/, e.g. /product/b7a1667f9b19ed20/",
        ),
    ],
    ctx: Context | None = None,
) -> DnsCardResponse:
    """Fetch one DNS-Shop product card.

    ## Return Format

    DnsCardResponse: {status, product_id, title, price_rub, old_price_rub,
    is_available, url, tier_used, meta}.

    ## Error Format

    ToolError: BadRequestError when the URL carries no product id;
    TransportDownError on CDP/Qrator failures; ParserDriftError when a rendered
    card has neither title nor price.
    """
    log_event("dns_card.start", input=product_url[:80])
    try:
        product_id = _extract_product_id(product_url)
        if product_id is None:
            raise_tool_error(
                BadRequestError(
                    f"could not extract a product id from {product_url!r}; pass a dns-shop.ru URL with /product/<id>/ "
                    f"(the id is a 16-hex string, e.g. /product/b7a1667f9b19ed20/)"
                )
            )
        # Rebuild from SITE_BASE rather than navigating what the caller passed:
        # this page opens in the operator's own logged-in Chrome, so the target
        # host must come from us, never from the argument.
        url = f"{SITE_BASE}/product/{product_id}/"
        cached = _cache.get(url)
        if cached is not None:
            payload, tier = cached, "cache"
        else:
            payload = await _cdp_render(url, _CARD_EXTRACT_JS, wait_ms=_PRICE_WAIT_MS, ctx=ctx)
            tier = "cdp"
            _cache.set(url, payload)
        title = R.flatten_text(payload.get("title"))
        # price_text is what the current extractor returns; price_rub keeps a
        # cache entry written by an older build readable.
        price = R.price_from_texts(payload.get("price_text"), payload.get("price_rub"))
        old_price = R.price_from_texts(payload.get("old_price_text"), payload.get("old_price_rub"))
        if price is not None and old_price is not None and old_price <= price:
            old_price = None
        if title is None and price is None:
            raise_tool_error(
                ParserDriftError("rendered card has neither title nor price — the card shape moved; verify manually")
            )
        result = DnsCardResponse(
            product_id=product_id,
            title=title,
            price_rub=price,
            old_price_rub=old_price,
            is_available=payload.get("is_available"),
            city=R.flatten_text(payload.get("city")),
            url=url,
            tier_used=tier,
        )
        attached = R.attach_meta(result.model_dump(by_alias=True, exclude={"meta"}), [], source="dns_card")
        result.meta = MetaOut(**attached["_meta"])
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("dns_card.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"dns_card failed: {exc}")))


_HISTORY_PAGE_JS = """
() => {
    let guid = null;
    try {
        guid = JSON.parse(document.querySelector('[data-products]').getAttribute('data-products'))[0].id || null;
    } catch (e) {}
    const h1 = document.querySelector('h1');
    return JSON.stringify({guid: guid, title: document.title || '', h1: h1 ? h1.textContent.trim() : null});
}
"""

_HISTORY_FETCH_JS = """
async (guid) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    const body = new FormData();
    body.append('productId', guid);
    const r = await fetch('/catalog/product/get-product-price-history/', {
        method: 'POST', body: body, credentials: 'same-origin',
        headers: {'X-CSRF-Token': meta ? meta.content : '', 'X-Requested-With': 'XMLHttpRequest',
                  'Accept': 'application/json, text/javascript, */*; q=0.01'}});
    return JSON.stringify({status: r.status, text: (await r.text()).slice(0, 500000)});
}
"""
_WEEK_RE = re.compile(r"^(\d{2})\.(\d{2})-\d{2}\.\d{2}$")


def _today() -> datetime.date:
    return datetime.date.today()


def _iso_weeks(labels: list[str], today: datetime.date) -> list[str]:
    """Week starts for DNS labels without a year: the newest week is this year, walking back across New Year."""
    out: list[str] = []
    year, later_month = today.year, today.month
    for label in reversed(labels):
        m = _WEEK_RE.match(label)
        if not m:
            out.append("")
            continue
        day, month = int(m.group(1)), int(m.group(2))
        if month > later_month:
            year -= 1
        later_month = month
        try:
            out.append(datetime.date(year, month, day).isoformat())
        except ValueError:
            out.append("")
    return out[::-1]


async def _history_render(url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """One tab on the product page, then, after a pause, one same-origin POST for its price history."""
    async with _cdp_lock:
        try:
            await _polite_wait()
        except SourceStoppedError as exc:
            raise_tool_error(exc)
        try:
            async with open_page(url, wait_ms=0, challenge_grace_s=_CHALLENGE_GRACE_S) as page:
                deadline = asyncio.get_running_loop().time() + _PRICE_WAIT_MS / 1000
                while True:
                    info = _parse_extract(await asyncio.wait_for(page.evaluate(_HISTORY_PAGE_JS), timeout=30.0))
                    if info.get("guid") or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.5)
                if not info.get("guid"):
                    if "доступ ограничен" in str(info.get("title") or "").lower():
                        _pacer.record_refusal("access-restricted page")
                        raise_tool_error(_blocked(403))
                    raise_tool_error(ParserDriftError("no product GUID on the DNS page (data-products moved?)"))
                try:
                    await _polite_wait()
                except SourceStoppedError as exc:
                    raise_tool_error(exc)
                answer = _parse_extract(
                    await asyncio.wait_for(page.evaluate(_HISTORY_FETCH_JS, info["guid"]), timeout=30.0)
                )
        except NavBlocked as exc:
            if exc.status in _REFUSAL_STATUSES:
                _pacer.record_refusal(f"http {exc.status}")
                raise_tool_error(_blocked(exc.status))
            raise_tool_error(TransportDownError(f"DNS page failed (HTTP {exc.status})", status_code=exc.status))
    status = answer.get("status")
    if status in _REFUSAL_STATUSES:
        _pacer.record_refusal(f"history http {status}")
        raise_tool_error(_blocked(status))
    if status != 200:
        raise_tool_error(TransportDownError(f"DNS price history HTTP {status}", status_code=status or None))
    try:
        body = json.loads(str(answer.get("text") or ""))
    except json.JSONDecodeError:
        raise_tool_error(ParserDriftError("DNS price history is not JSON"))
    data = body.get("data") if isinstance(body, dict) and body.get("result") else None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise_tool_error(ParserDriftError("DNS price history has no data.groups"))
    _pacer.record_success()
    return info, data


@mcp.tool(
    name="dns_price_history",
    annotations=ToolAnnotations(
        title="DNS-Shop Price History",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def dns_price_history(
    product_url: Annotated[
        str, Field(min_length=1, max_length=400, description="dns-shop.ru product URL containing /product/<id>/.")
    ],
) -> DnsPriceHistoryResponse:
    """Weekly average selling prices DNS shows on a product card (about half a year).

    ## Return Format

    DnsPriceHistoryResponse: {product_id, title, points: [{week, label, price_rub}],
    price_range, url, meta}. Oldest first; the last week is the current, unfinished one.

    ## Error Format

    ToolError: bad_request for a URL without a product id; transport_down with
    status_code when DNS refuses the tab or the request (401/403/429 stop DNS);
    parser_drift when the page has no product GUID or the answer has no weeks.
    """
    log_event("dns_price_history.start", input=product_url[:80])
    try:
        product_id = _extract_product_id(product_url)
        if product_id is None:
            raise_tool_error(BadRequestError(f"could not extract a product id from {product_url!r}"))
        url = f"{SITE_BASE}/product/{product_id}/"
        info, data = await _history_render(url)
        labels, prices = [], []
        for group in data["groups"]:
            for point in group.get("points") or [] if isinstance(group, dict) else []:
                price = R.coerce_price(point.get("priceNum")) if isinstance(point, dict) else None
                if price:
                    labels.append(str(point.get("label") or ""))
                    prices.append(price)
        weeks = _iso_weeks(labels, _today())
        points = [
            DnsPricePoint(week=w, label=lab, price_rub=p) for w, lab, p in zip(weeks, labels, prices, strict=True)
        ]
        range_text = data.get("priceRange")
        result = DnsPriceHistoryResponse(
            product_id=product_id,
            title=R.flatten_text(info.get("h1")),
            points=points,
            price_range=range_text if isinstance(range_text, str) else None,
            url=url,
        )
        result.meta = MetaOut(source="dns_price_history", healthy=True)
        log_event("dns_price_history.done", product_id=product_id, points=len(points))
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("dns_price_history.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"dns_price_history failed: {exc}")))


# CLI-only drift canary: ``marketplace-mcp doctor`` imports and calls dns_selfcheck()
# directly. It is deliberately NOT registered as an MCP tool — selfchecks are
# operator diagnostics, and their input/output schemas would be billed in every
# client request.
async def dns_selfcheck(ctx: Context | None = None) -> DnsSelfcheckResponse:
    """Structural drift canary for DNS-Shop (tri-state). Renders one live search
    page in the operator's Chrome and checks tiles extract.

    Qrator-blocked or CDP-down is ``inconclusive`` (transport), NEVER drift.
    Only a rendered page that yields zero tiles is ``drift``.

    ## Return Format

    DnsSelfcheckResponse: {status, healthy, connector, checks, ...}.

    ## Error Format

    Raises ToolError (TransportDownError) ONLY on an unexpected internal bug
    that prevents the canary from producing any verdict. Transport/block
    failures of individual sub-checks map to inconclusive entries, not errors.
    """
    log_event("dns_selfcheck.start")
    try:
        result = await _dns_selfcheck_impl(ctx)
        log_event("dns_selfcheck.done", status=result.status)
        return result
    except ToolError:
        raise
    except ConnectorError as exc:
        raise_tool_error(exc)
    except Exception as exc:
        log_event("dns_selfcheck.error", error=_redact(str(exc)), exc_type=type(exc).__name__)
        raise_tool_error(TransportDownError(_redact(f"dns_selfcheck failed: {exc}")))


async def _dns_selfcheck_impl(ctx: Context | None) -> DnsSelfcheckResponse:
    checks: dict[str, dict] = {}
    baseline = "cdp-search-shape-v1"
    url = f"{SEARCH_URL}?q={urllib.parse.quote('ноутбук')}"
    try:
        async with asyncio.timeout(90):
            payload = await _cdp_render(url, _SEARCH_EXTRACT_JS, wait_ms=_PRICE_WAIT_MS, ctx=ctx)
    except TimeoutError:
        checks["search"] = R.selfcheck_entry(
            "inconclusive", baseline=baseline, reason="timeout", notes=["render exceeded 90s"]
        )
    except Exception as exc:
        checks["search"] = R.selfcheck_entry(
            "inconclusive",
            baseline=baseline,
            reason="transport_down",
            notes=[f"{type(exc).__name__}: {str(exc)[:120]}"],
        )
    else:
        items_raw = payload.get("items") if isinstance(payload.get("items"), list) else []
        if items_raw:
            # Tiles extract — now ask the second question: did the SHAPE move?
            # The registry was measured on a captured page; a live payload that
            # loses a parser-critical path is structural drift even while tiles
            # still come back. Types may vary with the data (str/null), so the
            # required check is on key presence, not on the full signature.
            live_signature = R.shape_signature(payload)
            drift = R.diff_keys(SEARCH_SHAPE_REFERENCE, live_signature)
            missing_required = [
                key for key in SEARCH_REQUIRED_KEYS if not any(entry.startswith(f"{key}:") for entry in live_signature)
            ]
            if missing_required:
                checks["search"] = R.selfcheck_entry(
                    "drift",
                    baseline=baseline,
                    reason="shape_drift",
                    notes=[
                        f"{len(items_raw)} tiles extracted",
                        f"required paths missing: {', '.join(missing_required)}",
                    ],
                    shape_missing=drift["missing"],
                    shape_added=drift["added"],
                )
            else:
                notes = [f"{len(items_raw)} tiles extracted", "shape matches the captured reference"]
                if drift["added"]:
                    notes.append(f"{len(drift['added'])} new paths vs baseline (informational)")
                checks["search"] = R.selfcheck_entry(
                    "healthy", baseline=baseline, notes=notes, shape_added=drift["added"]
                )
        else:
            checks["search"] = R.selfcheck_entry(
                "drift", baseline=baseline, reason="parse_smoke_failed", notes=["zero tiles"]
            )

    result_dict = R.selfcheck_result(
        "dns",
        checks,
        required=("search",),
        server_version=SERVER_VERSION,
        server_started_at=SERVER_STARTED_AT,
        process_id=None,
    )
    return DnsSelfcheckResponse(**result_dict)


# Advertised output schemas are the dominant constant cost of an MCP mount:
# replace the full Pydantic tree with top-level field names (~64 % fewer
# wire tokens on the unified server).
apply_compact_output_schemas(mcp)
