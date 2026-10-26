"""T-5028 MR-2: (a) operator swap onto a held runtime is refused unless
--override-hold, which clears the hold through the harness CLI's landing path;
(b) one state-dir resolution for holds; (c) consume honours the controller's
subscription_cap and runtime_blocked records; reviewer minors m1 (no rehydrate of
a re-anchored runtime behind a dead pane) and m2 (runtime_blocked honoured by
consume, rank_and_select and the operator pin).

Helpers are borrowed from test_runtime_controller / the T-5021 and T-5028 MR-1
modules so their own tests are not collected twice. tmp_path stand-ins only:
harness landing (_land_with_spool) is always patched, so no git runs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import harness
from tools import harness_state as hs
from tools import persona_bringup
from tools import proactive_swap_init
from tools import runtime_controller as rc
from tools import session_chronicle
from tools import subscription_usability
from tools import test_runtime_controller as trc
from tools import test_t5021_runtime_hold_both_planes as t5021


def _me() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


class OperatorSwapHoldTests(unittest.TestCase):
    """(a) the controller's operator-swap entry point (proactive_swap_init -> operator_target_swap)."""

    def _fixture(self, root: Path):
        manifest = trc._operator_manifest(root, account=_me())
        state_root = root / "personas"
        primary = persona_bringup.load_binding(manifest, "recovery", mailbox_hub=root / "mail")
        rc._atomic_json(
            state_root / "recovery" / "runtime-controller" / "runtime.json",
            rc._runtime_payload(primary, session_id="primary", started_at=rc._utc_now()),
        )
        return manifest, state_root

    def _swap(self, root, manifest, state_root, *, correlation_id="c1", **kwargs):
        with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
            rc, "_replace_runtime", side_effect=trc._replace_and_record
        ) as replace, mock.patch.object(rc, "_prompt_successor"), mock.patch.object(
            rc, "_route_failure_alert"
        ) as routed:
            try:
                receipt = persona_bringup.operator_target_swap(
                    manifest_path=manifest, persona="recovery", destination="codex/fallback",
                    trigger="operator", correlation_id=correlation_id, state_root=state_root,
                    mailbox_hub=root / "mail", **kwargs,
                )
            except Exception as exc:  # noqa: BLE001 -- returned for assertions
                receipt = exc
        return receipt, replace, routed

    def test_fresh_swap_onto_a_held_destination_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            result, replace, _routed = self._swap(root, manifest, state_root)
            phase = rc.transition_state("recovery", state_root=state_root).get("phase")
        self.assertIsInstance(result, persona_bringup.OperatorSwapHeldError)
        self.assertIn("--override-hold", str(result))
        replace.assert_not_called()
        self.assertIsNone(phase)

    def test_unheld_destination_swaps_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            receipt, replace, _routed = self._swap(root, manifest, state_root)
        self.assertEqual(receipt.status, "pending")
        replace.assert_called_once()

    def test_malformed_holds_refuse_the_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            path = rc._holds_path("recovery", state_root=state_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{", encoding="utf-8")
            result, replace, _routed = self._swap(root, manifest, state_root)
        self.assertIsInstance(result, persona_bringup.OperatorSwapHeldError)
        replace.assert_not_called()

    def test_override_clears_the_hold_through_the_harness_landing_path_then_swaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            with mock.patch.object(harness, "_land_with_spool", return_value=(True, "landed")) as land:
                receipt, replace, routed = self._swap(
                    root, manifest, state_root, override_hold=True, override_reason="drill"
                )
            holds = rc.load_holds("recovery", state_root=state_root)
            evidence = trc._read_jsonl(rc._evidence_log_path("recovery", state_root))
        self.assertEqual(receipt.status, "pending")
        replace.assert_called_once()
        self.assertNotIn("codex/fallback", holds)
        land.assert_called_once()
        entry = land.call_args.args[0]
        self.assertIn("clear-hold on recovery: codex/fallback", entry["summary"])
        self.assertIn("--override-hold (c1): drill", entry["summary"])
        self.assertEqual(land.call_args.kwargs["state_root"], root)  # the harness root, not personas/
        row = next(item for item in evidence if item["event"] == "operator_swap_hold_overridden")
        self.assertEqual((row["destination"], row["ledger"], row["clear_hold_exit"]), ("codex/fallback", "landed", 0))
        routed.assert_called_once()
        self.assertIn("--override-hold", routed.call_args.args[1])

    def test_override_with_an_unlanded_ledger_entry_still_swaps_and_says_spooled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            with mock.patch.object(harness, "_land_with_spool", return_value=(False, "offline")):
                receipt, _replace, _routed = self._swap(
                    root, manifest, state_root, override_hold=True, override_reason="drill"
                )
            evidence = trc._read_jsonl(rc._evidence_log_path("recovery", state_root))
        self.assertEqual(receipt.status, "pending")
        row = next(item for item in evidence if item["event"] == "operator_swap_hold_overridden")
        self.assertEqual((row["ledger"], row["clear_hold_exit"]), ("spooled-or-unlanded", 2))

    def test_override_that_cannot_clear_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            result, replace, _routed = self._swap(
                root, manifest, state_root, override_hold=True, override_reason="drill",
                clear_hold_fn=lambda *a, **k: (2, "harness: error: nope"),
            )
        self.assertIsInstance(result, persona_bringup.OperatorSwapHeldError)
        self.assertIn("could not clear", str(result))
        replace.assert_not_called()

    def test_override_evidence_carries_the_harness_landing_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            with mock.patch.object(harness, "_land_with_spool", return_value=(False, "fetch timed out")):
                self._swap(root, manifest, state_root, override_hold=True, override_reason="drill")
            evidence = trc._read_jsonl(rc._evidence_log_path("recovery", state_root))
        row = next(item for item in evidence if item["event"] == "operator_swap_hold_overridden")
        self.assertIn("fetch timed out", row["clear_hold_detail"])
        self.assertIn("unlanded.jsonl", row["clear_hold_detail"])

    def test_swap_failing_after_a_successful_override_leaves_a_loud_trail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            with mock.patch.object(harness, "_land_with_spool", return_value=(True, "landed")), \
                    mock.patch.object(rc, "_operator_target_swap_resolved", side_effect=RuntimeError("replace blew up")):
                result, _replace, routed = self._swap(
                    root, manifest, state_root, override_hold=True, override_reason="drill"
                )
            evidence = trc._read_jsonl(rc._evidence_log_path("recovery", state_root))
        self.assertIsInstance(result, RuntimeError)
        outcome = next(item for item in evidence if item["event"] == "operator_swap_hold_override_outcome")
        self.assertFalse(outcome["ok"])
        self.assertIn("replace blew up", outcome["error"])
        self.assertTrue(outcome["hold_still_cleared"])
        routed.assert_called_once()
        self.assertIn("SWAP FAILED", routed.call_args.args[1])
        self.assertIn("replace blew up", routed.call_args.args[1])

    def test_successful_override_outcome_row_and_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            with mock.patch.object(harness, "_land_with_spool", return_value=(True, "landed")):
                receipt, _replace, routed = self._swap(
                    root, manifest, state_root, override_hold=True, override_reason="drill"
                )
            evidence = trc._read_jsonl(rc._evidence_log_path("recovery", state_root))
        outcome = next(item for item in evidence if item["event"] == "operator_swap_hold_override_outcome")
        self.assertEqual((outcome["ok"], outcome["status"]), (True, receipt.status))
        self.assertIn("swap pending", routed.call_args.args[1])

    def test_override_on_an_unheld_destination_is_a_noop_row_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            clear = mock.Mock()
            receipt, replace, routed = self._swap(
                root, manifest, state_root, override_hold=True, override_reason="drill", clear_hold_fn=clear
            )
            events = trc._evidence_events(state_root)
            evidence = trc._read_jsonl(rc._evidence_log_path("recovery", state_root))
        self.assertEqual(receipt.status, "pending")
        replace.assert_called_once()
        clear.assert_not_called()
        routed.assert_not_called()
        self.assertIn("operator_swap_override_hold_noop", events)
        self.assertNotIn("operator_swap_hold_overridden", events)
        self.assertNotIn("operator_swap_hold_override_outcome", events)
        row = next(item for item in evidence if item["event"] == "operator_swap_override_hold_noop")
        self.assertEqual(row["detail"], "override-hold-noop: destination not held")

    def test_override_requires_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            clear = mock.Mock()
            result, _replace, _routed = self._swap(
                root, manifest, state_root, override_hold=True, clear_hold_fn=clear
            )
        self.assertIsInstance(result, persona_bringup.OperatorSwapHeldError)
        clear.assert_not_called()

    def test_retry_of_the_same_transition_is_not_gated(self) -> None:
        """A hold set after initiation does not refuse the operator's retry of the
        same correlation id (fresh initiations only)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, state_root = self._fixture(root)
            first, _replace, _routed = self._swap(root, manifest, state_root)
            t5021._hold(state_root, "recovery", "codex/fallback")
            with mock.patch.object(rc, "_validate_operator_retry_state"):
                second, replace, _routed = self._swap(root, manifest, state_root)
        self.assertEqual(first.status, "pending")
        self.assertNotIsInstance(second, Exception)
        self.assertEqual(second.status, "pending")
        replace.assert_not_called()

    def test_cli_passes_override_hold_and_reason(self) -> None:
        with mock.patch.object(proactive_swap_init.persona_bringup, "operator_target_swap") as swap, \
                contextlib.redirect_stdout(io.StringIO()):
            swap.return_value = mock.Mock()
            with mock.patch.object(proactive_swap_init.dataclasses, "asdict", return_value={}):
                code = proactive_swap_init.main(
                    ["--persona", "recovery", "--destination", "codex/fallback", "--override-hold",
                     "--reason", "drill", "--state-root", "/nonexistent/personas"]
                )
        self.assertEqual(code, 0)
        self.assertTrue(swap.call_args.kwargs["override_hold"])
        self.assertEqual(swap.call_args.kwargs["override_reason"], "drill")

    def test_cli_rejects_override_hold_on_a_recovery_mode(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            proactive_swap_init.main(["--persona", "recovery", "--recover-started-unverified", "--override-hold"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--override-hold applies to a fresh operator swap only", err.getvalue())


class StateDirResolutionTests(unittest.TestCase):
    """(b) one resolution for holds across both planes and the harness CLI."""

    MATRIX = (
        {},
        {"PANTHEON_STATE_DIR": "/x/state"},
        {"PANTHEON_PERSONA_STATE_DIR": "/y/personas"},
        {"PANTHEON_STATE_DIR": "/z", "PANTHEON_PERSONA_STATE_DIR": "/z/personas"},
    )

    def _env(self, extra):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PANTHEON_STATE_DIR", None)
        os.environ.pop("PANTHEON_PERSONA_STATE_DIR", None)
        os.environ.update(extra)

    def test_every_resolver_agrees_on_the_personas_dir(self) -> None:
        for extra in self.MATRIX:
            with self.subTest(env=extra):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("PANTHEON_STATE_DIR", None)
                    os.environ.pop("PANTHEON_PERSONA_STATE_DIR", None)
                    os.environ.update(extra)
                    controller = rc._state_root()
                    resolved = {
                        "controller": controller,
                        "chronicle": session_chronicle.persona_state_root(),
                        "swap_cli": proactive_swap_init._default_state_root(),
                        "harness": hs.state_root_from_env() / "personas",
                    }
                self.assertEqual(len({str(v) for v in resolved.values()}), 1, resolved)
                self.assertEqual(
                    rc._holds_path("recovery", state_root=controller),
                    hs.holds_path("recovery", root=resolved["harness"].parent),
                )

    def test_state_dir_alone_moves_the_controller_too(self) -> None:
        self._env({"PANTHEON_STATE_DIR": "/x/state"})
        self.assertEqual(rc._state_root(), Path("/x/state/personas"))

    def test_disagreeing_env_refuses_in_the_harness_cli_with_exit_2(self) -> None:
        self._env({"PANTHEON_STATE_DIR": "/a", "PANTHEON_PERSONA_STATE_DIR": "/b/personas"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = harness.main(["recovery", "status"])
        self.assertEqual(code, 2)
        self.assertIn("disagree", err.getvalue())
        # The runtime planes never refuse: PANTHEON_PERSONA_STATE_DIR wins there.
        self.assertEqual(rc._state_root(), Path("/b/personas"))

    def test_persona_dir_not_named_personas_refuses_in_the_cli(self) -> None:
        self._env({"PANTHEON_PERSONA_STATE_DIR": "/b/elsewhere"})
        with self.assertRaises(hs.HarnessStateError):
            hs.state_root_from_env()

    def test_harness_hold_lands_where_the_controller_reads_under_persona_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            personas = Path(tmp) / "custom" / "personas"
            self._env({"PANTHEON_PERSONA_STATE_DIR": str(personas)})
            ledger = Path(tmp) / "ledger"
            ledger.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                code = harness.main(
                    ["recovery", "hold", "claude/primary", "--reason", "x"], ledger_dir=ledger,
                    # T-5483 seat-account guard: this test runs as the current user, not the seat.
                    euid_fn=os.geteuid, account_fn=lambda persona: pwd.getpwuid(os.geteuid()).pw_name,
                )
            holds = rc.load_holds("recovery")
        self.assertEqual(code, 0)
        self.assertIn("claude/primary", holds)

    def test_root_for_personas_dir(self) -> None:
        self.assertEqual(hs.root_for_personas_dir(Path("/s/personas")), Path("/s"))
        with self.assertRaises(hs.HarnessStateError):
            hs.root_for_personas_dir(Path("/s/state"))


class ConsumeHonoursControllerCooldownTests(unittest.TestCase):
    """(c) + m2: the consume selection loop reads the controller's last-attempt.json
    for subscription_cap and runtime_blocked only."""

    _bindings = trc.InitialViabilityTests._bindings
    _run_selector = trc.InitialViabilityTests._run_selector
    _selector_patches = trc.InitialViabilityTests._selector_patches

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("INVOCATION_ID", None)

    def _attempts(self, root, primary):
        return rc.initial_viability_state(primary.persona, state_root=root)["attempts"]

    def test_controller_subscription_cap_skips_the_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            until = rc._utc_now() + timedelta(hours=3)
            rc.record_last_attempt(
                primary.persona, primary, outcome="failed", failure_class="subscription_cap",
                state_root=root, cooldown_until_override=until,
            )
            events: list[str] = []
            selection = self._run_selector(root, primary, fallback, events=events)
            first = self._attempts(root, primary)[0]
        self.assertIs(selection.binding, fallback)
        self.assertNotIn("claude:start", events)
        self.assertEqual((first["status"], first["cause"]), ("skipped-cooldown", "controller-subscription-cap"))
        self.assertEqual(first["cooldown"]["controller_until"], rc._iso(until))
        self.assertEqual(first["cooldown"]["opened"], "false")

    def test_controller_runtime_blocked_skips_the_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            rc.record_last_attempt(
                primary.persona, primary, outcome="failed", failure_class="runtime_blocked", state_root=root
            )
            events: list[str] = []
            selection = self._run_selector(root, primary, fallback, events=events)
            first = self._attempts(root, primary)[0]
        self.assertIs(selection.binding, fallback)
        self.assertEqual((first["status"], first["cause"]), ("skipped-cooldown", "controller-runtime-blocked"))

    def _clear_then_refire(self, failure_class: str) -> None:
        """REBOUND 1 MAJOR: the controller-sourced skip must not be copied into
        consume's store as a LOCAL cooldown, or it outlives `harness clear-cooldown`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "personas"
            root.mkdir()
            primary, fallback = self._bindings(root)
            kwargs = {}
            if failure_class == "subscription_cap":
                kwargs["cooldown_until_override"] = rc._utc_now() + timedelta(hours=3)
            rc.record_last_attempt(
                primary.persona, primary, outcome="failed", failure_class=failure_class, state_root=root, **kwargs
            )
            first = self._run_selector(root, primary, fallback, events=[])
            state = rc.initial_viability_state(primary.persona, state_root=root)
            skip = state["attempts"][0]
            # The operator clears it, exactly as `harness <p> clear-cooldown` does.
            doc, _had = hs.clear_cooldown(
                primary.persona, "claude/primary", operator="orchestrator", now_ts="2026-10-17T00:00:00Z", root=root.parent
            )
            hs.write_last_attempt(primary.persona, "claude/primary", doc, root=root.parent)
            # Next fire: no live pane, so the selection loop runs again.
            with mock.patch.object(rc, "_matching_recorded_runtime_selection", return_value=None):
                second = self._run_selector(root, primary, fallback, events=[])
        self.assertIs(first.binding, fallback)
        self.assertEqual(skip["status"], "skipped-cooldown")
        self.assertEqual(skip["cooldown"]["opened"], "false")
        self.assertTrue(skip["cooldown"]["controller_cause"].startswith("controller-"))
        self.assertIsNone(rc._candidate_cooldown_open(state, "claude/primary", now=rc._utc_now()))
        self.assertIs(second.binding, primary)

    def test_clear_cooldown_after_a_controller_cap_is_honoured_next_fire(self) -> None:
        self._clear_then_refire("subscription_cap")

    def test_clear_cooldown_after_runtime_blocked_is_honoured_next_fire(self) -> None:
        self._clear_then_refire("runtime_blocked")

    def test_expired_controller_cap_does_not_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            rc.record_last_attempt(
                primary.persona, primary, outcome="failed", failure_class="subscription_cap",
                state_root=root, cooldown_until_override=rc._utc_now() - timedelta(minutes=1),
            )
            selection = self._run_selector(root, primary, fallback, events=[])
        self.assertIs(selection.binding, primary)

    def test_transient_controller_backoff_stays_local_to_the_controller(self) -> None:
        for failure_class in ("process_death", "readiness_timeout", "operational_failure"):
            with self.subTest(failure_class=failure_class), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                primary, fallback = self._bindings(root)
                rc.record_last_attempt(
                    primary.persona, primary, outcome="failed", failure_class=failure_class, state_root=root
                )
                selection = self._run_selector(root, primary, fallback, events=[])
                self.assertIs(selection.binding, primary)


class RuntimeBlockedEverywhereTests(unittest.TestCase):
    """m2: rank_and_select / select_usable_fallback and the operator pin."""

    def _bindings(self, root: Path):
        return (
            trc._binding(root, harness="claude", profile="primary"),
            trc._binding(root, harness="codex", profile="fallback"),
        )

    def test_rank_and_select_never_chooses_a_blocked_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude, codex = self._bindings(Path(tmp))
            probe = mock.Mock(verdict="usable")
            decision = subscription_usability.rank_and_select(
                [claude, codex], {"claude/primary": probe, "codex/fallback": probe},
                holds={}, primary_confirmed_capped=True, blocked={"claude/primary"},
            )
        self.assertIs(decision.chosen, codex)
        self.assertEqual(decision.per_candidate["claude/primary"], "runtime-blocked")

    def test_select_usable_fallback_skips_and_never_probes_a_blocked_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _claude, codex = self._bindings(root)
            rc.record_last_attempt("recovery", codex, outcome="failed", failure_class="runtime_blocked", state_root=root)
            with mock.patch.object(subscription_usability, "probe_usability") as probe:
                decision, probes = subscription_usability.select_usable_fallback(
                    "recovery", [codex], state_root=root, now=rc._utc_now(),
                    primary_confirmed_capped=True, allow_spend=False, holds={},
                )
        probe.assert_not_called()
        self.assertEqual(probes, {})
        self.assertIsNone(decision.chosen)

    def test_operator_pin_skips_a_blocked_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._bindings(root)
            trc._write_operator_transition(root, "codex/fallback")
            with mock.patch.object(rc, "_pane_runs_binding", return_value=True):
                control = rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root)
                rc.record_last_attempt("recovery", codex, outcome="failed", failure_class="runtime_blocked", state_root=root)
                pinned = rc._operator_pinned_runtime("recovery", [claude, codex], holds={}, state_root=root)
        self.assertIs(control, codex)
        self.assertIsNone(pinned)


class ReanchorDeadPaneTests(unittest.TestCase):
    """m1: a re-anchored runtime whose pane is gone by the pane_exists check is
    never rehydrated; the selection loop (with its cooldown checks) decides."""

    _bindings = trc.InitialViabilityTests._bindings
    _write_stale_codex_runtime_state = trc.InitialViabilityTests._write_stale_codex_runtime_state
    _write_stale_selected_codex_viability = trc.InitialViabilityTests._write_stale_selected_codex_viability

    def _recorded_claude(self, root, primary, fallback):
        self._write_stale_codex_runtime_state(root, fallback)
        self._write_stale_selected_codex_viability(root, primary, fallback)
        path = rc._initial_viability_path(primary.persona, root)
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(
            selected_runtime=rc._runtime_ref(primary),
            selected_harness=primary.launch.harness,
            selected_profile=primary.launch.profile,
        )
        rc._atomic_json(path, state)

    def _match(self, root, primary, fallback):
        return rc._matching_recorded_runtime_selection(
            (primary, fallback),
            adapter_factory=lambda binding: trc._Adapter(binding),
            state_root=root, backend=None, context=None,
            now=datetime(2026, 10, 17, tzinfo=timezone.utc),
            manifest_order=(rc._runtime_ref(primary), rc._runtime_ref(fallback)),
            attempt_limit=2,
        )

    def test_reanchored_runtime_behind_a_vanished_pane_is_not_rehydrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._recorded_claude(root, primary, fallback)
            t5021._hold(root, primary.persona, rc._runtime_ref(primary))
            # Live at the re-anchor (positively runs codex), gone by the later check.
            with mock.patch.object(rc, "_pane_live_or_unknown", return_value=(True, {})), mock.patch.object(
                rc, "_pane_runs_binding", side_effect=trc._pane_runs("codex")
            ), mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_rehydrate_recorded_runtime_selection"
            ) as rehydrate:
                result = self._match(root, primary, fallback)
            evidence = trc._evidence_events(root)
        self.assertIsNone(result)
        rehydrate.assert_not_called()
        self.assertIn("recorded_selection_held_reanchored", evidence)

    def test_controller_blocked_or_capped_recorded_runtime_is_not_rehydrated(self) -> None:
        for failure_class in ("runtime_blocked", "subscription_cap"):
            with self.subTest(failure_class=failure_class), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                primary, fallback = self._bindings(root)
                self._write_stale_codex_runtime_state(root, fallback)
                self._write_stale_selected_codex_viability(root, primary, fallback)
                kwargs = {}
                if failure_class == "subscription_cap":
                    kwargs["cooldown_until_override"] = rc._utc_now() + timedelta(days=400)
                rc.record_last_attempt(
                    fallback.persona, fallback, outcome="failed", failure_class=failure_class,
                    state_root=root, **kwargs,
                )
                with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                    rc, "_rehydrate_recorded_runtime_selection", return_value="rehydrated"
                ) as rehydrate:
                    result = self._match(root, primary, fallback)
                self.assertIsNone(result)
                rehydrate.assert_not_called()

    def test_unreanchored_recorded_runtime_still_rehydrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary, fallback = self._bindings(root)
            self._write_stale_codex_runtime_state(root, fallback)
            self._write_stale_selected_codex_viability(root, primary, fallback)
            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_rehydrate_recorded_runtime_selection", return_value="rehydrated"
            ) as rehydrate:
                result = self._match(root, primary, fallback)
        self.assertEqual(result, "rehydrated")
        rehydrate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
