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
a live disposable tmux pane showing a stand-in (or real) codex's own
update-available modal, and proves both that readiness is reached AND that a
real dismissal actually fired through that path.

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

Three-gate anti-false-green design
------------------------------------------------------------------------------
1. ``_await_modal_present`` independently polls the REAL live pane (via
   ``runtime_controller._capture_visible_runtime_pane``, not this module's
   own belief) until the update modal is genuinely visible, and HardStops if
   it never appears. A stand-in (or account state) that never renders the
   modal at all can never reach the rest of this driver.
2. ``codex_modal.dismiss_codex_update_modal`` is spied (side_effect wrapping
   the REAL function -- never a stub/fake) while
   ``rc._wait_for_runtime_input_ready`` runs; ``evidence["ready"]`` requires
   BOTH ``RuntimeInputReadiness.ready`` AND the spy having actually fired at
   least once. Reaching readiness through some other route (or a future
   regression that bypasses the dismissal branch) cannot read as ready.
3. ``codex_rc_ready_status`` -- mirrors hcr's own ``codex_ready_status``
   verbatim: "proven" iff ``mode == "real-cli"`` AND ``ready`` is True. Every
   stand-in run, however clean, reads "deferred" unconditionally.

Real-cli wet-run: forcing a genuinely fresh modal
------------------------------------------------------------------------------
A real codex account that has already dismissed the current latest version
(``dismissed_version`` in ``~/.codex/version.json``) will render only the
compact non-interactive notice, never the interactive picker -- so
``_await_modal_present`` would legitimately HardStop, not because the wiring
is broken but because there is nothing to dismiss. For ``mode="real-cli"``
runs, this driver actively clears ``version.json`` before launch (inside the
``_VersionJsonSnapshot`` context, which already captured the true pre-run
bytes and restores them verbatim on exit regardless of this reset) so the
interactive modal genuinely renders every real-cli proof run -- mirroring the
manual reset the T-2572 live investigation performed by hand. This does NOT
apply to stand-in runs, which render their own scripted modal unconditionally.
"""

from __future__ import annotations

import argparse
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
from typing import Any, Callable, Mapping
from unittest import mock

from tools import codex_modal
from tools import persona_bringup
from tools import runtime_controller as rc
from tools.bringup_acceptance import harness_codex_ready as hcr
from tools.bringup_acceptance.types import HardStop

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
_DEFAULT_MODAL_WAIT_TIMEOUT_S = 10.0
_DEFAULT_MODAL_WAIT_POLL_INTERVAL_S = 0.2

# The real hub this module must NEVER write to (mirrors hcr's own guard).
_REAL_HUB_DEFAULT = hcr._REAL_HUB_DEFAULT

_ROLE = "bringup-acceptance-codex-rc-ready-role"

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
          sandbox: workspace-write
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
    return "proven" if evidence.get("ready") is True else "deferred"


def run_codex_rc_ready_bringup(
    *,
    work_root: Path,
    account: str,
    persona: str | None = None,
    codex_executable: Path | None = None,
    ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
    ready_poll_interval_s: float = _DEFAULT_READY_POLL_INTERVAL_S,
    modal_wait_timeout_s: float = _DEFAULT_MODAL_WAIT_TIMEOUT_S,
    modal_wait_poll_interval_s: float = _DEFAULT_MODAL_WAIT_POLL_INTERVAL_S,
) -> Mapping[str, Any]:
    """Stand up a disposable tmux/systemd scope, launch codex (real or
    stand-in) via the real ``runtime_controller`` control-plane chain
    (``_start_runtime`` -> ``_verify_started_runtime``), confirm the update
    modal is genuinely on the live pane, then drive readiness through the
    REAL ``runtime_controller._wait_for_runtime_input_ready`` (not a smoke's
    own loop) -- asserting both that it reaches ready AND that a real
    dismissal fired through that path.

    ``codex_executable=None`` runs the safe stand-in (mode="stand-in",
    :func:`codex_rc_ready_status` can never read it as proven); a real path
    (e.g. the real ``codex`` binary) is the HO-operator real-cli fire
    (mode="real-cli") -- see module docstring for why real-cli runs also
    force-clear ``version.json`` before launch.
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

    ready = False
    dismiss_call_count = 0
    modal_confirmed_present = False
    readiness_ready = False
    readiness_reason = ""
    pane_sha256 = ""
    try:
        with hcr._VersionJsonSnapshot(codex_home):
            version_json_path = codex_home / "version.json"
            if mode == _MODE_REAL_CLI and version_json_path.exists():
                # Force a genuinely fresh modal for this proof run -- see
                # module docstring "Real-cli wet-run" section. The snapshot
                # above already captured the true pre-run bytes and restores
                # them verbatim on exit regardless of this reset.
                version_json_path.unlink()

            previous_cwd = Path.cwd()
            try:
                os.chdir(work_root)
                runtime_start = rc._start_runtime(
                    binding, adapter, replace=False, context=context,
                )
            finally:
                os.chdir(previous_cwd)

            rc._verify_started_runtime(
                binding, runtime_start, state_root=state_root, context=context,
            )

            def _capture() -> str:
                return rc._capture_visible_runtime_pane(target, context=context)

            # Gate 1: the modal must really be on the live pane before this
            # driver ever touches rc's readiness path.
            _await_modal_present(
                capture=_capture, sleep=time.sleep, monotonic=time.monotonic,
                timeout_s=modal_wait_timeout_s, poll_interval_s=modal_wait_poll_interval_s,
            )
            modal_confirmed_present = True

            # Gate 2: spy (never a stub) on the REAL dismissal function while
            # driving the REAL rc readiness entrypoint directly -- see module
            # docstring "Driven entrypoint" section for why this is the
            # right function and "Three-gate anti-false-green design" for
            # why a spy, not a mock.
            real_dismiss = codex_modal.dismiss_codex_update_modal

            def _spy_dismiss(**kwargs: Any) -> None:
                nonlocal dismiss_call_count
                dismiss_call_count += 1
                real_dismiss(**kwargs)

            with mock.patch.object(codex_modal, "dismiss_codex_update_modal", side_effect=_spy_dismiss):
                readiness = rc._wait_for_runtime_input_ready(
                    target, harness="codex", timeout=ready_timeout_s,
                    poll_interval=ready_poll_interval_s, context=context,
                )

            readiness_ready = readiness.ready
            readiness_reason = readiness.reason
            pane_sha256 = readiness.pane_sha256
            # Gate 3 (evidence-level): "reached ready" alone is not enough --
            # the dismissal spy must have actually fired through rc's own
            # codex-gated branch.
            ready = readiness_ready and dismiss_call_count >= 1
    finally:
        residue = _teardown_disposable_tmux_context(context)
        shutil.rmtree(work_root, ignore_errors=True)
        if residue:
            raise HardStop(f"codex-rc-ready driver left residue: {'; '.join(residue)}")

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
            "leg green -- see codex_rc_ready_status(). "
            "IMPORTANT for --codex-executable runs: this driver force-clears "
            "the target account's ~/.codex/version.json before launch (and "
            "restores it verbatim afterward) so the interactive update modal "
            "genuinely renders every real-cli run -- without this, an "
            "account that already dismissed the current latest version would "
            "never show the modal and this leg would correctly HardStop as "
            "unprovable rather than silently pass."
        ),
    )
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--persona", default=None)
    parser.add_argument("--codex-executable", type=Path, default=None)
    parser.add_argument("--ready-timeout-s", type=float, default=_DEFAULT_READY_TIMEOUT_S)
    parser.add_argument("--modal-wait-timeout-s", type=float, default=_DEFAULT_MODAL_WAIT_TIMEOUT_S)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        evidence = run_codex_rc_ready_bringup(
            work_root=args.work_root, account=args.account, persona=args.persona,
            codex_executable=args.codex_executable, ready_timeout_s=args.ready_timeout_s,
            modal_wait_timeout_s=args.modal_wait_timeout_s,
        )
    except HardStop as exc:
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
