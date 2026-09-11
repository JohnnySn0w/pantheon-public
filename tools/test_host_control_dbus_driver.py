"""Subprocess service host + shared domain fixture for the T-2912
private-bus integration tests.

NOT a test module: it contains zero unittest test cases or functions;
pytest collection here is deliberately a no-op. It is named test_* so it
sits unambiguously beside its consumer, ``tools/test_host_control_dbus.py``
(PO disposition OQ8, T-2912).

Two halves:

- Fixture library (no dbus imports): builds a real domain stack —
  temp manifest, registry, policy, journal, ``HostPlanService``,
  ``OperationService`` — in a caller-provided root. The test module
  imports these builders for its unguarded provider tests, so there is
  exactly one fixture definition (single-source over duplicate-and-drift).
- Driver entrypoint: ``python3 -m tools.test_host_control_dbus_driver
  <config.json>`` connects to the private bus address in the config,
  exports the ``HostBroker1Object``, writes a state file with the seeded
  handles, and serves a GLib main loop until killed. All stdout/stderr
  is expected to be redirected to files by the spawning test (a pipe
  read to EOF would hang the reader while the daemon holds it open).
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from tools.host_control.contracts import (
    BindOutcome,
    OperationReceipt,
    OperationRecord,
    RollbackIntent,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import OperationService
from tools.host_control.planner import HostPlanService
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer import PersonaInstaller

REPO_ROOT = Path(__file__).resolve().parent.parent

FIXED_NOW = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)

HOST_OPERATION_FAMILIES = frozenset(("persona.install", "persona.verify"))


def manifest_dict() -> dict:
    """Minimal valid persona-installer manifest (core-fixture shape)."""

    return {
        "manifest_version": 2,
        "persona_installer": {
            "schema": "pantheon-persona-install/v1",
            "framework": {"source": "pantheon-config", "revision": "test-rev"},
            "paths": {
                "repo": "pantheon-config",
                "unit_dir": ".config/systemd/user",
                "state_dir": ".local/state/pantheon/persona-installer",
            },
            "mailbox": {
                "hub": "/mailbox",
                "group": "pantheon-mailbox",
                "probe_peer": "orchestrator",
            },
            "systemd": {
                "service_template": (
                    "tools/systemd/pantheon-persona-consume@.service"
                ),
                "timer_template": (
                    "tools/systemd/pantheon-persona-consume@.timer"
                ),
            },
            "policy": {"bundle": ["rulebook/", "sop/", "adr/"]},
            "security": {
                "credential_mode": "references-only",
                "prohibit_auth_copy": True,
            },
            "lifecycle": {
                "require_disabled_first_fire": True,
                "require_mailbox_send_receive": True,
                "require_finite_next_elapse": True,
                "require_observed_timed_fire": True,
            },
            "provenance": {"binding_source": "instance.yaml"},
        },
        "instance": {"id": "test-instance", "harness_default": "claude-code"},
        "roles": {
            "recovery": {
                "persona": "recovery",
                "account": "claude-fallback",
                "harness": "claude-code",
            }
        },
    }


class MutableClock:
    """Aware-UTC clock the provider tests can advance past plan expiry."""

    def __init__(self, start: datetime = FIXED_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class _TripwireBinder:
    """Fails loudly if the transport ever chains past prepare (N4)."""

    def validate_and_bind(self, *args: object) -> BindOutcome:
        raise AssertionError(
            "transport called ValidateAndBind; Apply/Rollback must stop "
            "at the durable PREPARED record in this unit (T-2912 N4)"
        )


class _SeedBinder:
    """Fixture-only binder used to walk the SEEDED subject's operation
    to dispatch_intent so a staged receipt exists for theft tests."""

    def validate_and_bind(
        self,
        approval_handle: str,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
    ) -> BindOutcome:
        return BindOutcome(
            state="bound",
            authority_record_id="seed-authority-1",
            reason_code="approved",
        )


class _SeedExecutor:
    """Fixture-only executor; returns a bounded reference, writes nothing."""

    def execute(
        self,
        operation: OperationRecord,
        staged_receipt: OperationReceipt,
        intents: tuple[RollbackIntent, ...],
    ) -> str:
        return "seed-exec-1"


class _UnusedReconciler:
    def reconcile(self, *args: object) -> object:
        raise AssertionError("reconcile must not run in T-2912 tests")


@dataclass
class DomainFixture:
    root: Path
    files: RootOwnedFileLoader
    registry: SeatRegistry
    policy: HostPolicy
    journal: BrokerJournal
    installer: PersonaInstaller
    clock: MutableClock
    plan_service: HostPlanService
    operation_service: OperationService
    manifest_path: Path

    def close(self) -> None:
        self.journal.close()
        self.files.close()


def _handle_factory() -> Callable[[], str]:
    counter = {"n": 0}

    def next_handle() -> str:
        counter["n"] += 1
        return format(counter["n"], "064x")

    return next_handle


def build_domain(
    root: Path,
    *,
    subject_uid: int,
    seat_b_uid: int = 60001,
    register_subject: bool = True,
    clock: MutableClock | None = None,
    plan_ttl: timedelta = timedelta(hours=1),
    health_check: Callable[[], str] | None = None,
) -> DomainFixture:
    """Build a real domain stack in ``root``.

    ``subject_uid`` is seat ``alpha`` (the live caller in bus tests when
    it equals the caller's real uid). ``seat_b_uid`` is seat ``bravo``,
    the OTHER subject whose artifacts the theft tests try to steal.
    ``register_subject=False`` builds a registry that deliberately does
    NOT contain ``subject_uid`` (gate-9 unregistered-caller runs).
    """

    clock = clock or MutableClock()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    manifest_path = root / "instance.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest_dict(), sort_keys=False),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    installer = PersonaInstaller(REPO_ROOT)
    from tools.persona_installer import Manifest

    prototype = installer.plan(
        Manifest.load(manifest_path),
        "recovery",
        harness_override="codex",
        root=Path("/projection"),
    )
    framework_sha256 = prototype.provenance["source_artifact_sha256"]
    template_ids = [
        "tools/systemd/pantheon-persona-consume@.service",
        "tools/systemd/pantheon-persona-consume@.timer",
        "tools/systemd/pantheon-persona-quota@.service",
        "tools/systemd/pantheon-persona-quota@.timer",
    ]

    seats = [
        {
            "seat_id": "bravo",
            "uid": seat_b_uid,
            "persona": "bravo",
            "role": "worker",
            "instance": "test-instance",
            "account": "bravo",
            "harness": "codex",
            "install_root": "/home/bravo",
        },
        {
            "seat_id": "recovery",
            "uid": 60002,
            "persona": "recovery",
            "role": "recovery",
            "instance": "test-instance",
            "account": "claude-fallback",
            "harness": "codex",
            "install_root": "/projection",
        },
    ]
    if register_subject:
        seats.insert(
            0,
            {
                "seat_id": "alpha",
                "uid": subject_uid,
                "persona": "alpha",
                "role": "worker",
                "instance": "test-instance",
                "account": "alpha",
                "harness": "codex",
                "install_root": "/home/alpha",
            },
        )
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    registry_value = {
        "schema": "pantheon-host-seat-registry/v1",
        "seats": seats,
        "manifests": [
            # two registered revisions of the same bytes: the wire-tier
            # idempotency-conflict test needs a second policy-passing
            # request shape under one idempotency key
            {
                "revision": revision,
                "path": str(manifest_path),
                "sha256": manifest_sha256,
                "framework_revision": "test-rev",
                "framework_sha256": framework_sha256,
                "template_ids": template_ids,
            }
            for revision in ("manifest-r1", "manifest-r2")
        ],
    }
    policy_value = {
        "schema": "pantheon-host-policy/v1",
        "revision": "policy-r1",
        "grants": [
            {
                "subject_seat_id": seat,
                "action": "plan",
                "target_seat_id": "recovery",
                "manifest_revisions": ["manifest-r1", "manifest-r2"],
                "operation_families": ["persona.install"],
                "approval_class": "human",
            }
            for seat in ("alpha", "bravo")
        ],
    }
    registry_path = root / "registry.json"
    policy_path = root / "policy.json"
    for path, value in (
        (registry_path, registry_value),
        (policy_path, policy_value),
    ):
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        path.chmod(0o600)

    import os

    files = RootOwnedFileLoader._open_for_test(
        (root,),
        expected_owner_uid=os.getuid(),
    )
    registry = SeatRegistry.load(registry_path, files=files)
    policy = HostPolicy.load(policy_path, files=files)
    journal = BrokerJournal.open(root / "broker.sqlite3")
    handles = _handle_factory()
    plan_service = HostPlanService(
        registry=registry,
        policy=policy,
        journal=journal,
        installer=installer,
        clock=clock,
        handle_factory=handles,
        plan_ttl=plan_ttl,
        contract_version="org.pantheon.HostBroker1/v1",
        broker_revision="t2912-test",
        host_operation_families=HOST_OPERATION_FAMILIES,
        health_check=health_check or (lambda: "ready"),
    )
    operation_service = OperationService(
        registry=registry,
        journal=journal,
        binder=_TripwireBinder(),
        reconciler=_UnusedReconciler(),
        clock=clock,
        handle_factory=handles,
    )
    return DomainFixture(
        root=root,
        files=files,
        registry=registry,
        policy=policy,
        journal=journal,
        installer=installer,
        clock=clock,
        plan_service=plan_service,
        operation_service=operation_service,
        manifest_path=manifest_path,
    )


def _seed_service(
    fixture: DomainFixture,
    offset: int,
) -> OperationService:
    return OperationService(
        registry=fixture.registry,
        journal=fixture.journal,
        binder=_SeedBinder(),
        reconciler=_UnusedReconciler(),
        clock=fixture.clock,
        handle_factory=_handle_factory_offset(offset),
    )


def seed_other_subject(fixture: DomainFixture, *, seat_b_uid: int = 60001) -> dict:
    """Create seat-bravo artifacts (plan, dispatched operation, staged
    receipt) for the gate-15 cross-subject theft tests. Returns the
    handles a thief would need."""

    plan_result = fixture.plan_service.plan(
        seat_b_uid,
        "recovery",
        "manifest-r1",
        "seed-plan-1",
    )
    seed_operations = _seed_service(fixture, 1 << 32)
    record, _ = seed_operations.prepare_apply(
        seat_b_uid,
        plan_result.plan_handle,
        plan_result.plan_digest,
        "seed-apply-1",
        "seed-approval-1",
    )
    seed_operations.authorize(record.operation_handle)
    seed_operations.dispatch(record.operation_handle, _SeedExecutor())
    staged = fixture.journal.get_staged_receipt(record.operation_handle)
    return {
        "seat_b_plan_handle": plan_result.plan_handle,
        "seat_b_plan_digest": plan_result.plan_digest,
        "seat_b_operation_handle": record.operation_handle,
        "seat_b_receipt_handle": staged.receipt_handle,
    }


def seed_subject_complete_receipt(
    fixture: DomainFixture,
    *,
    subject_uid: int,
) -> dict:
    """Walk one operation for the CALLING subject to a complete receipt
    so the positive ``Rollback`` prepare path is exercisable over the
    bus without this unit owning an executor."""

    plan_result = fixture.plan_service.plan(
        subject_uid,
        "recovery",
        "manifest-r1",
        "seed-alpha-plan-1",
    )
    seed_operations = _seed_service(fixture, 1 << 33)
    record, _ = seed_operations.prepare_apply(
        subject_uid,
        plan_result.plan_handle,
        plan_result.plan_digest,
        "seed-alpha-apply-1",
        "seed-alpha-approval-1",
    )
    seed_operations.authorize(record.operation_handle)
    seed_operations.dispatch(record.operation_handle, _SeedExecutor())
    completed = seed_operations.append_result(
        record.operation_handle,
        ok=True,
        mutations=(),
        evidence=(),
        detail="seed complete",
    )
    subject = fixture.registry.subject_for_uid(subject_uid)
    # fixture-only plumbing: the journal deliberately has no receipt
    # enumeration API, so read the handle back over a separate
    # read-only connection, then re-load through the real API
    import sqlite3

    with sqlite3.connect(
        f"file:{fixture.root / 'broker.sqlite3'}?mode=ro",
        uri=True,
    ) as readback:
        row = readback.execute(
            "SELECT receipt_handle FROM operation_receipts "
            "WHERE operation_handle=? AND phase='complete'",
            (completed.operation_handle,),
        ).fetchone()
    receipt = fixture.journal.get_receipt(
        subject=subject,
        receipt_handle=str(row[0]),
    )
    return {
        "subject_plan_handle": plan_result.plan_handle,
        "subject_plan_digest": plan_result.plan_digest,
        "subject_complete_receipt_handle": receipt.receipt_handle,
    }


def _handle_factory_offset(base: int) -> Callable[[], str]:
    counter = {"n": base}

    def next_handle() -> str:
        counter["n"] += 1
        return format(counter["n"], "064x")

    return next_handle


def serve(config_path: str) -> int:
    """Driver entrypoint: export the service on the private bus named in
    the config and run until killed. See module docstring."""

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control import wire
    from tools.host_control.approval_source import BrokerApprovalSource
    from tools.host_control.dbus_service import (
        HostBroker1Object,
        UnavailableApprovalStatus,
    )

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    root = Path(config["root"])
    fixture = build_domain(
        root,
        subject_uid=int(config["subject_uid"]),
        register_subject=bool(config["register_subject"]),
        clock=MutableClock(datetime.now(timezone.utc)),
    )
    state = seed_other_subject(fixture)
    if bool(config["register_subject"]):
        state.update(
            seed_subject_complete_receipt(
                fixture,
                subject_uid=int(config["subject_uid"]),
            )
        )
    connection = dbus.bus.BusConnection(config["bus_address"])
    bus_name = dbus.service.BusName(wire.BUS_NAME, connection)
    approval_source = BrokerApprovalSource(
        journal=fixture.journal,
        clock=fixture.clock,
    )
    service_object = HostBroker1Object(
        connection,
        registry=fixture.registry,
        plan_service=fixture.plan_service,
        operation_service=fixture.operation_service,
        approval_source=approval_source,
        approval_status=UnavailableApprovalStatus(),
        approval_uid=int(config["approval_uid"]),
    )
    state_path = root / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    # keep the name and object alive for the life of the loop: releasing
    # the BusName reference would drop the well-known name ownership
    _keepalive = (bus_name, service_object)
    GLib.MainLoop().run()
    del _keepalive
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_host_control_dbus_driver <config.json>")
        return 2
    return serve(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
