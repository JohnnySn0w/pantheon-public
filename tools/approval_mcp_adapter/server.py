"""Privilege-free stdio approver MCP adapter (ORG-052 Unit 4b, T-2916).

Exactly four tools, each a one-to-one translation onto one fixed
``org.pantheon.HostApproval1.Reviewer`` method (see ``contract.TOOLS``).
The adapter has no host mutation, no durable state, no credential, no
broker token, no listener (stdio only - it owns no bus name), no
caller-identity argument (the approval service resolves the caller from
the bus-attested uid against root-owned approver policy), and no generic
command, file, D-Bus, or transport passthrough.

Registration of this adapter in a harness is presentation, not authority
(ORG-052 §5/§7): a seat that is not a policy approver can launch this
binary and will see the four tools listed (initialize and ``tools/list``
never touch the bus), but every call is denied server-side.

Initialize and ``tools/list`` never touch the bus, so the adapter starts
and lists its stable tools even when the approval service is absent;
calls then return a bounded ``authority_unavailable`` tool error and
reconnect on later calls (gate 6).

Errors are returned as explicit ``isError`` tool results built by the
adapter (mcp >= 2.1.0 turns raised handler exceptions into a generic
error). Unknown tools are protocol errors (JSON-RPC ``-32602``).

``main()`` is the only production entrypoint. It accepts ``--self-check``
(and ``--no-bus`` with it) and nothing else: no bus address, no config
file, no environment override.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import sys
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from tools.approval_mcp_adapter import contract
from tools.approval_mcp_adapter.bus_client import ReviewerClient


RateLimiter = contract.RateLimiter

_SNAKE = {
    "readOnlyHint": "read_only_hint",
    "destructiveHint": "destructive_hint",
    "idempotentHint": "idempotent_hint",
    "openWorldHint": "open_world_hint",
}


def tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name=tool.name,
            title=tool.title,
            description=tool.description,
            input_schema=contract.input_schema(tool),
            output_schema=contract.output_schema(tool),
            annotations=types.ToolAnnotations(
                **{
                    _SNAKE[key]: value
                    for key, value in contract.annotations(tool).items()
                }
            ),
        )
        for tool in contract.TOOLS
    ]


def error_result(error: contract.AdapterError) -> types.CallToolResult:
    # Text-only on purpose: structuredContent must conform to the tool's
    # outputSchema, which describes SUCCESS.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=contract.canonical_text(error.payload()))],
        is_error=True,
    )


def success_result(structured: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=contract.canonical_text(structured))],
        structured_content=structured,
        is_error=False,
    )


class ApprovalAdapter:
    """Stateless translator: tool call -> one D-Bus call -> tool result."""

    def __init__(self, client: ReviewerClient, limiter: RateLimiter | None = None) -> None:
        self._client = client
        self._limiter = limiter or RateLimiter()

    async def call_tool(self, name: str, arguments: object) -> types.CallToolResult:
        tool = contract.TOOLS_BY_NAME.get(name)
        if tool is None:
            raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {contract.sanitize_detail(name)}")
        try:
            args = contract.validate_arguments(tool, arguments)
            if not self._limiter.allow():
                raise contract.AdapterError("rate_limited")
            body = await self._client.call(tool.method, args, timeout=tool.timeout)
            return success_result(contract.shape_result(tool, body))
        except contract.AdapterError as error:
            return error_result(error)

    def build_server(self) -> Server:
        async def on_list_tools(ctx, params) -> types.ListToolsResult:
            return types.ListToolsResult(tools=tool_definitions())

        async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
            return await self.call_tool(params.name, params.arguments)

        return Server(
            contract.ADAPTER_NAME,
            version=contract.ADAPTER_CONTRACT_REVISION,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )


async def serve(address: str = contract.SYSTEM_BUS_ADDRESS) -> None:
    """Serve one stdio session. ``address`` is overridden only by
    never-packaged test drivers; ``main()`` always uses the default."""

    client = ReviewerClient(address)
    server = ApprovalAdapter(client).build_server()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await client.aclose()


# --------------------------------------------------------------------
# --self-check
# --------------------------------------------------------------------


def _runtime_component() -> tuple[str, str, str]:
    versions = []
    for dist, expected in contract.EXPECTED_RUNTIME.items():
        try:
            actual = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return ("unhealthy", f"{dist} not importable", "-")
        if actual != expected:
            return ("unhealthy", f"{dist} {actual} != pinned {expected}", f"{dist}=={actual}")
        versions.append(f"{dist}=={actual}")
    if sys.version_info[:2] != contract.EXPECTED_PYTHON:
        return ("unhealthy", "python version does not match the lock", "python")
    return ("ready", "", ",".join(versions))


async def self_check_components(address: str, *, probe_bus: bool) -> dict[str, tuple[str, str, str]]:
    components: dict[str, tuple[str, str, str]] = {"runtime": _runtime_component()}
    if not probe_bus:
        components["system_bus"] = ("not_configured", "bus probe skipped (--no-bus)", "-")
        components["approval_authority"] = ("not_configured", "bus probe skipped (--no-bus)", "-")
        return components
    client = ReviewerClient(address)
    try:
        owned, activatable = await client.service_name_state(timeout=contract.READ_CALL_TIMEOUT)
    except contract.AdapterError:
        components["system_bus"] = ("unhealthy", "system bus not reachable", "-")
        components["approval_authority"] = ("not_configured", "system bus not reachable", "-")
        return components
    finally:
        await client.aclose()
    components["system_bus"] = ("ready", "", "-")
    if owned:
        components["approval_authority"] = ("ready", "", contract.BUS_NAME)
    elif activatable:
        components["approval_authority"] = ("degraded", "approval service not running; D-Bus activatable", contract.BUS_NAME)
    else:
        components["approval_authority"] = ("degraded", "approval service not running and not activatable", contract.BUS_NAME)
    return components


def run_self_check(address: str = contract.SYSTEM_BUS_ADDRESS, *, probe_bus: bool = True) -> int:
    components = asyncio.run(self_check_components(address, probe_bus=probe_bus))
    report = contract.self_check_report(components)
    sys.stdout.write(contract.canonical_text(report) + "\n")
    return 1 if report["Status"] == "unhealthy" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pantheon-approval-mcp-adapter",
        description="Privilege-free stdio MCP adapter for org.pantheon.HostApproval1.Reviewer.",
    )
    parser.add_argument("--self-check", action="store_true", help="print one pantheon-health/v1 line and exit")
    parser.add_argument("--no-bus", action="store_true", help="with --self-check: skip the bus probe")
    args = parser.parse_args(argv)
    if args.no_bus and not args.self_check:
        parser.error("--no-bus requires --self-check")
    if args.self_check:
        return run_self_check(probe_bus=not args.no_bus)
    anyio.run(serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
