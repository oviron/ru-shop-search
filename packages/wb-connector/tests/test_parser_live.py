"""The WB card-item parser against LIVE captured API bodies.

``fixtures/search_v18_live.json`` is the v18 search answer for «rtx 5070» captured
2026-09-27 and ``fixtures/card_v4_live.json`` the card.wb.ru v4 answer for «ноутбук»
captured 2026-08-07, both trimmed (provenance in the ``*.provenance.json`` files).
They run the exact flattening ``wb_search`` and ``wb_card`` use on real bytes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from wb_connector import server
from wb_connector.models_output import WbCardItem

FIXTURES = Path(__file__).parent / "fixtures"

# Values read off the live capture when the fixtures were built.
SEARCH_EXPECTED = [
    {
        "nm_id": 355329855,
        "name": "Видеокарта RTX 5070 12 ГБ SHADOW 2X OC",
        "brand": "MSI",
        "color": "",
        "price_rub": 101812.0,
        "price_original_rub": 142995.0,
        "total_quantity": 24,
        "in_stock": True,
    },
    {
        "nm_id": 370245679,
        "name": "Видеокарта RTX 5070 12 ГБ GAMING OC",
        "brand": "Gigabyte",
        "color": "",
        "price_rub": 101033.0,
        "price_original_rub": 141901.0,
        "total_quantity": 37,
        "in_stock": True,
    },
    {
        "nm_id": 445317378,
        "name": "Видеокарта RTX 5070 EAGLE OC ICE SFF 12G, 12 Гб GDDR7",
        "brand": "Gigabyte",
        "color": "белый",
        "price_rub": 92104.0,
        "price_original_rub": 264000.0,
        "total_quantity": 1,
        "in_stock": True,
    },
]

CARD_EXPECTED = {
    "nm_id": 1280469586,
    "name": "Ноутбук 16 дюймов Ryzen 5-7535HS 16Гб 1ТБ WUXGA Win11 K16SFA",
    "brand": "TECNO",
    "color": "серый",
    "price_rub": 54676.0,
    "price_original_rub": 70824.0,
    "total_quantity": 26,
    "in_stock": True,
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_live_search_products_flatten_to_the_displayed_values() -> None:
    body = _load("search_v18_live.json")
    products = body["products"]
    assert len(products) == 3
    for raw, expected in zip(products, SEARCH_EXPECTED, strict=True):
        item = server._card_item_dict(raw)
        for key, value in expected.items():
            assert item[key] == value, f"{key}: {item[key]!r} != {value!r}"


def test_live_card_flattens_to_the_displayed_values() -> None:
    body = _load("card_v4_live.json")
    (raw,) = body["products"]
    item = server._card_item_dict(raw)
    for key, value in CARD_EXPECTED.items():
        assert item[key] == value, f"{key}: {item[key]!r} != {value!r}"


def test_live_prices_are_finite_positive_rubles() -> None:
    """The doctrine pinned by the audit waves, checked against live bytes: a
    WB price is a finite positive float or None, never 0/inf/nan."""
    for name in ("search_v18_live.json", "card_v4_live.json"):
        for raw in _load(name)["products"]:
            current, original = server._extract_price_rub(raw)
            for price in (current, original):
                if price is not None:
                    assert price > 0 and math.isfinite(price)


def test_live_items_build_the_wire_model() -> None:
    """The same dict flows into WbCardItem in wb_search/wb_card — build it."""
    for name in ("search_v18_live.json", "card_v4_live.json"):
        for raw in _load(name)["products"]:
            item = WbCardItem(**server._card_item_dict(raw))
            assert item.color == (raw["colors"][0]["name"] if raw["colors"] else "")


@pytest.mark.parametrize(
    "colors",
    [
        None,
        {},
        "gray",
        [],
        [None],
        [{"name": None}],
        [{"name": 7}],
        [{"name": " "}],
        [{"name": "gray"}, {"name": "white"}],
        [{"name": "gray"}, None],
    ],
)
def test_ambiguous_or_malformed_colors_do_not_select_a_variant(colors) -> None:
    item = WbCardItem(**server._card_item_dict({"colors": colors, "name": "Gray product"}))
    assert item.color == ""
