"""Unit-1 tests for the ORG-052 server-owned host planning core."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from tools.host_control.contracts import (
    HostControlError,
    IdempotencyConflict,
    PlanEnvelope,
    PolicyDenied,
    RegistryError,
    SeatRecord,
    SubjectContext,
    SubjectMismatch,
    compute_effect_digest,
)
from tools.host_control.contracts import MigrationRequired
from tools.host_control.journal import (
    APPLICATION_ID,
    DDL_V1,
    SCHEMA_DIGEST_V1,
    BrokerJournal,
)
from tools.host_control.planner import HostPlanService
from tools.host_control.policy import AuthorizationDecision, HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer import InstallPlan, Manifest, PersonaInstaller


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)


def _manifest_dict() -> dict:
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
            "policy": {"bundle": ["rulebook/", "sop/", "architecture/adr/"]},
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


def _plan_replace(plan: InstallPlan, **changes: object) -> InstallPlan:
    values = {
        field.name: getattr(plan, field.name)
        for field in dataclasses.fields(plan)
        if field.name != "digest"
    }
    values.update(changes)
    return InstallPlan.create(**values)


def _envelope_replace(
    plan: PlanEnvelope,
    **changes: object,
) -> PlanEnvelope:
    values = {
        field.name: getattr(plan, field.name)
        for field in dataclasses.fields(plan)
        if field.name != "digest"
    }
    values.update(changes)
    return PlanEnvelope.create(**values)


class HostControlCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.manifest_path = self.root / "instance.yaml"
        self.manifest_path.write_text(
            yaml.safe_dump(_manifest_dict(), sort_keys=False),
            encoding="utf-8",
        )
        self.manifest_path.chmod(0o600)
        self.installer = PersonaInstaller(REPO_ROOT)
        manifest = Manifest.load(self.manifest_path)
        prototype = self.installer.plan(
            manifest,
            "recovery",
            harness_override="codex",
            root=Path("/projection"),
        )
        self.framework_sha256 = prototype.provenance[
            "source_artifact_sha256"
        ]
        self.template_ids = (
            "tools/systemd/pantheon-persona-consume@.service",
            "tools/systemd/pantheon-persona-consume@.timer",
            "tools/systemd/pantheon-persona-quota@.service",
            "tools/systemd/pantheon-persona-quota@.timer",
        )
        self.registry_path = self.root / "registry.json"
        self.policy_path = self.root / "policy.json"
        self.journal_path = self.root / "broker.sqlite3"
        self.write_registry()
        self.write_policy()
        self.files = RootOwnedFileLoader._open_for_test(
            (self.root,),
            expected_owner_uid=os.getuid(),
        )
        self.registry = SeatRegistry.load(self.registry_path, files=self.files)
        self.policy = HostPolicy.load(self.policy_path, files=self.files)
        self.journal = BrokerJournal.open(self.journal_path)
        self.handles = iter(character * 64 for character in "123456789abcdef0")
        self.service = self.build_service()

    def build_service(
        self,
        *,
        registry: SeatRegistry | None = None,
        policy: HostPolicy | None = None,
        host_operation_families: frozenset[str] = frozenset(
            ("persona.install", "persona.verify")
        ),
    ) -> HostPlanService:
        return HostPlanService(
            registry=registry or self.registry,
            policy=policy or self.policy,
            journal=self.journal,
            installer=self.installer,
            clock=lambda: FIXED_NOW,
            handle_factory=lambda: next(self.handles),
            plan_ttl=timedelta(hours=1),
            contract_version="org.pantheon.HostBroker1/v1",
            broker_revision="unit-test",
            host_operation_families=host_operation_families,
            health_check=lambda: "ready",
        )

    def tearDown(self) -> None:
        self.journal.close()
        self.files.close()
        self.temp.cleanup()

    def write_json(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def registry_dict(self) -> dict:
        return {
            "schema": "pantheon-host-seat-registry/v1",
            "seats": [
                {
                    "seat_id": "cairn",
                    "uid": 1003,
                    "persona": "cairn",
                    "role": "worker",
                    "instance": "test-instance",
                    "account": "codex",
                    "harness": "codex",
                    "install_root": "/home/codex",
                },
                {
                    "seat_id": "peer",
                    "uid": 1004,
                    "persona": "peer",
                    "role": "worker",
                    "instance": "test-instance",
                    "account": "peer",
                    "harness": "codex",
                    "install_root": "/home/peer",
                },
                {
                    "seat_id": "recovery",
                    "uid": 1002,
                    "persona": "recovery",
                    "role": "recovery",
                    "instance": "test-instance",
                    "account": "claude-fallback",
                    "harness": "codex",
                    "install_root": "/projection",
                },
            ],
            "manifests": [
                {
                    "revision": "manifest-r1",
                    "path": str(self.manifest_path),
                    "sha256": hashlib.sha256(
                        self.manifest_path.read_bytes()
                    ).hexdigest(),
                    "framework_revision": "test-rev",
                    "framework_sha256": self.framework_sha256,
                    "template_ids": list(self.template_ids),
                }
            ],
        }

    def write_registry(self, value: dict | None = None) -> None:
        self.write_json(self.registry_path, value or self.registry_dict())

    def policy_dict(self) -> dict:
        grants = []
        for subject in ("cairn", "peer"):
            grants.append(
                {
                    "subject_seat_id": subject,
                    "action": "plan",
                    "target_seat_id": "recovery",
                    "manifest_revisions": ["manifest-r1"],
                    "operation_families": ["persona.install"],
                    "approval_class": "human",
                }
            )
        return {
            "schema": "pantheon-host-policy/v1",
            "revision": "policy-r1",
            "grants": grants,
        }

    def write_policy(self, value: dict | None = None) -> None:
        self.write_json(self.policy_path, value or self.policy_dict())

    def row_count(self) -> int:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute("SELECT count(*) FROM plans").fetchone()[0]

    def plan(self, uid: int = 1003, key: str = "request-1"):
        return self.service.plan(uid, "recovery", "manifest-r1", key)

    def stored_plan(self, result, uid: int = 1003):
        subject = self.registry.subject_for_uid(uid)
        return self.journal.get_plan(
            subject=subject,
            plan_handle=result.plan_handle,
            expected_digest=result.plan_digest,
        )

    def test_registry_resolves_subject_only_from_authenticated_uid(self) -> None:
        subject = self.registry.subject_for_uid(1003)
        self.assertEqual(
            subject,
            SubjectContext(1003, "cairn", "cairn", "worker", "test-instance"),
        )
        claimed_environment = {
            "PANTHEON_PERSONA": "recovery",
            "PANTHEON_ROLE": "recovery",
            "PANTHEON_INSTANCE": "forged",
            "PANTHEON_CAPABILITIES": "host.*",
            "PANTHEON_UID": "1002",
        }
        with mock.patch.dict(os.environ, claimed_environment):
            self.assertEqual(self.registry.subject_for_uid(1003), subject)
        with self.assertRaises(RegistryError):
            self.registry.subject_for_uid(9999)

    def test_registry_rejects_unknown_keys_unsafe_paths_and_hash_drift(self) -> None:
        cases: list[dict] = []
        extra = self.registry_dict()
        extra["unexpected"] = True
        cases.append(extra)
        seat_extra = self.registry_dict()
        seat_extra["seats"][0]["unexpected"] = True
        cases.append(seat_extra)
        manifest_extra = self.registry_dict()
        manifest_extra["manifests"][0]["unexpected"] = True
        cases.append(manifest_extra)
        relative = self.registry_dict()
        relative["manifests"][0]["path"] = "instance.yaml"
        cases.append(relative)
        escaped = self.registry_dict()
        escaped["manifests"][0]["path"] = str(
            self.root / "child" / ".." / "instance.yaml"
        )
        cases.append(escaped)
        outside = self.registry_dict()
        outside["manifests"][0]["path"] = "/etc/passwd"
        cases.append(outside)
        duplicate_seat = self.registry_dict()
        duplicate_seat_record = dict(duplicate_seat["seats"][0])
        duplicate_seat_record["uid"] = 2003
        duplicate_seat["seats"].append(duplicate_seat_record)
        cases.append(duplicate_seat)
        duplicate_uid = self.registry_dict()
        duplicate_uid_record = dict(duplicate_uid["seats"][0])
        duplicate_uid_record["seat_id"] = "duplicate-uid"
        duplicate_uid["seats"].append(duplicate_uid_record)
        cases.append(duplicate_uid)
        duplicate_manifest = self.registry_dict()
        duplicate_manifest["manifests"].append(
            dict(duplicate_manifest["manifests"][0])
        )
        cases.append(duplicate_manifest)
        for value in cases:
            self.write_registry(value)
            with self.subTest(case=cases.index(value)):
                with self.assertRaises(RegistryError):
                    SeatRegistry.load(self.registry_path, files=self.files)

        self.write_registry()
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b"\n")
        with self.assertRaises(RegistryError):
            SeatRegistry.load(self.registry_path, files=self.files)

    def test_capabilities_exposes_only_current_subject_intersection(self) -> None:
        result = self.service.capabilities(1003)
        self.assertEqual(result.contract_version, "org.pantheon.HostBroker1/v1")
        self.assertEqual(result.broker_revision, "unit-test")
        self.assertEqual(result.policy_revision, "policy-r1")
        self.assertEqual(result.health, "ready")
        self.assertEqual(result.host_operation_families, ("persona.install", "persona.verify"))
        self.assertEqual(len(result.grants), 1)
        encoded = json.dumps(dataclasses.asdict(result), sort_keys=True)
        for forbidden in ("peer", "1004", "approver", "credential", "executor"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(self.plan().plan_handle, "1" * 64)

    def test_plan_resolves_manifest_harness_root_and_target_server_side(self) -> None:
        result = self.plan()
        plan = self.stored_plan(result)
        self.assertEqual(plan.installer_plan.persona, "recovery")
        self.assertEqual(plan.installer_plan.account, "claude-fallback")
        self.assertEqual(plan.installer_plan.selected_harness, "codex")
        self.assertEqual(plan.installer_plan.root, "/projection")
        with self.assertRaises(TypeError):
            self.service.plan(
                1003,
                "recovery",
                "manifest-r1",
                "other",
                harness="claude",
            )

    def test_plan_full_digest_is_actor_bound_and_effect_digest_is_actor_neutral(self) -> None:
        first = self.plan(1003, "first")
        second = self.plan(1004, "second")
        self.assertNotEqual(first.plan_digest, second.plan_digest)
        self.assertEqual(first.effect_digest, second.effect_digest)

    def test_effect_digest_changes_for_every_execution_affecting_field(self) -> None:
        result = self.plan()
        stored = self.stored_plan(result)
        target = self.registry.seat("recovery")
        manifest = self.registry.manifest("manifest-r1")
        plan = stored.installer_plan
        baseline = stored.effect_digest

        target_cases = {
            "seat_id": (
                dataclasses.replace(target, seat_id="recovery-replacement"),
                plan,
            ),
            "instance": (
                dataclasses.replace(target, instance="changed-instance"),
                _plan_replace(plan, instance="changed-instance"),
            ),
            "role": (
                dataclasses.replace(target, role="changed-role"),
                _plan_replace(plan, role="changed-role"),
            ),
            "persona": (
                dataclasses.replace(target, persona="changed-persona"),
                _plan_replace(plan, persona="changed-persona"),
            ),
            "account": (
                dataclasses.replace(target, account="changed-account"),
                _plan_replace(plan, account="changed-account"),
            ),
            "selected_harness": (
                dataclasses.replace(target, harness="claude"),
                _plan_replace(plan, selected_harness="claude"),
            ),
            "install_root": (
                dataclasses.replace(target, install_root=Path("/changed-root")),
                _plan_replace(plan, root="/changed-root"),
            ),
        }
        for label, (changed_target, changed_plan) in target_cases.items():
            with self.subTest(target=label):
                self.assertNotEqual(
                    compute_effect_digest(
                        target=changed_target,
                        manifest=manifest,
                        installer_plan=changed_plan,
                    ),
                    baseline,
                )
        with self.assertRaises(HostControlError):
            compute_effect_digest(
                target=dataclasses.replace(target, role="mismatch"),
                manifest=manifest,
                installer_plan=plan,
            )

        for label, changed_plan in (
            (
                "configured_harness",
                _plan_replace(plan, configured_harness="codex"),
            ),
            (
                "manifest_schema_version",
                _plan_replace(plan, manifest_version=3),
            ),
        ):
            with self.subTest(plan_field=label):
                self.assertNotEqual(
                    compute_effect_digest(
                        target=target,
                        manifest=manifest,
                        installer_plan=changed_plan,
                    ),
                    baseline,
                )

        operation = plan.operations[0]
        operation_cases = {
            "order": tuple(reversed(plan.operations)),
            "kind": (
                dataclasses.replace(operation, kind="manager-check"),
                *plan.operations[1:],
            ),
            "target": (
                dataclasses.replace(operation, target="changed-target"),
                *plan.operations[1:],
            ),
            "precondition": (
                dataclasses.replace(operation, precondition={"state": "changed"}),
                *plan.operations[1:],
            ),
            "desired": (
                dataclasses.replace(operation, desired={"state": "changed"}),
                *plan.operations[1:],
            ),
            "inverse": (
                dataclasses.replace(operation, inverse={"state": "changed"}),
                *plan.operations[1:],
            ),
            "privilege": (
                dataclasses.replace(operation, privilege="unprivileged"),
                *plan.operations[1:],
            ),
            "owner": (
                dataclasses.replace(operation, owner="changed-owner"),
                *plan.operations[1:],
            ),
        }
        for label, operations in operation_cases.items():
            candidate = _plan_replace(plan, operations=operations)
            with self.subTest(label=label):
                self.assertNotEqual(
                    compute_effect_digest(
                        target=target,
                        manifest=manifest,
                        installer_plan=candidate,
                    ),
                    baseline,
                )

        digest_manifest_cases = (
            dataclasses.replace(manifest, revision="manifest-r2"),
            dataclasses.replace(manifest, sha256="a" * 64),
        )
        for candidate in digest_manifest_cases:
            with self.subTest(manifest=candidate):
                self.assertNotEqual(
                    compute_effect_digest(
                        target=target,
                        manifest=candidate,
                        installer_plan=plan,
                    ),
                    baseline,
                )

        rejected_manifest_cases = (
            dataclasses.replace(manifest, framework_revision="test-rev-2"),
            dataclasses.replace(manifest, framework_sha256="b" * 64),
            dataclasses.replace(manifest, template_ids=("changed",)),
        )
        for candidate in rejected_manifest_cases:
            with self.subTest(manifest=candidate):
                with self.assertRaises(HostControlError):
                    compute_effect_digest(
                        target=target,
                        manifest=candidate,
                        installer_plan=plan,
                    )

        action = plan.operator_actions[0]
        action_cases = {
            "order": tuple(reversed(plan.operator_actions)),
            "action_id": (
                dataclasses.replace(action, action_id="changed"),
                *plan.operator_actions[1:],
            ),
            "layer": (
                dataclasses.replace(action, layer=4),
                *plan.operator_actions[1:],
            ),
            "target": (
                dataclasses.replace(action, target="changed"),
                *plan.operator_actions[1:],
            ),
            "reason": (
                dataclasses.replace(action, reason="changed"),
                *plan.operator_actions[1:],
            ),
            "required_capability": (
                dataclasses.replace(action, required_capability="changed"),
                *plan.operator_actions[1:],
            ),
        }
        for label, changed_actions in action_cases.items():
            with self.subTest(operator_action=label):
                self.assertNotEqual(
                    compute_effect_digest(
                        target=target,
                        manifest=manifest,
                        installer_plan=_plan_replace(
                            plan,
                            operator_actions=changed_actions,
                        ),
                    ),
                    baseline,
                )

    def test_effect_digest_ignores_only_request_and_correlation_metadata(self) -> None:
        first = self.plan(1003, "first")
        second = self.plan(1004, "second")
        same_actor_new_request = self.plan(1003, "different-idempotency-key")
        self.assertEqual(first.effect_digest, second.effect_digest)
        self.assertEqual(first.effect_digest, same_actor_new_request.effect_digest)
        self.assertNotEqual(first.plan_digest, same_actor_new_request.plan_digest)
        one = self.stored_plan(first, 1003)
        two = self.stored_plan(second, 1004)
        self.assertNotEqual(one.owner, two.owner)
        self.assertNotEqual(one.plan_handle, two.plan_handle)
        for label, changed_envelope in (
            (
                "owner",
                _envelope_replace(
                    one,
                    owner=dataclasses.replace(
                        one.owner,
                        uid=2003,
                        seat_id="other-owner",
                    ),
                ),
            ),
            (
                "handle",
                _envelope_replace(one, plan_handle="f" * 64),
            ),
            (
                "timestamps",
                _envelope_replace(
                    one,
                    created_at="2026-08-01T00:00:00+00:00",
                    expires_at="2026-08-01T01:00:00+00:00",
                ),
            ),
        ):
            with self.subTest(correlation=label):
                self.assertEqual(changed_envelope.effect_digest, one.effect_digest)
                self.assertNotEqual(changed_envelope.digest, one.digest)

        target = self.registry.seat("recovery")
        manifest = self.registry.manifest("manifest-r1")
        changed_manifest = dataclasses.replace(
            manifest,
            path=self.root / "another-storage-location.yaml",
        )
        changed_inner = _plan_replace(
            one.installer_plan,
            provenance=dict(one.installer_plan.provenance)
            | {
                "manifest": "/different/local/path",
                "installer": "/different/installer/path",
                "receipt_dir": "/different/receipt/path",
                "arbitrary": "ignored",
            },
        )
        self.assertEqual(
            compute_effect_digest(
                target=target,
                manifest=changed_manifest,
                installer_plan=changed_inner,
            ),
            one.effect_digest,
        )
        for field_name, changed_value in (
            ("role", "changed-role"),
            ("persona", "changed-persona"),
            ("account", "changed-account"),
        ):
            changed_target = dataclasses.replace(
                target,
                **{field_name: changed_value},
            )
            changed_plan = _plan_replace(
                one.installer_plan,
                **{field_name: changed_value},
            )
            with self.subTest(execution_identity=field_name):
                self.assertNotEqual(
                    compute_effect_digest(
                        target=changed_target,
                        manifest=manifest,
                        installer_plan=changed_plan,
                    ),
                    one.effect_digest,
                )
        first_operation = one.installer_plan.operations[0]
        owner_changed = _plan_replace(
            one.installer_plan,
            operations=(
                dataclasses.replace(first_operation, owner="changed-owner"),
                *one.installer_plan.operations[1:],
            ),
        )
        self.assertNotEqual(
            compute_effect_digest(
                target=target,
                manifest=manifest,
                installer_plan=owner_changed,
            ),
            one.effect_digest,
        )

    def test_plan_idempotency_replays_identical_request_and_refuses_conflict(self) -> None:
        first = self.plan(key="same")
        replay = self.plan(key="same")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.plan_handle, replay.plan_handle)
        self.assertEqual(self.row_count(), 1)

        registry_value = self.registry_dict()
        second_manifest = dict(registry_value["manifests"][0])
        second_manifest["revision"] = "manifest-r2"
        registry_value["manifests"].append(second_manifest)
        self.write_registry(registry_value)
        policy_value = self.policy_dict()
        for grant in policy_value["grants"]:
            grant["manifest_revisions"].append("manifest-r2")
        self.write_policy(policy_value)
        changed_request_service = self.build_service(
            registry=SeatRegistry.load(self.registry_path, files=self.files),
            policy=HostPolicy.load(self.policy_path, files=self.files),
        )
        with self.assertRaises(IdempotencyConflict):
            changed_request_service.plan(
                1003,
                "recovery",
                "manifest-r2",
                "same",
            )
        with self.assertRaises(PolicyDenied):
            changed_request_service.plan(1003, "peer", "manifest-r1", "same")
        self.assertEqual(self.row_count(), 1)

    def test_plan_handle_and_digest_are_subject_bound(self) -> None:
        result = self.plan()
        with self.assertRaises(SubjectMismatch):
            self.journal.get_plan(
                subject=self.registry.subject_for_uid(1004),
                plan_handle=result.plan_handle,
            )
        with self.assertRaises(SubjectMismatch):
            self.journal.get_plan(
                subject=self.registry.subject_for_uid(1004),
                plan_handle=result.plan_handle,
                expected_digest=result.plan_digest,
            )

    def test_plan_summary_is_bounded_redacted_and_canonical(self) -> None:
        first = self.plan()
        replay = self.plan()
        self.assertLessEqual(len(first.summary_json.encode("utf-8")), 65536)
        self.assertEqual(first.summary_json, replay.summary_json)
        summary = json.loads(first.summary_json)
        self.assertEqual(summary["schema"], "pantheon-host-plan-summary/v1")
        self.assertEqual(summary["approval_class"], "human")
        self.assertEqual(summary["operation_families"], ["persona.install"])
        for forbidden in (
            "content",
            "credential",
            "endpoint",
            "environment",
            "executor",
            "helper_endpoint",
            "location",
            "receipt_dir",
            "secret",
        ):
            self.assertNotIn(forbidden, first.summary_json.lower())

    def test_plan_commit_survives_cross_process_restart(self) -> None:
        result = self.plan()
        self.journal.close()
        script = """
from pathlib import Path
from tools.host_control.contracts import SubjectContext
from tools.host_control.journal import BrokerJournal
j = BrokerJournal.open(Path(__import__('sys').argv[1]))
p = j.get_plan(
    subject=SubjectContext(1003, 'cairn', 'cairn', 'worker', 'test-instance'),
    plan_handle=__import__('sys').argv[2],
    expected_digest=__import__('sys').argv[3],
)
print(p.digest)
j.close()
"""
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                script,
                str(self.journal_path),
                result.plan_handle,
                result.plan_digest,
            ),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), result.plan_digest)
        self.journal = BrokerJournal.open(self.journal_path)

    def test_uncommitted_or_corrupt_plan_never_becomes_authority(self) -> None:
        result = self.plan()
        script = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('BEGIN IMMEDIATE')
c.execute(
  \"INSERT INTO plans SELECT ?, ?, effect_digest, subject_uid, \"
  \"subject_seat_id, action, target_seat_id, manifest_revision, ?, \"
  \"request_digest, plan_json, created_at, expires_at FROM plans LIMIT 1\",
  ('f' * 64, 'e' * 64, 'uncommitted'),
)
os._exit(17)
"""
        completed = subprocess.run(
            (sys.executable, "-c", script, str(self.journal_path)),
            cwd=REPO_ROOT,
            check=False,
        )
        self.assertEqual(completed.returncode, 17)
        with sqlite3.connect(self.journal_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM plans WHERE plan_handle=?",
                    ("f" * 64,),
                ).fetchone()[0],
                0,
            )
            original_plan_json = connection.execute(
                "SELECT plan_json FROM plans WHERE plan_handle=?",
                (result.plan_handle,),
            ).fetchone()[0]
            noncanonical_plan_json = json.dumps(
                json.loads(original_plan_json),
                indent=2,
            )
            self.assertNotEqual(noncanonical_plan_json, original_plan_json)
            connection.execute(
                "UPDATE plans SET plan_json=? WHERE plan_handle=?",
                (noncanonical_plan_json, result.plan_handle),
            )
            connection.commit()
        with self.assertRaises(HostControlError):
            self.stored_plan(result)
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "UPDATE plans SET plan_json=? WHERE plan_handle=?",
                ("{}", result.plan_handle),
            )
            connection.commit()
        with self.assertRaises(HostControlError):
            self.stored_plan(result)
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "UPDATE plans SET plan_json=?, plan_digest=? "
                "WHERE plan_handle=?",
                (original_plan_json, "c" * 64, result.plan_handle),
            )
            connection.commit()
        with self.assertRaises(HostControlError):
            self.stored_plan(result)

    def test_plan_record_persists_structured_approval_class_and_families(self) -> None:
        result = self.plan()
        stored = self.stored_plan(result)
        self.assertEqual(stored.approval_class, "human")
        self.assertEqual(stored.operation_families, ("persona.install",))
        raw = stored.to_dict()
        self.assertEqual(raw["approval_class"], "human")
        self.assertEqual(raw["operation_families"], ["persona.install"])
        forged_summary = dict(json.loads(result.summary_json))
        forged_summary["approval_class"] = "none"
        forged_summary["operation_families"] = []
        self.write_policy(
            {
                "schema": "pantheon-host-policy/v1",
                "revision": "policy-drifted",
                "grants": [],
            }
        )
        self.service._policy = HostPolicy.load(  # type: ignore[attr-defined]
            self.policy_path,
            files=self.files,
        )
        reloaded = self.stored_plan(result)
        self.assertEqual(reloaded.approval_class, "human")
        self.assertEqual(reloaded.operation_families, ("persona.install",))
        self.assertEqual(forged_summary["approval_class"], "none")

    def test_policy_denied_plan_is_not_journaled_or_replayable(self) -> None:
        scenarios: list[tuple[str, dict]] = []
        scenarios.append(
            (
                "subject_denied",
                {
                    "schema": "pantheon-host-policy/v1",
                    "revision": "policy-subject-deny",
                    "grants": [],
                },
            )
        )
        target_denied = self.policy_dict()
        target_denied["revision"] = "policy-target-deny"
        target_denied["grants"][0]["target_seat_id"] = "peer"
        scenarios.append(("target_denied", target_denied))
        manifest_denied = self.policy_dict()
        manifest_denied["revision"] = "policy-manifest-deny"
        manifest_denied["grants"][0]["manifest_revisions"] = ["other-revision"]
        scenarios.append(("manifest_denied", manifest_denied))
        family_denied = self.policy_dict()
        family_denied["revision"] = "policy-family-deny"
        family_denied["grants"][0]["operation_families"] = [
            "unsupported.family"
        ]
        scenarios.append(("family_denied", family_denied))

        for expected_reason, policy_value in scenarios:
            self.write_policy(policy_value)
            denied_policy = HostPolicy.load(self.policy_path, files=self.files)
            service = self.build_service(
                policy=denied_policy,
                host_operation_families=frozenset(("persona.install",)),
            )
            with self.subTest(reason=expected_reason), mock.patch.object(
                self.installer,
                "plan",
                side_effect=AssertionError("denial reached plan generation"),
            ):
                with self.assertRaises(PolicyDenied) as caught:
                    service.plan(
                        1003,
                        "recovery",
                        "manifest-r1",
                        f"denied-{expected_reason}",
                    )
                self.assertEqual(caught.exception.reason_code, expected_reason)
                self.assertEqual(self.row_count(), 0)

        self.write_policy(self.policy_dict())
        approval_policy = HostPolicy.load(self.policy_path, files=self.files)
        approval_service = self.build_service(policy=approval_policy)
        with mock.patch.object(
            HostPolicy,
            "authorize_plan",
            return_value=AuthorizationDecision(
                False,
                "none",
                (),
                "approval_class_denied",
            ),
        ), mock.patch.object(
            self.installer,
            "plan",
            side_effect=AssertionError("denial reached plan generation"),
        ):
            with self.assertRaises(PolicyDenied) as caught:
                approval_service.plan(
                    1003,
                    "recovery",
                    "manifest-r1",
                    "denied-approval",
                )
        self.assertEqual(caught.exception.reason_code, "approval_class_denied")
        self.assertEqual(self.row_count(), 0)

        invalid_approval = self.policy_dict()
        invalid_approval["grants"][0]["approval_class"] = "wildcard"
        self.write_policy(invalid_approval)
        with self.assertRaises(RegistryError):
            HostPolicy.load(self.policy_path, files=self.files)
        self.assertEqual(self.row_count(), 0)

    def test_journal_initializes_exact_v1_schema_and_refuses_version_drift(self) -> None:
        # Retargeted under its reviewed name (T-2909, directed by the
        # dispatcher): this test now guards DDL_V1 byte-stability against the
        # LITERAL v1 digest. Pinning the literal here means a self-consistent
        # edit of DDL_V1 plus SCHEMA_DIGEST_V1 cannot silently re-anchor what
        # "v1" means, which is the precondition for v1->v2 migration
        # correctness. v2 initialization and drift-refusal coverage lives in
        # tools.test_host_control_operations.
        locked_digest = (
            "82b1b18c874607a62119778fd92d7310"
            "ac75936c08e56fedd356fdc3fa8ee2fa"
        )
        self.assertEqual(
            hashlib.sha256(DDL_V1.encode("utf-8")).hexdigest(),
            locked_digest,
        )
        self.assertEqual(SCHEMA_DIGEST_V1, locked_digest)

        with sqlite3.connect(":memory:") as connection:
            connection.executescript(DDL_V1)
            tables = connection.execute(
                "SELECT name, strict FROM pragma_table_list "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            self.assertEqual(tables, [("journal_meta", 1), ("plans", 1)])
            indexes = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            self.assertEqual(
                indexes,
                [("plans_effect_digest_idx",), ("plans_subject_expiry_idx",)],
            )

        # Version drift is still refused: a byte-exact v1 journal no longer
        # serves (explicit migration required), and a mutated v1 is refused
        # outright rather than being offered migration.
        v1_path = self.root / "legacy-v1.sqlite3"
        with sqlite3.connect(v1_path) as connection:
            connection.executescript(DDL_V1)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT INTO journal_meta VALUES (1, 1, ?, ?)",
                (SCHEMA_DIGEST_V1, "2026-07-31T00:00:00+00:00"),
            )
            connection.commit()
        with self.assertRaises(MigrationRequired):
            BrokerJournal.open(v1_path)
        with sqlite3.connect(v1_path) as connection:
            connection.executescript("CREATE TABLE extra(value TEXT) STRICT;")
            connection.commit()
        with self.assertRaises(HostControlError) as caught:
            BrokerJournal.open(v1_path)
        self.assertNotIsInstance(caught.exception, MigrationRequired)

    def test_journal_enforces_unique_handles_digests_and_idempotency_tuples(self) -> None:
        self.plan()
        self.journal.close()
        with sqlite3.connect(self.journal_path) as connection:
            row = list(
                connection.execute(
                    "SELECT * FROM plans LIMIT 1"
                ).fetchone()
            )
            cases = []
            duplicate_handle = row.copy()
            duplicate_handle[8] = "another-key"
            cases.append(duplicate_handle)
            duplicate_digest = row.copy()
            duplicate_digest[0] = "e" * 64
            duplicate_digest[8] = "another-key"
            cases.append(duplicate_digest)
            duplicate_tuple = row.copy()
            duplicate_tuple[0] = "d" * 64
            duplicate_tuple[1] = "c" * 64
            cases.append(duplicate_tuple)
            for candidate in cases:
                with self.subTest(candidate=candidate[:2]):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO plans VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            candidate,
                        )
                    connection.rollback()
            independent_subject = row.copy()
            independent_subject[0] = "a" * 64
            independent_subject[1] = "b" * 64
            independent_subject[3] = 2003
            independent_subject[4] = "other-subject"
            connection.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                independent_subject,
            )
            independent_target = row.copy()
            independent_target[0] = "c" * 64
            independent_target[1] = "d" * 64
            independent_target[6] = "peer"
            connection.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                independent_target,
            )
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT count(*) FROM plans").fetchone()[0],
                3,
            )
        self.journal = BrokerJournal.open(self.journal_path)

    def test_trusted_loader_rejects_untrusted_modes_owners_links_and_races(self) -> None:
        target = self.root / "trusted.json"
        self.write_json(target, {"value": "ok"})
        self.assertEqual(self.files.read_json(target, max_bytes=1024)["value"], "ok")

        for mode in (0o620, 0o602):
            target.chmod(mode)
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(RegistryError):
                    self.files.read_json(target, max_bytes=1024)
        target.chmod(0o600)

        root_link = self.root / "root-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RegistryError):
            RootOwnedFileLoader._open_for_test(
                (root_link,),
                expected_owner_uid=os.getuid(),
            )

        real_component = self.root / "real-component"
        real_component.mkdir(mode=0o700)
        component_file = real_component / "value.json"
        self.write_json(component_file, {"value": "component"})
        component_link = self.root / "component-link"
        component_link.symlink_to(real_component, target_is_directory=True)
        with self.assertRaises(RegistryError):
            self.files.read_json(
                component_link / "value.json",
                max_bytes=1024,
            )

        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaises(RegistryError):
            self.files.read_json(link, max_bytes=1024)

        with self.assertRaises(RegistryError):
            self.files.read_bytes(
                self.root / ".." / self.root.name / "trusted.json",
                max_bytes=1024,
            )
        with self.assertRaises(RegistryError):
            self.files.read_bytes(Path("/etc/passwd"), max_bytes=1024)

        directory_target = self.root / "not-a-file"
        directory_target.mkdir(mode=0o700)
        with self.assertRaises(RegistryError):
            self.files.read_bytes(directory_target, max_bytes=1024)

        with mock.patch(
            "tools.host_control.trusted_files._validate_stat",
            side_effect=RegistryError("wrong owner"),
        ):
            with self.assertRaises(RegistryError):
                self.files.read_json(target, max_bytes=1024)

        target.write_bytes(b"x" * 20)
        with self.assertRaises(RegistryError):
            self.files.read_bytes(target, max_bytes=10)

        target.write_bytes(b"short-read")
        with mock.patch(
            "tools.host_control.trusted_files.os.read",
            return_value=b"",
        ):
            with self.assertRaises(RegistryError):
                self.files.read_bytes(target, max_bytes=1024)

        target.write_bytes(b'{"value":"old"}')
        original_read = os.read
        replaced = False

        def replacing_read(fd: int, size: int) -> bytes:
            nonlocal replaced
            value = original_read(fd, size)
            if not replaced:
                replaced = True
                replacement = self.root / "replacement.json"
                replacement.write_text('{"value":"new"}', encoding="utf-8")
                os.replace(replacement, target)
            return value

        with mock.patch(
            "tools.host_control.trusted_files.os.read",
            side_effect=replacing_read,
        ):
            with self.assertRaises(RegistryError):
                self.files.read_json(target, max_bytes=1024)

    def test_policy_registry_and_manifest_share_one_frozen_trust_boundary(self) -> None:
        with mock.patch.object(
            self.files,
            "read_json",
            wraps=self.files.read_json,
        ) as read_json, mock.patch.object(
            self.files,
            "read_bytes",
            wraps=self.files.read_bytes,
        ) as read_bytes, mock.patch.object(
            Manifest,
            "load",
            side_effect=AssertionError("planner reopened manifest path"),
        ):
            registry = SeatRegistry.load(self.registry_path, files=self.files)
            policy = HostPolicy.load(self.policy_path, files=self.files)
            result = self.build_service(
                registry=registry,
                policy=policy,
            ).plan(1003, "recovery", "manifest-r1", "shared-boundary")
        self.assertEqual(
            {call.args[0] for call in read_json.call_args_list},
            {self.registry_path, self.policy_path},
        )
        self.assertIn(
            self.manifest_path,
            {call.args[0] for call in read_bytes.call_args_list},
        )
        self.assertEqual(self.stored_plan(result).manifest_sha256, hashlib.sha256(
            self.manifest_path.read_bytes()
        ).hexdigest())

        duplicate_json = (
            '{"schema":"pantheon-host-policy/v1","revision":"one",'
            '"revision":"two","grants":[]}'
        )
        self.policy_path.write_text(duplicate_json, encoding="utf-8")
        with self.assertRaises(RegistryError):
            HostPolicy.load(self.policy_path, files=self.files)

    def test_effect_digest_is_not_the_installer_integrity_digest(self) -> None:
        result = self.plan()
        stored = self.stored_plan(result)
        changed = _plan_replace(
            stored.installer_plan,
            provenance=dict(stored.installer_plan.provenance)
            | {"manifest": "/storage-only-change"},
        )
        self.assertNotEqual(changed.digest, stored.installer_plan.digest)
        self.assertEqual(
            compute_effect_digest(
                target=self.registry.seat("recovery"),
                manifest=self.registry.manifest("manifest-r1"),
                installer_plan=changed,
            ),
            stored.effect_digest,
        )
        changed_manifest = dataclasses.replace(
            self.registry.manifest("manifest-r1"),
            revision="manifest-r2",
        )
        self.assertNotEqual(
            compute_effect_digest(
                target=self.registry.seat("recovery"),
                manifest=changed_manifest,
                installer_plan=stored.installer_plan,
            ),
            stored.effect_digest,
        )


if __name__ == "__main__":
    unittest.main()
