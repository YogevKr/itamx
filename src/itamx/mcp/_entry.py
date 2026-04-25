"""Console entry-point wrappers for optional MCP dependencies."""

from __future__ import annotations

import sys


def run() -> None:
    """Run the MCP server on STDIO."""
    try:
        from itamx.mcp.server import run as _run
    except ModuleNotFoundError:
        print(
            "MCP dependencies are not installed.\n"
            "Install them with:  pip install 'itamx[mcp]'",
            file=sys.stderr,
        )
        sys.exit(1)
    _run()


def run_http() -> None:
    """Run the MCP server over streamable HTTP."""
    try:
        from itamx.mcp.server import run_http as _run_http
    except ModuleNotFoundError:
        print(
            "MCP dependencies are not installed.\n"
            "Install them with:  pip install 'itamx[mcp]'",
            file=sys.stderr,
        )
        sys.exit(1)
    _run_http()
