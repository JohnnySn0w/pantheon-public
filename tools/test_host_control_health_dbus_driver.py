"""Private-bus driver for T-2914 Health1 integration tests."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Callable, Mapping

from tools.host_control.approval.policy import parse_approver_policy
from tools.host_control.approval_source import BrokerApprovalSource
from tools.host_control.contracts import PlanEnvelope, compute_effect_digest, sha256_json
from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS
from tools.host_control.dbus_service import UnavailableApprovalStatus
from tools.host_control.health import (
    APPROVAL_ROSTER,
    BROKER_ROSTER,
    HealthProbeResult,
    HealthProvider,
)
from tools.test_host_control_approval_dbus_driver import (
    ApprovalDomainFixture,
    approver_entry,
    approver_policy_raw,
    build_approval_domain,
    plan_metadata_fixture,
)
from tools.test_host_control_dbus_driver import (
    DomainFixture,
    MutableClock,
    _SeedExecutor,
    _seed_service,
    build_domain,
)
from tools.persona_installer import InstallOperation, InstallPlan


def _default_probe_state() -> dict:
    return {
        "broker": {
            "journal": {
                "status": "ready",
                "reason": "",
                "revision": "broker-journal-r1",
            },
            "registry": {
                "status": "ready",
                "reason": "",
                "revision": "registry-r1",
            },
            "policy": {
                "status": "ready",
                "reason": "",
                "revision": "policy-r1",
            },
            "approval_reachability": {
                "status": "not_configured",
                "reason": "approval reachability is not configured until T-2917",
                "revision": "T-2917",
            },
            "executor_reachability": {
                "status": "not_configured",
                "reason": "executor reachability is not configured until T-2917",
                "revision": "T-2917",
            },
            # T-2917 5d.4: the closed roster gained operation_driver (HO
            # ruling); a healthy driver by default
            "operation_driver": {
                "status": "ready",
                "reason": "",
                "revision": "T-2917-5d",
            },
        },
        "approval": {
            "journal": {
                "status": "ready",
                "reason": "",
                "revision": "approval-journal-r1",
            },
            "approver_policy": {
                "status": "not_configured",
                "reason": "approver policy is not configured until T-2920",
                "revision": "T-2920",
            },
            "broker_metadata_reachability": {
                "status": "ready",
                "reason": "",
                "revision": "broker-metadata-r1",
            },
        },
        "sentinels": {
            "plan": "PLAN-SECRET-SENTINEL",
            "grant": "GRANT-SECRET-SENTINEL",
            "journal": "JOURNAL-SECRET-SENTINEL",
        },
    }


def ensure_probe_state(path: Path) -> None:
    if not path.exists():
        path.write_text(json.dumps(_default_probe_state()), encoding="utf-8")


class DomainHealthProbe:
    touch_path: Path | None = None
    touch_gate_path: Path | None = None

    def __init__(
        self,
        path: Path,
        service: str,
        component: str,
        positive_control_reader: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._path = path
        self._service = service
        self._component = component
        self._positive_control_reader = positive_control_reader
        self._positive_control: dict[str, str] = {}

    def __call__(self) -> HealthProbeResult:
        if (
            self.touch_path is not None
            and self.touch_gate_path is not None
            and self.touch_gate_path.exists()
        ):
            with self.touch_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{self._service}.{self._component}\n")
        if self._positive_control_reader is not None:
            self._positive_control = self._positive_control_reader()
        data = json.loads(self._path.read_text(encoding="utf-8"))
        raw = data[self._service][self._component]
        if raw.get("raise"):
            raise RuntimeError(str(raw["raise"]))
        return HealthProbeResult(
            status=raw["status"],
            reason=raw["reason"],
            revision=raw["revision"],
        )

    def positive_control(self) -> dict[str, str]:
        return dict(self._positive_control)


def _sqlite_match(db_path: Path, table: str, column: str, needle: str) -> str:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            f"SELECT {column} FROM {table} WHERE {column} LIKE ? LIMIT 1",
            (f"%{needle}%",),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"seeded sentinel absent from {table}.{column}")
    return str(row[0])


def _sqlite_has_match(db_path: Path, table: str, column: str, needle: str) -> bool:
    try:
        _sqlite_match(db_path, table, column, needle)
    except RuntimeError:
        return False
    return True


def _seed_broker_plan(
    *,
    broker_fixture: DomainFixture,
    subject_uid: int,
    idempotency_key: str,
) -> tuple[str, str]:
    subject = broker_fixture.registry.subject_for_uid(subject_uid)
    target = broker_fixture.registry.seat("recovery")
    manifest = broker_fixture.registry.manifest("manifest-r1")
    installer_plan = InstallPlan.create(
        schema="pantheon-persona-install-plan/v1",
        manifest_version=1,
        instance="test-instance",
        role="recovery",
        persona="recovery",
        account="claude-fallback",
        configured_harness="codex",
        selected_harness="codex",
        root="/projection",
        operations=(
            InstallOperation(
                operation_id="framework.source",
                layer=0,
                # admitted kinds: T-2917 5b refuses a non-admitted kind
                # inside a broker plan at prepare as well as at plan time
                kind="source-check",
                target="pantheon-framework",
                precondition={},
                desired={
                    "artifacts": ",".join(
                        (
                            # T-5516: the installer code files (Q3 binding)
                            *FRAMEWORK_CODE_ARTIFACTS,
                            *manifest.template_ids,
                        )
                    ),
                },
                inverse=None,
                privilege="none",
                owner="recovery",
                provenance={"ticket": "T-2914"},
            ),
            InstallOperation(
                operation_id="health-seed-op",
                layer=10,
                kind="mkdir",
                target="/projection/health-seed",
                precondition={"exists": "false"},
                desired={"exists": "true"},
                inverse={"exists": "false"},
                privilege="user",
                owner="recovery",
                provenance={"ticket": "T-2914"},
            ),
        ),
        operator_actions=(),
        provenance={
            "ticket": "T-2914",
            "declared_revision": manifest.framework_revision,
            "source_artifact_sha256": manifest.framework_sha256,
        },
    )
    effect_digest = compute_effect_digest(
        target=target,
        manifest=manifest,
        installer_plan=installer_plan,
    )
    now = broker_fixture.clock()
    plan = PlanEnvelope.create(
        schema="pantheon-host-plan-envelope/v1",
        plan_handle="1" * 64,
        owner=subject,
        target_seat_id="recovery",
        manifest_revision=manifest.revision,
        manifest_sha256=manifest.sha256,
        framework_revision=manifest.framework_revision,
        framework_sha256=manifest.framework_sha256,
        template_ids=manifest.template_ids,
        policy_revision=broker_fixture.policy.revision,
        operation_families=("persona.install",),
        approval_class="human",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        installer_plan=installer_plan,
        effect_digest=effect_digest,
    )
    stored, _ = broker_fixture.journal.put_plan_or_replay(
        subject=subject,
        target_seat_id="recovery",
        manifest_revision="manifest-r1",
        idempotency_key=idempotency_key,
        request_digest=sha256_json(
            {
                "schema": "pantheon-host-plan-request/v1",
                "seat_id": "recovery",
                "manifest_revision": "manifest-r1",
            }
        ),
        plan=plan,
    )
    return stored.plan_handle, stored.digest


def _seed_domain_sentinels(
    *,
    broker_fixture: DomainFixture,
    approval_fixture: ApprovalDomainFixture,
    subject_uid: int,
    sentinels: Mapping[str, str],
) -> None:
    broker_db = broker_fixture.root / "broker.sqlite3"
    approval_db = approval_fixture.root / "approval.sqlite3"
    if (
        _sqlite_has_match(broker_db, "plans", "idempotency_key", sentinels["plan"])
        and _sqlite_has_match(
            broker_db,
            "operation_events",
            "payload_json",
            sentinels["journal"],
        )
        and _sqlite_has_match(
            approval_db,
            "grants",
            "audit_anchor",
            sentinels["grant"],
        )
    ):
        return
    plan_handle, plan_digest = _seed_broker_plan(
        broker_fixture=broker_fixture,
        subject_uid=subject_uid,
        idempotency_key=sentinels["plan"],
    )
    seed_operations = _seed_service(broker_fixture, 1 << 34)
    record, _ = seed_operations.prepare_apply(
        subject_uid,
        plan_handle,
        plan_digest,
        "seed-health-journal-apply",
        "5c" * 32,
    )
    seed_operations.authorize(record.operation_handle)
    seed_operations.dispatch(record.operation_handle, _SeedExecutor())
    seed_operations.append_result(
        record.operation_handle,
        ok=True,
        mutations=(),
        evidence=(),
        detail=sentinels["journal"],
    )

    approval_fixture.metadata.plans[plan_handle] = (
        plan_metadata_fixture(owner="mason", plan_digest=plan_digest)
    )
    opened = approval_fixture.service.review(
        "specialist",
        plan_handle,
        plan_digest,
        None,
    )
    approval_fixture.service.decide(
        "specialist",
        opened.record.review_handle,
        plan_digest,
        "granted",
        "approved",
        sentinels["grant"],
        "seed-health-grant-decision",
    )


def provider_for(
    *,
    path: Path,
    service: str,
    broker_fixture: DomainFixture | None = None,
    approval_fixture: ApprovalDomainFixture | None = None,
    sentinels: Mapping[str, str],
) -> tuple[HealthProvider, dict[str, DomainHealthProbe]]:
    roster = BROKER_ROSTER if service == "broker" else APPROVAL_ROSTER
    readers: dict[str, Callable[[], dict[str, str]]] = {}
    if broker_fixture is not None:
        broker_db = broker_fixture.root / "broker.sqlite3"

        def plan_control() -> dict[str, str]:
            matched = _sqlite_match(
                broker_db,
                "plans",
                "idempotency_key",
                sentinels["plan"],
            )
            return {
                "label": "plan",
                "surface": "broker plans.idempotency_key",
                "content": matched,
            }

        def journal_control() -> dict[str, str]:
            matched = _sqlite_match(
                broker_db,
                "operation_events",
                "payload_json",
                sentinels["journal"],
            )
            return {
                "label": "journal",
                "surface": "broker operation_events.payload_json",
                "content": matched,
            }

        readers["registry"] = plan_control
        readers["journal"] = journal_control
    if approval_fixture is not None:
        approval_db = approval_fixture.root / "approval.sqlite3"

        def grant_control() -> dict[str, str]:
            matched = _sqlite_match(
                approval_db,
                "grants",
                "audit_anchor",
                sentinels["grant"],
            )
            return {
                "label": "grant",
                "surface": "approval grants.audit_anchor",
                "content": matched,
            }

        readers["journal"] = grant_control
    probes = {
        name: DomainHealthProbe(path, service, name, readers.get(name))
        for name in roster
    }
    provider = HealthProvider(
        service=service,
        probes=probes,
    )
    return provider, probes


def _baseline_state(previous: Mapping[str, object]) -> dict[str, object]:
    return {
        "baseline_ready": True,
        "initial_status": {
            service: snapshot.status
            for service, snapshot in previous.items()
        },
        "initial_components": {
            service: {
                component.name: component.status
                for component in snapshot.subcomponents
            }
            for service, snapshot in previous.items()
        },
    }


def _probe_positive_controls(
    probes_by_service: Mapping[str, Mapping[str, DomainHealthProbe]],
) -> dict[str, dict[str, str]]:
    return {
        f"{service}.{component}": probe.positive_control()
        for service, probes in probes_by_service.items()
        for component, probe in probes.items()
        if probe.positive_control()
    }


def serve(config_path: str) -> int:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    import dbus
    import dbus.bus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    from tools.host_control import wire as broker_wire
    from tools.host_control.approval import wire as approval_wire
    from tools.host_control.approval.dbus_service import HostApproval1Object
    from tools.host_control.approval.identity import ApproverResolver
    from tools.host_control.dbus_service import HostBroker1Object
    from tools.host_control.health_dbus import (
        Health1Object,
        approval_health_path,
        broker_health_path,
    )

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    root = Path(config["root"])
    root.mkdir(parents=True, exist_ok=True)
    probe_path = Path(config["probe_path"])
    ensure_probe_state(probe_path)
    if config.get("touch_path"):
        DomainHealthProbe.touch_path = Path(config["touch_path"])
    if config.get("touch_gate_path"):
        DomainHealthProbe.touch_gate_path = Path(config["touch_gate_path"])
    provider_ref: dict[str, HealthProvider] = {}

    broker_root = root / "broker"
    broker_fixture = build_domain(
        broker_root,
        subject_uid=int(config["subject_uid"]),
        register_subject=True,
        clock=MutableClock(),
        health_check=lambda: provider_ref["broker"].capabilities_health(),
    )
    approval_root = root / "approval"
    policy_raw = approver_policy_raw(
        subjects={
            "specialist": int(config["subject_uid"]),
            "mason": int(config["subject_uid"]) + 1,
        },
        approvers={"specialist": approver_entry()},
    )
    approval_fixture = build_approval_domain(
        approval_root,
        policy_raw=policy_raw,
        consumer_uids=frozenset({int(config["broker_uid"])}),
    )
    sentinels = _default_probe_state()["sentinels"]
    _seed_domain_sentinels(
        broker_fixture=broker_fixture,
        approval_fixture=approval_fixture,
        subject_uid=int(config["subject_uid"]),
        sentinels=sentinels,
    )
    broker_provider, broker_probes = provider_for(
        path=probe_path,
        service="broker",
        broker_fixture=broker_fixture,
        sentinels=sentinels,
    )
    approval_provider, approval_probes = provider_for(
        path=probe_path,
        service="approval",
        approval_fixture=approval_fixture,
        sentinels=sentinels,
    )
    provider_ref["broker"] = broker_provider

    connection = dbus.bus.BusConnection(config["bus_address"])
    broker_name = dbus.service.BusName(broker_wire.BUS_NAME, connection)
    approval_name = dbus.service.BusName(approval_wire.BUS_NAME, connection)
    broker_object = HostBroker1Object(
        connection,
        registry=broker_fixture.registry,
        plan_service=broker_fixture.plan_service,
        operation_service=broker_fixture.operation_service,
        approval_source=BrokerApprovalSource(
            journal=broker_fixture.journal,
            clock=broker_fixture.clock,
        ),
        approval_status=UnavailableApprovalStatus(),
        approval_uid=int(config["approval_uid"]),
    )
    approval_object = HostApproval1Object(
        connection,
        service=approval_fixture.service,
        approver_resolver=ApproverResolver(parse_approver_policy(policy_raw)),
        consumer_uid=int(config["broker_uid"]),
        ingress_uid=int(config["ingress_uid"]),
    )
    monitor_uids = frozenset(int(uid) for uid in config["monitor_uids"])
    broker_health = Health1Object(
        connection,
        object_path=broker_health_path(),
        provider=broker_provider,
        monitor_uids=monitor_uids,
    )
    approval_health = Health1Object(
        connection,
        object_path=approval_health_path(),
        provider=approval_provider,
        monitor_uids=monitor_uids,
    )

    broker_base_xml = dbus.service.Object.Introspect(
        broker_health,
        broker_health_path(),
        connection,
    )
    broker_wrapped_xml = broker_health.Introspect(
        broker_health_path(),
        connection,
    )
    emit_path = Path(config["emit_path"])
    previous = {
        "broker": broker_provider.snapshot(),
        "approval": approval_provider.snapshot(),
    }
    state_path = root / "state.json"
    # T-4118: write state.json ATOMICALLY (temp file + rename). A concurrent
    # reader is the test harness's `_spawn_driver` poll loop, which json.loads
    # this file. A plain `write_text` truncates-then-writes, exposing a window
    # where the reader sees an empty/truncated file and JSONDecodeError-crashes
    # -- a window that `pytest -n auto` contention widens enough to false-fail
    # the harness setup. `Path.replace` is an atomic rename on the same
    # filesystem, so the reader only ever sees the complete document.
    state_tmp = state_path.with_suffix(".json.tmp")
    state_tmp.write_text(
        json.dumps(
            {
                "broker_health_path": broker_health_path(),
                "approval_health_path": approval_health_path(),
                "broker_base_introspection": broker_base_xml,
                "broker_wrapped_introspection": broker_wrapped_xml,
                "sentinels": sentinels,
                "baseline": _baseline_state(previous),
                "probe_positive_controls": _probe_positive_controls(
                    {
                        "broker": broker_probes,
                        "approval": approval_probes,
                    }
                ),
            }
        ),
        encoding="utf-8",
    )
    state_tmp.replace(state_path)

    seen_emit = {"value": ""}

    def poll_emit() -> bool:
        if emit_path.exists():
            marker = emit_path.read_text(encoding="utf-8")
            if marker != seen_emit["value"]:
                seen_emit["value"] = marker
                service = (marker.strip() or "broker").split(":", 1)[0]
                if service in ("broker", "both"):
                    current = broker_provider.snapshot()
                    broker_health.emit_properties_changed(
                        current,
                        previous["broker"],
                    )
                    previous["broker"] = current
                if service in ("approval", "both"):
                    current = approval_provider.snapshot()
                    approval_health.emit_properties_changed(
                        current,
                        previous["approval"],
                    )
                    previous["approval"] = current
        return True

    GLib.timeout_add(100, poll_emit)
    _keepalive = (
        broker_name,
        approval_name,
        broker_object,
        approval_object,
        broker_health,
        approval_health,
    )
    GLib.MainLoop().run()
    del _keepalive
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_host_control_health_dbus_driver <config.json>")
        return 2
    return serve(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
