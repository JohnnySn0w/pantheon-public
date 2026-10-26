"""T-2917 5c.1: per-call-site argv/timeout parity for LocalHostBackend's
user-manager path after it moved behind the typed port (SystemctlUserManager).

Each test drives a real LocalHostBackend call site through a recording ``_run``
override (which never calls the real ``_run``) and asserts that the argv --
exact list/tuple type AND contents -- and the timeout (including whether the
``timeout`` keyword was passed at all) equal what the pre-refactor code at
9e8fa21fe emitted. The expected argv are hard-coded from that revision.

Site -> port mapping (pre-5c.1 argv shape in brackets):

  _unit_fire_show (pre-enqueue/capture/completion/cleanup polls)
      -> read_unit(UNIT_FIRE_POLL, timeout=...)       [list, timeout passed]
  _stop_and_prove_unit_fire_inactive stop
      -> stop_unit(timeout=...)                        [tuple, timeout passed]
  _manager_ready show-environment
      -> manager_environment()                         [tuple, no timeout]
  _timer_unit_state
      -> read_unit(UNIT_FILE_ACTIVE)                   [tuple, no timeout]
  _unit_inactive_dead / _prove_unit_inactive_dead / _prove_unit_notfound_inactive_dead
      -> read_unit(UNIT_FILE_ACTIVE)                   [tuple, no timeout]
  _stop_unit_or_converged_not_loaded / _stop_and_prove_inactive_dead stops
      -> stop_unit()                                   [tuple, no timeout]
  preflight unit-fire is-enabled
      -> unit_file_enablement()                        [tuple, no timeout]
  execute unit-fire is-enabled
      -> unit_file_enablement(timeout=...)             [tuple, timeout passed]
  execute unit-fire start --no-block
      -> start_unit(wait=False, timeout=...)           [tuple, timeout passed]
  codex-telegram-disabled-first (preflight AND execute) show / is-enabled / cat
      -> read_unit(RECEIVER_ACTIVE) [list] / unit_file_enablement() [tuple]
         / environment_file_declared() [tuple]          (in that order)
  legacy-convergence probe service / timer reads (incl. the settle loop)
      -> read_unit(SERVICE_SETTLE) [list] / read_unit(TIMER_ELAPSE) [list]
  timer-observe service / timer reads
      -> read_unit(SERVICE_OBSERVE) [list] / read_unit(TIMER_ELAPSE) [list]
  stale-unit-failure-reset show / reset-failed
      -> read_unit(STALE_RESET) [list, --all after unit] / reset_failed_unit() [tuple]
  execute daemon-reload
      -> reload()                                      [tuple]
  execute timer-enable enable --now
      -> enable_now()                                  [tuple]
  rollback_mutation timer-enable (disabled before-state)
      stop x2 -> stop_unit(); disable --now -> disable_now(); daemon-reload -> reload();
      reset-failed x2 -> reset_failed_unit()            [all tuple, no timeout]
  rollback_mutation timer-enable (enabled before-state) enable --now
      -> enable_now()                                  [tuple]
  RELOAD_STATE: no LocalHostBackend site (port groundwork for 5c.2).

Out of port scope and still emitted directly via ``_run`` (asserted unchanged):
  _identity_ready: ("loginctl", "show-user", <owner>, "-p", "Linger", "--value")
                   ("systemctl", "is-active", "user@<uid>.service")  -- SYSTEM manager/logind.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import persona_installer as pim
from tools.persona_installer import (
    EXECUTION_CONTEXT_ALL_IN_ONE,
    InstallOperation,
    LocalHostBackend,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    PreflightResult,
    SystemctlUserManager,
)
from tools.user_systemd import ReadPurpose

_MISSING = object()

SERVICE = "pantheon-persona-consume@probe.service"
TIMER = "pantheon-persona-consume@probe.timer"
RECEIVER = "codex-telegram-poller.service"
INVOCATION = "0123456789abcdef0123456789abcdef"


def _show_props(*names: str) -> list[str]:
    out: list[str] = []
    for name in names:
        out.extend(("-p", name))
    return out


# Hard-coded from 9e8fa21fe tools/persona_installer.py.
UNIT_FIRE_SHOW = ["systemctl", "--user", "show", SERVICE] + _show_props(
    "InvocationID", "ActiveState", "SubState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"
)
SERVICE_SETTLE_SHOW = ["systemctl", "--user", "show", SERVICE] + _show_props(
    "InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job"
)
SERVICE_OBSERVE_SHOW = ["systemctl", "--user", "show", SERVICE] + _show_props(
    "InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus"
)
TIMER_ELAPSE_SHOW = ["systemctl", "--user", "show", TIMER] + _show_props(
    "ActiveState", "SubState", "NextElapseUSecRealtime", "NextElapseUSecMonotonic"
)
RECEIVER_SHOW = ["systemctl", "--user", "show", RECEIVER] + _show_props("ActiveState", "MainPID")
BRINGUP_SHOW = ["systemctl", "--user", "show", SERVICE] + _show_props(
    "ActiveState", "SubState", "MainPID", "NRestarts"
)
STALE_SHOW = ["systemctl", "--user", "show", SERVICE, "--all"] + _show_props(
    "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "InvocationID", "Job"
)


def unit_file_show(unit: str) -> tuple[str, ...]:
    return (
        "systemctl",
        "--user",
        "show",
        unit,
        "-p",
        "LoadState",
        "-p",
        "UnitFileState",
        "-p",
        "ActiveState",
        "-p",
        "SubState",
    )


def cp(argv, rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)


class RecordingBackend(LocalHostBackend):
    """Records every _run call; never calls the real _run."""

    def __init__(self, handlers=()) -> None:
        super().__init__(command_timeout=1, timed_fire_timeout=1)
        self.calls: list[tuple[type, list[str], object]] = []
        self.handlers = list(handlers)

    def _run(self, argv, *, timeout=_MISSING):  # type: ignore[override]
        self.calls.append((type(argv), list(argv), timeout))
        command = tuple(argv)
        for handler in self.handlers:
            result = handler(command)
            if result is not None:
                return result
        return cp(command)

    def _current_account_only(self, account: str) -> PreflightResult:  # type: ignore[override]
        return PreflightResult(True, "current-account", f"test account {account}")

    def _unit_fire_bootstrap_blocker(self, operation):  # type: ignore[override]
        """T-5471: exact-argv parity fixtures model a ready host; the real gates are
        exercised in test_persona_installer_converge."""
        return None

    def _unit_fire_monotonic(self) -> float:  # type: ignore[override]
        return 0.0

    def _unit_fire_sleep(self, seconds: float) -> None:  # type: ignore[override]
        return None

    def user_manager_calls(self) -> list[tuple[type, list[str], object]]:
        return [call for call in self.calls if call[1] and call[1][0] == "systemctl"]


def on(prefix: tuple[str, ...], result_factory):
    def handler(command: tuple[str, ...]):
        if command[: len(prefix)] == prefix:
            return result_factory(command)
        return None

    return handler


def operation(kind: str, target: str, *, desired=None, provenance=None) -> InstallOperation:
    return InstallOperation(
        operation_id=f"probe.{kind}",
        layer=1,
        kind=kind,  # type: ignore[arg-type]
        target=target,
        precondition={},
        desired=dict(desired or {}),
        inverse=None,
        privilege="unprivileged",
        owner="probe",
        provenance=dict(provenance or {"install_root": "/"}),
    )


def context(root: str):
    import dataclasses

    return dataclasses.replace(EXECUTION_CONTEXT_ALL_IN_ONE, allowed_roots=(root,))


INACTIVE_DEAD = "LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\nSubState=dead\n"
IDLE_POLL = "InvocationID=\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nSuccessExitStatus=\nJob=\n"


class ShowArgvTableTests(unittest.TestCase):
    def test_show_argv_matches_historical_argv_for_every_purpose_with_a_site(self) -> None:
        expected = {
            ReadPurpose.UNIT_FIRE_POLL: (SERVICE, UNIT_FIRE_SHOW),
            ReadPurpose.SERVICE_SETTLE: (SERVICE, SERVICE_SETTLE_SHOW),
            ReadPurpose.SERVICE_OBSERVE: (SERVICE, SERVICE_OBSERVE_SHOW),
            ReadPurpose.TIMER_ELAPSE: (TIMER, TIMER_ELAPSE_SHOW),
            ReadPurpose.RECEIVER_ACTIVE: (RECEIVER, RECEIVER_SHOW),
            ReadPurpose.SERVICE_BRINGUP_OBSERVE: (SERVICE, BRINGUP_SHOW),
            ReadPurpose.STALE_RESET: (SERVICE, STALE_SHOW),
            ReadPurpose.UNIT_FILE_ACTIVE: (TIMER, unit_file_show(TIMER)),
        }
        for purpose, (unit, argv) in expected.items():
            with self.subTest(purpose=purpose):
                actual = SystemctlUserManager.show_argv(unit, purpose)
                self.assertIs(type(actual), type(argv))
                self.assertEqual(actual, argv)

    def test_lenient_reads_emit_list_argv_without_timeout(self) -> None:
        cases = [
            (SERVICE, ReadPurpose.SERVICE_SETTLE, SERVICE_SETTLE_SHOW),
            (SERVICE, ReadPurpose.SERVICE_OBSERVE, SERVICE_OBSERVE_SHOW),
            (TIMER, ReadPurpose.TIMER_ELAPSE, TIMER_ELAPSE_SHOW),
            (RECEIVER, ReadPurpose.RECEIVER_ACTIVE, RECEIVER_SHOW),
        ]
        for unit, purpose, argv in cases:
            with self.subTest(purpose=purpose):
                backend = RecordingBackend()
                backend._user_unit_read(unit, purpose)
                self.assertEqual(backend.calls, [(list, argv, _MISSING)])


class DirectSiteTests(unittest.TestCase):
    def test_controller_show_goes_through_the_port_with_the_historical_argv(self) -> None:
        # T-5452: the bring-up observe's private subprocess is gone; the same argv
        # now comes out of SystemctlUserManager.show_argv (list, no timeout kwarg).
        good = "ActiveState=active\nSubState=running\nMainPID=42\nNRestarts=0\n"
        backend = RecordingBackend([on(("systemctl",), lambda c: cp(c, 0, good))])
        values = backend._controller_show(SERVICE)
        self.assertEqual(
            values, {"ActiveState": "active", "SubState": "running", "MainPID": "42", "NRestarts": "0"}
        )
        self.assertEqual(backend.calls, [(list, BRINGUP_SHOW, _MISSING)])

    def test_manager_ready_show_environment(self) -> None:
        backend = RecordingBackend()
        self.assertTrue(backend._manager_ready().ok)
        self.assertEqual(backend.calls, [(tuple, ["systemctl", "--user", "show-environment"], _MISSING)])

    def test_timer_unit_state(self) -> None:
        backend = RecordingBackend([on(("systemctl",), lambda c: cp(c, 0, INACTIVE_DEAD))])
        state = backend._timer_unit_state(TIMER)
        self.assertEqual(state.classification, "disabled")
        self.assertEqual(backend.calls, [(tuple, list(unit_file_show(TIMER)), _MISSING)])

    def test_unit_inactive_dead_and_proofs(self) -> None:
        backend = RecordingBackend([on(("systemctl",), lambda c: cp(c, 0, INACTIVE_DEAD))])
        backend._unit_inactive_dead(SERVICE)
        backend._prove_unit_inactive_dead(SERVICE)
        with self.assertRaises(OperationFailed):
            backend._prove_unit_notfound_inactive_dead(SERVICE)
        self.assertEqual(backend.calls, [(tuple, list(unit_file_show(SERVICE)), _MISSING)] * 3)

    def test_stop_and_prove_inactive_dead(self) -> None:
        backend = RecordingBackend([on(("systemctl", "--user", "show"), lambda c: cp(c, 0, INACTIVE_DEAD))])
        backend._stop_and_prove_inactive_dead(SERVICE)
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "stop", SERVICE], _MISSING),
                (tuple, list(unit_file_show(SERVICE)), _MISSING),
            ],
        )

    def test_stop_unit_or_converged_not_loaded_failed_stop(self) -> None:
        notfound = "LoadState=not-found\nUnitFileState=\nActiveState=inactive\nSubState=dead\n"
        backend = RecordingBackend(
            [
                on(("systemctl", "--user", "stop"), lambda c: cp(c, 5, "", "Unit not loaded.")),
                on(("systemctl", "--user", "show"), lambda c: cp(c, 0, notfound)),
            ]
        )
        state = backend._stop_unit_or_converged_not_loaded(SERVICE)
        self.assertEqual(state.classification, "not-found")
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "stop", SERVICE], _MISSING),
                (tuple, list(unit_file_show(SERVICE)), _MISSING),
            ],
        )

    def test_unit_fire_show_passes_timeout(self) -> None:
        backend = RecordingBackend([on(("systemctl",), lambda c: cp(c, 0, IDLE_POLL))])
        backend._unit_fire_show(
            SERVICE,
            timeout=12.5,
            phase="capture",
            classification="state-unavailable",
            start_count=0,
            cleanup_state="pending",
        )
        self.assertEqual(backend.calls, [(list, UNIT_FIRE_SHOW, 12.5)])

    def test_unit_fire_cleanup_stop_passes_timeout(self) -> None:
        backend = RecordingBackend([on(("systemctl", "--user", "show"), lambda c: cp(c, 0, IDLE_POLL))])
        backend._stop_and_prove_unit_fire_inactive(SERVICE, total_deadline=30.0)
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "stop", SERVICE], 30.0),
                (list, UNIT_FIRE_SHOW, 30.0),
            ],
        )

    def test_stale_show_and_reset_failed(self) -> None:
        reset_required = (
            "LoadState=not-found\nActiveState=failed\nSubState=failed\nResult=exit-code\n"
            f"ExecMainStatus=1\nInvocationID={INVOCATION}\nJob=\n"
        )
        clean = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\nJob=\n"
        shows = [reset_required, clean]
        backend = RecordingBackend([on(("systemctl", "--user", "show"), lambda c: cp(c, 0, shows.pop(0)))])
        with tempfile.TemporaryDirectory() as root:
            _mutation, evidence = backend.execute(operation("stale-unit-failure-reset", SERVICE), context(root))
        self.assertEqual(evidence.evidence["classification"], "reset")
        self.assertEqual(
            backend.calls,
            [
                (list, STALE_SHOW, _MISSING),
                (tuple, ["systemctl", "--user", "reset-failed", SERVICE], _MISSING),
                (list, STALE_SHOW, _MISSING),
            ],
        )

    def test_execute_daemon_reload(self) -> None:
        backend = RecordingBackend()
        with tempfile.TemporaryDirectory() as root:
            backend.execute(operation("daemon-reload", "user-manager"), context(root))
        self.assertEqual(backend.calls, [(tuple, ["systemctl", "--user", "daemon-reload"], _MISSING)])


class CodexTelegramSiteTests(unittest.TestCase):
    EXPECTED = [
        (list, RECEIVER_SHOW, _MISSING),
        (tuple, ["systemctl", "--user", "is-enabled", RECEIVER], _MISSING),
        (tuple, ["systemctl", "--user", "cat", RECEIVER], _MISSING),
    ]

    def handlers(self):
        return [
            on(("systemctl", "--user", "show", RECEIVER), lambda c: cp(c, 0, "ActiveState=inactive\nMainPID=0\n")),
            on(("systemctl", "--user", "is-enabled"), lambda c: cp(c, 1, "disabled\n")),
            on(("systemctl", "--user", "cat"), lambda c: cp(c, 0, "[Unit]\nDescription=x\n")),
        ]

    def test_preflight(self) -> None:
        backend = RecordingBackend(self.handlers())
        result = backend.preflight(operation("codex-telegram-disabled-first", RECEIVER))
        self.assertTrue(result.ok, result)
        self.assertEqual(
            backend.calls,
            [(tuple, ["systemctl", "--user", "show-environment"], _MISSING)] + self.EXPECTED,
        )

    def test_execute(self) -> None:
        backend = RecordingBackend(self.handlers())
        with tempfile.TemporaryDirectory() as root:
            _mutation, evidence = backend.execute(operation("codex-telegram-disabled-first", RECEIVER), context(root))
        self.assertEqual(
            dict(evidence.evidence),
            {"active": "inactive", "enabled": "disabled", "main_pid": "0", "environment_file": "absent"},
        )
        self.assertEqual(backend.calls, self.EXPECTED)


class UnitFireSiteTests(unittest.TestCase):
    def patches(self):
        budget = SimpleNamespace(seconds=200, candidate_count=1, post_start_count=0)
        viability = SimpleNamespace(require_invocation_receipt=False)
        return (
            patch.object(pim, "_parse_unit_fire_timeout_budget", return_value=budget),
            patch.object(pim, "_parse_unit_fire_initial_viability_desired", return_value=viability),
        )

    def test_preflight_is_enabled_has_no_timeout(self) -> None:
        clean = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nInvocationID=\nJob=\n"
        backend = RecordingBackend(
            [
                on(("systemctl", "--user", "is-enabled"), lambda c: cp(c, 1, "disabled\n")),
                on(("systemctl", "--user", "show"), lambda c: cp(c, 0, clean)),
            ]
        )
        first, second = self.patches()
        with first, second:
            result = backend.preflight(operation("unit-fire", SERVICE, desired={"timer": TIMER}))
        self.assertTrue(result.ok, result)
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "show-environment"], _MISSING),
                (tuple, ["systemctl", "--user", "is-enabled", TIMER], _MISSING),
                # T-5471: the read-only idle prediction (the stale-reset classifier's read)
                (list, STALE_SHOW, _MISSING),
            ],
        )

    def test_execute_is_enabled_show_start_stop_pass_timeouts(self) -> None:
        backend = RecordingBackend(
            [
                on(("systemctl", "--user", "is-enabled"), lambda c: cp(c, 1, "disabled\n")),
                on(("systemctl", "--user", "show"), lambda c: cp(c, 0, IDLE_POLL)),
                on(("systemctl", "--user", "start"), lambda c: cp(c, 1, "", "start refused")),
            ]
        )
        first, second = self.patches()
        with first, second, tempfile.TemporaryDirectory() as root:
            with self.assertRaises(OperationFailed) as raised:
                backend.execute(operation("unit-fire", SERVICE, desired={"timer": TIMER}), context(root))
        self.assertIn("start refused", str(raised.exception))
        # budget 200s, teardown margin 60s, fake clock 0: observation deadline
        # 140s, total deadline 200s (the pre-5c.1 timeout arithmetic).
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "is-enabled", TIMER], 140.0),
                (list, UNIT_FIRE_SHOW, 140.0),
                (tuple, ["systemctl", "--user", "start", "--no-block", SERVICE], 140.0),
                (tuple, ["systemctl", "--user", "stop", SERVICE], 200.0),
                (list, UNIT_FIRE_SHOW, 200.0),
            ],
        )


class TimerSiteTests(unittest.TestCase):
    def layout(self, home: Path) -> tuple[Path, Path]:
        canonical = home / ".config" / "systemd" / "user" / "pantheon-persona-consume@.timer"
        link = home / ".config" / "systemd" / "user" / "timers.target.wants" / TIMER
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("[Timer]\n", encoding="utf-8")
        return canonical, link

    def test_execute_timer_enable_enable_now(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(os.path.realpath(tmp))
            canonical, link = self.layout(home)
            backend = RecordingBackend([on(("systemctl", "--user", "show"), lambda c: cp(c, 0, INACTIVE_DEAD))])
            op = operation(
                "timer-enable",
                TIMER,
                desired={"canonical_template_path": str(canonical), "enablement_link_path": str(link)},
                provenance={"install_root": str(home)},
            )
            backend.execute(op, context(str(home)))
        self.assertEqual(
            backend.calls,
            [
                (tuple, list(unit_file_show(TIMER)), _MISSING),
                (tuple, ["systemctl", "--user", "enable", "--now", TIMER], _MISSING),
            ],
        )

    def test_legacy_convergence_probe_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(os.path.realpath(tmp))
            canonical, link = self.layout(home)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(canonical)
            service_samples = [
                "InvocationID=\nActiveState=activating\nResult=success\nExecMainStatus=0\nSuccessExitStatus=\nJob=7\n",
                "InvocationID=\nActiveState=inactive\nResult=success\nExecMainStatus=5\nSuccessExitStatus=3 4 5\nJob=\n",
            ]
            backend = RecordingBackend(
                [
                    on(("systemctl", "--user", "show", SERVICE), lambda c: cp(c, 0, service_samples.pop(0))),
                    on(
                        ("systemctl", "--user", "show", TIMER),
                        lambda c: cp(c, 0, "ActiveState=active\nSubState=waiting\nNextElapseUSecRealtime=n/a\nNextElapseUSecMonotonic=12345\n"),
                    ),
                ]
            )
            op = operation(
                "timer-enablement-absence-check",
                "consume-timer",
                desired={"timer": TIMER, "canonical_template_path": str(canonical), "enablement_link_path": str(link)},
            )
            result = backend.preflight(op)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.classification, "converged-legacy")
        self.assertEqual(backend.events["connective.timer-observe"].evidence["next_elapse_basis"], "monotonic")
        self.assertEqual(backend.events["connective.unit-fire"].evidence["success_exit_status"], "3 4 5")
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "show-environment"], _MISSING),
                (list, SERVICE_SETTLE_SHOW, _MISSING),
                (list, SERVICE_SETTLE_SHOW, _MISSING),
                (list, TIMER_ELAPSE_SHOW, _MISSING),
            ],
        )

    def test_timer_observe_reads(self) -> None:
        backend = RecordingBackend(
            [
                on(
                    ("systemctl", "--user", "show", SERVICE),
                    lambda c: cp(c, 0, f"InvocationID={INVOCATION}\nActiveState=inactive\nResult=success\nExecMainStatus=0\nSuccessExitStatus=\n"),
                ),
                on(
                    ("systemctl", "--user", "show", TIMER),
                    lambda c: cp(c, 0, "ActiveState=active\nSubState=waiting\nNextElapseUSecRealtime=Sat 2026-08-15 04:00:00 UTC\nNextElapseUSecMonotonic=0\n"),
                ),
            ]
        )
        backend.events["connective.unit-fire"] = OperationEvidence(
            "connective.unit-fire",
            1,
            "unit-fire",
            True,
            "ok",
            {"invocation_id": INVOCATION, "initial_viability_receipt_sha256": "ab" * 32},
        )
        backend.events["connective.mailbox-probe"] = OperationEvidence(
            "connective.mailbox-probe", 1, "mailbox-probe", True, "ok", {"sha256": "cd" * 32}
        )
        with tempfile.TemporaryDirectory() as root:
            _mutation, evidence = backend.execute(
                operation("timer-observe", TIMER, desired={"service": SERVICE}), context(root)
            )
        self.assertEqual(evidence.evidence["next_elapse_basis"], "realtime")
        self.assertEqual(evidence.evidence["post_exit_invocation_id_state"], "same")
        self.assertEqual(
            backend.calls,
            [(list, SERVICE_OBSERVE_SHOW, _MISSING), (list, TIMER_ELAPSE_SHOW, _MISSING)],
        )


class RollbackSiteTests(unittest.TestCase):
    def test_rollback_disabled_before_state(self) -> None:
        backend = RecordingBackend([on(("systemctl", "--user", "show"), lambda c: cp(c, 0, INACTIVE_DEAD))])
        mutation = MutationRecord("probe.timer-enable", "timer-enable", TIMER, "probe", True, {"state": "disabled"}, "timer-enabled")
        with tempfile.TemporaryDirectory() as root:
            backend.rollback_mutation(mutation, allowed_roots=(root,))
        timer_show = (tuple, list(unit_file_show(TIMER)), _MISSING)
        service_show = (tuple, list(unit_file_show(SERVICE)), _MISSING)
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["systemctl", "--user", "stop", TIMER], _MISSING),
                timer_show,
                (tuple, ["systemctl", "--user", "stop", SERVICE], _MISSING),
                service_show,
                service_show,
                timer_show,
                (tuple, ["systemctl", "--user", "disable", "--now", TIMER], _MISSING),
                (tuple, ["systemctl", "--user", "daemon-reload"], _MISSING),
                (tuple, ["systemctl", "--user", "reset-failed", TIMER], _MISSING),
                (tuple, ["systemctl", "--user", "reset-failed", SERVICE], _MISSING),
                timer_show,
                service_show,
            ],
        )

    def test_rollback_disable_tolerates_missing_unit_text(self) -> None:
        backend = RecordingBackend(
            [
                on(("systemctl", "--user", "show"), lambda c: cp(c, 0, INACTIVE_DEAD)),
                on(("systemctl", "--user", "disable"), lambda c: cp(c, 1, "", "Unit file x does not exist.")),
            ]
        )
        mutation = MutationRecord("probe.timer-enable", "timer-enable", TIMER, "probe", True, {"state": "disabled"}, "timer-enabled")
        with tempfile.TemporaryDirectory() as root:
            backend.rollback_mutation(mutation, allowed_roots=(root,))
        self.assertIn((tuple, ["systemctl", "--user", "daemon-reload"], _MISSING), backend.calls)

    def test_rollback_enabled_before_state(self) -> None:
        backend = RecordingBackend()
        mutation = MutationRecord("probe.timer-enable", "timer-enable", TIMER, "probe", True, {"state": "enabled"}, "timer-enabled")
        with tempfile.TemporaryDirectory() as root:
            backend.rollback_mutation(mutation, allowed_roots=(root,))
        self.assertEqual(backend.calls, [(tuple, ["systemctl", "--user", "enable", "--now", TIMER], _MISSING)])


class ProxyAndScopeTests(unittest.TestCase):
    def test_run_replaced_after_user_manager_created_still_intercepts(self) -> None:
        backend = RecordingBackend()
        manager = backend.user_manager
        self.assertIs(manager, backend.user_manager)
        seen: list[tuple[list[str], object]] = []

        def replacement(argv, *, timeout=_MISSING):
            seen.append((list(argv), timeout))
            return cp(argv, 0, "PATH=/usr/bin\n")

        backend._run = replacement  # type: ignore[method-assign]
        manager.manager_environment()
        manager.stop_unit(SERVICE, timeout=3.0)
        self.assertEqual(
            seen,
            [
                (["systemctl", "--user", "show-environment"], _MISSING),
                (["systemctl", "--user", "stop", SERVICE], 3.0),
            ],
        )
        self.assertEqual(backend.calls, [])

    def test_no_timeout_keyword_where_legacy_passed_none(self) -> None:
        backend = RecordingBackend([on(("systemctl", "--user", "show"), lambda c: cp(c, 0, INACTIVE_DEAD))])
        manager = backend.user_manager
        manager.reload()
        manager.reset_failed_unit(SERVICE)
        manager.enable_now(TIMER)
        manager.disable_now(TIMER)
        manager.stop_unit(SERVICE)
        manager.unit_file_enablement(TIMER)
        manager.environment_file_declared(SERVICE)
        manager.read_unit(TIMER, ReadPurpose.UNIT_FILE_ACTIVE)
        self.assertTrue(all(call[2] is _MISSING for call in backend.calls), backend.calls)

    def test_identity_ready_still_direct_and_unchanged(self) -> None:
        backend = RecordingBackend(
            [
                on(("loginctl",), lambda c: cp(c, 0, "yes\n")),
                on(("systemctl", "is-active"), lambda c: cp(c, 0, "active\n")),
            ]
        )
        user = SimpleNamespace(pw_uid=4242, pw_gid=4242, pw_dir="/nonexistent")
        group = SimpleNamespace(gr_gid=5151)
        with patch.object(pim.pwd, "getpwnam", return_value=user), patch.object(
            pim.grp, "getgrnam", return_value=group
        ), patch.object(pim.os, "getgrouplist", return_value=[4242, 5151]):
            result = backend._identity_ready(operation("identity-check", "probe", desired={"mailbox_group": "mail"}))
        self.assertTrue(result.ok, result)
        self.assertEqual(
            backend.calls,
            [
                (tuple, ["loginctl", "show-user", "probe", "-p", "Linger", "--value"], _MISSING),
                (tuple, ["systemctl", "is-active", "user@4242.service"], _MISSING),
            ],
        )



class ManagerStateIsNotATransportCallTests(unittest.TestCase):
    """T-2917 5c.1 ruling (1): no new argv in a pure refactor."""

    def test_manager_state_raises_without_running_anything(self) -> None:
        calls: list[object] = []
        manager = SystemctlUserManager(lambda argv, timeout=None: calls.append(argv))
        with self.assertRaises(NotImplementedError):
            manager.manager_state()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
