"""T-5028 MR-1: holds fail CLOSED on a malformed holds.json (e), consume selection
skips a held recorded runtime (g), and runtime_blocked blocks until cleared (d).

(e) load_holds returned {} for a malformed overrides/holds.json on both planes, so
held runtimes became selectable. It now returns a fail-closed view in which EVERY
runtime is held, through every access path, and alerts Recovery once per cooldown.
Each call site that reads holds is pinned below with a malformed file.

Helpers are borrowed (not inherited) from test_runtime_controller and the T-5021
test module so their own tests are not collected twice. tmp_path stand-ins only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import harness
from tools import harness_state as hs
from tools import mailbox
from tools import persona_bringup
from tools import runtime_controller as rc
try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover
    import _seat_state_isolation  # type: ignore[no-redef]
from tools import subscription_usability
from tools import test_runtime_controller as trc
from tools import test_t5021_runtime_hold_both_planes as t5021

KEY = "<all:holds-malformed>"  # pinned literal: rc.HOLDS_MALFORMED_KEY

#: name -> writer(path). Every shape that exists on disk but is not a holds document.
MALFORMED_SHAPES = {
    "invalid-json": lambda p: p.write_text('{"holds": {"claude/primary": ', encoding="utf-8"),
    "empty-file": lambda p: p.write_text("", encoding="utf-8"),
    "top-level-list": lambda p: p.write_text('["claude/primary"]', encoding="utf-8"),
    "missing-holds": lambda p: p.write_text('{"schema": "pantheon-persona-runtime-holds/v1"}', encoding="utf-8"),
    "holds-not-object": lambda p: p.write_text('{"holds": ["claude/primary"]}', encoding="utf-8"),
    "is-a-directory": lambda p: p.mkdir(),
}


def _malform(root: Path, persona: str = "recovery", shape: str = "invalid-json") -> Path:
    path = rc._holds_path(persona, state_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    MALFORMED_SHAPES[shape](path)
    return path


# T-6118: plain `python3 -m unittest` never loads tools/conftest.py; without this the controller
# lifecycle tests wrote the REAL Chronicle / persona state. See tools/_seat_state_isolation.py.
def setUpModule() -> None:
    _seat_state_isolation.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()


@contextlib.contextmanager
def _no_alert():
    with mock.patch.object(rc, "_route_failure_alert") as routed:
        yield routed


class FailClosedViewTests(unittest.TestCase):
    """Condition 1: 'held' through EVERY access path, for every malformed shape."""

    def _assert_all_held(self, view) -> None:
        for ref in ("claude/primary", "codex/fallback", "anything/else"):
            self.assertIn(ref, view)
            self.assertFalse(ref not in view)
            self.assertEqual(view[ref]["by"], "fail-closed")
            self.assertTrue(view[ref]["reason"].startswith("holds-malformed: "))
            self.assertIsNotNone(view.get(ref))
            self.assertTrue(view.get(ref, {}))
            self.assertEqual(view.get(ref, None)["by"], "fail-closed")
        self.assertTrue(bool(view))
        self.assertEqual(len(view), 1)
        self.assertEqual(list(view.keys()), [KEY])
        self.assertEqual([k for k, _v in view.items()], [KEY])
        self.assertEqual(sorted(view), [KEY])
        self.assertTrue(view or {})  # the `(holds or {})` shape keeps the view

    def test_every_malformed_shape_fails_closed_on_every_access_path(self) -> None:
        for shape in MALFORMED_SHAPES:
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp, _no_alert():
                root = Path(tmp)
                _malform(root, shape=shape)
                self._assert_all_held(rc.load_holds("recovery", state_root=root))

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_unreadable_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            path = rc._holds_path("recovery", state_root=root)
            path.parent.mkdir(parents=True)
            path.write_text('{"holds": {}}', encoding="utf-8")
            path.chmod(0)
            try:
                view = rc.load_holds("recovery", state_root=root)
            finally:
                path.chmod(0o600)
        self._assert_all_held(view)
        self.assertIn("unreadable", view[KEY]["reason"])

    def test_malformed_key_constant_is_pinned(self) -> None:
        self.assertEqual(rc.HOLDS_MALFORMED_KEY, KEY)

    def test_absent_file_is_still_no_holds_and_never_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert() as routed:
            holds = rc.load_holds("recovery", state_root=Path(tmp))
        self.assertEqual(holds, {})
        self.assertNotIn("claude/primary", holds)
        routed.assert_not_called()

    def test_well_formed_file_is_a_plain_dict_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert() as routed:
            root = Path(tmp)
            t5021._hold(root, "recovery", "claude/primary")
            holds = rc.load_holds("recovery", state_root=root)
        self.assertIs(type(holds), dict)
        self.assertIn("claude/primary", holds)
        self.assertNotIn("codex/fallback", holds)
        routed.assert_not_called()

    def test_alert_routes_once_then_the_stamp_suppresses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert() as routed:
            root = Path(tmp)
            path = _malform(root)
            for _fire in range(3):
                rc.load_holds("recovery", state_root=root)
        routed.assert_called_once()
        agent, msg, vocab = routed.call_args.args
        self.assertEqual(agent, "recovery")
        self.assertIn(str(path), msg)
        self.assertIn("treated as operator-held", msg)
        self.assertEqual(vocab, "holds-malformed (recovery)")

    def test_an_alert_failure_never_breaks_the_fail_closed_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            rc, "_route_failure_alert", side_effect=RuntimeError("hub down")
        ):
            root = Path(tmp)
            _malform(root)
            view = rc.load_holds("recovery", state_root=root)
        self.assertIn("claude/primary", view)


class ControllerPlaneCallSiteTests(unittest.TestCase):
    """Each controller-plane holds reader, with a malformed holds.json."""

    def _bindings(self, root: Path):
        return (
            trc._binding(root, harness="claude", profile="primary"),
            trc._binding(root, harness="codex", profile="fallback"),
        )

    def test_select_viable_runtime_selects_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude, codex = self._bindings(root)
            _malform(root)
            holds = rc.load_holds("recovery", state_root=root)
            selected, skipped = rc._select_viable_runtime("recovery", [claude, codex], holds, rc._utc_now(), state_root=root)
        self.assertIsNone(selected)
        self.assertEqual([row["reason"] for row in skipped], ["held", "held"])

    def test_earliest_cooldown_expiry_ignores_every_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude, codex = self._bindings(root)
            rc.record_last_attempt("recovery", codex, outcome="failed", failure_class="process_death", state_root=root)
            _malform(root)
            holds = rc.load_holds("recovery", state_root=root)
            earliest = rc._earliest_cooldown_expiry("recovery", [claude, codex], holds, state_root=root)
        self.assertIsNone(earliest)

    def test_controller_main_takes_the_all_held_sleep_and_starts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert() as routed:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            _malform(root)
            sleeps: list[float] = []
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root, sleep=sleeps.append,
                    start_runtime_fn=lambda b: (_ for _ in ()).throw(AssertionError("launched under malformed holds")),
                    max_reselect_cycles=1,
                )
        self.assertEqual(result, 1)
        self.assertEqual(sleeps, [rc.ALL_HELD_RECHECK_SECONDS])
        self.assertIn("WARN all candidates held", err.getvalue())
        routed.assert_called_once()

    def test_dbus_health_reports_all_held_and_the_malformed_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude, codex = self._bindings(root)
            _malform(root)
            props = rc.dbus_health_properties("recovery", candidates=[claude, codex], state_root=root)
        self.assertEqual(props["ViabilityStatus"], "all-held")
        self.assertEqual(props["Holds"], [KEY])

    def test_operator_pinned_runtime_never_pins_under_malformed_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude, codex = self._bindings(root)
            trc._write_operator_transition(root, "codex/fallback")
            _malform(root)
            holds = rc.load_holds("recovery", state_root=root)
            with mock.patch.object(rc, "_pane_runs_binding", return_value=True):
                pinned = rc._operator_pinned_runtime("recovery", [claude, codex], holds=holds, state_root=root)
                control = rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root)
        self.assertIsNone(pinned)
        self.assertIsNotNone(control)  # the fixture does pin when nothing is held

    def test_mid_session_cap_swap_is_not_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude = trc._stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            codex = trc._stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            control = rc._mid_session_cap_swap_eligible(
                claude, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
            )
            _malform(root, persona=claude.persona)
            eligible = rc._mid_session_cap_swap_eligible(
                claude, candidates=[claude, codex], holds=None, now=rc._utc_now(), state_root=root
            )
        self.assertTrue(control)
        self.assertFalse(eligible)

    def test_fallback_return_is_not_eligible(self) -> None:
        """No edit in _fallback_return_eligible (T-5069 owns it); it reads load_holds,
        so it inherits the fail-closed view."""
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude = trc._stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = trc._stateless_binding(root, harness="codex", profile="interactive-chatgpt", fallback_return=True)
            control = rc._fallback_return_eligible(
                codex, candidates=[claude, codex], holds={}, now=rc._utc_now(), state_root=root
            )
            _malform(root, persona=codex.persona)
            target = rc._fallback_return_eligible(
                codex, candidates=[claude, codex], holds=None, now=rc._utc_now(), state_root=root
            )
        self.assertIsNotNone(control)
        self.assertIsNone(target)

    def test_planned_swap_abandons_a_target_under_malformed_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary")
            fallback = trc._binding(root, harness="codex", profile="fallback")
            current = rc.RuntimeHandle(primary.launch, primary.controller.tmux, "s1", rc._utc_now())
            handoff = root / "HANDOFF.md"
            handoff.write_text("# old\n", encoding="utf-8")
            with mock.patch.object(rc.chronicle, "archive_required", return_value=root / "a.md"), \
                    mock.patch.object(rc, "_replace_runtime") as replace, \
                    mock.patch.object(rc, "_start_runtime") as start_runtime, \
                    mock.patch.object(rc, "_prompt_successor"), mock.patch.object(rc, "submit_to_runtime"), \
                    mock.patch.object(rc, "_route_failure_alert"):
                kwargs = dict(message_id="m1", drain_id="d1", handoff_path=handoff, state_root=root,
                              trigger="planned-quota")
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(current, fallback, **kwargs)
                _malform(root)
                handoff.write_text("# fresh\n", encoding="utf-8")
                with self.assertRaises(rc.RuntimeTransitionAbandoned):
                    rc.planned_swap(current, fallback, **kwargs)
            state = rc.transition_state("recovery", state_root=root)
        replace.assert_not_called()
        start_runtime.assert_not_called()
        self.assertEqual(state["abandon_reason"], "operator-hold")


class ConsumePlaneCallSiteTests(unittest.TestCase):
    _bindings = trc.InitialViabilityTests._bindings
    _run_selector = trc.InitialViabilityTests._run_selector
    _selector_patches = trc.InitialViabilityTests._selector_patches
    _write_stale_codex_runtime_state = trc.InitialViabilityTests._write_stale_codex_runtime_state
    _write_stale_selected_codex_viability = trc.InitialViabilityTests._write_stale_selected_codex_viability

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("INVOCATION_ID", None)

    def test_selection_loop_launches_nothing_and_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            _malform(root, persona=primary.persona)
            events: list[str] = []
            with self.assertRaises(rc.RuntimeViabilityExhausted):
                self._run_selector(root, primary, fallback, events=events)
            state = rc.initial_viability_state(primary.persona, state_root=root)
        self.assertFalse([e for e in events if e.endswith(":start")])
        self.assertEqual([a["status"] for a in state["attempts"]], ["skipped-held", "skipped-held"])

    def test_ensure_runtime_refuses_the_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            primary, _fallback = self._bindings(root)
            _malform(root, persona=primary.persona)
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime"
            ) as start_runtime, mock.patch.object(rc, "_stop_prior_harness_receiver_before_start"):
                with self.assertRaises(rc.RuntimeViabilityBlocked) as caught:
                    rc.ensure_runtime(primary, trc._Adapter(primary), state_root=root)
        start_runtime.assert_not_called()
        self.assertEqual(caught.exception.reason, "runtime-held")

    def test_recorded_runtime_behind_a_dead_pane_is_not_rehydrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            _malform(root, persona=primary.persona)
            with mock.patch.object(rc, "_pane_process_pid", return_value=None), mock.patch.object(
                rc, "pane_exists", return_value=False
            ), mock.patch.object(rc, "_rehydrate_recorded_runtime_selection") as rehydrate:
                result = rc._matching_recorded_runtime_selection(
                    (primary, fallback),
                    adapter_factory=lambda binding: trc._Adapter(binding),
                    state_root=root, backend=None, context=None,
                    now=datetime(2026, 10, 17, tzinfo=timezone.utc),
                    manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
                    attempt_limit=2,
                )
        self.assertIsNone(result)
        rehydrate.assert_not_called()

    def test_quota_reset_return_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            fallback = trc._binding(root, harness="codex", profile="fallback")
            primary = trc._binding(root, harness="claude", profile="primary")
            mailbox.provision(fallback.mailbox)
            # Hold claude only while selection runs (so codex is selected and reused),
            # then the file turns malformed before the quota-reset return reads it.
            t5021._hold(root, primary.persona, "claude/primary")
            current = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, "session-fallback", rc._utc_now())

            def ensure(selected, adapter, *, state_root=None):
                handle = trc._record_handle_runtime_state(selected, current, Path(state_root))
                rc._holds_path(primary.persona, state_root=root).unlink()
                _malform(root, persona=primary.persona)
                return handle

            recovery = {
                "primary_profile": "claude/primary",
                "reset_passed_at": "2026-08-03T12:00:00Z",
                "probe_observed_at": "2026-08-03T12:01:00Z",
            }
            with mock.patch.object(rc, "ensure_runtime", side_effect=ensure), mock.patch.object(
                rc, "planned_swap"
            ) as planned, trc._provenance_patch():
                result = persona_bringup.wake_persistent_runtime(
                    fallback, adapter=trc._Adapter(fallback), primary_binding=primary,
                    recovery_evidence=recovery, state_dir=root, cwd=root,
                )
            evidence = trc._evidence_events(root)
        planned.assert_not_called()
        self.assertEqual(result["harness"], "codex")
        self.assertIn("quota_reset_return_skipped_held", evidence)

    def test_held_evidence_dedupe_still_logs_once_under_the_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            primary = trc._binding(root, harness="claude", profile="primary")
            _malform(root)
            for _fire in range(3):
                with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(rc, "_start_runtime"):
                    with self.assertRaises(rc.RuntimeViabilityBlocked):
                        rc.ensure_runtime(primary, trc._Adapter(primary), state_root=root)
            events = trc._evidence_events(root)
        self.assertEqual(events.count("runtime_launch_refused_held"), 1)


class SubscriptionUsabilityCallSiteTests(unittest.TestCase):
    def test_rank_and_select_chooses_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            claude = trc._binding(root, harness="claude", profile="primary")
            codex = trc._binding(root, harness="codex", profile="fallback")
            _malform(root)
            holds = rc.load_holds("recovery", state_root=root)
            decision = subscription_usability.rank_and_select(
                [claude, codex], {}, holds=holds, primary_confirmed_capped=True
            )
        self.assertIsNone(decision.chosen)

    def test_select_usable_fallback_probes_and_chooses_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _no_alert():
            root = Path(tmp)
            codex = trc._binding(root, harness="codex", profile="fallback")
            _malform(root)
            with mock.patch.object(subscription_usability, "probe_usability") as probe:
                decision, probes = subscription_usability.select_usable_fallback(
                    "recovery", [codex], state_root=root, now=rc._utc_now(),
                    primary_confirmed_capped=True, allow_spend=False,
                )
        probe.assert_not_called()
        self.assertEqual(probes, {})
        self.assertIsNone(decision.chosen)


class HarnessCliMalformedHoldsTests(unittest.TestCase):
    """harness_state raises on a malformed file; status must say so, not crash."""

    def _status(self, root: Path, persona: str = "recovery") -> tuple[int, str]:
        args = harness.build_parser().parse_args([persona, "status"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = harness._do_status(args, root=root, pane_probe=lambda bindings: None)
        return code, out.getvalue()

    def test_status_reports_malformed_and_marks_every_runtime_held(self) -> None:
        for shape in MALFORMED_SHAPES:
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                personas = root / "personas"
                binding = trc._binding(personas, harness="codex", profile="fallback")
                rc.record_last_attempt("recovery", binding, outcome="success", state_root=personas)
                _malform(personas, shape=shape)
                code, out = self._status(root)
                self.assertEqual(code, 0)
                self.assertIn("holds: MALFORMED", out)
                self.assertIn("EVERY runtime as held", out)
                self.assertIn("codex/fallback: outcome=success", out)
                self.assertIn("[held]", out)

    def test_hold_on_a_malformed_file_refuses_with_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger"
            ledger.mkdir()
            _malform(root / "personas")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = harness.main(
                    ["recovery", "hold", "claude/primary", "--reason", "x"], root=root, ledger_dir=ledger
                )
        self.assertEqual(code, 2)
        self.assertIn("malformed JSON", err.getvalue())


class RecordedSelectionHeldTests(unittest.TestCase):
    """(g) specialist 2026-10-15: initial-viability recorded claude, the operator then held
    it and the live pane runs codex. Consume blocked every fire with
    runtime-state-missing-or-mismatched against the HELD runtime."""

    _bindings = trc.InitialViabilityTests._bindings
    _reuse_patches = trc.InitialViabilityTests._reuse_patches
    _write_stale_codex_runtime_state = trc.InitialViabilityTests._write_stale_codex_runtime_state
    _write_stale_selected_codex_viability = trc.InitialViabilityTests._write_stale_selected_codex_viability

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("INVOCATION_ID", None)

    def _recorded_claude(self, root: Path, primary, fallback, *, codex_runtime_json: bool):
        recorded = trc._fixture_source_provenance(
            runtime_start_boundary_at=datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
            runtime_start_observed_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
        )
        self._write_stale_codex_runtime_state(root, fallback, source_provenance=recorded)
        self._write_stale_selected_codex_viability(root, primary, fallback)
        path = rc._initial_viability_path(primary.persona, root)
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(
            selected_runtime=rc._runtime_ref(primary),
            selected_harness=primary.launch.harness,
            selected_profile=primary.launch.profile,
        )
        rc._atomic_json(path, state)
        if not codex_runtime_json:
            rc._atomic_json(rc._persona_state(primary.persona, root) / "runtime.json", {})
        return recorded

    def _ensure(self, root, primary, fallback, recorded, events, pane_runs):
        pane, pid, scope, start = self._reuse_patches(events)
        with pane, pid, scope, start as start_runtime, trc._provenance_patch_with(
            source_hashes=recorded.source_hashes,
        ), mock.patch.object(rc, "_pane_runs_binding", side_effect=trc._pane_runs(pane_runs)):
            try:
                return rc.ensure_viable_runtime(
                    (primary, fallback),
                    adapter_factory=lambda binding: trc._Adapter(binding),
                    state_root=root,
                    backend=trc._ViabilityBackend(events),
                ), start_runtime
            except rc.RuntimeViabilityBlocked as exc:
                return exc, start_runtime

    def test_held_recorded_selection_reanchors_onto_the_live_codex_and_reuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            recorded = self._recorded_claude(root, primary, fallback, codex_runtime_json=True)
            t5021._hold(root, primary.persona, rc._runtime_ref(primary))
            events: list[str] = []
            reused, start_runtime = self._ensure(root, primary, fallback, recorded, events, "codex")
            evidence = trc._evidence_events(root)
        start_runtime.assert_not_called()
        self.assertIsInstance(reused, rc.RuntimeViabilitySelection)
        self.assertEqual(reused.receipt.status, "reused-recorded")
        self.assertIs(reused.binding, fallback)
        self.assertIn("recorded_selection_held_reanchored", evidence)
        self.assertNotIn("runtime_viability_blocked", evidence)

    def test_missing_runtime_json_blocks_against_codex_with_the_expected_token(self) -> None:
        """The specialist shape: the controller launched codex, so runtime.json is empty.
        The block (still the expected runtime_viability_blocked token, so fleet_health
        keeps specialist expected) now names the runtime the pane runs, not the held one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            recorded = self._recorded_claude(root, primary, fallback, codex_runtime_json=False)
            t5021._hold(root, primary.persona, rc._runtime_ref(primary))
            events: list[str] = []
            block, start_runtime = self._ensure(root, primary, fallback, recorded, events, "codex")
        start_runtime.assert_not_called()
        self.assertIsInstance(block, rc.RuntimeViabilityBlocked)
        self.assertEqual(block.reason, "runtime-state-missing-or-mismatched")
        self.assertEqual(block.policy_skip_reason, "runtime_viability_blocked")
        self.assertEqual(block.evidence["selected_runtime"], rc._runtime_ref(fallback))

    def test_unheld_recorded_selection_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            recorded = self._recorded_claude(root, primary, fallback, codex_runtime_json=True)
            events: list[str] = []
            block, start_runtime = self._ensure(root, primary, fallback, recorded, events, "codex")
            evidence = trc._evidence_events(root)
        start_runtime.assert_not_called()
        self.assertIsInstance(block, rc.RuntimeViabilityBlocked)
        self.assertEqual(block.evidence["selected_runtime"], rc._runtime_ref(primary))
        self.assertNotIn("recorded_selection_held_reanchored", evidence)

    def test_pane_running_no_unheld_candidate_keeps_the_record(self) -> None:
        """Never a guess: an unreadable or held pane does not re-anchor."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            recorded = self._recorded_claude(root, primary, fallback, codex_runtime_json=True)
            t5021._hold(root, primary.persona, rc._runtime_ref(primary))
            events: list[str] = []
            block, _start = self._ensure(root, primary, fallback, recorded, events, "claude")
            evidence = trc._evidence_events(root)
        self.assertIsInstance(block, rc.RuntimeViabilityBlocked)
        self.assertEqual(block.evidence["selected_runtime"], rc._runtime_ref(primary))
        self.assertNotIn("recorded_selection_held_reanchored", evidence)


class RuntimeBlockedTests(unittest.TestCase):
    """(d) runtime_blocked blocks until `harness clear-cooldown` clears it."""

    def _bindings(self, root: Path):
        return (
            trc._binding(root, harness="claude", profile="primary"),
            trc._binding(root, harness="codex", profile="fallback"),
        )

    def test_select_viable_runtime_skips_a_blocked_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            rc.record_last_attempt("recovery", claude, outcome="failed", failure_class="runtime_blocked", state_root=root)
            far_future = rc._utc_now() + timedelta(days=3650)
            selected, skipped = rc._select_viable_runtime("recovery", [claude, codex], {}, far_future, state_root=root)
        self.assertIs(selected, codex)
        self.assertEqual(skipped, [{"runtime": "claude/primary", "reason": "runtime-blocked"}])

    def test_every_candidate_blocked_selects_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            for binding in (claude, codex):
                rc.record_last_attempt("recovery", binding, outcome="failed", failure_class="runtime_blocked", state_root=root)
            selected, _skipped = rc._select_viable_runtime("recovery", [claude, codex], {}, rc._utc_now(), state_root=root)
        self.assertIsNone(selected)

    def test_clear_cooldown_makes_a_blocked_runtime_selectable_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            personas = root / "personas"
            claude, codex = self._bindings(personas)
            rc.record_last_attempt("recovery", claude, outcome="failed", failure_class="runtime_blocked", state_root=personas)
            doc, had = hs.clear_cooldown("recovery", "claude/primary", operator="orchestrator", now_ts="2026-10-17T00:00:00Z", root=root)
            hs.write_last_attempt("recovery", "claude/primary", doc, root=root)
            selected, _skipped = rc._select_viable_runtime("recovery", [claude, codex], {}, rc._utc_now(), state_root=personas)
        self.assertTrue(had)
        self.assertIsNone(doc["failure_class"])
        self.assertEqual(doc["consecutive_failures"], 1)  # failure history is kept
        self.assertIs(selected, claude)

    def test_clear_cooldown_leaves_other_failure_classes_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, _codex = self._bindings(root / "personas")
            rc.record_last_attempt(
                "recovery", claude, outcome="failed", failure_class="process_death", state_root=root / "personas"
            )
            doc, had = hs.clear_cooldown("recovery", "claude/primary", operator="orchestrator", now_ts="2026-10-17T00:00:00Z", root=root)
        self.assertTrue(had)
        self.assertEqual(doc["failure_class"], "process_death")

    def test_controller_main_sleeps_when_every_candidate_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            for binding in (claude, codex):
                rc.record_last_attempt("recovery", binding, outcome="failed", failure_class="runtime_blocked", state_root=root)
            sleeps: list[float] = []
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = rc.controller_main(
                    "recovery", candidates=[claude, codex], state_root=root, sleep=sleeps.append,
                    start_runtime_fn=lambda b: (_ for _ in ()).throw(AssertionError("blocked runtime started")),
                    max_reselect_cycles=1,
                )
        self.assertEqual(result, 1)
        self.assertEqual(sleeps, [rc.ALL_HELD_RECHECK_SECONDS])
        self.assertIn("runtime_blocked: claude/primary, codex/fallback", err.getvalue())

    def test_controller_main_starts_the_unblocked_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            rc.record_last_attempt("recovery", claude, outcome="failed", failure_class="runtime_blocked", state_root=root)
            started: list[str] = []

            def start(binding):
                started.append(rc._binding_runtime_ref(binding))
                return (
                    rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                    rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()),
                )

            result = rc.controller_main(
                "recovery", candidates=[claude, codex], state_root=root, sleep=lambda s: None,
                start_runtime_fn=start, block_until_death_fn=lambda binding: None,
                persist_record_fn=lambda *a, **k: "persisted",
            )
        self.assertEqual(result, 0)
        self.assertEqual(started, ["codex/fallback"])

    def test_harness_status_labels_a_blocked_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, _codex = self._bindings(root / "personas")
            rc.record_last_attempt(
                "recovery", claude, outcome="failed", failure_class="runtime_blocked", state_root=root / "personas"
            )
            args = harness.build_parser().parse_args(["recovery", "status"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                harness._do_status(args, root=root, pane_probe=lambda bindings: None)
        self.assertIn("failure_class=runtime_blocked", out.getvalue())
        self.assertIn("[blocked]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
