"""Entry point for the ru-shop-search plugin MCP server.

Exposed as the ``marketplace-mcp`` console script. stdio is the default
transport; set ``MCP_TRANSPORT=http`` for HTTP — see ``mcp_core.runtime``.
Diagnostics go to stderr; nothing here may write to stdout.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Run the server, or the operator CLI (doctor, stops, resume, browser) when a subcommand is given.

    Subcommands go to stdout freely — only the server path owns the JSON-RPC stream.
    """
    if len(sys.argv) > 1:
        from marketplace_connector.cli import main as cli_main

        return cli_main(sys.argv[1:])

    from mcp_core.runtime import run_server

    from marketplace_connector.server import mcp

    return run_server(mcp, server_name="ru-shop-search")


if __name__ == "__main__":
    sys.exit(main())
