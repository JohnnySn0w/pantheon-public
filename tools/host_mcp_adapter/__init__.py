"""Privilege-free stdio host MCP adapter (ORG-052 Unit 4a, T-2915).

Deliberately imports nothing: ``contract`` is pure, ``bus_client`` and
``server`` need the pinned runtime (mcp 2.3.0, jeepney 0.9.0).
"""
