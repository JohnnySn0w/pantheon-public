"""TEST-ONLY stdio driver for the approver MCP adapter (T-2916).

Runs ``tools.approval_mcp_adapter.server.serve`` against an explicit bus
address (a private test bus). This module is the ONLY way to point the
approver adapter anywhere but the system bus, and it is never packaged
(the ``approval`` build profile's packaged-module set excludes it; a test
pins that).

Usage: python -m tools.test_approval_mcp_adapter_driver <bus-address>
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_approval_mcp_adapter_driver <bus-address>", file=sys.stderr)
        return 2
    import anyio

    from tools.approval_mcp_adapter.server import serve

    anyio.run(serve, argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
