"""DNS extractors on the 2026-09-27 captures taken through the plugin's own tab.

The page came through Qrator's self-reloading 401 in the anonymous profile.
The city the site picked from the IP is redacted to «Город по IP».
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dns_connector import server
from mcp_core.domtest import JsdomUnavailable, run_extractor

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.dns-shop.ru/catalog/17a89aab16404e77/videokarty/?q=rtx+5070&stock=no"
CARD_URL = "https://www.dns-shop.ru/product/b58aaa7e00a9d582/"


def _extract(js_source: str, name: str, page_url: str) -> dict:
    try:
        return run_extractor(js_source, FIXTURES / name, page_url=page_url)
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))


def test_search_reads_every_tile_with_price_and_stock() -> None:
    payload = _extract(server._SEARCH_EXTRACT_JS, "search_videokarty_live.html", SEARCH_URL)
    items = [server._search_item_from_tile(tile) for tile in payload["items"]]

    assert [(i.product_id, i.price_rub, i.old_price_rub, i.in_stock) for i in items] == [
        ("b58aaa7e00a9d582", 90299.0, None, True),
        ("3e9973b9d4b9d582", 89299.0, None, True),
        ("8f0eced0eaa9d582", 90299.0, None, True),
        ("fe5cfcefe77fd582", 150999.0, None, None),
    ]
    assert items[3].availability == "При заказе"
    assert payload["city"] == "Город по IP"


def test_card_reads_price_stock_and_city_not_the_instalment() -> None:
    payload = _extract(server._CARD_EXTRACT_JS, "card_palit_live.html", CARD_URL)

    assert payload["title"] == "Видеокарта Palit GeForce RTX 5070 Infinity 3 [NE75070019K9-GB2050S]"
    assert server.R.price_from_texts(payload["price_text"]) == 90299.0
    assert payload["old_price_text"] is None
    assert payload["is_available"] is True
    assert payload["city"] == "Город по IP"
