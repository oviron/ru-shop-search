"""End-to-end stdio MCP check: spawn a server, speak the real protocol.

``list_tools()`` on an in-process FastMCP object proves the decorators ran. It
does not prove the console script starts, that the JSON-RPC handshake
completes, or that stdout stays clean enough to parse — which are the three
ways a stdio MCP server actually fails for a user.

This script launches each server the way a client does (console script,
stdio), performs initialize / tools/list / tools/call, and reports what it
saw. Tools that would hit the network are not called; ``shops`` is, because it
is pure local state.

Run: uv run python scripts/e2e_stdio_check.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tomllib
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"marketplace-mcp": {"search", "card", "shops"}}
EXPECTED_INSTALLED = {
    "wildberries",
    "ozon",
    "yandex_market",
    "detsky_mir",
    "avito",
    "lamoda",
    "dns",
    "citilink",
    "mvideo",
    "aliexpress",
}

TIMEOUT_S = 60.0
EXPECTED_VERSION = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


def validate_shops(payload: object) -> str | None:
    """Reject incomplete introspection even when tools/call itself succeeded."""
    if not isinstance(payload, dict):
        return "shops did not return structured content"
    shops = payload.get("shops")
    if not isinstance(shops, list) or not all(isinstance(shop, dict) for shop in shops):
        return "shops returned an invalid list"
    installed = {shop.get("key") for shop in shops if shop.get("installed") is True}
    if installed != EXPECTED_INSTALLED:
        return f"expected installed shops {sorted(EXPECTED_INSTALLED)}, got {sorted(map(str, installed))}"
    if payload.get("server_version") != EXPECTED_VERSION:
        return f"shops version must be {EXPECTED_VERSION}, got {payload.get('server_version')}"
    return None


async def probe(script: str, expected: set[str]) -> tuple[str, bool, str]:
    path = shutil.which(script)
    if path is None:
        return script, False, "console script not on PATH"

    params = StdioServerParameters(command=path, args=[])
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=TIMEOUT_S)
            listed = await asyncio.wait_for(session.list_tools(), timeout=TIMEOUT_S)
            count = len(listed.tools)
            server_name = init.serverInfo.name
            version = init.serverInfo.version

            detail = f"{count} tools, server={server_name} v{version}"
            if version != EXPECTED_VERSION:
                return script, False, f"expected version {EXPECTED_VERSION}, got {version}"
            names = [tool.name for tool in listed.tools]
            if set(names) != expected or len(names) != len(expected):
                return script, False, f"expected tools {sorted(expected)}, got {sorted(names)}"

            # One real tools/call, on a tool that touches no network.
            called = await asyncio.wait_for(session.call_tool("shops", {}), timeout=TIMEOUT_S)
            if called.isError:
                return script, False, f"shops returned an error: {called.content}"
            failure = validate_shops(called.structuredContent)
            if failure is not None:
                return script, False, failure
            detail += f", tools/call ok: {len(EXPECTED_INSTALLED)} shops installed"

            return script, True, detail
    except TimeoutError:
        return script, False, f"timed out after {TIMEOUT_S}s"
    except Exception as exc:
        return script, False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    results = []
    for script, expected in EXPECTED_TOOLS.items():
        results.append(await probe(script, expected))

    width = max(len(name) for name, _, _ in results)
    failures = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"{mark}  {name:<{width}}  {detail}", file=sys.stderr)

    total = len(results)
    print(f"\n{total - failures}/{total} servers completed a real MCP session", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
