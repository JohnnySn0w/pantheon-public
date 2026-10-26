"""T-5476: periodic timer-observe tolerates an immediately-firing timer, and rollback across a
completed bootstrap unit-fire stops the session that fire launched BEFORE it reverts the
containment key (and keeps the key if the stop is not confirmed).

Intent: Mason's first real HC install (T-5446) completes, and a failed install never leaves an
orphaned, unsupervised runtime under a deleted containment key.

Fakes / seams / tmp roots only. Nothing here runs tmux, systemctl, signals a real process, or
touches a real settings.json or a live seat.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import persona_installer as _pim
from tools.persona_installer import (
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    MutationRecord,
    OperationFailed,
    SimulatedHostBackend,
)
from tools import test_persona_installer as _tpi  # module import: pytest must not re-collect its cases
from tools import test_persona_installer_periodics as _tpp
from tools.test_persona_installer_shell_box import KEY, OP_ID, make_op

EXECUTION_CONTEXT_STAGED_ACTIVATE = _tpi.EXECUTION_CONTEXT_STAGED_ACTIVATE
TEST_INVOCATION_ID = _tpi.TEST_INVOCATION_ID
_with_codex_config_fixture = _tpi._with_codex_config_fixture
_with_roots = _tpi._with_roots

OWNER = "tester"
SERVICE = "pantheon-persona-quota@recovery.service"
TIMER = "pantheon-persona-quota@recovery.timer"
OLD_RUN = "a" * 32
NEW_RUN = "b" * 32


def _elapse(finite: bool):
    return types.SimpleNamespace(kind="finite" if finite else "unset", usec=1 if finite else None)


def _timer(active: str, sub: str, finite: bool = True):
    return types.SimpleNamespace(
        active_state=active, sub_state=sub,
        next_elapse_realtime=_elapse(finite), next_elapse_monotonic=_elapse(False),
    )


def _service(active: str = "inactive", result: str = "success", invocation: str = NEW_RUN):
    return types.SimpleNamespace(active_state=active, result=result, invocation_id=invocation)


def _observe_op(**desired) -> InstallOperation:
    base = {"service": SERVICE, "next_elapse": "finite",
            "observe_window_seconds": "60", "observe_interval_seconds": "2"}
    return InstallOperation(
        operation_id="periodic.quota.timer-observe", layer=1, kind="seat-periodic-timer-observe",
        target=TIMER, precondition={}, desired={**base, **desired}, inverse=None,
        privilege="operator-required", owner=OWNER, provenance={},
    )


class ObserveBackend(LocalHostBackend):
    """A scripted timer + service; no systemd, no wall clock."""

    def __init__(self, timers, services=None) -> None:
        super().__init__()
        self.timers = list(timers)
        self.services = list(services or [_service()])
        self.sleeps: list[float] = []
        self.reads = 0

    def _observe_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def _user_unit_read(self, unit, purpose):  # type: ignore[override]
        if unit == TIMER:
            self.reads += 1
            return self.timers[min(self.reads - 1, len(self.timers) - 1)]
        index = min(max(self.reads - 1, 0), len(self.services) - 1)
        return self.services[index]


# ---------------------------------------------------------------------------
# Gap 3: seat-periodic-timer-observe
# ---------------------------------------------------------------------------
class PeriodicObserveTests(unittest.TestCase):
    def test_periodic_observe_tolerates_immediately_firing_timer(self) -> None:
        backend = ObserveBackend([_timer("active", "running"), _timer("active", "running"),
                                  _timer("active", "waiting")])
        observed = backend._execute_periodic_observe(_observe_op())
        self.assertEqual(observed["next_elapse_finite"], "true")
        self.assertEqual(observed["observe_samples"], "3")
        self.assertEqual(observed["observe_transient_seen"], "true")
        self.assertEqual(backend.sleeps, [2, 2])

    def test_periodic_observe_tolerates_elapsed_then_waiting(self) -> None:
        backend = ObserveBackend([_timer("active", "elapsed"), _timer("active", "waiting")])
        observed = backend._execute_periodic_observe(_observe_op())
        self.assertEqual(observed["observe_samples"], "2")

    def test_periodic_observe_already_waiting_needs_no_poll(self) -> None:
        backend = ObserveBackend([_timer("active", "waiting")])
        observed = backend._execute_periodic_observe(_observe_op())
        self.assertEqual((observed["observe_samples"], observed["observe_transient_seen"], backend.sleeps),
                         ("1", "false", []))

    def test_periodic_observe_fails_when_timer_never_waits(self) -> None:
        backend = ObserveBackend([_timer("active", "running")])
        with self.assertRaises(OperationFailed) as raised:
            backend._execute_periodic_observe(_observe_op())
        self.assertIn("within 60s", str(raised.exception))
        self.assertEqual(raised.exception.evidence["observe_samples"], "31")
        self.assertEqual(backend.sleeps, [2] * 30)  # bounded: 60s total at 2s

    def test_periodic_observe_fails_when_waiting_never_gets_a_finite_elapse(self) -> None:
        backend = ObserveBackend([_timer("active", "waiting", finite=False)])
        with self.assertRaises(OperationFailed):
            backend._execute_periodic_observe(_observe_op())
        self.assertEqual(len(backend.sleeps), 30)

    def test_periodic_observe_fails_when_triggered_service_failed(self) -> None:
        backend = ObserveBackend([_timer("active", "running"), _timer("active", "waiting")],
                                 [_service("failed", "exit-code", NEW_RUN)])
        with self.assertRaises(OperationFailed) as raised:
            backend._execute_periodic_observe(_observe_op())
        self.assertIn("triggered service failed", str(raised.exception))

    def test_periodic_observe_fails_when_triggered_service_finished_unsuccessfully(self) -> None:
        backend = ObserveBackend([_timer("active", "waiting")], [_service("inactive", "exit-code")])
        with self.assertRaises(OperationFailed):
            backend._execute_periodic_observe(_observe_op())

    def test_periodic_observe_fails_fast_on_an_inactive_timer(self) -> None:
        backend = ObserveBackend([_timer("inactive", "dead")])
        with self.assertRaises(OperationFailed) as raised:
            backend._execute_periodic_observe(_observe_op())
        self.assertIn("inactive/dead", str(raised.exception))
        self.assertEqual(backend.sleeps, [])

    def test_periodic_observe_ignores_a_stale_failure_from_before_the_arm(self) -> None:
        backend = ObserveBackend([_timer("active", "waiting")], [_service("failed", "exit-code", OLD_RUN)])
        backend._periodic_service_baselines = {SERVICE: OLD_RUN}
        observed = backend._execute_periodic_observe(_observe_op())
        self.assertEqual(observed["observe_stale_service_failure_ignored"], "true")

    def test_periodic_observe_fails_on_a_new_run_failure_despite_a_baseline(self) -> None:
        backend = ObserveBackend([_timer("active", "running"), _timer("active", "waiting")],
                                 [_service("failed", "exit-code", NEW_RUN)])
        backend._periodic_service_baselines = {SERVICE: OLD_RUN}
        with self.assertRaises(OperationFailed):
            backend._execute_periodic_observe(_observe_op())

    def test_periodic_enable_captures_the_service_baseline_before_arming(self) -> None:
        backend = ObserveBackend([_timer("active", "waiting")], [_service("failed", "exit-code", OLD_RUN)])
        enable = dataclasses.replace(_observe_op(), kind="seat-periodic-timer-enable")
        backend._capture_periodic_service_baseline(enable)
        self.assertEqual(backend._periodic_service_baselines, {SERVICE: OLD_RUN})


class PeriodicPlanAndDryRunTests(_tpp.PeriodicCase):
    def test_plan_carries_the_bound_and_the_service_baseline_inputs(self) -> None:
        plan = self.plan()
        for name in ("quota", "keep-going"):
            observe = self.op(plan, f"periodic.{name}.timer-observe")
            enable = self.op(plan, f"periodic.{name}.timer-enable")
            self.assertEqual(observe.desired["observe_window_seconds"], "60")
            self.assertEqual(observe.desired["observe_interval_seconds"], "2")
            self.assertEqual(enable.desired["service"], observe.desired["service"])

    def test_periodic_observe_dry_run_predicts_transient_tolerance_on_both_backends(self) -> None:
        plan = self.plan()
        observe = self.op(plan, "periodic.quota.timer-observe")
        import os
        import pwd

        rooted = dataclasses.replace(observe, provenance={
            **observe.provenance, "install_root": "/", "account": pwd.getpwuid(os.geteuid()).pw_name})
        for backend, operation in ((SimulatedHostBackend(), observe), (LocalHostBackend(), rooted)):
            with self.subTest(backend=type(backend).__name__):
                result = backend.preflight(operation)
                self.assertTrue(result.ok)
                self.assertEqual(result.classification, "transient-tolerant")
                for needle in ("active+running/elapsed is tolerated", "60s", "2s", "active+waiting",
                               observe.desired["service"]):
                    self.assertIn(needle, result.detail)

    def test_simulated_observe_evidence_states_the_tolerance_and_bound(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        observe = self.op(plan, "periodic.quota.timer-observe")
        backend.enabled_timers.add(observe.target)
        _, evidence = backend.execute(observe, _pim.EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence["transient_tolerant"], "true")
        self.assertEqual(evidence.evidence["observe_window_seconds"], "60")
        self.assertEqual(evidence.evidence["observe_interval_seconds"], "2")


# ---------------------------------------------------------------------------
# Gap 4: rollback across a completed bootstrap fire (simulated, real apply/rollback loops)
# ---------------------------------------------------------------------------
class SimFireRollbackTests(_tpp.PeriodicCase):
    def run_apply(self, **backend_kwargs):
        plan = self.plan()
        backend = SimulatedHostBackend(fail_operation="periodic.quota.timer-observe")
        backend.fire_launches_session = True
        for key, value in backend_kwargs.items():
            setattr(backend, key, value)
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        return plan, backend, report

    def settings_env(self, backend):
        entry = next(v for k, v in backend.entries.items() if k.endswith("/.claude/settings.json"))
        import base64

        return json.loads(base64.b64decode(entry["content_b64"])).get("env", {})

    def test_unit_fire_precedes_nothing_it_must_outlive_in_the_plan(self) -> None:
        ids = self.ids(self.plan())
        # reverse-order rollback therefore reaches the unit-fire record BEFORE the settings key
        self.assertLess(ids.index(OP_ID), ids.index("connective.unit-fire"))

    def test_rollback_stops_fire_launched_session_before_reverting_shell_box_key(self) -> None:
        plan, backend, report = self.run_apply()
        self.assertEqual(report.status, "failed")
        stop = backend.events.index("rollback-stop-session:connective.unit-fire")
        key = backend.events.index(f"rollback:{OP_ID}")
        self.assertLess(stop, key)
        self.assertFalse(backend.fire_session_alive)
        self.assertNotIn(KEY, self.settings_env(backend))

    def test_rollback_keeps_shell_box_key_when_stop_unconfirmed(self) -> None:
        plan, backend, report = self.run_apply(fire_session_stop_confirms=False)
        self.assertTrue(backend.fire_session_alive)
        self.assertIn(KEY, self.settings_env(backend))  # never a live session under a deleted prefix
        notes = "; ".join(report.rollback)
        self.assertIn("connective.unit-fire: rollback failed", notes)
        self.assertIn("still alive", notes)
        self.assertIn(f"{OP_ID}: rollback failed: containment key kept", notes)
        # the persisted failed receipt keeps BOTH records, so a later standalone rollback retries
        # the stop FIRST and aborts before the key if it still cannot be confirmed
        receipt = report.receipt
        self.assertIsNotNone(receipt)
        changed = {m.operation_id: m for m in receipt.mutations if m.changed}
        self.assertIn("connective.unit-fire", changed)
        self.assertIn(OP_ID, changed)
        self.assertIn(receipt.receipt_id, backend.receipts)
        again = self.installer.rollback(receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest)
        self.assertEqual(again.status, "failed")
        self.assertIn(KEY, self.settings_env(backend))
        backend.fire_session_stop_confirms = True
        retried = self.installer.rollback(receipt, backend, self.actor(plan), plan=plan, manifest=self.manifest)
        self.assertIn(retried.status, {"rolled-back", "noop"}, retried)
        self.assertFalse(backend.fire_session_alive)
        self.assertNotIn(KEY, self.settings_env(backend))
        self.assertLess(
            backend.events.index("rollback-stop-session:connective.unit-fire", backend.events.index("receipt:failed")),
            len(backend.events),
        )

    def test_rollback_does_not_touch_foreign_session(self) -> None:
        plan, backend, report = self.run_apply(fire_session_foreign=True)
        self.assertNotIn("rollback-stop-session:connective.unit-fire", backend.events)
        self.assertTrue(backend.fire_session_alive)
        self.assertIn(KEY, self.settings_env(backend))
        self.assertIn("rollback refused", "; ".join(report.rollback))

    def test_fire_failing_after_launch_stops_session_in_the_simulated_backend(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        backend.fire_launches_session = True
        backend.fail_after_fire_launch = True
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "failed")
        self.assertFalse(backend.fire_session_alive)
        self.assertIn("fire-session-stop:connective.unit-fire", backend.events)
        self.assertNotIn(KEY, self.settings_env(backend))

    def test_fire_failing_after_launch_unconfirmed_keeps_key_and_records_the_session(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        backend.fire_launches_session = True
        backend.fail_after_fire_launch = True
        backend.fire_session_stop_confirms = False
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertTrue(backend.fire_session_alive)
        self.assertIn(KEY, self.settings_env(backend))
        self.assertIn("connective.unit-fire", {m.operation_id for m in report.receipt.mutations if m.changed})

    def test_simulated_dry_run_default_is_unchanged_without_a_launched_session(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "applied", report.errors)
        fire = next(m for m in report.receipt.mutations if m.operation_id == "connective.unit-fire")
        self.assertFalse(fire.changed)


# ---------------------------------------------------------------------------
# Gap 4: the local backend (identity match, SIGTERM, bounded confirm, key gate)
# ---------------------------------------------------------------------------
T0 = datetime(2026, 10, 25, 20, 0, 0, tzinfo=timezone.utc)
TARGET = "suborch1-main:main.0"
PID = 4242


class FireBackend(LocalHostBackend):
    """Fake pane, fake process table, fake signals. No tmux, no kill."""

    def __init__(self) -> None:
        super().__init__()
        self.panes: list[str] = []  # scripted pane pids, consumed per query; last one repeats
        self.procs: dict[int, tuple[str, str]] = {}
        self.signals: list[tuple[int, int]] = []
        self.sleeps: list[float] = []
        self.stubborn = False
        self.wall = T0

    def _fire_wall_clock(self):
        return self.wall

    def _observe_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def _run(self, argv, *, timeout=None):  # type: ignore[override]
        argv = list(argv)
        if argv[:2] == ["tmux", "display-message"]:
            pid = self.panes.pop(0) if len(self.panes) > 1 else (self.panes[0] if self.panes else "absent")
            if pid == "absent":
                return subprocess.CompletedProcess(argv, 1, "", "no pane")
            session, _, rest = TARGET.partition(":")
            window, _, pane = rest.rpartition(".")
            return subprocess.CompletedProcess(argv, 0, f"{session}\t{window}\t{pane}\t{pid}\n", "")
        raise AssertionError(f"unexpected command {argv}")

    def _proc_stat(self, pid):  # type: ignore[override]
        return self.procs.get(pid)

    def _signal_pid(self, pid, sig):  # type: ignore[override]
        self.signals.append((pid, sig))
        if pid not in self.procs:
            raise ProcessLookupError
        if not self.stubborn:
            del self.procs[pid]


class LocalFireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.backend = FireBackend()
        self.state_root = self.tmp / "personas"
        self.runtime_json = self.state_root / "mason" / "runtime-controller" / "runtime.json"
        self.runtime_json.parent.mkdir(parents=True)
        self.fire = InstallOperation(
            operation_id="connective.unit-fire", layer=1, kind="unit-fire",
            target="pantheon-persona-consume@mason.service", precondition={},
            desired={"tmux_target": TARGET, "persona_state_root": str(self.state_root)},
            inverse=None, privilege="operator-required", owner=OWNER, provenance={"persona": "mason"},
        )
        self.started = (T0 + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        self.write_runtime(self.started)

    def write_runtime(self, started: str, target: str = TARGET) -> None:
        self.runtime_json.write_text(json.dumps(
            {"tmux_target": target, "started_at": started, "runtime_scope_unit": "pantheon-runtime.scope"}))

    def launch(self, pid: int = PID, ticks: str = "777") -> dict[str, str]:
        self.backend.procs[pid] = ("S", ticks)
        self.backend.panes = [str(pid)]
        identity = self.backend._fire_launch_identity(
            self.fire, pre_pane_pid="absent", fire_wall_start=T0, invocation_id=TEST_INVOCATION_ID)
        assert identity is not None
        return identity

    def record(self, identity: dict[str, str]) -> MutationRecord:
        return MutationRecord(self.fire.operation_id, "unit-fire", self.fire.target, OWNER, True,
                              identity, "fire-launched")

    # --- identity capture -------------------------------------------------
    def test_identity_is_captured_from_runtime_json_and_the_live_pane(self) -> None:
        identity = self.launch()
        self.assertEqual(
            (identity["pane_pid"], identity["tmux_target"], identity["runtime_started_at"],
             identity["pane_start_ticks"], identity["invocation_id"]),
            (str(PID), TARGET, self.started, "777", TEST_INVOCATION_ID))

    def test_pane_that_existed_before_the_fire_is_not_ours(self) -> None:
        self.backend.panes = [str(PID)]
        self.backend.procs[PID] = ("S", "777")
        self.assertIsNone(self.backend._fire_launch_identity(
            self.fire, pre_pane_pid="999", fire_wall_start=T0, invocation_id=""))

    def test_runtime_json_older_than_the_fire_is_not_ours(self) -> None:
        # same clock basis on both sides: UTC wall time (fire start vs runtime.json started_at)
        self.write_runtime((T0 - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
        self.backend.panes = [str(PID)]
        self.assertIsNone(self.backend._fire_launch_identity(
            self.fire, pre_pane_pid="absent", fire_wall_start=T0, invocation_id=""))

    # --- rollback of the fire record ---------------------------------------
    def test_rollback_sigterms_the_matched_session_and_confirms_it_gone(self) -> None:
        mutation = self.record(self.launch())
        self.assertEqual(self.backend.can_rollback(mutation).classification, "owned")
        self.backend.rollback_mutation(mutation, allowed_roots=(str(self.tmp),))
        import signal

        self.assertEqual(self.backend.signals, [(PID, signal.SIGTERM)])
        self.assertNotIn(PID, self.backend.procs)

    def test_rollback_session_already_gone_is_clean_and_sends_no_signal(self) -> None:
        mutation = self.record(self.launch())
        del self.backend.procs[PID]
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")
        self.backend.rollback_mutation(mutation, allowed_roots=(str(self.tmp),))
        self.assertEqual(self.backend.signals, [])

    def test_rollback_does_not_touch_foreign_session(self) -> None:
        mutation = self.record(self.launch())
        for label, mutate in {
            "pid reused (start time differs)": lambda: self.backend.procs.__setitem__(PID, ("S", "888")),
            "pane is another pid": lambda: setattr(self.backend, "panes", ["5151"]),
            "runtime.json restarted": lambda: self.write_runtime("2026-10-25T21:00:00Z"),
            "runtime.json other target": lambda: self.write_runtime(self.started, "other:main.0"),
        }.items():
            with self.subTest(label):
                self.backend.procs[PID] = ("S", "777")
                self.backend.panes = [str(PID)]
                self.write_runtime(self.started)
                mutate()
                self.backend.signals.clear()
                if label.startswith("pid reused"):
                    # a reused pid is a DIFFERENT process: ours is gone, nothing to signal
                    self.backend.rollback_mutation(mutation, allowed_roots=(str(self.tmp),))
                else:
                    self.assertFalse(self.backend.can_rollback(mutation).ok)
                    with self.assertRaises(OperationFailed) as raised:
                        self.backend.rollback_mutation(mutation, allowed_roots=(str(self.tmp),))
                    self.assertIn("left untouched", str(raised.exception))
                self.assertEqual(self.backend.signals, [])

    def test_rollback_fails_loudly_when_the_stop_is_not_confirmed_within_10s(self) -> None:
        self.backend.stubborn = True
        mutation = self.record(self.launch())
        with self.assertRaises(OperationFailed) as raised:
            self.backend.rollback_mutation(mutation, allowed_roots=(str(self.tmp),))
        self.assertIn("still alive 10s after SIGTERM", str(raised.exception))
        self.assertEqual(self.backend.sleeps, [1] * 10)  # bounded poll through the seam
        self.assertEqual(len(self.backend.signals), 1)  # SIGTERM only, no escalation

    # --- the containment key gate ------------------------------------------
    def settings_mutation(self):
        script = self.tmp / "box.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        target = self.tmp / ".claude" / "settings.json"
        target.parent.mkdir()
        op = make_op(str(target), str(script))
        mutation, _ = self.backend.execute(op, ExecutionContext("all-in-one", allowed_roots=(str(self.tmp),)))
        return target, mutation

    def test_rollback_keeps_shell_box_key_when_stop_unconfirmed(self) -> None:
        target, settings = self.settings_mutation()
        self.backend.stubborn = True
        fire = self.record(self.launch())
        self.backend._fire_launched_sessions.append(dict(fire.before))
        roots = (str(self.tmp),)
        with self.assertRaises(OperationFailed):
            self.backend.rollback_mutation(fire, allowed_roots=roots)
        with self.assertRaises(OperationFailed) as raised:
            self.backend.rollback_mutation(settings, allowed_roots=roots)
        self.assertIn("containment key kept", str(raised.exception))
        self.assertIn(KEY, json.loads(target.read_text())["env"])
        # once the session is really gone the key reverts
        self.backend.stubborn = False
        self.backend.rollback_mutation(fire, allowed_roots=roots)
        self.backend.rollback_mutation(settings, allowed_roots=roots)
        self.assertNotIn(KEY, json.loads(target.read_text()).get("env", {}))

    def test_key_reverts_only_after_the_fire_record_stopped_the_session(self) -> None:
        target, settings = self.settings_mutation()
        fire = self.record(self.launch())
        self.backend._fire_launched_sessions.append(dict(fire.before))
        order: list[str] = []
        orig_stop, orig_env = self.backend._stop_matched_session, self.backend._rollback_settings_env
        self.backend._stop_matched_session = lambda i: (order.append("stop"), orig_stop(i))[1]
        self.backend._rollback_settings_env = lambda m: (order.append("key"), orig_env(m))[1]
        for mutation in reversed([settings, fire]):  # the apply loop: reverse op order
            check = self.backend.can_rollback(mutation)
            if check.ok:
                self.backend.rollback_mutation(mutation, allowed_roots=(str(self.tmp),))
        self.assertEqual(order, ["stop", "key"])

    # --- a fire that fails after its launch --------------------------------
    def test_fire_failing_after_launch_stops_session(self) -> None:
        self.backend.procs[PID] = ("S", "777")
        self.backend.panes = [str(PID)]
        evidence = self.backend._stop_fire_launched_session(
            self.fire, pre_pane_pid="absent", fire_wall_start=T0, invocation_id=TEST_INVOCATION_ID)
        self.assertEqual((evidence["fire_session_match"], evidence["fire_session_stopped"]),
                         ("matched", "true"))
        self.assertNotIn(PID, self.backend.procs)

    def test_fire_failing_after_launch_unconfirmed_reports_identity_for_the_receipt(self) -> None:
        self.backend.stubborn = True
        self.backend.procs[PID] = ("S", "777")
        self.backend.panes = [str(PID)]
        evidence = self.backend._stop_fire_launched_session(
            self.fire, pre_pane_pid="absent", fire_wall_start=T0, invocation_id="")
        self.assertEqual(evidence["fire_session_stopped"], "false")
        recovery = self.backend.issued_timer_enable_recovery_mutation(self.fire, evidence)
        self.assertIsNotNone(recovery)
        self.assertTrue(recovery.changed)
        self.assertEqual(recovery.before["pane_pid"], str(PID))

    def test_fire_failing_after_launch_leaves_a_pre_existing_pane_alone(self) -> None:
        self.backend.procs[PID] = ("S", "777")
        self.backend.panes = [str(PID)]
        self.assertEqual(self.backend._stop_fire_launched_session(
            self.fire, pre_pane_pid="999", fire_wall_start=T0, invocation_id=""), {})
        self.assertEqual(self.backend.signals, [])

    def test_unmatched_new_pane_is_not_signalled_but_holds_the_key(self) -> None:
        self.write_runtime((T0 - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        self.backend.procs[PID] = ("S", "777")
        self.backend.panes = [str(PID)]
        evidence = self.backend._stop_fire_launched_session(
            self.fire, pre_pane_pid="absent", fire_wall_start=T0, invocation_id="")
        self.assertEqual(evidence["fire_session_match"], "unmatched")
        self.assertEqual(self.backend.signals, [])
        self.assertIsNotNone(self.backend._live_session_key_block())


# ---------------------------------------------------------------------------
# Gap 4: the real LocalHostBackend.execute of unit-fire (scripted systemd + pane)
# ---------------------------------------------------------------------------
class FireExecuteBackend(_tpi.LocalHostStageBackend):
    def __init__(self) -> None:
        super().__init__()
        self.pane_queries = 0
        self.launched = True
        self.stubborn = False
        self.procs: dict[int, tuple[str, str]] = {}
        self.signals: list[int] = []
        self.target = ""
        self.wall = datetime.now(timezone.utc)
        self._fake_clock = 1000.0

    def _fire_wall_clock(self):
        return self.wall

    def _run(self, argv, *, timeout=None):  # type: ignore[override]
        if list(argv)[:2] == ["tmux", "display-message"]:
            self.pane_queries += 1
            if self.pane_queries == 1 or not self.launched:  # first query: before the fire
                return subprocess.CompletedProcess(list(argv), 1, "", "no pane")
            session, _, rest = self.target.partition(":")
            window, _, pane = rest.rpartition(".")
            return subprocess.CompletedProcess(list(argv), 0, f"{session}\t{window}\t{pane}\t{PID}\n", "")
        return super()._run(argv, timeout=timeout)

    def _proc_stat(self, pid):  # type: ignore[override]
        return self.procs.get(pid)

    def _signal_pid(self, pid, sig):  # type: ignore[override]
        self.signals.append(pid)
        if not self.stubborn:
            self.procs.pop(pid, None)


class FireExecuteTests(unittest.TestCase):
    setUp = _tpi.InstallerCase.setUp
    tearDown = _tpi.InstallerCase.tearDown
    write_manifest = _tpi.InstallerCase.write_manifest
    actor = _tpi.InstallerCase.actor
    operation = _tpi.InstallerCase.operation
    orchestrator_local_stage_fixture = _tpi.InstallerCase.orchestrator_local_stage_fixture
    write_invocation_viability_receipt = _tpi.InstallerCase.write_invocation_viability_receipt

    def prepare(self, name: str, backend: FireExecuteBackend):
        _root, plan, _stage = self.orchestrator_local_stage_fixture(name)
        self.write_invocation_viability_receipt(plan)
        fire = self.operation(plan, "connective.unit-fire")
        backend.target = fire.desired["tmux_target"]
        backend.procs[PID] = ("S", "777")
        runtime = Path(fire.desired["persona_state_root"]) / plan.persona / "runtime-controller" / "runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        started = (backend.wall + timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
        runtime.write_text(json.dumps({"tmux_target": backend.target, "started_at": started,
                                       "runtime_scope_unit": "pantheon-runtime.scope"}))
        return plan, fire

    @_with_codex_config_fixture
    def test_plan_names_the_pane_the_fire_launches(self) -> None:
        _root, plan, _stage = self.orchestrator_local_stage_fixture("t5476-plan")
        fire = self.operation(plan, "connective.unit-fire")
        controller = self.operation(plan, "service.runtime-controller.enable-observe")
        self.assertEqual(fire.desired["tmux_target"], controller.desired["tmux_target"])

    @_with_codex_config_fixture
    def test_completed_fire_returns_a_receipt_owned_launch_record(self) -> None:
        backend = FireExecuteBackend()
        _plan, fire = self.prepare("t5476-ok", backend)
        mutation, evidence = backend.execute(fire, _with_roots(EXECUTION_CONTEXT_STAGED_ACTIVATE, self.root))
        self.assertTrue(evidence.ok, evidence.detail)
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.kind, "unit-fire")
        self.assertEqual(
            (mutation.before["pane_pid"], mutation.before["tmux_target"], mutation.before["invocation_id"],
             mutation.before["pane_start_ticks"]),
            (str(PID), backend.target, TEST_INVOCATION_ID, "777"))
        self.assertEqual(evidence.evidence["fire_session_pid"], str(PID))

    @_with_codex_config_fixture
    def test_completed_fire_with_a_pre_existing_pane_records_no_ownership(self) -> None:
        backend = FireExecuteBackend()
        _plan, fire = self.prepare("t5476-attached", backend)
        backend.pane_queries = 1  # the pre-fire query already saw a pane: swap the order
        orig = backend._run

        def run(argv, *, timeout=None):
            if list(argv)[:2] == ["tmux", "display-message"]:
                session, _, rest = backend.target.partition(":")
                window, _, pane = rest.rpartition(".")
                return subprocess.CompletedProcess(list(argv), 0, f"{session}\t{window}\t{pane}\t{PID}\n", "")
            return orig(argv, timeout=timeout)

        backend._run = run  # type: ignore[assignment]
        mutation, evidence = backend.execute(fire, _with_roots(EXECUTION_CONTEXT_STAGED_ACTIVATE, self.root))
        self.assertFalse(mutation.changed)
        self.assertEqual(evidence.evidence["fire_session"], "none-owned")

    @_with_codex_config_fixture
    def test_fire_failing_after_launch_stops_session_through_execute(self) -> None:
        class TimeoutBackend(FireExecuteBackend):
            def __init__(self) -> None:
                super().__init__()
                self.service_show_sequence = [
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                    f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
                    f"InvocationID={TEST_INVOCATION_ID}\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nJob=1\n",
                    "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nJob=\n",
                ]

            def _unit_fire_sleep(self, seconds: float) -> None:  # type: ignore[override]
                self._fake_clock = 1268.0

        backend = TimeoutBackend()
        _plan, fire = self.prepare("t5476-fail", backend)
        with self.assertRaises(OperationFailed) as raised:
            backend.execute(fire, _with_roots(EXECUTION_CONTEXT_STAGED_ACTIVATE, self.root))
        self.assertIn("completion-timeout", str(raised.exception))
        self.assertEqual(raised.exception.evidence["fire_session_stopped"], "true")
        self.assertEqual(backend.signals, [PID])
        self.assertNotIn(PID, backend.procs)


if __name__ == "__main__":  # keep last (main_guard_lint)
    unittest.main()
