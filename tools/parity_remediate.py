#!/usr/bin/env python3
"""tools/parity_remediate.py — Recovery's bounded seat-parity self-repair (T-3798).

operator-directed 2026-09-18: "You're the repair instance, you should handle
that." Recovery runs the seat-parity audit (parity_audit_runner.py), which is
READ-ONLY BY INVARIANT — it CATCHES drift and escalates to orchestrator, but never
enables/injects. That left a latency gap: a seat can sit BROKEN on a purely
mechanical, safely-reversible axis (e.g. its inbox-consumer timer disabled ->
mail auto-delivery silently stops) from the moment the audit catches it until a
human notices and fixes it by hand. On 2026-09-18 that gap let archivist miss a
mailbox message for ~5h after the 19:45 audit had already flagged her BROKEN on
`inbox-consumer-timer-present`.

This tool closes that loop for the SAFE, MECHANICAL, REVERSIBLE subset of axes
ONLY. It re-runs the read-only audit in-process (race-free — no stale report),
and for each axis in SAFE_AXES whose verdict is a genuine FAIL, applies the
bounded repair AS THE SEAT'S OWN ACCOUNT, then re-verifies. Everything else is
left to the audit's existing orchestrator escalation.

BOUNDED BY DESIGN:
  * SAFE_AXES allowlist only — mechanical, idempotent, reversible repairs (enable
    a templated user timer). A HARD break on any OTHER axis (creds, permissions,
    harness, MCP) is NOT touched here; it stays the audit's escalation to orchestrator.
  * verdict == "FAIL" only. CARVE_OUT_VERIFIED / CARVE_OUT_BROKEN are NOT FAIL,
    so an INTENTIONAL exemption (a ratified carve-out, including a mid-bringup
    window) is never "repaired" — the authorised-no is respected for free.
  * idempotent: `enable --now` on an already-enabled timer is a no-op.
  * verify-after: a repair that does not take is reported as a FAILURE and
    escalated to orchestrator (a mechanical repair Recovery cannot apply IS a real problem).
  * the READ-ONLY audit invariant is preserved — this is a SEPARATE tool; the
    runner is imported only for its read-only run_fleet_audit().
  * pokes no live agent pane; touches only user-systemd timer enable-state.

Exit 0 regardless (like the audit): it is a scheduled remediation, not a gate.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import parity_audit_runner as par  # noqa: E402  (read-only run_fleet_audit + mailbox escalation helpers)
import parity_render as pr  # noqa: E402
from seat_exec import seat_argv  # noqa: E402
import notify_policy  # noqa: E402  (seat-maintenance registry, T-2984)

AGENT = "recovery"

# --- The safe-repair allowlist ------------------------------------------------
# Maps a parity axis_id to the templated user-timer whose enable-state IS the
# axis. Repair = `systemctl --user enable --now <unit>` on the seat's account.
# ONLY add an axis here if its repair is mechanical, idempotent and reversible,
# and enabling it cannot change agent BEHAVIOUR in a way a seat-owner might not
# want (that is why the behaviour-affecting monitor timers — keep-going,
# mailbox-wake, activity, context-fill — are deliberately NOT here yet; they are
# the same mechanical shape but warrant orchestrator/seat-owner intent first).
#
# GOVERNANCE (orchestrator, T-3798 close 2026-09-18): LOOP ORCHESTRATOR before widening this set
# for anything behaviour-affecting — monitor timers definitely, keep-going /
# mailbox-wake probably. The ACP-bridge is NEVER auto-remediated here without a
# supervised path: auto-restarting it risks the split-brain hazard archivist
# surfaced 2026-09-18. Widening is a deliberate, orchestrator-in-the-loop change, not a
# reflex.
SAFE_AXES: dict[str, str] = {
    "inbox-consumer-timer-present": "pantheon-inbox-consumer@{persona}.timer",
}


@dataclass(frozen=True)
class Repair:
    """A planned repair: PURE data, no I/O. Produced by plan_repairs()."""
    seat: str          # persona
    axis_id: str
    unit_template: str


@dataclass(frozen=True)
class RepairResult:
    seat: str
    axis_id: str
    unit: str
    applied: bool      # the enable command returned success
    verified: bool     # post-repair the timer reads enabled+active
    detail: str

    @property
    def ok(self) -> bool:
        return self.applied and self.verified


# --- PURE planning (the part the tests drive directly) ------------------------
def plan_repairs(reports, safe_axes: dict[str, str] | None = None) -> list[Repair]:
    """From a fleet of SeatReports, select the (seat, axis) cells that are a
    genuine FAIL on a SAFE axis. PURE: no clock, no I/O, no side effects.

    A carve-out shows as CARVE_OUT_VERIFIED / CARVE_OUT_BROKEN — NOT "FAIL" —
    so an intentional exemption is never selected. UNMEASURABLE is not FAIL
    either (we do not guess at an ambiguous probe)."""
    axes = SAFE_AXES if safe_axes is None else safe_axes
    out: list[Repair] = []
    for rep in reports:
        for oc in rep.outcomes:
            if oc.axis_id in axes and oc.verdict == "FAIL":
                out.append(Repair(seat=rep.seat, axis_id=oc.axis_id,
                                  unit_template=axes[oc.axis_id]))
    return out


def resolve_account(persona: str, seats) -> str | None:
    for s in seats:
        if s.persona == persona:
            return s.account
    return None


# --- Execution (mockable via `run`) -------------------------------------------
def _seat_systemctl(account: str, args: list[str], run=subprocess.run) -> subprocess.CompletedProcess:
    """Run `systemctl --user <args>` on <account>'s own user manager. Runs
    directly when <account> IS the current (auditor) account — sudo -u to self
    is refused on this host — otherwise crosses the user boundary via Recovery's
    NOPASSWD `sudo -u`, exporting XDG_RUNTIME_DIR so --user reaches the right
    manager."""
    inner = "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user " + " ".join(args)
    cmd = seat_argv(account, ["bash", "-c", inner], sudo=("sudo", "-u"))
    return run(cmd, capture_output=True, text=True)


def apply_repair(repair: Repair, account: str, run=subprocess.run) -> RepairResult:
    """Enable+start the seat's timer, then re-verify it reads enabled AND active.
    Idempotent (enable --now on an already-good timer is a no-op)."""
    unit = repair.unit_template.format(persona=repair.seat)
    enable = _seat_systemctl(account, ["enable", "--now", unit], run=run)
    applied = enable.returncode == 0
    is_enabled = _seat_systemctl(account, ["is-enabled", unit], run=run).stdout.strip()
    is_active = _seat_systemctl(account, ["is-active", unit], run=run).stdout.strip()
    verified = is_enabled == "enabled" and is_active == "active"
    detail = (f"enable rc={enable.returncode}; is-enabled={is_enabled or '?'}; "
              f"is-active={is_active or '?'}")
    if not applied:
        detail += f"; stderr={enable.stderr.strip()[:200]}"
    return RepairResult(seat=repair.seat, axis_id=repair.axis_id, unit=unit,
                        applied=applied, verified=verified, detail=detail)


def _seat_in_maintenance(persona: str) -> bool:
    """T-5222: a seat held down on purpose (seat-maintenance armed) must not be
    re-enabled behind the operator's back. Fail-open like every other consumer:
    notify_policy.in_maintenance returns False on any registry fault. ORG-106 L4c: a shim
    over the shared seat_state.maintenance_active (same registry, same fail-open)."""
    return _seat_state_mod().maintenance_active(persona)


def _seat_state_mod():
    """ORG-106 L4c: the seat_state module, importable from a package or a bare run."""
    try:
        from tools import seat_state
    except ImportError:  # bare run with cwd=tools/
        import seat_state  # type: ignore
    return seat_state


def _log(msg: str) -> None:
    print(f"[parity_remediate] {msg}", file=sys.stderr)


def _escalate_failed(results: list[RepairResult]) -> None:
    """A mechanical repair Recovery could not apply/verify is a real problem — hand
    it to orchestrator via the same lane the audit uses. Reuses the runner's mailbox
    helper if present; degrades to a loud log otherwise (never raises)."""
    lines = [f"- {r.seat}/{r.axis_id} ({r.unit}): {r.detail}" for r in results]
    body = ("Recovery auto-remediation could NOT apply/verify a mechanical parity "
            "repair (safe-axis FAIL). Needs a look:\n" + "\n".join(lines))
    try:  # reuse the audit's own orchestrator mailbox lane; never let it crash the pass
        par._default_mailbox_sink(
            subject="parity self-repair FAILED — mechanical fix did not take",
            body=body, severity="normal")
        return
    except Exception as e:
        _log(f"orchestrator escalation raised, falling back to log: {e}")
    _log("ESCALATE(orchestrator): " + body.replace("\n", " | "))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv

    reports, _axes = par.run_fleet_audit(execute=True)  # read-only
    seats = pr.load_seats()
    planned = plan_repairs(reports)

    if not planned:
        _log("no safe-axis FAILs — fleet clean, nothing to repair.")
        return 0

    results: list[RepairResult] = []
    for repair in planned:
        if _seat_in_maintenance(repair.seat):
            _log(f"SKIP {repair.seat}/{repair.axis_id}: seat-maintenance armed.")
            continue
        # ORG-106 L4c: a sleeping seat is not repaired back up (remediation_vetoed).
        # T-5918: nor is a seat a harness-manager run_state pause (U5) took down on purpose
        # (repair_vetoed); vetoes_repair() covers both and is False for an unknown reader.
        ss = _seat_state_mod()
        held_by = ss.safe_effective_for(repair.seat)
        if ss.vetoes_repair(held_by):
            why = ("remediation_vetoed" if held_by.remediation_vetoed
                   else "repair_vetoed: HALTED mid-pause, roll back first" if held_by.alertable is True
                   else "repair_vetoed: a run_state pause")
            _log(f"SKIP {repair.seat}/{repair.axis_id}: seat-state {held_by.state} ({why}) vetoes repair.")
            continue
        account = resolve_account(repair.seat, seats)
        if account is None:
            _log(f"SKIP {repair.seat}/{repair.axis_id}: no account binding.")
            continue
        if dry_run:
            _log(f"DRY-RUN would repair {repair.seat}/{repair.axis_id} "
                 f"({repair.unit_template.format(persona=repair.seat)}) on {account}.")
            continue
        res = apply_repair(repair, account)
        results.append(res)
        verb = "REPAIRED" if res.ok else "FAILED-TO-REPAIR"
        _log(f"{verb} {res.seat}/{res.axis_id} ({res.unit}) on {account}: {res.detail}")

    failed = [r for r in results if not r.ok]
    if failed:
        _escalate_failed(failed)
    fixed = [r for r in results if r.ok]
    if fixed:
        _log(f"self-repaired {len(fixed)} safe-axis break(s): "
             + ", ".join(f"{r.seat}/{r.axis_id}" for r in fixed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
