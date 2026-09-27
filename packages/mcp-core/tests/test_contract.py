"""Cross-connector contract tests: the invariants every parser must hold.

These pin the rules that keep a marketplace read trustworthy, asserted against
the shared helpers and every connector's parsing of a priceless listing. A
parser that breaks one of these is not a style issue — it is the specific bug
class (a dead listing ranked cheapest, a firewall page read as data) that the
runtime exists to prevent.

Offline by construction: no network, no Chrome, just the helpers and fixtures.
"""

from __future__ import annotations

from mcp_core import resilience as R

# ----------------------------------------------------------- None-not-zero ----


def test_coerce_price_refuses_to_guess_on_ambiguous_input():
    """The coercion contract: a range, an empty string, an absent value, a zero
    and a negative are all None — never a plausible number that would rank a
    dead listing cheapest."""
    assert R.coerce_price("1 999 ₽ 2 999 ₽") is None  # a range is ambiguous
    assert R.coerce_price("") is None
    assert R.coerce_price(None) is None
    assert R.coerce_price(0) is None
    assert R.coerce_price(-10) is None


def test_coerce_price_parses_grouped_display_strings():
    assert R.coerce_price("3 983 ₽") == 3983.0
    assert R.coerce_price(45500) == 45500.0


def test_first_present_distinguishes_absent_from_null():
    """Multi-alias binding must survive a renamed field without inventing one."""
    assert R.first_present({"price": 10}, "cost", "price") == 10
    assert R.first_present({"price": None}, "cost", "price") is None
    assert R.first_present({}, "cost", "price") is None


# ------------------------------------- every connector: priceless is None ----


def test_avito_pricelss_item_is_none_not_zero():
    from avito_connector.server import _parse_search_items

    items, _ = _parse_search_items({"items": [{"id": 1, "title": "x"}]})

    assert items[0]["price_rub"] is None
    assert items[0]["price_rub"] != 0


# ------------------------------------------------- firewall is never data ----


def test_avito_firewall_body_is_not_parsed_as_items():
    """The firewall JSON must not silently yield a plausible empty result."""
    from avito_connector.server import _parse_search_items

    items, total = _parse_search_items({"too-many-requests": {"message": "blocked"}})

    assert items == [], "a firewall body produced items"
    assert total is None


# --------------------------------------------------------- block not absence ----


def test_blocked_and_not_found_are_distinct_taxonomy_codes():
    """transport_down and not_found must stay separate: one is 'we were
    refused', the other is 'it does not exist'. Conflating them tells a caller
    the product is gone when the network is."""
    from mcp_core.errors import NotFoundError, TransportDownError

    assert NotFoundError("x").status_code != TransportDownError("x").status_code
