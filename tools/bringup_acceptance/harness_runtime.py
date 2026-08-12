"""tools/bringup_acceptance/harness_runtime.py -- fresh-runtime readiness +
diagnose acceptance module (T-2556 phase 7 / T-2569).

Four independently-gap-able legs:

1. ``diagnose`` -- re-reads real ground truth (``persona_bringup.
   diagnose_persona``) and enforces the binding contract: exit-0-ness is
   computed from AUTHORITATIVE observations only. ``<harness>.auth_material``
   is (today, verified in harness_adapter/diagnostics.py) always
   ``authoritative=False`` / ``status="unavailable"`` -- a missing auth
   material must never fail the daemon. This module HardStops if that
   contract ever regresses (auth_material observation absent, or flips
   authoritative, or reports a status this module doesn't recognize as
   non-blocking), rather than silently inheriting a changed meaning.
2. ``readiness_gate`` -- proves the REAL exported
   ``tools.submit_verify.ready_for_submission`` predicate against REAL
   ``tmux capture-pane`` output (positive + negative control), using a
   fully private ``tmux -S <socket file>`` server. No systemd scope, no
   default tmux server interaction of any kind -- total isolation.
3. ``ensure_runtime_chain`` -- proves the CONTRACT of the real public
   ``runtime_controller.ensure_runtime``: scope-start -> started-verify ->
   readiness-gate -> runtime.json publication, IN THAT ORDER, with the
   right schema/fields -- hash-chained into this run's own evidence tree.
   The systemd/tmux-touching internals (``_start_runtime`` etc.) are
   mocked, reusing test_runtime_controller.py's own established seam
   (read-only patch; runtime_controller.py itself is never edited).
   Dropping to these PRIVATE ``rc`` names is required here: ``ensure_
   runtime`` has no public callback/observer seam for the internal call
   order, so proving the SEQUENCE has no higher-level hook to attach to.
4. ``kick_lands_executes`` -- NOT attempted off-prod. See "Q5 finding"
   below. This module's own row stays ``proven`` for legs 1-3; the
   composite capability :data:`COMPOSITE_ID` additionally needs this leg
   (T-2570, ``harness-runtime-live``) before it reads GREEN -- mirrors
   ``mailbox_roundtrip.py``'s own "substrate-proven, composite needs the
   live leg" framing verbatim.

Q5 finding (why the live kick is deferred, not attempted here). UPDATED --
T-2589 removed the first of the two blockers below; only the second still
applies to THIS module's own leg (unbuilt, still deferred to T-2570):

* ~~``runtime_controller.TMUX_RUNTIME_SCOPE_UNIT`` is a hardcoded
  module-level singleton, not parameterized per-binding~~ -- T-2589 landed
  :class:`tools.runtime_controller.TmuxRuntimeContext`, threaded through
  ``_start_runtime``/``_verify_started_runtime``/``_verify_runtime_input_
  ready``/``_replace_runtime``/``submit_to_runtime``/``planned_swap``, so a
  disposable named tmux socket + disposable systemd scope is now first-class
  for those call paths. (``ensure_runtime()`` itself was NOT given a
  ``context`` parameter by T-2589 -- only the swap-path functions above --
  so the claim about ``ensure_runtime()`` specifically still holds; the
  scope/socket-singleton framing above does not.) See
  ``harness_fallback_swap.py``'s own ``run_live_kick_lands_executes`` (T-2572)
  for a live driver built against those parameterized call paths -- this
  module's leg still doesn't have one; that's still open work, not a
  structural impossibility.
* ``send_keys_submit.sh``'s "landed and got consumed" check is a
  per-harness *pending-text* predicate (claude's ``[Pasted text`` sentinel
  clearing) tightly coupled to the real CLI's TUI rendering. A disposable
  stand-in process would have to faithfully reproduce that rendering
  contract to prove anything real; a divergent emulation proves the
  emulation, not the chain -- the same class of gap T-2382 already needed
  a live window for. T-2589 does not touch this one.

``ready_for_submission`` itself (leg 2) has NO such coupling -- it is a
pure, harness-agnostic predicate over captured pane text (its own docstring:
"generalized to accept either harness's composer glyph") -- so it is
exercised for real here, against real (fully isolated) tmux output.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

from tools import persona_bringup
from tools import runtime_controller as rc
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
from tools.harness_adapter.types import (
    CommandSpec,
    DiagnosticReport,
    FailoverPolicy,
    HarnessLaunchConfig,
    RuntimeControllerConfig,
    TmuxTarget,
)
from tools.submit_verify import ready_for_submission


MODULE_ID = "harness-runtime"
COMPOSITE_ID = "harness-runtime-fully-functional"
DEFERRED_TO_TICKET = "T-2570"

_KICK_LANDS_EXECUTES_DEFERRAL_REASON = (
    "UPDATED (T-2589): runtime_controller.TmuxRuntimeContext now parameterizes the tmux "
    "socket/scope for _start_runtime/_verify_started_runtime/_verify_runtime_input_ready/"
    "_replace_runtime/submit_to_runtime/planned_swap, so THAT structural blocker is gone "
    "for those call paths (see harness_fallback_swap.py's run_live_kick_lands_executes, "
    "T-2572, for a live driver built against them). ensure_runtime() itself was NOT given "
    "a context parameter, so isolating it off-prod is still unbuilt here -- that remains "
    "open work for this module's own leg, not a structural impossibility. Separately, "
    "send_keys_submit.sh's landed+consumed check is a per-harness pending-text predicate "
    "tightly coupled to the real CLI's TUI rendering (claude's paste-sentinel) -- a "
    "disposable stand-in would prove its own emulation, not the real chain (same class of "
    "gap T-2382 needed a live window for); T-2589 does not touch this one. This leg is "
    "proven live in the coordinated T-2570 window."
)

# Frame edge (╭/─x10/╮) + composer glyph (❯) + no confirm-dialog text --
# satisfies tools.submit_verify.ready_for_submission's full conjunction.
_READY_FIXTURE_TEXT = (
    "╭──────────╮\n"
    "│ bringup-acceptance probe │\n"
    "╰──────────╯\n"
    "❯ \n"
)
_NOT_READY_FIXTURE_TEXT = "plain text with no frame or composer glyph\n"


# ---------------------------------------------------------------------------
# Leg 1: diagnose
# ---------------------------------------------------------------------------


def _resolve_mailbox_hub() -> Path:
    """Same default persona_bringup.py's own CLI parser uses (_common_parser)."""
    return persona_bringup.DEFAULT_MAILBOX_HUB


def _diagnose_verdict(report: DiagnosticReport) -> Mapping[str, Any]:
    """Pure filter over an already-collected report -- no I/O.

    Exit-0-ness is AUTHORITATIVE-checks-only. Enforces the auth_material
    non-authoritative contract as a HardStop-on-regression guard: if a
    future diagnostics change ever makes auth_material authoritative (or
    drops it, or reports a status this module doesn't recognize as
    non-blocking), that is a contract change this module must not silently
    inherit.
    """
    auth_observations = [
        observation for observation in report.observations
        if observation.check_id.endswith(".auth_material")
    ]
    if not auth_observations:
        raise HardStop(
            "diagnose: no auth_material observation found -- contract changed underfoot"
        )
    for observation in auth_observations:
        if observation.authoritative:
            raise HardStop(
                f"diagnose: {observation.check_id} is now authoritative=True -- this "
                "module's exit-0 contract assumes auth_material stays non-authoritative "
                "(unavailable auth material must never fail the daemon)"
            )
        if observation.status not in ("unavailable", "pass"):
            raise HardStop(
                f"diagnose: {observation.check_id} status={observation.status!r}, "
                "expected 'unavailable' (descriptor-only, non-authoritative) or 'pass'"
            )
    authoritative_failures = [
        observation.check_id
        for observation in report.observations
        if observation.authoritative and observation.status == "fail"
    ]
    return {
        "exit_code": 1 if authoritative_failures else 0,
        "authoritative_failures": authoritative_failures,
        "auth_material_check_ids": [observation.check_id for observation in auth_observations],
        "observations": [
            {
                "check_id": observation.check_id,
                "status": observation.status,
                "authoritative": observation.authoritative,
            }
            for observation in report.observations
        ],
    }


def _diagnose_leg(ctx: RunContext, *, mailbox_hub: Path | None = None) -> Mapping[str, Any]:
    """Re-observes real ground truth for ``ctx.persona`` (read-only -- no
    mutation anywhere in ``diagnose_persona``'s own checks) and applies
    :func:`_diagnose_verdict`. ``mailbox_hub`` overridable so tests never
    depend on the real, host-specific default hub (mirrors hook_emissions.
    py's own ``PANTHEON_BRINGUP_ACCOUNT_HOME`` override-seam convention)."""
    resolved_hub = mailbox_hub if mailbox_hub is not None else _resolve_mailbox_hub()
    binding = persona_bringup.load_binding(
        persona_bringup.DEFAULT_MANIFEST, ctx.persona, mailbox_hub=resolved_hub,
    )
    report = persona_bringup.diagnose_persona(
        binding,
        harness_home=binding.account_home,
        cwd=persona_bringup.REPO_ROOT,
        execution_context=f"{ctx.account}:{persona_bringup.REPO_ROOT}",
    )
    return _diagnose_verdict(report)


# ---------------------------------------------------------------------------
# Leg 2: readiness gate -- real, fully isolated tmux
# ---------------------------------------------------------------------------


def _tmux(socket_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("tmux", "-S", str(socket_path), *args),
        check=False, capture_output=True, text=True, timeout=10,
    )


# ---------------------------------------------------------------------------
# Leg 3: ensure_runtime chain contract (mock-injected)
# ---------------------------------------------------------------------------


def _disposable_binding(ctx: RunContext, tmux_target: TmuxTarget) -> SimpleNamespace:
    """Duck-typed PersonaBinding stand-in -- ensure_runtime only reads
    attributes, never isinstance-checks (test_runtime_controller.py's own
    ``_binding()`` helper uses the same SimpleNamespace shape)."""
    return SimpleNamespace(
        persona=ctx.persona,
        role=f"bringup-acceptance-{ctx.run_id}",
        account=ctx.account,
        account_home=Path(f"/home/{ctx.account}"),
        mailbox=ctx.hub_path / ctx.persona,
        selected_harness=ctx.harness,
        configured_harness=ctx.harness,
        launch=HarnessLaunchConfig(
            harness=ctx.harness, profile=ctx.profile, executable=Path("/bin/echo"),
            model=None, mode="interactive", meter="subscription",
            quota_source="statusline" if ctx.harness == "claude" else "app-server",
            native={}, sources=("bringup-acceptance",),
        ),
        controller=RuntimeControllerConfig(tmux=tmux_target, policy=FailoverPolicy(5.0, ())),
    )


class _DisposableAdapter:
    """Minimal PantheonHarnessAdapter stand-in -- only build_interactive_launch
    is on ensure_runtime's call path."""

    def __init__(self, binding: SimpleNamespace) -> None:
        self._binding = binding

    def build_interactive_launch(self, target: TmuxTarget) -> CommandSpec:
        return CommandSpec(
            argv=(str(self._binding.launch.executable),), stdin=None, cwd=Path("/tmp"),
        )


def _ensure_runtime_chain_leg(ctx: RunContext, *, state_root: Path) -> Mapping[str, Any]:
    """Calls the REAL public ``runtime_controller.ensure_runtime`` with the
    systemd/tmux-touching internals mocked (test_runtime_controller.py's own
    seam) and asserts the call ORDER + the published runtime.json schema.
    Read-only patch: runtime_controller.py itself is never edited."""
    tmux_target = TmuxTarget(f"bringup-acceptance-{ctx.run_id}", "harness", "0")
    binding = _disposable_binding(ctx, tmux_target)
    adapter = _DisposableAdapter(binding)
    call_order: list[str] = []

    def _start_runtime(*_args: Any, **_kwargs: Any) -> "rc.RuntimeStart":
        call_order.append("start_runtime")
        return rc.RuntimeStart(
            scope_unit=f"bringup-acceptance-{ctx.run_id}.scope", created_scope=True,
        )

    def _verify_started_runtime(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("verify_started_runtime")

    def _verify_runtime_input_ready(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("verify_runtime_input_ready")

    with mock.patch.object(rc, "_pane_exists", return_value=False), mock.patch.object(
        rc, "_start_runtime", side_effect=_start_runtime,
    ), mock.patch.object(
        rc, "_verify_started_runtime", side_effect=_verify_started_runtime,
    ), mock.patch.object(
        rc, "_verify_runtime_input_ready", side_effect=_verify_runtime_input_ready,
    ):
        handle = rc.ensure_runtime(binding, adapter, state_root=state_root)

    state_path = state_root / ctx.persona / "runtime-controller" / "runtime.json"
    published = state_path.read_text(encoding="utf-8")
    return {
        "call_order": call_order,
        "handle_profile_matches": handle.profile == binding.launch,
        "handle_tmux_matches": handle.tmux == tmux_target,
        "runtime_json_path": str(state_path),
        "runtime_json": published,
    }


# ---------------------------------------------------------------------------
# The AcceptanceModule
# ---------------------------------------------------------------------------


class HarnessRuntimeModule:
    id = MODULE_ID
    tier: Tier = "core"

    def __init__(self) -> None:
        self._socket_path: Path | None = None
        self._chain_state_dir: Path | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        matches = tuple(entry for entry in target.entries if entry.module_id == self.id)
        applies = any(entry.status == "applies" for entry in matches)
        reason = (
            f"{len(matches)} {self.id}-tagged entr{'y' if len(matches) == 1 else 'ies'} on target"
            if matches else f"no {self.id}-tagged entries on target"
        )
        return ModuleDeclaration(
            module_id=self.id, tier=self.tier, applies=applies, reason=reason,
            contributes_to=(COMPOSITE_ID,),
        )

    def _readiness_gate_leg(self, ctx: RunContext, *, evidence_root: Path) -> Mapping[str, Any]:
        socket_path = evidence_root / ctx.run_id / "modules" / self.id / f"tmux-{ctx.run_id}.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path = socket_path
        session = f"bringup-acceptance-{ctx.run_id}-ready"
        negative_session = f"bringup-acceptance-{ctx.run_id}-not-ready"

        try:
            start = _tmux(
                socket_path, "new-session", "-d", "-s", session, "-x", "80", "-y", "24",
                "sh", "-c", f"printf %s {shlex.quote(_READY_FIXTURE_TEXT)}; sleep 5",
            )
            if start.returncode != 0:
                raise HardStop(
                    f"readiness_gate: could not start disposable tmux session: {start.stderr}"
                )
            time.sleep(0.3)
            ready_captured = _tmux(socket_path, "capture-pane", "-p", "-t", session).stdout

            negative_start = _tmux(
                socket_path, "new-session", "-d", "-s", negative_session, "-x", "80", "-y", "24",
                "sh", "-c", f"printf %s {shlex.quote(_NOT_READY_FIXTURE_TEXT)}; sleep 5",
            )
            if negative_start.returncode != 0:
                raise HardStop(
                    f"readiness_gate: could not start negative-control session: "
                    f"{negative_start.stderr}"
                )
            time.sleep(0.3)
            not_ready_captured = _tmux(
                socket_path, "capture-pane", "-p", "-t", negative_session
            ).stdout
        finally:
            _tmux(socket_path, "kill-server")
            if socket_path.exists():
                socket_path.unlink()
            self._socket_path = None

        ready = ready_for_submission(ready_captured)
        not_ready = ready_for_submission(not_ready_captured)
        if not ready:
            raise HardStop(
                f"readiness_gate: positive control did not read ready: {ready_captured!r}"
            )
        if not_ready:
            raise HardStop(
                f"readiness_gate: negative control falsely read ready: {not_ready_captured!r}"
            )
        return {
            "positive_control": {
                "ready": ready,
                "pane_sha256": hashlib.sha256(ready_captured.encode("utf-8")).hexdigest(),
            },
            "negative_control": {
                "ready": not_ready,
                "pane_sha256": hashlib.sha256(not_ready_captured.encode("utf-8")).hexdigest(),
            },
        }

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": ctx.persona})

        gaps: list[str] = []
        legs: dict[str, Any] = {}
        evidence_paths: list[Path] = []

        diagnose_detail = _diagnose_leg(ctx, mailbox_hub=_resolve_mailbox_hub())
        recorder.append("diagnose", diagnose_detail)
        legs["diagnose"] = diagnose_detail
        evidence_paths.append(write_module_evidence(paths, self.id, "diagnose", diagnose_detail))
        if diagnose_detail["exit_code"] != 0:
            gaps.append(
                f"diagnose: authoritative check(s) failed: {diagnose_detail['authoritative_failures']}"
            )

        readiness_detail = self._readiness_gate_leg(ctx, evidence_root=ctx.evidence_root)
        recorder.append("readiness_gate", readiness_detail)
        legs["readiness_gate"] = readiness_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "readiness_gate", readiness_detail)
        )

        self._chain_state_dir = paths.module_dir(self.id) / "ensure_runtime_chain_state"
        chain_detail = _ensure_runtime_chain_leg(ctx, state_root=self._chain_state_dir)
        recorder.append("ensure_runtime_chain", chain_detail)
        legs["ensure_runtime_chain"] = chain_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "ensure_runtime_chain", chain_detail)
        )

        deferred_detail = {
            "deferred_to": DEFERRED_TO_TICKET,
            "leg": "kick_lands_executes",
            "reason": _KICK_LANDS_EXECUTES_DEFERRAL_REASON,
        }
        recorder.append("kick_lands_executes_deferred", deferred_detail)
        legs["kick_lands_executes"] = deferred_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "kick_lands_executes", deferred_detail)
        )

        status = "gap" if gaps else "proven"
        recorder.append("verdict", {"status": status, "gaps": gaps})
        return ModuleEvidence(
            module_id=self.id, status=status, trace_path=recorder.path,
            evidence_paths=tuple(evidence_paths),
            detail={"legs": legs, "gaps": gaps},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        residue: list[str] = []
        if self._socket_path is not None and self._socket_path.exists():
            residue.append(f"leftover tmux socket: {self._socket_path}")
        if self._chain_state_dir is not None and self._chain_state_dir.exists():
            shutil.rmtree(self._chain_state_dir)
            if self._chain_state_dir.exists():
                residue.append(f"leftover ensure_runtime_chain state dir: {self._chain_state_dir}")
        if residue:
            raise HardStop(f"{self.id}: restore residue: " + "; ".join(residue))
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail={
                "socket_path": str(self._socket_path) if self._socket_path else None,
                "chain_state_dir": str(self._chain_state_dir) if self._chain_state_dir else None,
            },
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        gaps = tuple(evidence.detail.get("gaps", ()))
        legs_summary = (
            "diagnose + real-tmux readiness-gate + ensure_runtime-chain-contract "
            + ("proven" if not gaps else "gapped: " + "; ".join(gaps))
        )
        summary = (
            f"{self.id}: {legs_summary}; kick-lands-executes leg deferred to "
            f"{DEFERRED_TO_TICKET} (coordinated live window) -- substrate-proven here, "
            f"composite '{COMPOSITE_ID}' additionally needs that leg before GREEN"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )
