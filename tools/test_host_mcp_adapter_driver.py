"""TEST-ONLY stdio driver for the host MCP adapter (T-2915).

Runs ``tools.host_mcp_adapter.server.serve`` against an explicit bus
address (a private test bus). This module is the ONLY way to point the
adapter anywhere but the system bus, and it is never packaged
(``build_runtime.PACKAGED_MODULES`` excludes it; a test pins that).

Usage: python -m tools.test_host_mcp_adapter_driver <bus-address>
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_host_mcp_adapter_driver <bus-address>", file=sys.stderr)
        return 2
    import anyio

    from tools.host_mcp_adapter.server import serve

    anyio.run(serve, argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
