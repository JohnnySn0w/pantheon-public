"""Shared Telegram receiver handoff action ordering."""

from __future__ import annotations

from typing import Iterable


def receiver_handoff_actions(
    *,
    claude_receiver_topology: str,
    watchdog_units: Iterable[str] = (),
    direction: str,
    phase: str,
) -> tuple[str, ...]:
    has_watchdog = bool(tuple(watchdog_units))
    runtime_owned = claude_receiver_topology == "runtime-owned"
    if direction.endswith("-to-codex") and phase == "pre":
        actions: list[str] = []
        if has_watchdog:
            actions.extend(("snapshot-watchdog-units", "disable-watchdog-timer", "stop-watchdog-service"))
        if runtime_owned:
            actions.extend(
                (
                    "snapshot-dormant-claude-unit",
                    "disable-dormant-claude-receiver",
                    "stop-dormant-claude-receiver",
                    "probe-claude-present",
                )
            )
        else:
            actions.extend(("snapshot-claude-receiver", "disable-claude-receiver", "stop-claude-receiver", "probe-claude-absent"))
        return tuple(actions)
    if direction.endswith("-to-codex") and phase == "post":
        return ("probe-claude-absent", "start-codex-poller", "probe-codex-present", "health-codex-no-409")
    if direction.endswith("-to-claude") and phase == "pre":
        return ("stop-codex-poller", "probe-codex-absent")
    if direction.endswith("-to-claude") and phase == "post":
        if runtime_owned:
            actions = ["probe-claude-present"]
            if has_watchdog:
                actions.append("restore-watchdog-units")
            actions.append("complete-receiver-topology-snapshot")
            return tuple(actions)
        actions = ["restore-claude-receiver", "probe-claude-present"]
        if has_watchdog:
            actions.append("restore-watchdog-units")
        actions.append("complete-receiver-topology-snapshot")
        return tuple(actions)
    return ()
