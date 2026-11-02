"""T-5533: the broker plans from a root-registered, digest-pinned plan.

Intent (T-2652 / ORG-052 section 1 and 3, split protocol from privilege):
``HostPlanService.plan`` no longer runs ``PersonaInstaller``; it reads the plan
the root installer registered for (seat, manifest revision), and applies its
checks to it. Pinned here:

* golden equivalence: for the core fixture, the new path (producer-registered
  plan) returns exactly the plan digest, effect digest and summary bytes that the
  old in-process path computed (``_reference_in_process_plan`` is a verbatim
  test-local copy of the removed planner body);
* every refusal on the registered-plan path, each before a handle is minted or
  anything is journaled: sha mismatch and drift, a plan registered only for
  another seat, a missing plan record, wrong owner, wrong mode, symlink, target
  mismatch, framework revision and digest mismatch, non-canonical/malformed/
  duplicate-key/bad-digest bytes, a plan bound to another manifest, and a plan
  that is stale against the framework copy (orchestrator's scope add);
* policy denial still happens before the registered plan is read.
"""

from __future__ import annotations

import json
import unittest
from datetime import timezone
from pathlib import Path
from unittest import mock

from tools import host_plan_producer as HPP
from tools._host_control_plan_fixtures import framework_overlay, plan_bytes, register_plan
from tools.host_control.contracts import (
    HostControlError,
    PlanEnvelope,
    PolicyDenied,
    RegistryError,
    compute_effect_digest,
)
from tools.host_control.envelope import require_executor_envelope
from tools.host_control.journal import BrokerJournal
from tools.host_control.planner import HostPlanService, partition_installer_plan
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.self_amendment import default_protected_roots, refuse_self_amendment
from tools.persona_installer import Manifest, PersonaInstaller
from tools import test_host_control_core as _core  # module import: never re-collect its TestCases
from tools.test_host_control_core import FIXED_NOW, REPO_ROOT, _plan_replace


class HostControlCase(unittest.TestCase):
    """The core fixture's setUp and helpers, without its test methods."""

    setUp = _core.HostControlCase.setUp
    tearDown = _core.HostControlCase.tearDown
    build_service = _core.HostControlCase.build_service
    write_json = _core.HostControlCase.write_json
    registry_dict = _core.HostControlCase.registry_dict
    write_registry = _core.HostControlCase.write_registry
    policy_dict = _core.HostControlCase.policy_dict
    write_policy = _core.HostControlCase.write_policy
    row_count = _core.HostControlCase.row_count
    plan = _core.HostControlCase.plan


def _reference_in_process_plan(case: HostControlCase, service: HostPlanService, *, handle: str, registry=None):
    """The pre-T-5533 broker path, verbatim in substance: parse the frozen
    manifest, check the template binding, authorize, plan IN-PROCESS with
    PersonaInstaller, then the same checks and envelope. Returns
    ``(plan_digest, effect_digest, summary_json)``."""

    registry, policy = registry or case.registry, case.policy
    subject = registry.subject_for_uid(1003)
    target = registry.seat("recovery")
    manifest_record = registry.manifest("manifest-r1")
    frozen = Manifest.from_bytes(manifest_record.path, registry.read_manifest_bytes(manifest_record))
    HPP.validate_frozen_manifest(frozen, manifest_record.template_ids)
    decision = policy.authorize_plan(subject, target, manifest_record)
    assert decision.allowed
    allowed_families = tuple(
        family for family in decision.operation_families if family in ("persona.install", "persona.verify")
    )
    full_plan = PersonaInstaller(REPO_ROOT).plan(
        frozen, target.persona, harness_override=target.harness, root=target.install_root
    )
    assert full_plan.validate_digest()
    service.runtime.refuse_outside_table(
        (operation.kind for operation in full_plan.operations), manifest_revision="manifest-r1"
    )
    refuse_self_amendment(full_plan.operations, default_protected_roots(()))
    installer_plan, _residue = partition_installer_plan(full_plan)
    require_executor_envelope(operation.kind for operation in installer_plan.operations)
    effect_digest = compute_effect_digest(target=target, manifest=manifest_record, installer_plan=installer_plan)
    created = FIXED_NOW.astimezone(timezone.utc)
    envelope = PlanEnvelope.create(
        schema="pantheon-host-plan-envelope/v1",
        plan_handle=handle,
        owner=subject,
        target_seat_id=target.seat_id,
        manifest_revision=manifest_record.revision,
        manifest_sha256=manifest_record.sha256,
        framework_revision=manifest_record.framework_revision,
        framework_sha256=manifest_record.framework_sha256,
        template_ids=manifest_record.template_ids,
        policy_revision=policy.revision,
        operation_families=allowed_families,
        approval_class=decision.approval_class,
        created_at=created.isoformat(),
        expires_at=(created + case_ttl()).isoformat(),
        installer_plan=installer_plan,
        effect_digest=effect_digest,
    )
    return envelope.digest, envelope.effect_digest, HostPlanService.plan_summary(envelope)


def case_ttl():
    from datetime import timedelta

    return timedelta(hours=1)


class GoldenEquivalenceTest(HostControlCase):
    def test_registered_plan_path_equals_the_old_in_process_path(self) -> None:
        # register the PRODUCER's bytes (not the fixture prototype); T-5533 review
        # r1: the codex fixture plus its claude-harness and alt-root variants.
        for harness, install_root in (("codex", "/projection"), ("claude", "/projection"), ("codex", "/alt-root")):
            with self.subTest(harness=harness, install_root=install_root):
                seat = {
                    "seat_id": "recovery", "uid": 1002, "persona": "recovery", "role": "recovery",
                    "instance": "test-instance", "account": "claude-fallback", "harness": harness,
                    "install_root": install_root,
                }
                value = self.registry_dict()
                value["seats"] = [item if item["seat_id"] != "recovery" else seat for item in value["seats"]]
                manifest = dict(value["manifests"][0])
                produced = HPP.produce_plan(framework_root=REPO_ROOT, seat_record=seat, manifest_record=manifest)
                register_plan(value, self.root, produced, seat_id="recovery", manifest_revision="manifest-r1")
                self.write_registry(value)
                registry = SeatRegistry.load(self.registry_path, files=self.files, framework_root=self.framework)
                service = self.build_service(registry=registry)
                result = service.plan(1003, "recovery", "manifest-r1", f"golden-{harness}-{install_root}")
                expected = _reference_in_process_plan(
                    self, service, handle=result.plan_handle, registry=registry
                )
                self.assertEqual((result.plan_digest, result.effect_digest, result.summary_json), expected)


class RegisteredPlanRefusalTest(HostControlCase):
    def service_with_plan(self, plan, *, seat_id="recovery", revision="manifest-r1", value=None):
        value = value or self.registry_dict()
        value["plans"] = []
        register_plan(value, self.root, plan, seat_id=seat_id, manifest_revision=revision)
        self.write_registry(value)
        self.registry = SeatRegistry.load(self.registry_path, files=self.files, framework_root=self.framework)
        return self.build_service(registry=self.registry)

    def assertRefused(self, service, error, message) -> None:
        with self.assertRaisesRegex(error, message):
            service.plan(1003, "recovery", "manifest-r1", "refused")
        self.assertEqual(self.row_count(), 0)

    def test_the_registered_fixture_plan_is_accepted(self) -> None:
        self.assertTrue(self.plan().plan_handle)

    def test_registered_sha_mismatch_and_drift_are_refused(self) -> None:
        value = self.registry_dict()
        value["plans"][0]["sha256"] = "0" * 64
        self.write_registry(value)
        with self.assertRaisesRegex(RegistryError, "registered plan hash mismatch"):
            SeatRegistry.load(self.registry_path, files=self.files, framework_root=self.framework)
        record = self.registry.plan_record("recovery", "manifest-r1")
        record.path.write_bytes(plan_bytes(self.prototype) + b"\n")
        self.assertRefused(self.service, RegistryError, "registered plan hash drift")

    def test_a_plan_registered_only_for_another_seat_is_refused(self) -> None:
        service = self.service_with_plan(self.prototype, seat_id="peer")
        self.assertRefused(service, RegistryError, "no registered plan")

    def test_a_missing_plan_record_is_refused(self) -> None:
        value = self.registry_dict()
        del value["plans"]
        self.write_registry(value)
        service = self.build_service(registry=SeatRegistry.load(self.registry_path, files=self.files, framework_root=self.framework))
        self.assertRefused(service, RegistryError, "no registered plan")

    def test_wrong_owner_mode_and_symlink_are_refused(self) -> None:
        record = self.registry.plan_record("recovery", "manifest-r1")
        inode = record.path.stat().st_ino
        from tools.host_control import trusted_files

        original = trusted_files._validate_stat

        def other_owner(value, *, expected_owner_uid, kind):
            if value.st_ino == inode:
                raise RegistryError(f"untrusted {kind} owner")
            return original(value, expected_owner_uid=expected_owner_uid, kind=kind)

        with mock.patch.object(trusted_files, "_validate_stat", other_owner):
            self.assertRefused(self.service, RegistryError, "untrusted file owner")
        record.path.chmod(0o622)
        self.assertRefused(self.service, RegistryError, "untrusted file mode")
        record.path.chmod(0o600)
        real = self.root / "elsewhere.json"
        real.write_bytes(record.path.read_bytes())
        real.chmod(0o600)
        record.path.unlink()
        record.path.symlink_to(real)
        self.assertRefused(self.service, RegistryError, "")

    def test_target_mismatch_is_refused(self) -> None:
        for field, changed in (("account", "someone-else"), ("root", "/elsewhere"), ("selected_harness", "claude")):
            with self.subTest(field=field):
                service = self.service_with_plan(_plan_replace(self.prototype, **{field: changed}))
                self.assertRefused(service, HostControlError, f"target mismatch: {field}")

    def test_framework_revision_and_digest_mismatch_are_refused(self) -> None:
        for key, message in (
            ("declared_revision", "framework revision mismatch"),
            ("source_artifact_sha256", "framework digest mismatch"),
        ):
            with self.subTest(key=key):
                provenance = dict(self.prototype.provenance) | {key: "f" * 64}
                service = self.service_with_plan(_plan_replace(self.prototype, provenance=provenance))
                self.assertRefused(service, HostControlError, message)

    def test_a_plan_bound_to_another_manifest_is_refused(self) -> None:
        provenance = dict(self.prototype.provenance) | {"manifest": "/elsewhere/instance.yaml"}
        service = self.service_with_plan(_plan_replace(self.prototype, provenance=provenance))
        self.assertRefused(service, HostControlError, "bound to another manifest")

    def test_malformed_registered_bytes_are_refused(self) -> None:
        good = self.prototype.to_dict()
        tampered_digest = dict(good) | {"digest": "0" * 64}
        cases = {
            "not canonical": json.dumps(good, indent=1, sort_keys=True).encode("utf-8"),
            "invalid digest": json.dumps(tampered_digest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            "malformed": b'{"schema":"x"}',
            "not valid JSON": b"\xff not json",
            "duplicate JSON key": b'{"schema":"a","schema":"b"}',
        }
        for message, data in cases.items():
            with self.subTest(case=message):
                service = self.service_with_plan(data)
                self.assertRefused(service, HostControlError, message)

    def test_a_registry_without_a_framework_root_makes_plan_refuse(self) -> None:
        # T-5533 review r1 B1: fail closed; staleness cannot be proven without the copy.
        service = self.build_service(registry=SeatRegistry.load(self.registry_path, files=self.files))
        self.assertRefused(service, RegistryError, "no framework root configured")

    def test_policy_denial_happens_before_the_registered_plan_is_read(self) -> None:
        value = self.policy_dict()
        value["grants"][0]["target_seat_id"] = "peer"
        self.write_policy(value)
        service = self.build_service(policy=HostPolicy.load(self.policy_path, files=self.files))
        with mock.patch.object(SeatRegistry, "read_plan_bytes", side_effect=AssertionError("plan read")):
            with self.assertRaises(PolicyDenied):
                service.plan(1003, "recovery", "manifest-r1", "denied")
        self.assertEqual(self.row_count(), 0)


class FrameworkCopyStalenessThroughTheBrokerTest(HostControlCase):
    """orchestrator's scope add, at the broker path: after a framework-copy edit that
    changes the artifact digest, the previously registered plan is refused."""

    def test_a_framework_copy_edit_makes_the_broker_refuse_the_registered_plan(self) -> None:
        framework = self.framework
        registry = SeatRegistry.load(self.registry_path, files=self.files, framework_root=framework)
        service = self.build_service(registry=registry)
        self.assertTrue(service.plan(1003, "recovery", "manifest-r1", "fresh").plan_handle)
        target = framework / self.template_ids[0]
        target.write_bytes(target.read_bytes() + b"\n# framework copy updated\n")
        with self.assertRaisesRegex(RegistryError, "stale against the framework copy"):
            service.plan(1003, "recovery", "manifest-r1", "stale")
        self.assertEqual(self.row_count(), 1)
        with self.assertRaisesRegex(RegistryError, "stale against the framework copy"):
            self.build_service(
                registry=SeatRegistry.load(self.registry_path, files=self.files, framework_root=framework)
            ).plan(1003, "recovery", "manifest-r1", "stale-reload")
        self.assertEqual(self.row_count(), 1)
