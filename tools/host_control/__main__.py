"""Production broker daemon entrypoint (T-2920 U2; ORG-052 sections 3-6).

``python3 -m tools.host_control`` takes NO arguments: the config path is fixed
(``broker_config.DEFAULT_BROKER_CONFIG_PATH``) and every input is root-owned
data. The staged unit (``dist/pantheon-host-broker.service``) runs it, through
the ``/usr/local/libexec/pantheon-host-broker`` launcher the T-2920 installer
materializes, as ``User=pantheon-hostbroker``. Nothing here installs or starts
anything; ``run`` carries the seams the tests use (test-mode trusted loader,
private-bus connection factory, injected euid).

Wiring (the T-2920 carry-ins, each asserted by tools/test_host_control_daemon_entrypoints.py):

* euid 0 is refused before any file is read (ORG-052 gate 19; the runtime
  table refuses again);
* the seat registry is loaded WITH the configured framework copy (T-5533 B1:
  Plan() refuses without it);
* ``load_daemon_runtime`` digests the op registry of THAT framework copy and the
  canonical ``startup_record()`` is logged on one line, for comparison with the
  root-side gate-32a digest (T-2924);
* the operation driver's context sets ``reconcile=True`` with a
  ``BrokerLiveStateReconciler(ExecutorReconcileProbe(DBusExecutorPort))``
  (T-2917 D25) and ``compensate`` from the config (default OFF, HO ruling Q4);
* ``make_broker_health_provider`` gets ``operation_driver_probe`` and the
  driver-cached ``BusReachability`` legs (T-2917 MR-2), so a broker with zero
  executors reachable starts DEGRADED, never crashes.

Thread model (driver.py): the driver thread owns its own journal handle, its
own blocking bus connection, the real approval binder and the reconciler. The
GLib main loop only prepares and submits; its OperationService has fail-closed
binder/reconciler stand-ins, because a ValidateAndBind on the loop would wait
on HostApproval1, which calls back PlanMetadata on this same loop.

Exit codes: 0 clean stop (SIGTERM/SIGINT); 78 (EX_CONFIG) start refused, always
BEFORE any bus connection; 69 (EX_UNAVAILABLE) the broker bus name could not be
claimed.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import signal
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from tools.host_control.approval_client import ApprovalConsumerClient, DBusConsumerTransport
from tools.host_control.approval_source import BrokerApprovalSource
from tools.host_control.broker_config import (
    DEFAULT_BROKER_CONFIG_PATH,
    BrokerConfig,
    load_broker_config,
)
from tools.host_control.contracts import ApprovalUnavailable, HostControlError, RecoveryRefused
from tools.host_control.driver import (
    DriverContext,
    OperationDriver,
    approval_reachability_probe,
    executor_reachability_probe,
    operation_driver_probe,
)
from tools.host_control.execution_source import BrokerExecutionSource
from tools.host_control.executor_client import (
    BusReachability,
    DBusExecutorPort,
    DBusExecutorTransport,
    ExecutorReconcileProbe,
)
from tools.host_control.health import HealthProvider, constant_probe, make_broker_health_provider
from tools.host_control.journal import USER_VERSION as BROKER_JOURNAL_VERSION
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import OperationService
from tools.host_control.planner import HostPlanService
from tools.host_control.policy import HostPolicy
from tools.host_control.reconciler import BrokerLiveStateReconciler
from tools.host_control.registry import SeatRegistry
from tools.host_control.runtime_table import (
    DaemonRuntime,
    DaemonStartRefused,
    load_checkout_op_registry,
    load_daemon_runtime,
)
from tools.host_control.trusted_files import RootOwnedFileLoader

EXIT_START_REFUSED = 78
EXIT_NAME_UNAVAILABLE = 69
#: the public contract version this revision serves (ORG-052 section 4)
CONTRACT_VERSION = "org.pantheon.HostBroker1/v1"
LOG_PREFIX = "host-broker"
STARTUP_RECORD_TAG = "startup-record"
#: the seat registry's own read cap (registry.py SeatRegistry.load)
REGISTRY_MAX_BYTES = 1024 * 1024

#: ``connect(main_loop)``: a NEW bus connection. ``main_loop=True`` is attached
#: to the default GLib loop (the service connection); ``False`` is blocking-only
#: (the driver thread's outbound calls, the ApprovalGet status calls).
Connect = Callable[[bool], object]


def system_bus(main_loop: bool) -> object:
    import dbus
    import dbus.mainloop

    if main_loop:
        return dbus.SystemBus(private=True)
    return dbus.SystemBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)


def _log(message: str) -> None:
    print(f"{LOG_PREFIX}: {message}", file=sys.stderr, flush=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_handle() -> str:
    return secrets.token_hex(32)


class LoopBinder:
    """The main loop never binds (see module docstring): fail closed."""

    def validate_and_bind(self, *args: object) -> object:
        raise ApprovalUnavailable("binding runs on the operation driver thread only")


class LoopReconciler:
    """The main loop never reconciles (see module docstring): fail closed."""

    def reconcile(self, *args: object) -> object:
        raise RecoveryRefused("reconciliation runs on the operation driver thread only")


class _LazyConnection:
    """One blocking connection, made on first use in the calling thread."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect
        self._connection: object | None = None

    def __call__(self) -> object:
        if self._connection is None:
            self._connection = self._connect(False)
        return self._connection


@dataclass
class BrokerStack:
    """Everything the broker builds BEFORE its first bus connection."""

    config: BrokerConfig
    files: RootOwnedFileLoader
    registry: SeatRegistry
    policy: HostPolicy
    runtime: DaemonRuntime
    journal: BrokerJournal
    driver: OperationDriver
    health: HealthProvider
    plan_service: HostPlanService
    operation_service: OperationService
    context_factory: Callable[[], DriverContext]

    def close(self) -> None:
        self.driver.stop()
        self.journal.close()
        self.files.close()


def driver_context_factory(
    *,
    config: BrokerConfig,
    registry: SeatRegistry,
    connect: Connect,
) -> Callable[[], DriverContext]:
    """The production ``DriverContext``; the factory runs IN the driver thread."""

    def factory() -> DriverContext:
        journal = BrokerJournal.open(config.journal_path)
        outbound = connect(False)
        port = DBusExecutorPort(
            registry=registry,
            execution_source=BrokerExecutionSource(journal=journal, registry=registry),
            transport=DBusExecutorTransport(lambda: outbound),
        )
        reconciler = BrokerLiveStateReconciler(journal=journal, probe=ExecutorReconcileProbe(port=port))
        binder = ApprovalConsumerClient(
            transport=DBusConsumerTransport(lambda: outbound),
            ownership=journal,
        )
        service = OperationService(
            registry=registry,
            journal=journal,
            binder=binder,
            reconciler=reconciler,
            clock=_utc_now,
            handle_factory=_new_handle,
        )

        def close() -> None:
            journal.close()
            closer = getattr(outbound, "close", None)
            if callable(closer):
                closer()

        return DriverContext(
            service,
            journal,
            port,
            close,
            reconcile=True,
            reachability=BusReachability(
                registry=registry,
                transport=DBusExecutorTransport(lambda: outbound),
                approval_uid=config.approval_uid,
            ),
            compensate=config.compensate,
        )

    return factory


def build_broker(
    config: BrokerConfig,
    files: RootOwnedFileLoader,
    *,
    connect: Connect,
    geteuid: Callable[[], int] = os.geteuid,
    log: Callable[[str], None] = _log,
) -> BrokerStack:
    """Load and assemble the broker. Raises ``HostControlError`` or
    ``DaemonStartRefused``; opens no bus connection (the driver is built, not
    started)."""

    registry = SeatRegistry.load(config.registry_path, files=files, framework_root=config.framework_root)
    policy = HostPolicy.load(config.policy_path, files=files)
    runtime = load_daemon_runtime(
        host_operation_families=config.host_operation_families,
        registry_loader=lambda: load_checkout_op_registry(config.framework_root),
        geteuid=geteuid,
    )
    log(f"{STARTUP_RECORD_TAG} {runtime.startup_record()}")
    registry_digest = hashlib.sha256(
        files.read_bytes(config.registry_path, max_bytes=REGISTRY_MAX_BYTES)
    ).hexdigest()
    journal = BrokerJournal.open(config.journal_path)
    try:
        factory = driver_context_factory(config=config, registry=registry, connect=connect)
        driver = OperationDriver(context_factory=factory)
        health = make_broker_health_provider(
            journal_probe=constant_probe(revision=f"broker-journal-v{BROKER_JOURNAL_VERSION}"),
            registry_probe=constant_probe(revision=f"seat-registry-{registry_digest[:16]}"),
            policy_probe=constant_probe(revision=policy.revision),
            approval_reachability_probe=approval_reachability_probe(driver),
            executor_reachability_probe=executor_reachability_probe(driver),
            operation_driver_probe=operation_driver_probe(driver),
        )
        plan_service = HostPlanService(
            registry=registry,
            policy=policy,
            journal=journal,
            clock=_utc_now,
            handle_factory=_new_handle,
            plan_ttl=timedelta(seconds=config.plan_ttl_seconds),
            contract_version=CONTRACT_VERSION,
            broker_revision=config.broker_revision,
            host_operation_families=config.host_operation_families,
            health_check=health.capabilities_health,
            runtime=runtime,
            # gate 36: the configured trusted-config roots and framework copy
            protected_roots=(*config.trusted_roots, config.framework_root),
        )
        operation_service = OperationService(
            registry=registry,
            journal=journal,
            binder=LoopBinder(),
            reconciler=LoopReconciler(),
            clock=_utc_now,
            handle_factory=_new_handle,
        )
    except BaseException:
        journal.close()
        raise
    return BrokerStack(
        config=config,
        files=files,
        registry=registry,
        policy=policy,
        runtime=runtime,
        journal=journal,
        driver=driver,
        health=health,
        plan_service=plan_service,
        operation_service=operation_service,
        context_factory=factory,
    )


def run(
    *,
    config_path: Path = DEFAULT_BROKER_CONFIG_PATH,
    open_loader: Callable[[tuple[Path, ...]], RootOwnedFileLoader] = RootOwnedFileLoader.open,
    geteuid: Callable[[], int] = os.geteuid,
    connect: Connect = system_bus,
    run_loop: bool = True,
    log: Callable[[str], None] = _log,
) -> int:
    if geteuid() == 0:
        log("start refused: the broker runs unprivileged; refusing euid 0 (ORG-052 gate 19)")
        return EXIT_START_REFUSED
    files: RootOwnedFileLoader | None = None
    try:
        with open_loader((config_path.parent,)) as bootstrap:
            config = load_broker_config(config_path, files=bootstrap)
        files = open_loader(config.trusted_roots)
        stack = build_broker(config, files, connect=connect, geteuid=geteuid, log=log)
    except (HostControlError, DaemonStartRefused, OSError, sqlite3.Error) as exc:
        if files is not None:
            files.close()
        log(f"start refused: {type(exc).__name__}")
        return EXIT_START_REFUSED

    import dbus
    import dbus.exceptions
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control import wire
    from tools.host_control.dbus_service import HostBroker1Object
    from tools.host_control.health_dbus import Health1Object, broker_health_path

    try:
        dbus.mainloop.glib.threads_init()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        connection = connect(True)
        try:
            bus_name = dbus.service.BusName(wire.BUS_NAME, connection, do_not_queue=True)
        except dbus.exceptions.DBusException:
            log("bus name unavailable")
            return EXIT_NAME_UNAVAILABLE
        status_connection = _LazyConnection(connect)
        service_object = HostBroker1Object(
            connection,
            registry=stack.registry,
            plan_service=stack.plan_service,
            operation_service=stack.operation_service,
            approval_source=BrokerApprovalSource(journal=stack.journal, clock=_utc_now),
            approval_status=ApprovalConsumerClient(
                transport=DBusConsumerTransport(status_connection),
                ownership=stack.journal,
            ),
            approval_uid=stack.config.approval_uid,
            execution_source=BrokerExecutionSource(journal=stack.journal, registry=stack.registry),
            driver=stack.driver,
        )
        health_object = Health1Object(
            connection,
            object_path=broker_health_path(),
            provider=stack.health,
            monitor_uids=stack.config.monitor_uids,
        )
        stack.driver.start()
        log(f"serving {wire.BUS_NAME} (compensate={'on' if stack.config.compensate else 'off'})")
        _keepalive = (bus_name, service_object, health_object)
        if run_loop:
            loop = GLib.MainLoop()

            def stop() -> bool:
                loop.quit()
                return False

            for signum in (signal.SIGTERM, signal.SIGINT):
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, stop)
            loop.run()
        del _keepalive
        return 0
    finally:
        stack.close()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        print("usage: python3 -m tools.host_control (no arguments)", file=sys.stderr)
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
