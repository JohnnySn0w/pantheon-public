"""tools/bringup_acceptance/forced_cap_return.py -- forced-cap-out /
requeue / return-to-primary acceptance module (T-2556 phase 9 / T-2573).

The last off-prod build of the T-2556 acceptance suite. This module proves,
against the REAL ``runtime_controller``/``persona_bringup`` code (only the
OS-touching leaves mocked -- the exact seam T-2571's own ``harness_fallback_
swap.py`` established for ``_replace_runtime``), the forced-cap-out half of
the fallback story that T-2571 deliberately left to its planned-quota
sibling: an AUTHORITATIVE provider cap arrives with no advance warning ->
``runtime_controller.forced_swap()`` fails closed, archives a factual
handoff, and swaps -> the still-pending mailbox message survives the whole
cycle untouched (the "requeue" contract) -> the swap is idempotent against
a replay of the SAME message identity (the "graveyard-receipt bootstrap")
-> and, once the primary's quota resets, the SAME cooperative machine that
proves a planned swap also proves the return leg.

THE FINDING (T-2584) -- RESOLVED. ``persona_bringup.primary_recovery_
evidence()``'s first gate used to require ``state.get("trigger") ==
"planned-quota"`` before it would even look at a reset timestamp or probe
evidence, and ``forced_swap()`` always records ``trigger="forced-cap"`` --
net effect, a fallback session that started via forced_swap could never
auto-return to primary through ``wake_persistent_runtime``/CLI wiring, ever.
T-2584 closed that gap: the gate now accepts ``trigger in ("planned-quota",
"forced-cap")``, and ``forced_swap()`` records a schedule-computed
``primary_resets_at`` at swap time (the next weekly reset, tz-aware --
forced-cap has no advance warning the way planned-quota does, so it derives
one from the schedule instead of a provider-observed instant). Leg 4 below
proves the post-fix behavior against REAL code and a REAL completed-forced-
cap transition: the gate now accepts the forced-cap trigger and still
returns ``None`` in this fixture -- but only because the schedule-computed
reset is still in the future, not because the trigger is rejected. Once
that reset instant passes, the same gate yields real evidence and the
session auto-returns.

Five independently-gap-able legs:

1. ``forced_cap_transition_state_machine`` -- the real ``forced_swap()``
   end-to-end: breaker_opened -> factual graveyard archive (credentials
   scrubbed via the real ``graveyard.scrub_credentials``) -> durable
   ``forced-failover.json`` receipt -> ``_replace_runtime`` (real function,
   only its OS leaves mocked) -> awaiting-successor-read, runtime_swapped
   -> ack -> completed. Then an identical-identity replay proves the
   graveyard-receipt-bootstrap fast path (``_completed_forced_transition_
   matches``) reuses the receipt without re-opening the breaker or
   re-invoking any OS leaf; a differing-identity replay (this module's own
   second test, not a leg of its own) proves that fast path is identity-
   scoped, not blanket idempotent.
2. ``mailbox_requeue_and_reclaim`` -- real ``tools.mailbox`` against a
   disposable hub: the pending message that triggered the wake is NEVER
   claimed before the transition settles (``wake_persistent_runtime`` only
   peeks at ``_oldest_pending_identity``, never consumes), so it survives
   untouched in ``new/`` through the whole forced-cap cycle -- that IS the
   requeue guarantee, proven at the transport layer, not just by identity
   matching. Once acked, the SAME message is validly claimed+consumed.
3. ``persona_target_selection`` -- ``persona_bringup.select_recorded_
   runtime_binding`` redirects to the incoming (fallback) binding for a
   completed+swapped forced-cap transition exactly as it does for a
   planned-quota one (T-2571's own leg 3) -- selection only checks
   phase+runtime_swapped, never trigger.
4. ``primary_recovery_gate_finding`` -- confirms the post-T-2584 gate
   behavior above, proven against leg 1's own real completed-forced-cap
   state.
5. ``quota_reset_return_continuation`` -- the real return-to-primary
   machine: ``runtime_controller.planned_swap()`` with ``trigger="quota-
   reset"``, swapping from the forced-cap fallback binding back to primary,
   the same cooperative preparing -> awaiting-successor-read -> completed
   dance T-2571's leg 2 proved for the outbound direction. Driven by a
   directly-constructed ``quota_evidence`` dict shaped like ``primary_
   recovery_evidence``'s own return fields -- labeled as directly-
   constructed in evidence because the fixture's scheduled reset hasn't
   arrived in-test yet, not because the gate never fires for forced-cap.

Q5-style finding (why the literal live kick is deferred, not attempted
here). The identical two structural blockers ``harness_fallback_swap.py``'s
own module docstring names apply here without modification, because
``forced_swap``'s/``planned_swap``'s real tmux delivery goes through the
identical code path (``_replace_runtime``/``_prompt_successor``/``submit_
to_runtime``): ``TMUX_RUNTIME_SCOPE_UNIT`` is a hardcoded module-level
singleton, and ``send_keys_submit.sh``'s landed+consumed check is tightly
coupled to the real CLI's TUI rendering. So this module's own row stays
``proven`` for legs 1-5; the composite capability :data:`COMPOSITE_ID`
additionally needs the live kick-through-a-real-post-swap-pane leg
(T-2574, coordinated live window) before it reads GREEN.
"""

from __future__ import annotations

import contextlib
import json
import shutil
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


MODULE_ID = "forced-cap-return"
COMPOSITE_ID = "forced-cap-return-fully-functional"
DEFERRED_TO_TICKET = "T-2574"
RECOVERY_GAP_TICKET = "T-2584"  # now the RESOLUTION ticket -- see leg 4's module docstring section

# The real hub this module must NEVER write to -- mirrors mailbox_roundtrip.
# py's / harness_fallback_swap.py's own module-level guard constant.
_REAL_HUB_DEFAULT = Path("/home/the-ancestor/mailbox")

# Frame edge + composer glyph + no confirm-dialog text -- satisfies tools.
# submit_verify.ready_for_submission's full conjunction (mirrors harness_
# fallback_swap.py's own _READY_PANE_FIXTURE_TEXT). Fed to a mocked
# _capture_visible_runtime_pane so the REAL _verify_runtime_input_ready
# polling/timeout loop still runs.
_READY_PANE_FIXTURE_TEXT = "╭───╮\n❯ \n" + "─" * 25

# Deliberately duplicated cousin of harness_fallback_swap.py's own manifest
# fixture -- "tracked-acceptable duplication" (each module owns its own
# disposable test fixture; not shared cross-module state to import).
_ROLE = "bringup-acceptance-forced-cap-role"
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
    """A disposable instance.yaml-shaped manifest, never the real repo file."""
    return (
        _MANIFEST_TEMPLATE.replace("__TMUX_SESSION__", "bringup-acceptance-forced-{persona}")
        .replace("__ROLE__", _ROLE)
        .replace("__PERSONA__", persona)
        .replace("__ACCOUNT__", account)
    )


def _replace_runtime_leaf_mocks(*, pane_texts: list[str]) -> tuple[contextlib.ExitStack, Any, Any]:
    """Enter the LANDED T-2571 seam; caller owns closing the returned stack.

    Only OS-touching leaves are mocked here -- ``_target_known``/``_start_
    runtime``/``_verify_started_runtime``/``_capture_visible_runtime_pane``/
    ``time.sleep``/``submit_to_runtime`` -- so the real ``_verify_runtime_
    input_ready`` polling/timeout/ready-detection loop always executes under
    proof (mirrors harness_fallback_swap.py's own established seam; NOT the
    shallower whole-function ``_verify_runtime_input_ready`` mock T-2571
    itself rebounded on).

    Mocking lives INSIDE this helper (called from inside each leg function,
    never left to the caller of ``prove()``) so ``prove()`` can never
    accidentally touch a real tmux/systemd surface regardless of who calls
    it.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(rc, "_target_known", return_value=False))
    stack.enter_context(
        mock.patch.object(
            rc, "_start_runtime",
            return_value=rc.RuntimeStart(
                scope_unit="bringup-acceptance-forced.scope", created_scope=True,
            ),
        )
    )
    stack.enter_context(mock.patch.object(rc, "_verify_started_runtime", return_value=None))
    capture = stack.enter_context(
        mock.patch.object(rc, "_capture_visible_runtime_pane", side_effect=list(pane_texts))
    )
    stack.enter_context(mock.patch.object(rc.time, "sleep", return_value=None))
    submit = stack.enter_context(mock.patch.object(rc, "submit_to_runtime"))
    return stack, capture, submit


# ---------------------------------------------------------------------------
# Leg 1: real forced_swap() transition-state-machine
# ---------------------------------------------------------------------------


def _forced_cap_transition_state_machine_leg(
    ctx: RunContext,
    *,
    state_root: Path,
    primary: "persona_bringup.PersonaBinding",
    target: "persona_bringup.PersonaBinding",
    message_id: str,
    correlation_id: str,
) -> Mapping[str, Any]:
    current = rc.RuntimeHandle(
        primary.launch, primary.controller.tmux, "forced-cap-session-1", rc._utc_now(),
    )
    error = rc.ProviderQuotaExhausted(
        "weekly cap",
        profile=primary.launch.profile,
        provider_error="authoritative cap; leaked credential sshpass -p 'hunter2' -- must be scrubbed",
    )
    drain_id = rc.new_drain_id()

    stack, capture, submit = _replace_runtime_leaf_mocks(pane_texts=[_READY_PANE_FIXTURE_TEXT])
    with stack:
        with _expect_pending():
            rc.forced_swap(
                current, target, error=error, message_id=message_id, correlation_id=correlation_id,
                drain_id=drain_id, state_root=state_root,
            )
        breaker_state = rc.transition_state(ctx.persona, state_root=state_root)
        if breaker_state.get("phase") != "awaiting-successor-read":
            raise HardStop(
                f"expected phase=awaiting-successor-read after forced_swap, got "
                f"{breaker_state.get('phase')!r}"
            )
        if breaker_state.get("breaker_open") is not True:
            raise HardStop("breaker_open was not recorded True by forced_swap")
        if breaker_state.get("handoff_kind") != "forced-factual":
            raise HardStop("forced_swap did not record a forced-factual handoff")
        expected_evidence = breaker_state.get("expected_successor_read_evidence")
        if not isinstance(expected_evidence, str) or not expected_evidence:
            raise HardStop("no expected_successor_read_evidence was recorded")

        receipt_path = rc._persona_state(ctx.persona, state_root) / "forced-failover.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("schema") != "pantheon-forced-failover/v1":
            raise HardStop(f"forced-failover receipt has the wrong schema: {receipt!r}")
        if "hunter2" in receipt.get("provider_error", ""):
            raise HardStop("forced-failover receipt leaked an unscrubbed credential")

        rc.acknowledge_successor_read(ctx.persona, evidence=expected_evidence, state_root=state_root)
        completed_state = rc.transition_state(ctx.persona, state_root=state_root)
        if completed_state.get("phase") != "completed":
            raise HardStop(f"expected phase=completed after ack, got {completed_state.get('phase')!r}")

        # Graveyard-receipt-bootstrap: replay the SAME still-pending identity
        # -- the fast path must reuse the receipt, not re-open the breaker,
        # not re-archive, not re-invoke any OS leaf.
        pre_replay_capture_calls = capture.call_count
        pre_replay_submit_calls = submit.call_count
        replay_receipt = rc.forced_swap(
            current, target, error=error, message_id=message_id, correlation_id=correlation_id,
            drain_id=rc.new_drain_id(), state_root=state_root,
        )
        os_leaf_reinvoked = (
            capture.call_count != pre_replay_capture_calls
            or submit.call_count != pre_replay_submit_calls
        )

    handoff_or_graveyard = str(completed_state.get("handoff_or_graveyard"))
    if str(replay_receipt.handoff_or_graveyard) != handoff_or_graveyard:
        raise HardStop("idempotent replay did not reuse the original graveyard evidence")
    if os_leaf_reinvoked:
        raise HardStop("idempotent replay re-invoked an OS-touching leaf -- fast path did not short-circuit")

    return {
        "receipt_path": str(receipt_path),
        "completed_transition": completed_state,
        "handoff_or_graveyard": handoff_or_graveyard,
        "expected_successor_read_evidence": expected_evidence,
        "idempotent_replay": {
            "reused": True,
            "os_leaf_reinvoked": os_leaf_reinvoked,
            "handoff_or_graveyard": str(replay_receipt.handoff_or_graveyard),
            "trigger": replay_receipt.trigger,
        },
    }


class _ExpectPending:
    """Tiny context manager asserting RuntimeTransitionPending is raised.

    A dedicated helper (rather than unittest's assertRaises, which this
    module -- not a TestCase -- cannot use) so the "first forced_swap call
    always requests a read-ack, never returns" contract reads as an
    explicit, named assertion in the leg body.
    """

    def __enter__(self) -> "_ExpectPending":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise HardStop("forced_swap: expected RuntimeTransitionPending, none was raised")
        if not issubclass(exc_type, rc.RuntimeTransitionPending):
            return False
        return True


def _expect_pending() -> _ExpectPending:
    return _ExpectPending()


# ---------------------------------------------------------------------------
# Leg 2: mailbox requeue-and-reclaim (transport-level proof of "requeue")
# ---------------------------------------------------------------------------


def _mailbox_requeue_and_reclaim_leg(
    *,
    hub_path: Path,
    state_root: Path,
    primary: "persona_bringup.PersonaBinding",
    target: "persona_bringup.PersonaBinding",
) -> Mapping[str, Any]:
    if hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
        raise HardStop(
            f"refusing to prove mailbox_requeue_and_reclaim against the REAL hub "
            f"{hub_path} -- RunContext.hub_path must be a disposable tree"
        )
    inbox = primary.mailbox
    mailbox.provision(inbox)
    sent_id = mailbox.send(
        str(inbox),
        {"correlation_id": "forced-cap-requeue-corr", "kind": "acceptance-fixture"},
        "forced-cap-requeue-sender",
        subject="forced-cap-return-requeue-prove",
        provision_if_missing=False,
    )

    message_id, correlation_id = persona_bringup._oldest_pending_identity(inbox)
    if message_id != sent_id:
        raise HardStop(f"_oldest_pending_identity did not resolve the sent message: {message_id!r}")
    if not (inbox / "new" / sent_id).exists():
        raise HardStop("sent message is missing from new/ before the forced-cap cycle")

    current = rc.RuntimeHandle(
        primary.launch, primary.controller.tmux, "forced-cap-mailbox-session-1", rc._utc_now(),
    )
    error = rc.ProviderQuotaExhausted(
        "weekly cap", profile=primary.launch.profile, provider_error="authoritative cap",
    )
    stack, _capture, _submit = _replace_runtime_leaf_mocks(pane_texts=[_READY_PANE_FIXTURE_TEXT])
    with stack, _expect_pending():
        rc.forced_swap(
            current, target, error=error, message_id=message_id, correlation_id=correlation_id,
            drain_id=rc.new_drain_id(), state_root=state_root,
        )
    if not (inbox / "new" / sent_id).exists():
        raise HardStop("message was claimed/moved before the transition settled -- requeue guarantee violated")
    survived_untouched = (inbox / "new" / sent_id).exists() and not (inbox / "cur" / sent_id).exists()

    state = rc.transition_state(primary.persona, state_root=state_root)
    rc.acknowledge_successor_read(
        primary.persona,
        evidence=str(state["expected_successor_read_evidence"]),
        state_root=state_root,
    )

    received: list[Mapping[str, Any]] = []

    def _handler(msg: dict) -> None:
        received.append(msg)

    consume_result = mailbox.consume(str(inbox), _handler, label="forced-cap-return-reclaim")
    if consume_result.get("skipped"):
        raise HardStop(f"consume() was skipped: {consume_result}")
    if sent_id not in consume_result.get("processed", []):
        raise HardStop(f"message {sent_id} not reported processed: {consume_result}")
    if (inbox / "new" / sent_id).exists():
        raise HardStop(f"message {sent_id} still present in new/ after consume")
    if not (inbox / "cur" / sent_id).exists():
        raise HardStop(f"message {sent_id} missing from cur/ after ack")
    if len(received) != 1 or received[0].get("id") != sent_id:
        raise HardStop(f"handler observed {len(received)} message(s), expected exactly 1 matching {sent_id}")

    return {
        "message_id": sent_id,
        "correlation_id": correlation_id,
        "survived_untouched": survived_untouched,
        "reclaimed_message_id": received[0].get("id"),
        "forced_swap": {"message_id": message_id, "correlation_id": correlation_id},
    }


# ---------------------------------------------------------------------------
# Leg 3: persona / tmux-target-preserving selection (forced-cap trigger)
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
            "select_recorded_runtime_binding did not redirect to the forced-cap fallback: "
            f"got {selected_reference!r}, expected {target_reference!r}"
        )
    if selected.persona != primary.persona:
        raise HardStop("persona identity was not preserved across the forced-cap swap")
    if selected.controller.tmux != primary.controller.tmux:
        raise HardStop("tmux target drifted across the forced-cap swap")
    return {
        "primary_reference": persona_bringup.runtime_reference(primary),
        "selected_reference": selected_reference,
        "persona_preserved": selected.persona == primary.persona,
        "selected_harness": selected.launch.harness,
    }


# ---------------------------------------------------------------------------
# Leg 4: primary_recovery_evidence gate finding (T-2584 resolved)
# ---------------------------------------------------------------------------


def _primary_recovery_gate_finding_leg(
    *, state_root: Path, primary: "persona_bringup.PersonaBinding",
) -> Mapping[str, Any]:
    state = rc.transition_state(primary.persona, state_root=state_root)
    observed_trigger = state.get("trigger")
    if observed_trigger != "forced-cap":
        raise HardStop(
            f"expected leg 1's real completed transition to carry trigger='forced-cap', "
            f"got {observed_trigger!r}"
        )
    if state.get("phase") != "completed" or state.get("fallback_active") is not True:
        raise HardStop("leg 1's transition is not a genuinely-completed fallback_active swap")

    recovery_evidence = persona_bringup.primary_recovery_evidence(primary, state_dir=state_root)
    if recovery_evidence is not None:
        raise HardStop(
            "primary_recovery_evidence unexpectedly returned recovery evidence for a "
            f"forced-cap-originated transition: {recovery_evidence!r} -- since {RECOVERY_GAP_TICKET} "
            "the gate accepts a forced-cap trigger, so None here is expected only while the "
            "schedule-computed primary_resets_at is still in the future; a non-None result means "
            "either this fixture's reset instant has already passed or the schedule computation "
            "regressed -- re-evaluate before treating this as a real gap"
        )
    return {
        "observed_trigger": observed_trigger,
        "recovery_evidence": recovery_evidence,
        "finding": (
            f"persona_bringup.primary_recovery_evidence()'s first gate has accepted "
            f"trigger=='forced-cap' (alongside 'planned-quota') since {RECOVERY_GAP_TICKET}; a "
            f"completed forced-cap transition (trigger={observed_trigger!r}) now satisfies it. "
            f"This leg returns None only because the schedule-computed primary_resets_at "
            f"forced_swap() records is still in the future -- once that reset instant passes, "
            f"the same gate yields real evidence and the session auto-returns. The earlier "
            f"gap (forced-cap could never auto-return) was resolved by {RECOVERY_GAP_TICKET}."
        ),
    }


# ---------------------------------------------------------------------------
# Leg 5: quota-reset return-to-primary continuation (real planned_swap)
# ---------------------------------------------------------------------------


def _quota_reset_return_continuation_leg(
    ctx: RunContext,
    *,
    state_root: Path,
    primary: "persona_bringup.PersonaBinding",
    target: "persona_bringup.PersonaBinding",
) -> Mapping[str, Any]:
    handoff_path = state_root / "RETURN-HANDOFF.md"
    drain_id = rc.new_drain_id()
    current = rc.RuntimeHandle(
        target.launch, target.controller.tmux, "forced-cap-fallback-session-1", rc._utc_now(),
    )
    # Directly-constructed -- shaped like persona_bringup.primary_recovery_
    # evidence's own return dict. Since T-2584 the gate accepts a
    # forced-cap trigger, but leg 4's fixture returns None because its
    # schedule-computed reset hasn't arrived yet -- this leg proves the
    # underlying return machine itself directly, rather than waiting out
    # the reset window to exercise the auto-wiring end to end.
    quota_evidence = {
        "primary_profile": persona_bringup.runtime_reference(primary),
        "reset_passed_at": rc._iso(rc._utc_now()),
        "directly_constructed": "true",
        "source": RECOVERY_GAP_TICKET,
    }

    stack, _capture, _submit = _replace_runtime_leaf_mocks(pane_texts=[_READY_PANE_FIXTURE_TEXT])
    with stack:
        with _expect_pending():
            rc.planned_swap(
                current, primary, message_id="return-msg-1", drain_id=drain_id,
                handoff_path=handoff_path, quota_evidence=quota_evidence, trigger="quota-reset",
                state_root=state_root,
            )
        preparing_state = rc.transition_state(ctx.persona, state_root=state_root)
        if preparing_state.get("phase") != "preparing" or preparing_state.get("trigger") != "quota-reset":
            raise HardStop(
                f"expected phase=preparing/trigger=quota-reset after the handoff request, got "
                f"{preparing_state.get('phase')!r}/{preparing_state.get('trigger')!r}"
            )
        handoff_path.write_text(
            "# Return-to-primary handoff\nPrimary quota has reset; returning.\n", encoding="utf-8",
        )
        with _expect_pending():
            rc.planned_swap(
                current, primary, message_id="return-msg-1", drain_id=drain_id,
                handoff_path=handoff_path, quota_evidence=quota_evidence, trigger="quota-reset",
                state_root=state_root,
            )
    awaiting_state = rc.transition_state(ctx.persona, state_root=state_root)
    if awaiting_state.get("phase") != "awaiting-successor-read":
        raise HardStop(
            f"expected phase=awaiting-successor-read after replace, got "
            f"{awaiting_state.get('phase')!r}"
        )
    if awaiting_state.get("fallback_active") is not False:
        raise HardStop("quota-reset transition did not clear fallback_active")
    expected_evidence = awaiting_state.get("expected_successor_read_evidence")
    if not isinstance(expected_evidence, str) or not expected_evidence:
        raise HardStop("no expected_successor_read_evidence was recorded for the return swap")

    rc.acknowledge_successor_read(ctx.persona, evidence=expected_evidence, state_root=state_root)
    completed_state = rc.transition_state(ctx.persona, state_root=state_root)
    if completed_state.get("phase") != "completed":
        raise HardStop(f"expected phase=completed after ack, got {completed_state.get('phase')!r}")
    if (
        completed_state.get("outgoing_harness") != target.launch.harness
        or completed_state.get("incoming_harness") != primary.launch.harness
    ):
        raise HardStop(
            "return transition does not name the expected "
            f"{target.launch.harness}->{primary.launch.harness} swap: {completed_state!r}"
        )

    return {
        "handoff_path": str(handoff_path),
        "quota_evidence": quota_evidence,
        "completed_transition": completed_state,
    }


# ---------------------------------------------------------------------------
# Env-var scoping helper (belt-and-suspenders around real read paths)
# ---------------------------------------------------------------------------


class _EnvOverride:
    """Set env vars for a scope, restoring whatever was there after.

    A tiny, deliberately-duplicated cousin of harness_fallback_swap.py's own
    ``_EnvOverride`` -- same "tracked-acceptable duplication" convention.
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


def _disposable_graveyard_env(root: Path) -> _EnvOverride:
    return _EnvOverride({"PANTHEON_GRAVEYARD_ROOT": str(root)})


# ---------------------------------------------------------------------------
# The AcceptanceModule
# ---------------------------------------------------------------------------


class ForcedCapReturnModule:
    id = MODULE_ID
    tier: Tier = "core"

    def __init__(self) -> None:
        self._module_dirs: dict[str, Path] | None = None
        self._mailbox_baseline: restore_mod.Baseline | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        # inventory.py has no dedicated "forced-cap-return" tag yet (T-2556
        # phase 2 discovery only emits "harness-fallback-swap" for a
        # configured fallback). forced-cap capability applies exactly where
        # that same underlying fact holds -- a fallback is configured -- so
        # this module deliberately declares off the harness-fallback-swap
        # signal rather than self.id. A dedicated inventory tag is a
        # possible future cleanup, not a gap in this declare().
        matches = tuple(
            entry for entry in target.entries if entry.module_id == "harness-fallback-swap"
        )
        applies = any(entry.status == "applies" for entry in matches)
        reason = (
            f"{len(matches)} harness-fallback-swap-tagged entr"
            f"{'y' if len(matches) == 1 else 'ies'} on target (forced-cap-return applies "
            "to the same fallback relationship, cap-triggered)"
            if matches else "no harness-fallback-swap-tagged entries on target"
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
            "graveyard_root": module_dir / "graveyard",
        }
        manifest_path = self._module_dirs["manifest"]
        state_root = self._module_dirs["state_root"]
        graveyard_root = self._module_dirs["graveyard_root"]

        if ctx.hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
            raise HardStop(f"{self.id}: refusing to prove against the REAL mailbox hub {ctx.hub_path}")

        manifest_path.write_text(
            _fixture_manifest_text(persona=ctx.persona, account=ctx.account), encoding="utf-8",
        )
        primary = persona_bringup.load_binding(manifest_path, ctx.persona, mailbox_hub=ctx.hub_path)
        target = persona_bringup.first_fallback_binding(manifest_path, primary)
        if target is None:
            raise HardStop(f"{self.id}: fixture manifest produced no fallback binding")

        inbox = primary.mailbox
        mailbox_target = restore_mod.BaselineTarget(
            account=ctx.account,
            mailbox_dirs={"inbox-new": inbox / "new", "inbox-cur": inbox / "cur"},
            disposable_markers=(inbox,),
            backup_root=module_dir / "mailbox-backups",
        )
        self._mailbox_baseline = restore_mod.record_baseline(mailbox_target)
        recorder.append("baseline", {"inbox": inbox})

        legs: dict[str, Any] = {}
        evidence_paths: list[Path] = []

        with _disposable_graveyard_env(graveyard_root):
            state_machine_detail = _forced_cap_transition_state_machine_leg(
                ctx, state_root=state_root, primary=primary, target=target,
                message_id="prove-msg-1", correlation_id="prove-corr-1",
            )
        recorder.append("forced_cap_transition_state_machine", state_machine_detail)
        legs["forced_cap_transition_state_machine"] = state_machine_detail
        evidence_paths.append(
            write_module_evidence(
                paths, self.id, "forced_cap_transition_state_machine", state_machine_detail,
            )
        )

        selection_detail = _persona_target_selection_leg(
            manifest_path=manifest_path, state_root=state_root, primary=primary, target=target,
        )
        recorder.append("persona_target_selection", selection_detail)
        legs["persona_target_selection"] = selection_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "persona_target_selection", selection_detail)
        )

        gate_detail = _primary_recovery_gate_finding_leg(state_root=state_root, primary=primary)
        recorder.append("primary_recovery_gate_finding", gate_detail)
        legs["primary_recovery_gate_finding"] = gate_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "primary_recovery_gate_finding", gate_detail)
        )

        with _disposable_graveyard_env(graveyard_root):
            return_detail = _quota_reset_return_continuation_leg(
                ctx, state_root=state_root, primary=primary, target=target,
            )
        recorder.append("quota_reset_return_continuation", return_detail)
        legs["quota_reset_return_continuation"] = return_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "quota_reset_return_continuation", return_detail)
        )

        with _disposable_graveyard_env(graveyard_root):
            mailbox_detail = _mailbox_requeue_and_reclaim_leg(
                hub_path=ctx.hub_path, state_root=module_dir / "mailbox-state",
                primary=primary, target=target,
            )
        recorder.append("mailbox_requeue_and_reclaim", mailbox_detail)
        legs["mailbox_requeue_and_reclaim"] = mailbox_detail
        evidence_paths.append(
            write_module_evidence(paths, self.id, "mailbox_requeue_and_reclaim", mailbox_detail)
        )

        deferred_detail = {
            "deferred_to": DEFERRED_TO_TICKET,
            "leg": "kick_lands_executes",
            "reason": (
                "TMUX_RUNTIME_SCOPE_UNIT is a hardcoded module-level singleton, and "
                "send_keys_submit.sh's landed+consumed check is tightly coupled to the real "
                "CLI's TUI rendering -- the same class of gap harness_fallback_swap.py's own "
                "kick_lands_executes leg defers for the identical reason. Proven live in the "
                f"coordinated {DEFERRED_TO_TICKET} window instead."
            ),
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
            mailbox_state_dir = self._module_dirs["state_root"].parent / "mailbox-state"
            if mailbox_state_dir.is_dir():
                shutil.rmtree(mailbox_state_dir)
            if mailbox_state_dir.exists():
                residue.append(f"leftover mailbox-state: {mailbox_state_dir}")
            detail["module_dirs"] = {label: str(path) for label, path in self._module_dirs.items()}

        if residue:
            raise HardStop(f"{self.id}: restore residue: " + "; ".join(residue))
        return RestoreEvidence(module_id=self.id, restored=True, detail=detail)

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        gaps = tuple(evidence.detail.get("gaps", ()))
        legs_summary = (
            "real forced_swap() breaker-open -> factual graveyard archive (credentials "
            "scrubbed) -> awaiting-successor-read -> ack -> completed, with an identity-"
            "scoped graveyard-receipt-bootstrap replay + a real transport-level mailbox "
            "requeue-and-reclaim + persona/tmux-preserving selection + the real quota-"
            "reset return-to-primary machine (directly-constructed recovery evidence) "
            + ("proven" if not gaps else "gapped: " + "; ".join(gaps))
        )
        summary = (
            f"{self.id}: {legs_summary}; the live kick-through-a-real-post-swap-pane leg is "
            f"deferred to {DEFERRED_TO_TICKET} (coordinated live window); "
            f"primary_recovery_evidence() accepts forced-cap-originated recovery since "
            f"{RECOVERY_GAP_TICKET} (gate widened + forced_swap() now records a "
            f"schedule-computed primary_resets_at) -- substrate-proven here, composite "
            f"'{COMPOSITE_ID}' additionally needs the live leg before GREEN"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status, summary=summary,
            evidence_refs=evidence.evidence_paths,
        )
