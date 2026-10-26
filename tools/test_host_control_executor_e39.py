"""ORG-052 gate 39, the founding-incident regression: the FIXTURE proof
(T-2917 5c.2).

The REAL ``ExecutorEngine`` runs an apply of activation kinds
(daemon-reload; stale-unit-failure-reset on its reset-required path; a
scripted unit-fire) in a CHILD process whose environment is full of decoys:

* ``PATH`` is ONLY a decoy bin dir whose ``systemctl``/``loginctl``/
  ``busctl`` are shell scripts that append to a tripwire file and exit 1 (so
  the real command-line client can never run);
* ``XDG_RUNTIME_DIR`` is a decoy runtime dir whose ``bus`` and
  ``systemd/private`` are listening unix sockets served by tripwire threads
  in the parent (any accept is recorded); ``DBUS_SYSTEM_BUS_ADDRESS`` points
  at another tripwire socket;
* ``DBUS_SESSION_BUS_ADDRESS`` is a SECOND private bus running a decoy fake
  systemd1 whose call log is the session tripwire;
* the child installs ``sys.addaudithook`` and records process-spawn and
  socket-connect events.

Variants: ``green`` (the executor's ``DBusUserManager`` with the
``runtime_root`` test override) must reach the TARGET fake systemd1 with the
exact expected call sequence, fire no tripwire and label every activation
record ``test-override`` -> GREEN. ``red-systemctl`` (an injected operator
``SystemctlUserManager``) and ``red-session-bus`` (a manager that follows
``DBUS_SESSION_BUS_ADDRESS``) must each be RED, naming the tripwire that
fired: the harness detects exactly the regressions gate 39 forbids.

System-python tier (ruling D1). Fixture evidence, labelled test-override:
real systemd agreement and real cross-uid are live items for T-2924.

``/usr/bin/python3 -m tools.test_host_control_executor_e39 --report`` prints
one JSON line per variant.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from tools import _host_control_testbus as testbus
from tools import test_host_control_executor_user_bus as ub  # module import: never re-collect its TestCases

REPO_ROOT = Path(__file__).resolve().parent.parent
DBUS = ub.DBUS

SERVICE = "pantheon-persona-consume@specialist.service"
TIMER = "pantheon-persona-consume@specialist.timer"
STALE_ID = "0123456789abcdef0123456789abcdef"
FIRE_ID = "fedcba9876543210fedcba9876543210"
FORBIDDEN_TOOLS = ("systemctl", "loginctl", "busctl")
SPAWN_EVENTS = ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn", "os.system")
EVIDENCE_LABEL = "fixture evidence (test-override); real systemd agreement and real cross-uid are T-2924"

RECEIVER = "codex-telegram-poller.service"


def seat_root_for(run_dir: Path) -> Path:
    """The seat root the child's fixture will have (build_executor_fixture)."""

    from tools import _host_control_executor_fixtures as fx

    return (run_dir / "fixture").resolve() / "inst" / "home" / fx.ACCOUNT


def scenario_for(seat_root: Path) -> dict:
    """The fake systemd1 scenario. Review L2(c): the GREEN run also covers
    disabled-first (receiver inspection), timer-enable (EnableUnitFiles +
    Reload + StartUnit + a GetJob wait) and timer-observe (Verify)."""

    from tools import persona_installer as pim

    link = pim._consume_timer_enablement_link_path(seat_root, TIMER)
    canonical = pim._consume_timer_template_path(seat_root, TIMER)
    return {
        "environment": ["PATH=/fake"],
        "units": {
            SERVICE: {
                "props": {
                    "LoadState": "not-found",
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainStatus": 1,
                    "InvocationID": STALE_ID,
                    "Job": [0, "/"],
                    "SuccessExitStatus": [[], []],
                },
                "on_reset_failed": {
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "Result": "success",
                    "ExecMainStatus": 0,
                    "InvocationID": "",
                },
                "on_start": [
                    {"loads": 1, "props": {"ActiveState": "activating", "SubState": "start", "InvocationID": FIRE_ID}},
                    {"loads": 0, "props": {"ActiveState": "inactive", "SubState": "dead", "Result": "success", "ExecMainStatus": 0}},
                ],
            },
            TIMER: {
                "unit_file_state": "disabled",
                "props": {
                    "LoadState": "loaded",
                    "UnitFileState": "disabled",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "NextElapseUSecRealtime": 0,
                    "NextElapseUSecMonotonic": 0,
                },
                "enable_symlink": {"link": str(link), "target": str(canonical)},
                "on_enable": {
                    "UnitFileState": "enabled",
                    "ActiveState": "active",
                    "SubState": "waiting",
                    "NextElapseUSecMonotonic": 123456789,
                },
            },
            RECEIVER: {
                "unit_file_state": "disabled",
                "props": {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "MainPID": 0,
                          "EnvironmentFiles": []},
            },
        },
    }


def _reads(purpose_name: str, unit: str = SERVICE) -> list[tuple]:
    from tools.user_systemd import ReadPurpose

    return ub.read_unit_calls(unit, ReadPurpose[purpose_name])


def _disabled_first_calls() -> list[tuple]:
    return (
        _reads("RECEIVER_ACTIVE", RECEIVER)
        + [("GetUnitFileState", RECEIVER)]
        + [("LoadUnit", RECEIVER), ("Get", ub.SERVICE_IFACE, "EnvironmentFiles")]
    )


def expected_green_calls() -> list[tuple]:
    return (
        # APPLY pre-pass: manager reachable, disabled-first inspection,
        # unit-fire's enablement gate, timer-enable's unit-file gate
        [("Get", ub.MANAGER_IFACE, "Environment")]
        + _disabled_first_calls()
        + [("GetUnitFileState", TIMER)]
        + _reads("UNIT_FILE_ACTIVE", TIMER)
        # daemon-reload
        + [("Reload",)]
        # disabled-first (diagnostics.read)
        + _disabled_first_calls()
        # stale-unit-failure-reset, reset-required path
        + _reads("STALE_RESET")
        + [("ResetFailedUnit", SERVICE)]
        + _reads("STALE_RESET")
        # unit-fire: enablement, pre-enqueue idle proof, no-block start,
        # capture (activating + InvocationID), completion (inactive success)
        + [("GetUnitFileState", TIMER)]
        + _reads("UNIT_FIRE_POLL")
        + [("StartUnit", SERVICE, "replace")]
        + _reads("UNIT_FIRE_POLL")
        + _reads("UNIT_FIRE_POLL")
        # timer-enable: unit-file state, then enable --now as Manager calls
        # with the job wait (job 2: one GetJob in flight, then NoSuchJob)
        + _reads("UNIT_FILE_ACTIVE", TIMER)
        + [("EnableUnitFiles", (TIMER,), False, False), ("Reload",), ("StartUnit", TIMER, "replace"),
           ("GetJob", 2), ("GetJob", 2)]
        # VERIFY: timer-enable live state, timer-observe live state
        + _reads("UNIT_FILE_ACTIVE", TIMER)
        + _reads("SERVICE_OBSERVE")
        + _reads("TIMER_ELAPSE", TIMER)
    )


# ---------------------------------------------------------------------------
# parent-side decoys
# ---------------------------------------------------------------------------


class TripwireListener:
    """A listening unix socket; any accepted connection is recorded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.hits: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            self.hits.append(str(self.path))
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.server.close()


_TRIPWIRE_SCRIPT = """#!/bin/sh
echo "$0 $*" >> {trip}
exit 1
"""


class E39Harness:
    """Target bus (with the registry-derived runtime address), decoy session
    bus, decoy sockets and decoy bin. Construct from the main thread."""

    def __init__(self) -> None:
        self.target = ub.UserBusFixture("t2917e-")
        self.session = ub.UserBusFixture("t2917s-")
        self.work = Path(ub.short_tmpdir("t29e39-"))
        self.decoy_bin = self.work / "bin"
        self.decoy_bin.mkdir()
        self.trip_file = self.work / "decoy-bin.trip"
        for tool in FORBIDDEN_TOOLS:
            script = self.decoy_bin / tool
            script.write_text(_TRIPWIRE_SCRIPT.format(trip=self.trip_file), encoding="utf-8")
            script.chmod(0o755)
        self.xdg = self.work / "xdg"
        self.listeners = {
            "xdg-bus": TripwireListener(self.xdg / "bus"),
            "xdg-systemd-private": TripwireListener(self.xdg / "systemd" / "private"),
            "system-bus": TripwireListener(self.work / "system_bus_socket"),
        }
        self._runs = 0

    def close(self) -> None:
        for listener in self.listeners.values():
            listener.close()
        self.target.close()
        self.session.close()
        import shutil

        shutil.rmtree(self.work, ignore_errors=True)

    def run(self, variant: str) -> dict[str, Any]:
        self._runs += 1
        run_dir = self.work / f"run-{self._runs}"
        run_dir.mkdir()
        scenario = scenario_for(seat_root_for(run_dir))
        target_calls = self.target.start_fake(scenario)
        session_calls = self.session.start_fake(scenario)
        before = {name: len(listener.hits) for name, listener in self.listeners.items()}
        if self.trip_file.exists():
            self.trip_file.unlink()
        config = {
            "variant": variant,
            "work": str(run_dir),
            "runtime_root": str(self.target.runtime_root),
            "result": str(run_dir / "result.json"),
            "audit": str(run_dir / "audit.json"),
        }
        config_path = run_dir / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = {
            "PATH": str(self.decoy_bin),
            "XDG_RUNTIME_DIR": str(self.xdg),
            "DBUS_SESSION_BUS_ADDRESS": self.session.handle.bus_address,
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={self.listeners['system-bus'].path}",
            "HOME": str(run_dir),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        child = subprocess.run(
            [sys.executable, "-m", "tools.test_host_control_executor_e39", "--child", str(config_path)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        self.target.stop_fake()
        self.session.stop_fake()
        if child.returncode != 0:
            raise AssertionError(f"E39 child failed ({child.returncode}): {child.stderr[-2000:]}")
        result = json.loads(Path(config["result"]).read_text(encoding="utf-8"))
        audit = json.loads(Path(config["audit"]).read_text(encoding="utf-8"))
        return verdict(
            variant=variant,
            result=result,
            audit=audit,
            target_calls=ub.read_calls(target_calls),
            session_calls=ub.read_calls(session_calls),
            decoy_bin_hits=self.trip_file.read_text(encoding="utf-8").splitlines() if self.trip_file.exists() else [],
            socket_hits={
                name: listener.hits[before[name]:] for name, listener in self.listeners.items()
            },
        )


PORT_EVIDENCE_KINDS = (
    "daemon-reload",
    "stale-unit-failure-reset",
    "unit-fire",
    "timer-enable",
    "timer-observe",
    "codex-telegram-disabled-first",
)
EXPECTED_GREEN_COVERAGE = (
    ("apply", "daemon-reload"),
    ("apply", "codex-telegram-disabled-first"),
    ("apply", "stale-unit-failure-reset"),
    ("apply", "unit-fire"),
    ("apply", "timer-enable"),
    ("verify", "timer-enable"),
    ("verify", "timer-observe"),
)


def verdict(
    *,
    variant: str,
    result: dict,
    audit: list,
    target_calls: list[tuple],
    session_calls: list[tuple],
    decoy_bin_hits: list[str],
    socket_hits: dict[str, list[str]],
) -> dict[str, Any]:
    fired: list[str] = []
    if decoy_bin_hits:
        fired.append("decoy-bin")
    for name, hits in sorted(socket_hits.items()):
        if hits:
            fired.append(f"decoy-socket:{name}")
    if session_calls:
        fired.append("decoy-session-bus")
    audit_exec = [
        entry for entry in audit
        if entry[0] in SPAWN_EVENTS and any(tool in entry[1] for tool in FORBIDDEN_TOOLS)
    ]
    if audit_exec:
        fired.append("audit-exec")
    port_kinds = PORT_EVIDENCE_KINDS
    activation = [item for item in result["evidence"] if item["kind"] in port_kinds]
    labels = sorted({item["evidence"].get("user_manager_address_source", "") for item in activation})
    calls_match = [list(item) for item in target_calls] == [list(item) for item in expected_green_calls()]
    covered = sorted({(item["phase"], item["kind"]) for item in activation if item["ok"]})
    green = (
        not fired
        and calls_match
        and result["ok"]
        and covered == sorted(EXPECTED_GREEN_COVERAGE)
        and labels == ["test-override"]
    )
    return {
        "variant": variant,
        "verdict": "GREEN" if green else "RED",
        "fired": fired,
        "target_calls_match": calls_match,
        "target_call_count": len(target_calls),
        "decoy_session_call_count": len(session_calls),
        "decoy_bin_hits": len(decoy_bin_hits),
        "audit_exec_events": len(audit_exec),
        "engine_ok": bool(result["ok"]),
        "engine_detail": result["detail"],
        "address_source_labels": labels,
        "covered": [f"{phase}:{kind}" for phase, kind in covered],
        "evidence": EVIDENCE_LABEL,
    }


# ---------------------------------------------------------------------------
# the child
# ---------------------------------------------------------------------------


def _child(config_path: str) -> int:
    events: list[list[str]] = []

    def hook(event: str, args: tuple) -> None:
        if event in SPAWN_EVENTS or event == "socket.connect":
            try:
                events.append([event, repr(args)[:400]])
            except Exception:
                events.append([event, "<unrepresentable>"])

    sys.addaudithook(hook)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    from tools import _host_control_executor_fixtures as fx
    from tools import persona_installer as pim
    from tools import runtime_invocation_receipt
    from tools.host_control.executor.user_bus import DBusUserManager
    from tools.persona_installer import InstallOperation

    def fake_receipt(*args: object, **kwargs: object) -> dict:
        return {
            "selected_runtime": "claude/interactive-subscription",
            "selected_harness": "claude",
            "selected_profile": "default",
            "provider_completion_status": "completed",
            "invocation_receipt_sha256": "ab" * 32,
        }

    runtime_invocation_receipt.validate_initial_viability_invocation_receipt = fake_receipt  # type: ignore[assignment]
    fixture = fx.build_executor_fixture(Path(config["work"]) / "fixture")
    seat = fixture.seat_root

    def operation(operation_id: str, kind: str, target: str, desired: dict[str, str]) -> InstallOperation:
        return InstallOperation(
            operation_id=operation_id,
            layer=1,
            kind=kind,  # type: ignore[arg-type]
            target=target,
            precondition={},
            desired=desired,
            inverse=None,
            privilege="unprivileged",
            owner=fx.ACCOUNT,
            provenance={"persona": "specialist"},
            execution_scope="all",  # type: ignore[arg-type]
        )

    seconds = pim._unit_fire_timeout_seconds(candidate_count=1, post_start_count=0)
    link = pim._consume_timer_enablement_link_path(seat, TIMER)
    canonical = pim._consume_timer_template_path(seat, TIMER)
    timer_enable = operation(
        "connective.timer-enable",
        "timer-enable",
        TIMER,
        {"enablement_link_path": str(link), "canonical_template_path": str(canonical)},
    )
    verify_operations = (
        timer_enable,
        operation("connective.timer-observe", "timer-observe", TIMER, {"service": SERVICE}),
    )
    operations = (
        operation("connective.daemon-reload", "daemon-reload", fx.ACCOUNT, {}),
        operation(
            "codex-telegram.disabled-first",
            "codex-telegram-disabled-first",
            RECEIVER,
            {"active": "inactive", "enabled": "disabled", "environment_file": "forbidden"},
        ),
        operation("connective.stale-unit-failure-reset", "stale-unit-failure-reset", SERVICE, {}),
        operation(
            "connective.unit-fire",
            "unit-fire",
            SERVICE,
            {
                "timer": TIMER,
                "timer_state": "disabled",
                "result": "success",
                "exec_main_status": "0",
                "unit_fire_timeout_seconds": str(seconds),
                "unit_fire_timeout_candidate_count": "1",
                "unit_fire_timeout_post_start_count": "0",
                "unit_fire_timeout_formula": pim.UNIT_FIRE_TIMEOUT_FORMULA,
                "account_home": str(seat),
                "persona_state_root": str(seat / ".local/state/pantheon/personas"),
                "allowed_runtime_refs": '["claude/interactive-subscription"]',
                "require_invocation_receipt": "true",
                "invocation_receipt_schema": "pantheon-runtime-initial-viability-invocation/v1",
                "receiver_receipt_required": "false",
                "receiver_operations_by_runtime": "{}",
            },
        ),
        timer_enable,
    )
    variant = config["variant"]
    runtime_root = Path(config["runtime_root"])
    if variant == "green":
        manager: Any = DBusUserManager(uid=os.getuid(), runtime_root=runtime_root)
    elif variant == "red-systemctl":
        manager = pim.SystemctlUserManager(
            lambda argv, timeout=None: subprocess.run(
                list(argv), check=False, capture_output=True, text=True, timeout=timeout or 10
            )
        )
    elif variant == "red-session-bus":

        class SessionBusUserManager(DBusUserManager):
            """RED control: follows the environment's session bus."""

            def _open(self) -> object:
                import dbus.bus

                return dbus.bus.BusConnection(os.environ["DBUS_SESSION_BUS_ADDRESS"])

        manager = SessionBusUserManager(uid=os.getuid(), runtime_root=runtime_root)
    else:
        raise SystemExit(f"unknown variant {variant}")
    try:
        result = fixture.engine(user_manager=manager).run(fx.parsed(fixture, operations))
        verify = fixture.engine(user_manager=manager).run(
            fx.parsed(fixture, verify_operations, execution_kind="verify")
        )
    finally:
        fixture.close()
    Path(config["result"]).write_text(
        json.dumps(
            {
                "ok": result.ok and verify.ok,
                "detail": f"{result.detail}; {verify.detail}",
                "mutations": len(result.mutations),
                "evidence": [
                    {
                        "phase": phase,
                        "operation_id": item.operation_id,
                        "kind": item.kind,
                        "ok": item.ok,
                        "detail": item.detail,
                        "evidence": dict(item.evidence),
                    }
                    for phase, run in (("apply", result), ("verify", verify))
                    for item in run.evidence
                ],
            }
        ),
        encoding="utf-8",
    )
    Path(config["audit"]).write_text(json.dumps(events), encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(DBUS, "dbus-python and dbus-daemon are required")
class E39FixtureProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.harness = E39Harness()
        cls.outcomes: dict[str, dict] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()
        super().tearDownClass()

    def outcome(self, variant: str) -> dict:
        if variant not in self.outcomes:
            self.outcomes[variant] = self.harness.run(variant)
        return self.outcomes[variant]

    def test_green_reaches_the_target_manager_and_fires_no_tripwire(self) -> None:
        outcome = self.outcome("green")
        self.assertEqual(outcome["verdict"], "GREEN", outcome)
        self.assertEqual(outcome["fired"], [])
        self.assertTrue(outcome["target_calls_match"])
        self.assertEqual(outcome["target_call_count"], len(expected_green_calls()))
        self.assertEqual(outcome["decoy_session_call_count"], 0)
        self.assertEqual(outcome["audit_exec_events"], 0)
        self.assertEqual(outcome["address_source_labels"], ["test-override"])
        self.assertTrue(outcome["engine_ok"])

    def test_red_injected_operator_client_trips_the_decoy_bin(self) -> None:
        outcome = self.outcome("red-systemctl")
        self.assertEqual(outcome["verdict"], "RED", outcome)
        self.assertIn("decoy-bin", outcome["fired"])
        self.assertIn("audit-exec", outcome["fired"])
        self.assertEqual(outcome["target_call_count"], 0)
        self.assertFalse(outcome["engine_ok"])

    def test_red_session_bus_manager_trips_the_decoy_session_bus(self) -> None:
        outcome = self.outcome("red-session-bus")
        self.assertEqual(outcome["verdict"], "RED", outcome)
        self.assertIn("decoy-session-bus", outcome["fired"])
        self.assertGreater(outcome["decoy_session_call_count"], 0)
        self.assertEqual(outcome["target_call_count"], 0)

    def test_expected_sequence_is_not_vacuous(self) -> None:
        calls = expected_green_calls()
        members = [call[0] for call in calls]
        for member in ("Reload", "ResetFailedUnit", "StartUnit", "GetUnitFileState", "LoadUnit",
                       "EnableUnitFiles", "GetJob"):
            self.assertIn(member, members)
        self.assertIn(("Get", ub.SERVICE_IFACE, "EnvironmentFiles"), calls)

    def test_green_covers_enable_disabled_first_and_observe(self) -> None:
        outcome = self.outcome("green")
        for phase, kind in EXPECTED_GREEN_COVERAGE:
            self.assertIn(f"{phase}:{kind}", outcome["covered"])


def _report() -> int:
    harness = E39Harness()
    try:
        for variant in ("green", "red-systemctl", "red-session-bus"):
            print(json.dumps(harness.run(variant), sort_keys=True))
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        raise SystemExit(_child(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--report":
        raise SystemExit(_report())
    unittest.main()
