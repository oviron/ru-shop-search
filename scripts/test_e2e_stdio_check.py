"""The release probe must reject a stale or incomplete server, not just count tools."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import e2e_stdio_check as probe


def shops_payload():
    return {
        "shops": [{"key": key, "installed": True} for key in sorted(probe.EXPECTED_INSTALLED)]
        + [{"key": "pepper", "installed": False}],
        "server_version": probe.EXPECTED_VERSION,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"shops": []},
        {"shops": [{"key": "wildberries", "installed": True}]},
        {"server_version": "0.0.0"},
    ],
)
def test_shops_reject_incomplete_or_stale_state(change):
    assert probe.validate_shops(shops_payload() | change) is not None


@pytest.mark.parametrize("payload", [None, {}, [], {"shops": [None]}])
def test_shops_reject_missing_or_malformed_payload(payload):
    assert probe.validate_shops(payload) is not None


def test_shops_accept_complete_current_state():
    assert probe.validate_shops(shops_payload()) is None


@pytest.mark.parametrize("version", ["0.0.0", probe.EXPECTED_VERSION])
async def test_probe_checks_running_version(monkeypatch, version):
    @asynccontextmanager
    async def stdio(_params):
        yield None, None

    class Session:
        def __init__(self, *_streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(name="wb", version=version))

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in ("search", "card", "shops")])

        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=False, structuredContent=shops_payload(), content=[])

    monkeypatch.setattr(probe.shutil, "which", lambda _script: "fake-mcp")
    monkeypatch.setattr(probe, "stdio_client", stdio)
    monkeypatch.setattr(probe, "ClientSession", Session)
    _, ok, detail = await probe.probe("marketplace-mcp", {"search", "card", "shops"})
    assert ok is (version == probe.EXPECTED_VERSION), detail
    if not ok:
        assert "expected version" in detail
