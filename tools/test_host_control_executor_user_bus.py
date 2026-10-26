"""T-2917 5c.2: ``DBusUserManager`` against a fake systemd1 on a PRIVATE bus.

System-python tier (dbus-python + dbus-daemon; ruling D1: fixture evidence
on the canonical host, the CI gap is T-4931). The private bus listens at the
spawn socket AND at ``<runtime_root>/<uid>/bus``, so the manager reaches it
through its registry-derived address with the ``runtime_root`` test override
only (ruling D7) and no symlink. The fake systemd1
(tools/_host_control_fake_systemd1.py) records every call, so each port
method's exact call sequence is asserted.

Fixture evidence: real systemd agreement and real cross-uid are T-2924.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tools import _host_control_testbus as testbus
from tools.user_systemd import (
    TYPED_CANONICAL_JSON,
    ManagerState,
    ReadPurpose,
    UnitFileEnablement,
    UserManagerCallFailed,
    snapshot_digest_evidence,
    unit_snapshot_from_native,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    import dbus  # noqa: F401
    import dbus.bus

    from tools.host_control.executor.user_bus import DBusUserManager
    from tools.host_control.executor.user_manager import UserManagerUnavailable

    DBUS = testbus.DBUS_DAEMON is not None
except ImportError:  # pragma: no cover
    DBUS = False

UNIT_IFACE = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE = "org.freedesktop.systemd1.Service"
TIMER_IFACE = "org.freedesktop.systemd1.Timer"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
PROPERTY_INTERFACES = {
    "LoadState": UNIT_IFACE,
    "ActiveState": UNIT_IFACE,
    "SubState": UNIT_IFACE,
    "UnitFileState": UNIT_IFACE,
    "InvocationID": UNIT_IFACE,
    "Job": UNIT_IFACE,
    "NeedDaemonReload": UNIT_IFACE,
    "Result": SERVICE_IFACE,
    "ExecMainStatus": SERVICE_IFACE,
    "SuccessExitStatus": SERVICE_IFACE,
    "MainPID": SERVICE_IFACE,
    "NRestarts": SERVICE_IFACE,
    "NextElapseUSecRealtime": TIMER_IFACE,
    "NextElapseUSecMonotonic": TIMER_IFACE,
}

_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={{socket}}</listen>
  <listen>unix:path={runtime_socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

_PLAIN_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

SERVICE = "pantheon-persona-consume@specialist.service"
TIMER = "pantheon-persona-consume@specialist.timer"
VALID_ID = "0123456789abcdef0123456789abcdef"

# Native (bus-layer) values of a fully populated unit, and the scenario
# JSON the fake turns into exactly these typed D-Bus values.
NATIVE_UNIT = {
    "LoadState": "loaded",
    "ActiveState": "inactive",
    "SubState": "dead",
    "UnitFileState": "disabled",
    "InvocationID": bytes.fromhex(VALID_ID),
    "Job": (7, "/org/freedesktop/systemd1/job/7"),
    "NeedDaemonReload": False,
    "Result": "success",
    "ExecMainStatus": 5,
    "SuccessExitStatus": ([3, 4, 5], [15]),
    "MainPID": 0,
    "NRestarts": 3,
    "NextElapseUSecRealtime": 0,
    "NextElapseUSecMonotonic": 123456789,
}
SCENARIO_UNIT_PROPS = {
    "LoadState": "loaded",
    "ActiveState": "inactive",
    "SubState": "dead",
    "UnitFileState": "disabled",
    "InvocationID": VALID_ID,
    "Job": [7, "/org/freedesktop/systemd1/job/7"],
    "NeedDaemonReload": False,
    "Result": "success",
    "ExecMainStatus": 5,
    "SuccessExitStatus": [[3, 4, 5], [15]],
    "MainPID": 0,
    "NRestarts": 3,
    "NextElapseUSecRealtime": 0,
    "NextElapseUSecMonotonic": 123456789,
    "EnvironmentFiles": [],
}


def short_tmpdir(prefix: str) -> str:
    """A short temp dir (AF_UNIX path limit). Every caller removes it in its
    own close()/tearDown/finally."""

    return testbus._short_tmpdir(prefix)


class UserBusFixture:
    """A private bus listening ALSO at ``<runtime_root>/<uid>/bus`` plus a
    restartable fake systemd1. Call from the main thread (spawn_daemon)."""

    def __init__(self, prefix: str = "t2917u-") -> None:
        self.uid = os.getuid()
        self.runtime_root = Path(short_tmpdir("t29rt-"))
        (self.runtime_root / str(self.uid)).mkdir(mode=0o700)
        self.runtime_socket = self.runtime_root / str(self.uid) / "bus"
        self.handle = testbus.spawn_daemon(
            prefix, _BUS_CONFIG.format(runtime_socket=self.runtime_socket)
        )
        deadline = time.monotonic() + 20
        while not self.runtime_socket.exists():
            if time.monotonic() > deadline:
                raise AssertionError("runtime bus socket never appeared")
            time.sleep(0.05)
        self.client = dbus.bus.BusConnection(self.handle.bus_address)
        self.work = Path(short_tmpdir("t29fk-"))
        self.fake: subprocess.Popen | None = None
        self.calls_path: Path | None = None
        self._count = 0

    def start_fake(self, scenario: dict, *, bus_address: str | None = None) -> Path:
        self.stop_fake()
        self._count += 1
        scenario_path = self.work / f"scenario-{self._count}.json"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        self.calls_path = self.work / f"calls-{self._count}.jsonl"
        self.calls_path.write_text("", encoding="utf-8")
        log = open(self.work / f"fake-{self._count}.log", "w", encoding="utf-8")  # noqa: SIM115
        self.fake = subprocess.Popen(
            [sys.executable, "-m", "tools._host_control_fake_systemd1",
             bus_address or self.handle.bus_address, str(scenario_path), str(self.calls_path)],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        log.close()
        self.handle.register_driver(self.fake.pid)
        wait_for_owner(self.client, self.fake)
        return self.calls_path

    def stop_fake(self) -> None:
        if self.fake is not None and self.fake.poll() is None:
            self.fake.terminate()
            try:
                self.fake.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.fake.kill()
                self.fake.wait(timeout=5)
        self.fake = None

    def calls(self) -> list[tuple]:
        return read_calls(self.calls_path)

    def manager(self, **kwargs: object) -> "DBusUserManager":
        return DBusUserManager(uid=self.uid, runtime_root=self.runtime_root, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self.stop_fake()
        try:
            self.client.close()
        except Exception:
            pass
        testbus.stop_daemon(self.handle)
        import shutil

        shutil.rmtree(self.runtime_root, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)


def wait_for_owner(client: object, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not client.name_has_owner("org.freedesktop.systemd1"):  # type: ignore[attr-defined]
        if process.poll() is not None:
            raise AssertionError("fake systemd1 exited early")
        if time.monotonic() > deadline:
            raise AssertionError("fake systemd1 never owned its name")
        time.sleep(0.05)


def read_calls(path: Path | None) -> list[tuple]:
    """Calls as tuples: ("Get", interface, property) for property reads,
    else (member, *args)."""

    if path is None or not path.exists():
        return []
    out: list[tuple] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["member"] == "Get":
            out.append(("Get", *entry["args"]))
        else:
            out.append((entry["member"], *[tuple(a) if isinstance(a, list) else a for a in entry["args"]]))
    return out


def read_unit_calls(unit: str, purpose: "ReadPurpose") -> list[tuple]:
    return [("LoadUnit", unit)] + [("Get", PROPERTY_INTERFACES[name], name) for name in purpose.properties]


@unittest.skipUnless(DBUS, "dbus-python and dbus-daemon are required")
class DBusUserManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.bus = UserBusFixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bus.close()
        super().tearDownClass()

    def tearDown(self) -> None:
        self.bus.stop_fake()

    def scenario(self, **units: dict) -> dict:
        return {"units": units}

    # -- address and labels ---------------------------------------------

    def test_address_is_registry_derived_and_labelled_test_override(self) -> None:
        manager = self.bus.manager()
        self.assertEqual(manager.address, f"unix:path={self.bus.runtime_root}/{self.bus.uid}/bus")
        self.assertEqual(manager.address_source, "test-override")
        self.assertEqual(manager.transport, "dbus")

    # -- reads ----------------------------------------------------------

    def test_manager_state_and_environment(self) -> None:
        self.bus.start_fake({"system_state": "degraded", "version": "255-fake", "environment": ["A=1", "B=2", "C=3"]})
        manager = self.bus.manager()
        self.assertEqual(manager.manager_state(), ManagerState(True, "degraded", "255-fake"))
        self.assertEqual(manager.manager_environment(), 3)
        self.assertEqual(
            self.bus.calls(),
            [
                ("Get", MANAGER_IFACE, "SystemState"),
                ("Get", MANAGER_IFACE, "Version"),
                ("Get", MANAGER_IFACE, "Environment"),
            ],
        )

    def test_read_unit_exact_sequence_and_typed_snapshot_per_purpose(self) -> None:
        self.bus.start_fake(self.scenario(**{SERVICE: {"props": SCENARIO_UNIT_PROPS}}))
        manager = self.bus.manager()
        seen = 0
        for purpose in ReadPurpose:
            with self.subTest(purpose=purpose.name):
                snapshot = manager.read_unit(SERVICE, purpose)
                calls = self.bus.calls()
                self.assertEqual(calls[seen:], read_unit_calls(SERVICE, purpose))
                seen = len(calls)
                expected = unit_snapshot_from_native(
                    SERVICE,
                    purpose,
                    {name: NATIVE_UNIT[name] for name in purpose.properties},
                    transport="dbus",
                )
                self.assertEqual(snapshot, expected)
                self.assertEqual(snapshot.source.encoding, TYPED_CANONICAL_JSON)
                keys = snapshot_digest_evidence("pre", snapshot.source)
                self.assertEqual(set(keys), {"pre_snapshot_length", "pre_snapshot_sha256", "pre_snapshot_encoding"})
                self.assertFalse(any("stdout" in key for key in keys))

    def test_unknown_unit_reads_as_not_found(self) -> None:
        self.bus.start_fake({})
        snapshot = self.bus.manager().read_unit("pantheon-persona-consume@nobody.service", ReadPurpose.STALE_RESET)
        self.assertEqual((snapshot.load_state, snapshot.active_state, snapshot.sub_state), ("not-found", "inactive", "dead"))
        self.assertEqual(snapshot.invocation_id, "")
        self.assertEqual(snapshot.job_id, 0)

    def test_unit_file_enablement(self) -> None:
        self.bus.start_fake(
            self.scenario(
                **{
                    "a.timer": {"unit_file_state": "enabled"},
                    "b.timer": {"unit_file_state": "static"},
                    "c.timer": {"unit_file_state": "disabled"},
                    "d.timer": {"unit_file_error": "org.freedesktop.systemd1.NoSuchUnit"},
                }
            )
        )
        manager = self.bus.manager()
        self.assertEqual(manager.unit_file_enablement("a.timer"), UnitFileEnablement("enabled", True))
        self.assertEqual(manager.unit_file_enablement("b.timer"), UnitFileEnablement("static", True))
        self.assertEqual(manager.unit_file_enablement("c.timer"), UnitFileEnablement("disabled", False))
        self.assertEqual(manager.unit_file_enablement("d.timer"), UnitFileEnablement("not-found", False))
        self.assertEqual(
            self.bus.calls(),
            [("GetUnitFileState", name) for name in ("a.timer", "b.timer", "c.timer", "d.timer")],
        )

    def test_unit_file_enablement_file_not_found_reads_as_not_found(self) -> None:
        # Regression: FileNotFound (the sd-bus mapping of ENOENT for a
        # missing unit file) is a unit-file answer, not an unavailable bus;
        # an absent bus SOCKET is caught before connecting (os.stat).
        self.bus.start_fake(self.scenario(**{"e.timer": {"unit_file_error": "org.freedesktop.DBus.Error.FileNotFound"}}))
        self.assertEqual(self.bus.manager().unit_file_enablement("e.timer"), UnitFileEnablement("not-found", False))

    def test_environment_file_declared_reads_the_typed_list(self) -> None:
        # D4: the operator path matched the substring in the unit file text
        # (e.g. a comment "# no EnvironmentFile= here" answered True). The
        # bus transport reads the typed EnvironmentFiles list only: an empty
        # list is False whatever the unit's text says.
        self.bus.start_fake(
            self.scenario(
                **{
                    "codex-telegram-poller.service": {"props": {"EnvironmentFiles": []}},
                    "x.service": {"props": {"EnvironmentFiles": [["/etc/x.env", False]]}},
                }
            )
        )
        manager = self.bus.manager()
        self.assertFalse(manager.environment_file_declared("codex-telegram-poller.service"))
        self.assertTrue(manager.environment_file_declared("x.service"))
        self.assertEqual(
            self.bus.calls(),
            [
                ("LoadUnit", "codex-telegram-poller.service"),
                ("Get", SERVICE_IFACE, "EnvironmentFiles"),
                ("LoadUnit", "x.service"),
                ("Get", SERVICE_IFACE, "EnvironmentFiles"),
            ],
        )

    # -- writes ---------------------------------------------------------

    def test_reload_and_reset_failed(self) -> None:
        self.bus.start_fake(self.scenario(**{"bad.service": {"reset_failed_error": "org.freedesktop.systemd1.NoSuchUnit"}}))
        manager = self.bus.manager()
        self.assertTrue(manager.reload().ok)
        self.assertTrue(manager.reset_failed_unit(SERVICE).ok)
        refused = manager.reset_failed_unit("bad.service")
        self.assertFalse(refused.ok)
        self.assertEqual(refused.error_name, "org.freedesktop.systemd1.NoSuchUnit")
        self.assertEqual(
            self.bus.calls(),
            [("Reload",), ("ResetFailedUnit", SERVICE), ("ResetFailedUnit", "bad.service")],
        )

    def test_start_without_wait_issues_only_start_unit(self) -> None:
        self.bus.start_fake({})
        outcome = self.bus.manager().start_unit(SERVICE, wait=False, timeout=10)
        self.assertTrue(outcome.ok)
        self.assertEqual(self.bus.calls(), [("StartUnit", SERVICE, "replace")])

    def test_start_with_wait_and_stop_wait_for_the_job(self) -> None:
        self.bus.start_fake({"job_polls": 2})
        manager = self.bus.manager()
        self.assertTrue(manager.stop_unit(SERVICE, timeout=10).ok)
        self.assertEqual(
            self.bus.calls(),
            [("StopUnit", SERVICE, "replace"), ("GetJob", 1), ("GetJob", 1), ("GetJob", 1)],
        )
        self.assertTrue(manager.start_unit(SERVICE, wait=True, timeout=10).ok)
        self.assertEqual(
            self.bus.calls()[4:],
            [("StartUnit", SERVICE, "replace"), ("GetJob", 2), ("GetJob", 2), ("GetJob", 2)],
        )

    def test_enable_now_and_disable_now_sequences(self) -> None:
        link_dir = Path(short_tmpdir("t29ln-"))
        try:
            link = link_dir / "timers.target.wants" / TIMER
            target = link_dir / "pantheon-persona-consume@.timer"
            self.bus.start_fake(
                self.scenario(**{TIMER: {"unit_file_state": "disabled",
                                         "enable_symlink": {"link": str(link), "target": str(target)}}})
            )
            manager = self.bus.manager()
            enabled = manager.enable_now(TIMER)
            self.assertTrue(enabled.ok, enabled)
            self.assertTrue(link.is_symlink())
            self.assertEqual(
                self.bus.calls(),
                [
                    ("EnableUnitFiles", (TIMER,), False, False),
                    ("Reload",),
                    ("StartUnit", TIMER, "replace"),
                    ("GetJob", 1),
                    ("GetJob", 1),
                ],
            )
            self.assertEqual(enabled.transport_evidence["changes"], "1")
            self.assertTrue(manager.disable_now(TIMER).ok)
            self.assertFalse(os.path.lexists(link))
            self.assertEqual(
                self.bus.calls()[5:],
                [
                    ("DisableUnitFiles", (TIMER,), False),
                    ("Reload",),
                    ("StopUnit", TIMER, "replace"),
                    ("GetJob", 2),
                    ("GetJob", 2),
                ],
            )
        finally:
            import shutil

            shutil.rmtree(link_dir, ignore_errors=True)

    # -- error mapping ----------------------------------------------------

    def test_method_error_maps_to_call_failed_with_its_name(self) -> None:
        self.bus.start_fake(self.scenario(**{"broken.service": {"load_error": "org.freedesktop.systemd1.NoSuchUnit"}}))
        with self.assertRaises(UserManagerCallFailed) as caught:
            self.bus.manager().read_unit("broken.service", ReadPurpose.RECEIVER_ACTIVE)
        self.assertEqual(caught.exception.error_name, "org.freedesktop.systemd1.NoSuchUnit")

    def test_manager_gone_maps_to_unavailable(self) -> None:
        self.bus.start_fake({})
        manager = self.bus.manager()
        self.assertEqual(manager.manager_environment(), 1)
        self.bus.stop_fake()
        with self.assertRaises(UserManagerUnavailable):
            manager.manager_environment()

    def test_no_manager_on_the_bus_is_unavailable(self) -> None:
        self.bus.stop_fake()
        with self.assertRaises(UserManagerUnavailable):
            self.bus.manager().manager_environment()


class _FakeConnection:
    """Answers the bus-driver attestation calls; records the rest."""

    def __init__(self, owner_uid: int, owner: str = ":1.9") -> None:
        self.owner_uid = owner_uid
        self.owner = owner
        self.calls: list[tuple] = []

    def call_blocking(self, destination, path, interface, method, signature, args, timeout=None, byte_arrays=False):
        self.calls.append((destination, interface, method, tuple(args)))
        if method == "GetNameOwner":
            return self.owner
        if method == "GetConnectionUnixUser":
            return self.owner_uid
        if method == "Get":
            return ["A=1"]
        raise AssertionError(f"unexpected call {method}")

    def close(self) -> None:
        pass


@unittest.skipUnless(DBUS, "dbus-python is required (the manager imports it lazily per call)")
class AttestationTests(unittest.TestCase):
    """The socket and manager-owner checks, through the ``connect`` seam."""

    def setUp(self) -> None:
        self.root = Path(short_tmpdir("t29at-"))
        self.uid = os.getuid()
        self.sockets: list[socket.socket] = []

    def tearDown(self) -> None:
        for item in self.sockets:
            item.close()
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def bind_socket(self, uid: int) -> Path:
        directory = self.root / str(uid)
        directory.mkdir(mode=0o700)
        path = directory / "bus"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        self.sockets.append(server)
        return path

    def test_owner_uid_matching_the_seat_is_attested_and_calls_go_to_the_unique_name(self) -> None:
        self.bind_socket(self.uid)
        connection = _FakeConnection(self.uid)
        manager = DBusUserManager(uid=self.uid, runtime_root=self.root, connect=lambda address: connection)
        self.assertEqual(manager.manager_environment(), 1)
        self.assertEqual(connection.calls[-1][0], ":1.9")
        self.assertEqual(connection.calls[-1][2], "Get")

    def test_manager_owned_by_another_uid_is_unavailable(self) -> None:
        self.bind_socket(self.uid)
        connection = _FakeConnection(self.uid + 1)
        manager = DBusUserManager(uid=self.uid, runtime_root=self.root, connect=lambda address: connection)
        with self.assertRaises(UserManagerUnavailable):
            manager.manager_environment()
        self.assertNotIn("Get", [call[2] for call in connection.calls])

    def test_path_that_is_not_a_socket_is_unavailable(self) -> None:
        directory = self.root / str(self.uid)
        directory.mkdir(mode=0o700)
        (directory / "bus").write_text("not a socket", encoding="utf-8")
        connection = _FakeConnection(self.uid)
        manager = DBusUserManager(uid=self.uid, runtime_root=self.root, connect=lambda address: connection)
        with self.assertRaises(UserManagerUnavailable):
            manager.manager_environment()
        self.assertEqual(connection.calls, [])

    def test_socket_not_owned_by_the_seat_uid_is_unavailable(self) -> None:
        other = self.uid + 7
        self.bind_socket(other)  # owned by the test uid, named for another uid
        connection = _FakeConnection(other)
        manager = DBusUserManager(uid=other, runtime_root=self.root, connect=lambda address: connection)
        with self.assertRaises(UserManagerUnavailable):
            manager.manager_environment()
        self.assertEqual(connection.calls, [])

    def test_absent_socket_is_unavailable(self) -> None:
        manager = DBusUserManager(uid=self.uid, runtime_root=self.root, connect=lambda address: _FakeConnection(self.uid))
        with self.assertRaises(UserManagerUnavailable):
            manager.manager_environment()


if __name__ == "__main__":
    unittest.main()
