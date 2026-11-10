"""Test helper (ORG-106 L4b, T-5527): build EffectiveSeatState values for monitor tests.

Not a test module (no test_ prefix). Monitors read the seat through
tools.seat_state.effective_for / safe_effective_for; their tests patch that and return
what eff() builds, so no test touches real state, /var/lib/pantheon or sudo."""
from __future__ import annotations

from tools import seat_state as ss


def eff(state: str = ss.STATE_ACTIVE, *, asleep: bool = False, oom: bool = False,
        maintenance: bool = False, fault: str | None = None,
        owner: str | None = "operator", reason: str | None = "r",
        expires: str | None = None, run_state_pause: bool = False,
        rollback_cmd: str = "python3 -m tools.germinate rollback --run r1") -> ss.EffectiveSeatState:
    """state: active | paused-intake | deep-sleep. asleep => deep-sleep phase asleep
    (expected_down). oom / maintenance add those sources on top of the record.
    run_state_pause => a paused-intake record from a harness-manager run_state pause
    (T-5918: repair_vetoed)."""
    sources = []
    if oom:
        sources.append(ss.SRC_OOM)
    if state == ss.STATE_DEEP_SLEEP:
        sources.append(ss.SRC_SEAT_STATE)
    if maintenance:
        sources.append(ss.SRC_MAINTENANCE)
    if state in (ss.STATE_PAUSED_INTAKE, ss.STATE_HALTED):
        sources.append(ss.SRC_SEAT_STATE)
    halted = state == ss.STATE_HALTED
    sleeping = state == ss.STATE_DEEP_SLEEP
    return ss.EffectiveSeatState(
        state=state,
        intake_blocked=bool(sources),
        launch_blocked=oom or sleeping,
        remediation_vetoed=maintenance or sleeping,
        expected_down=sleeping and asleep,
        sources=tuple(sources),
        owner=owner if sources else None,
        reason=reason if sources else None,
        expires=expires if sources else None,
        fault=fault,
        repair_vetoed=halted or (run_state_pause and state == ss.STATE_PAUSED_INTAKE),
        alertable=halted,
        rollback_cmd=rollback_cmd if halted else None,
    )
