"""Child-process side of the E29 tier-A journal-cut matrix (T-2917 5d.5;
``tools/test_host_control_e29_matrix.py``). NOT a test module.

``python3 -m tools._host_control_e29_child <config.json>`` reopens the
parent's disposable broker domain (registry + journal on disk), runs ONE
scripted step of the operation lifecycle and, at the configured cut point,
writes ``CUT`` to the inherited pipe and blocks. The parent then SIGKILLs
this process (its own child), reopens the journal and runs recovery. Cut
point P9 instead ends the process through the executor service's real
hard-deadline watchdog exit (``EXIT_HARD_DEADLINE``).

Every fake here is fixture-only: the approval binder persists its bindings
in a JSON file so the parent sees the binding the killed process made, and
the executor port records the execution it "holds" the same way.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BLOCK_SECONDS = 300.0  # a stray child never outlives this
SERVE_WAIT_SECONDS = 60.0  # P9: the watchdog exit waits at most this long for spec_served (T-4982)


def _load(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _store(path: str | Path, value: dict) -> None:
    temporary = Path(f"{path}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class FileBinder:
    """Fixture approval: identical-tuple binds return the ORIGINAL binding
    (HostApproval1's documented retry semantics), persisted across the
    kill so the parent observes what the killed process bound."""

    def __init__(self, path: str | Path, *, cut=None) -> None:
        self.path = path
        self.cut = cut
        self.calls: list[tuple[str, ...]] = []

    def validate_and_bind(self, approval_handle, plan_handle, expected_digest, operation_handle):
        from tools.host_control.contracts import BindOutcome

        key = (approval_handle, plan_handle, expected_digest, operation_handle)
        self.calls.append(key)
        bindings = _load(self.path)
        existing = bindings.get(operation_handle)
        if existing is not None:
            if tuple(existing["tuple"]) != key:
                return BindOutcome(state="refused", authority_record_id=None, reason_code="tuple_mismatch")
            return BindOutcome(state="bound", authority_record_id=existing["authority"], reason_code="bound")
        authority = secrets.token_hex(32)
        bindings[operation_handle] = {"tuple": list(key), "authority": authority}
        _store(self.path, bindings)
        if self.cut is not None:
            self.cut()  # killed AFTER the approval side bound
        return BindOutcome(state="bound", authority_record_id=authority, reason_code="bound")


def _cut(fd: int) -> None:
    os.write(fd, b"CUT\n")
    threading.Event().wait(BLOCK_SECONDS)
    os._exit(3)


def main(argv: list[str]) -> int:
    from tools.host_control.driver import DriverContext, DriverCore
    from tools.host_control.execution_source import BrokerExecutionSource
    from tools.host_control.executor import wire as executor_wire
    from tools.host_control.executor_client import CollectedResult
    from tools.host_control.journal import BrokerJournal
    from tools.host_control.operations import OperationService
    from tools.host_control.registry import SeatRegistry
    from tools.host_control.trusted_files import RootOwnedFileLoader

    config = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    fd = int(config["fd"])
    point, kind = config["point"], config["kind"]
    root = Path(config["root"])
    files = RootOwnedFileLoader._open_for_test((root,), expected_owner_uid=os.getuid())
    registry = SeatRegistry.load(root / "registry.json", files=files)
    journal = BrokerJournal.open(root / "broker.sqlite3")
    now = datetime.fromisoformat(config["now"])
    cut = lambda: _cut(fd)  # noqa: E731
    source = BrokerExecutionSource(journal=journal, registry=registry)

    if point in ("P5", "P9"):
        # this process is the EXECUTOR: the spec is served to it, then it dies
        handle = config["handle"]
        record = journal.load_operation_unbound(handle)
        digest = source.expected_digest(record)
        if point == "P5":
            source.operation_spec(int(config["seat_uid"]), handle, digest)
            cut()
        from tools.host_control.executor import dbus_service

        class StuckEngine:
            run_budget = 0.0

            def run(self, parsed):
                threading.Event().wait(BLOCK_SECONDS)

        # P9 must die AFTER the broker journaled spec_served: the 0.2 s timer
        # may fire first on a loaded runner (T-4982), so its exit callback
        # waits for the serve to be committed, then takes the REAL exit path
        served = threading.Event()

        class Spec:
            def operation_spec(self, broker_unique_name, operation_handle, expected_digest):
                spec = source.operation_spec(int(config["seat_uid"]), operation_handle, expected_digest)
                served.set()
                return spec

        def after_serve(callback):
            def fire():
                served.wait(SERVE_WAIT_SECONDS)  # never forever: an unserved P9 still exits, and the parent fails loudly
                callback()

            return fire

        bus = SimpleNamespace(unix_user=lambda name: 4242, name_owner=lambda name: ":1.1")
        with mock.patch.object(dbus_service, "parse_spec", return_value=SimpleNamespace(execution_kind=kind)):
            service = dbus_service.ExecutorService(
                identity=SimpleNamespace(),  # type: ignore[arg-type]
                registry=registry,
                broker_uid=4242,
                engine=StuckEngine(),  # type: ignore[arg-type]
                spec_source=Spec(),
                bus=bus,  # type: ignore[arg-type]
                # the worker runs in THIS thread (the journal's sqlite
                # connection is thread-bound); the watchdog timer thread
                # takes the REAL exit path, armed short (budget + grace
                # scaled down)
                start_worker=lambda work: work(),
                watchdog=lambda seconds, callback: dbus_service._arm_daemon_timer(0.2, after_serve(callback)),
            )
            service.execute(":1.2", handle, digest)
            threading.Event().wait(BLOCK_SECONDS)
        return 4  # never reached: the watchdog exits the process with EXIT_HARD_DEADLINE

    binder = FileBinder(config["binder_state"], cut=cut if point == "P1" else None)

    class Port:
        def __init__(self) -> None:
            self.executions = 0

        def ready(self, operation) -> bool:
            if point == "P2":
                cut()  # authorized; DISPATCH_INTENT never written
            return True

        def execute(self, operation, staged_receipt, intents) -> str:
            if point == "P3":
                cut()  # DISPATCH_INTENT durable; Execute never sent
            self.executions += 1
            reference = secrets.token_hex(32)
            state = _load(config["executor_state"])
            state[operation.operation_handle] = reference
            _store(config["executor_state"], state)
            if point == "P4":
                cut()  # the executor holds the execution; the reply is lost
            return reference

        def collect(self, operation, execution_ref):
            if point == "P6":
                cut()  # executor done; broker killed before collect
            result = executor_wire.ExecutionResult(ok=True, mutations=(), evidence=(), detail="done")
            return CollectedResult(ok=True, result=result)

    service = OperationService(
        registry=registry,
        journal=journal,
        binder=binder,
        reconciler=SimpleNamespace(reconcile=lambda *args: (_ for _ in ()).throw(AssertionError("no reconcile"))),
        clock=lambda: now,
        handle_factory=lambda: secrets.token_hex(32),
    )
    if point == "P7":
        real_append = service.append_result

        def append_after_cut(*args, **kwargs):
            cut()  # collected; killed before the append
            return real_append(*args, **kwargs)

        service.append_result = append_after_cut  # type: ignore[method-assign]
    if point == "P8":
        real_update = BrokerJournal._guarded_update  # a staticmethod

        def update_then_cut(connection, *, current, new):
            real_update(connection, current=current, new=new)
            if new.state in ("completed", "failed"):
                cut()  # INSIDE record_terminal's transaction, before COMMIT

        BrokerJournal._guarded_update = staticmethod(update_then_cut)  # type: ignore[method-assign]

    subject = int(config["subject_uid"])
    if kind == "apply":
        record, _ = service.prepare_apply(subject, config["plan_handle"], config["plan_digest"], config["key"], "ab" * 32)
    elif kind == "verify":
        record, _ = service.prepare_verify(subject, "recovery", config["plan_handle"], config["key"])
    else:
        record, _ = service.prepare_rollback(subject, config["receipt_handle"], config["key"], "cd" * 32)
    _store(config["handle_out"], {"handle": record.operation_handle})
    if point == "P0":
        cut()  # PREPARED; nothing bound
    core = DriverCore(
        DriverContext(service, journal, Port(), lambda: None, reconcile=True),
        sleeper=lambda seconds: None,
        collect_timeout=0.0,
    )
    core.drive(record.operation_handle)
    return 5  # the cut point was never reached


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
