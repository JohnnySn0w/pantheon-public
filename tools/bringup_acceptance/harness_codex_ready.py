"""tools/bringup_acceptance/harness_codex_ready.py -- codex-comes-up-ready
acceptance smoke (T-2572, path-B re-scope 2026-08-05).

The operator re-scoped T-2572 (path B): the full live claude<->codex disposable
SWAP (``harness_fallback_swap.py``'s own ``run_live_kick_lands_executes`` /
``HARNESS_FALLBACK_SWAP_LIVE_ID``) is NOT this ticket's deliverable. This
module is narrower and deliberately standalone: it proves that a REAL codex
process comes up interactive-ready under ``runtime_controller``'s
disposable-scope control-plane launch (the T-2589
:class:`~tools.runtime_controller.TmuxRuntimeContext` seam) -- the path-B
off-prod floor gating the T-2590 Recovery cutover.

NOT wired into ``tools.bringup_acceptance.composites``/``registry`` (orchestrator
review, T-2572): ``composites.py`` reserves ``HARNESS_FALLBACK_SWAP_LIVE_ID``
for the FULL two-sided claude<->codex swap, exactly the shape the operator's
re-scope cut. Wiring this codex-only smoke into that composite would
silently re-broaden back toward the descoped shape. This module stays a
standalone driver + guards + tests + ``main()`` CLI, mirroring
``harness_fallback_swap.py``'s own live-kick shape
(``run_live_kick_lands_executes`` / ``kick_lands_executes_status`` /
``_assert_disposable_tmux_context`` / ``_teardown_disposable_tmux_context``)
rather than its full declare/prove/restore/report ``AcceptanceModule``.

The update-available modal (open design question, T-2572 brief)
------------------------------------------------------------------
Live-investigated 2026-08-05 against codex-cli 0.136.0 (latest 0.146.1):

* No suppression lever exists. Exhausted ``codex --help`` (full),
  ``codex update --help``, ``codex features list``, ~10 ``-c key=value``
  guesses (``updates.check_on_startup``, ``check_updates``,
  ``disable_update_check``, ``hide_update_notice``, etc.) under
  ``--strict-config``, and a binary-strings sweep for ``CODEX_*``/``*update*``
  config keys. Nothing flips ``codex doctor``'s "startup update check: true"
  field.
* Chose precise deterministic dismissal (option B), live-verified end to
  end: reproduced the exact modal on a disposable tmux socket against real
  codex, confirmed the raw modal reads ``ready_for_submission() is False``
  (no frame edge), navigated to "Skip until next version" with the cursor
  position RE-VERIFIED via a fresh capture before every keystroke (never a
  blind fixed sequence -- a wrong keystroke on "Update now" would run
  ``npm install``), landed on a real, stable, ``ready_for_submission()``
  ``True`` composer.
* Dismissal is durable: codex persists ``dismissed_version`` into
  ``~/.codex/version.json``, so a subsequent launch for the same latest
  version shows only a compact non-blocking notice instead of the
  interactive picker. :func:`_is_codex_update_modal` only matches the
  interactive picker's own text, so both states (interactive modal, compact
  notice, no update available) flow through :func:`_wait_for_codex_ready`
  correctly with no special-casing. :class:`_VersionJsonSnapshot` restores
  ``version.json`` to its pre-run bytes after every run (residue discipline
  extends to this file, not just tmux/scope) -- which also means every
  real-cli run re-exercises the interactive modal, rather than leaking a
  "shows it once ever" side effect into the real account's codex home.
* ``--no-alt-screen``: tested, not needed -- ``tmux capture-pane -p``
  captures the visible pane correctly with or without it, and adding it
  would require editing ``harness_adapter/codex.py`` (``_append_native_args``
  only reads the ``sandbox``/``config_overrides`` native keys), which is out
  of this leg's file scope.

Out-of-scope discovery (orchestrator review, T-2572 -- do NOT fix here; filed as
T-2594): this module routes around the private
``runtime_controller._verify_runtime_input_ready`` precisely because it has
no modal awareness -- which means the REAL codex-failover bringup path
(``runtime_controller.ensure_runtime``/``planned_swap``) would itself get
stuck on the same update/first-run modals in production. This smoke proves
codex CAN come up ready WHEN the modals are handled; it does NOT make the
real failover path modal-safe. Two fix paths for that follow-up: (i) thread
modal-handling into the real bringup path, or (ii) pre-configure the codex
home (persist ``dismissed_version`` + pre-trust every launch cwd) so no
modal ever appears there.

Manifest fidelity: the disposable fixture manifest's codex native config
(``sandbox: workspace-write`` + ``config_overrides: {approval_policy:
never}``) matches the REAL production ``instance.yaml`` byte-for-byte
(``recovery`` role, instance.yaml:201-204; ``peer-daemon-codex``
role, instance.yaml:209-212).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from tools import codex_modal
from tools import persona_bringup
from tools import runtime_controller as rc
from tools.bringup_acceptance.types import HardStop
from tools.submit_verify import ready_for_submission

# --------------------------------------------------------------------------- #
# Mode tagging -- the false-green guard (mirrors harness_fallback_swap.py's   #
# _LIVE_KICK_MODE_STAND_IN / _LIVE_KICK_MODE_REAL_CLI /                       #
# kick_lands_executes_status verbatim).                                      #
# --------------------------------------------------------------------------- #

_MODE_STAND_IN = "stand-in"
_MODE_REAL_CLI = "real-cli"

# Distinct from harness_fallback_swap.py's own "pantheon-t2572-" prefix so
# residue/guard diagnostics stay unambiguous between the two T-2572 modules.
_SOCKET_PREFIX = "pantheon-t2572-codex-ready-"
_SCOPE_PREFIX = "pantheon-t2572-codex-ready-"

_DEFAULT_READY_TIMEOUT_S = 30.0
_DEFAULT_READY_POLL_INTERVAL_S = 0.5
_MODAL_NAV_POLL_S = 0.3
_MAX_MODAL_NAV_ATTEMPTS = 4

# The real hub this module must NEVER write to (mirrors harness_fallback_
# swap.py's own module-level guard constant).
_REAL_HUB_DEFAULT = Path("/home/the-ancestor/mailbox")

_ROLE = "bringup-acceptance-codex-ready-role"

# Faithful to instance.yaml's real codex roles (see module docstring for
# citations) -- sandbox=workspace-write + config_overrides.approval_policy=
# never. Session templated on {persona} ONLY (mirrors harness_fallback_
# swap.py's _fixture_manifest_text / _validate_stable_tmux_template).
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
            "__TMUX_SESSION__", "pantheon-t2572-codex-ready-{persona}"
        )
        .replace("__ROLE__", _ROLE)
        .replace("__PERSONA__", persona)
        .replace("__ACCOUNT__", account)
    )


# --------------------------------------------------------------------------- #
# Modal detection / precise dismissal (pure functions over pane text).       #
#                                                                              #
# T-2594: the actual logic now lives in ``tools.codex_modal`` -- the single  #
# audited never-blind-keystroke path shared with the real bringup path       #
# (``runtime_controller._wait_for_runtime_input_ready``). The names below    #
# stay module-level attributes of THIS module (not just re-exports used      #
# internally) because this module's own tests patch them directly via       #
# ``mock.patch.object(hcr, "_dismiss_codex_update_modal", ...)`` --          #
# :func:`_wait_for_codex_ready` resolves them from its own module globals    #
# at call time, so the patch keeps working regardless of where the          #
# underlying function is actually defined.                                   #
# --------------------------------------------------------------------------- #

_MAX_MODAL_NAV_ATTEMPTS = codex_modal.MAX_MODAL_NAV_ATTEMPTS
_MODAL_NAV_POLL_S = codex_modal.MODAL_NAV_POLL_S

_is_codex_update_modal = codex_modal.is_codex_update_modal
_cursor_on_skip_until_next_version = codex_modal.cursor_on_skip_until_next_version
# T-2599: same module-level-attribute convention as the update-modal names
# above -- this module's own tests patch these directly.
_is_codex_model_migration_modal = codex_modal.is_codex_model_migration_modal


def _dismiss_codex_update_modal(
    *,
    capture: Callable[[], str],
    send_keys: Callable[[str], None],
    sleep: Callable[[float], None],
) -> None:
    """Thin HardStop-translating shim over :func:`codex_modal.dismiss_codex_
    update_modal` -- this module's own tests/callers expect a ``HardStop``
    (``tools.bringup_acceptance.types``) on exhaustion, not the shared
    module's dependency-free ``ModalNavigationFailed`` (see codex_modal.py's
    own docstring for why it doesn't know about ``HardStop``)."""
    try:
        codex_modal.dismiss_codex_update_modal(
            capture=capture,
            send_keys=send_keys,
            sleep=sleep,
            max_attempts=_MAX_MODAL_NAV_ATTEMPTS,
            poll_interval_s=_MODAL_NAV_POLL_S,
        )
    except codex_modal.ModalNavigationFailed as exc:
        raise HardStop(str(exc)) from exc


def _dismiss_codex_model_migration_modal(
    *,
    capture: Callable[[], str],
    send_keys: Callable[[str], None],
    sleep: Callable[[float], None],
) -> None:
    """Thin HardStop-translating shim over :func:`codex_modal.dismiss_codex_
    model_migration_modal` -- same rationale as :func:`_dismiss_codex_update_
    modal` above (T-2599)."""
    try:
        codex_modal.dismiss_codex_model_migration_modal(
            capture=capture,
            send_keys=send_keys,
            sleep=sleep,
            max_attempts=_MAX_MODAL_NAV_ATTEMPTS,
            poll_interval_s=_MODAL_NAV_POLL_S,
        )
    except codex_modal.ModalNavigationFailed as exc:
        raise HardStop(str(exc)) from exc


def _wait_for_codex_ready(
    *,
    timeout_s: float,
    poll_interval_s: float,
    capture: Callable[[], str],
    send_keys: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Poll ``capture()`` until ``ready_for_submission`` is True, dismissing
    codex's update-available modal and model-migration modal (each at most
    once) along the way (T-2599: both live checks in one loop, same as
    ``runtime_controller._wait_for_runtime_input_ready`` -- codex's real
    startup order can show both, in that order).

    Returns the final ready pane text. HardStops with the raw captured pane
    text on timeout -- never silently passes.
    """
    deadline = monotonic() + timeout_s
    dismissed_update_modal = False
    dismissed_migration_modal = False
    pane_text = ""
    while True:
        pane_text = capture()
        if ready_for_submission(pane_text):
            return pane_text
        if not dismissed_update_modal and _is_codex_update_modal(pane_text):
            _dismiss_codex_update_modal(capture=capture, send_keys=send_keys, sleep=sleep)
            dismissed_update_modal = True
            continue
        if not dismissed_migration_modal and _is_codex_model_migration_modal(pane_text):
            _dismiss_codex_model_migration_modal(
                capture=capture, send_keys=send_keys, sleep=sleep,
            )
            dismissed_migration_modal = True
            continue
        if monotonic() >= deadline:
            raise HardStop(
                f"codex readiness poll timed out after {timeout_s}s "
                f"(update_modal_dismissed={dismissed_update_modal}, "
                f"migration_modal_dismissed={dismissed_migration_modal}): {pane_text!r}"
            )
        sleep(poll_interval_s)


# --------------------------------------------------------------------------- #
# version.json residue discipline (orchestrator review, T-2572 refinement A).       #
# --------------------------------------------------------------------------- #


class _VersionJsonSnapshot:
    """Snapshots ``<codex_home>/version.json`` bytes; restores verbatim on
    ``__exit__``.

    A real codex launch's update-dismissal writes ``dismissed_version`` into
    this file as a side effect (live-verified 2026-08-05) -- residue
    discipline extends to this file, not just tmux/scope. Restoring to the
    PRE-run snapshot also means every real-cli run re-exercises the
    interactive modal, rather than leaking a "shown once ever" side effect
    into the real account's codex home.
    """

    def __init__(self, codex_home: Path) -> None:
        self._path = codex_home / "version.json"
        self._existed = False
        self._bytes: bytes = b""

    def __enter__(self) -> "_VersionJsonSnapshot":
        if self._path.exists():
            self._existed = True
            self._bytes = self._path.read_bytes()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._existed:
            self._path.write_bytes(self._bytes)
        elif self._path.exists():
            self._path.unlink()


# --------------------------------------------------------------------------- #
# Disposable tmux/systemd context guard + teardown (mirrors harness_         #
# fallback_swap.py's _assert_disposable_tmux_context /                       #
# _teardown_disposable_tmux_context -- tracked-acceptable duplication, same  #
# convention that file cites for its own _EnvOverride cousin).               #
# --------------------------------------------------------------------------- #


def _assert_disposable_tmux_context(context: "rc.TmuxRuntimeContext | None") -> None:
    """HardStop unless ``context`` is unambiguously this driver's own
    disposable namespace -- NEVER the default tmux socket or the production
    ``pantheon-runtime-tmux.scope``."""
    if context is None:
        raise HardStop("codex-ready driver requires an explicit disposable TmuxRuntimeContext")
    if not context.socket:
        raise HardStop(
            "codex-ready driver refuses an empty/default tmux socket -- must target a "
            "disposable named socket, never the default server"
        )
    if context.scope_unit == rc.TMUX_RUNTIME_SCOPE_UNIT:
        raise HardStop(
            f"codex-ready driver refuses the production scope unit {rc.TMUX_RUNTIME_SCOPE_UNIT!r}"
        )
    if not context.socket.startswith(_SOCKET_PREFIX) or not context.scope_unit.startswith(
        _SCOPE_PREFIX
    ):
        raise HardStop(
            f"codex-ready driver refuses a socket/scope outside its own "
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

    # Belt-and-suspenders beyond the scope stop above: scan for any surviving
    # PID still cgroup-tagged to the disposable scope and SIGKILL it.
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

    # kill-server returns as soon as the signal is sent, not once the server
    # has actually unlinked its socket file -- poll briefly before declaring
    # residue.
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
# Safe stand-in codex executable.                                            #
# --------------------------------------------------------------------------- #

_STAND_IN_SCRIPT_TEMPLATE = """\
#!/bin/bash
# Generated by tools.bringup_acceptance.harness_codex_ready (T-2572) -- SAFE
# stand-in for the real codex CLI. Renders codex's own captured
# update-available picker text at startup -- WITH THE CURSOR PRE-POSITIONED
# ON "Skip until next version" (option 3) -- then, once ONE line of input is
# received, renders a static ready-composer frame satisfying
# tools.submit_verify.ready_for_submission. Pre-positioning the cursor is
# deliberate: it lets this fixture exercise the driver's REAL capture ->
# verify-cursor-on-3 -> send Enter step against a genuinely live tmux pane
# (real systemd --user scope, real tmux, real _capture_visible_runtime_pane
# / _run_tmux send-keys) without needing to emulate a live TUI's per-
# keystroke Down-arrow redraw in bash. The "never blind-sends a fixed key
# sequence" safety property -- including the Down-press navigation this
# fixture does NOT exercise -- is proven separately, and more precisely, by
# this module's own pure-function tests against real captured modal-text
# fixtures (DismissModalTest).
set -u
cat <<'CODEX_READY_MODAL_FIXTURE'

  ✨ Update available! 0.0.0 -> 0.0.1

  Release notes: https://example.invalid/release-notes

  1. Update now (runs `npm install -g @openai/codex`)
  2. Skip
› 3. Skip until next version

  Press enter to continue
CODEX_READY_MODAL_FIXTURE
read -r _dismiss_line
cat <<'CODEX_READY_COMPOSER_FIXTURE'
╭───╮
›
─────────────────────────
CODEX_READY_COMPOSER_FIXTURE
while IFS= read -r _line; do :; done
"""


def _write_stand_in_script(path: Path) -> None:
    path.write_text(_STAND_IN_SCRIPT_TEMPLATE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------- #
# The driver.                                                                 #
# --------------------------------------------------------------------------- #


def codex_ready_status(evidence: Mapping[str, Any]) -> str:
    """The ONLY function allowed to say the codex-ready leg is green.

    Returns ``"proven"`` iff ``evidence["mode"]`` is ``"real-cli"`` AND
    ``evidence["ready"]`` is True. Every stand-in run -- including every
    automated test invocation -- reads ``"deferred"``, unconditionally, no
    matter how clean the run (mirrors harness_fallback_swap.py's
    ``kick_lands_executes_status`` verbatim).
    """
    if evidence.get("mode") != _MODE_REAL_CLI:
        return "deferred"
    return "proven" if evidence.get("ready") is True else "deferred"


def run_codex_ready_bringup(
    *,
    work_root: Path,
    account: str,
    persona: str | None = None,
    codex_executable: Path | None = None,
    ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
    ready_poll_interval_s: float = _DEFAULT_READY_POLL_INTERVAL_S,
) -> Mapping[str, Any]:
    """Stand up a disposable tmux/systemd scope, launch codex (real or
    stand-in) via the real ``runtime_controller`` control-plane chain, poll
    until it is genuinely input-ready (dismissing the update modal if it
    appears), and tear everything down.

    ``codex_executable=None`` runs the safe stand-in (mode="stand-in",
    :func:`codex_ready_status` can never read it as proven); a real path
    (e.g. the real ``codex`` binary) is the HO-operator real-cli fire
    (mode="real-cli").
    """
    run_id = uuid.uuid4().hex[:12]
    persona = persona or f"bringup-acceptance-codex-ready-{run_id}"
    mode = _MODE_REAL_CLI if codex_executable is not None else _MODE_STAND_IN

    work_root.mkdir(parents=True, exist_ok=True)
    manifest_path = work_root / "instance.yaml"
    state_root = work_root / "state"
    mailbox_hub = work_root / "mailbox"
    if mailbox_hub.resolve() == _REAL_HUB_DEFAULT.resolve():
        raise HardStop("codex-ready driver refuses the REAL mailbox hub")
    manifest_path.write_text(
        _fixture_manifest_text(persona=persona, account=account), encoding="utf-8",
    )

    resolved_executable = codex_executable
    if resolved_executable is None:
        resolved_executable = work_root / "stand-in-codex.sh"
        _write_stand_in_script(resolved_executable)

    binding = persona_bringup.load_binding(
        manifest_path, persona, harness_override="codex",
        executable_override=resolved_executable, mailbox_hub=mailbox_hub,
    )
    # Deliberately the REAL account home (getpass.getuser(), never a
    # synthetic account) -- see module docstring: a synthetic CODEX_HOME
    # would hit an auth/login wall and prove nothing about the real
    # daemon's bringup readiness.
    adapter = persona_bringup.build_adapter(binding, harness_home=binding.account_home)
    codex_home = binding.account_home / ".codex"

    context = _new_disposable_tmux_context(run_id)
    target = binding.controller.tmux

    def _capture() -> str:
        return rc._capture_visible_runtime_pane(target, context=context)

    def _send_key(key: str) -> None:
        rc._run_tmux("send-keys", "-t", rc._target_text(target), key, context=context)

    ready = False
    pane_text = ""
    try:
        with _VersionJsonSnapshot(codex_home):
            # build_interactive_launch's CommandSpec.cwd reads Path.cwd()
            # synchronously at THIS one call -- scope the chdir as tightly
            # as possible around it (P7: minimal blast radius) rather than
            # around the whole driver.
            previous_cwd = Path.cwd()
            try:
                os.chdir(work_root)
                # A fresh disposable target has never existed before --
                # always a create, never a replace.
                runtime_start = rc._start_runtime(
                    binding, adapter, replace=False, context=context,
                )
            finally:
                os.chdir(previous_cwd)

            rc._verify_started_runtime(
                binding, runtime_start, state_root=state_root, context=context,
            )

            pane_text = _wait_for_codex_ready(
                timeout_s=ready_timeout_s, poll_interval_s=ready_poll_interval_s,
                capture=_capture, send_keys=_send_key,
            )
            ready = True
    finally:
        residue = _teardown_disposable_tmux_context(context)
        shutil.rmtree(work_root, ignore_errors=True)
        if residue:
            raise HardStop(f"codex-ready driver left residue: {'; '.join(residue)}")

    return {
        "mode": mode,
        "run_id": run_id,
        "persona": persona,
        "context": {"socket": context.socket, "scope_unit": context.scope_unit},
        "codex_executable": str(resolved_executable),
        "codex_home": str(codex_home),
        "ready": ready,
        "pane_sha256": hashlib.sha256(pane_text.encode("utf-8")).hexdigest(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_codex_ready.py",
        description=(
            "T-2572 codex-comes-up-ready acceptance smoke -- HO-OPERATOR "
            "ENTRYPOINT. Pass --codex-executable pointing at the real codex "
            "binary to actually prove the leg (mode=real-cli); omit it to "
            "run the safe stand-in (mode=stand-in), which never flips the "
            "leg green -- see codex_ready_status()."
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
        evidence = run_codex_ready_bringup(
            work_root=args.work_root, account=args.account, persona=args.persona,
            codex_executable=args.codex_executable, ready_timeout_s=args.ready_timeout_s,
        )
    except HardStop as exc:
        sys.stderr.write(f"[harness_codex_ready] HardStop: {exc}\n")
        return 1
    status = codex_ready_status(evidence)
    print(json.dumps({**evidence, "status": status}, indent=2, default=str))
    if evidence["mode"] == _MODE_STAND_IN:
        sys.stderr.write(
            "[harness_codex_ready] mode=stand-in: this proves the driver + "
            "plumbing only -- the leg stays deferred/RED until a real-cli run\n"
        )
        return 0
    if status != "proven":
        sys.stderr.write("[harness_codex_ready] real-cli run did not reach ready\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
