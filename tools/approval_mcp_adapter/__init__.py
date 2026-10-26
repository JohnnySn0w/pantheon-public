"""Privilege-free stdio approver MCP adapter (ORG-052 Unit 4b, T-2916).

Four tools, one-to-one onto ``org.pantheon.HostApproval1.Reviewer``. A
separate binary from the host adapter (``tools.host_mcp_adapter``); it
shares that adapter's single hash-locked runtime
(``tools/host_mcp_adapter/dist/requirements.lock``) and is built with
``build_runtime.py --adapter approval``.

Registration is presentation, not authority: only a seat named by root-owned
approver policy is registered (``tools.harness_adapter.host_mcp_registration``),
and the approval service re-authorizes the bus-derived uid on every call.
Revocation is server-side and, as staged, takes effect when HostApproval1
restarts with the new policy (no hot reload): the T-2920 install leg must
restart or reload the service on every approver-policy install.

Deliberately imports nothing: ``contract`` is pure, ``bus_client`` and
``server`` need the pinned runtime (mcp 2.3.0, jeepney 0.9.0).
"""
