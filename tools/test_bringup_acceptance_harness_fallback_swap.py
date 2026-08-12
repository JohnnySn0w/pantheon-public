"""Tests for tools.bringup_acceptance.harness_fallback_swap (T-2556 phase 8 / T-2571).

Four legs, each independently exercised, plus the full declare -> prove ->
restore -> report lifecycle:

* argv-gate -- a pure guard function (0/1/2-occurrence synthetic argvs) plus
  the real ClaudeHarnessAdapter output for the implicit-default and
  explicit-override cases.
* transition-state-machine -- the REAL runtime_controller.planned_swap /
  acknowledge_successor_read / assert_drain_allowed contract, with only the
  OS-touching leaves mocked (test_runtime_controller.py's own established
  seam for _replace_runtime: _target_known/_start_runtime/
  _verify_started_runtime/_capture_visible_runtime_pane). The REAL
  _verify_runtime_input_ready polling/timeout/ready-detection loop runs
  under proof, fed a canned-but-real pane string via the mocked capture
  call. _controller_source_provenance is patched HERE (test-side only,
  mirrors test_bringup_acceptance_harness_runtime.py's own convention)
  because the real one requires a clean git worktree, which a live
  development checkout is not.
* persona-target-selection -- a hand-built completed+swapped transition
  record (independent of the transition-state-machine leg's own timing) and
  the real persona_bringup.select_recorded_runtime_binding.
* correlated-mailbox-roundtrip -- real tools.mailbox calls against a
  disposable hub, reusing test_bringup_acceptance_mailbox_roundtrip.py's own
  "real transport, disposable tree" convention.

No live production systemd/tmux server, real instance.yaml, or real mailbox
hub is ever touched by this suite.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import mailbox
from tools import persona_bringup
from tools import runtime_controller as rc
from tools.bringup_acceptance import harness_fallback_swap
from tools.bringup_acceptance.harness_fallback_swap import (
    COMPOSITE_ID,
    DEFERRED_TO_TICKET,
    MODULE_ID,
    HarnessFallbackSwapModule,
    _correlated_mailbox_roundtrip_leg,
    _fixture_manifest_text,
    _persona_target_selection_leg,
    _transition_state_machine_leg,
    assert_exactly_one_permission_mode,
    kick_lands_executes_status,
    run_live_kick_lands_executes,
)
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    RunContext,
    TargetInventory,
)
from tools.submit_verify import ready_for_submission


def _skip_unless_disposable_tmux_available(test: unittest.TestCase) -> None:
    """Self-skip cleanly when this environment can't run real tmux/systemd
    --user -- LiveKickDisposableScopeLiveTests proves the real path where it
    can, never fakes a pass where it can't."""
    if shutil.which("tmux") is None or shutil.which("systemd-run") is None:
        test.skipTest("tmux/systemd-run not available in this environment")
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if probe.stdout.strip() not in ("running", "degraded"):
        test.skipTest(
            f"no usable systemd --user session in this environment: "
            f"stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )


def _fixture_source_provenance(**kwargs: object) -> rc.RuntimeSourceProvenance:
    """Mirrors test_runtime_controller.py's own helper of the same name --
    the real _controller_source_provenance requires a clean git worktree,
    which a live development checkout is not."""
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


def _run_context(root: Path) -> RunContext:
    return RunContext(
        run_id="20260805T130000Z",
        persona="swap-acceptance-persona",
        account="claude-fallback",
        harness="claude",
        profile="interactive-subscription",
        hub_path=root / "hub",
        state_path=root / "state",
        evidence_root=root / "evidence",
        baseline_ref="deadbeef" * 5,
    )


def _load_bindings(root: Path, ctx: RunContext) -> tuple[Path, "persona_bringup.PersonaBinding", "persona_bringup.PersonaBinding"]:
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
    def test_applies_when_inventory_marks_fallback_swap_present(self) -> None:
        module = HarnessFallbackSwapModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("fallback:codex/interactive-chatgpt"), tier="core",
                    status="applies", module_id=MODULE_ID,
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertTrue(decl.applies)
        self.assertEqual(decl.tier, "core")
        self.assertIn(COMPOSITE_ID, decl.contributes_to)

    def test_does_not_apply_when_no_fallback_configured(self) -> None:
        module = HarnessFallbackSwapModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("fallback:recovery:none-configured"), tier="core",
                    status="absent", module_id=MODULE_ID,
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)


# ------------------------------------------------------------------- argv gate


class ArgvGateGuardTest(unittest.TestCase):
    """The guard function itself, not just today's adapter output -- proves
    the "exactly once, not zero/twice" contract against contrived argvs."""

    def test_exactly_one_passes(self) -> None:
        index = assert_exactly_one_permission_mode(
            ("claude", "--permission-mode", "default"), expected_value="default", label="t",
        )
        self.assertEqual(index, 1)

    def test_zero_occurrences_raises(self) -> None:
        with self.assertRaises(HardStop):
            assert_exactly_one_permission_mode(("claude",), expected_value="default", label="t")

    def test_two_occurrences_raises(self) -> None:
        with self.assertRaises(HardStop):
            assert_exactly_one_permission_mode(
                ("claude", "--permission-mode", "default", "--permission-mode", "default"),
                expected_value="default", label="t",
            )

    def test_wrong_value_raises(self) -> None:
        with self.assertRaises(HardStop):
            assert_exactly_one_permission_mode(
                ("claude", "--permission-mode", "acceptEdits"),
                expected_value="default", label="t",
            )

    def test_dangling_flag_with_no_value_raises(self) -> None:
        with self.assertRaises(HardStop):
            assert_exactly_one_permission_mode(
                ("claude", "--permission-mode"), expected_value="default", label="t",
            )


class ArgvGateLegTest(unittest.TestCase):
    def test_implicit_default_and_explicit_override_each_appear_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            detail = harness_fallback_swap._argv_gate_leg(ctx)
            self.assertEqual(detail["cases"]["implicit_default"]["value"], "default")
            self.assertEqual(detail["cases"]["explicit_override"]["value"], "acceptEdits")
            for case in detail["cases"].values():
                self.assertEqual(
                    len([v for v in case["argv"] if v == "--permission-mode"]), 1
                )


# --------------------------------------------------------- transition state machine


class TransitionStateMachineLegTest(unittest.TestCase):
    def test_full_phase_sequence_ack_and_drain_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            _manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"

            with _provenance_patch():
                detail = _transition_state_machine_leg(
                    ctx, manifest_path=_manifest_path, state_root=state_root,
                    primary=primary, target=target,
                )

            self.assertEqual(len(detail["events"]), 2)
            self.assertIn("handoff_requested", detail["events"][0])
            self.assertIn("awaiting_successor_read", detail["events"][1])

            completed = detail["completed_transition"]
            self.assertEqual(completed["phase"], "completed")
            self.assertTrue(completed["successor_read_ack"])
            self.assertTrue(completed["runtime_swapped"])
            self.assertEqual(completed["outgoing_harness"], "claude")
            self.assertEqual(completed["incoming_harness"], "codex")

            # Re-read from disk directly -- never trust the returned detail alone.
            reread = rc.transition_state(ctx.persona, state_root=state_root)
            self.assertEqual(reread["phase"], "completed")
            self.assertTrue(reread["successor_read_ack"])
            self.assertTrue(reread["runtime_swapped"])

            # Drain barrier: the claimed drain stays barred; a fresh one is allowed.
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.assert_drain_allowed(ctx.persona, detail["drain_id"], state_root=state_root)
            rc.assert_drain_allowed(
                ctx.persona, detail["new_drain_id_allowed"], state_root=state_root,
            )

            # runtime.json for the incoming (codex) binding was really published.
            runtime_json_path = state_root / target.persona / "runtime-controller" / "runtime.json"
            published = json.loads(runtime_json_path.read_text(encoding="utf-8"))
            self.assertEqual(published["harness"], "codex")
            self.assertEqual(published["profile"], "interactive-chatgpt")

    def test_first_call_never_replaces_runtime_before_handoff_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"
            drain_id = rc.new_drain_id()
            current = rc.RuntimeHandle(
                primary.launch, primary.controller.tmux, "session-1", rc._utc_now(),
            )
            handoff_path = state_root / "HANDOFF.md"

            with mock.patch.object(rc, "_replace_runtime") as replace, mock.patch.object(
                rc, "submit_to_runtime"
            ):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc.planned_swap(
                        current, target, message_id="m1", drain_id=drain_id,
                        handoff_path=handoff_path, state_root=state_root,
                    )
            replace.assert_not_called()
            self.assertFalse(handoff_path.exists())


# ------------------------------------------------------- persona target selection


class PersonaTargetSelectionLegTest(unittest.TestCase):
    def test_selects_incoming_binding_once_completed_and_preserves_persona_and_tmux(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"

            # Independent of the transition-state-machine leg's own timing:
            # hand-build a completed+swapped transition record directly.
            rc._begin_transition(
                ctx.persona, trigger="planned-quota",
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
            self.assertTrue(detail["role_preserved"])
            self.assertEqual(detail["selected_harness"], "codex")

            # tmux target is byte-identical -- the "stable operator target" contract.
            self.assertEqual(primary.controller.tmux, target.controller.tmux)

    def test_preparing_phase_selection_stays_on_the_outgoing_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            manifest_path, primary, target = _load_bindings(root, ctx)
            state_root = root / "state-root"

            rc._begin_transition(
                ctx.persona, trigger="planned-quota",
                outgoing_profile=primary.launch.profile, incoming_profile=target.launch.profile,
                outgoing_harness=primary.launch.harness, incoming_harness=target.launch.harness,
                drain_id="d1", state_root=state_root,
            )
            selected = persona_bringup.select_recorded_runtime_binding(
                manifest_path, primary, state_dir=state_root,
            )
            self.assertEqual(
                persona_bringup.runtime_reference(selected), persona_bringup.runtime_reference(primary),
            )


# --------------------------------------------------- correlated mailbox roundtrip


class CorrelatedMailboxRoundtripLegTest(unittest.TestCase):
    def test_reply_carries_the_post_swap_resolved_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_path = root / "hub"
            selected_reference = {"harness": "codex", "profile": "interactive-chatgpt"}

            detail = _correlated_mailbox_roundtrip_leg(
                hub_path=hub_path, selected_reference=selected_reference,
            )

            self.assertEqual(detail["reply_payload"]["correlation_id"], detail["nonce"])
            self.assertEqual(detail["reply_payload"]["post_swap_harness"], "codex")
            self.assertEqual(detail["reply_payload"]["post_swap_profile"], "interactive-chatgpt")

    def test_refuses_the_real_hub(self) -> None:
        with self.assertRaises(HardStop):
            _correlated_mailbox_roundtrip_leg(
                hub_path=harness_fallback_swap._REAL_HUB_DEFAULT,
                selected_reference={"harness": "codex", "profile": "interactive-chatgpt"},
            )


# ------------------------------------------------------- live kick lands executes


class LiveKickGuardrailTests(unittest.TestCase):
    """Cheap wiring tests -- no tmux/systemd touched."""

    def test_rejects_none_context(self) -> None:
        with self.assertRaises(HardStop):
            harness_fallback_swap._assert_disposable_tmux_context(None)

    def test_rejects_empty_socket(self) -> None:
        context = rc.TmuxRuntimeContext(socket="", scope_unit="pantheon-t2572-x.scope")
        with self.assertRaises(HardStop):
            harness_fallback_swap._assert_disposable_tmux_context(context)

    def test_rejects_production_scope_unit(self) -> None:
        context = rc.TmuxRuntimeContext(
            socket="pantheon-t2572-x", scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
        )
        with self.assertRaises(HardStop):
            harness_fallback_swap._assert_disposable_tmux_context(context)

    def test_rejects_socket_outside_its_own_namespace(self) -> None:
        context = rc.TmuxRuntimeContext(
            socket="some-other-socket", scope_unit="pantheon-t2572-x.scope",
        )
        with self.assertRaises(HardStop):
            harness_fallback_swap._assert_disposable_tmux_context(context)

    def test_accepts_well_formed_disposable_context(self) -> None:
        context = harness_fallback_swap._new_disposable_tmux_context("abc123")
        self.assertTrue(context.socket.startswith("pantheon-t2572-"))
        self.assertTrue(context.scope_unit.startswith("pantheon-t2572-"))

    def test_stand_in_script_renders_ready_and_logs_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "stand-in.sh"
            receipt_log = root / "receipts.log"
            harness_fallback_swap._write_stand_in_script(
                script, receipt_log=receipt_log, glyph="❯",
            )
            self.assertTrue(os.access(script, os.X_OK))

            result = subprocess.run(
                [str(script)], input="hello world\n", capture_output=True, text=True, timeout=5,
            )
            self.assertTrue(ready_for_submission(result.stdout))
            self.assertTrue(receipt_log.exists())
            self.assertIn("hello world", receipt_log.read_text(encoding="utf-8"))


class KickLandsExecutesStatusTests(unittest.TestCase):
    """Pure-function tests of the structural false-green guard -- no run
    needed to prove a stand-in can never read green."""

    def _completed_transition(self) -> dict:
        return {"phase": "completed", "runtime_swapped": True, "successor_read_ack": True}

    def test_stand_in_mode_never_proven_even_with_a_completed_transition(self) -> None:
        evidence = {"mode": "stand-in", "completed_transition": self._completed_transition()}
        self.assertEqual(kick_lands_executes_status(evidence), "deferred")

    def test_real_cli_mode_with_completed_transition_reads_proven(self) -> None:
        evidence = {"mode": "real-cli", "completed_transition": self._completed_transition()}
        self.assertEqual(kick_lands_executes_status(evidence), "proven")

    def test_real_cli_mode_with_incomplete_transition_stays_deferred(self) -> None:
        evidence = {
            "mode": "real-cli",
            "completed_transition": {"phase": "awaiting-successor-read"},
        }
        self.assertEqual(kick_lands_executes_status(evidence), "deferred")

    def test_missing_mode_defaults_to_deferred(self) -> None:
        self.assertEqual(
            kick_lands_executes_status({"completed_transition": self._completed_transition()}),
            "deferred",
        )


class LiveKickDisposableScopeLiveTests(unittest.TestCase):
    """Real tmux + real systemd --user, disposable socket/scope only -- never
    the default socket or the production pantheon-runtime-tmux.scope. Self-
    skips cleanly if this environment can't run them (see setUp)."""

    def setUp(self) -> None:
        _skip_unless_disposable_tmux_available(self)

    def test_stand_in_mode_completes_tears_down_cleanly_and_stays_deferred(self) -> None:
        account = getpass.getuser()
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "live-kick"
            with _provenance_patch():
                evidence = run_live_kick_lands_executes(work_root=work_root, account=account)

            self.assertEqual(evidence["mode"], harness_fallback_swap._LIVE_KICK_MODE_STAND_IN)
            transition = evidence["completed_transition"]
            self.assertEqual(transition["phase"], "completed")
            self.assertTrue(transition["runtime_swapped"])
            self.assertTrue(transition["successor_read_ack"])
            self.assertTrue(all(evidence["landed_evidence"].values()))

            # Structural false-green guard: a stand-in run, however clean,
            # NEVER reads "proven" -- this is the whole point of tagging mode.
            self.assertEqual(kick_lands_executes_status(evidence), "deferred")

            # The driver removes its own work_root.
            self.assertFalse(work_root.exists())

            # Zero-residue: the disposable scope/socket are really gone.
            scope_unit = evidence["context"]["scope_unit"]
            socket_name = evidence["context"]["socket"]
            show = subprocess.run(
                ["systemctl", "--user", "show", "--property=ActiveState", scope_unit],
                check=False, capture_output=True, text=True,
            )
            self.assertNotIn("ActiveState=active", show.stdout)
            has_session = subprocess.run(
                ["tmux", "-L", socket_name, "has-session"],
                check=False, capture_output=True, text=True,
            )
            self.assertNotEqual(has_session.returncode, 0)

    def test_teardown_runs_even_when_ack_fails_mid_sequence(self) -> None:
        account = getpass.getuser()
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "live-kick-fail"
            with _provenance_patch(), mock.patch.object(
                rc, "acknowledge_successor_read", side_effect=RuntimeError("forced failure"),
            ):
                with self.assertRaises((RuntimeError, HardStop)):
                    run_live_kick_lands_executes(work_root=work_root, account=account)

            # The driver's own work_root is gone regardless of the failure.
            self.assertFalse(work_root.exists())
            # No pantheon-t2572-* scope survives the forced mid-sequence
            # failure -- guaranteed teardown even on an exception (P4).
            leftover = subprocess.run(
                [
                    "systemctl", "--user", "list-units", "--all", "--no-legend",
                    "pantheon-t2572-*.scope",
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(leftover.stdout.strip(), "")


class TeardownDisposableScopeUnloadRaceTests(unittest.TestCase):
    """Hermetic (no real tmux/systemd needed -- always runs in CI) regression
    coverage for T-2619: ``_teardown_disposable_tmux_context``'s own residue
    check must poll the SAME predicate a caller actually cares about --
    ``list-units --all`` reporting the transient scope truly unloaded -- not
    an ``ActiveState`` snapshot. ``systemctl stop`` returning does not mean
    the scope has been unloaded from the manager's unit list yet; GC of a
    stopped transient unit's load-state is a separate, asynchronous step
    that can lag further under load -- exactly the race the live
    ``LiveKickDisposableScopeLiveTests`` test above intermittently lost
    under full-suite load. Reproduced deterministically here via a scripted
    ``subprocess.run`` stand-in rather than real-environment timing: 15/15
    synthetic-CPU-load trials against real disposable scopes came back
    clean when isolating this live, so a controlled fake is what makes the
    race provably fixed rather than merely no-longer-observed."""

    def _context(self) -> rc.TmuxRuntimeContext:
        return harness_fallback_swap._new_disposable_tmux_context("t2619race")

    def _scripted_run(self, list_units_stdouts):
        """subprocess.run stand-in: every ``list-units --all`` call consumes
        the next scripted stdout (simulating the unit lingering N polls
        before GC removes it); every other call (kill-server/stop/
        reset-failed) is a no-op success -- this driver's list-units poll is
        the only thing under test."""
        stdout_iter = iter(list_units_stdouts)

        def _run(argv, **kwargs):
            if argv[:5] == ["systemctl", "--user", "list-units", "--all", "--no-legend"]:
                return subprocess.CompletedProcess(argv, 0, stdout=next(stdout_iter), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        return _run

    def test_teardown_polls_list_units_until_scope_actually_unloads(self) -> None:
        context = self._context()
        listed_line = f"{context.scope_unit} loaded inactive dead\n"
        # First two polls still show the unit loaded (simulated GC lag under
        # load); the third poll reports it finally unloaded.
        run_mock = mock.Mock(side_effect=self._scripted_run([listed_line, listed_line, ""]))
        with mock.patch.object(
            harness_fallback_swap.subprocess, "run", run_mock,
        ), mock.patch.object(harness_fallback_swap.time, "sleep", return_value=None):
            residue = harness_fallback_swap._teardown_disposable_tmux_context(context)

        self.assertEqual(residue, [])
        list_units_calls = [
            call for call in run_mock.call_args_list
            if call.args[0][:5] == ["systemctl", "--user", "list-units", "--all", "--no-legend"]
        ]
        # Proves the driver actually BLOCKED/retried -- it did not accept
        # the first (still-loaded) answer as clean.
        self.assertEqual(len(list_units_calls), 3)

    def test_teardown_surfaces_residue_when_scope_never_unloads_within_deadline(self) -> None:
        context = self._context()
        listed_line = f"{context.scope_unit} loaded inactive dead\n"
        run_mock = mock.Mock(side_effect=self._scripted_run(_repeat(listed_line)))
        # time.sleep is a no-op, so time.monotonic() must actually advance
        # across polls for the deadline to be reachable -- fake the clock.
        clock = _FakeMonotonicClock(step=0.05)
        with mock.patch.object(
            harness_fallback_swap.subprocess, "run", run_mock,
        ), mock.patch.object(
            harness_fallback_swap.time, "sleep", return_value=None,
        ), mock.patch.object(harness_fallback_swap.time, "monotonic", clock):
            residue = harness_fallback_swap._teardown_disposable_tmux_context(context)

        self.assertTrue(
            any("still listed" in item for item in residue),
            f"expected list-units-still-listed residue, got: {residue!r}",
        )


def _repeat(value):
    while True:
        yield value


class _FakeMonotonicClock:
    """Deterministic stand-in for time.monotonic() that advances by ``step``
    each call -- lets a bounded-deadline poll loop actually reach its
    deadline without a real wall-clock sleep."""

    def __init__(self, *, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        current = self._now
        self._now += self._step
        return current


# ----------------------------------------------------------------------- prove


class ProveTest(unittest.TestCase):
    def test_prove_records_all_legs_names_the_deferred_boundary_and_restores_clean(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = HarnessFallbackSwapModule()

            with _provenance_patch():
                evidence = module.prove(ctx)

            self.assertEqual(evidence.status, "proven")
            legs = evidence.detail["legs"]
            self.assertEqual(
                set(legs),
                {
                    "argv_gate", "transition_state_machine", "persona_target_selection",
                    "correlated_mailbox_roundtrip", "kick_lands_executes",
                },
            )
            self.assertEqual(legs["kick_lands_executes"]["deferred_to"], DEFERRED_TO_TICKET)
            self.assertEqual(
                legs["transition_state_machine"]["completed_transition"]["phase"], "completed",
            )
            self.assertEqual(
                legs["persona_target_selection"]["selected_harness"],
                legs["correlated_mailbox_roundtrip"]["reply_payload"]["post_swap_harness"],
            )

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            for path in (
                root / "evidence" / ctx.run_id / "modules" / MODULE_ID / "instance.yaml",
            ):
                self.assertFalse(path.exists())

            row = module.report(evidence)
            self.assertIn(DEFERRED_TO_TICKET, row.summary)
            self.assertIn(COMPOSITE_ID, row.summary)
            self.assertIn("POST-SWAP-RESOLVED", row.summary)


if __name__ == "__main__":
    unittest.main()
