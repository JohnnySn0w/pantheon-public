"""Tests for tools.bringup_acceptance.forced_cap_return (T-2556 phase 9 / T-2573).

Five legs, each independently exercised, plus the full declare -> prove ->
restore -> report lifecycle:

* forced_cap_transition_state_machine -- the REAL runtime_controller.
  forced_swap() end-to-end (breaker_opened -> factual Chronicle archive ->
  durable forced-failover receipt, credentials scrubbed -> replace -> await
  successor read -> ack), plus an identical-identity replay proving the
  chronicle-receipt-bootstrap fast path (no re-open, no re-archive, no
  re-replace), plus a differing-identity replay proving that fast path is
  identity-scoped, not blanket idempotent.
* mailbox_requeue_and_reclaim -- real tools.mailbox against a disposable
  hub: the pending message survives untouched in new/ through the whole
  forced-cap cycle (nothing ever claims it before the transition settles --
  that IS the requeue guarantee), then gets validly claimed+consumed once
  the transition completes.
* persona_target_selection -- persona_bringup.select_recorded_runtime_binding
  redirects identically for a trigger="forced-cap" completed transition (it
  only checks phase+runtime_swapped, not trigger).
* primary_recovery_gate_finding -- confirms the POST-T-2584 gate behavior:
  persona_bringup.primary_recovery_evidence() now accepts trigger==
  "forced-cap" (T-2584 widened the gate and forced_swap() now records a
  schedule-computed primary_resets_at). It still returns None for this
  leg's genuinely-completed forced-cap transition -- but only because the
  scheduled reset instant is still in the future, not because the trigger
  is rejected. Proven against real code + real state, not fabricated.
* quota_reset_return_continuation -- the REAL return-to-primary machine:
  runtime_controller.planned_swap() with trigger="quota-reset", swapping
  from the forced-cap fallback binding back to primary, driven by a
  directly-constructed quota_evidence dict (shaped like primary_recovery_
  evidence's own return fields -- labeled as directly-constructed because
  leg 4's fixture reset hasn't arrived in-test yet, not because the gate
  never fires for forced-cap).

No live production systemd/tmux server, real instance.yaml, or real mailbox
hub is ever touched by this suite.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import persona_bringup
from tools import runtime_controller as rc
from tools.bringup_acceptance import forced_cap_return
from tools.bringup_acceptance.forced_cap_return import (
    COMPOSITE_ID,
    DEFERRED_TO_TICKET,
    MODULE_ID,
    RECOVERY_GAP_TICKET,
    ForcedCapReturnModule,
    _forced_cap_transition_state_machine_leg,
    _fixture_manifest_text,
    _mailbox_requeue_and_reclaim_leg,
    _persona_target_selection_leg,
    _primary_recovery_gate_finding_leg,
    _quota_reset_return_continuation_leg,
)
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    RunContext,
    TargetInventory,
)


def _fixture_source_provenance(**kwargs: object) -> rc.RuntimeSourceProvenance:
    """Mirrors test_bringup_acceptance_harness_fallback_swap.py's own helper --
    the real _controller_source_provenance requires a clean git worktree, which
    a live development checkout is not."""
    source_hashes = {relative: "a" * 64 for relative in rc.RUNTIME_SOURCE_HASH_PATHS}
    return rc.RuntimeSourceProvenance(
        revision="1" * 40,
        source_hashes=source_hashes,
        source_manifest_sha256=rc._source_manifest_hash(source_hashes),
        runtime_start_boundary_at=kwargs["runtime_start_boundary_at"],
        runtime_start_observed_at=kwargs["runtime_start_observed_at"],
    )


def _provenance_patch():
    return mock.patch.object(
        rc, "_controller_source_provenance",
        side_effect=lambda **kwargs: _fixture_source_provenance(**kwargs),
    )


def _replace_runtime_seam(*, pane_texts: list[str]):
    """The LANDED T-2571 seam: mock only OS-touching leaves, let the real
    _verify_runtime_input_ready readiness/timeout loop execute against a
    canned-but-real pane string fed through the mocked capture call."""
    return (
        mock.patch.object(rc, "_target_known", return_value=False),
        mock.patch.object(
            rc, "_start_runtime",
            return_value=rc.RuntimeStart(scope_unit="bringup-acceptance-forced.scope", created_scope=True),
        ),
        mock.patch.object(rc, "_verify_started_runtime", return_value=None),
        mock.patch.object(rc, "_capture_visible_runtime_pane", side_effect=list(pane_texts)),
        mock.patch.object(rc.time, "sleep", return_value=None),
        mock.patch.object(rc, "submit_to_runtime"),
    )


def _run_context(root: Path) -> RunContext:
    return RunContext(
        run_id="20260805T140000Z",
        persona="forced-cap-acceptance-persona",
        account="claude-fallback",
        harness="claude",
        profile="interactive-subscription",
        hub_path=root / "hub",
        state_path=root / "state",
        evidence_root=root / "evidence",
        baseline_ref="deadbeef" * 5,
    )


def _load_bindings(root: Path, ctx: RunContext):
    manifest_path = root / "instance.yaml"
    manifest_path.write_text(
        _fixture_manifest_text(persona=ctx.persona, account=ctx.account), encoding="utf-8",
    )
    primary = persona_bringup.load_binding(manifest_path, ctx.persona, mailbox_hub=ctx.hub_path)
    target = persona_bringup.first_fallback_binding(manifest_path, primary)
    assert target is not None
    return manifest_path, primary, target


# --------------------------------------------------------------------- declare


class DeclareTest(unittest.TestCase):
    def test_applies_when_harness_fallback_swap_signal_present(self) -> None:
        module = ForcedCapReturnModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("fallback:codex/interactive-chatgpt"), tier="core",
                    status="applies", module_id="harness-fallback-swap",
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertTrue(decl.applies)
        self.assertEqual(decl.tier, "core")
        self.assertIn(COMPOSITE_ID, decl.contributes_to)

    def test_does_not_apply_when_no_fallback_signal(self) -> None:
        module = ForcedCapReturnModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("fallback:recovery:none-configured"), tier="core",
                    status="absent", module_id="harness-fallback-swap",
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)


# ------------------------------------------------- forced_cap_transition_state_machine


class ForcedCapTransitionStateMachineLegTest(unittest.TestCase):
    def test_full_phase_sequence_receipt_scrub_and_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"
            chronicle_root = root / "chronicle-root"

            with _provenance_patch(), mock.patch.dict(
                "os.environ", {"PANTHEON_CHRONICLE_ROOT": str(chronicle_root)},
            ):
                # The leg self-contains its own OS-leaf mocking (matches
                # harness_fallback_swap.py's landed seam) -- nothing to wrap here.
                detail = _forced_cap_transition_state_machine_leg(
                    ctx, state_root=state_root, primary=primary, target=target,
                    message_id="msg-1", correlation_id="corr-1",
                )
                self.assertFalse(detail["idempotent_replay"]["os_leaf_reinvoked"])

            completed = detail["completed_transition"]
            self.assertEqual(completed["phase"], "completed")
            self.assertEqual(completed["trigger"], "forced-cap")
            self.assertEqual(completed["handoff_kind"], "forced-factual")
            self.assertTrue(completed["successor_read_ack"])
            self.assertTrue(completed["runtime_swapped"])
            self.assertTrue(completed["fallback_active"])
            self.assertEqual(completed["outgoing_harness"], "claude")
            self.assertEqual(completed["incoming_harness"], "codex")

            receipt_path = Path(detail["receipt_path"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema"], "pantheon-forced-failover/v1")
            self.assertEqual(receipt["message_id"], "msg-1")
            self.assertEqual(receipt["correlation_id"], "corr-1")
            self.assertFalse(receipt["model_authored_handoff"])
            self.assertNotIn("hunter2", receipt["provider_error"])
            self.assertIn("<redacted-cred>", receipt["provider_error"])

            self.assertTrue(detail["idempotent_replay"]["reused"])
            self.assertEqual(detail["idempotent_replay"]["chronicle_evidence"], detail["chronicle_evidence"])

    def test_differing_identity_replay_does_not_reuse_the_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"
            chronicle_root = root / "chronicle-root"

            with _provenance_patch(), mock.patch.dict(
                "os.environ", {"PANTHEON_CHRONICLE_ROOT": str(chronicle_root)},
            ):
                _forced_cap_transition_state_machine_leg(
                    ctx, state_root=state_root, primary=primary, target=target,
                    message_id="msg-1", correlation_id="corr-1",
                )
                current = rc.RuntimeHandle(
                    primary.launch, primary.controller.tmux, "forced-cap-session-1", rc._utc_now(),
                )
                error = rc.ProviderQuotaExhausted(
                    "weekly cap", profile=primary.launch.profile, provider_error="authoritative cap",
                )
                # A fresh mock context for a genuinely-new cap event (differing
                # message identity) -- a real second _replace_runtime is expected,
                # proving the fast path is identity-scoped, not blanket idempotent.
                seam = _replace_runtime_seam(
                    pane_texts=[forced_cap_return._READY_PANE_FIXTURE_TEXT]
                )
                with seam[0], seam[1], seam[2], seam[3] as capture, seam[4], seam[5]:
                    with self.assertRaises(rc.RuntimeTransitionPending):
                        rc.forced_swap(
                            current, target, error=error, message_id="msg-2", correlation_id="corr-2",
                            drain_id=rc.new_drain_id(), state_root=state_root,
                        )
                    self.assertEqual(capture.call_count, 1)


# ------------------------------------------------------- mailbox_requeue_and_reclaim


class MailboxRequeueAndReclaimLegTest(unittest.TestCase):
    def test_message_survives_the_transition_then_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"
            chronicle_root = root / "chronicle-root"

            with _provenance_patch(), mock.patch.dict(
                "os.environ", {"PANTHEON_CHRONICLE_ROOT": str(chronicle_root)},
            ):
                detail = _mailbox_requeue_and_reclaim_leg(
                    hub_path=ctx.hub_path, state_root=state_root, primary=primary, target=target,
                )

            self.assertTrue(detail["survived_untouched"])
            self.assertEqual(detail["reclaimed_message_id"], detail["message_id"])
            self.assertEqual(detail["forced_swap"]["message_id"], detail["message_id"])

    def test_refuses_the_real_hub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            with self.assertRaises(HardStop):
                _mailbox_requeue_and_reclaim_leg(
                    hub_path=forced_cap_return._REAL_HUB_DEFAULT, state_root=root / "state",
                    primary=primary, target=target,
                )


# --------------------------------------------------------- persona_target_selection


class PersonaTargetSelectionLegTest(unittest.TestCase):
    def test_selects_incoming_binding_for_a_forced_cap_completed_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"

            rc._begin_transition(
                ctx.persona, trigger="forced-cap",
                outgoing_profile=primary.launch.profile, incoming_profile=target.launch.profile,
                outgoing_harness=primary.launch.harness, incoming_harness=target.launch.harness,
                drain_id="d1", state_root=state_root,
            )
            state = rc.transition_state(ctx.persona, state_root=state_root)
            state["phase"] = "completed"
            state["runtime_swapped"] = True
            state["successor_read_ack"] = True
            rc._atomic_json(rc._transition_path(ctx.persona, state_root), state)

            detail = _persona_target_selection_leg(
                manifest_path=manifest_path, state_root=state_root, primary=primary, target=target,
            )
            self.assertEqual(detail["selected_reference"], persona_bringup.runtime_reference(target))
            self.assertTrue(detail["persona_preserved"])
            self.assertEqual(primary.controller.tmux, target.controller.tmux)


# --------------------------------------------------- primary_recovery_gate_finding


class PrimaryRecoveryGateFindingLegTest(unittest.TestCase):
    def test_real_completed_forced_cap_state_never_yields_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"
            chronicle_root = root / "chronicle-root"

            with _provenance_patch(), mock.patch.dict(
                "os.environ", {"PANTHEON_CHRONICLE_ROOT": str(chronicle_root)},
            ):
                _forced_cap_transition_state_machine_leg(
                    ctx, state_root=state_root, primary=primary, target=target,
                    message_id="msg-1", correlation_id="corr-1",
                )

            detail = _primary_recovery_gate_finding_leg(state_root=state_root, primary=primary)
            self.assertIsNone(detail["recovery_evidence"])
            self.assertEqual(detail["observed_trigger"], "forced-cap")
            self.assertIn(RECOVERY_GAP_TICKET, detail["finding"])


# ------------------------------------------------- quota_reset_return_continuation


class QuotaResetReturnContinuationLegTest(unittest.TestCase):
    def test_full_return_to_primary_phase_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"
            chronicle_root = root / "chronicle-root"

            with _provenance_patch(), mock.patch.dict(
                "os.environ", {"PANTHEON_CHRONICLE_ROOT": str(chronicle_root)},
            ):
                _forced_cap_transition_state_machine_leg(
                    ctx, state_root=state_root, primary=primary, target=target,
                    message_id="msg-1", correlation_id="corr-1",
                )
                detail = _quota_reset_return_continuation_leg(
                    ctx, state_root=state_root, primary=primary, target=target,
                )

            completed = detail["completed_transition"]
            self.assertEqual(completed["phase"], "completed")
            self.assertEqual(completed["trigger"], "quota-reset")
            self.assertFalse(completed["fallback_active"])
            self.assertEqual(completed["outgoing_harness"], "codex")
            self.assertEqual(completed["incoming_harness"], "claude")
            self.assertTrue(detail["quota_evidence"]["directly_constructed"])


# ----------------------------------------------------------------------- prove


class ProveTest(unittest.TestCase):
    def test_prove_records_all_legs_names_the_deferred_and_gap_boundaries_and_restores_clean(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = ForcedCapReturnModule()

            with _provenance_patch():
                evidence = module.prove(ctx)

            self.assertEqual(evidence.status, "proven")
            legs = evidence.detail["legs"]
            self.assertEqual(
                set(legs),
                {
                    "forced_cap_transition_state_machine", "mailbox_requeue_and_reclaim",
                    "persona_target_selection", "primary_recovery_gate_finding",
                    "quota_reset_return_continuation", "kick_lands_executes",
                },
            )
            self.assertEqual(legs["kick_lands_executes"]["deferred_to"], DEFERRED_TO_TICKET)
            self.assertIsNone(legs["primary_recovery_gate_finding"]["recovery_evidence"])

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            self.assertFalse(
                (root / "evidence" / ctx.run_id / "modules" / MODULE_ID / "instance.yaml").exists()
            )

            row = module.report(evidence)
            self.assertIn(DEFERRED_TO_TICKET, row.summary)
            self.assertIn(COMPOSITE_ID, row.summary)
            self.assertIn(RECOVERY_GAP_TICKET, row.summary)


if __name__ == "__main__":
    unittest.main()
