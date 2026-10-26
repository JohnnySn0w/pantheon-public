"""PROVISIONAL executor entrypoint (T-2917 5b, ruling B5).

``python3 -m tools.host_control.executor`` takes NO arguments: the config
path is fixed, every identity input is root-owned data plus ``getuid()``.
T-2920 owns the final install path, unit wiring and config schema; nothing
in this unit installs or starts it. ``run`` carries the seams the tests use
(private bus factory, test-mode trusted loader, injected uid).

Exit codes: 0 clean stop; 78 (EX_CONFIG) start refused (config/registry
invalid, or this uid is not a registered seat), checked BEFORE any bus
connection is made; 69 (EX_UNAVAILABLE) the executor bus name could not be
claimed.

The user manager (T-2917 5c.2) is ``DBusUserManager(uid=<registry uid>)``:
the seat's own user bus at its registry-derived address, built only after
identity resolution and connected lazily on first use.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from tools.host_control.contracts import HostControlError
from tools.host_control.executor.config import (
    DEFAULT_CONFIG_PATH,
    load_executor_config,
    require_hub_outside_seat_root,
    require_hub_without_symlinks,
)
from tools.host_control.executor.engine import ExecutorEngine
from tools.host_control.executor.identity import resolve_executor_identity
from tools.host_control.executor.mailbox_probe import MailboxProbe
from tools.host_control.executor.user_manager import UserManagerPort
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader

EXIT_START_REFUSED = 78
EXIT_NAME_UNAVAILABLE = 69


def run(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    open_loader: Callable[[tuple[Path, ...]], RootOwnedFileLoader] = RootOwnedFileLoader.open,
    getuid: Callable[[], int] = os.getuid,
    connection_factory: Callable[[], object] | None = None,
    user_manager: UserManagerPort | None = None,
    run_loop: bool = True,
) -> int:
    try:
        with open_loader((config_path.parent,)) as bootstrap:
            config = load_executor_config(config_path, files=bootstrap)
        files = open_loader(config.trusted_roots)
        registry = SeatRegistry.load(config.registry_path, files=files)
        identity = resolve_executor_identity(getuid=getuid, registry=registry)
        # T-2917 5d.3 (T-2917:D19): the mailbox hub comes from the root-owned
        # config only and never overlaps the seat root
        require_hub_outside_seat_root(config, identity.seat_root)
        # T-2917:D33: no symlinked hub component (lstat from / downward),
        # before any bus connection
        require_hub_without_symlinks(config.mailbox_hub)
        mailbox = MailboxProbe(hub=config.mailbox_hub, uid=identity.uid, clock=time.monotonic)
    except HostControlError as exc:
        print(f"host-executor: start refused: {type(exc).__name__}", file=sys.stderr)
        return EXIT_START_REFUSED

    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control.executor.dbus_service import (
        DBusBusIdentity,
        DBusSpecSource,
        ExecutorService,
        HostExecutor1Object,
    )

    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    factory = connection_factory or (lambda: dbus.SystemBus(private=True))
    connection = factory()
    if user_manager is None:
        # T-2917 5c.2 (gate 39): the seat's user manager over the seat's
        # OWN user bus, its address derived from the REGISTRY uid; the
        # runtime_root test override is never passed here (ruling D7).
        from tools.host_control.executor.user_bus import DBusUserManager

        user_manager = DBusUserManager(uid=identity.uid)
    engine = ExecutorEngine(
        identity=identity,
        framework_files=files,
        framework_root=config.framework_root,
        user_manager=user_manager,
        mailbox_probe=mailbox,
    )
    service = ExecutorService(
        identity=identity,
        registry=registry,
        broker_uid=config.broker_uid,
        engine=engine,
        spec_source=DBusSpecSource(factory),
        bus=DBusBusIdentity(connection),
    )
    try:
        bus_name = dbus.service.BusName(
            identity.bus_name, connection, do_not_queue=True
        )
    except dbus.exceptions.DBusException:
        print("host-executor: bus name unavailable", file=sys.stderr)
        return EXIT_NAME_UNAVAILABLE
    service_object = HostExecutor1Object(connection, service=service)
    _keepalive = (bus_name, service_object)
    if run_loop:
        GLib.MainLoop().run()
    del _keepalive
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        print("usage: python3 -m tools.host_control.executor (no arguments)", file=sys.stderr)
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
