"""Subprocess hosts + shared tree for the T-2917 5b broker <-> executor
private-bus end-to-end tests (tools/test_host_control_executor_dbus.py).

NOT a test module (no test cases; the ``tools/test_host_control_dbus_driver.py``
precedent). ``prepare_tree`` writes one disposable tree: a framework copy
(the "root-owned registry copy" the executor's repo-sourced checks read),
a trusted dir holding the manifest, registry and policy, and a seat
install root. Both the broker and the executor then run as subprocesses:

    python3 -m tools.test_host_control_executor_dbus_driver broker   <config.json>
    python3 -m tools.test_host_control_executor_dbus_driver executor <config.json>

Fixture boundaries, stated so evidence is never relabelled: one Unix uid
plays broker, seat and executor (real cross-uid is T-2924); the approval
binder is an auto-binding fake (HostApproval1 is exercised elsewhere); the
user manager, account and group lookups are fakes (the real user-bus port
is 5c); the installer plan is NARROWED to the classes this fixture
executes without a user manager (diagnostics.read, files.seat_root and,
since T-2917 5d.3, mailbox.probe against a disposable hub under the tree;
never the live post-office hub).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "claude-fallback"
TEMPLATES = (
    "tools/systemd/pantheon-persona-consume@.service",
    "tools/systemd/pantheon-persona-consume@.timer",
    "tools/systemd/pantheon-persona-quota@.service",
    "tools/systemd/pantheon-persona-quota@.timer",
)
# T-5447: the seat-core periodic templates the real plan now READS (rendered into
# seat-local residue ops). They are copied into the framework fixture but are NOT
# registered template_ids: the broker's artifact binding is unchanged.
SEAT_CORE_TEMPLATES = (
    "tools/systemd/pantheon-inbox-consumer@.service",
    "tools/systemd/pantheon-inbox-consumer@.timer",
    "tools/systemd/pantheon-keep-going@.service",
    "tools/systemd/pantheon-keep-going@.timer",
    "tools/systemd/pantheon-restart-drain.service",
    "tools/systemd/pantheon-restart-drain.timer",
    "tools/systemd/pantheon-unit-failure-alert@.service",
    "tools/systemd/pantheon-mailbox-wake@.service",
    "tools/systemd/pantheon-mailbox-wake@.timer",
    "tools/systemd/pantheon-runtime-controller@.service",  # T-5448
)
# T-2917 5d.3: widened by mailbox.probe (the capability flip)
IMPLEMENTED = frozenset(("diagnostics.read", "files.seat_root", "mailbox.probe"))


def _narrow_installer_class():
    from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
    from tools.persona_installer import InstallPlan, PersonaInstaller

    class NarrowInstaller(PersonaInstaller):
        def plan(self, *args, **kwargs):  # type: ignore[override]
            plan = super().plan(*args, **kwargs)
            values = {
                name: getattr(plan, name)
                for name in plan.__dataclass_fields__
                if name != "digest"
            }
            values["operations"] = tuple(
                op for op in plan.operations if EXECUTOR_KIND_CLASSES.get(op.kind) in IMPLEMENTED
            )
            return InstallPlan.create(**values)

    return NarrowInstaller


def _tighten(path: Path) -> None:
    for directory, _dirs, files in os.walk(path):
        Path(directory).chmod(0o755)
        for name in files:
            (Path(directory) / name).chmod(0o644)


def prepare_tree(root: Path) -> dict:
    from tools import test_host_control_dbus_driver as broker_fixture
    from tools.persona_installer import Manifest

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    framework = root / "framework"
    from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS

    # T-5516: the framework copy carries all installer code files (Q3 binding).
    for relative in (*FRAMEWORK_CODE_ARTIFACTS, *TEMPLATES, *SEAT_CORE_TEMPLATES):
        target = framework / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)
    for relative in ("pinned-rules/recovery.md", "rulebook/r.md", "sop/s.md", "adr/a.md"):
        target = framework / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    _tighten(framework)
    trusted = root / "trusted"
    trusted.mkdir()
    trusted.chmod(0o755)
    manifest_path = trusted / "instance.yaml"
    manifest_path.write_text(yaml.safe_dump(broker_fixture.manifest_dict(), sort_keys=False), encoding="utf-8")
    manifest_path.chmod(0o644)
    install_root = root / "inst"
    seat_root = install_root / "home" / ACCOUNT
    seat_root.mkdir(parents=True)
    for path in (install_root, install_root / "home", seat_root):
        path.chmod(0o755)
    # T-2917 5d.3: a disposable mailbox hub (the plan's hub under the
    # install root): the seat's own maildir and the probe peer's
    mailbox_hub = install_root / "mailbox"
    for persona in ("recovery", "orchestrator"):
        for sub in ("tmp", "new", "cur"):
            (mailbox_hub / persona / sub).mkdir(parents=True)
    prototype = _narrow_installer_class()(framework).plan(
        Manifest.load(manifest_path), "recovery", harness_override="codex", root=install_root
    )
    uid = os.getuid()
    registry = {
        "schema": "pantheon-host-seat-registry/v1",
        "seats": [
            {
                "seat_id": "recovery",
                "uid": uid,
                "persona": "recovery",
                "role": "recovery",
                "instance": "test-instance",
                "account": ACCOUNT,
                "harness": "codex",
                "install_root": str(install_root),
            }
        ],
        "manifests": [
            {
                "revision": "manifest-r1",
                "path": str(manifest_path),
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "framework_revision": "test-rev",
                "framework_sha256": prototype.provenance["source_artifact_sha256"],
                "template_ids": list(TEMPLATES),
            }
        ],
    }
    policy = {
        "schema": "pantheon-host-policy/v1",
        "revision": "policy-r1",
        "grants": [
            {
                "subject_seat_id": "recovery",
                "action": "plan",
                "target_seat_id": "recovery",
                "manifest_revisions": ["manifest-r1"],
                "operation_families": ["persona.install"],
                "approval_class": "none",
            }
        ],
    }
    for name, value in (("registry.json", registry), ("policy.json", policy)):
        path = trusted / name
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        path.chmod(0o644)
    (root / "linger").mkdir()
    (root / "linger" / ACCOUNT).write_text("", encoding="utf-8")
    return {
        "root": str(root),
        "framework": str(framework),
        "trusted": str(trusted),
        "seat_root": str(seat_root),
        "journal": str(root / "broker.sqlite3"),
        "mailbox_hub": str(mailbox_hub),
    }


class _AutoBinder:
    """Fixture-only approval: binds every request (HostApproval1 itself is
    proven by the approval suites; this unit proves the executor path)."""

    def validate_and_bind(self, approval_handle, plan_handle, expected_digest, operation_handle):
        from tools.host_control.contracts import BindOutcome

        return BindOutcome(state="bound", authority_record_id=secrets.token_hex(32), reason_code="bound")


class _NoReconciler:
    def reconcile(self, *args):
        from tools.host_control.contracts import RecoveryRefused

        raise RecoveryRefused("reconciler lands in 5d")


def serve_broker(config: dict) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control import wire
    from tools.host_control.approval_source import BrokerApprovalSource
    from tools.host_control.dbus_service import HostBroker1Object, UnavailableApprovalStatus
    from tools.host_control.driver import DriverContext, OperationDriver
    from tools.host_control.execution_source import BrokerExecutionSource
    from tools.host_control.executor_client import DBusExecutorPort, DBusExecutorTransport
    from tools.host_control.journal import BrokerJournal
    from tools.host_control.operations import OperationService
    from tools.host_control.planner import HostPlanService
    from tools.host_control.policy import HostPolicy
    from tools.host_control.ratelimit import BrokerRateLimits, BucketSpec
    from tools.host_control.registry import SeatRegistry
    from tools.host_control.trusted_files import RootOwnedFileLoader

    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    trusted = Path(config["trusted"])
    files = RootOwnedFileLoader._open_for_test((trusted,), expected_owner_uid=os.getuid())
    registry = SeatRegistry.load(trusted / "registry.json", files=files)
    policy = HostPolicy.load(trusted / "policy.json", files=files)
    journal_path = Path(config["journal"])
    journal = BrokerJournal.open(journal_path)
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    handles = lambda: secrets.token_hex(32)  # noqa: E731
    plan_service = HostPlanService(
        registry=registry,
        policy=policy,
        journal=journal,
        installer=_narrow_installer_class()(Path(config["framework"])),
        clock=clock,
        handle_factory=handles,
        plan_ttl=timedelta(hours=1),
        contract_version="org.pantheon.HostBroker1/v1",
        broker_revision="t2917-5b-test",
        host_operation_families=frozenset(("persona.install",)),
        health_check=lambda: "ready",
    )

    def service_for(journal_handle: BrokerJournal, reconciler=None) -> OperationService:
        return OperationService(
            registry=registry,
            journal=journal_handle,
            binder=_AutoBinder(),
            reconciler=reconciler or _NoReconciler(),
            clock=clock,
            handle_factory=handles,
        )

    def driver_context() -> DriverContext:
        # runs IN the driver thread: its own journal handle and its own
        # blocking connection with no main loop attached
        thread_journal = BrokerJournal.open(journal_path)
        outbound = dbus.bus.BusConnection(config["bus_address"], mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        port = DBusExecutorPort(
            registry=registry,
            execution_source=BrokerExecutionSource(journal=thread_journal, registry=registry),
            transport=DBusExecutorTransport(lambda: outbound, timeout=20.0),
        )
        if config.get("reconcile"):
            # T-2917 5d.5 (E29 tier B): the live-state reconciler over the
            # seat executor's read-only reconcile probe
            from tools.host_control.executor_client import ExecutorReconcileProbe
            from tools.host_control.reconciler import BrokerLiveStateReconciler

            reconciler = BrokerLiveStateReconciler(
                journal=thread_journal,
                probe=ExecutorReconcileProbe(port=port, timeout=60.0, poll_interval=0.05),
            )
            return DriverContext(
                service_for(thread_journal, reconciler), thread_journal, port, thread_journal.close, reconcile=True
            )
        return DriverContext(service_for(thread_journal), thread_journal, port, thread_journal.close)

    outcomes_path = Path(config["root"]) / "driver-outcomes.jsonl"

    def record_outcome(handle: str, outcome: str) -> None:
        with outcomes_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"handle": handle, "outcome": outcome}) + "\n")

    driver = OperationDriver(
        context_factory=driver_context,
        poll_schedule=(0.05, 0.1, 0.2),
        collect_timeout=60.0,
        retry_backoff=float(config.get("retry_backoff", 0.2)),
        max_attempts=int(config.get("max_attempts", 5)),
        on_outcome=record_outcome,
    )
    connection = dbus.bus.BusConnection(config["bus_address"])
    bus_name = dbus.service.BusName(wire.BUS_NAME, connection)
    service_object = HostBroker1Object(
        connection,
        registry=registry,
        plan_service=plan_service,
        operation_service=service_for(journal),
        approval_source=BrokerApprovalSource(journal=journal, clock=clock),
        approval_status=UnavailableApprovalStatus(),
        approval_uid=os.getuid() + 54321,
        execution_source=BrokerExecutionSource(journal=journal, registry=registry),
        driver=driver,
        # the tests poll OperationGet; the per-uid limiter itself is
        # proven in tools/test_host_control_ratelimit.py
        rate_limits=BrokerRateLimits(
            mutating=BucketSpec(capacity=1000, refill_per_second=1000.0),
            read=BucketSpec(capacity=100000, refill_per_second=100000.0),
        ),
    )
    driver.start()
    _keepalive = (bus_name, service_object)
    GLib.MainLoop().run()
    del _keepalive
    return 0


def serve_executor(config: dict) -> int:
    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools import _host_control_executor_fixtures as fx
    from tools.host_control.executor.dbus_service import (
        DBusBusIdentity,
        DBusSpecSource,
        ExecutorService,
        HostExecutor1Object,
    )
    from tools.host_control.executor.engine import ExecutorEngine
    from tools.host_control.executor.identity import resolve_executor_identity
    from tools.host_control.executor.mailbox_probe import MailboxProbe
    from tools.host_control.registry import SeatRegistry
    from tools.host_control.trusted_files import RootOwnedFileLoader

    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    uid = os.getuid()
    trusted = Path(config["trusted"])
    registry_files = RootOwnedFileLoader._open_for_test((trusted,), expected_owner_uid=uid)
    registry = SeatRegistry.load(trusted / "registry.json", files=registry_files)
    identity = resolve_executor_identity(getuid=os.getuid, registry=registry)
    framework = Path(config["framework"])
    engine = ExecutorEngine(
        identity=identity,
        framework_files=RootOwnedFileLoader._open_for_test((framework,), expected_owner_uid=uid),
        framework_root=framework,
        user_manager=fx.FakeUserManager(),
        which=lambda name: f"/usr/bin/{name}",
        pwd_lookup=lambda value: SimpleNamespace(pw_name=ACCOUNT, pw_gid=4242),
        group_lookup=lambda name: SimpleNamespace(gr_gid=5151),
        grouplist=lambda account, gid: [gid, 5151],
        linger_dir=Path(config["root"]) / "linger",
        # T-2917 5d.3: the hub from the executor's (fixture) config only
        mailbox_probe=MailboxProbe(hub=Path(config["mailbox_hub"]), uid=uid, clock=time.monotonic),
    )
    if config.get("hold_path"):
        engine = _HeldEngine(engine, config)  # type: ignore[assignment]
    watchdog_seconds = config.get("watchdog_seconds")
    extra = {}
    if watchdog_seconds:
        # T-2917 5d.5 (E29 P9, tier B): the REAL watchdog exit, armed short
        from tools.host_control.executor.dbus_service import _arm_daemon_timer

        extra["watchdog"] = lambda seconds, callback: _arm_daemon_timer(float(watchdog_seconds), callback)
    connection = dbus.bus.BusConnection(config["bus_address"])
    service = ExecutorService(
        **extra,
        identity=identity,
        registry=registry,
        broker_uid=int(config.get("broker_uid", uid)),
        engine=engine,
        spec_source=DBusSpecSource(
            lambda: dbus.bus.BusConnection(config["bus_address"], mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        ),
        bus=DBusBusIdentity(connection),
    )
    bus_name = dbus.service.BusName(identity.bus_name, connection, do_not_queue=True)
    service_object = HostExecutor1Object(connection, service=service)
    _keepalive = (bus_name, service_object)
    GLib.MainLoop().run()
    del _keepalive
    return 0


class _HeldEngine:
    """E29 tier-B fault point (TEST code only; nothing in production): an
    APPLY run whose ``hold_path`` exists marks ``held_marker`` at run entry
    (after the spec was served, before any operation) and waits there until
    ``release_path`` appears or the process is killed."""

    def __init__(self, engine, config: dict) -> None:
        self._engine = engine
        self._config = config

    @property
    def run_budget(self) -> float:
        return self._engine.run_budget

    def run(self, spec):
        hold = Path(self._config["hold_path"])
        if spec.execution_kind == "apply" and hold.exists():
            Path(self._config["held_marker"]).write_text("held\n", encoding="utf-8")
            release = Path(self._config["release_path"])
            deadline = time.monotonic() + 300
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        return self._engine.run(spec)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("broker", "executor"):
        print("usage: test_host_control_executor_dbus_driver broker|executor <config.json>")
        return 2
    config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    return serve_broker(config) if argv[1] == "broker" else serve_executor(config)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
