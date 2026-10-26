"""T-5021: an operator hold is honoured by BOTH runtime planes.

The consume/wake plane (persona_bringup -> runtime_controller.ensure_viable_runtime)
never read overrides/holds.json, so pantheon-persona-consume@specialist launched the
held claude runtime on the first fire after its clone went clean (2026-10-15
01:46:58Z). These tests pin: a held runtime is never selected or launched by the
consume plane (selection loop, recorded-runtime rehydrate, the ensure_runtime
launch chokepoint, the quota-reset return swap) nor by the controller plane.

Helpers are borrowed (not inherited) from test_runtime_controller so its own
tests are not collected twice. tmp_path stand-ins only.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import mailbox
from tools import persona_bringup
from tools import runtime_controller as rc
from tools import test_runtime_controller as trc

def _hold(root: Path, persona: str, *runtimes: str) -> None:
    rc._atomic_json(
        rc._holds_path(persona, state_root=root),
        {
            "schema": "pantheon-persona-runtime-holds/v1",
            "holds": {
                ref: {"reason": "T-5021 test", "set_by": "operator:orchestrator", "set_ts": "2026-10-14T19:06:01Z"}
                for ref in runtimes
            },
        },
    )


class ConsumePlaneHoldTests(unittest.TestCase):
    _bindings = trc.InitialViabilityTests._bindings
    _selector_patches = trc.InitialViabilityTests._selector_patches
    _run_selector = trc.InitialViabilityTests._run_selector
    _write_stale_codex_runtime_state = trc.InitialViabilityTests._write_stale_codex_runtime_state
    _write_stale_selected_codex_viability = trc.InitialViabilityTests._write_stale_selected_codex_viability
    _reuse_patches = trc.InitialViabilityTests._reuse_patches

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("INVOCATION_ID", None)

    def test_selection_loop_skips_held_primary_and_selects_fallback(self) -> None:
        """The specialist incident shape: claude held, no live pane, clean source."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            _hold(root, primary.persona, rc._runtime_ref(primary))
            events: list[str] = []
            selection = self._run_selector(root, primary, fallback, events=events)
        self.assertNotIn("claude:start", events)
        self.assertIn("codex:start", events)
        self.assertIs(selection.binding, fallback)
        first = selection.receipt.attempts[0]
        self.assertEqual((first.runtime, first.status, first.cause), ("claude/primary", "skipped-held", "operator-hold"))
        self.assertEqual(selection.receipt.attempts[1].status, "selected")

    def test_held_candidate_does_not_consume_the_attempt_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            _hold(root, primary.persona, rc._runtime_ref(primary))
            events: list[str] = []
            selection = self._run_selector(root, primary, fallback, events=events, attempt_limit=1)
        self.assertIs(selection.binding, fallback)

    def test_all_held_launches_nothing_and_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            _hold(root, primary.persona, rc._runtime_ref(primary), rc._runtime_ref(fallback))
            events: list[str] = []
            with self.assertRaises(rc.RuntimeViabilityExhausted):
                self._run_selector(root, primary, fallback, events=events)
            state = rc.initial_viability_state(primary.persona, state_root=root)
        self.assertFalse([e for e in events if e.endswith(":start")])
        self.assertEqual(state["status"], "exhausted")
        self.assertEqual([a["status"] for a in state["attempts"]], ["skipped-held", "skipped-held"])

    def test_no_hold_keeps_manifest_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            events: list[str] = []
            selection = self._run_selector(root, primary, fallback, events=events)
        self.assertIs(selection.binding, primary)
        self.assertNotIn("codex:start", events)

    def test_held_recorded_runtime_behind_a_dead_pane_is_not_rehydrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            _hold(root, primary.persona, rc._runtime_ref(fallback))
            with mock.patch.object(rc, "_pane_process_pid", return_value=None), mock.patch.object(
                rc, "pane_exists", return_value=False
            ), mock.patch.object(rc, "_rehydrate_recorded_runtime_selection") as rehydrate:
                result = rc._matching_recorded_runtime_selection(
                    (primary, fallback),
                    adapter_factory=lambda binding: trc._Adapter(binding),
                    state_root=root,
                    backend=None,
                    context=None,
                    now=datetime(2026, 10, 15, 1, 46, 58, tzinfo=timezone.utc),
                    manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
                    attempt_limit=2,
                )
            evidence = trc._evidence_events(root)
        self.assertIsNone(result)
        rehydrate.assert_not_called()
        self.assertIn("recorded_runtime_rehydrate_skipped_held", evidence)

    def test_unheld_recorded_runtime_behind_a_dead_pane_still_rehydrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            _hold(root, primary.persona, rc._runtime_ref(primary))
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_rehydrate_recorded_runtime_selection", return_value="rehydrated"
            ) as rehydrate:
                result = rc._matching_recorded_runtime_selection(
                    (primary, fallback),
                    adapter_factory=lambda binding: trc._Adapter(binding),
                    state_root=root,
                    backend=None,
                    context=None,
                    now=datetime(2026, 10, 15, 1, 46, 58, tzinfo=timezone.utc),
                    manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
                    attempt_limit=2,
                )
        self.assertEqual(result, "rehydrated")
        rehydrate.assert_called_once()

    def test_live_held_pane_is_still_reused_not_relaunched(self) -> None:
        """A hold never kills or blocks a live runtime (controller semantics): reuse is
        not a launch."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[str] = []
            primary, fallback = self._bindings(root)
            recorded = trc._fixture_source_provenance(
                runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
                runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
            )
            self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            _hold(root, primary.persona, rc._runtime_ref(fallback))
            pane, pid, scope, start = self._reuse_patches(events)
            with pane, pid, scope, start as start_runtime, trc._provenance_patch_with(
                source_hashes=recorded.source_hashes,
            ):
                reused = rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: trc._Adapter(binding),
                    state_root=root,
                    backend=trc._ViabilityBackend(events),
                )
        start_runtime.assert_not_called()
        self.assertEqual(reused.receipt.status, "reused-recorded")

    def test_ensure_runtime_refuses_to_launch_a_held_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, _fallback = self._bindings(root)
            _hold(root, primary.persona, rc._runtime_ref(primary))
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime"
            ) as start_runtime, mock.patch.object(rc, "_stop_prior_harness_receiver_before_start") as stop:
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_runtime(primary, trc._Adapter(primary), state_root=root)
            evidence = trc._evidence_events(root)
        start_runtime.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(caught.exception.reason, "runtime-held")
        self.assertEqual(caught.exception.policy_skip_reason, "runtime_held")
        self.assertEqual(caught.exception.evidence["runtime"], "claude/primary")
        self.assertIn("runtime_launch_refused_held", evidence)

    def test_ensure_runtime_unheld_launch_branch_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            _hold(root, primary.persona, rc._runtime_ref(fallback))
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", side_effect=RuntimeError("launch reached")
            ) as start_runtime, mock.patch.object(rc, "_stop_prior_harness_receiver_before_start"):
                with self.assertRaises(rc.RuntimeOperationalFailure):
                    rc.ensure_runtime(primary, trc._Adapter(primary), state_root=root)
        start_runtime.assert_called_once()

    def test_quota_reset_return_to_a_held_primary_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = trc._binding(root, harness="codex", profile="fallback")
            primary = trc._binding(root, harness="claude", profile="primary")
            mailbox.provision(fallback.mailbox)
            _hold(root, primary.persona, "claude/primary")
            current = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, "session-fallback", rc._utc_now())
            recovery = {
                "primary_profile": "claude/primary",
                "reset_passed_at": "2026-08-03T12:00:00Z",
                "probe_observed_at": "2026-08-03T12:01:00Z",
            }
            with mock.patch.object(
                rc,
                "ensure_runtime",
                side_effect=lambda selected, adapter, *, state_root=None: trc._record_handle_runtime_state(
                    selected, current, Path(state_root),
                ),
            ), mock.patch.object(rc, "planned_swap") as planned, trc._provenance_patch():
                result = persona_bringup.wake_persistent_runtime(
                    fallback,
                    adapter=trc._Adapter(fallback),
                    primary_binding=primary,
                    recovery_evidence=recovery,
                    state_dir=root,
                    cwd=root,
                )
            evidence = trc._evidence_events(root)
        planned.assert_not_called()
        self.assertEqual(result["harness"], "codex")
        self.assertIn("quota_reset_return_skipped_held", evidence)

    def test_consume_maps_a_held_refusal_to_soft_exit_5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root)
            mailbox.provision(binding.mailbox)
            report = trc.DiagnosticReport(harness="codex", execution_context="test", observations=())
            refusal = rc.RuntimeViabilityBlocked(
                "runtime-held", {"reason": "runtime-held"}, policy_skip_reason="runtime_held"
            )
            with mock.patch.object(persona_bringup, "load_binding", return_value=binding), mock.patch.object(
                persona_bringup, "diagnose_persona", return_value=report
            ), mock.patch.object(persona_bringup, "build_adapter", return_value=trc._Adapter(binding)), mock.patch.object(
                persona_bringup, "wake_persistent_runtime", side_effect=refusal
            ), mock.patch.object(persona_bringup, "_print_json") as printed:
                code = persona_bringup.main(
                    ["consume", "--persona", "recovery", "--state-dir", str(root / "state"),
                     "--mailbox-hub", str(root / "mail")]
                )
        self.assertEqual(code, 5)
        self.assertEqual(printed.call_args.args[0]["consume"]["skipped"], "runtime_held")


class PreparingTransitionToHeldTargetTests(unittest.TestCase):
    """Review rebound MAJOR: a planned transition that began BEFORE the hold (handoff
    requested on fire N, hold set, handoff written, fire N+1) must not replace the
    runtime with the held target -- for both triggers, with no usability gate."""

    def _drive(self, root: Path, current, target, *, trigger: str):
        handoff = root / "HANDOFF.md"
        handoff.write_text("# old\n", encoding="utf-8")
        with mock.patch.object(rc.chronicle, "archive_required", return_value=root / "a.md"), \
                mock.patch.object(rc, "_replace_runtime") as replace, \
                mock.patch.object(rc, "_start_runtime") as start_runtime, \
                mock.patch.object(rc, "_prompt_successor"), mock.patch.object(rc, "submit_to_runtime"), \
                mock.patch.object(rc, "_route_failure_alert"):
            kwargs = dict(message_id="m1", drain_id="d1", handoff_path=handoff, state_root=root, trigger=trigger)
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.planned_swap(current, target, **kwargs)  # fire N: handoff requested
            self.assertEqual(rc.transition_state("recovery", state_root=root)["phase"], "preparing")
            _hold(root, "recovery", rc._runtime_ref(target))  # the operator holds the target
            handoff.write_text("# fresh handoff\n", encoding="utf-8")
            with self.assertRaises(rc.RuntimeTransitionAbandoned) as caught:
                rc.planned_swap(current, target, **kwargs)  # fire N+1
        replace.assert_not_called()
        start_runtime.assert_not_called()
        self.assertIn("operator-held", str(caught.exception))
        return rc.transition_state("recovery", state_root=root)

    def test_preparing_planned_quota_to_a_held_fallback_launches_nothing_and_abandons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary")
            fallback = trc._binding(root, harness="codex", profile="fallback")
            current = rc.RuntimeHandle(primary.launch, primary.controller.tmux, "s1", rc._utc_now())
            state = self._drive(root, current, fallback, trigger="planned-quota")
            evidence = trc._evidence_events(root)
        self.assertEqual(state["phase"], "abandoned")
        self.assertEqual(state["abandon_reason"], "operator-hold")
        self.assertFalse(state["runtime_swapped"])
        self.assertIn("planned_swap_abandoned", evidence)
        self.assertNotIn("runtime_replaced", evidence)

    def test_preparing_quota_reset_to_a_held_primary_launches_nothing_and_abandons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            fallback = trc._binding(root, harness="codex", profile="fallback")
            completed = {
                "schema": "pantheon-runtime-transition/v1", "transition_id": "t0", "phase": "completed",
                "fallback_active": True, "runtime_swapped": True, "trigger": "planned-quota",
                "outgoing_harness": "claude", "outgoing_profile": "primary",
                "incoming_harness": "codex", "incoming_profile": "fallback",
            }
            rc._atomic_json(rc._transition_path("recovery", root), completed)
            current = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, "s1", rc._utc_now())
            state = self._drive(root, current, primary, trigger="quota-reset")
            evidence = trc._evidence_events(root)
        # The seat stays on the fallback: the completed fallback record is restored.
        self.assertEqual(state["phase"], "completed")
        self.assertTrue(state["fallback_active"])
        self.assertEqual(state["last_abandon_reason"], "operator-hold")
        self.assertIn("planned_swap_abandoned", evidence)

    def test_preparing_branch_of_the_wake_path_does_not_launch_a_held_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
            fallback = trc._binding(root, harness="codex", profile="fallback")
            mailbox.provision(primary.mailbox)
            current = rc.RuntimeHandle(primary.launch, primary.controller.tmux, "s1", rc._utc_now())
            handoff = fallback.account_home / "HANDOFF.md"
            handoff.write_text("# old\n", encoding="utf-8")
            with mock.patch.object(rc, "submit_to_runtime"), mock.patch.object(rc, "_route_failure_alert"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(current, fallback, message_id=None, drain_id="d0",
                                    trigger="planned-quota", state_root=root)
            _hold(root, "recovery", "codex/fallback")
            handoff.write_text("# fresh\n", encoding="utf-8")
            with mock.patch.object(rc, "ensure_runtime", return_value=current), \
                    mock.patch.object(rc.chronicle, "archive_required", return_value=root / "a.md"), \
                    mock.patch.object(rc, "_replace_runtime") as replace, \
                    mock.patch.object(rc, "_start_runtime") as start_runtime, \
                    mock.patch.object(rc, "_route_failure_alert"):
                with self.assertRaises(rc.RuntimeTransitionAbandoned):
                    persona_bringup.wake_persistent_runtime(
                        primary, adapter=trc._Adapter(primary), fallback_binding=fallback,
                        primary_binding=primary, state_dir=root, cwd=root,
                    )
            state = rc.transition_state("recovery", state_root=root)
        replace.assert_not_called()
        start_runtime.assert_not_called()
        self.assertEqual(state["phase"], "abandoned")

    def test_unheld_preparing_target_still_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary")
            fallback = trc._binding(root, harness="codex", profile="fallback")
            _hold(root, "recovery", "claude/primary")  # an unrelated hold
            current = rc.RuntimeHandle(primary.launch, primary.controller.tmux, "s1", rc._utc_now())
            handoff = root / "HANDOFF.md"
            handoff.write_text("# old\n", encoding="utf-8")
            with mock.patch.object(rc.chronicle, "archive_required", return_value=root / "a.md"), \
                    mock.patch.object(rc, "_replace_runtime") as replace, \
                    mock.patch.object(rc, "_prompt_successor"), mock.patch.object(rc, "submit_to_runtime"):
                kwargs = dict(message_id="m1", drain_id="d1", handoff_path=handoff, state_root=root,
                              trigger="planned-quota")
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(current, fallback, **kwargs)
                handoff.write_text("# fresh handoff\n", encoding="utf-8")
                with self.assertRaises(rc.RuntimeTransitionPending) as caught:
                    rc.planned_swap(current, fallback, **kwargs)
        self.assertNotIsInstance(caught.exception, rc.RuntimeTransitionAbandoned)
        replace.assert_called_once()


class HeldEvidenceDedupeTests(unittest.TestCase):
    """Review rebound MINOR 4: the held-skip evidence rows are written once per
    hold-set change, not on every consume fire."""

    def _refuse(self, root: Path, binding) -> None:
        with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(rc, "_start_runtime"):
            with self.assertRaises(rc.RuntimeViabilityBlocked):
                rc.ensure_runtime(binding, trc._Adapter(binding), state_root=root)

    def test_refusal_evidence_once_per_hold_set_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary")
            _hold(root, "recovery", "claude/primary")
            for _fire in range(3):
                self._refuse(root, primary)
            self.assertEqual(trc._evidence_events(root).count("runtime_launch_refused_held"), 1)
            # The hold set changes (a second runtime held): log again, once.
            _hold(root, "recovery", "claude/primary", "codex/fallback")
            for _fire in range(2):
                self._refuse(root, primary)
            self.assertEqual(trc._evidence_events(root).count("runtime_launch_refused_held"), 2)

    def test_quota_reset_skip_evidence_once_across_fires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = trc._binding(root, harness="codex", profile="fallback")
            primary = trc._binding(root, harness="claude", profile="primary")
            mailbox.provision(fallback.mailbox)
            _hold(root, primary.persona, "claude/primary")
            current = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, "session-fallback", rc._utc_now())
            recovery = {"primary_profile": "claude/primary", "reset_passed_at": "2026-08-03T12:00:00Z",
                        "probe_observed_at": "2026-08-03T12:01:00Z"}
            selection = rc.RuntimeViabilitySelection(fallback, trc._Adapter(fallback), current, None)
            with mock.patch.object(rc, "ensure_viable_runtime", return_value=selection), \
                    mock.patch.object(rc, "planned_swap") as planned:
                for _fire in range(3):
                    persona_bringup.wake_persistent_runtime(
                        fallback, adapter=trc._Adapter(fallback), primary_binding=primary,
                        recovery_evidence=recovery, state_dir=root, cwd=root,
                    )
            events = trc._evidence_events(root)
        planned.assert_not_called()
        self.assertEqual(events.count("quota_reset_return_skipped_held"), 1)


class RunCommandHeldRuntimeTests(unittest.TestCase):
    """Review rebound MINOR 3: `persona_bringup run` on a held runtime is a soft
    policy skip (exit 5, one stderr line), not a traceback."""

    def _run(self, root: Path, refusal: Exception):
        import contextlib
        import io

        binding = trc._binding(root)
        report = trc.DiagnosticReport(harness="codex", execution_context="test", observations=())
        err = io.StringIO()
        with mock.patch.object(persona_bringup, "load_binding", return_value=binding), mock.patch.object(
            persona_bringup, "diagnose_persona", return_value=report
        ), mock.patch.object(persona_bringup, "build_adapter", return_value=trc._Adapter(binding)), mock.patch.object(
            rc, "ensure_runtime", side_effect=refusal
        ), mock.patch.object(persona_bringup, "orientation_prompt", return_value="hello"), mock.patch.object(
            rc, "submit_to_runtime"
        ) as submit, contextlib.redirect_stderr(err):
            code = persona_bringup.main(
                ["run", "--persona", "recovery", "--state-dir", str(root / "state"),
                 "--mailbox-hub", str(root / "mail"), "--prompt", "hello"]
            )
        submit.assert_not_called()
        return code, err.getvalue()

    def test_held_runtime_exits_5_with_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refusal = rc.RuntimeViabilityBlocked("runtime-held", {}, policy_skip_reason="runtime_held")
            code, err = self._run(Path(tmp), refusal)
        self.assertEqual(code, 5)
        self.assertEqual(err.strip(), "runtime codex/interactive held by operator")

    def test_deep_sleep_exits_5_with_one_line(self) -> None:
        # ORG-106 L3a: a deep-sleeping seat is a soft policy skip too, not a traceback.
        with tempfile.TemporaryDirectory() as tmp:
            refusal = rc.RuntimeViabilityBlocked("seat-sleep", {}, policy_skip_reason="seat_sleep")
            code, err = self._run(Path(tmp), refusal)
        self.assertEqual(code, 5)
        self.assertEqual(err.strip(), "seat is in deep-sleep")

    def test_other_viability_blocks_still_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refusal = rc.RuntimeViabilityBlocked("recorded-scope-still-active", {})
            with self.assertRaises(rc.RuntimeViabilityBlocked):
                self._run(Path(tmp), refusal)


class ControllerPlaneHoldTests(unittest.TestCase):
    """The controller already honoured holds; pin it for every hold set."""

    def _start_recorder(self, started: list[str]):
        def start(binding):
            started.append(rc._binding_runtime_ref(binding))
            return (
                rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()),
            )

        return start

    def test_held_runtime_is_never_passed_to_start(self) -> None:
        for held in ((), ("claude/primary",), ("codex/fallback",)):
            with self.subTest(held=held), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                claude = trc._binding(root, harness="claude", profile="primary")
                codex = trc._binding(root, harness="codex", profile="fallback")
                if held:
                    _hold(root, "recovery", *held)
                started: list[str] = []
                rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root,
                    sleep=lambda s: None, start_runtime_fn=self._start_recorder(started),
                    block_until_death_fn=lambda binding: None,
                    persist_record_fn=lambda *a, **k: "persisted",
                )
                self.assertEqual(len(started), 1)
                self.assertNotIn(started[0], held)

    def test_held_operator_destination_is_never_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = trc._binding(root, harness="claude", profile="primary")
            codex = trc._binding(root, harness="codex", profile="fallback")
            trc._write_operator_transition(root, "codex/fallback")
            _hold(root, "recovery", "codex/fallback")
            started: list[str] = []
            with mock.patch.object(rc, "_pane_runs_binding", return_value=True):
                rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root,
                    sleep=lambda s: None, start_runtime_fn=self._start_recorder(started),
                    block_until_death_fn=lambda binding: None,
                    persist_record_fn=lambda *a, **k: "persisted",
                )
        self.assertEqual(started, ["claude/primary"])


class ControllerAllHeldSleepTests(unittest.TestCase):
    """T-5021 (separate commit): every candidate held -> sleep in-process with a
    journal WARN each cycle, never exit into a Restart=always loop."""

    def _bindings(self, root: Path):
        return (
            trc._binding(root, harness="claude", profile="primary"),
            trc._binding(root, harness="codex", profile="fallback"),
        )

    def test_all_held_sleeps_with_a_warn_each_cycle_and_rereads_holds(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            _hold(root, "recovery", "claude/primary", "codex/fallback")
            sleeps: list[float] = []
            started: list[str] = []

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 2:  # an operator clears one hold mid-wait
                    _hold(root, "recovery", "claude/primary")

            def start(binding):
                started.append(rc._binding_runtime_ref(binding))
                return (
                    rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                    rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()),
                )

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root, sleep=sleep,
                    start_runtime_fn=start, block_until_death_fn=lambda binding: None,
                    persist_record_fn=lambda *a, **k: "persisted", max_reselect_cycles=5,
                )
        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [rc.ALL_HELD_RECHECK_SECONDS] * 2)
        self.assertEqual(started, ["codex/fallback"])
        warn = "[runtime_controller] recovery: WARN all candidates held (claude/primary, codex/fallback); sleeping 60s"
        self.assertEqual(err.getvalue().splitlines().count(warn), 2)

    def test_all_held_bounded_loop_still_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            _hold(root, "recovery", "claude/primary", "codex/fallback")
            sleeps: list[float] = []
            result = rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root, sleep=sleeps.append,
                start_runtime_fn=lambda b: (_ for _ in ()).throw(AssertionError("held runtime started")),
                max_reselect_cycles=1,
            )
        self.assertEqual(result, 1)
        self.assertEqual(sleeps, [rc.ALL_HELD_RECHECK_SECONDS])

    def test_no_candidates_still_exits_one_without_warn(self) -> None:
        """The all-held branch needs at least one candidate, every one held; any
        other no-viable/no-expiry shape keeps the old exit 1."""
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sleeps: list[float] = []
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = rc.controller_main(
                    "recovery", candidates=[], state_root=root, sleep=sleeps.append,
                    start_runtime_fn=lambda b: (_ for _ in ()).throw(AssertionError("must not start")),
                )
        self.assertEqual(result, 1)
        self.assertEqual(sleeps, [])
        self.assertNotIn("WARN all candidates held", err.getvalue())

    def test_runtime_blocked_record_blocks_until_cleared(self) -> None:
        """T-5028 (d) replaces the T-5021 pin: runtime_blocked (cooldown_until None
        by design) is NOT selectable until `harness clear-cooldown`. Held claude +
        blocked codex: nothing starts and the controller takes the all-held sleep."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            _hold(root, "recovery", "claude/primary")
            rc.record_last_attempt("recovery", codex, outcome="failed", failure_class="runtime_blocked", state_root=root)
            sleeps: list[float] = []
            result = rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root, sleep=sleeps.append,
                start_runtime_fn=lambda b: (_ for _ in ()).throw(AssertionError("blocked runtime started")),
                max_reselect_cycles=1,
            )
        self.assertEqual(result, 1)
        self.assertEqual(sleeps, [rc.ALL_HELD_RECHECK_SECONDS])

if __name__ == "__main__":
    unittest.main()
