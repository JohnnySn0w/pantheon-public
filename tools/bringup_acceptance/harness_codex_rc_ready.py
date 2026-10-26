"""tools/bringup_acceptance/harness_codex_rc_ready.py -- live-verify the T-2594
rc-path codex-modal wiring (T-2596, pre-cutover gate on T-2590).

T-2572's ``harness_codex_ready.py`` proves codex can come up interactive-ready
under a real, disposable-scope ``runtime_controller`` control-plane launch --
but it routes readiness through its OWN ``_wait_for_codex_ready`` poll loop,
never through ``runtime_controller``'s real bringup readiness function. T-2594
then added codex-gated update-modal dismissal directly into that real
function (``runtime_controller._wait_for_runtime_input_ready``) -- the actual
entrypoint used by ``ensure_runtime``/``_replace_runtime``/``planned_swap``/
``forced_swap``. T-2594's own tests mock ``_capture_visible_runtime_pane``/
``_run_tmux``, so the real closures wired into that path (the ``capture``/
``send_keys`` lambdas defined inline at
``runtime_controller._wait_for_runtime_input_ready``, and its call into
``tools.codex_modal.dismiss_codex_update_modal``) had never run against a
live tmux pane. This module closes that gap: it drives the REAL
``runtime_controller._wait_for_runtime_input_ready`` (not a smoke's own loop,
not ``_verify_runtime_input_ready`` -- see "Driven entrypoint" below) against
a live disposable tmux pane and records whether the real controller path
needed to dismiss an update modal or reached a normal ready composer without
one.

Deliberately a SIBLING module, not a leg bolted onto ``harness_codex_ready``
(hereafter "hcr"): hcr's own ``_wait_for_codex_ready`` in-file loop is a
*different, still-valuable* proof (the pure dismissal algorithm + driver/
plumbing), and entangling "route through rc" behind a flag on that driver
would risk regressing its existing green tests and blur two genuinely
different proof shapes. Mirrors the precedent hcr itself already sets with
``harness_fallback_swap.py``: distinct namespace-prefixed disposable-context
guards duplicated per module rather than shared (see hcr's own comments,
"tracked-acceptable duplication"). What IS reused verbatim from hcr (fixture
fidelity, not per-module namespacing): ``_write_stand_in_script``/
``_STAND_IN_SCRIPT_TEMPLATE``, ``_VersionJsonSnapshot``, ``_REAL_HUB_DEFAULT``.

Driven entrypoint -- ``_wait_for_runtime_input_ready``, not
``_verify_runtime_input_ready``
------------------------------------------------------------------------------
The closures under question (capture/send_keys lambdas, the codex-gated
branch, the call into ``codex_modal.dismiss_codex_update_modal``) all live
INSIDE ``runtime_controller._wait_for_runtime_input_ready`` itself.
``_verify_runtime_input_ready`` is a thin wrapper one level up that only
translates the returned ``RuntimeInputReadiness`` into an exception/evidence
write on failure -- no additional closures of concern, and that translation
is already unit-tested (``test_runtime_controller.py``). Driving
``_wait_for_runtime_input_ready`` directly is therefore fully faithful to the
flagged wiring gap, and returns a ``RuntimeInputReadiness`` this module can
assert on directly (mode/reason/ready) rather than only observing an
exception.

Typed-outcome anti-false-green design
------------------------------------------------------------------------------
1. ``codex_modal.dismiss_codex_update_modal`` is spied from before runtime
   startup through controller readiness. ``dismissal_proven`` requires a
   captured interactive update modal, at least one rc-gated dismissal call,
   and readiness afterward.
2. ``dismissal_not_needed`` is real-cli only. It requires bounded version
   evidence from the selected executable, no update modal in captured
   samples, a normal ready composer in captured samples, and
   ``RuntimeInputReadiness.ready`` with the exact success reason.
3. ``codex_rc_ready_status`` reports "proven" only for real-cli evidence
   whose typed outcome is ``dismissal_proven`` or ``dismissal_not_needed``.
   Stand-in runs stay deferred regardless of how cleanly they exercise the
   wiring.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence
from unittest import mock

from tools import codex_modal
from tools import persona_bringup
from tools import runtime_controller as rc
from tools.bringup_acceptance.codex_acceptance_config_guard import (
    AcceptanceConfigGuardError,
    ReversibleAcceptanceConfigGuard,
    converge_worker_guard_hook,
    parse_codex_cli_semver,
)
from tools.bringup_acceptance import harness_codex_ready as hcr
from tools.bringup_acceptance.types import HardStop
from tools.submit_verify import ready_for_submission

# --------------------------------------------------------------------------- #
# Mode tagging -- the false-green guard (mirrors hcr's own                    #
# _MODE_STAND_IN / _MODE_REAL_CLI / codex_ready_status verbatim).             #
# --------------------------------------------------------------------------- #

_MODE_STAND_IN = "stand-in"
_MODE_REAL_CLI = "real-cli"

# Distinct from both hcr's "pantheon-t2572-codex-ready-" and harness_fallback_
# swap's "pantheon-t2572-" prefixes -- unambiguous residue/guard diagnostics
# across all three modules, and immediately greppable as the T-2596 leg.
_SOCKET_PREFIX = "pantheon-t2596-codex-rc-ready-"
_SCOPE_PREFIX = "pantheon-t2596-codex-rc-ready-"

_DEFAULT_READY_TIMEOUT_S = 30.0
_DEFAULT_READY_POLL_INTERVAL_S = 0.5
# The real hub this module must NEVER write to (mirrors hcr's own guard).
_REAL_HUB_DEFAULT = hcr._REAL_HUB_DEFAULT

_ROLE = "bringup-acceptance-codex-rc-ready-role"
RcReadyOutcome = Literal["dismissal_proven", "dismissal_not_needed", "failed"]
_VERSION_EVIDENCE_TIMEOUT_S = 5.0
_VERSION_EVIDENCE_MAX_TEXT_BYTES = 4096

# Faithful to instance.yaml's real codex roles -- see hcr's own module
# docstring for the byte-for-byte citation this mirrors.
_MANIFEST_TEMPLATE = """\
harness_runtime:
  schema: pantheon-harness-runtime/v1
  controller:
    tmux:
      session: "__TMUX_SESSION__"
      window: harness
      pane: "0"
    quota:
      weekly_remaining_percent: 5
  harnesses:
    codex:
      default_profile: interactive-chatgpt
      profiles:
        interactive-chatgpt:
          executable: /bin/echo
          mode: interactive
          meter: subscription
          quota_source: app-server
  roles:
    __ROLE__:
      fallbacks: []
      codex:
        profile: interactive-chatgpt
        model: null
        native:
          sandbox: danger-full-access
          config_overrides:
            approval_policy: never

roles:
  __ROLE__:
    persona: __PERSONA__
    account: __ACCOUNT__
    harness: codex
"""


def _fixture_manifest_text(*, persona: str, account: str) -> str:
    """A disposable instance.yaml-shaped manifest, never the real repo file."""
    return (
        _MANIFEST_TEMPLATE.replace(
            "__TMUX_SESSION__", "pantheon-t2596-codex-rc-ready-{persona}"
        )
        .replace("__ROLE__", _ROLE)
        .replace("__PERSONA__", persona)
        .replace("__ACCOUNT__", account)
    )


# --------------------------------------------------------------------------- #
# Disposable tmux/systemd context guard + teardown (duplicated from hcr with  #
# this module's own namespace prefix -- see module docstring's precedent     #
# note; tracked-acceptable duplication, same convention hcr cites for its    #
# own relationship to harness_fallback_swap.py).                            #
# --------------------------------------------------------------------------- #


def _assert_disposable_tmux_context(context: "rc.TmuxRuntimeContext | None") -> None:
    """HardStop unless ``context`` is unambiguously this driver's own
    disposable namespace -- NEVER the default tmux socket or the production
    ``pantheon-runtime-tmux.scope``."""
    if context is None:
        raise HardStop("codex-rc-ready driver requires an explicit disposable TmuxRuntimeContext")
    if not context.socket:
        raise HardStop(
            "codex-rc-ready driver refuses an empty/default tmux socket -- must target a "
            "disposable named socket, never the default server"
        )
    if context.scope_unit == rc.TMUX_RUNTIME_SCOPE_UNIT:
        raise HardStop(
            f"codex-rc-ready driver refuses the production scope unit {rc.TMUX_RUNTIME_SCOPE_UNIT!r}"
        )
    if not context.socket.startswith(_SOCKET_PREFIX) or not context.scope_unit.startswith(
        _SCOPE_PREFIX
    ):
        raise HardStop(
            f"codex-rc-ready driver refuses a socket/scope outside its own "
            f"{_SOCKET_PREFIX!r}/{_SCOPE_PREFIX!r} namespace: "
            f"socket={context.socket!r} scope_unit={context.scope_unit!r}"
        )


def _new_disposable_tmux_context(run_id: str) -> "rc.TmuxRuntimeContext":
    context = rc.TmuxRuntimeContext(
        socket=f"{_SOCKET_PREFIX}{run_id}", scope_unit=f"{_SCOPE_PREFIX}{run_id}.scope",
    )
    _assert_disposable_tmux_context(context)
    return context


def _teardown_disposable_tmux_context(context: "rc.TmuxRuntimeContext") -> list[str]:
    """Best-effort, idempotent teardown of a disposable tmux socket/scope.

    Returns a residue list instead of raising directly so the caller can
    combine it with any exception already propagating out of the try-block.
    Byte-for-byte the same shape as hcr's own teardown -- see that module for
    the rationale of each step; duplicated here only because it is keyed to
    THIS module's own namespace-prefixed context.
    """
    _assert_disposable_tmux_context(context)
    residue: list[str] = []

    subprocess.run(
        ["tmux", "-L", context.socket, "kill-server"], check=False, capture_output=True, text=True,
    )
    subprocess.run(
        ["systemctl", "--user", "stop", context.scope_unit],
        check=False, capture_output=True, text=True,
    )
    subprocess.run(
        ["systemctl", "--user", "reset-failed", context.scope_unit],
        check=False, capture_output=True, text=True,
    )

    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if rc._pid_in_scope(pid, context.scope_unit, proc_root=proc_root):
            try:
                os.kill(pid, signal.SIGKILL)
                residue.append(f"had to SIGKILL surviving pid {pid} in {context.scope_unit}")
            except ProcessLookupError:
                pass

    show = subprocess.run(
        ["systemctl", "--user", "show", "--property=ActiveState", context.scope_unit],
        check=False, capture_output=True, text=True,
    )
    if "ActiveState=active" in show.stdout:
        residue.append(f"scope {context.scope_unit} still active after stop")

    socket_path = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / context.socket
    deadline = time.monotonic() + 2.0
    while socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if socket_path.exists():
        try:
            socket_path.unlink()
        except OSError:
            pass
    if socket_path.exists():
        residue.append(f"leftover tmux socket file: {socket_path}")

    return residue


# --------------------------------------------------------------------------- #
# Gate 1 -- the modal must genuinely be on the live pane before this driver   #
# ever touches rc. A pure deadline-loop, fully unit-testable with injected    #
# capture/sleep/monotonic (mirrors hcr's own _wait_for_codex_ready shape).    #
# --------------------------------------------------------------------------- #


def _await_modal_present(
    *,
    capture: Callable[[], str],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_s: float,
    poll_interval_s: float,
) -> str:
    """Poll ``capture()`` until it shows codex's interactive update modal.

    HardStops on timeout -- this is gate 1 of the anti-false-green design: a
    stand-in (or real account state) that never actually renders the modal
    can never reach the rc-path proof below.
    """
    deadline = monotonic() + timeout_s
    pane_text = ""
    while True:
        pane_text = capture()
        if codex_modal.is_codex_update_modal(pane_text):
            return pane_text
        if monotonic() >= deadline:
            raise HardStop(
                "codex-rc-ready driver: expected codex update-modal never appeared "
                f"on the pane within {timeout_s}s -- cannot prove rc's codex-gated "
                f"dismissal wiring: {pane_text!r}"
            )
        sleep(poll_interval_s)


@dataclasses.dataclass(frozen=True)
class CodexExecutableVersionEvidence:
    executable: Path
    argv: tuple[str, ...]
    returncode: int | None
    stdout_sha256: str | None
    stderr_sha256: str | None
    stdout_excerpt: str
    stderr_excerpt: str
    parsed_semver: str | None
    timed_out: bool
    ok: bool
    reason: str


def _bounded_text(value: str, max_text_bytes: int) -> str:
    raw = value.encode("utf-8", errors="replace")[:max_text_bytes]
    return raw.decode("utf-8", errors="replace")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def collect_codex_executable_version_evidence(
    codex_executable: Path,
    *,
    timeout_s: float = _VERSION_EVIDENCE_TIMEOUT_S,
    max_text_bytes: int = _VERSION_EVIDENCE_MAX_TEXT_BYTES,
) -> CodexExecutableVersionEvidence:
    argv = (str(codex_executable), "--version")
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _bounded_text(exc.stdout or "", max_text_bytes) if isinstance(exc.stdout, str) else ""
        stderr = _bounded_text(exc.stderr or "", max_text_bytes) if isinstance(exc.stderr, str) else ""
        return CodexExecutableVersionEvidence(
            executable=codex_executable,
            argv=argv,
            returncode=None,
            stdout_sha256=_text_sha256(stdout) if stdout else None,
            stderr_sha256=_text_sha256(stderr) if stderr else None,
            stdout_excerpt=stdout,
            stderr_excerpt=stderr,
            parsed_semver=None,
            timed_out=True,
            ok=False,
            reason="version evidence command timed out",
        )
    stdout = _bounded_text(result.stdout, max_text_bytes)
    stderr = _bounded_text(result.stderr, max_text_bytes)
    parsed = parse_codex_cli_semver(stdout + "\n" + stderr)
    ok = result.returncode == 0 and parsed is not None
    reason = "version evidence parsed" if ok else "version evidence missing codex-cli semver"
    if result.returncode != 0:
        reason = f"version evidence command exited {result.returncode}"
    return CodexExecutableVersionEvidence(
        executable=codex_executable,
        argv=argv,
        returncode=result.returncode,
        stdout_sha256=_text_sha256(stdout) if stdout else None,
        stderr_sha256=_text_sha256(stderr) if stderr else None,
        stdout_excerpt=stdout,
        stderr_excerpt=stderr,
        parsed_semver=parsed,
        timed_out=False,
        ok=ok,
        reason=reason,
    )


def _version_evidence_payload(
    evidence: CodexExecutableVersionEvidence | None,
) -> Mapping[str, Any] | None:
    if evidence is None:
        return None
    return dataclasses.asdict(evidence)


def _classify_rc_ready_outcome(
    *,
    mode: str,
    codex_executable: Path,
    version_evidence: CodexExecutableVersionEvidence | None,
    captured_samples: Sequence[str],
    readiness: rc.RuntimeInputReadiness,
    update_dismiss_call_count: int,
) -> tuple[RcReadyOutcome, str]:
    modal_present = any(codex_modal.is_codex_update_modal(sample) for sample in captured_samples)
    ready_composer = any(ready_for_submission(sample) for sample in captured_samples)
    if modal_present:
        if update_dismiss_call_count < 1:
            return "failed", "codex update modal observed without rc-gated dismissal"
        if readiness.ready and readiness.reason == "runtime input readiness confirmed":
            return "dismissal_proven", "codex update modal dismissed through rc and readiness followed"
        return "failed", f"codex update modal dismissed but readiness did not follow: {readiness.reason}"
    if not readiness.ready or readiness.reason != "runtime input readiness confirmed":
        return "failed", f"runtime input readiness not confirmed: {readiness.reason}"
    if mode != _MODE_REAL_CLI:
        return "failed", "no-modal readiness proof requires real-cli mode"
    if version_evidence is None or not version_evidence.ok:
        reason = version_evidence.reason if version_evidence is not None else "missing version evidence"
        return "failed", reason
    if version_evidence.executable != codex_executable:
        return "failed", "version evidence executable does not match selected executable"
    if not ready_composer:
        return "failed", "no-modal readiness proof missing captured ready composer"
    return "dismissal_not_needed", "real-cli reached ready composer with no update modal observed"


# --------------------------------------------------------------------------- #
# The driver.                                                                 #
# --------------------------------------------------------------------------- #


def codex_rc_ready_status(evidence: Mapping[str, Any]) -> str:
    """The ONLY function allowed to say the rc-path codex-ready leg is green.

    Returns ``"proven"`` iff ``evidence["mode"]`` is ``"real-cli"`` AND
    ``evidence["ready"]`` is True (which itself already requires BOTH rc
    reaching readiness AND the dismissal spy having actually fired -- see
    :func:`run_codex_rc_ready_bringup`). Every stand-in run reads
    ``"deferred"``, unconditionally -- mirrors hcr's ``codex_ready_status``.
    """
    if evidence.get("mode") != _MODE_REAL_CLI:
        return "deferred"
    return (
        "proven"
        if evidence.get("ready") is True
        and evidence.get("rc_ready_outcome") in {"dismissal_proven", "dismissal_not_needed"}
        else "deferred"
    )


def run_codex_rc_ready_bringup(
    *,
    work_root: Path,
    account: str,
    persona: str | None = None,
    codex_executable: Path | None = None,
    ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
    ready_poll_interval_s: float = _DEFAULT_READY_POLL_INTERVAL_S,
) -> Mapping[str, Any]:
    """Stand up a disposable tmux/systemd scope, launch codex (real or
    stand-in) via the real ``runtime_controller`` control-plane chain
    (``_start_runtime`` -> ``_verify_started_runtime``), then drive
    readiness through the REAL ``runtime_controller._wait_for_runtime_input_
    ready`` (not a smoke's own loop), classifying the observed path as
    ``dismissal_proven``, ``dismissal_not_needed``, or ``failed``.

    ``codex_executable=None`` runs the safe stand-in (mode="stand-in",
    :func:`codex_rc_ready_status` can never read it as proven); a real path
    (e.g. the real ``codex`` binary) is the HO-operator real-cli fire
    (mode="real-cli").
    """
    run_id = uuid.uuid4().hex[:12]
    persona = persona or f"bringup-acceptance-codex-rc-ready-{run_id}"
    mode = _MODE_REAL_CLI if codex_executable is not None else _MODE_STAND_IN

    work_root.mkdir(parents=True, exist_ok=True)
    manifest_path = work_root / "instance.yaml"
    state_root = work_root / "state"
    mailbox_hub = work_root / "mailbox"
    if mailbox_hub.resolve() == _REAL_HUB_DEFAULT.resolve():
        raise HardStop("codex-rc-ready driver refuses the REAL mailbox hub")
    manifest_path.write_text(
        _fixture_manifest_text(persona=persona, account=account), encoding="utf-8",
    )

    resolved_executable = codex_executable
    if resolved_executable is None:
        resolved_executable = work_root / "stand-in-codex.sh"
        hcr._write_stand_in_script(resolved_executable)

    binding = persona_bringup.load_binding(
        manifest_path, persona, harness_override="codex",
        executable_override=resolved_executable, mailbox_hub=mailbox_hub,
    )
    # Deliberately the REAL account home -- see hcr's own module docstring:
    # a synthetic CODEX_HOME would hit an auth/login wall and prove nothing
    # about the real daemon's bringup readiness.
    adapter = persona_bringup.build_adapter(binding, harness_home=binding.account_home)
    codex_home = binding.account_home / ".codex"

    context = _new_disposable_tmux_context(run_id)
    target = binding.controller.tmux
    launch_cwd = getattr(binding, "launch_cwd", work_root)
    if hasattr(adapter, "build_interactive_launch"):
        launch_spec = adapter.build_interactive_launch(target, cwd=launch_cwd)
        acceptance_project_cwd = Path(launch_spec.cwd)
    else:
        acceptance_project_cwd = Path(launch_cwd)

    ready = False
    dismiss_call_count = 0
    modal_confirmed_present = False
    readiness_ready = False
    readiness_reason = ""
    pane_sha256 = ""
    rc_ready_outcome: RcReadyOutcome = "failed"
    rc_ready_outcome_reason = "not run"
    captured_samples: list[str] = []
    version_evidence: CodexExecutableVersionEvidence | None = None
    pending_error: BaseException | None = None
    guard: ReversibleAcceptanceConfigGuard | None = None
    try:
        try:
            with hcr._VersionJsonSnapshot(codex_home):
                guard = ReversibleAcceptanceConfigGuard(
                    codex_home=codex_home,
                    project_cwd=acceptance_project_cwd,
                    owner="harness_codex_rc_ready",
                    # T-5317: the one prepare_codex_launch writer the replay
                    # can't model; converge it before the snapshot.
                    converge=lambda: converge_worker_guard_hook(codex_home, binding),
                )
                guard.__enter__()
                if mode == _MODE_REAL_CLI:
                    version_evidence = collect_codex_executable_version_evidence(resolved_executable)

                real_dismiss = codex_modal.dismiss_codex_update_modal

                def _spy_dismiss(**kwargs: Any) -> None:
                    nonlocal dismiss_call_count
                    dismiss_call_count += 1
                    real_dismiss(**kwargs)

                real_capture_visible = rc._capture_visible_runtime_pane

                def _recording_capture(*args: Any, **kwargs: Any) -> str:
                    pane_text = real_capture_visible(*args, **kwargs)
                    captured_samples.append(pane_text)
                    return pane_text

                with mock.patch.object(
                    codex_modal, "dismiss_codex_update_modal", side_effect=_spy_dismiss,
                ), mock.patch.object(rc, "_capture_visible_runtime_pane", side_effect=_recording_capture):
                    runtime_start = rc._start_runtime(
                        binding, adapter, replace=False, context=context,
                    )

                    rc._verify_started_runtime(
                        binding, runtime_start, state_root=state_root, context=context,
                    )

                    # Capture one live sample before the wait path. In real runs
                    # the wait path records more samples through the same patched
                    # controller capture; mocked tests still get source pane
                    # evidence instead of a no-sample false green.
                    try:
                        _recording_capture(target, context=context)
                    except Exception:
                        pass

                    readiness = rc._wait_for_runtime_input_ready(
                        target, harness="codex", timeout=ready_timeout_s,
                        poll_interval=ready_poll_interval_s, context=context,
                    )

                readiness_ready = readiness.ready
                readiness_reason = readiness.reason
                pane_sha256 = readiness.pane_sha256
                modal_confirmed_present = any(
                    codex_modal.is_codex_update_modal(sample) for sample in captured_samples
                )
                rc_ready_outcome, rc_ready_outcome_reason = _classify_rc_ready_outcome(
                    mode=mode,
                    codex_executable=resolved_executable,
                    version_evidence=version_evidence,
                    captured_samples=captured_samples,
                    readiness=readiness,
                    update_dismiss_call_count=dismiss_call_count,
                )
                ready = rc_ready_outcome in {"dismissal_proven", "dismissal_not_needed"}
        except BaseException as exc:
            pending_error = exc
    finally:
        if guard is not None and guard.expectation is not None:
            try:
                guard.restore()
            except AcceptanceConfigGuardError as exc:
                exc.original_exception = pending_error
                pending_error = exc
        residue = _teardown_disposable_tmux_context(context)
        if guard is not None:
            guard.attach_teardown_residue(residue)
        shutil.rmtree(work_root, ignore_errors=True)
        if residue:
            if isinstance(pending_error, AcceptanceConfigGuardError):
                pending_error.teardown_residue = tuple(residue)
            elif pending_error is None:
                pending_error = HardStop(
                    f"codex-rc-ready driver left residue: {'; '.join(residue)}"
                )
        if pending_error is not None:
            raise pending_error

    return {
        "mode": mode,
        "run_id": run_id,
        "persona": persona,
        "context": {"socket": context.socket, "scope_unit": context.scope_unit},
        "codex_executable": str(resolved_executable),
        "codex_home": str(codex_home),
        "modal_confirmed_present": modal_confirmed_present,
        "dismissed_via_rc": dismiss_call_count >= 1,
        "dismiss_call_count": dismiss_call_count,
        "rc_readiness_ready": readiness_ready,
        "rc_readiness_reason": readiness_reason,
        "rc_ready_outcome": rc_ready_outcome,
        "rc_ready_outcome_reason": rc_ready_outcome_reason,
        "version_evidence": _version_evidence_payload(version_evidence),
        "captured_sample_count": len(captured_samples),
        "captured_ready_composer": any(ready_for_submission(sample) for sample in captured_samples),
        "ready": ready,
        "pane_sha256": pane_sha256,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_codex_rc_ready.py",
        description=(
            "T-2596 rc-path codex-modal wiring live-verify -- HO-OPERATOR "
            "ENTRYPOINT. Pass --codex-executable pointing at the real codex "
            "binary to actually prove the leg (mode=real-cli); omit it to "
            "run the safe stand-in (mode=stand-in), which never flips the "
            "leg green -- see codex_rc_ready_status(). Real-cli runs accept "
            "either an rc-gated update-modal dismissal followed by readiness "
            "or bounded no-modal version evidence plus a captured ready "
            "composer."
        ),
    )
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--persona", default=None)
    parser.add_argument("--codex-executable", type=Path, default=None)
    parser.add_argument("--ready-timeout-s", type=float, default=_DEFAULT_READY_TIMEOUT_S)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        evidence = run_codex_rc_ready_bringup(
            work_root=args.work_root, account=args.account, persona=args.persona,
            codex_executable=args.codex_executable, ready_timeout_s=args.ready_timeout_s,
        )
    except (HardStop, AcceptanceConfigGuardError) as exc:
        sys.stderr.write(f"[harness_codex_rc_ready] HardStop: {exc}\n")
        return 1
    status = codex_rc_ready_status(evidence)
    print(json.dumps({**evidence, "status": status}, indent=2, default=str))
    if evidence["mode"] == _MODE_STAND_IN:
        sys.stderr.write(
            "[harness_codex_rc_ready] mode=stand-in: this proves the driver + "
            "rc-path wiring only -- the leg stays deferred/RED until a "
            "real-cli run\n"
        )
        return 0
    if status != "proven":
        sys.stderr.write("[harness_codex_rc_ready] real-cli run did not reach proven\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
