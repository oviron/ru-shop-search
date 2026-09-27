"""Citilink extractors on the 2026-09-27 captures taken through the plugin's own tab.

The city the site picked from the IP is redacted to «Город по IP».
"""

from __future__ import annotations

from pathlib import Path

import pytest
from citilink_connector import server
from mcp_core.domtest import JsdomUnavailable, run_extractor

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.citilink.ru/search/?text=rtx%205070"
NOTHING_URL = "https://www.citilink.ru/search/?text=zxqvwj%20pplkq"
CARD_URL = (
    "https://www.citilink.ru/product/videokarta-palit-pci-e-rtx5070-infinity-3-nv-rtx5070-12gb-192bit-gddr7-2092141/"
)


def _extract(js_source: str, name: str, page_url: str) -> dict:
    try:
        return run_extractor(js_source, FIXTURES / name, page_url=page_url)
    except JsdomUnavailable as exc:
        pytest.skip(str(exc))


def test_search_reads_price_discount_rating_and_stock() -> None:
    payload = _extract(server._SEARCH_EXTRACT_JS, "search_rtx5070_live.html", SEARCH_URL)
    items = [server._search_item(tile) for tile in payload["items"]]

    assert [(i.price_rub, i.old_price_rub, i.rating, i.review_count, i.in_stock) for i in items] == [
        (96690.0, None, 4.8, 13, True),
        (94560.0, None, 4.9, 18, True),
        (105500.0, 114000.0, 4.7, 7, True),
        (None, None, 5.0, 2, False),
    ]
    assert items[1].product_id == "videokarta-palit-pci-e-rtx5070-infinity-3-nv-rtx5070-12gb-192bit-gddr7-2092141"
    assert items[1].url == CARD_URL
    assert items[1].title == (
        "Видеокарта Palit NVIDIA GeForce RTX 5070 PA-RTX5070 Infinity 3 12ГБ Infinity 3, GDDR7, Ret [ne75070019k9-gb2050s]"
    )
    assert items[3].title == "Видеокарта ASUS NVIDIA GeForce RTX 5070TI PRIME-RTX5070TI-O16G"
    assert (payload["city"], payload["nothing"]) == ("Город по IP", False)


def test_nothing_found_skips_the_priced_recommendations() -> None:
    payload = _extract(server._SEARCH_EXTRACT_JS, "search_nothing_live.html", NOTHING_URL)

    assert (payload["items"], payload["nothing"]) == ([], True)


def test_card_reads_the_product_from_next_data() -> None:
    payload = _extract(server._CARD_EXTRACT_JS, "card_palit_live.html", CARD_URL)

    product = payload["product"]
    assert (product["id"], product["isAvailable"], product["rating"]) == ("2092141", True, 4.9)
    assert (product["price"]["current"], product["price"]["old"]) == ("94560", "0")
    assert product["counters"]["opinions"] == 18
    assert set(product) == {"id", "name", "isAvailable", "price", "brand", "rating", "counters"}
    assert (payload["city"], payload["not_found"]) == ("Город по IP", False)
