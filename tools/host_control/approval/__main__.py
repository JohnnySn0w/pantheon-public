"""Production approval daemon entrypoint (T-2920 U2; ORG-052 section 5).

``python3 -m tools.host_control.approval`` takes NO arguments: the config path
is fixed (``approval.config.DEFAULT_APPROVAL_CONFIG_PATH``) and every input is
root-owned data. The staged unit (``dist/pantheon-host-approval.service``) runs
it, through the ``/usr/local/libexec/pantheon-host-approval`` launcher the T-2920
installer materializes, as ``User=pantheon-hostapproval``. Nothing here installs
or starts anything; ``run`` carries the seams the tests use.

Wiring:

* euid 0 is refused before any file is read (ORG-052 gate 19);
* the approver policy is read through the root-owned loader and the
  ``HumanIngressRegistration`` comes from the root config (T-2919: chat id ==
  user id, one human per ingress uid); ``ApprovalService`` cross-checks it
  against the policy at construction, before any bus connection;
* plan metadata comes from the live broker over its OWN connection (the
  handlers make blocking broker calls from inside the GLib dispatch), cards go
  to the separately supervised ingress asynchronously over another
  (``DbusTelegramPresenter`` + ``GLibScheduler``, T-5518);
* at start EVERY still-pending human request is re-armed
  (``resume_all_human_deliveries``: a keyset walk that terminates by
  construction), not only the first page (T-5518; T-2920 U2 ruling);
* ``org.pantheon.Health1`` is exported for the monitor uids. Its
  ``broker_metadata_reachability`` leg is honestly ``not_configured``: a Health
  Get must never make a blocking bus call on the loop.

Exit codes: 0 clean stop (SIGTERM/SIGINT); 78 (EX_CONFIG) start refused, always
BEFORE any bus connection; 69 (EX_UNAVAILABLE) the approval bus name could not
be claimed.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from tools.host_control.approval.config import (
    DEFAULT_APPROVAL_CONFIG_PATH,
    ApprovalConfig,
    load_approval_config,
)
from tools.host_control.approval.contracts import MetadataUnavailable, PlanMetadata
from tools.host_control.approval.journal import SCHEMA_DIGEST as APPROVAL_JOURNAL_DIGEST
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import (
    ApproverPolicy,
    PolicyApproverAuthorizer,
    load_approver_policy,
)
from tools.host_control.approval.presentation import AsyncHumanCardPresenter, DeliveryScheduler
from tools.host_control.approval.service import ApprovalService, PlanMetadataSource
from tools.host_control.approval.telegram_presenter import DbusTelegramPresenter, GLibScheduler
from tools.host_control.contracts import HostControlError
from tools.host_control.health import (
    HealthProvider,
    constant_probe,
    make_approval_health_provider,
    unavailable_probe,
)
from tools.host_control.trusted_files import RootOwnedFileLoader

EXIT_START_REFUSED = 78
EXIT_NAME_UNAVAILABLE = 69
LOG_PREFIX = "host-approval"
RESUME_PAGE = 100

#: ``connect(main_loop)``: a NEW bus connection; ``True`` attaches it to the
#: default GLib loop (async replies), ``False`` is blocking-only.
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


@dataclass
class ApprovalStack:
    """Everything the approval service builds BEFORE its first bus connection."""

    config: ApprovalConfig
    files: RootOwnedFileLoader
    policy: ApproverPolicy
    journal: ApprovalJournal
    service: ApprovalService
    health: HealthProvider

    def close(self) -> None:
        self.journal.close()
        self.files.close()


def build_approval(
    config: ApprovalConfig,
    files: RootOwnedFileLoader,
    *,
    metadata_source: PlanMetadataSource,
    card_presenter: AsyncHumanCardPresenter,
    delivery_scheduler: DeliveryScheduler,
) -> ApprovalStack:
    """Load and assemble the approval service. Raises ``HostControlError``;
    the seams it is handed must connect lazily (no bus traffic here)."""

    policy = load_approver_policy(files, config.approver_policy_path)
    journal = ApprovalJournal.open(config.journal_path)
    try:
        service = ApprovalService(
            journal=journal,
            metadata_source=metadata_source,
            clock=_utc_now,
            authorizer=PolicyApproverAuthorizer(policy),
            registered_consumer_uids=frozenset((config.broker_uid,)),
            human_ingress=config.human_ingress,
            card_presenter=card_presenter,
            delivery_scheduler=delivery_scheduler,
        )
        health = make_approval_health_provider(
            journal_probe=constant_probe(revision=f"approval-journal-{APPROVAL_JOURNAL_DIGEST[:16]}"),
            approver_policy_probe=constant_probe(revision=policy.revision),
            broker_metadata_reachability_probe=unavailable_probe(
                reason="broker metadata reachability is not probed by this revision (no blocking Health call)",
                revision="T-2920-U2",
            ),
        )
    except BaseException:
        journal.close()
        raise
    return ApprovalStack(
        config=config,
        files=files,
        policy=policy,
        journal=journal,
        service=service,
        health=health,
    )


class _LazyMetadata:
    """``PlanMetadataSource`` whose broker client (and its connection) is made
    on first use, so construction stays bus-free. A failure to connect is
    ``MetadataUnavailable`` (the seam's fail-closed outage error)."""

    def __init__(self, make: Callable[[], PlanMetadataSource]) -> None:
        self._make = make
        self._client: PlanMetadataSource | None = None

    def _get(self) -> PlanMetadataSource:
        if self._client is None:
            try:
                self._client = self._make()
            except Exception as exc:
                raise MetadataUnavailable("broker metadata connection unavailable") from exc
        return self._client

    def plan_metadata(self, plan_handle: str) -> PlanMetadata:
        return self._get().plan_metadata(plan_handle)

    def plan_summary(self, plan_handle: str, expected_digest: str) -> tuple[str, str]:
        return self._get().plan_summary(plan_handle, expected_digest)


class _LazyConnection:
    def __init__(self, connect: Connect, main_loop: bool) -> None:
        self._connect = connect
        self._main_loop = main_loop
        self._connection: object | None = None

    def __call__(self) -> object:
        if self._connection is None:
            self._connection = self._connect(self._main_loop)
        return self._connection


def run(
    *,
    config_path: Path = DEFAULT_APPROVAL_CONFIG_PATH,
    open_loader: Callable[[tuple[Path, ...]], RootOwnedFileLoader] = RootOwnedFileLoader.open,
    geteuid: Callable[[], int] = os.geteuid,
    connect: Connect = system_bus,
    run_loop: bool = True,
    log: Callable[[str], None] = _log,
    resume_page: int = RESUME_PAGE,
) -> int:
    if geteuid() == 0:
        log("start refused: the approval service runs unprivileged; refusing euid 0 (ORG-052 gate 19)")
        return EXIT_START_REFUSED

    metadata_connection = _LazyConnection(connect, main_loop=False)
    presenter_connection = _LazyConnection(connect, main_loop=True)

    def make_metadata() -> PlanMetadataSource:
        from tools.host_control.approval.broker_client import BrokerPlanMetadataClient

        return BrokerPlanMetadataClient(metadata_connection())

    files: RootOwnedFileLoader | None = None
    try:
        with open_loader((config_path.parent,)) as bootstrap:
            config = load_approval_config(config_path, files=bootstrap)
        files = open_loader(config.trusted_roots)
        stack = build_approval(
            config,
            files,
            metadata_source=_LazyMetadata(make_metadata),
            card_presenter=DbusTelegramPresenter(presenter_connection),
            delivery_scheduler=GLibScheduler(),
        )
    except (HostControlError, OSError, sqlite3.Error) as exc:
        if files is not None:
            files.close()
        log(f"start refused: {type(exc).__name__}")
        return EXIT_START_REFUSED

    import dbus
    import dbus.exceptions
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control.approval import wire
    from tools.host_control.approval.dbus_service import HostApproval1Object
    from tools.host_control.approval.identity import ApproverResolver
    from tools.host_control.health_dbus import Health1Object, approval_health_path

    try:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        connection = connect(True)
        try:
            bus_name = dbus.service.BusName(wire.BUS_NAME, connection, do_not_queue=True)
        except dbus.exceptions.DBusException:
            log("bus name unavailable")
            return EXIT_NAME_UNAVAILABLE
        service_object = HostApproval1Object(
            connection,
            service=stack.service,
            approver_resolver=ApproverResolver(stack.policy),
            consumer_uid=stack.config.broker_uid,
            ingress_uid=stack.config.human_ingress.uid,
        )
        health_object = Health1Object(
            connection,
            object_path=approval_health_path(),
            provider=stack.health,
            monitor_uids=stack.config.monitor_uids,
        )
        # T-5518: every pending card resumes its Present (async; the replies
        # are processed once the loop runs)
        resumed = stack.service.resume_all_human_deliveries(page=resume_page)
        log(
            f"serving {wire.BUS_NAME} (policy {stack.policy.revision}, "
            f"journal {APPROVAL_JOURNAL_DIGEST[:16]}, resumed {resumed} pending human requests)"
        )
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
        print("usage: python3 -m tools.host_control.approval (no arguments)", file=sys.stderr)
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
