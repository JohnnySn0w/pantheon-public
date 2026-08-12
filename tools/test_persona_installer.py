"""Tests for the ORG-049 manifest-driven persona installer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tools.agent_role_map import instance_binding, resolve_instance_role
from tools.persona_bringup import load_binding
from tools.persona_installer import (
    ApplyReceipt,
    ActorContext,
    LocalHostBackend,
    Manifest,
    ManifestError,
    OperationFailed,
    PersonaInstaller,
    PreflightResult,
    SimulatedHostBackend,
    _finite_next_elapse_basis,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _manifest_dict(*, second_persona: bool = False) -> dict:
    roles: dict[str, dict] = {
        "recovery": {
            "persona": "recovery",
            "account": "claude-fallback",
            "harness": "claude-code",
        }
    }
    if second_persona:
        roles["peer-daemon-cairn"] = {
            "persona": "cairn",
            "account": "codex",
            "harness": "codex",
        }
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
                "service_template": "tools/systemd/pantheon-persona-consume@.service",
                "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
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
        "roles": roles,
    }


class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.write_manifest(_manifest_dict(second_persona=True))
        self.manifest = Manifest.load(self.manifest_path)
        self.installer = PersonaInstaller(REPO_ROOT)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, value: dict) -> None:
        self.manifest_path.write_text(
            yaml.safe_dump(value, sort_keys=False),
            encoding="utf-8",
        )

    def test_manifest_from_bytes_matches_load_and_never_reopens_path(self) -> None:
        raw = self.manifest_path.read_bytes()
        parsed = Manifest.from_bytes(self.manifest_path, raw)
        self.assertEqual(parsed, Manifest.load(self.manifest_path))

        missing_path = self.root / "does-not-exist.yaml"
        self.assertEqual(
            Manifest.from_bytes(missing_path, raw),
            parsed.__class__(
                path=missing_path,
                version=parsed.version,
                instance_id=parsed.instance_id,
                installer=parsed.installer,
                roles=parsed.roles,
            ),
        )

        for invalid in (b"\xff", b"not: [valid"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ManifestError):
                    Manifest.from_bytes(self.manifest_path, invalid)

        self.manifest_path.write_bytes(b"\xff")
        with self.assertRaises(ManifestError):
            Manifest.load(self.manifest_path)

    def plan(self, persona: str = "recovery", harness: str | None = None):
        return self.installer.plan(
            self.manifest,
            persona,
            harness_override=harness,
            root=self.root / "projection",
        )

    def actor(self, plan, *, all_capabilities: bool = True) -> ActorContext:
        capabilities = frozenset(
            operation.provenance["required_capability"]
            for operation in plan.operations
            if operation.provenance.get("required_capability")
        )
        capabilities |= frozenset(
            action.required_capability for action in plan.operator_actions
        )
        if not all_capabilities:
            capabilities = frozenset()
        return ActorContext(
            uid=1000,
            persona="cairn",
            role="installer",
            instance=plan.instance,
            capabilities=capabilities,
        )

    def apply(self, plan, backend=None):
        backend = backend or SimulatedHostBackend()
        return (
            backend,
            self.installer.apply(plan, plan.digest, backend, self.actor(plan)),
        )

    def operation(self, plan, operation_id: str):
        return next(item for item in plan.operations if item.operation_id == operation_id)

    def test_versioned_manifest_preserves_roles_consumers(self) -> None:
        manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        self.assertGreaterEqual(manifest.version, 2)
        self.assertEqual(resolve_instance_role("recovery"), "recovery")
        self.assertEqual(instance_binding("recovery")["account"], "claude-fallback")
        runtime = load_binding(REPO_ROOT / "instance.yaml", "recovery")
        self.assertEqual(runtime.role, "recovery")
        self.assertEqual(runtime.account, "claude-fallback")
        from tools import context_fill_monitor, restart_drain, ticket

        self.assertEqual(
            context_fill_monitor.AGENT_CONFIG["recovery"]["sudo_user"],
            "claude-fallback",
        )
        self.assertEqual(
            restart_drain.DRAIN_AGENT_CONFIG["recovery"]["restart_service"],
            # T-2684: recovery is control-plane-supervised (T-2590 cutover) —
            # restart_service is derived to the consume unit, mirroring
            # specialist's T-2658 shape.
            "pantheon-persona-consume@recovery.service",
        )
        self.assertIn("recovery", ticket._valid_target_values())
        self.assertIn("codex", ticket._valid_target_values())

    def test_plan_marks_operator_required_and_privileged_steps(self) -> None:
        plan = self.plan()
        privileges = {item.privilege for item in plan.operations}
        self.assertEqual(
            privileges,
            {"unprivileged", "privileged-helper", "operator-required"},
        )
        self.assertTrue(plan.operator_actions)
        self.assertTrue(
            all(
                item.provenance.get("required_capability")
                for item in plan.operations
                if item.privilege != "unprivileged"
            )
        )

    def test_manifest_relative_paths_cannot_escape_declared_roots(self) -> None:
        mutations = (
            ("paths", "unit_dir", "/etc/systemd/user"),
            ("paths", "state_dir", "../../operator-state"),
            ("systemd", "service_template", "../foreign.service"),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                raw = _manifest_dict()
                raw["persona_installer"][section][key] = value
                self.write_manifest(raw)
                manifest = Manifest.load(self.manifest_path)
                with self.assertRaises(ManifestError):
                    self.installer.plan(
                        manifest,
                        "recovery",
                        root=self.root / "projection",
                    )
        raw = _manifest_dict()
        raw["persona_installer"]["policy"]["bundle"] = ["/etc/"]
        self.write_manifest(raw)
        with self.assertRaises(ManifestError):
            self.installer.plan(
                Manifest.load(self.manifest_path),
                "recovery",
                root=self.root / "projection",
            )

    def test_apply_refuses_unavailable_privilege(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan, all_capabilities=False),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(backend.entries, {})
        self.assertEqual(backend.events, [])

    def test_apply_refuses_backend_unavailable_before_mutation(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(unavailable_kinds=("manager-check",))
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(backend.entries, {})
        self.assertEqual(backend.events, [])

    def test_plan_digest_mismatch_refuses_before_mutation(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend()
        report = self.installer.apply(
            plan,
            "0" * 64,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(backend.entries, {})

    def test_unknown_existing_file_fails_closed(self) -> None:
        plan = self.plan()
        target = self.operation(plan, "connective.service-unit").target
        backend = SimulatedHostBackend()
        backend.seed_file(target, "operator-owned file\n")
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertEqual(
            json.loads(json.dumps(backend.entries[target])),
            backend.entries[target],
        )
        self.assertEqual(backend.events, [])

    def test_apply_failure_rolls_back_only_owned_unchanged_paths(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(fail_operation="connective.daemon-reload")
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertGreater(report.changed, 0)
        self.assertEqual(backend.entries, {})
        self.assertTrue(all("restored" in item for item in report.rollback))

    def test_rollback_refuses_post_apply_drift(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertIsNotNone(applied.receipt)
        target = self.operation(plan, "connective.service-unit").target
        backend.drift(target, "operator changed this after apply\n")
        report = self.installer.rollback(
            applied.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "refused")
        self.assertIn("drifted", report.errors[0])

    def test_user_systemd_first_fire_precedes_timer_enable(self) -> None:
        plan = self.plan()
        backend, report = self.apply(plan)
        self.assertEqual(report.status, "applied")
        self.assertLess(
            backend.events.index("harness.direct-fire"),
            backend.events.index("connective.timer-enable"),
        )
        self.assertLess(
            backend.events.index("connective.unit-fire"),
            backend.events.index("connective.timer-enable"),
        )
        self.assertLess(
            backend.events.index("connective.mailbox-probe"),
            backend.events.index("connective.timer-enable"),
        )

    def test_timer_next_elapse_checked_after_oneshot_inactive(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(finite_next_elapse=False)
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("timer observation incomplete", report.errors[0])
        self.assertFalse(backend.timer_enabled)

    def test_finite_next_elapse_basis_accepts_realtime_and_monotonic(self) -> None:
        cases = (
            (
                {
                    "NextElapseUSecRealtime": "Sat 2026-08-02 14:40:00 UTC",
                    "NextElapseUSecMonotonic": "infinity",
                },
                "realtime",
            ),
            (
                {
                    "NextElapseUSecRealtime": "",
                    "NextElapseUSecMonotonic": " 2min 5s ",
                },
                "monotonic",
            ),
            (
                {
                    "NextElapseUSecRealtime": "tomorrow",
                    "NextElapseUSecMonotonic": "5min",
                },
                "realtime",
            ),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(_finite_next_elapse_basis(values), expected)

    def test_finite_next_elapse_basis_rejects_systemd_sentinels(self) -> None:
        invalid_values = ("", "0", "n/a", "N/A", "infinity", " InFiNiTy ")
        for realtime in invalid_values:
            for monotonic in invalid_values:
                with self.subTest(realtime=realtime, monotonic=monotonic):
                    self.assertIsNone(
                        _finite_next_elapse_basis(
                            {
                                "NextElapseUSecRealtime": realtime,
                                "NextElapseUSecMonotonic": monotonic,
                            }
                        )
                    )

    def test_timer_acceptance_requires_observed_timed_fire(self) -> None:
        plan = self.plan()
        backend = SimulatedHostBackend(observed_timed_fire=False)
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("timer observation incomplete", report.errors[0])
        self.assertFalse(backend.timer_enabled)

    def test_mailbox_acl_send_and_receive_are_both_verified(self) -> None:
        plan = self.plan()
        for send, receive in ((False, True), (True, False)):
            with self.subTest(send=send, receive=receive):
                backend = SimulatedHostBackend(
                    mailbox_send=send,
                    mailbox_receive=receive,
                )
                report = self.installer.apply(
                    plan,
                    plan.digest,
                    backend,
                    self.actor(plan),
                )
                self.assertEqual(report.status, "failed")
                self.assertIn("mailbox ACL direction failed", report.errors[0])

    def test_secret_values_and_auth_file_copy_are_rejected(self) -> None:
        for key in ("value", "auth_file", "copy_from", "template"):
            with self.subTest(key=key):
                raw = _manifest_dict()
                raw["roles"]["recovery"]["credential_refs"] = [
                    {
                        "id": "github/recovery",
                        "kind": "deploy-key",
                        "provider": "host-file",
                        "owner": "claude-fallback",
                        "scope": ["repo:read"],
                        "rotation": {"strategy": "replace", "revoke": "remove old"},
                        "direct_use_justification": "provider cannot mint leases",
                        key: "forbidden",
                    }
                ]
                self.write_manifest(raw)
                manifest = Manifest.load(self.manifest_path)
                with self.assertRaises(ManifestError):
                    manifest.binding("recovery")

    def test_credential_ref_requires_provider_scope_and_rotation_plan(self) -> None:
        raw = _manifest_dict()
        valid = {
            "id": "github/recovery",
            "kind": "deploy-key",
            "provider": "host-file",
            "owner": "claude-fallback",
            "scope": ["repo:read"],
            "rotation": {"strategy": "replace", "revoke": "remove old grant"},
            "location": "/run/user/1001/pantheon/github-recovery",
            "direct_use_justification": "provider cannot mint leases",
        }
        raw["roles"]["recovery"]["credential_refs"] = [valid]
        self.write_manifest(raw)
        binding = Manifest.load(self.manifest_path).binding("recovery")
        self.assertEqual(binding.credential_refs[0].reference_id, "github/recovery")
        for missing in ("provider", "scope", "rotation"):
            with self.subTest(missing=missing):
                broken = _manifest_dict()
                item = dict(valid)
                item.pop(missing)
                broken["roles"]["recovery"]["credential_refs"] = [item]
                self.write_manifest(broken)
                with self.assertRaises(ManifestError):
                    Manifest.load(self.manifest_path).binding("recovery")

    def test_harness_switch_preserves_persona_role_and_owner(self) -> None:
        normal = self.plan()
        switched = self.plan(harness="codex")
        self.assertEqual(
            (normal.persona, normal.role, normal.account),
            (switched.persona, switched.role, switched.account),
        )
        self.assertEqual(normal.configured_harness, "claude")
        self.assertEqual(switched.selected_harness, "codex")

    def test_verify_reports_all_layers_and_provenance(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertEqual(applied.status, "applied")
        report = self.installer.verify(plan, backend)
        self.assertEqual(report.status, "verified")
        self.assertEqual([item.layer for item in report.layers], [0, 1, 2, 3, 4])
        self.assertTrue(all(item.ok for item in report.layers))
        self.assertEqual(report.provenance["persona"], "recovery")
        self.assertEqual(report.provenance["instance"], "test-instance")

    def test_existing_seat_reapply_is_noop(self) -> None:
        plan = self.plan()
        backend, first = self.apply(plan)
        second = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(first.status, "applied")
        self.assertEqual(second.status, "noop")
        self.assertEqual(second.changed, 0)

    def test_successful_rollback_disables_owned_timer_and_restores_files(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertTrue(backend.timer_enabled)
        report = self.installer.rollback(
            applied.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "rolled-back")
        self.assertFalse(backend.timer_enabled)
        self.assertEqual(backend.entries, {})

    def test_staged_receipt_precedes_activation_and_recovers_crash(self) -> None:
        class CrashAfterTimerEnable(SimulatedHostBackend):
            def execute(self, operation):
                result = super().execute(operation)
                if operation.kind == "timer-enable":
                    raise SystemExit("simulated process crash")
                return result

        plan = self.plan()
        backend = CrashAfterTimerEnable()
        with self.assertRaisesRegex(SystemExit, "simulated process crash"):
            self.installer.apply(
                plan,
                plan.digest,
                backend,
                self.actor(plan),
            )
        self.assertTrue(backend.timer_enabled)
        self.assertLess(
            backend.events.index("receipt:staged"),
            backend.events.index("connective.timer-enable"),
        )
        raw_receipt = next(iter(backend.receipts.values()))
        self.assertEqual(raw_receipt["phase"], "staged")
        receipt = ApplyReceipt.from_dict(raw_receipt)
        rollback = self.installer.rollback(receipt, backend, self.actor(plan))
        self.assertEqual(rollback.status, "rolled-back")
        self.assertFalse(backend.timer_enabled)
        self.assertEqual(backend.entries, {})

    def test_failed_receipt_retains_partial_timer_enable_intent(self) -> None:
        class PartialTimerFailure(SimulatedHostBackend):
            suppress_first_timer_rollback = True

            def execute(self, operation):
                result = super().execute(operation)
                if operation.kind == "timer-enable":
                    raise OperationFailed("enable returned failure after side effect")
                return result

            def can_rollback(self, mutation):
                if (
                    mutation.kind == "timer-enable"
                    and self.suppress_first_timer_rollback
                ):
                    self.suppress_first_timer_rollback = False
                    return PreflightResult(
                        False,
                        "injected",
                        "first automatic timer rollback unavailable",
                    )
                return super().can_rollback(mutation)

        plan = self.plan()
        backend = PartialTimerFailure()
        report = self.installer.apply(
            plan,
            plan.digest,
            backend,
            self.actor(plan),
        )
        self.assertEqual(report.status, "failed")
        self.assertTrue(backend.timer_enabled)
        self.assertEqual(report.receipt.phase, "failed")
        self.assertIn(
            "connective.timer-enable",
            {item.operation_id for item in report.receipt.mutations},
        )
        recovered = self.installer.rollback(
            report.receipt,
            backend,
            self.actor(plan),
        )
        self.assertEqual(recovered.status, "rolled-back")
        self.assertFalse(backend.timer_enabled)
        self.assertEqual(backend.entries, {})

    def test_second_persona_disposable_codex_seat_has_no_persona_branch(self) -> None:
        recovery = self.plan("recovery", "codex")
        cairn = self.plan("cairn")
        self.assertEqual(cairn.selected_harness, "codex")
        self.assertEqual(cairn.account, "codex")
        recovery_service = self.operation(recovery, "connective.service-unit").desired["content"]
        cairn_service = self.operation(cairn, "connective.service-unit").desired["content"]
        recovery_body = "\n".join(recovery_service.splitlines()[2:])
        cairn_body = "\n".join(cairn_service.splitlines()[2:])
        self.assertEqual(recovery_body, cairn_body)
        self.assertNotIn("recovery", cairn_body.lower())

    def test_fixture_only_proof_cannot_satisfy_live_acceptance(self) -> None:
        plan = self.plan()
        backend, applied = self.apply(plan)
        self.assertEqual(applied.status, "applied")
        report = self.installer.verify(plan, backend)
        self.assertEqual(report.status, "verified")
        self.assertEqual(report.evidence_scope, "fixture")
        self.assertFalse(report.live_acceptance)

    def test_local_backend_refuses_cross_account_before_mutation(self) -> None:
        plan = self.installer.plan(self.manifest, "recovery", root=Path("/"))
        backend = LocalHostBackend()
        foreign = SimpleNamespace(pw_uid=999_999)
        with patch("tools.persona_installer.pwd.getpwnam", return_value=foreign), \
             patch("tools.persona_installer.os.geteuid", return_value=1000), \
             patch.object(backend, "_run", side_effect=AssertionError("cross-account refusal must precede systemctl")) as run:
            report = self.installer.apply(plan, plan.digest, backend, self.actor(plan))
        self.assertEqual(report.status, "refused")
        self.assertIn("target account", report.errors[0])
        run.assert_not_called()

    def test_cross_process_plan_is_deterministic_and_secret_free(self) -> None:
        command = [
            sys.executable,
            str(REPO_ROOT / "tools" / "persona_installer.py"),
            "plan",
            "--manifest",
            str(self.manifest_path),
            "--persona",
            "recovery",
            "--root",
            str(self.root / "projection"),
        ]
        first = subprocess.run(command, check=True, capture_output=True, text=True)
        second = subprocess.run(command, check=True, capture_output=True, text=True)
        first_json = json.loads(first.stdout)
        second_json = json.loads(second.stdout)
        self.assertEqual(first_json, second_json)
        rendered = json.dumps(first_json).lower()
        self.assertNotIn('"value"', rendered)
        self.assertNotIn("copy_from", rendered)
        self.assertNotIn("auth_file", rendered)


if __name__ == "__main__":
    unittest.main()
