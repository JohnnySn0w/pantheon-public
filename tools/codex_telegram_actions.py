"""Shared Telegram receiver handoff action ordering."""

from __future__ import annotations

from typing import Iterable, Literal

#: T-4184: what the caller knows about the runtime being replaced. "live" (the
#: default, the consume transition path) means the prior claude session is up,
#: so the runtime-owned claude-to-codex pre phase can prove its receiver still
#: present. "unproven" (a controller codex launch over a dead or capped claude)
#: drops that presence proof: it would burn the full deadline and fail exactly
#: when claude is gone. The absence proof in the post phase is unaffected.
PriorRuntime = Literal["live", "unproven"]
PRIOR_RUNTIME_CHOICES: tuple[str, ...] = ("live", "unproven")

#: T-4184: error prefix of a failed codex 409 health step (the poller WAS
#: started and proven present, but logged sustained getUpdates conflicts).
#: Shared so the runtime controller can word its alert without importing the
#: bridge or naming the step.
HEALTH_409_ERROR_PREFIX = "sustained Telegram getUpdates 409 observed"


def receiver_handoff_actions(
    *,
    claude_receiver_topology: str,
    watchdog_units: Iterable[str] = (),
    direction: str,
    phase: str,
    prior_runtime: PriorRuntime = "live",
) -> tuple[str, ...]:
    if prior_runtime not in PRIOR_RUNTIME_CHOICES:
        raise ValueError(f"unsupported prior_runtime {prior_runtime!r}")
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
                )
            )
            if prior_runtime == "live":
                actions.append("probe-claude-present")
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
