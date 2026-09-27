"""Offline tests for the unified marketplace server.

The unified server is a mount point, so its tests assert two things: every
installed connector's tools appear under their own names, and a connector that
fails to import is skipped rather than sinking the server.
"""

from __future__ import annotations

import asyncio

from marketplace_connector import server


def test_all_installed_sources_are_mounted():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}

    # One definitive tool per installed source. Selfchecks are no longer
    # mounted (they are operator-only diagnostics for `marketplace-mcp doctor`),
    # so a source's mount marker is the first model-facing tool it guarantees.
    expected_markers = {
        "wb_search",
        "ozon_card",
        "yandex_search",
        "detmir_card",
        "avito_search",
        "lamoda_card",
        "dns_search",
        "citilink_search",
        "compare_prices",
    }
    missing = expected_markers - names
    assert not missing, f"sources not mounted: {missing}"


def test_tool_names_keep_their_source_prefixes():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}

    assert "wb_search" in names
    assert "ozon_card" in names
    assert "avito_seller" in names
    assert "compare_prices" in names


def test_the_mounted_count_matches_the_imported_sources():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    # 31 mounted tools across 10 servers, plus marketplace_sources, which this
    # server owns rather than mounts. Operator-only *_selfcheck diagnostics are not MCP tools.
    own = {"marketplace_sources"}
    assert own <= names
    assert len(tools) == 32, f"expected 31 mounted tools + 1 own, got {len(tools)}"
    assert len(names - own) == 31


def test_marketplace_sources_reports_what_mounted():
    """A skipped source must be visible to the client, not just to stderr."""
    result = asyncio.run(server.marketplace_sources())

    assert result.mounted_count == 10
    assert result.skipped_count == 0
    assert result.skipped == {}
    assert "wildberries" in result.mounted
    assert "citilink" in result.mounted
    assert "aliexpress" in result.mounted
    assert result.server_version == server.SERVER_VERSION
    assert result.capabilities["dns"]["requires_cdp"] is True
    assert result.capabilities["wildberries"]["text_search"] is True


def test_marketplace_sources_capabilities_mark_skipped_sources(monkeypatch):
    monkeypatch.setattr(server, "_MOUNTED", ["wildberries"])
    monkeypatch.setattr(server, "_SKIPPED", {"ozon": "missing"})

    result = asyncio.run(server.marketplace_sources())

    assert result.capabilities["ozon"]["mounted"] is False
    assert result.capabilities["wildberries"]["mounted"] is True


def test_marketplace_sources_surfaces_a_skipped_source(monkeypatch):
    """Simulate the broken-install case the defensive import exists for."""
    monkeypatch.setattr(server, "_MOUNTED", ["wildberries"])
    monkeypatch.setattr(server, "_SKIPPED", {"taobao": "ModuleNotFoundError: No module named 'playwright'"})

    result = asyncio.run(server.marketplace_sources())

    assert result.mounted == ["wildberries"]
    assert result.skipped_count == 1
    assert "playwright" in result.skipped["taobao"]
