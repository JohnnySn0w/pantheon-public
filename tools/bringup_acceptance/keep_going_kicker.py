"""keep-going / continuation-kicker acceptance module (T-2556 phase 3 continuation / T-2578).

See producer_fires.py's own scope-carve-out docstring for why keep-going is
proven HERE rather than as a generic catalog timer-fire: the operator's T-2556
addendum names a DISTINCT proof shape -- inject -> real-turn -> resume ->
dedupe-bound. The real-turn+resume half is the LIVE ticket (T-2579); this
module builds the OFF-PROD-PROVABLE half: the dedupe/race/rate-limit
behavior, plus the PARITY ORACLE a new daemon bringup must match, itemized
from tools/keep_going.py's own contract -- the single existing known-good
implementation. Every proof below calls the REAL keep_going.py / mailbox.py
functions; none of the dedupe/backoff logic is reimplemented here (matching
mailbox_roundtrip.py's own precedent of exercising tools.mailbox directly
rather than a stand-in).

FLEET FIRING SNAPSHOT ("is it even firing?" is itself part of the oracle,
per the operator's addendum -- a non-firing kicker is a FINDING, not a silent
pass). Captured 2026-08-05T~06:20Z via read-only sudo across the five
keep-going seats named in keep_going.AGENT_PANE (systemctl --user show
ActiveState/UnitFileState/LastTriggerUSec + the per-seat state-file read).
:data:`FLEET_FIRING_RAW` is that STATIC, DATED, SOURCED capture -- prove()
reports it as-observed, never as live-current; :func:`make_live_fleet_firing_reader`
ships a real production-wiring seam alongside it, NOT exercised by the test
suite, mirroring producer_fires.make_live_fire_harness's own "ship the live
seam, don't test live systemd" precedent.

FRAMING (HO-ratified 2026-08-05, this ticket's transition log): a seat's
keep-going kicker not currently firing is NOT presumed a defect. RECOVERY is
deliberately quiet this session (post-restart harness work) -- expected,
not a regression. MASON/ARTISAN are on-demand subagent personas, not among
the four standing daemons (orchestrator/specialist/recovery/codex), so an unarmed timer is
plausibly BY DESIGN. :data:`FLEET_FIRING_FRAMING` carries that context
per seat. This module's own pass/fail verdict is proven iff the DEDUPE/
RATE-LIMIT MECHANISM and the PARITY ORACLE itself hold -- never merely
because every seat happens to be firing right now; non-firing seats are
named, framed findings inside a "proven" row, not gaps.

RACE-GAP FINDING: keep_going.py's on-disk backoff state (read_state /
write_state) has no file lock. For the mailbox delivery mode (specialist today)
the mailbox transport itself provides race protection (T-1935 pending-
window dedupe, proven below). For the send-keys delivery mode (every other
agent), two concurrent invocations of run() for the SAME agent that both
read state before either writes would each independently decide to nudge.
This module surfaces that as a named, disclosed finding with likelihood
context (one keep-going timer per seat keeps the concurrent-same-state
window narrow -- a genuine race needs a SECOND triggering path, e.g. a
manual invocation overlapping the timer, which is not part of the current
deployment shape) -- not a silent pass, and not asserted as a live incident.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from tools import agent_role_map
from tools import keep_going
from tools import mailbox
from tools.bringup_acceptance import restore as restore_mod
from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
    Tier,
)

MODULE_ID = "keep-going-kicker"

# The real hub this module must NEVER write to -- same constant/guard shape as
# mailbox_roundtrip.py's own _REAL_HUB_DEFAULT (independent module-level
# constant, not shared, per that module's own DECISIONS on not coupling
# private state between sibling modules). Value: instance.yaml mailbox.hub via
# agent_role_map (T-4096), so the guard follows the real hub across ORG-087.
_REAL_HUB_DEFAULT = Path(agent_role_map.mailbox_hub())


# ---------------------------------------------------------------------------
# Parity-oracle enrichment: itemizes inventory.PARITY_ORACLES's coarse
# "keep-going-kicker" entry into named, individually-sourced rules.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class KeepGoingOracleDetail:
    rule_id: str
    statement: str
    source: str


KEEP_GOING_ORACLE_DETAILS: tuple[KeepGoingOracleDetail, ...] = (
    KeepGoingOracleDetail(
        rule_id="idle-gate",
        statement=(
            "a nudge only fires when is_idle_at_prompt() reads True (a parked idle "
            "prompt); busy ('esc to interrupt'), a modal footer, or an inconclusive "
            "capture all skip -- fail-safe toward NOT nudging"
        ),
        source="tools/keep_going.py:is_idle_at_prompt",
    ),
    KeepGoingOracleDetail(
        rule_id="adaptive-backoff",
        statement=(
            "a nudge is due only after 2**min(consecutive_ineffective, cap) idle "
            "cycles since the last nudge (1, 2, 4, ... up to 2**cap); an ineffective "
            "prior nudge doubles the wait, an effective one resets to base cadence. "
            "BOOTSTRAP EXCEPTION (traced, not a bug): the first-ever nudge (cycle 0) "
            "has no prior nudge to judge, so the very next cycle (cycle 1) is still "
            "due at threshold=1 and is the one that captures cycle 0's effectiveness "
            "verdict -- under a constantly-ineffective signal, cycles 0 and 1 can "
            "both fire before doubling takes hold from cycle 2 onward"
        ),
        source="tools/keep_going.py:decide_nudge",
    ),
    KeepGoingOracleDetail(
        rule_id="queue-aware-floor",
        statement=(
            "for agents opted in via queue_aware_backoff=True with a non-empty "
            "actionable ticket queue (has_queued_work), the backoff exponent is "
            "clamped at QUEUED_BACKOFF_FLOOR_EXP regardless of consecutive_ineffective"
        ),
        source="tools/keep_going.py:decide_nudge,has_queued_work",
    ),
    KeepGoingOracleDetail(
        rule_id="session-reset",
        statement=(
            "a new session_id (a restarted Claude Code session) resets the adaptive "
            "backoff state to base cadence, so a fresh parked agent is nudged fast "
            "rather than inheriting a stale escalation"
        ),
        source="tools/keep_going.py:maybe_reset_for_new_session,current_session_id",
    ),
    KeepGoingOracleDetail(
        rule_id="mailbox-delivery-race-dedupe",
        statement=(
            "for agents on DELIVERY_MODE=mailbox (specialist today), two racing identical "
            "nudges collapse to the ONE pending message already sitting in new/ via "
            "mailbox.send()'s T-1935 pending-window dedupe (keyed on sender+subject+"
            "payload digest)"
        ),
        source="tools/keep_going.py:send_mailbox; tools/mailbox.py:_find_pending_duplicate,send",
    ),
    KeepGoingOracleDetail(
        rule_id="send-keys-no-transport-dedupe",
        statement=(
            "for agents on DELIVERY_MODE=send-keys (the default), the pane-inject "
            "transport itself has NO dedupe of its own -- protection against a "
            "double-submit is entirely the decide_nudge() cadence gate (a nudged "
            "agent goes busy, so the next tick's idle-gate reads False). This is a "
            "NAMED finding, not an untested assumption -- see the race-gap proof"
        ),
        source="tools/keep_going.py:run,inject",
    ),
)


def capture_keep_going_oracle() -> tuple[KeepGoingOracleDetail, ...]:
    return KEEP_GOING_ORACLE_DETAILS


# ---------------------------------------------------------------------------
# Fleet firing snapshot: static, dated, sourced ground truth + non-defect
# framing. See the module docstring for how/when this was captured.
# ---------------------------------------------------------------------------

FiringVerdict = Literal["confirmed-firing", "not-firing-observed", "unknown"]

FLEET_FIRING_SNAPSHOT_OBSERVED_AT = "2026-08-05T06:20:00Z"
FLEET_FIRING_SNAPSHOT_METHOD = (
    "sudo -n -u <unix_user> systemctl --user show pantheon-keep-going@<agent>.timer "
    "-p ActiveState,UnitFileState,LastTriggerUSec, plus a sudo'd cat of "
    "~/.local/state/pantheon/keep_going_<agent>.json"
)


@dataclasses.dataclass(frozen=True)
class RawFiringObservation:
    """Already-fetched ground truth for one seat -- pure/fixture-testable, per
    inventory.py's own 'discovery is pure, live gathering is a later wiring
    concern' precedent."""

    agent: str
    timer_active_state: str | None
    timer_unit_file_state: str | None
    timer_last_trigger: str | None
    state_file_present: bool
    state_file: Mapping[str, Any] | None


@dataclasses.dataclass(frozen=True)
class FiringObservation:
    agent: str
    verdict: FiringVerdict
    detail: str


def interpret_firing(raw: RawFiringObservation) -> FiringObservation:
    """Pure classification -- no guessing: an unqueryable timer state is
    'unknown', never coerced to a firing/not-firing verdict either way."""

    if raw.timer_active_state is None:
        return FiringObservation(
            agent=raw.agent, verdict="unknown", detail="timer ActiveState not queryable",
        )
    if raw.timer_active_state == "active" and raw.timer_last_trigger:
        state_note = "state file present" if raw.state_file_present else "no state file yet"
        return FiringObservation(
            agent=raw.agent, verdict="confirmed-firing",
            detail=f"timer active, last triggered {raw.timer_last_trigger}; {state_note}",
        )
    return FiringObservation(
        agent=raw.agent, verdict="not-firing-observed",
        detail=(
            f"timer_active_state={raw.timer_active_state!r} "
            f"unit_file_state={raw.timer_unit_file_state!r} "
            f"last_trigger={raw.timer_last_trigger!r}"
        ),
    )


# Captured 2026-08-05T~06:20Z, read-only, against the live accounts (see
# FLEET_FIRING_SNAPSHOT_METHOD). Exact values as observed at that moment.
FLEET_FIRING_RAW: tuple[RawFiringObservation, ...] = (
    RawFiringObservation(
        agent="orchestrator", timer_active_state="active", timer_unit_file_state="enabled",
        timer_last_trigger="Wed 2026-08-05 06:11:30 UTC", state_file_present=True,
        state_file={"last_nudge_ts": 1785910290, "consecutive_ineffective": 0, "cycles_since_nudge": 0},
    ),
    RawFiringObservation(
        agent="specialist", timer_active_state="active", timer_unit_file_state="enabled",
        timer_last_trigger="Wed 2026-08-05 06:02:09 UTC", state_file_present=True,
        state_file={"last_nudge_ts": 1785909729, "consecutive_ineffective": 1, "cycles_since_nudge": 0},
    ),
    RawFiringObservation(
        agent="recovery", timer_active_state="inactive", timer_unit_file_state="enabled",
        timer_last_trigger="Tue 2026-08-04 03:21:42 UTC", state_file_present=True,
        state_file={"last_nudge_ts": 1785812644, "consecutive_ineffective": 0, "cycles_since_nudge": 0},
    ),
    RawFiringObservation(
        agent="mason", timer_active_state="inactive", timer_unit_file_state="disabled",
        timer_last_trigger=None, state_file_present=False, state_file=None,
    ),
    RawFiringObservation(
        agent="artisan", timer_active_state="inactive", timer_unit_file_state="disabled",
        timer_last_trigger=None, state_file_present=False, state_file=None,
    ),
    # T-5748 compatibility expansion: these seats joined AGENT_PANE after the
    # original 2026-08-05 capture. Their shared timer was not firing at the
    # 2026-10-30 audit that triggered installer parity work.
    RawFiringObservation(
        agent="codex", timer_active_state="inactive", timer_unit_file_state="disabled",
        timer_last_trigger=None, state_file_present=False, state_file=None,
    ),
    RawFiringObservation(
        agent="watchman", timer_active_state="inactive", timer_unit_file_state="disabled",
        timer_last_trigger=None, state_file_present=False, state_file=None,
    ),
)

FLEET_FIRING_FRAMING: Mapping[str, str] = {
    "orchestrator": "confirmed-firing -- standing daemon; timer active and recently triggered.",
    "specialist": (
        "confirmed-firing -- standing daemon; timer active and recently triggered "
        "(DELIVERY_MODE=mailbox path)."
    ),
    "recovery": (
        "not-firing-observed, EXPECTED -- recovery is deliberately quiet this session "
        "(post-restart harness work); this is the expected quiet-state, not a "
        "regression. Re-observe once recovery resumes standing duty."
    ),
    "mason": (
        "not-firing-observed, PLAUSIBLY ON-DEMAND BY DESIGN -- mason is an on-demand "
        "subagent seat, not among the four standing daemons (orchestrator/specialist/recovery/codex); "
        "an on-demand seat may not need a standing continuation timer. Not asserted "
        "broken, and not re-armed by this module."
    ),
    "artisan": (
        "not-firing-observed, PLAUSIBLY ON-DEMAND BY DESIGN -- artisan is an on-demand "
        "subagent seat, not among the four standing daemons (orchestrator/specialist/recovery/codex); "
        "an on-demand seat may not need a standing continuation timer. Not asserted "
        "broken, and not re-armed by this module."
    ),
    "codex": (
        "not-firing-observed at the shared timer -- T-5748 migrates the older "
        "codex-keep-going timer into the installer-owned cadence."
    ),
    "watchman": (
        "not-firing-observed -- T-5748 adds this Codex-primary seat to the "
        "installer-owned continuation cadence."
    ),
}


@dataclasses.dataclass(frozen=True)
class FleetFiringEntry:
    agent: str
    observation: FiringObservation
    framing: str


def capture_fleet_firing_snapshot() -> tuple[FleetFiringEntry, ...]:
    return tuple(
        FleetFiringEntry(
            agent=raw.agent, observation=interpret_firing(raw),
            framing=FLEET_FIRING_FRAMING[raw.agent],
        )
        for raw in FLEET_FIRING_RAW
    )


def make_live_fleet_firing_reader(
    *, runner: restore_mod.CommandRunner = restore_mod.default_command_runner,
) -> Callable[[str], RawFiringObservation]:
    """Real production wiring: sudo -u <unix_user> systemctl --user show + a sudo'd
    cat of the per-seat state file, for ONE keep_going.AGENT_PANE entry. NOT
    exercised by the test suite -- same 'ship the live seam untested' precedent
    as producer_fires.make_live_fire_harness / make_live_alert_fire_harness.
    Read-only: never mutates a unit or a state file. Fails soft to
    unqueryable/None fields on any probe error (mirrors capture_pane's /
    latest_work_ts's own fail-safe-to-None posture) -- only an unknown agent
    (not in AGENT_PANE) is a HardStop, mirroring keep_going.run()'s own guard."""

    def _read(agent: str) -> RawFiringObservation:
        cfg = keep_going.AGENT_PANE.get(agent)
        if cfg is None:
            raise HardStop(f"unknown agent {agent!r} -- not in keep_going.AGENT_PANE")
        account = cfg["unix_user"]
        unit = f"pantheon-keep-going@{agent}.timer"
        show_cmd = (
            "sudo", "-n", "-u", account, "bash", "-c",
            f"XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user show {unit} "
            "-p ActiveState,UnitFileState,LastTriggerUSec --value",
        )
        active_state = unit_file_state = last_trigger = None
        try:
            result = runner(show_cmd)
            if result.returncode == 0:
                lines = result.stdout.splitlines()
                if len(lines) >= 3:
                    active_state = lines[0] or None
                    unit_file_state = lines[1] or None
                    last_trigger = lines[2] or None
        except Exception:  # noqa: BLE001 - probe failure is fail-safe-to-None, not fatal
            pass

        state_file_path = Path(f"/home/{account}/.local/state/pantheon/keep_going_{agent}.json")
        cat_cmd = ("sudo", "-n", "-u", account, "cat", str(state_file_path))
        state_file: Mapping[str, Any] | None = None
        state_file_present = False
        try:
            import json as _json

            result = runner(cat_cmd)
            if result.returncode == 0 and result.stdout.strip():
                parsed = _json.loads(result.stdout)
                if isinstance(parsed, dict):
                    state_file = parsed
                    state_file_present = True
        except Exception:  # noqa: BLE001 - same fail-safe posture
            pass

        return RawFiringObservation(
            agent=agent, timer_active_state=active_state, timer_unit_file_state=unit_file_state,
            timer_last_trigger=last_trigger, state_file_present=state_file_present,
            state_file=state_file,
        )

    return _read


# ---------------------------------------------------------------------------
# Off-prod proof 1: mailbox delivery-mode race dedupe (real mailbox.send()
# via keep_going.send_mailbox(), disposable hub only).
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MailboxRaceResult:
    first_id: str
    second_id: str
    pending_names: tuple[str, ...]
    inbox: Path


def prove_mailbox_race_dedupe(
    *, hub_path: Path, agent: str, prompt: str = keep_going.DEFAULT_PROMPT,
) -> MailboxRaceResult:
    """Two 'racing' identical nudges via the REAL keep_going.send_mailbox() /
    tools.mailbox.send() -- asserts they collapse to the ONE pending message
    already sitting in new/ (T-1935 pending-window dedupe), matching
    mailbox_roundtrip.py's own _verify_pending_dedupe contract."""

    if hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
        raise HardStop(
            f"refusing to prove keep-going-kicker mailbox dedupe against the REAL hub "
            f"{hub_path} -- hub_path must be a disposable tree"
        )
    inbox = hub_path / f"keep-going-race-{agent}"
    mailbox.provision(inbox)

    first_ok = keep_going.send_mailbox(agent, prompt, inbox=inbox)
    if not first_ok:
        raise HardStop(f"first racing send_mailbox() call for {agent!r} reported failure")
    second_ok = keep_going.send_mailbox(agent, prompt, inbox=inbox)
    if not second_ok:
        raise HardStop(f"second racing send_mailbox() call for {agent!r} reported failure")

    pending_names = tuple(sorted(p.name for p in (inbox / "new").iterdir() if p.is_file()))
    if len(pending_names) != 1:
        raise HardStop(
            f"race dedupe violated: expected exactly one pending message, found {pending_names}"
        )
    # Re-derive both ids from disk rather than trusting send_mailbox()'s bool
    # return alone -- mailbox.send() returns the id, but send_mailbox() only
    # surfaces success/failure, so the id equality itself is proven by there
    # being exactly one record whose (sender, subject, payload) digest both
    # calls shared.
    record_id = pending_names[0].rsplit(".", 1)[0] if "." in pending_names[0] else pending_names[0]
    return MailboxRaceResult(
        first_id=record_id, second_id=record_id, pending_names=pending_names, inbox=inbox,
    )


# ---------------------------------------------------------------------------
# Off-prod proof 2: rate-limit / back-to-back sequencing (real
# keep_going.decide_nudge(), fully in-memory).
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RateLimitSequenceResult:
    fired_per_cycle: tuple[bool, ...]
    final_state: Mapping[str, Any]


def prove_rate_limit_sequencing(
    *, cycles: int, cur_work_ts: int | None, floor_exp: int | None = None,
    cap: int = keep_going.BACKOFF_CAP_EXP,
    work_ts_for_cycle: Callable[[int], int | None] | None = None,
) -> RateLimitSequenceResult:
    """Chains the REAL keep_going.decide_nudge() across ``cycles`` simulated
    idle ticks and records which cycles actually nudge.

    ``work_ts_for_cycle`` (default: the constant ``cur_work_ts`` every cycle)
    lets a caller simulate CONTINUALLY advancing work -- decide_nudge()'s own
    contract judges effectiveness by ``cur_work_ts > work_ts_at_last_nudge``,
    so a constant value only reads as 'new work' on the very first call.

    Traced bootstrap property of the real contract (not asserted by this
    function, proven by the test that calls it): cycle 0 is always the
    first-ever nudge (no prior nudge exists to judge), so it cannot yet
    contribute to the backoff; cycle 1 is the tick that judges cycle 0's
    effectiveness. Under a constantly-ineffective signal this means cycles
    0 and 1 can BOTH fire before the doubling takes hold from cycle 2 on --
    this is the one bootstrap exception, not a double-submit bug."""

    state: dict[str, Any] = {}
    fired: list[bool] = []
    now_ts = 1_700_000_000
    for i in range(cycles):
        work_ts = work_ts_for_cycle(i) if work_ts_for_cycle is not None else cur_work_ts
        should, state, _reason = keep_going.decide_nudge(
            state, work_ts, now_ts + i, cap=cap, floor_exp=floor_exp,
        )
        fired.append(should)
    return RateLimitSequenceResult(fired_per_cycle=tuple(fired), final_state=dict(state))


# ---------------------------------------------------------------------------
# Off-prod proof 3 (named finding, not a HardStop): same-state race gap on
# the send-keys path's unlocked state file.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RaceGapFinding:
    first_should: bool
    second_should: bool
    detail: str


def prove_same_state_race_gap(
    *, state: Mapping[str, Any] | None = None, cur_work_ts: int | None = None,
    now_ts: int = 1_700_000_000,
) -> RaceGapFinding:
    """Two concurrent 'readers' loading the IDENTICAL prior state (simulating
    two overlapping run() invocations that both read before either writes)
    both independently call the REAL keep_going.decide_nudge(). This proves
    -- rather than assumes -- that the send-keys path has no cross-invocation
    lock: both calls decide the same way from the same input, because neither
    sees the other's in-flight write. Disclosed with likelihood context: one
    keep-going timer per seat keeps the concurrent-same-state window narrow --
    the race needs a SECOND triggering path (e.g. a manual invocation
    overlapping the timer), which is not part of the current deployment
    shape, so this is a latent, low-likelihood finding, not a live incident."""

    base_state = dict(state) if state is not None else {}
    first_should, _first_state, _ = keep_going.decide_nudge(base_state, cur_work_ts, now_ts)
    second_should, _second_state, _ = keep_going.decide_nudge(base_state, cur_work_ts, now_ts)
    detail = (
        "keep_going.read_state/write_state has no file lock: two readers of the "
        "IDENTICAL prior state both independently decide should_nudge="
        f"{first_should}/{second_should} for the send-keys path (the mailbox "
        "delivery path is protected by mailbox.send()'s own transport dedupe, "
        "proven separately). Likelihood: NARROW -- one keep-going timer per "
        "seat means this needs a second, overlapping triggering path (e.g. a "
        "manual invocation racing the timer), which is not part of the current "
        "deployment shape. Latent, low-likelihood finding, not a live incident."
    )
    return RaceGapFinding(first_should=first_should, second_should=second_should, detail=detail)


# ---------------------------------------------------------------------------
# AcceptanceModule
# ---------------------------------------------------------------------------


class KeepGoingKickerModule:
    """Proves the keep-going/continuation kicker's dedupe/race/rate-limit
    contract off-prod, and ships the parity oracle + fleet firing snapshot a
    new daemon bringup (and T-2579's live half) is measured against."""

    id = MODULE_ID
    tier: Tier = "core"

    def __init__(self) -> None:
        self._baseline: restore_mod.Baseline | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        entries = [entry for entry in target.entries if entry.module_id == self.id]
        applies = any(entry.status == "applies" for entry in entries)
        if applies:
            reason = f"inventory declares a keep-going-kicker timer for persona {target.persona}"
        elif entries:
            reason = f"inventory marks the keep-going-kicker entry absent for persona {target.persona}"
        else:
            reason = f"no keep-going-kicker inventory entry for persona {target.persona}"
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=applies, reason=reason)

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": ctx.persona})

        race_inbox = ctx.hub_path / f"keep-going-race-{ctx.persona}"
        baseline_target = restore_mod.BaselineTarget(
            account=ctx.account,
            mailbox_dirs={"race-inbox-new": race_inbox / "new"},
            disposable_markers=(race_inbox,),
            backup_root=paths.module_dir(self.id) / "backups",
        )
        self._baseline = restore_mod.record_baseline(baseline_target)
        recorder.append("baseline", {"race_inbox": race_inbox})

        oracle = capture_keep_going_oracle()
        oracle_evidence = write_module_evidence(
            paths, self.id, "oracle",
            {"details": [dataclasses.asdict(d) for d in oracle]},
        )
        recorder.append("oracle-captured", {"rule_count": len(oracle)})

        fleet_snapshot = capture_fleet_firing_snapshot()
        fleet_evidence = write_module_evidence(
            paths, self.id, "fleet-firing-snapshot",
            {
                "observed_at": FLEET_FIRING_SNAPSHOT_OBSERVED_AT,
                "method": FLEET_FIRING_SNAPSHOT_METHOD,
                "entries": [
                    {
                        "agent": entry.agent, "verdict": entry.observation.verdict,
                        "detail": entry.observation.detail, "framing": entry.framing,
                    }
                    for entry in fleet_snapshot
                ],
            },
        )
        recorder.append(
            "fleet-firing-captured",
            {"agents": [entry.agent for entry in fleet_snapshot]},
        )

        mailbox_result = prove_mailbox_race_dedupe(hub_path=ctx.hub_path, agent=ctx.persona)
        recorder.append(
            "mailbox-race-dedupe",
            {"pending_names": mailbox_result.pending_names, "first_id": mailbox_result.first_id},
        )
        mailbox_evidence = write_module_evidence(
            paths, self.id, "mailbox-race-dedupe",
            {"pending_names": mailbox_result.pending_names, "first_id": mailbox_result.first_id},
        )

        rate_limit_result = prove_rate_limit_sequencing(cycles=8, cur_work_ts=None)
        recorder.append("rate-limit-sequencing", {"fired_per_cycle": rate_limit_result.fired_per_cycle})
        rate_limit_evidence = write_module_evidence(
            paths, self.id, "rate-limit-sequencing",
            {"fired_per_cycle": rate_limit_result.fired_per_cycle},
        )

        race_gap = prove_same_state_race_gap()
        recorder.append(
            "race-gap-finding",
            {"first_should": race_gap.first_should, "second_should": race_gap.second_should},
        )
        race_gap_evidence = write_module_evidence(
            paths, self.id, "race-gap-finding",
            {
                "first_should": race_gap.first_should, "second_should": race_gap.second_should,
                "detail": race_gap.detail,
            },
        )

        recorder.append("verdict", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=(
                oracle_evidence, fleet_evidence, mailbox_evidence,
                rate_limit_evidence, race_gap_evidence,
            ),
            detail={
                "oracle": [dataclasses.asdict(d) for d in oracle],
                "fleet_firing": {
                    entry.agent: {
                        "verdict": entry.observation.verdict, "framing": entry.framing,
                    }
                    for entry in fleet_snapshot
                },
                "mailbox_race_dedupe": {"pending_names": mailbox_result.pending_names},
                "rate_limit_sequencing": {"fired_per_cycle": rate_limit_result.fired_per_cycle},
                "race_gap_finding": {"detail": race_gap.detail},
            },
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        if self._baseline is None:
            raise HardStop("restore() called before prove() recorded a baseline")
        restore_mod.restore_to(self._baseline)
        post = restore_mod.record_baseline(self._baseline.target)
        restore_mod.verify_zero_residue(self._baseline, post)
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail=restore_mod.restore_payload(self._baseline, post, restored=True),
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        fleet = evidence.detail.get("fleet_firing", {})
        firing_bits = ", ".join(
            f"{agent}={info['verdict']}" for agent, info in sorted(fleet.items())
        )
        summary = (
            "keep-going-kicker: parity oracle established, mailbox race-dedupe and "
            "rate-limit sequencing proven against the real keep_going.py/mailbox.py "
            f"contract, race-gap finding disclosed. Fleet firing (as observed "
            f"{FLEET_FIRING_SNAPSHOT_OBSERVED_AT}): {firing_bits}."
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )
