"""T-2917 5c.2: the per-seat executor's user_systemd.activate kinds, the apply
pre-pass, the unit allowlist, budgets, the B9 legacy-convergence branch,
disabled-first as diagnostics.read, and read-only Verify observations.

Fixture evidence only: the engine drives the grown ``FakeUserManager``
(native typed values, transport "fake", address_source "test-override")
against a disposable seat root. The real user bus is exercised separately
(the user-bus and E39 suites); a real systemd user manager is T-2924.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import _host_control_executor_fixtures as fx
from tools.host_control import driver as driver_module
from tools.host_control.contracts import Unsupported
from tools.host_control.executor import engine as engine_module
from tools import user_systemd as _us
from tools.host_control.executor.user_manager import UserManagerUnavailable
from tools.host_control.executor.wire import EXECUTOR_RUN_BUDGET_SECONDS, InvalidSpec
from tools.persona_installer import (
    UNIT_FIRE_TIMEOUT_FORMULA,
    _consume_timer_enablement_link_path,
    _consume_timer_template_path,
    _unit_fire_timeout_seconds,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONA = "seat"
SERVICE = f"pantheon-persona-consume@{PERSONA}.service"
TIMER = f"pantheon-persona-consume@{PERSONA}.timer"
RECEIVER = "codex-telegram-poller.service"
INVOCATION_HEX = "0123456789abcdef0123456789abcdef"
INVOCATION = bytes.fromhex(INVOCATION_HEX)
NO_JOB = (0, "/")
WRITE_CALLS = ("reload:", "reset_failed_unit:", "start_unit:", "stop_unit:", "enable_now:", "disable_now:")


def _service_state(
    active: str = "inactive",
    sub: str = "dead",
    result: str = "success",
    exec_status: int = 0,
    invocation: bytes = b"",
    policy: tuple[list[int], list[int]] = ([], []),
    job: tuple[int, str] = NO_JOB,
    load: str = "loaded",
) -> dict[str, object]:
    return {
        "LoadState": load,
        "ActiveState": active,
        "SubState": sub,
        "Result": result,
        "ExecMainStatus": exec_status,
        "InvocationID": invocation,
        "Job": job,
        "SuccessExitStatus": policy,
        "UnitFileState": "static",
        "MainPID": 0,
        "NeedDaemonReload": False,
    }


def _timer_state(
    active: str = "inactive",
    sub: str = "dead",
    unit_file: str = "disabled",
    load: str = "loaded",
    realtime: int = 0,
    monotonic: int = 0,
) -> dict[str, object]:
    return {
        "LoadState": load,
        "UnitFileState": unit_file,
        "ActiveState": active,
        "SubState": sub,
        "NextElapseUSecRealtime": realtime,
        "NextElapseUSecMonotonic": monotonic,
        "NeedDaemonReload": False,
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x")
        self.addCleanup(self.fixture.close)
        self.seat = self.fixture.seat_root
        self.fake = self.fixture.user_manager
        self.clock = FakeClock()
        self.link = _consume_timer_enablement_link_path(self.seat, TIMER)
        self.canonical = _consume_timer_template_path(self.seat, TIMER)

    # -- helpers --------------------------------------------------------

    def engine(self, **overrides):
        values = dict(clock=self.clock, sleep=self.clock.sleep)
        values.update(overrides)
        return self.fixture.engine(**values)

    def run_ops(self, *operations, kind="apply", engine=None):
        spec = fx.parsed(self.fixture, tuple(operations), execution_kind=kind)
        return (engine or self.engine()).run(spec)

    def write_calls(self) -> list[str]:
        return [call for call in self.fake.calls if call.startswith(WRITE_CALLS)]

    def add_framework_entrypoints(self) -> None:
        framework = self.fixture.framework_root
        for relative, data in (
            ("tools/persona_bringup.py", b"# framework diagnose entrypoint\n"),
            ("instance.yaml", b"instance: fixture\n"),
        ):
            path = framework / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        fx._tighten(framework)

    def make_link(self, target: Path | None = None) -> None:
        self.link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target or self.canonical), self.link)

    def unit_fire_op(self, **desired_overrides: str):
        seconds = _unit_fire_timeout_seconds(candidate_count=1, post_start_count=0)
        desired = {
            "timer": TIMER,
            "timer_state": "disabled",
            "result": "success",
            "exec_main_status": "0",
            "unit_fire_timeout_seconds": str(seconds),
            "unit_fire_timeout_candidate_count": "1",
            "unit_fire_timeout_post_start_count": "0",
            "unit_fire_timeout_formula": UNIT_FIRE_TIMEOUT_FORMULA,
            "account_home": str(self.seat),
            "persona_state_root": str(self.seat / ".local/state/pantheon/personas"),
            "allowed_runtime_refs": '["claude/interactive-subscription"]',
            "require_invocation_receipt": "true",
            "invocation_receipt_schema": "pantheon-runtime-initial-viability-invocation/v1",
            "receiver_receipt_required": "false",
            "receiver_operations_by_runtime": "{}",
        }
        desired.update(desired_overrides)
        operation = fx.op("connective.unit-fire", "unit-fire", SERVICE, desired)
        return dataclasses.replace(operation, provenance={"persona": PERSONA})

    def timer_enable_op(self):
        return fx.op(
            "connective.timer-enable",
            "timer-enable",
            TIMER,
            {
                "after": "direct-fire,unit-fire,mailbox-probe",
                "enablement_link_path": str(self.link),
                "canonical_template_path": str(self.canonical),
            },
        )

    def timer_observe_op(self):
        return fx.op(
            "connective.timer-observe",
            "timer-observe",
            TIMER,
            {"service": SERVICE, "next_elapse": "finite-after-inactive"},
        )

    def absence_op(self):
        return fx.op(
            "connective.timer-enablement-absent",
            "timer-enablement-absence-check",
            str(self.link),
            {"timer": TIMER, "canonical_template_path": str(self.canonical), "state": "absent"},
        )

    def stale_op(self, unit: str = SERVICE):
        return fx.op(
            "connective.stale-unit-failure-reset",
            "stale-unit-failure-reset",
            unit,
            {"load_state": "not-found", "active_state": "inactive", "sub_state": "dead"},
        )

    def reload_op(self, unit: str = RECEIVER):
        return fx.op(
            "codex-telegram.daemon-reload",
            "daemon-reload",
            fx.ACCOUNT,
            {"unit": unit, "start": "forbidden", "enable": "forbidden"},
        )

    def direct_fire_op(self):
        return fx.op(
            "harness.direct-fire",
            "direct-fire",
            PERSONA,
            {"harness": "codex", "manifest": "/seat/checkout/instance.yaml", "repo": "/seat/checkout"},
        )

    def disabled_first_op(self):
        return fx.op(
            "codex-telegram.disabled-first",
            "codex-telegram-disabled-first",
            RECEIVER,
            {"active": "inactive", "enabled": "disabled", "environment_file": "forbidden"},
        )

    def converged_fake(self) -> None:
        self.fake.units[SERVICE] = _service_state(exec_status=5, policy=([3, 4, 5], []), invocation=INVOCATION)
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=12345)
        self.fake.enablement[TIMER] = "enabled"


# ---------------------------------------------------------------------------
# parse_spec: admission, allowlist, budgets
# ---------------------------------------------------------------------------


class AdmissionTests(_Case):
    def test_each_activation_kind_is_admitted_for_apply_and_verify(self) -> None:
        for operation in (
            self.reload_op(),
            self.stale_op(),
            self.direct_fire_op(),
            self.unit_fire_op(),
            self.timer_enable_op(),
            self.timer_observe_op(),
            self.disabled_first_op(),
        ):
            for kind in ("apply", "verify"):
                with self.subTest(kind=operation.kind, execution=kind):
                    spec = fx.parsed(self.fixture, (operation,), execution_kind=kind)
                    self.assertEqual([op.kind for op in spec.operations], [operation.kind])

    def test_unit_outside_the_allowlist_refuses_the_spec(self) -> None:
        for operation in (
            dataclasses.replace(self.unit_fire_op(), target="evil.service"),
            self.unit_fire_op(timer="evil.timer"),
            self.stale_op("evil.service"),
            self.reload_op("dbus.service"),
            dataclasses.replace(self.timer_observe_op(), desired={"service": "evil.service"}),
            dataclasses.replace(self.disabled_first_op(), target="sshd.service"),
        ):
            with self.subTest(kind=operation.kind):
                with self.assertRaises(InvalidSpec) as caught:
                    fx.parsed(self.fixture, (operation,))
                self.assertIn("allowlist", str(caught.exception))

    def test_allowlist_shapes(self) -> None:
        # review M1: per (kind, field) shapes
        ok = engine_module.unit_shape_ok
        roles = engine_module.UNIT_ROLES
        self.assertTrue(ok(SERVICE, roles[("unit-fire", "target")]))
        self.assertTrue(ok(TIMER, roles[("unit-fire", "timer")]))
        self.assertTrue(ok("pantheon-persona-consume@recovery-2.timer", roles[("timer-enable", "target")]))
        self.assertTrue(ok(RECEIVER, roles[("codex-telegram-disabled-first", "target")]))
        self.assertTrue(ok(RECEIVER, roles[("daemon-reload", "unit")]))
        for kind_field in (
            ("unit-fire", "target"), ("unit-fire", "timer"), ("stale-unit-failure-reset", "target"),
            ("timer-enable", "target"), ("timer-observe", "target"), ("timer-observe", "service"),
        ):
            with self.subTest(role=kind_field):
                self.assertFalse(ok(RECEIVER, roles[kind_field]))
        self.assertFalse(ok(SERVICE, roles[("daemon-reload", "unit")]))
        self.assertFalse(ok(TIMER, roles[("unit-fire", "target")]))
        for unit in ("evil.service", "pantheon-persona-consume@.timer", "pantheon-persona-consume@A.timer",
                     "codex-telegram-poller.timer", "pantheon-persona-consume@x.service.d", ""):
            for shapes in roles.values():
                self.assertFalse(ok(unit, shapes), unit)

    def test_mailbox_probe_is_unsupported_under_the_pre_flip_table(self) -> None:
        # MR-2 review (MEDIUM): the 5c "still Unsupported" pin, kept against
        # the PRE-flip table so the executor-side B4 gate stays tested
        from tools.test_host_control_driver import pre_flip_table

        with pre_flip_table():
            for kind in ("apply", "verify"):
                with self.subTest(execution=kind), self.assertRaises(Unsupported):
                    fx.parsed(self.fixture, (fx.op("mbox", "mailbox-probe", "t"),), execution_kind=kind)

    def test_mailbox_probe_is_implemented_for_apply_and_verify(self) -> None:
        # T-2917 5d.3 (the capability flip, T-2917:D22) replaces the 5c
        # "mailbox.probe is still Unsupported" pin
        for kind in ("apply", "verify", "reconcile"):
            for op_kind in ("mailbox-probe", "mailbox-writable-root-check"):
                with self.subTest(execution=kind, op=op_kind):
                    parsed = fx.parsed(self.fixture, (fx.op("mbox", op_kind, "t"),), execution_kind=kind)
                    self.assertEqual(parsed.execution_kind, kind)


class BudgetTests(_Case):
    def test_budget_constants_and_ordering(self) -> None:
        from tools.host_control.executor.wire import EXIT_HARD_DEADLINE, HARD_DEADLINE_GRACE_SECONDS

        self.assertEqual(EXECUTOR_RUN_BUDGET_SECONDS, 3300)
        self.assertEqual(driver_module.DEFAULT_COLLECT_TIMEOUT, 3600)
        self.assertGreater(driver_module.DEFAULT_COLLECT_TIMEOUT, EXECUTOR_RUN_BUDGET_SECONDS)
        # T-2917 5d.1 (T-2917:D11): the watchdog fires before the broker
        # gives up collecting: budget + grace < collect timeout
        self.assertEqual((HARD_DEADLINE_GRACE_SECONDS, EXIT_HARD_DEADLINE), (120.0, 70))
        self.assertLess(EXECUTOR_RUN_BUDGET_SECONDS + HARD_DEADLINE_GRACE_SECONDS, driver_module.DEFAULT_COLLECT_TIMEOUT)

    def test_engine_run_budget_is_capped(self) -> None:
        self.assertEqual(self.engine(run_budget=99_999)._run_budget, EXECUTOR_RUN_BUDGET_SECONDS)
        self.assertEqual(self.engine(run_budget=10)._run_budget, 10)

    def test_exhausted_budget_fails_the_next_operation(self) -> None:
        result = self.run_ops(self.reload_op(), engine=self.engine(run_budget=0))
        self.assertFalse(result.ok)
        self.assertIn("budget exhausted", result.evidence[-1].detail)
        self.assertEqual(self.write_calls(), [])

    def test_unit_fire_longer_than_the_run_budget_is_refused_before_any_write(self) -> None:
        self.fake.units[SERVICE] = _service_state()
        result = self.run_ops(self.unit_fire_op(), engine=self.engine(run_budget=100))
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("budget", result.evidence[0].detail)
        self.assertEqual(self.write_calls(), [])


# ---------------------------------------------------------------------------
# the apply pre-pass
# ---------------------------------------------------------------------------


class PrePassTests(_Case):
    def assert_refused(self, result) -> None:
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertEqual(len(result.evidence), 1)
        self.assertIn("pre-pass", result.evidence[0].detail)
        self.assertEqual(result.evidence[0].evidence.get("phase"), "pre-pass")
        self.assertIn("nothing was changed", result.detail)
        self.assertEqual(self.write_calls(), [])
        self.assertFalse((self.seat / "made-by-apply").exists())

    def mkdir_op(self):
        return fx.op("seat.mkdir", "mkdir", str(self.seat / "made-by-apply"), {"mode": "0755"})

    def test_manager_unreachable(self) -> None:
        self.fake.reachable = False
        self.assert_refused(self.run_ops(self.mkdir_op(), self.reload_op()))
        self.assertIn("user manager unavailable", self.run_ops(self.reload_op()).evidence[0].detail)

    def test_unit_fire_with_timer_already_enabled_and_no_legacy_convergence(self) -> None:
        self.fake.enablement[TIMER] = "enabled"
        self.fake.units[SERVICE] = _service_state()
        result = self.run_ops(self.mkdir_op(), self.unit_fire_op())
        self.assert_refused(result)
        self.assertIn("already enabled", result.evidence[0].detail)

    def test_timer_enable_with_a_stray_link(self) -> None:
        self.fake.units[TIMER] = _timer_state()
        self.make_link(self.seat / "elsewhere.timer")
        result = self.run_ops(self.mkdir_op(), self.timer_enable_op())
        self.assert_refused(result)
        self.assertIn("link already exists", result.evidence[0].detail)

    def test_disabled_first_receiver_enabled(self) -> None:
        self.fake.enablement[RECEIVER] = "enabled"
        self.fake.units[RECEIVER] = _service_state()
        result = self.run_ops(self.mkdir_op(), self.disabled_first_op())
        self.assert_refused(result)
        self.assertIn("enabled before cutover", result.evidence[0].detail)

    def test_direct_fire_without_framework_entrypoint(self) -> None:
        result = self.run_ops(self.mkdir_op(), self.direct_fire_op())
        self.assert_refused(result)
        self.assertIn("framework copy lacks", result.evidence[0].detail)

    def test_verify_has_no_pre_pass(self) -> None:
        self.fake.reachable = False
        result = self.run_ops(self.reload_op(), kind="verify")
        self.assertFalse(result.ok)
        self.assertNotIn("pre-pass", result.evidence[0].detail)


# ---------------------------------------------------------------------------
# daemon-reload and stale-unit-failure-reset
# ---------------------------------------------------------------------------


class ReloadAndStaleTests(_Case):
    def test_daemon_reload_applies_through_the_port(self) -> None:
        result = self.run_ops(self.reload_op())
        self.assertTrue(result.ok, result.evidence)
        self.assertIn("reload:", self.fake.calls)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["reload_ok"], "true")
        self.assertEqual(evidence["user_manager_transport"], "fake")
        self.assertEqual(evidence["user_manager_address_source"], "test-override")
        self.assertEqual(result.mutations[0].before, {"state": "not-applicable"})
        self.assertFalse(result.mutations[0].changed)

    def test_daemon_reload_refusal_fails_with_the_error_name(self) -> None:
        self.fake.refuse["reload"] = "org.freedesktop.systemd1.AccessDenied"
        result = self.run_ops(self.reload_op())
        self.assertFalse(result.ok)
        self.assertEqual(result.evidence[0].evidence["error_name"], "org.freedesktop.systemd1.AccessDenied")
        self.assertEqual(result.mutations, ())

    def clean_stale(self) -> dict[str, object]:
        return _service_state(load="not-found")

    def test_stale_clean_noop(self) -> None:
        self.fake.units[SERVICE] = self.clean_stale()
        result = self.run_ops(self.stale_op())
        self.assertTrue(result.ok, result.evidence)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "noop")
        self.assertIn("pre_snapshot_sha256", evidence)
        self.assertEqual(evidence["pre_snapshot_encoding"], "typed-canonical-json")
        self.assertFalse(any("stdout" in key for key in evidence))
        self.assertEqual(result.mutations[0].before, {"state": "clean-noop"})
        self.assertEqual(result.mutations[0].after_fingerprint, "evidence-only")
        self.assertEqual(self.write_calls(), [])

    def test_stale_reset_required_resets_only_the_plan_named_unit(self) -> None:
        self.fake.units[SERVICE] = _service_state(
            load="not-found", active="failed", sub="failed", result="exit-code", exec_status=1, invocation=INVOCATION
        )

        def on_call(fake, name, args):
            if name == "reset_failed_unit":
                fake.units[args[0]] = self.clean_stale()

        self.fake.on_call = on_call
        result = self.run_ops(self.stale_op())
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(self.write_calls(), [f"reset_failed_unit:{SERVICE}"])
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "reset")
        self.assertIn("post_snapshot_sha256", evidence)
        self.assertFalse(any("stdout" in key for key in evidence))
        self.assertEqual(result.mutations[0].before, {"state": "reset-required"})

    def test_stale_residual_state_fails(self) -> None:
        self.fake.units[SERVICE] = _service_state(
            load="not-found", active="failed", sub="failed", result="exit-code", exec_status=1, invocation=INVOCATION
        )
        result = self.run_ops(self.stale_op())
        self.assertFalse(result.ok)
        self.assertIn("residual state", result.evidence[0].detail)
        self.assertEqual(result.evidence[0].evidence["classification"], "refused")

    def test_stale_loaded_service_refused_without_reset(self) -> None:
        self.fake.units[SERVICE] = _service_state(load="loaded")
        result = self.run_ops(self.stale_op())
        self.assertFalse(result.ok)
        self.assertIn("pre-existing loaded service", result.evidence[0].detail)
        self.assertEqual(self.write_calls(), [])


# ---------------------------------------------------------------------------
# direct-fire
# ---------------------------------------------------------------------------


class DirectFireTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.add_framework_entrypoints()
        self.children: list[tuple[list[str], dict]] = []
        self.returncode = 0

    def run_child(self, argv, **kwargs):
        self.children.append((list(argv), kwargs))
        return SimpleNamespace(returncode=self.returncode, stdout='{"ok": true}\n', stderr="")

    def test_argv_uses_the_injected_python_and_the_framework_copy(self) -> None:
        engine = self.engine(run_child=self.run_child, python="/fake/python3")
        result = self.run_ops(self.direct_fire_op(), engine=engine)
        self.assertTrue(result.ok, result.evidence)
        argv, kwargs = self.children[0]
        framework = self.fixture.framework_root
        self.assertEqual(
            argv,
            [
                "/fake/python3",
                "-I",
                str(framework / "tools/persona_bringup.py"),
                "diagnose",
                "--manifest",
                str(framework / "instance.yaml"),
                "--persona",
                PERSONA,
                "--harness",
                "codex",
            ],
        )
        self.assertNotIn("/seat/checkout", " ".join(argv))
        self.assertEqual(kwargs["cwd"], str(framework))
        self.assertLessEqual(kwargs["timeout"], engine_module.DIRECT_FIRE_TIMEOUT_SECONDS)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["observation"], "fire")
        self.assertEqual(evidence["plan_repo_used"], "false")
        self.assertEqual(evidence["interpreter"], "sys.executable -I")

    def test_default_interpreter_is_sys_executable(self) -> None:
        import sys

        self.run_ops(self.direct_fire_op(), engine=self.engine(run_child=self.run_child))
        self.assertEqual(self.children[0][0][0], sys.executable)

    def test_nonzero_exit_fails(self) -> None:
        self.returncode = 1
        result = self.run_ops(self.direct_fire_op(), engine=self.engine(run_child=self.run_child))
        self.assertFalse(result.ok)
        self.assertEqual(result.evidence[0].evidence["returncode"], "1")
        self.assertEqual(result.mutations, ())

    def test_bad_harness_or_persona_refused(self) -> None:
        for desired_harness, target in (("bash", PERSONA), ("codex", "../etc")):
            with self.subTest(harness=desired_harness, target=target):
                operation = dataclasses.replace(
                    self.direct_fire_op(), target=target, desired={"harness": desired_harness}
                )
                result = self.run_ops(operation, engine=self.engine(run_child=self.run_child))
                self.assertFalse(result.ok)
        self.assertEqual(self.children, [])

    def test_verify_reruns_diagnose(self) -> None:
        result = self.run_ops(self.direct_fire_op(), kind="verify", engine=self.engine(run_child=self.run_child))
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.evidence[0].evidence["observation"], "rerun-diagnose")
        self.assertEqual(result.mutations, ())
        self.assertEqual(len(self.children), 1)


# ---------------------------------------------------------------------------
# unit-fire
# ---------------------------------------------------------------------------


RECEIPT = {
    "selected_runtime": "claude/interactive-subscription",
    "selected_harness": "claude",
    "selected_profile": "default",
    "provider_completion_status": "completed",
    "invocation_receipt_sha256": "f" * 64,
}


class _UnitFireBase(_Case):
    def setUp(self) -> None:
        super().setUp()
        (self.seat / ".local/state/pantheon/personas").mkdir(parents=True)
        self.fake.units[SERVICE] = _service_state()
        self.fake.units[TIMER] = _timer_state()
        patcher = mock.patch(
            "tools.runtime_invocation_receipt.validate_initial_viability_invocation_receipt",
            return_value=RECEIPT,
        )
        self.validate = patcher.start()
        self.addCleanup(patcher.stop)

    def script(self, *, capture: dict[str, object], completion: dict[str, object]) -> None:
        """start_unit -> the next poll reads ``capture``; the poll after that
        reads ``completion``."""

        state = {"started": False, "stopped": False, "reads": 0}

        def on_call(fake, name, args):
            if name == "start_unit":
                state["started"] = True
                fake.units[SERVICE] = capture
            elif name == "stop_unit":
                state["stopped"] = True
                fake.units[SERVICE] = _service_state()
            elif (
                name == "read_unit"
                and state["started"]
                and not state["stopped"]
                and args[1] == "UNIT_FIRE_POLL"
            ):
                state["reads"] += 1
                if state["reads"] >= 2:
                    fake.units[SERVICE] = completion

        self.fake.on_call = on_call


class UnitFireTests(_UnitFireBase):
    def test_happy_path(self) -> None:
        self.script(
            capture=_service_state(active="activating", sub="start", invocation=INVOCATION, job=(7, "/j/7")),
            completion=_service_state(invocation=INVOCATION),
        )
        result = self.run_ops(self.unit_fire_op())
        self.assertTrue(result.ok, result.evidence)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["invocation_id"], INVOCATION_HEX)
        self.assertEqual(evidence["post_exit_invocation_id_state"], "same")
        self.assertEqual(evidence["capture_active_state"], "activating")
        self.assertEqual(evidence["user_manager_address_source"], "test-override")
        self.assertEqual(evidence["initial_viability_receipt_sha256"], "f" * 64)
        self.assertEqual(self.write_calls(), [f"start_unit:{SERVICE},wait=False"])
        self.assertEqual(self.validate.call_args.kwargs["invocation_id"], INVOCATION_HEX)
        self.assertEqual(result.mutations[0].before, {"state": "not-applicable"})

    def test_in_policy_exit_five(self) -> None:
        self.script(
            capture=_service_state(active="active", sub="running", invocation=INVOCATION, policy=([3, 4, 5], [])),
            completion=_service_state(exec_status=5, policy=([3, 4, 5], [])),
        )
        result = self.run_ops(self.unit_fire_op())
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.evidence[0].evidence["exec_main_status"], "5")
        self.assertEqual(result.evidence[0].evidence["success_exit_status"], "3 4 5")
        self.assertEqual(result.evidence[0].evidence["post_exit_invocation_id_state"], "empty")

    def test_not_idle_before_enqueue_never_starts(self) -> None:
        self.fake.units[SERVICE] = _service_state(active="active", sub="running", invocation=INVOCATION)
        result = self.run_ops(self.unit_fire_op())
        self.assertFalse(result.ok)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "pre-existing-invocation")
        self.assertEqual(evidence["cleanup_state"], "not-owned")
        self.assertEqual(self.write_calls(), [])

    def test_failed_state_cleans_up_once(self) -> None:
        self.script(
            capture=_service_state(active="failed", sub="failed", result="exit-code", exec_status=1),
            completion=_service_state(),
        )
        result = self.run_ops(self.unit_fire_op())
        self.assertFalse(result.ok)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "unit-failed")
        self.assertEqual(evidence["cleanup_state"], "inactive")
        self.assertEqual(self.write_calls().count(f"stop_unit:{SERVICE}"), 1)
        self.assertEqual(evidence["user_manager_address_source"], "test-override")

    def test_malformed_invocation_is_refused_and_cleaned_up(self) -> None:
        malformed = b"\x01" * 16  # valid length but hex "0101..." is all digits: not lowercase
        self.script(
            capture=_service_state(active="activating", sub="start", invocation=malformed),
            completion=_service_state(),
        )
        result = self.run_ops(self.unit_fire_op())
        self.assertFalse(result.ok)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "malformed-invocation-id")
        self.assertEqual(evidence["cleanup_state"], "inactive")
        self.assertEqual(self.write_calls().count(f"stop_unit:{SERVICE}"), 1)

    def test_start_refusal_cleans_up(self) -> None:
        self.fake.refuse["start_unit"] = "org.freedesktop.systemd1.NoSuchUnit"
        result = self.run_ops(self.unit_fire_op())
        self.assertFalse(result.ok)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "start-failed")
        self.assertEqual(self.write_calls().count(f"stop_unit:{SERVICE}"), 1)

    def test_capture_timeout_uses_fake_time(self) -> None:
        self.script(capture=_service_state(active="activating", sub="start"), completion=_service_state(active="activating", sub="start"))
        result = self.run_ops(self.unit_fire_op())
        self.assertFalse(result.ok)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "capture-timeout")
        self.assertEqual(evidence["cleanup_state"], "inactive")
        self.assertTrue(self.clock.sleeps)

    def test_cleanup_that_cannot_prove_inactive_is_unsafe(self) -> None:
        def stuck(fake, name, args):
            if name == "start_unit":
                fake.units[SERVICE] = _service_state(active="activating", sub="start")

        self.fake.on_call = stuck
        result = self.run_ops(self.unit_fire_op())
        self.assertFalse(result.ok)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["classification"], "unsafe-cleanup")
        self.assertEqual(evidence["cleanup_state"], "unsafe-active")
        self.assertEqual(self.write_calls().count(f"stop_unit:{SERVICE}"), 1)

    def test_account_home_outside_the_seat_is_refused(self) -> None:
        result = self.run_ops(self.unit_fire_op(account_home="/elsewhere", persona_state_root="/elsewhere/s"))
        self.assertFalse(result.ok)
        self.assertEqual(self.write_calls(), [])


# ---------------------------------------------------------------------------
# timer-enable
# ---------------------------------------------------------------------------


class TimerEnableTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.fake.units[TIMER] = _timer_state()

    def enable_creates_link(self, fake, name, args) -> None:
        if name == "enable_now":
            self.make_link()
            fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=5)

    def assert_owned(self, mutation) -> None:
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.before["owned"], "true")
        self.assertEqual(mutation.before["activation_intent"], "false")
        self.assertEqual(mutation.before["state"], "disabled")
        self.assertEqual(mutation.before["enablement_link_path"], str(self.link))
        self.assertEqual(mutation.after_fingerprint, "timer-enabled")

    def test_enable_returns_an_owned_mutation(self) -> None:
        self.fake.on_call = self.enable_creates_link
        result = self.run_ops(self.timer_enable_op())
        self.assertTrue(result.ok, result.evidence)
        self.assert_owned(result.mutations[0])
        self.assertEqual(result.mutations[0].before["enablement_link_actual_readlink"], str(self.canonical))
        self.assertEqual(self.write_calls(), [f"enable_now:{TIMER}"])
        self.assertEqual(result.evidence[0].evidence["enable_command_issued"], "true")

    def test_refused_enable_still_returns_the_owned_mutation(self) -> None:
        self.fake.refuse["enable_now"] = "org.freedesktop.systemd1.AccessDenied"
        result = self.run_ops(self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assertEqual(len(result.mutations), 1)
        self.assert_owned(result.mutations[0])
        self.assertEqual(result.evidence[0].evidence["enable_error_name"], "org.freedesktop.systemd1.AccessDenied")

    def test_raising_enable_still_returns_the_owned_mutation(self) -> None:
        def explode(fake, name, args):
            if name == "enable_now":
                raise RuntimeError("bus went away mid-call")

        self.fake.on_call = explode
        result = self.run_ops(self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assertEqual(len(result.mutations), 1)
        self.assert_owned(result.mutations[0])
        self.assertIn("timer enable call failed", result.evidence[0].detail)

    def test_enable_without_a_canonical_link_fails_but_keeps_the_record(self) -> None:
        result = self.run_ops(self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assert_owned(result.mutations[0])
        self.assertIn("did not resolve to the canonical template", result.evidence[0].detail)

    def test_already_enabled_without_legacy_seed_fails_without_enable(self) -> None:
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=5)
        result = self.run_ops(self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("already enabled", result.evidence[0].detail)
        self.assertEqual(self.write_calls(), [])

    def test_not_loaded_timer_is_refused(self) -> None:
        self.fake.units[TIMER] = _timer_state(load="not-found", unit_file="")
        result = self.run_ops(self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assertEqual(self.write_calls(), [])

    def test_declared_paths_must_match_the_seat_derived_paths(self) -> None:
        operation = dataclasses.replace(
            self.timer_enable_op(),
            desired={"enablement_link_path": str(self.seat / "x.timer"), "canonical_template_path": str(self.canonical)},
        )
        result = self.run_ops(operation)
        self.assertFalse(result.ok)
        self.assertEqual(self.write_calls(), [])


# ---------------------------------------------------------------------------
# timer-observe
# ---------------------------------------------------------------------------


class TimerObserveTests(_UnitFireBase):
    def test_without_mailbox_evidence_fails(self) -> None:
        self.script(
            capture=_service_state(active="activating", sub="start", invocation=INVOCATION),
            completion=_service_state(invocation=INVOCATION),
        )
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=99)
        result = self.run_ops(self.unit_fire_op(), self.timer_observe_op())
        self.assertFalse(result.ok)
        self.assertTrue(result.evidence[0].ok, result.evidence[0])
        self.assertIn("lacks correlated durable mailbox-probe evidence", result.evidence[1].detail)

    def test_without_unit_fire_evidence_fails(self) -> None:
        result = self.run_ops(self.timer_observe_op())
        self.assertFalse(result.ok)
        self.assertIn("lacks disabled first-fire evidence", result.evidence[0].detail)
        self.assertEqual(self.write_calls(), [])


# ---------------------------------------------------------------------------
# B9: timer-enablement-absence-check with a present link
# ---------------------------------------------------------------------------


class LegacyConvergenceTests(_UnitFireBase):
    def test_converged_legacy_is_admitted_and_seeds_the_run(self) -> None:
        self.converged_fake()
        self.make_link()
        result = self.run_ops(self.absence_op(), self.unit_fire_op(), self.timer_observe_op())
        self.assertTrue(result.ok, result.evidence)
        absent, unit_fire, observe = result.evidence
        self.assertEqual(absent.evidence["classification"], "converged-legacy")
        self.assertEqual(absent.evidence["user_manager_address_source"], "test-override")
        self.assertEqual(unit_fire.evidence["post_exit_invocation_id_state"], "legacy-unknown")
        self.assertEqual(unit_fire.evidence["exec_main_status"], "5")
        self.assertEqual(unit_fire.evidence["success_exit_status"], "3 4 5")
        self.assertEqual(observe.evidence["next_elapse_basis"], "monotonic")
        self.assertEqual(observe.evidence["mailbox_effect_correlated"], "legacy-unknown")
        self.assertEqual(self.write_calls(), [])

    def test_absent_link_passes_without_any_port_call(self) -> None:
        result = self.run_ops(self.absence_op())
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.evidence[0].evidence["link_state"], "absent")
        self.assertEqual(self.fake.calls, [])

    def test_non_converged_fails_closed(self) -> None:
        for mutate in (
            lambda: self.fake.units.__setitem__(TIMER, _timer_state(active="active", sub="running", unit_file="enabled", monotonic=1)),
            lambda: self.fake.units.__setitem__(TIMER, _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=2**64 - 1)),
            lambda: self.fake.units.__setitem__(SERVICE, _service_state(exec_status=1, policy=([3, 4, 5], []))),
            lambda: self.fake.units.__setitem__(SERVICE, _service_state(result="exit-code")),
        ):
            with self.subTest():
                self.converged_fake()
                mutate()
                if not self.link.is_symlink():
                    self.make_link()
                result = self.run_ops(self.absence_op(), kind="verify")
                # verify skips staged-cutover scope only; fx.op uses scope "all"
                self.assertFalse(result.ok)
                self.assertEqual(result.evidence[0].evidence["classification"], "conflicting")

    def test_non_canonical_link_fails_closed(self) -> None:
        self.converged_fake()
        self.make_link(self.seat / "elsewhere.timer")
        result = self.run_ops(self.absence_op(), kind="verify")
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.evidence[0].detail)


# ---------------------------------------------------------------------------
# codex-telegram-disabled-first (diagnostics.read) and D4
# ---------------------------------------------------------------------------


class DisabledFirstTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.fake.units[RECEIVER] = _service_state()

    def test_is_diagnostics_read(self) -> None:
        from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, OUT_OF_ENVELOPE_KINDS

        self.assertEqual(EXECUTOR_KIND_CLASSES["codex-telegram-disabled-first"], "diagnostics.read")
        self.assertNotIn("codex-telegram-disabled-first", OUT_OF_ENVELOPE_KINDS)

    def test_disabled_inactive_receiver_passes(self) -> None:
        result = self.run_ops(self.disabled_first_op())
        self.assertTrue(result.ok, result.evidence)
        evidence = result.evidence[0].evidence
        self.assertEqual(evidence["enabled"], "disabled")
        self.assertEqual(evidence["main_pid"], "0")
        self.assertEqual(evidence["environment_file"], "absent")
        self.assertEqual(self.write_calls(), [])

    def test_active_receiver_refused(self) -> None:
        state = _service_state(active="active", sub="running")
        state["MainPID"] = 4242
        self.fake.units[RECEIVER] = state
        result = self.run_ops(self.disabled_first_op(), kind="verify")
        self.assertFalse(result.ok)
        self.assertIn("active before cutover", result.evidence[0].detail)

    def test_d4_environment_file_refused(self) -> None:
        self.fake.environment_files[RECEIVER] = True
        result = self.run_ops(self.disabled_first_op(), kind="verify")
        self.assertFalse(result.ok)
        self.assertIn("must not embed an EnvironmentFile", result.evidence[0].detail)
        self.assertEqual(result.evidence[0].evidence["environment_file"], "declared")

    def test_d4_difference_is_documented_in_code(self) -> None:
        user_bus = (REPO_ROOT / "tools/host_control/executor/user_bus.py").read_text(encoding="utf-8")
        docstring = ast.get_docstring(ast.parse(user_bus)) or ""
        self.assertIn("D4", docstring)
        self.assertIn("EnvironmentFiles", docstring)
        self.assertIn("comment", docstring)
        engine_text = (REPO_ROOT / "tools/host_control/executor/engine.py").read_text(encoding="utf-8")
        self.assertIn("D4", engine_text)


# ---------------------------------------------------------------------------
# Verify: read-only observations, no mutation, no write calls
# ---------------------------------------------------------------------------


class VerifyReadOnlyTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.add_framework_entrypoints()
        self.fake.units[SERVICE] = _service_state(load="not-found")
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=7)
        self.fake.units[RECEIVER] = _service_state()
        self.make_link()

    def run_child(self, argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_each_activation_kind_verifies_read_only(self) -> None:
        engine = self.engine(run_child=self.run_child)
        for operation in (
            self.reload_op(),
            self.stale_op(),
            self.direct_fire_op(),
            self.unit_fire_op(),
            self.timer_enable_op(),
            self.timer_observe_op(),
            self.disabled_first_op(),
        ):
            with self.subTest(kind=operation.kind):
                self.fake.calls.clear()
                result = self.run_ops(operation, kind="verify", engine=engine)
                self.assertEqual(result.mutations, ())
                self.assertEqual(self.write_calls(), [])
                self.assertEqual(len(result.evidence), 1)
                self.assertTrue(result.evidence[0].ok, (operation.kind, result.evidence[0]))

    def test_verify_daemon_reload_reports_pending_reload(self) -> None:
        state = _service_state()
        state["NeedDaemonReload"] = True
        self.fake.units[RECEIVER] = state
        result = self.run_ops(self.reload_op(), kind="verify")
        self.assertFalse(result.ok)
        self.assertEqual(result.evidence[0].evidence["observation"], "need-daemon-reload")


# ---------------------------------------------------------------------------
# __main__ wiring (static)
# ---------------------------------------------------------------------------


class EntrypointWiringTests(unittest.TestCase):
    def test_main_builds_dbus_user_manager_with_only_the_uid(self) -> None:
        path = REPO_ROOT / "tools/host_control/executor/__main__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "DBusUserManager"
        ]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.args, [])
        self.assertEqual([keyword.arg for keyword in call.keywords], ["uid"])
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        self.assertNotIn("UnavailableUserManager", names)


# ---------------------------------------------------------------------------
# 5c.2 review fixes: M1 per-kind allowlist, M2 isolated child, L1, L4
# ---------------------------------------------------------------------------


class ReceiverRefusalTests(_UnitFireBase):
    """Review M1: the codex receiver is reachable only by disabled-first and
    the daemon-reload unit read; every persona kind refuses it at parse time
    AND (for a spec that slipped past parse) at call time, with no port call."""

    def receiver_ops(self):
        unit_fire = self.unit_fire_op()
        return {
            "unit-fire.target": dataclasses.replace(unit_fire, target=RECEIVER),
            "unit-fire.timer": self.unit_fire_op(timer=RECEIVER),
            "stale-unit-failure-reset.target": self.stale_op(RECEIVER),
            "timer-observe.target": dataclasses.replace(self.timer_observe_op(), target=RECEIVER),
            "timer-observe.service": fx.op(
                "connective.timer-observe", "timer-observe", TIMER, {"service": RECEIVER}
            ),
            "timer-enable.target": dataclasses.replace(self.timer_enable_op(), target=RECEIVER),
        }

    def test_parse_refuses_the_receiver_for_persona_kinds(self) -> None:
        for name, operation in self.receiver_ops().items():
            with self.subTest(role=name), self.assertRaises(InvalidSpec) as caught:
                fx.parsed(self.fixture, (operation,))
            self.assertIn("per-kind unit allowlist", str(caught.exception))
        self.assertEqual(self.fake.calls, [])

    def test_call_time_refuses_the_receiver_with_no_port_call(self) -> None:
        # bypass parse_spec: hand the engine a ParsedSpec directly
        from tools.host_control.executor.engine import ParsedSpec

        for name, operation in self.receiver_ops().items():
            with self.subTest(role=name):
                self.fake.calls.clear()
                spec = ParsedSpec(operation_handle=fx.OPERATION_HANDLE, execution_kind="apply",
                                  spec_digest="0" * 64, operations=(operation,))
                result = self.engine().run(spec)
                self.assertFalse(result.ok)
                self.assertIn("UnitRefused", result.evidence[0].detail)
                touching = [call for call in self.fake.calls if RECEIVER in call]
                self.assertEqual(touching, [], name)
                self.assertEqual(self.write_calls(), [])

    def test_receiver_stays_allowed_for_disabled_first_and_reload_read(self) -> None:
        fx.parsed(self.fixture, (self.disabled_first_op(), self.reload_op()))
        with self.assertRaises(InvalidSpec):
            fx.parsed(self.fixture, (self.reload_op(SERVICE),))


class DirectFireIsolationTests(_Case):
    """Review M2: ``-I`` and an explicit minimal environment."""

    def setUp(self) -> None:
        super().setUp()
        self.add_framework_entrypoints()
        self.children: list = []

        def run_child(argv, **kwargs):
            self.children.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        self.run_child = run_child

    def test_argv1_is_isolated_mode_and_env_is_exact(self) -> None:
        self.run_ops(self.direct_fire_op(), engine=self.engine(run_child=self.run_child, python="/fake/python3"))
        argv, kwargs = self.children[0]
        self.assertEqual(argv[1], "-I")
        self.assertEqual(sorted(kwargs["env"]), ["HOME", "LANG", "PATH"])
        self.assertEqual(kwargs["env"]["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(kwargs["env"]["HOME"], str(self.seat))
        self.assertEqual(kwargs["env"]["LANG"], "C.UTF-8")

    def test_nothing_is_inherited_from_the_executor_environment(self) -> None:
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil.py",
                                          "DBUS_SESSION_BUS_ADDRESS": "unix:path=/decoy"}):
            self.run_ops(self.direct_fire_op(), engine=self.engine(run_child=self.run_child))
        env = self.children[0][1]["env"]
        self.assertFalse(any(key.startswith("PYTHON") for key in env))
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertNotIn("/evil", " ".join(env.values()))


class DirectFireRealEntrypointTests(_Case):
    """Review M2 precondition: the REAL persona_bringup.py diagnose runs
    under ``-I`` with exactly the engine's child environment (read-only:
    diagnose observes files and resolves harness binaries; it changes
    nothing). A diagnose verdict of pass or fail is fine; an import or
    startup failure is not."""

    def test_real_diagnose_starts_isolated_with_the_minimal_env(self) -> None:
        import json
        import sys

        import getpass

        from tools.persona_installer import Manifest

        # the executor always diagnoses ITS OWN seat's persona; here that is
        # the manifest persona bound to the current account. A CI runner's
        # account is not a seat, so fall back to the first manifest persona
        # rather than skip (T-5057: the dbus-tiers job forbids skips). This
        # test only proves the real entrypoint starts under -I with the
        # minimal env; diagnose reports an unbound persona as observations,
        # never as an import or startup failure.
        roles = [r for r in Manifest.load(REPO_ROOT / "instance.yaml").roles.values()
                 if r.get("persona")]
        mine = [role for role in roles if role.get("account") == getpass.getuser()]
        chosen = (mine or sorted(roles, key=lambda r: str(r["persona"])))[0]
        persona, harness = str(chosen["persona"]), str(chosen.get("harness") or "claude")
        engine = self.engine()
        env = dict(engine._diagnose_env())
        env["HOME"] = os.path.expanduser("~")
        result = subprocess.run(
            [sys.executable, "-I", str(REPO_ROOT / "tools/persona_bringup.py"), "diagnose",
             "--manifest", str(REPO_ROOT / "instance.yaml"), "--persona", persona, "--harness", harness],
            check=False, capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL, env=env,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr[-2000:])
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"diagnose printed no JSON report; stderr tail:\n{result.stderr[-2000:]}")
        self.assertTrue(report["observations"])
        self.assertNotIn("Traceback", result.stderr)


class TimerEnablePrePassGateTests(_Case):
    """Review L1: an already-enabled timer with a canonical link and no
    legacy seed is refused in the PRE-PASS, before an earlier mkdir commits."""

    def test_mkdir_then_enabled_timer_is_refused_before_mkdir(self) -> None:
        self.make_link()
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=7)
        self.fake.enablement[TIMER] = "enabled"
        made = self.seat / "made-by-apply"
        mkdir = fx.op("seat.mkdir", "mkdir", str(made), {"mode": "0755"})
        result = self.run_ops(mkdir, self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("pre-pass", result.evidence[0].detail)
        self.assertIn("timer already enabled at the activation boundary", result.evidence[0].detail)
        self.assertFalse(made.exists())
        self.assertEqual(self.write_calls(), [])


class CleanupFailureTests(_UnitFireBase):
    """Review L4: a refused or unreachable cleanup stop keeps unsafe-cleanup
    and records cleanup_state."""

    def failing_fire(self) -> None:
        self.script(capture=_service_state(active="failed", sub="failed", result="exit-code", exec_status=1,
                                           invocation=INVOCATION), completion=_service_state())

    def test_refused_stop_is_unsafe_cleanup(self) -> None:
        self.failing_fire()
        self.fake.refuse["stop_unit"] = "org.freedesktop.systemd1.AccessDenied"
        result = self.run_ops(self.unit_fire_op())
        evidence = result.evidence[0].evidence
        self.assertFalse(result.ok)
        self.assertEqual(evidence["classification"], "unsafe-cleanup")
        self.assertIn("cleanup_state", evidence)

    def test_stop_raising_call_failed_or_unavailable_is_unsafe_cleanup(self) -> None:
        for error in (
            _us.UserManagerCallFailed("org.freedesktop.systemd1.NoSuchUnit", "gone"),
            UserManagerUnavailable("bus gone"),
        ):
            with self.subTest(error=type(error).__name__):
                self.fake.calls.clear()
                self.fake.units[SERVICE] = _service_state()
                self.failing_fire()
                previous = self.fake.on_call

                def on_call(fake, name, args, _error=error, _previous=previous):
                    if name == "stop_unit":
                        raise _error
                    _previous(fake, name, args)

                self.fake.on_call = on_call
                result = self.run_ops(self.unit_fire_op())
                evidence = result.evidence[0].evidence
                self.assertFalse(result.ok)
                self.assertEqual(evidence["classification"], "unsafe-cleanup")
                self.assertEqual(evidence["cleanup_state"], "stop-failed")
                self.assertEqual(evidence["phase"], "cleanup")


# ---------------------------------------------------------------------------
# 5c.2 re-review N1: one consume instance per spec; seeds keyed by timer
# ---------------------------------------------------------------------------

OTHER = "other"
OTHER_SERVICE = f"pantheon-persona-consume@{OTHER}.service"
OTHER_TIMER = f"pantheon-persona-consume@{OTHER}.timer"


class ConsumeInstanceTests(_UnitFireBase):
    def reviewer_ops(self):
        # unit-fire {target=@seat.service, timer=@other.timer} + timer-enable {@seat.timer}
        return (self.unit_fire_op(timer=OTHER_TIMER), self.timer_enable_op())

    def other_converged_and_seat_enabled_unproven(self) -> None:
        self.fake.units[OTHER_SERVICE] = _service_state(exec_status=5, policy=([3, 4, 5], []), invocation=INVOCATION)
        self.fake.units[OTHER_TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=99)
        self.fake.enablement[OTHER_TIMER] = "enabled"
        other_link = _consume_timer_enablement_link_path(self.seat, OTHER_TIMER)
        other_link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(_consume_timer_template_path(self.seat, OTHER_TIMER)), other_link)
        # @seat.timer enabled but never proven (no legacy shape, no link)
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=7)
        self.fake.enablement[TIMER] = "enabled"

    def test_reviewer_scenario_is_refused_at_parse_time(self) -> None:
        with self.assertRaises(InvalidSpec) as caught:
            fx.parsed(self.fixture, self.reviewer_ops())
        self.assertIn("timer fires", str(caught.exception))

    def test_reviewer_scenario_with_parse_bypassed_reports_no_converged_success(self) -> None:
        from tools.host_control.executor.engine import ParsedSpec

        self.other_converged_and_seat_enabled_unproven()
        spec = ParsedSpec(operation_handle=fx.OPERATION_HANDLE, execution_kind="apply",
                          spec_digest="0" * 64, operations=self.reviewer_ops())
        result = self.engine().run(spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        for item in result.evidence:
            self.assertFalse(item.ok and "converged" in item.detail, item)
        self.assertEqual(self.write_calls(), [])

    def test_seed_for_one_timer_never_vouches_for_another(self) -> None:
        from tools.host_control.executor.engine import ParsedSpec

        self.other_converged_and_seat_enabled_unproven()
        other_absence = fx.op(
            "connective.timer-enablement-absent",
            "timer-enablement-absence-check",
            str(_consume_timer_enablement_link_path(self.seat, OTHER_TIMER)),
            {"timer": OTHER_TIMER,
             "canonical_template_path": str(_consume_timer_template_path(self.seat, OTHER_TIMER))},
        )
        spec = ParsedSpec(operation_handle=fx.OPERATION_HANDLE, execution_kind="apply",
                          spec_digest="0" * 64, operations=(other_absence, self.timer_observe_op()))
        result = self.engine().run(spec)
        absent, observe = result.evidence
        self.assertTrue(absent.ok, absent)
        self.assertEqual(absent.evidence["classification"], "converged-legacy")
        self.assertFalse(observe.ok)
        self.assertNotIn("existing live convergence evidence retained", observe.detail)

    def test_pairing_and_single_instance_refusals(self) -> None:
        cases = {
            "unit-fire target not its timer's service": (self.unit_fire_op(timer=OTHER_TIMER),),
            "timer-observe service not its timer's service": (
                fx.op("connective.timer-observe", "timer-observe", TIMER, {"service": OTHER_SERVICE}),
            ),
            "two consume instances": (self.unit_fire_op(), self.stale_op(OTHER_SERVICE)),
            "two instances via timer-enable": (self.stale_op(), dataclasses.replace(self.timer_enable_op(), target=OTHER_TIMER)),
        }
        for name, operations in cases.items():
            with self.subTest(case=name), self.assertRaises(InvalidSpec):
                fx.parsed(self.fixture, operations)
        # one instance, correctly paired, parses
        fx.parsed(self.fixture, (self.stale_op(), self.unit_fire_op(), self.timer_enable_op(), self.timer_observe_op()))


class TimerEnableNotLoadedPrePassTests(_Case):
    """The apply handler refuses a not-found timer; the pre-pass now does
    too, before an earlier mkdir commits."""

    def test_mkdir_then_not_found_timer_is_refused_before_mkdir(self) -> None:
        self.fake.units[TIMER] = _timer_state(load="not-found", unit_file="")
        made = self.seat / "made-by-apply"
        mkdir = fx.op("seat.mkdir", "mkdir", str(made), {"mode": "0755"})
        result = self.run_ops(mkdir, self.timer_enable_op())
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("pre-pass", result.evidence[0].detail)
        self.assertIn("timer unit is not loaded", result.evidence[0].detail)
        self.assertFalse(made.exists())
        self.assertEqual(self.write_calls(), [])


if __name__ == "__main__":
    unittest.main()
