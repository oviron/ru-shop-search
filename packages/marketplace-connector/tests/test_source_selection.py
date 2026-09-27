"""MARKETPLACE_SOURCES сужает набор магазинов compare."""

from __future__ import annotations

import importlib

import pytest
from mcp_core.source_selection import ENV_VAR, SourceSelectionError, canonical, selected, wanted


def test_aliases_and_spacing_are_accepted(monkeypatch):
    monkeypatch.setenv(ENV_VAR, " WB , Yandex-Market ,ali")
    assert selected() == {"wildberries", "yandex_market", "aliexpress"}


def test_unknown_source_is_rejected_instead_of_silently_dropped(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "wildberries,typo_source")

    with pytest.raises(SourceSelectionError, match=r"unknown source.*typo_source"):
        selected()


def test_wanted_defaults_to_keeping_everything():
    assert wanted("ozon", None) is True
    assert wanted("ozon", {"wildberries"}) is False
    assert canonical("Detmir") == "detsky_mir"


def test_compare_queries_only_the_selected_sources(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "wildberries,ozon")
    import compare_connector.server as compare

    compare = importlib.reload(compare)
    try:
        assert set(compare.SOURCES) == {"wildberries", "ozon"}
    finally:
        monkeypatch.delenv(ENV_VAR, raising=False)
        importlib.reload(compare)
