"""Shared test fixtures for the T-2917 5b per-seat executor tests.

Not a test module (leading underscore, the ``_host_control_testbus.py``
precedent): builds a disposable seat root, a root-owned-style framework
copy, a registry whose target seat is the CURRENT uid (the executor runs
as the test uid on a private bus; real cross-uid is T-2924), fake
user-manager and account-lookup seams, and canonical execution specs.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from tools.host_control.contracts import canonical_json, sha256_json
from tools.host_control.executor.engine import (
    EXECUTION_SPEC_SCHEMA,
    ExecutorEngine,
    ParsedSpec,
    parse_spec,
)
from tools.host_control.executor.identity import ExecutorIdentity, resolve_executor_identity
from tools.host_control.executor.user_manager import ManagerState, UserManagerUnavailable
from tools.user_systemd import (
    ReadPurpose,
    UnitFileEnablement,
    UnitSnapshot,
    UserManagerCallOutcome,
    unit_file_enablement_from_state,
    unit_snapshot_from_native,
)
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer import InstallOperation

ACCOUNT = "seatacct"
SEAT_ID = "seat-a"
OTHER_UID_OFFSET = 7
MAILBOX_GROUP = "pantheon-mailbox"
OPERATION_HANDLE = "ab" * 32
FRAMEWORK_ARTIFACTS = ("tools/persona_installer.py", "tools/systemd/unit.service")
POLICY_BUNDLE = ("rulebook/", "sop/")


class FakeUserManager:
    """The 5b read fake, grown (T-2917 5c.2) to the full typed port.

    ``units`` maps a unit name to its NATIVE property values (as a bus
    layer would deliver them); ``read_unit`` returns the purpose's subset
    through ``unit_snapshot_from_native``. ``on_call`` (optional) is called
    as ``on_call(self, name, args)`` BEFORE each port call so a test can
    script state transitions (e.g. a start that fires the unit). Every port
    call is appended to ``calls`` as ``"name:arg,..."``; the 5b methods keep
    their exact 5b behaviour."""

    transport = "fake"
    address: str | None = None
    address_source = "test-override"

    def __init__(self, reachable: bool = True, system_state: str = "running") -> None:
        self.reachable = reachable
        self.system_state = system_state
        self.calls: list[str] = []
        self.units: dict[str, dict[str, object]] = {}
        self.enablement: dict[str, str] = {}
        self.environment_files: dict[str, bool] = {}
        self.environment_entries = 1
        self.refuse: dict[str, str] = {}
        self.on_call = None
        # T-2917 5d.1 (T-2917:D11): the timeout every timed port call
        # received, as (name, timeout), and every deadline the engine bound;
        # neither is recorded in ``calls`` (call-sequence pins stay exact)
        self.timeouts: list[tuple[str, float | None]] = []
        self.bound_deadlines: list[float | None] = []

    def bind_deadline(self, deadline: float | None) -> None:
        self.bound_deadlines.append(deadline)

    def manager_state(self) -> ManagerState:
        self.calls.append("manager_state")
        if not self.reachable:
            raise UserManagerUnavailable("fake manager unreachable")
        return ManagerState(True, self.system_state)

    def unit_properties(self, unit: str, names: tuple[str, ...]) -> Mapping[str, str]:
        raise UserManagerUnavailable("not used in 5b")

    def unit_file_state(self, unit: str) -> str:
        raise UserManagerUnavailable("not used in 5b")

    # --- the 5c port (T-2917 5c.2) ---------------------------------------

    def _enter(self, name: str, *args: object) -> None:
        self.calls.append(f"{name}:" + ",".join(str(item) for item in args))
        if self.on_call is not None:
            self.on_call(self, name, args)
        if not self.reachable:
            raise UserManagerUnavailable("fake manager unreachable")

    def _outcome(self, name: str) -> UserManagerCallOutcome:
        error = self.refuse.get(name)
        if error:
            return UserManagerCallOutcome(ok=False, error_name=error, detail=f"fake refused {name}")
        return UserManagerCallOutcome(ok=True, transport_evidence={"call": name})

    def manager_environment(self) -> int:
        self._enter("manager_environment")
        return self.environment_entries

    def read_unit(self, unit: str, purpose: ReadPurpose, *, timeout: float | None = None) -> UnitSnapshot:
        self.timeouts.append(("read_unit", timeout))
        self._enter("read_unit", unit, purpose.name)
        values = self.units.get(unit, {})
        return unit_snapshot_from_native(
            unit,
            purpose,
            {name: values[name] for name in purpose.properties if name in values},
            transport=self.transport,
        )

    def unit_file_enablement(self, unit: str, *, timeout: float | None = None) -> UnitFileEnablement:
        self.timeouts.append(("unit_file_enablement", timeout))
        self._enter("unit_file_enablement", unit)
        return unit_file_enablement_from_state(self.enablement.get(unit, "disabled"))

    def environment_file_declared(self, unit: str) -> bool:
        self._enter("environment_file_declared", unit)
        return self.environment_files.get(unit, False)

    def reload(self) -> UserManagerCallOutcome:
        self._enter("reload")
        return self._outcome("reload")

    def reset_failed_unit(self, unit: str) -> UserManagerCallOutcome:
        self._enter("reset_failed_unit", unit)
        return self._outcome("reset_failed_unit")

    def start_unit(self, unit: str, *, wait: bool, timeout: float | None = None) -> UserManagerCallOutcome:
        self.timeouts.append(("start_unit", timeout))
        self._enter("start_unit", unit, f"wait={wait}")
        return self._outcome("start_unit")

    def stop_unit(self, unit: str, *, timeout: float | None = None) -> UserManagerCallOutcome:
        self.timeouts.append(("stop_unit", timeout))
        self._enter("stop_unit", unit)
        return self._outcome("stop_unit")

    def enable_now(self, unit: str) -> UserManagerCallOutcome:
        self._enter("enable_now", unit)
        return self._outcome("enable_now")

    def disable_now(self, unit: str) -> UserManagerCallOutcome:
        self._enter("disable_now", unit)
        return self._outcome("disable_now")


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _tighten(root: Path) -> None:
    """Clear group/other write everywhere (RootOwnedFileLoader refuses it)."""

    for directory, dirnames, filenames in os.walk(root):
        Path(directory).chmod(0o755)
        for name in filenames:
            (Path(directory) / name).chmod(0o644)


@dataclass
class ExecutorFixture:
    root: Path
    seat_root: Path
    framework_root: Path
    registry: SeatRegistry
    identity: ExecutorIdentity
    framework_files: RootOwnedFileLoader
    registry_files: RootOwnedFileLoader
    user_manager: FakeUserManager
    linger_dir: Path
    in_group: bool = True

    def close(self) -> None:
        self.framework_files.close()
        self.registry_files.close()

    def engine(self, **overrides: object) -> ExecutorEngine:
        uid = self.identity.uid
        values: dict[str, object] = dict(
            identity=self.identity,
            framework_files=self.framework_files,
            framework_root=self.framework_root,
            user_manager=self.user_manager,
            which=lambda name: f"/usr/bin/{name}" if name == "codex" else None,
            pwd_lookup=lambda value: SimpleNamespace(pw_name=ACCOUNT, pw_gid=4242)
            if value == uid
            else (_ for _ in ()).throw(KeyError(value)),
            group_lookup=lambda name: SimpleNamespace(gr_gid=5151),
            grouplist=lambda account, gid: [gid, 5151] if self.in_group else [gid],
            linger_dir=self.linger_dir,
        )
        values.update(overrides)
        return ExecutorEngine(**values)  # type: ignore[arg-type]

    def artifact_digest(self) -> str:
        digest = hashlib.sha256()
        for relative in sorted(FRAMEWORK_ARTIFACTS):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update((self.framework_root / relative).read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()


def build_executor_fixture(base: Path, *, register_uid: bool = True) -> ExecutorFixture:
    root = base.resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    install_root = root / "inst"
    seat_root = install_root / "home" / ACCOUNT
    seat_root.mkdir(parents=True)
    for path in (install_root, install_root / "home", seat_root):
        path.chmod(0o755)
    framework = root / "framework"
    _write(framework / "tools/persona_installer.py", b"# framework copy\n")
    _write(framework / "tools/systemd/unit.service", b"[Unit]\n")
    _write(framework / "rulebook/principles.md", b"p\n")
    _write(framework / "sop/README.txt", b"s\n")
    _write(framework / "pinned-rules/seat.md", b"brief\n")
    _write(framework / "codex/standing.md", b"standing instructions\n")
    _tighten(framework)
    trusted = root / "trusted"
    trusted.mkdir()
    trusted.chmod(0o755)
    manifest = trusted / "manifest.yaml"
    _write(manifest, b"manifest: fixture\n", 0o644)
    uid = os.getuid()
    seats = [
        {
            "seat_id": "other",
            "uid": uid + OTHER_UID_OFFSET,
            "persona": "other",
            "role": "worker",
            "instance": "test-instance",
            "account": "other",
            "harness": "codex",
            "install_root": str(install_root),
        }
    ]
    if register_uid:
        seats.insert(
            0,
            {
                "seat_id": SEAT_ID,
                "uid": uid,
                "persona": "seat",
                "role": "worker",
                "instance": "test-instance",
                "account": ACCOUNT,
                "harness": "codex",
                "install_root": str(install_root),
            },
        )
    registry_path = trusted / "registry.json"
    _write(
        registry_path,
        json.dumps(
            {
                "schema": "pantheon-host-seat-registry/v1",
                "seats": seats,
                "manifests": [
                    {
                        "revision": "manifest-r1",
                        "path": str(manifest),
                        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                        "framework_revision": "test-rev",
                        "framework_sha256": "0" * 64,
                        "template_ids": ["tools/systemd/unit.service"],
                    }
                ],
            }
        ).encode("utf-8"),
        0o644,
    )
    registry_files = RootOwnedFileLoader._open_for_test((trusted,), expected_owner_uid=uid)
    registry = SeatRegistry.load(registry_path, files=registry_files)
    framework_files = RootOwnedFileLoader._open_for_test((framework,), expected_owner_uid=uid)
    linger = root / "linger"
    linger.mkdir()
    (linger / ACCOUNT).write_text("", encoding="utf-8")
    identity = (
        resolve_executor_identity(getuid=os.getuid, registry=registry)
        if register_uid
        else None
    )
    return ExecutorFixture(
        root=root,
        seat_root=seat_root,
        framework_root=framework,
        registry=registry,
        identity=identity,  # type: ignore[arg-type]
        framework_files=framework_files,
        registry_files=registry_files,
        user_manager=FakeUserManager(),
        linger_dir=linger,
    )


def op(
    operation_id: str,
    kind: str,
    target: str,
    desired: Mapping[str, str] | None = None,
    *,
    owner: str = ACCOUNT,
    scope: str = "all",
    precondition: Mapping[str, str] | None = None,
) -> InstallOperation:
    return InstallOperation(
        operation_id=operation_id,
        layer=1,
        kind=kind,  # type: ignore[arg-type]
        target=target,
        precondition=dict(precondition or {}),
        desired=dict(desired or {}),
        inverse={"state": "receipt-snapshot"} if kind in ("mkdir", "atomic-write", "user-unit-install") else None,
        privilege="unprivileged",
        owner=owner,
        provenance={},
        execution_scope=scope,  # type: ignore[arg-type]
    )


def content_op(operation_id: str, kind: str, target: Path, content: str, mode: str = "0644") -> InstallOperation:
    return op(
        operation_id,
        kind,
        str(target),
        {
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "mode": mode,
        },
    )


def spec_payload(
    fixture: ExecutorFixture,
    operations: tuple[InstallOperation, ...],
    *,
    execution_kind: str = "apply",
    target_seat_id: str = SEAT_ID,
    operation_handle: str = OPERATION_HANDLE,
    allowed_roots: list[str] | None = None,
) -> dict:
    return {
        "schema": EXECUTION_SPEC_SCHEMA,
        "operation_handle": operation_handle,
        "execution_kind": execution_kind,
        "target_seat_id": target_seat_id,
        "plan_handle": "cd" * 32,
        "plan_digest": "ef" * 32,
        "effect_digest": "12" * 32,
        "manifest_revision": "manifest-r1",
        "framework_revision": "test-rev",
        "receipt_handle": None,
        "receipt_digest": None,
        "allowed_roots": allowed_roots if allowed_roots is not None else [str(fixture.seat_root)],
        "operations": [
            {
                "operation_id": item.operation_id,
                "layer": item.layer,
                "kind": item.kind,
                "target": item.target,
                "precondition": dict(item.precondition),
                "desired": dict(item.desired),
                "inverse": None if item.inverse is None else dict(item.inverse),
                "privilege": item.privilege,
                "owner": item.owner,
                "provenance": dict(item.provenance),
                "execution_scope": item.execution_scope,
            }
            for item in operations
        ],
        "rollback_mutations": [],
        # T-2917:D28: a reconcile spec carries its journal-derived probe nonce
        **({"probe_nonce": 0} if execution_kind == "reconcile" else {}),
    }


def parsed(
    fixture: ExecutorFixture,
    operations: tuple[InstallOperation, ...],
    **kwargs: object,
) -> ParsedSpec:
    payload = spec_payload(fixture, operations, **kwargs)  # type: ignore[arg-type]
    digest = sha256_json(payload)
    return parse_spec(
        schema_version=EXECUTION_SPEC_SCHEMA,
        execution_kind=payload["execution_kind"],
        returned_digest=digest,
        spec_json=canonical_json(payload),
        expected_digest=digest,
        operation_handle=payload["operation_handle"],
        identity=fixture.identity,
        registry=fixture.registry,
    )
