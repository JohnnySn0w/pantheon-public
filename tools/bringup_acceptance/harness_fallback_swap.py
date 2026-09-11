"""tools/bringup_acceptance/harness_fallback_swap.py -- planned harness-swap
transition-state-machine acceptance module (T-2556 phase 8 / T-2571).

THE T-2382 gap: T-2382 proved the argv gate (Claude runtime carries exactly
one explicit ``--permission-mode default``) live, but never exercised the
FULL cooperative transition ``runtime_controller.planned_swap()`` drives.
This module proves that whole machine end-to-end, off-prod/in-suite,
against the REAL ``runtime_controller``/``persona_bringup`` code (only the
OS-touching leaves mocked -- the exact seam ``test_runtime_controller.py``'s
own ``test_replace_runtime_publishes_after_ready`` already uses):

    _begin_transition -> phase=preparing (handoff requested, barred drains)
      -> handoff verified changed -> chronicle.archive_required
      -> _replace_runtime (real function; only its OS leaves are mocked)
      -> phase=awaiting-successor-read, runtime_swapped=True
      -> _prompt_successor -> acknowledge_successor_read
      -> phase=completed, successor_read_ack=True
      -> drain barrier: the ORIGINAL claimed drain_id stays barred, a NEW
         drain_id is allowed (assert_drain_allowed's own post-ack contract).

Four independently-gap-able legs:

1. ``argv_gate`` -- proves ``ClaudeHarnessAdapter.build_interactive_launch``
   (claude.py) emits EXACTLY ONE ``--permission-mode`` token, both for the
   implicit-default case and an explicit native override -- never zero,
   never two. Pure function of a ``HarnessLaunchConfig``; no isolation
   needed.
2. ``transition_state_machine`` -- the real ``planned_swap`` /
   ``acknowledge_successor_read`` / ``assert_drain_allowed`` contract,
   claude persona/tmux/state preserved, over a disposable
   ``PANTHEON_CHRONICLE_ROOT`` and a disposable ``state_root``. Only
   ``_target_known``/``_start_runtime``/``_verify_started_runtime``/
   ``_capture_visible_runtime_pane`` are mocked -- the EXACT seam
   ``test_runtime_controller.py``'s own ``test_replace_runtime_publishes_
   after_ready`` already established for ``_replace_runtime``. ``_replace_
   runtime`` itself, the REAL ``_verify_runtime_input_ready`` polling/
   timeout/ready-detection loop (against a canned but real ``tools.
   submit_verify.ready_for_submission`` check on the mocked pane text),
   ``chronicle.archive_required``, and every phase/ack/drain-barrier check
   run for REAL.
3. ``persona_target_selection`` -- proves ``persona_bringup.
   select_recorded_runtime_binding`` redirects to the incoming (fallback)
   binding once leg 2's transition reaches completed+runtime_swapped, and
   that persona identity + tmux target (the stable operator target) are
   byte-identical to the outgoing binding's. Requires a REAL (but disposable
   tmp-file) ``instance.yaml``-shaped manifest -- ``load_runtime_reference``
   calls ``persona_bringup.load_binding`` against a manifest PATH, so a
   duck-typed stand-in (harness_runtime.py's own ``_disposable_binding``
   shape) cannot stand in for this leg.
4. ``correlated_mailbox_roundtrip`` -- a nonce-correlated send -> consume ->
   reply chain against a disposable hub (reusing ``mailbox_roundtrip.py``'s
   own disposable-hub + correlation-id + ground-truth-from-disk pattern),
   where the reply payload carries the harness/profile
   ``select_recorded_runtime_binding`` resolved AFTER the swap (leg 3's own
   output). This is the honest, off-prod-provable extent of "roundtrip
   THROUGH the swapped runtime": it proves correlation against the
   POST-SWAP-RESOLVED runtime identity, not a literal message delivered
   through a live post-swap tmux pane -- see the Q5-style deferral below.

Q5-style finding, UPDATED (T-2589 changed the answer -- read this before
assuming the live kick is structurally impossible off-prod; it no longer
is). ``harness_runtime.py``'s own module docstring named two blockers for
its own ``kick_lands_executes`` leg; the first one no longer holds here:

* ~~``runtime_controller.TMUX_RUNTIME_SCOPE_UNIT`` is a hardcoded
  module-level singleton ... not parameterizable per-binding~~ -- T-2589
  landed :class:`tools.runtime_controller.TmuxRuntimeContext`, threaded
  through ``_start_runtime``/``_verify_started_runtime``/
  ``_verify_runtime_input_ready``/``_replace_runtime``/
  ``submit_to_runtime``/``planned_swap``. A disposable named tmux socket +
  disposable systemd scope unit is now first-class, so the real
  ``_replace_runtime``/``submit_to_runtime`` CAN safely land a message on an
  actually-live successor pane off-prod, isolated from the production
  default socket / ``pantheon-runtime-tmux.scope`` by construction.
* ``send_keys_submit.sh``'s "landed and got consumed" check is STILL a
  per-harness pending-text predicate tightly coupled to a real CLI's TUI
  rendering -- unaffected by T-2589. A disposable stand-in that faithfully
  emulated that rendering would only prove its own emulation, not the real
  chain, which is why the live driver below proves the disposable-scope
  MECHANICS (stand up -> swap -> submit lands -> ack -> teardown) against a
  SAFE long-lived stand-in command, and reserves the REAL claude<->codex
  CLI invocation for an HO-operator-run coordinated window (T-2572) --
  never run automatically by this module's own ``prove()`` or by this
  suite's tests.

:func:`run_live_kick_lands_executes` is that driver (its own test class,
``LiveKickDisposableScopeLiveTests``, proves it for real against real tmux +
real systemd --user, on a disposable socket/scope, using the safe stand-in).
:func:`kick_lands_executes_status` is the STRUCTURAL false-green guard: it
returns ``"proven"`` only for evidence tagged ``mode="real-cli"`` with a
genuinely completed transition -- a stand-in run (including every automated
test run) is tagged ``mode="stand-in"`` and can never read anything but
``"deferred"``, no matter how clean the run. This module's own row stays
``proven`` for legs 1-4; the composite capability :data:`COMPOSITE_ID`
additionally needs a ``real-cli``-tagged live-kick run (T-2572, coordinated
live window, HO-operator-invoked via this module's ``main()`` CLI) before it
reads GREEN -- mirrors ``harness_runtime.py``'s own "substrate-proven,
composite needs the live leg" framing verbatim.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools import mailbox
from tools import persona_bringup
from tools import runtime_controller as rc
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
from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter.types import HarnessLaunchConfig, TmuxTarget


MODULE_ID = "harness-fallback-swap"
COMPOSITE_ID = "harness-fallback-swap-fully-functional"
DEFERRED_TO_TICKET = "T-2572"

# The real hub this module must NEVER write to -- mirrors mailbox_roundtrip.
# py's own module-level guard constant (see that module's docstring for why
# it is not imported from either mailbox.py or persona_bringup.py).
_REAL_HUB_DEFAULT = Path("/home/the-ancestor/mailbox")

_ROLE = "bringup-acceptance-swap-role"
_SWAP_SENDER_PERSONA = "swap-roundtrip-sender"
_SWAP_RECIPIENT_PERSONA = "swap-roundtrip-recipient"
_SWAP_CONSUME_LABEL = "harness-fallback-swap-roundtrip-prove"

# Frame edge + composer glyph + no confirm-dialog text -- satisfies tools.
# submit_verify.ready_for_submission's full conjunction (mirrors harness_
# runtime.py's own _READY_FIXTURE_TEXT). Fed to a mocked _capture_visible_
# runtime_pane so the REAL _verify_runtime_input_ready polling/timeout loop
# still runs, exactly matching test_runtime_controller.py's own
# test_replace_runtime_publishes_after_ready seam for _replace_runtime.
_READY_PANE_FIXTURE_TEXT = "╭───╮\n❯ \n" + "─" * 25

_KICK_LANDS_EXECUTES_DEFERRAL_REASON = (
    "T-2589 parameterized runtime_controller's tmux socket/scope (TmuxRuntimeContext), "
    "so a real post-swap pane CAN now receive an actually-landed message off-prod on a "
    "disposable socket/scope -- that structural blocker is gone. A live driver "
    "(run_live_kick_lands_executes, this module) exists and is proven against a SAFE "
    "long-lived stand-in command in this suite's own tests (real tmux, real systemd "
    "--user, disposable socket+scope, guaranteed teardown). What remains deferred is "
    "only the REAL claude<->codex CLI invocation itself -- reserved for an HO-operator-run "
    "coordinated window (this module's main() CLI is the entrypoint) because "
    "send_keys_submit.sh's landed+consumed check is a per-harness pending-text predicate "
    "tightly coupled to a real CLI's TUI rendering, which a stand-in cannot prove without "
    "just proving its own emulation. kick_lands_executes_status() is the structural guard: "
    "it reads 'proven' only for mode='real-cli' evidence, never for a stand-in run."
)

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
    claude:
      default_profile: interactive-subscription
      profiles:
        interactive-subscription:
          executable: /bin/echo
          mode: interactive
          meter: subscription
          quota_source: statusline
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
      fallbacks:
        - codex/interactive-chatgpt
      claude:
        profile: interactive-subscription
        model: null
        native:
          permission_mode: default
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
    harness: claude-code
"""


def _fixture_manifest_text(*, persona: str, account: str) -> str:
    """A disposable instance.yaml-shaped manifest, never the real repo file.

    Tmux session is templated on ``{persona}`` ONLY (never ``{harness}``/
    ``{profile}`` -- ``_validate_stable_tmux_template`` enforces that), which
    is exactly what makes the tmux target byte-identical across the primary
    and fallback bindings this module builds from it -- the "stable operator
    target" leg 3 proves.
    """
    return (
        _MANIFEST_TEMPLATE.replace("__TMUX_SESSION__", "bringup-acceptance-swap-{persona}")
        .replace("__ROLE__", _ROLE)
        .replace("__PERSONA__", persona)
        .replace("__ACCOUNT__", account)
    )


# ---------------------------------------------------------------------------
# Leg 1: argv gate
# ---------------------------------------------------------------------------


def _claude_launch(native: Mapping[str, Any]) -> HarnessLaunchConfig:
    return HarnessLaunchConfig(
        harness="claude", profile="interactive-subscription", executable=Path("/bin/echo"),
        model=None, mode="interactive", meter="subscription", quota_source="statusline",
        native=dict(native), sources=("bringup-acceptance",),
    )


def _permission_mode_positions(argv: tuple[str, ...]) -> list[int]:
    return [index for index, value in enumerate(argv) if value == "--permission-mode"]


def assert_exactly_one_permission_mode(
    argv: tuple[str, ...], *, expected_value: str, label: str
) -> int:
    """Guard function proving the "exactly once, not zero/twice" contract.

    Kept as its own callable (not inlined) so the guard itself -- not just
    today's adapter behavior -- is what a regression test exercises.
    """
    positions = _permission_mode_positions(argv)
    if len(positions) != 1:
        raise HardStop(
            f"argv_gate[{label}]: expected exactly one --permission-mode token, got "
            f"{len(positions)}: {argv!r}"
        )
    index = positions[0]
    if index + 1 >= len(argv) or argv[index + 1] != expected_value:
        raise HardStop(
            f"argv_gate[{label}]: expected {expected_value!r} at position {index + 1}, "
            f"argv={argv!r}"
        )
    return index


def _argv_gate_leg(ctx: RunContext) -> Mapping[str, Any]:
    target = TmuxTarget(f"bringup-acceptance-argv-{ctx.run_id}", "harness", "0")
    cases: dict[str, Any] = {}
    for label, native, expected_value in (
        ("implicit_default", {}, "default"),
        ("explicit_override", {"permission_mode": "acceptEdits"}, "acceptEdits"),
    ):
        adapter = ClaudeHarnessAdapter(
            "bringup-acceptance", claude_home=Path("/tmp"), launch=_claude_launch(native),
        )
        spec = adapter.build_interactive_launch(target, cwd=Path("/tmp/launch"))
        index = assert_exactly_one_permission_mode(
            spec.argv, expected_value=expected_value, label=label,
        )
        cases[label] = {
            "argv": spec.argv, "permission_mode_index": index, "value": spec.argv[index + 1],
        }
    return {"cases": cases}


# ---------------------------------------------------------------------------
# Leg 2: real planned_swap transition-state-machine
# ---------------------------------------------------------------------------


def _transition_state_machine_leg(
    ctx: RunContext,
    *,
    manifest_path: Path,
    state_root: Path,
    primary: "persona_bringup.PersonaBinding",
    target: "persona_bringup.PersonaBinding",
) -> Mapping[str, Any]:
    del manifest_path  # not read directly here -- primary/target are pre-resolved
    handoff_path = state_root / "HANDOFF.md"
    drain_id = rc.new_drain_id()
    current = rc.RuntimeHandle(
        primary.launch, primary.controller.tmux, "bringup-acceptance-session-1", rc._utc_now(),
    )
    events: list[str] = []

    # Before any transition exists, a drain is simply allowed (nothing to bar).
    rc.assert_drain_allowed(ctx.persona, drain_id, state_root=state_root)

    with mock.patch.object(rc, "_target_known", return_value=False), mock.patch.object(
        rc, "_start_runtime",
        return_value=rc.RuntimeStart(
            scope_unit=f"bringup-acceptance-{ctx.run_id}.scope", created_scope=True,
        ),
    ), mock.patch.object(rc, "_verify_started_runtime", return_value=None), mock.patch.object(
        rc, "_capture_visible_runtime_pane", side_effect=[_READY_PANE_FIXTURE_TEXT],
    ), mock.patch.object(rc.time, "sleep", return_value=None), mock.patch.object(
        rc, "submit_to_runtime"
    ) as submit:
        try:
            rc.planned_swap(
                current, target, message_id="bringup-acceptance-msg-1", drain_id=drain_id,
                handoff_path=handoff_path, state_root=state_root,
            )
            raise HardStop(
                "planned_swap: expected RuntimeTransitionPending requesting a handoff "
                "on the first call"
            )
        except rc.RuntimeTransitionPending as exc:
            events.append(f"handoff_requested: {exc}")

        preparing_state = rc.transition_state(ctx.persona, state_root=state_root)
        if preparing_state.get("phase") != "preparing":
            raise HardStop(
                f"expected phase=preparing after the handoff request, got "
                f"{preparing_state.get('phase')!r}"
            )

        for probe_drain in (drain_id, rc.new_drain_id()):
            try:
                rc.assert_drain_allowed(ctx.persona, probe_drain, state_root=state_root)
                raise HardStop(
                    f"assert_drain_allowed did not bar drain {probe_drain} during preparing"
                )
            except rc.RuntimeTransitionPending:
                pass

        handoff_path.write_text(
            "# Bringup-acceptance transition handoff\nSwap proof in progress.\n",
            encoding="utf-8",
        )

        try:
            rc.planned_swap(
                current, target, message_id="bringup-acceptance-msg-1", drain_id=drain_id,
                handoff_path=handoff_path, state_root=state_root,
            )
            raise HardStop(
                "planned_swap: expected RuntimeTransitionPending awaiting successor "
                "read-ack on the second call"
            )
        except rc.RuntimeTransitionPending as exc:
            events.append(f"awaiting_successor_read: {exc}")

        awaiting_state = rc.transition_state(ctx.persona, state_root=state_root)
        if awaiting_state.get("phase") != "awaiting-successor-read":
            raise HardStop(
                f"expected phase=awaiting-successor-read after replace, got "
                f"{awaiting_state.get('phase')!r}"
            )
        if awaiting_state.get("runtime_swapped") is not True:
            raise HardStop("runtime_swapped is not True after the replace step")
        if (
            awaiting_state.get("outgoing_harness") != primary.launch.harness
            or awaiting_state.get("incoming_harness") != target.launch.harness
        ):
            raise HardStop(
                "transition record does not name the expected "
                f"{primary.launch.harness}->{target.launch.harness} swap: {awaiting_state!r}"
            )
        expected_evidence = awaiting_state.get("expected_successor_read_evidence")
        if not isinstance(expected_evidence, str) or not expected_evidence:
            raise HardStop("no expected_successor_read_evidence was recorded")

        try:
            rc.assert_drain_allowed(ctx.persona, drain_id, state_root=state_root)
            raise HardStop(
                "assert_drain_allowed did not bar the claimed drain during "
                "awaiting-successor-read"
            )
        except rc.RuntimeTransitionPending:
            pass

        rc.acknowledge_successor_read(
            ctx.persona, evidence=expected_evidence, state_root=state_root,
        )

        if submit.call_count != 2:
            raise HardStop(
                f"expected exactly 2 submit_to_runtime calls (handoff request + "
                f"successor prompt), got {submit.call_count}"
            )

    completed_state = rc.transition_state(ctx.persona, state_root=state_root)
    if completed_state.get("phase") != "completed":
        raise HardStop(f"expected phase=completed after ack, got {completed_state.get('phase')!r}")
    if completed_state.get("successor_read_ack") is not True:
        raise HardStop("successor_read_ack is not True after acknowledge_successor_read")

    try:
        rc.assert_drain_allowed(ctx.persona, drain_id, state_root=state_root)
        raise HardStop("the originally-claimed drain_id was not barred after successor ack")
    except rc.RuntimeTransitionPending:
        pass
    new_drain_id = rc.new_drain_id()
    rc.assert_drain_allowed(ctx.persona, new_drain_id, state_root=state_root)

    return {
        "events": events,
        "handoff_path": str(handoff_path),
        "drain_id": drain_id,
        "new_drain_id_allowed": new_drain_id,
        "expected_successor_read_evidence": expected_evidence,
        "completed_transition": completed_state,
        "outgoing": {"harness": primary.launch.harness, "profile": primary.launch.profile},
        "incoming": {"harness": target.launch.harness, "profile": target.launch.profile},
    }


# ---------------------------------------------------------------------------
# Leg 3: persona / tmux-target-preserving selection
# ---------------------------------------------------------------------------


def _persona_target_selection_leg(
    *,
    manifest_path: Path,
    state_root: Path,
    primary: "persona_bringup.PersonaBinding",
    target: "persona_bringup.PersonaBinding",
) -> Mapping[str, Any]:
    selected = persona_bringup.select_recorded_runtime_binding(
        manifest_path, primary, state_dir=state_root,
    )
    selected_reference = persona_bringup.runtime_reference(selected)
    target_reference = persona_bringup.runtime_reference(target)
    if selected_reference != target_reference:
        raise HardStop(
            "select_recorded_runtime_binding did not redirect to the incoming fallback: "
            f"got {selected_reference!r}, expected {target_reference!r}"
        )
    if selected.persona != primary.persona:
        raise HardStop("persona identity was not preserved across the swap")
    if selected.role != primary.role:
        raise HardStop("role identity was not preserved across the swap")
    if selected.controller.tmux != primary.controller.tmux:
        raise HardStop(
            f"tmux target drifted across the swap: {selected.controller.tmux!r} != "
            f"{primary.controller.tmux!r}"
        )
    return {
        "primary_reference": persona_bringup.runtime_reference(primary),
        "selected_reference": selected_reference,
        "tmux_target": rc.runtime_target_text(selected.controller.tmux),
        "persona_preserved": selected.persona == primary.persona,
        "role_preserved": selected.role == primary.role,
        "selected_harness": selected.launch.harness,
        "selected_profile": selected.launch.profile,
    }


# ---------------------------------------------------------------------------
# Leg 4: correlated mailbox roundtrip against the post-swap-resolved binding
# ---------------------------------------------------------------------------


def _correlated_mailbox_roundtrip_leg(
    *, hub_path: Path, selected_reference: Mapping[str, str],
) -> Mapping[str, Any]:
    if hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
        raise HardStop(
            f"refusing to prove correlated_mailbox_roundtrip against the REAL hub "
            f"{hub_path} -- RunContext.hub_path must be a disposable tree"
        )
    sender_inbox = hub_path / _SWAP_SENDER_PERSONA
    recipient_inbox = hub_path / _SWAP_RECIPIENT_PERSONA
    mailbox.provision(sender_inbox)
    mailbox.provision(recipient_inbox)

    nonce = uuid.uuid4().hex
    payload = {
        "nonce": nonce, "correlation_id": nonce,
        "reply_route": {"recipient_inbox": str(sender_inbox)},
    }
    message_id = mailbox.send(
        str(recipient_inbox), payload, _SWAP_SENDER_PERSONA,
        subject=f"harness-fallback-swap-roundtrip:{nonce}",
    )

    received: list[Mapping[str, Any]] = []

    def _handler(msg: dict) -> None:
        if not isinstance(msg.get("payload"), dict) or msg["payload"].get("nonce") != nonce:
            raise HardStop(f"handler observed unexpected message: {msg.get('id')}")
        received.append(msg)
        route = msg["payload"]["reply_route"]
        mailbox.send(
            route["recipient_inbox"],
            {
                "kind": "persona_result",
                "correlation_id": msg["payload"]["correlation_id"],
                "nonce": nonce,
                "post_swap_harness": selected_reference["harness"],
                "post_swap_profile": selected_reference["profile"],
            },
            _SWAP_RECIPIENT_PERSONA,
            subject=f"result:{msg['payload']['correlation_id']}",
        )

    consume_result = mailbox.consume(str(recipient_inbox), _handler, label=_SWAP_CONSUME_LABEL)

    if consume_result.get("skipped"):
        raise HardStop(f"consume() was skipped: {consume_result}")
    if message_id not in consume_result.get("processed", []):
        raise HardStop(f"message {message_id} not reported processed: {consume_result}")
    if (recipient_inbox / "new" / message_id).exists():
        raise HardStop(f"message {message_id} still present in new/ after consume")
    if not (recipient_inbox / "cur" / message_id).exists():
        raise HardStop(f"message {message_id} missing from cur/ after ack")
    if len(received) != 1 or received[0].get("id") != message_id:
        raise HardStop(
            f"handler observed {len(received)} message(s), expected exactly 1 matching "
            f"{message_id}"
        )

    reply_names = sorted(
        entry.name for entry in (sender_inbox / "new").iterdir() if entry.is_file()
    )
    if len(reply_names) != 1:
        raise HardStop(f"expected exactly one reply under {sender_inbox}/new, found {reply_names}")
    reply_record = json.loads((sender_inbox / "new" / reply_names[0]).read_text(encoding="utf-8"))
    reply_payload = reply_record.get("payload")
    if not isinstance(reply_payload, dict) or reply_payload.get("correlation_id") != nonce:
        raise HardStop(f"reply-route correlation mismatch: {reply_payload!r}")
    if (
        reply_payload.get("post_swap_harness") != selected_reference["harness"]
        or reply_payload.get("post_swap_profile") != selected_reference["profile"]
    ):
        raise HardStop(
            "correlated reply did not carry the post-swap-resolved runtime identity: "
            f"{reply_payload!r} vs {selected_reference!r}"
        )

    return {
        "nonce": nonce, "message_id": message_id, "reply_id": reply_names[0],
        "reply_payload": reply_payload,
    }


# ---------------------------------------------------------------------------
# Env-var scoping helpers (belt-and-suspenders around real read paths)
# ---------------------------------------------------------------------------


class _EnvOverride:
    """Set env vars for a scope, restoring whatever was there after.

    A tiny, deliberately-duplicated cousin of mailbox_roundtrip.py's own
    ``_disposable_env`` -- same "tracked-acceptable duplication" convention
    restore.py's own docstring names for its relationship to recorder.py.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        import os

        for key, value in self._values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *exc_info: object) -> None:
        import os

        for key, saved in self._saved.items():
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved


def _disposable_chronicle_env(root: Path) -> _EnvOverride:
    return _EnvOverride({"PANTHEON_CHRONICLE_ROOT": str(root)})


def _disposable_mailbox_env(hub_path: Path, state_path: Path) -> _EnvOverride:
    return _EnvOverride(
        {"PANTHEON_MAILBOX_HUB": str(hub_path), "PANTHEON_PERSONA_STATE_DIR": str(state_path)},
    )


# ---------------------------------------------------------------------------
# The AcceptanceModule
# ---------------------------------------------------------------------------


class HarnessFallbackSwapModule:
    id = MODULE_ID
    tier: Tier = "core"

    def __init__(self) -> None:
        self._module_dirs: dict[str, Path] | None = None
        self._mailbox_baseline: restore_mod.Baseline | None = None

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

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": ctx.persona})

        module_dir = paths.module_dir(self.id)
        self._module_dirs = {
            "manifest": module_dir / "instance.yaml",
            "state_root": module_dir / "state",
            "chronicle_root": module_dir / "chronicle",
        }
        manifest_path = self._module_dirs["manifest"]
        state_root = self._module_dirs["state_root"]
        chronicle_root = self._module_dirs["chronicle_root"]

        if ctx.hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
            raise HardStop(
                f"{self.id}: refusing to prove against the REAL mailbox hub {ctx.hub_path}"
            )
        sender_inbox = ctx.hub_path / _SWAP_SENDER_PERSONA
        recipient_inbox = ctx.hub_path / _SWAP_RECIPIENT_PERSONA
        # Baseline FIRST -- before anything risky -- so restore() can still
        # clean up even if a later leg raises HardStop partway (mirrors
        # mailbox_roundtrip.py's own ordering).
        mailbox_target = restore_mod.BaselineTarget(
            account=ctx.account,
            mailbox_dirs={
                "sender-new": sender_inbox / "new",
                "recipient-new": recipient_inbox / "new",
                "recipient-cur": recipient_inbox / "cur",
            },
            disposable_markers=(sender_inbox, recipient_inbox),
            backup_root=module_dir / "mailbox-backups",
        )
        self._mailbox_baseline = restore_mod.record_baseline(mailbox_target)
        recorder.append(
            "baseline", {"sender_inbox": sender_inbox, "recipient_inbox": recipient_inbox},
        )

        legs: dict[str, Any] = {}
        evidence_paths: list[Path] = []

        argv_detail = _argv_gate_leg(ctx)
        recorder.append("argv_gate", argv_detail)
        legs["argv_gate"] = argv_detail
        evidence_paths.append(write_module_evidence(paths, self.id, "argv_gate", argv_detail))

        manifest_path.write_text(
            _fixture_manifest_text(persona=ctx.persona, account=ctx.account), encoding="utf-8",
        )
        primary = persona_bringup.load_binding(manifest_path, ctx.persona, mailbox_hub=ctx.hub_path)
        target = persona_bringup.first_fallback_binding(manifest_path, primary)
        if target is None:
            raise HardStop(f"{self.id}: fixture manifest produced no fallback binding")

        with _disposable_chronicle_env(chronicle_root):
            transition_detail = _transition_state_machine_leg(
                ctx, manifest_path=manifest_path, state_root=state_root,
                primary=primary, target=target,
            )
        recorder.append("transition_state_machine", transition_detail)
        legs["transition_state_machine"] = transition_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "transition_state_machine", transition_detail)
        )

        selection_detail = _persona_target_selection_leg(
            manifest_path=manifest_path, state_root=state_root, primary=primary, target=target,
        )
        recorder.append("persona_target_selection", selection_detail)
        legs["persona_target_selection"] = selection_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "persona_target_selection", selection_detail)
        )

        with _disposable_mailbox_env(ctx.hub_path, ctx.state_path):
            mailbox_detail = _correlated_mailbox_roundtrip_leg(
                hub_path=ctx.hub_path,
                selected_reference={
                    "harness": selection_detail["selected_harness"],
                    "profile": selection_detail["selected_profile"],
                },
            )
        recorder.append("correlated_mailbox_roundtrip", mailbox_detail)
        legs["correlated_mailbox_roundtrip"] = mailbox_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "correlated_mailbox_roundtrip", mailbox_detail)
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

        recorder.append("verdict", {"status": "proven", "gaps": []})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=tuple(evidence_paths), detail={"legs": legs, "gaps": []},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        residue: list[str] = []
        detail: dict[str, Any] = {}

        if self._mailbox_baseline is not None:
            with _disposable_mailbox_env(ctx.hub_path, ctx.state_path):
                restore_mod.restore_to(self._mailbox_baseline)
                post = restore_mod.record_baseline(self._mailbox_baseline.target)
            try:
                restore_mod.verify_zero_residue(self._mailbox_baseline, post)
                mailbox_restored = True
            except HardStop as exc:
                residue.append(str(exc))
                mailbox_restored = False
            detail["mailbox"] = restore_mod.restore_payload(
                self._mailbox_baseline, post, restored=mailbox_restored,
            )

        if self._module_dirs is not None:
            for label, path in self._module_dirs.items():
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                if path.exists():
                    residue.append(f"leftover {label}: {path}")
            detail["module_dirs"] = {label: str(path) for label, path in self._module_dirs.items()}

        if residue:
            raise HardStop(f"{self.id}: restore residue: " + "; ".join(residue))
        return RestoreEvidence(module_id=self.id, restored=True, detail=detail)

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        gaps = tuple(evidence.detail.get("gaps", ()))
        legs_summary = (
            "argv-gate (exactly-one --permission-mode) + real planned_swap transition-"
            "state-machine (preparing -> awaiting-successor-read -> completed, successor-"
            "read ack, runtime_swapped, drain barrier) + persona/tmux-target-preserving "
            "selection + a correlated mailbox roundtrip against the POST-SWAP-RESOLVED "
            "binding (not a literal message through a live post-swap pane) "
            + ("proven" if not gaps else "gapped: " + "; ".join(gaps))
        )
        summary = (
            f"{self.id}: {legs_summary}; the live kick-through-a-real-post-swap-pane leg "
            f"has a driver now (run_live_kick_lands_executes, stand-in-proven) but stays "
            f"deferred to {DEFERRED_TO_TICKET} until an HO-operator real-cli run -- "
            f"kick_lands_executes_status() only reads 'proven' for mode='real-cli' evidence, "
            f"substrate-proven here, composite '{COMPOSITE_ID}' additionally needs that "
            f"real-cli leg before GREEN"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status, summary=summary,
            evidence_refs=evidence.evidence_paths,
        )


# ---------------------------------------------------------------------------
# Live kick-lands-executes driver (T-2572).
#
# NEVER invoked by declare()/prove()/restore()/report() above -- those stay
# side-effect-light for every default acceptance-suite/CI run. This driver
# only runs when explicitly called (by its own tests, with the safe stand-in
# below) or by an HO operator via main() (with real claude/codex binaries).
# ---------------------------------------------------------------------------


_LIVE_KICK_MODE_STAND_IN = "stand-in"
_LIVE_KICK_MODE_REAL_CLI = "real-cli"

_LIVE_KICK_SOCKET_PREFIX = "pantheon-t2572-"
_LIVE_KICK_SCOPE_PREFIX = "pantheon-t2572-"

_LIVE_KICK_DEFAULT_GLYPHS = {"claude": "❯", "codex": "›"}

# Rendered once at stand-in startup -- frame + composer glyph satisfies
# tools.submit_verify.ready_for_submission's full conjunction (mirrors this
# module's own _READY_PANE_FIXTURE_TEXT, parameterized on __GLYPH__ so the
# same template serves both the claude-shaped and codex-shaped stand-in).
_LIVE_STAND_IN_SCRIPT_TEMPLATE = """\
#!/bin/bash
# Generated by tools.bringup_acceptance.harness_fallback_swap
# (run_live_kick_lands_executes, T-2572) -- SAFE long-lived stand-in for a
# real claude/codex CLI. Renders a static ready-fixture frame + composer
# glyph once at startup (satisfies tools.submit_verify.ready_for_submission),
# then appends every line of injected input to a receipt log -- proof a
# submission actually landed -- without executing anything resembling a real
# CLI. Ignores all argv: harness adapters append real per-harness flags
# (e.g. --permission-mode) this stand-in never parses or needs.
set -u
receipt_log=__RECEIPT_LOG__
printf '%s\\n%s \\n%s\\n' '╭───╮' '__GLYPH__' '─────────────────────────'
while IFS= read -r line; do
  printf '%s\\t%s\\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" "$line" >> "$receipt_log"
done
"""


def _assert_disposable_tmux_context(context: "rc.TmuxRuntimeContext | None") -> None:
    """HardStop unless ``context`` is unambiguously the live-kick driver's own
    disposable namespace -- NEVER the default tmux socket or the production
    ``pantheon-runtime-tmux.scope``. This is the guardrail that makes it safe
    to un-mock the OS leaves below; every call site in this driver runs this
    first."""
    if context is None:
        raise HardStop("live kick driver requires an explicit disposable TmuxRuntimeContext")
    if not context.socket:
        raise HardStop(
            "live kick driver refuses an empty/default tmux socket -- must target a "
            "disposable named socket, never the default server"
        )
    if context.scope_unit == rc.TMUX_RUNTIME_SCOPE_UNIT:
        raise HardStop(
            f"live kick driver refuses the production scope unit {rc.TMUX_RUNTIME_SCOPE_UNIT!r}"
        )
    if not context.socket.startswith(_LIVE_KICK_SOCKET_PREFIX) or not context.scope_unit.startswith(
        _LIVE_KICK_SCOPE_PREFIX
    ):
        raise HardStop(
            f"live kick driver refuses a socket/scope outside its own "
            f"{_LIVE_KICK_SOCKET_PREFIX!r}/{_LIVE_KICK_SCOPE_PREFIX!r} namespace: "
            f"socket={context.socket!r} scope_unit={context.scope_unit!r}"
        )


def _new_disposable_tmux_context(run_id: str) -> rc.TmuxRuntimeContext:
    context = rc.TmuxRuntimeContext(
        socket=f"{_LIVE_KICK_SOCKET_PREFIX}{run_id}",
        scope_unit=f"{_LIVE_KICK_SCOPE_PREFIX}{run_id}.scope",
    )
    _assert_disposable_tmux_context(context)
    return context


def _write_stand_in_script(path: Path, *, receipt_log: Path, glyph: str) -> None:
    """Render + chmod +x a disposable stand-in pane program at ``path``.

    See ``_LIVE_STAND_IN_SCRIPT_TEMPLATE``'s own comment for what this
    deliberately proves (plumbing) and does NOT attempt (a faithful real-CLI
    TUI emulation).
    """
    text = _LIVE_STAND_IN_SCRIPT_TEMPLATE.replace(
        "__RECEIPT_LOG__", shlex.quote(str(receipt_log)),
    ).replace("__GLYPH__", glyph)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _teardown_disposable_tmux_context(context: rc.TmuxRuntimeContext) -> list[str]:
    """Best-effort, idempotent teardown of a disposable tmux socket/scope.

    Safe to call even if nothing was ever created -- kill-server/systemctl
    against a socket/scope that never existed is a no-op, not an error. Runs
    unconditionally from the driver's own ``finally`` (guaranteed teardown
    even on an exception/HardStop mid-sequence). Returns a residue list
    instead of raising directly so the caller can decide how to combine it
    with any exception already propagating out of the try-block.
    """
    _assert_disposable_tmux_context(context)
    residue: list[str] = []

    subprocess.run(
        ["tmux", "-L", context.socket, "kill-server"],
        check=False, capture_output=True, text=True,
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
    # PID still cgroup-tagged to the disposable scope and SIGKILL it. A stray
    # process is exactly the residue this teardown exists to catch -- never
    # leave one running just because the scope stop above reported success.
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

    # `systemctl stop` returning does not guarantee the transient scope has
    # been fully unloaded from the manager's unit list yet -- ActiveState
    # can already read "inactive" while `list-units --all` still lists the
    # unit for a beat (GC of a stopped transient unit's load-state is a
    # separate, asynchronous step from the stop job completing, and lags
    # further under load). Poll the SAME signal any caller -- including this
    # driver's own tests -- actually checks for residue: `list-units --all`
    # reporting the unit gone, not an ActiveState snapshot (T-2619).
    deadline = time.monotonic() + 2.0
    still_listed = True
    while time.monotonic() < deadline:
        listed = subprocess.run(
            [
                "systemctl", "--user", "list-units", "--all", "--no-legend",
                context.scope_unit,
            ],
            check=False, capture_output=True, text=True,
        )
        if listed.stdout.strip() == "":
            still_listed = False
            break
        time.sleep(0.1)
    if still_listed:
        residue.append(f"scope {context.scope_unit} still listed by systemd after stop")

    # kill-server returns as soon as the signal is sent, not once the server
    # has actually unlinked its socket file -- poll briefly before declaring
    # residue (observed: the file can still exist for tens of ms afterward).
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


def kick_lands_executes_status(evidence: Mapping[str, Any]) -> str:
    """The ONLY function allowed to say the live-kick leg is green.

    Structural false-green guard (T-2572 review): a stand-in run proves the
    DRIVER + disposable-scope plumbing + teardown -- it never proves the live
    capability itself. Returns ``"proven"`` iff ``evidence["mode"]`` is
    ``"real-cli"`` AND the transition genuinely completed; every stand-in run
    -- including every automated test invocation of this driver -- reads
    ``"deferred"``, unconditionally, no matter how clean the run.
    """
    if evidence.get("mode") != _LIVE_KICK_MODE_REAL_CLI:
        return "deferred"
    transition = evidence.get("completed_transition") or {}
    if (
        transition.get("phase") == "completed"
        and transition.get("runtime_swapped") is True
        and transition.get("successor_read_ack") is True
    ):
        return "proven"
    return "deferred"


def run_live_kick_lands_executes(
    *,
    work_root: Path,
    account: str,
    persona: str | None = None,
    current_harness: str = "claude",
    target_harness: str = "codex",
    current_executable: Path | None = None,
    target_executable: Path | None = None,
) -> Mapping[str, Any]:
    """Stand up a disposable tmux scope, swap it for real, tear it down.

    ``current_executable``/``target_executable`` default to the safe
    long-lived stand-in (never a real CLI) -- passing both as real binaries
    (e.g. ``/bin/claude``, ``/usr/bin/codex``) is the HO-operator real-cli
    fire (see ``main()``); NEVER done by this driver's own tests. Evidence is
    tagged ``mode`` accordingly; ``kick_lands_executes_status()`` is the only
    function permitted to read that tag as green.

    Nothing here is mocked: ``_target_known``/``_start_runtime``/
    ``_verify_started_runtime``/``_capture_visible_runtime_pane``/
    ``submit_to_runtime`` all run for real, against a disposable named tmux
    socket + disposable systemd --user scope (T-2589's
    ``TmuxRuntimeContext``) -- never the default socket or the production
    ``pantheon-runtime-tmux.scope``.
    """
    run_id = uuid.uuid4().hex[:12]
    persona = persona or f"bringup-acceptance-live-kick-{run_id}"
    mode = (
        _LIVE_KICK_MODE_REAL_CLI
        if current_executable is not None and target_executable is not None
        else _LIVE_KICK_MODE_STAND_IN
    )

    work_root.mkdir(parents=True, exist_ok=True)
    manifest_path = work_root / "instance.yaml"
    state_root = work_root / "state"
    chronicle_root = work_root / "chronicle"
    handoff_path = work_root / "HANDOFF.md"
    receipt_log = work_root / "receipts.log"
    mailbox_hub = work_root / "mailbox"
    if mailbox_hub.resolve() == _REAL_HUB_DEFAULT.resolve():
        raise HardStop("live kick driver refuses the REAL mailbox hub")
    manifest_path.write_text(
        _fixture_manifest_text(persona=persona, account=account), encoding="utf-8",
    )

    resolved_current_executable = current_executable
    if resolved_current_executable is None:
        resolved_current_executable = work_root / "stand-in-current.sh"
        _write_stand_in_script(
            resolved_current_executable, receipt_log=receipt_log,
            glyph=_LIVE_KICK_DEFAULT_GLYPHS[persona_bringup.normalize_harness(current_harness)],
        )
    resolved_target_executable = target_executable
    if resolved_target_executable is None:
        resolved_target_executable = work_root / "stand-in-target.sh"
        _write_stand_in_script(
            resolved_target_executable, receipt_log=receipt_log,
            glyph=_LIVE_KICK_DEFAULT_GLYPHS[persona_bringup.normalize_harness(target_harness)],
        )

    # NOT first_fallback_binding()/load_runtime_reference() -- neither exposes
    # executable_override, which this driver needs for both the stand-in and
    # the real-cli path.
    current = persona_bringup.load_binding(
        manifest_path, persona, harness_override=current_harness,
        executable_override=resolved_current_executable, mailbox_hub=mailbox_hub,
    )
    target = persona_bringup.load_binding(
        manifest_path, persona, harness_override=target_harness,
        executable_override=resolved_target_executable, mailbox_hub=mailbox_hub,
    )

    context = _new_disposable_tmux_context(run_id)
    message_id = f"live-kick-{run_id}"
    drain_id = rc.new_drain_id()
    events: list[str] = []

    try:
        with _disposable_chronicle_env(chronicle_root):
            current_handle = rc._replace_runtime(current, state_root=state_root, context=context)
            events.append("current_runtime_started")

            try:
                rc.planned_swap(
                    current_handle, target, message_id=message_id, drain_id=drain_id,
                    handoff_path=handoff_path, state_root=state_root, context=context,
                )
                raise HardStop(
                    "planned_swap: expected RuntimeTransitionPending requesting a handoff "
                    "on the first live call"
                )
            except rc.RuntimeTransitionPending as exc:
                events.append(f"handoff_requested: {exc}")

            handoff_path.write_text(
                "# T-2572 live-kick handoff\nLive disposable-scope swap proof in progress.\n",
                encoding="utf-8",
            )

            try:
                rc.planned_swap(
                    current_handle, target, message_id=message_id, drain_id=drain_id,
                    handoff_path=handoff_path, state_root=state_root, context=context,
                )
                raise HardStop(
                    "planned_swap: expected RuntimeTransitionPending awaiting successor "
                    "read-ack on the second live call"
                )
            except rc.RuntimeTransitionPending as exc:
                events.append(f"awaiting_successor_read: {exc}")

            awaiting_state = rc.transition_state(persona, state_root=state_root)
            expected_evidence = awaiting_state.get("expected_successor_read_evidence")
            if not isinstance(expected_evidence, str) or not expected_evidence:
                raise HardStop("no expected_successor_read_evidence recorded by the live swap")

            rc.acknowledge_successor_read(persona, evidence=expected_evidence, state_root=state_root)

        completed_transition = rc.transition_state(persona, state_root=state_root)

        # Adversarial verification -- two independent signals, never trust
        # submit_to_runtime's return value alone (P4).
        pane_text = rc._capture_visible_runtime_pane(target.controller.tmux, context=context)
        receipt_text = receipt_log.read_text(encoding="utf-8") if receipt_log.exists() else ""
        handoff_prompt_needle = f"Prepare a concise runtime-transfer handoff at {handoff_path}"
        successor_prompt_needle = "Runtime transfer: read the continuity evidence at"
        landed = {
            "handoff_request_in_receipt_log": handoff_prompt_needle in receipt_text,
            "successor_prompt_in_receipt_log": successor_prompt_needle in receipt_text,
            "successor_prompt_in_captured_pane": successor_prompt_needle in pane_text,
        }
        if not all(landed.values()):
            raise HardStop(f"live kick submission evidence incomplete: {landed!r}")

        return {
            "mode": mode,
            "run_id": run_id,
            "persona": persona,
            "message_id": message_id,
            "drain_id": drain_id,
            "context": {"socket": context.socket, "scope_unit": context.scope_unit},
            "current": {
                "harness": current.launch.harness, "profile": current.launch.profile,
                "executable": str(current.launch.executable),
            },
            "target": {
                "harness": target.launch.harness, "profile": target.launch.profile,
                "executable": str(target.launch.executable),
            },
            "events": events,
            "completed_transition": completed_transition,
            "landed_evidence": landed,
            "receipt_log": str(receipt_log),
        }
    finally:
        residue = _teardown_disposable_tmux_context(context)
        shutil.rmtree(work_root, ignore_errors=True)
        if residue:
            raise HardStop(f"live kick driver left residue: {'; '.join(residue)}")


def _build_live_kick_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_fallback_swap.py live-kick",
        description=(
            "T-2572 live kick-lands-executes driver -- HO-OPERATOR ENTRYPOINT. Pass both "
            "--current-executable and --target-executable pointing at REAL claude/codex "
            "binaries to actually prove the live leg (mode=real-cli); omit either to run "
            "the safe stand-in (mode=stand-in), which never flips the leg green -- see "
            "kick_lands_executes_status()."
        ),
    )
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--persona", default=None)
    parser.add_argument("--current-harness", default="claude")
    parser.add_argument("--target-harness", default="codex")
    parser.add_argument("--current-executable", type=Path, default=None)
    parser.add_argument("--target-executable", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_live_kick_parser().parse_args(argv)
    try:
        evidence = run_live_kick_lands_executes(
            work_root=args.work_root, account=args.account, persona=args.persona,
            current_harness=args.current_harness, target_harness=args.target_harness,
            current_executable=args.current_executable, target_executable=args.target_executable,
        )
    except HardStop as exc:
        sys.stderr.write(f"[harness_fallback_swap live-kick] HardStop: {exc}\n")
        return 1
    status = kick_lands_executes_status(evidence)
    print(json.dumps({**evidence, "status": status}, indent=2, default=str))
    if evidence["mode"] == _LIVE_KICK_MODE_STAND_IN:
        sys.stderr.write(
            "[harness_fallback_swap live-kick] mode=stand-in: this proves the driver + "
            "plumbing only -- the leg stays deferred/RED until a real-cli run\n"
        )
        return 0
    if status != "proven":
        sys.stderr.write("[harness_fallback_swap live-kick] real-cli run did not complete\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
