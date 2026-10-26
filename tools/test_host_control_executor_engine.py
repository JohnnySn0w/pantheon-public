"""T-2917 5b: the per-seat executor's identity, config, spec validation,
seat-root file layer and closed operation loop (no D-Bus here).

Fixture evidence only: the executor runs as the test uid against a
disposable seat root; real cross-uid and the real user manager are T-2924.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import _host_control_executor_fixtures as fx
from tools.host_control import execution_source as broker_execution_source
from tools.host_control.contracts import RegistryError, Unsupported, canonical_json, sha256_json
from tools.host_control.executor import engine as engine_module
from tools.host_control.executor.config import (
    EXECUTOR_CONFIG_SCHEMA,
    parse_executor_config,
)
from tools.host_control.executor.engine import EXECUTION_SPEC_SCHEMA, parse_spec
from tools.host_control.executor.identity import (
    ExecutorStartRefused,
    resolve_executor_identity,
    seat_root_for,
)
from tools.host_control.executor.seat_files import SeatFiles, SeatPathRefused, parse_mode
from tools.host_control.executor.wire import (
    ExecutionResult,
    ExecutorDenied,
    InvalidSpec,
    bus_name_for_uid,
    decode_result,
    encode_result,
)


class _Case(unittest.TestCase):
    register_uid = True

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x", register_uid=self.register_uid)
        self.addCleanup(self.fixture.close)
        self.seat = self.fixture.seat_root

    def run_ops(self, *operations, kind="apply", engine=None):
        spec = fx.parsed(self.fixture, tuple(operations), execution_kind=kind)
        return (engine or self.fixture.engine()).run(spec)


class IdentityTests(_Case):
    def test_registered_uid_resolves_its_own_seat_and_bus_name(self) -> None:
        identity = self.fixture.identity
        self.assertEqual(identity.uid, os.getuid())
        self.assertEqual(identity.seat.seat_id, fx.SEAT_ID)
        self.assertEqual(identity.bus_name, f"org.pantheon.HostExecutor1.U{os.getuid()}")
        self.assertEqual(identity.seat_root, self.seat)

    def test_unregistered_uid_refuses_to_start(self) -> None:
        with self.assertRaises(ExecutorStartRefused):
            resolve_executor_identity(getuid=lambda: 999_999, registry=self.fixture.registry)

    def test_identity_inputs_are_only_getuid_and_the_registry(self) -> None:
        # the unit instance name, argv and environment play no part: a
        # different uid resolves a different (registered) seat
        other = resolve_executor_identity(
            getuid=lambda: os.getuid() + fx.OTHER_UID_OFFSET, registry=self.fixture.registry
        )
        self.assertEqual(other.seat.seat_id, "other")
        with mock.patch.dict(os.environ, {"SEAT_ID": "other"}), mock.patch("sys.argv", ["x", "other"]):
            again = resolve_executor_identity(getuid=os.getuid, registry=self.fixture.registry)
        self.assertEqual(again.seat.seat_id, fx.SEAT_ID)
        signature = resolve_executor_identity.__code__.co_varnames[: resolve_executor_identity.__code__.co_kwonlyargcount]
        self.assertEqual(set(signature), {"getuid", "registry"})

    def test_bus_name_is_derived_from_the_uid(self) -> None:
        self.assertEqual(bus_name_for_uid(0), "org.pantheon.HostExecutor1.U0")
        for bad in (-1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                bus_name_for_uid(bad)  # type: ignore[arg-type]

    def test_seat_root_derivation_matches_the_installer_home(self) -> None:
        seat = self.fixture.identity.seat
        self.assertEqual(seat_root_for(seat), seat.install_root / "home" / fx.ACCOUNT)


class UnregisteredStartTests(_Case):
    register_uid = False

    def test_fixture_without_the_uid_refuses(self) -> None:
        with self.assertRaises(ExecutorStartRefused):
            resolve_executor_identity(getuid=os.getuid, registry=self.fixture.registry)


class ConfigTests(unittest.TestCase):
    def good(self) -> dict:
        return {
            "schema": EXECUTOR_CONFIG_SCHEMA,
            "registry_path": "/etc/pantheon/registry.json",
            "trusted_roots": ["/etc/pantheon"],
            "broker_uid": 991,
            "framework_root": "/etc/pantheon/framework",
            # T-2917 5d.3 (T-2917:D19): the root-configured mailbox hub
            "mailbox_hub": "/srv/pantheon/mailbox",
        }

    def test_valid_config_parses(self) -> None:
        config = parse_executor_config(self.good())
        self.assertEqual(config.broker_uid, 991)
        self.assertEqual(config.framework_root, Path("/etc/pantheon/framework"))
        self.assertEqual(config.mailbox_hub, Path("/srv/pantheon/mailbox"))

    def test_config_refusals(self) -> None:
        cases = {
            "extra key": dict(self.good(), extra=1),
            "schema": dict(self.good(), schema="v0"),
            "bool uid": dict(self.good(), broker_uid=True),
            "neg uid": dict(self.good(), broker_uid=-1),
            "relative registry": dict(self.good(), registry_path="registry.json"),
            "unnormalized": dict(self.good(), framework_root="/etc/pantheon/../x"),
            "registry outside": dict(self.good(), registry_path="/home/x/registry.json"),
            "framework outside": dict(self.good(), framework_root="/home/seat/pantheon-config"),
            "no roots": dict(self.good(), trusted_roots=[]),
        }
        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(RegistryError):
                parse_executor_config(value)


class SpecValidationTests(_Case):
    def payload(self, **kwargs):
        operation = fx.op("diag.harness", "harness-check", "codex", {"harness": "codex"})
        return fx.spec_payload(self.fixture, (operation,), **kwargs)

    def parse(self, payload, *, expected=None, returned=None, schema=EXECUTION_SPEC_SCHEMA, kind=None):
        digest = sha256_json(payload)
        return parse_spec(
            schema_version=schema,
            execution_kind=kind or payload["execution_kind"],
            returned_digest=returned or digest,
            spec_json=canonical_json(payload),
            expected_digest=expected or digest,
            operation_handle=fx.OPERATION_HANDLE,
            identity=self.fixture.identity,
            registry=self.fixture.registry,
        )

    def test_transcribed_schema_matches_the_broker(self) -> None:
        self.assertEqual(EXECUTION_SPEC_SCHEMA, broker_execution_source.EXECUTION_SPEC_SCHEMA)

    def test_valid_spec_parses(self) -> None:
        parsed = self.parse(self.payload())
        self.assertEqual(parsed.execution_kind, "apply")
        self.assertEqual(len(parsed.operations), 1)

    def test_digest_is_recomputed_by_the_executor(self) -> None:
        payload = self.payload()
        with self.assertRaises(InvalidSpec):
            self.parse(payload, expected="0" * 64, returned="0" * 64)
        with self.assertRaises(InvalidSpec):
            self.parse(payload, returned="1" * 64)

    def test_target_must_be_this_executors_seat(self) -> None:
        with self.assertRaises(ExecutorDenied):
            self.parse(self.payload(target_seat_id="other"))
        with self.assertRaises(ExecutorDenied):
            self.parse(self.payload(target_seat_id="unknown-seat"))

    def test_operation_owner_must_be_the_seat_account(self) -> None:
        foreign = fx.op("diag.harness", "harness-check", "codex", owner="other")
        payload = fx.spec_payload(self.fixture, (foreign,))
        with self.assertRaises(ExecutorDenied):
            self.parse(payload)

    def test_out_of_envelope_and_unimplemented_kinds_are_refused(self) -> None:
        for kind, error in (
            ("claude-mcp-server-registration", InvalidSpec),
            ("future-kind", InvalidSpec),
            # T-2917 5c.2: unit-fire/daemon-reload are implemented now;
            # T-2917 5d.3 implements mailbox.probe (the flip); the executor's
            # own Unsupported gate stays pinned against the PRE-flip table
            # (MR-2 review: require_revision_implemented must keep biting)
            ("mailbox-probe", Unsupported),
        ):
            with self.subTest(kind=kind):
                from tools.test_host_control_driver import pre_flip_table

                payload = fx.spec_payload(self.fixture, (fx.op("x", kind, "t"),))
                with pre_flip_table(), self.assertRaises(error):
                    self.parse(payload)

    def test_rollback_spec_requires_a_receipt_and_bound_mutations(self) -> None:
        # T-2917 5d.2 replaces the 5b "rollback is Unsupported" pin: a
        # rollback spec parses only with its receipt and receipt mutations
        # bound to its own operations
        with self.assertRaises(InvalidSpec):
            self.parse(self.payload(execution_kind="rollback"))  # no receipt
        target = str(self.seat / "d")
        payload = fx.spec_payload(
            self.fixture, (fx.op("dir", "mkdir", target, {"mode": "0755"}),), execution_kind="rollback"
        )
        payload.update(receipt_handle="ab" * 32, receipt_digest="cd" * 32)
        with self.assertRaises(InvalidSpec):
            self.parse(payload)  # no mutations to restore
        mutation = {
            "operation_id": "dir", "kind": "mkdir", "target": target, "owner": fx.ACCOUNT,
            "changed": True, "before": {"state": "absent"}, "after_fingerprint": "directory",
        }
        parsed = self.parse(dict(payload, rollback_mutations=[mutation]))
        self.assertEqual(parsed.execution_kind, "rollback")
        self.assertEqual([m.operation_id for m in parsed.rollback_mutations], ["dir"])
        for bad in (dict(mutation, kind="atomic-write"), dict(mutation, changed=False), dict(mutation, operation_id="x" * 129)):
            with self.subTest(bad=sorted(bad.items())[:2]), self.assertRaises(InvalidSpec):
                self.parse(dict(payload, rollback_mutations=[bad]))

    def test_shape_refusals(self) -> None:
        payload = self.payload()
        with self.assertRaises(InvalidSpec):
            self.parse(payload, schema="pantheon-host-execution-spec/v0")
        with self.assertRaises(InvalidSpec):
            self.parse(payload, kind="verify")
        with self.assertRaises(InvalidSpec):
            self.parse(self.payload(allowed_roots=["/elsewhere"]))
        extra = dict(self.payload(), extra=1)
        with self.assertRaises(InvalidSpec):
            self.parse(extra)
        non_canonical = json.dumps(payload)
        digest = sha256_json(payload)
        with self.assertRaises(InvalidSpec):
            parse_spec(
                schema_version=EXECUTION_SPEC_SCHEMA,
                execution_kind="apply",
                returned_digest=digest,
                spec_json=non_canonical,
                expected_digest=digest,
                operation_handle=fx.OPERATION_HANDLE,
                identity=self.fixture.identity,
                registry=self.fixture.registry,
            )


class SeatFilesTests(_Case):
    def files(self, uid=None) -> SeatFiles:
        return SeatFiles(seat_root=self.seat, owner_uid=os.getuid() if uid is None else uid)

    def test_containment_refusals(self) -> None:
        files = self.files()
        for target in (
            "/etc/passwd",
            str(self.seat),
            str(self.seat) + "/../escape",
            "relative/path",
            str(self.seat.parent / "seatacct2" / "x"),
        ):
            with self.subTest(target=target), self.assertRaises(SeatPathRefused):
                files.write_atomic(target, b"x", 0o644)

    def test_symlink_component_is_never_traversed(self) -> None:
        outside = self.fixture.root / "outside"
        outside.mkdir()
        (self.seat / "link").symlink_to(outside)
        files = self.files()
        with self.assertRaises(SeatPathRefused):
            files.write_atomic(str(self.seat / "link" / "f"), b"x", 0o644)
        with self.assertRaises(SeatPathRefused):
            files.mkdir(str(self.seat / "link" / "d"), 0o755)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_final_component_is_refused(self) -> None:
        outside = self.fixture.root / "outside-file"
        outside.write_text("keep", encoding="utf-8")
        (self.seat / "f").symlink_to(outside)
        files = self.files()
        with self.assertRaises(SeatPathRefused):
            files.write_atomic(str(self.seat / "f"), b"x", 0o644)
        with self.assertRaises(SeatPathRefused):
            files.snapshot(str(self.seat / "f"))
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_owner_mismatch_is_refused(self) -> None:
        (self.seat / "f").write_text("x", encoding="utf-8")
        foreign = self.files(uid=os.getuid() + 1)
        with self.assertRaises(SeatPathRefused):
            foreign.write_atomic(str(self.seat / "f"), b"y", 0o644)
        with self.assertRaises(SeatPathRefused):
            foreign.kind(str(self.seat / "f"))

    def test_modes_with_special_bits_are_refused(self) -> None:
        for raw in ("4755", "2755", "1777", "8", "rwx", "07777"):
            with self.subTest(raw=raw), self.assertRaises(SeatPathRefused):
                parse_mode(raw, "0644")
        self.assertEqual(parse_mode("0600", "0644"), 0o600)
        self.assertEqual(parse_mode(None, "0644"), 0o644)

    def test_write_requires_existing_parent_and_is_atomic(self) -> None:
        files = self.files()
        with self.assertRaises(SeatPathRefused):
            files.write_atomic(str(self.seat / "missing" / "f"), b"x", 0o644)
        self.assertTrue(files.write_atomic(str(self.seat / "f"), b"hello", 0o600))
        self.assertEqual((self.seat / "f").read_bytes(), b"hello")
        self.assertEqual((self.seat / "f").stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.seat.iterdir()), ["f"])

    def test_snapshot_shape_matches_the_installer(self) -> None:
        from tools.persona_installer import _snapshot

        (self.seat / "f").write_bytes(b"abc")
        (self.seat / "f").chmod(0o640)
        (self.seat / "d").mkdir()
        files = self.files()
        for name in ("f", "d", "absent"):
            with self.subTest(name=name):
                self.assertEqual(files.snapshot(str(self.seat / name)), _snapshot(self.seat / name))


class DiagnosticsTests(_Case):
    def test_identity_check_green_and_red(self) -> None:
        identity = fx.op(
            "host.identity",
            "identity-check",
            fx.ACCOUNT,
            {"account": fx.ACCOUNT, "mailbox_group": fx.MAILBOX_GROUP, "linger": "enabled", "user_manager": "active"},
        )
        result = self.run_ops(identity)
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.evidence[0].evidence["user_manager"], "active")
        (self.fixture.linger_dir / fx.ACCOUNT).unlink()
        self.fixture.in_group = False
        self.fixture.user_manager.reachable = False
        red = self.run_ops(identity).evidence[0]
        self.assertFalse(red.ok)
        for phrase in ("linger", "mailbox group", "user manager"):
            self.assertIn(phrase, red.detail)

    def test_manager_check_uses_only_the_port(self) -> None:
        check = fx.op("host.user-manager", "manager-check", fx.ACCOUNT, {"manager_uid": fx.ACCOUNT})
        self.assertTrue(self.run_ops(check).ok)
        self.assertIn("manager_state", self.fixture.user_manager.calls)
        self.fixture.user_manager.reachable = False
        self.assertFalse(self.run_ops(check).ok)

    def test_nothing_shells_out(self) -> None:
        # gate 39 groundwork: no systemctl --user, no loginctl, no git
        operations = (
            fx.op("host.identity", "identity-check", fx.ACCOUNT,
                  {"account": fx.ACCOUNT, "mailbox_group": fx.MAILBOX_GROUP}),
            fx.op("host.user-manager", "manager-check", fx.ACCOUNT),
            fx.op("framework.source", "source-check", str(self.fixture.framework_root),
                  {"artifacts": ",".join(fx.FRAMEWORK_ARTIFACTS), "artifact_sha256": self.fixture.artifact_digest(),
                   "revision": "test-rev"}),
            fx.op("harness", "harness-check", "codex"),
        )
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess.run")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess.Popen")):
            result = self.run_ops(*operations)
        self.assertTrue(result.ok, result.evidence)

    def test_source_check_digests_the_root_owned_copy_without_git(self) -> None:
        good = fx.op("framework.source", "source-check", str(self.fixture.framework_root),
                     {"artifacts": ",".join(fx.FRAMEWORK_ARTIFACTS), "artifact_sha256": self.fixture.artifact_digest(),
                      "revision": "abc"})
        evidence = self.run_ops(good).evidence[0]
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence["git"], "not-used")
        self.assertEqual(evidence.evidence["revision_proof"], "planner-effect-digest")
        drifted = fx.op("framework.source", "source-check", str(self.fixture.framework_root),
                        {"artifacts": ",".join(fx.FRAMEWORK_ARTIFACTS), "artifact_sha256": "0" * 64})
        self.assertFalse(self.run_ops(drifted).ok)
        escaping = fx.op("framework.source", "source-check", str(self.fixture.framework_root),
                         {"artifacts": "../outside", "artifact_sha256": "0" * 64})
        self.assertFalse(self.run_ops(escaping).ok)

    def test_repo_sourced_targets_never_read_a_seat_checkout(self) -> None:
        checkout = self.seat / "pantheon-config"
        checkout.mkdir()
        (checkout / "rulebook").mkdir()
        for kind, desired in (
            ("source-check", {"artifacts": "x", "artifact_sha256": "0" * 64}),
            ("policy-check", {"bundle": "rulebook/"}),
            ("codex-standing-instructions-source-check", {"source_sha256": "0" * 64}),
        ):
            with self.subTest(kind=kind):
                evidence = self.run_ops(fx.op("repo", kind, str(checkout), desired)).evidence[0]
                self.assertFalse(evidence.ok)
                self.assertIn("SeatPathRefused", evidence.detail)

    def test_policy_check_over_the_framework_copy(self) -> None:
        ok = fx.op("policy", "policy-check", str(self.fixture.framework_root), {"bundle": ",".join(fx.POLICY_BUNDLE)})
        self.assertTrue(self.run_ops(ok).ok)
        missing = fx.op("policy", "policy-check", str(self.fixture.framework_root), {"bundle": "adr/"})
        self.assertFalse(self.run_ops(missing).ok)

    def test_brief_check_framework_and_seat_readability_only(self) -> None:
        framework_brief = fx.op("brief", "brief-check", str(self.fixture.framework_root / "pinned-rules/seat.md"))
        evidence = self.run_ops(framework_brief).evidence[0]
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence["source"], "framework-root")
        (self.seat / "brief.md").write_text("mine", encoding="utf-8")
        seat_brief = fx.op("brief", "brief-check", str(self.seat / "brief.md"))
        evidence = self.run_ops(seat_brief).evidence[0]
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence, {"source": "seat-root", "trust": "readability-only"})
        elsewhere = fx.op("brief", "brief-check", "/etc/hostname")
        self.assertFalse(self.run_ops(elsewhere).ok)

    def test_codex_standing_instruction_checks(self) -> None:
        source = self.fixture.framework_root / "codex/standing.md"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        source_check = fx.op("csi.source", "codex-standing-instructions-source-check", str(source),
                             {"source_sha256": digest, "source_commit": "deadbeef"})
        evidence = self.run_ops(source_check).evidence[0]
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence["commit_proof"], "not-checked-no-git")
        target = self.seat / "AGENTS.md"
        target_check = fx.op("csi.target", "codex-standing-instructions-target-check", str(target), {"sha256": digest})
        self.assertFalse(self.run_ops(target_check).ok)
        target.write_bytes(source.read_bytes())
        self.assertTrue(self.run_ops(target_check).ok)
        override = fx.op("csi.override", "codex-standing-instructions-override-check", str(self.seat / "override.md"))
        self.assertTrue(self.run_ops(override).ok)
        (self.seat / "override.md").write_text("  \n", encoding="utf-8")
        self.assertTrue(self.run_ops(override).ok)
        (self.seat / "override.md").write_text("override!", encoding="utf-8")
        self.assertFalse(self.run_ops(override).ok)

    def test_claude_mcp_check_is_a_read_only_audit(self) -> None:
        config = self.seat / ".claude.json"
        check = fx.op("mcp", "claude-mcp-server-check", str(config),
                      {"name": "pantheon", "scope": "user", "expected": "{}", "home": str(self.seat),
                       "config_path": str(config)})
        evidence = self.run_ops(check).evidence[0]
        self.assertTrue(evidence.ok)
        self.assertEqual(evidence.evidence["verdict"], "absent")
        config.write_text(json.dumps({"mcpServers": {"pantheon": {"command": "x"}}}), encoding="utf-8")
        before = config.read_bytes()
        evidence = self.run_ops(check).evidence[0]
        self.assertEqual(evidence.evidence["verdict"], "conflicting")
        self.assertEqual(config.read_bytes(), before)
        outside = fx.op("mcp", "claude-mcp-server-check", str(config),
                        {"name": "p", "scope": "user", "expected": "{}", "home": "/root"})
        self.assertFalse(self.run_ops(outside).ok)

    def test_timer_enablement_absence_fails_closed_when_present(self) -> None:
        link = self.seat / "timers.target.wants" / "c.timer"
        check = fx.op("timer.absent", "timer-enablement-absence-check", str(link),
                      {"timer": "c.timer", "canonical_template_path": str(self.seat / "c.timer")})
        self.assertTrue(self.run_ops(check).ok)
        link.parent.mkdir()
        link.symlink_to(self.seat / "c.timer")
        evidence = self.run_ops(check).evidence[0]
        self.assertFalse(evidence.ok)
        # T-2917 5c.2: the B9 branch judges a present link through the port;
        # this dangling, non-consume link is not converged legacy state
        self.assertIn("consume timer enablement link already exists", evidence.detail)


class FileApplyTests(_Case):
    def test_mkdir_and_write_converge_and_record_before_snapshots(self) -> None:
        directory = self.seat / ".config"
        target = directory / "unit.service"
        result = self.run_ops(
            fx.op("dir", "mkdir", str(directory), {"mode": "0700"}),
            fx.content_op("unit", "user-unit-install", target, "# Managed by pantheon persona installer\n[Unit]\n"),
        )
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual([m.changed for m in result.mutations], [True, True])
        self.assertEqual(result.mutations[0].before, {"state": "absent"})
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        again = self.run_ops(
            fx.op("dir", "mkdir", str(directory), {"mode": "0700"}),
            fx.content_op("unit", "user-unit-install", target, "# Managed by pantheon persona installer\n[Unit]\n"),
        )
        self.assertEqual([m.changed for m in again.mutations], [False, False])

    def test_unknown_file_is_never_overwritten(self) -> None:
        target = self.seat / "notes.txt"
        target.write_text("user data", encoding="utf-8")
        result = self.run_ops(fx.content_op("w", "atomic-write", target, "replacement"))
        self.assertFalse(result.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "user data")

    def test_managed_file_converges_with_receipt_snapshot(self) -> None:
        target = self.seat / "managed.json"
        target.write_text(json.dumps({"managed_by": "pantheon-persona-installer", "v": 1}), encoding="utf-8")
        old = target.read_bytes()
        result = self.run_ops(fx.content_op("w", "atomic-write", target, '{"managed_by":"pantheon-persona-installer","v":2}'))
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.mutations[0].before["fingerprint"], hashlib.sha256(old).hexdigest())

    def test_hash_pin_mismatch_is_refused(self) -> None:
        bad = fx.op("w", "codex-standing-instructions-install", str(self.seat / "AGENTS.md"),
                    {"content": "x", "sha256": "0" * 64, "mode": "0644"})
        result = self.run_ops(bad)
        self.assertFalse(result.ok)
        self.assertFalse((self.seat / "AGENTS.md").exists())

    def test_apply_stops_at_the_first_failure_and_keeps_prior_mutations(self) -> None:
        result = self.run_ops(
            fx.op("dir", "mkdir", str(self.seat / "a"), {"mode": "0755"}),
            fx.op("bad", "mkdir", "/etc/should-not", {"mode": "0755"}),
            fx.op("never", "mkdir", str(self.seat / "b"), {"mode": "0755"}),
        )
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], ["dir"])
        self.assertEqual(len(result.evidence), 2)
        self.assertFalse((self.seat / "b").exists())

    def test_verify_is_read_only(self) -> None:
        directory = self.seat / "d"
        target = self.seat / "f"
        operations = (
            fx.op("dir", "mkdir", str(directory), {"mode": "0755"}),
            fx.content_op("file", "atomic-write", target, "# Managed by pantheon persona installer\n"),
            fx.op("harness", "harness-check", "codex"),
            fx.op("staged", "timer-enablement-absence-check", str(self.seat / "w" / "t"), scope="staged-cutover"),
        )
        missing = self.run_ops(*operations, kind="verify")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.mutations, ())
        self.assertFalse(directory.exists())
        self.assertFalse(target.exists())
        # staged-cutover scope is apply-time only (installer parity)
        self.assertEqual([e.operation_id for e in missing.evidence], ["dir", "file", "harness"])
        self.assertTrue(self.run_ops(*operations[:3]).ok)
        verified = self.run_ops(*operations, kind="verify")
        self.assertTrue(verified.ok, verified.evidence)
        self.assertEqual(verified.mutations, ())


class FifoReadTests(_Case):
    """Review M1: a seat-uid FIFO planted at a read target must be refused
    at once, never block the executor worker (which would leave it Busy
    until restart)."""

    def _bounded(self, work):
        import threading

        outcome: dict = {}

        def run():
            try:
                outcome["value"] = work()
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(20)
        self.assertFalse(thread.is_alive(), "read blocked on a FIFO")
        return outcome

    def test_read_bytes_refuses_a_fifo_without_blocking(self) -> None:
        fifo = self.seat / "AGENTS.md"
        os.mkfifo(fifo)
        files = SeatFiles(seat_root=self.seat, owner_uid=os.getuid())
        outcome = self._bounded(lambda: files.read_bytes(str(fifo)))
        self.assertIsInstance(outcome.get("error"), SeatPathRefused)
        outcome = self._bounded(lambda: files.snapshot(str(fifo)))
        self.assertIsInstance(outcome.get("error"), SeatPathRefused)

    def test_verify_reads_of_a_fifo_fail_the_check_and_return(self) -> None:
        fifo = self.seat / "planted"
        os.mkfifo(fifo)
        operations = (
            fx.op("csi.target", "codex-standing-instructions-target-check", str(fifo), {"sha256": "0" * 64}),
            fx.op("csi.override", "codex-standing-instructions-override-check", str(fifo)),
            fx.op("mcp", "claude-mcp-server-check", str(fifo), {"name": "p", "scope": "user", "expected": "{}"}),
            fx.op("brief", "brief-check", str(fifo)),
            fx.content_op("file", "atomic-write", fifo, "x"),
        )
        outcome = self._bounded(lambda: self.run_ops(*operations, kind="verify"))
        result = outcome["value"]
        self.assertEqual(len(result.evidence), len(operations))
        self.assertTrue(all(not item.ok for item in result.evidence))


class PostCommitFailureTests(_Case):
    """Review M2: once the rename or mkdir committed, a later failure must
    not drop the mutation (the receipt drives rollback)."""

    def _fail_on_directory(self, real):
        import stat as stat_module

        def wrapper(fd, *args):
            if stat_module.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "injected directory failure")
            return real(fd, *args)

        return wrapper

    def assert_kept(self, result, operation_id: str, target: Path) -> None:
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], [operation_id])
        mutation = result.mutations[0]
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.before, {"state": "absent"})
        self.assertFalse(result.evidence[-1].ok)
        self.assertIn("changed, then failed", result.evidence[-1].detail)
        self.assertTrue(target.exists())

    def test_parent_fsync_failure_after_rename_keeps_the_mutation(self) -> None:
        from tools.host_control.executor import seat_files as module

        target = self.seat / "f"
        with mock.patch.object(module.os, "fsync", side_effect=self._fail_on_directory(os.fsync)):
            result = self.run_ops(fx.content_op("w", "atomic-write", target, "# Managed by pantheon persona installer\n"))
        self.assert_kept(result, "w", target)
        self.assertIsNotNone(result.mutations[0].after_fingerprint)

    def test_chmod_failure_after_mkdir_keeps_the_mutation(self) -> None:
        from tools.host_control.executor import seat_files as module

        target = self.seat / "d"
        with mock.patch.object(module.os, "fchmod", side_effect=self._fail_on_directory(os.fchmod)):
            result = self.run_ops(fx.op("d", "mkdir", str(target), {"mode": "0755"}))
        self.assert_kept(result, "d", target)

    def test_after_snapshot_failure_keeps_the_mutation(self) -> None:
        real = SeatFiles.snapshot
        calls = {"n": 0}

        def flaky(files, path):
            calls["n"] += 1
            if calls["n"] == 2:  # 1 = before, 2 = after
                raise SeatPathRefused("injected after-snapshot failure")
            return real(files, path)

        target = self.seat / "d2"
        with mock.patch.object(SeatFiles, "snapshot", flaky):
            result = self.run_ops(fx.op("d2", "mkdir", str(target), {"mode": "0755"}))
        self.assert_kept(result, "d2", target)
        self.assertIsNone(result.mutations[0].after_fingerprint)

    def test_pre_commit_failure_records_no_mutation(self) -> None:
        from tools.host_control.executor import seat_files as module

        target = self.seat / "f"
        with mock.patch.object(module.os, "replace", side_effect=OSError(5, "injected")):
            result = self.run_ops(fx.content_op("w", "atomic-write", target, "x"))
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertFalse(target.exists())
        self.assertEqual(sorted(p.name for p in self.seat.iterdir()), [])


class ResultWireTests(_Case):
    def test_result_round_trip_and_binding(self) -> None:
        result = self.run_ops(fx.op("dir", "mkdir", str(self.seat / "d"), {"mode": "0755"}))
        raw = encode_result(operation_handle=fx.OPERATION_HANDLE, spec_digest="cd" * 32, result=result)
        kinds = {"dir": "mkdir"}
        decoded = decode_result(raw, operation_handle=fx.OPERATION_HANDLE, spec_digest="cd" * 32, operation_kinds=kinds)
        self.assertEqual(decoded.mutations, result.mutations)
        self.assertEqual(decoded.evidence, result.evidence)
        for kwargs in (
            {"operation_handle": "00" * 32, "spec_digest": "cd" * 32, "operation_kinds": kinds},
            {"operation_handle": fx.OPERATION_HANDLE, "spec_digest": "00" * 32, "operation_kinds": kinds},
            {"operation_handle": fx.OPERATION_HANDLE, "spec_digest": "cd" * 32, "operation_kinds": {"dir": "atomic-write"}},
            {"operation_handle": fx.OPERATION_HANDLE, "spec_digest": "cd" * 32, "operation_kinds": {}},
        ):
            with self.subTest(kwargs=sorted(kwargs)), self.assertRaises(Exception):
                decode_result(raw, **kwargs)

    def test_oversize_result_degrades_evidence_but_never_drops_mutations(self) -> None:
        from tools.persona_installer import MutationRecord, OperationEvidence

        mutation = MutationRecord("w", "atomic-write", str(self.seat / "f"), fx.ACCOUNT, True, {"state": "absent"}, "a" * 64)
        huge = ExecutionResult(
            ok=True,
            mutations=(mutation,),
            evidence=tuple(OperationEvidence(f"e{i}", 1, "mkdir", True, "x" * 200, {"k": "v" * 200}) for i in range(4000)),
            detail="",
        )
        raw = encode_result(operation_handle=fx.OPERATION_HANDLE, spec_digest="cd" * 32, result=huge)
        self.assertLessEqual(len(raw.encode("utf-8")), 1024 * 1024)
        value = json.loads(raw)
        self.assertFalse(value["ok"])
        self.assertEqual(len(value["mutations"]), 1)
        self.assertIn("mutations kept", value["detail"])
        self.assertTrue(all(item["detail"] == "" and not item["ok"] for item in value["evidence"]))
        # even more evidence: the evidence list goes, the mutation stays
        huger = ExecutionResult(
            ok=True,
            mutations=(mutation,),
            evidence=tuple(OperationEvidence(f"e{i}" + "y" * 100, 1, "mkdir", True, "x", {}) for i in range(9000)),
            detail="",
        )
        value = json.loads(encode_result(operation_handle=fx.OPERATION_HANDLE, spec_digest="cd" * 32, result=huger))
        self.assertEqual((len(value["mutations"]), value["evidence"], value["ok"]), (1, [], False))


class ResultBudgetTests(_Case):
    """Review R1: replays the reviewer's two-500-KiB probe."""

    MARK = "# Managed by pantheon persona installer\n"

    def test_second_large_replacement_is_refused_before_it_commits(self) -> None:
        big = self.MARK + ("A" * (500 * 1024))
        for name in ("one", "two"):
            (self.seat / name).write_text(big, encoding="utf-8")
        result = self.run_ops(
            fx.content_op("w1", "atomic-write", self.seat / "one", self.MARK + "new1\n"),
            fx.content_op("w2", "atomic-write", self.seat / "two", self.MARK + "new2\n"),
        )
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], ["w1"])
        self.assertEqual(result.mutations[0].before["state"], "file")
        self.assertIn("ResultBudgetExceeded", result.evidence[-1].detail)
        # first replaced, second untouched (refused pre-commit)
        self.assertTrue((self.seat / "one").read_text(encoding="utf-8").endswith("new1\n"))
        self.assertEqual((self.seat / "two").read_text(encoding="utf-8"), big)
        raw = encode_result(operation_handle=fx.OPERATION_HANDLE, spec_digest="cd" * 32, result=result)
        encoded = json.loads(raw)
        self.assertEqual([m["operation_id"] for m in encoded["mutations"]], ["w1"])
        self.assertLess(len(raw.encode("utf-8")), 1024 * 1024)
        # the broker receipt carrying these records stays under its CHECK
        self._assert_broker_receipt_fits(result)

    def _assert_broker_receipt_fits(self, result) -> None:
        from tools import test_host_control_dbus_driver as broker_fixture
        from tools.host_control.contracts import BindOutcome

        with tempfile.TemporaryDirectory() as temp:
            domain = broker_fixture.build_domain(Path(temp) / "d", subject_uid=1003)
            try:
                plan = domain.plan_service.plan(1003, "recovery", "manifest-r1", "r1-plan")
                service = broker_fixture._seed_service(domain, 1 << 45)
                record, _ = service.prepare_apply(1003, plan.plan_handle, plan.plan_digest, "r1", "ab" * 32)
                service.authorize(record.operation_handle)
                service.dispatch(record.operation_handle, broker_fixture._SeedExecutor())
                final = service.append_result(
                    record.operation_handle,
                    ok=False,
                    mutations=result.mutations,
                    evidence=result.evidence,
                    detail=result.detail,
                )
                self.assertEqual(final.state, "failed")
                import sqlite3

                with sqlite3.connect(domain.root / "broker.sqlite3") as connection:
                    size = connection.execute(
                        "SELECT length(receipt_json) FROM operation_receipts WHERE operation_handle=? AND phase='failed'",
                        (record.operation_handle,),
                    ).fetchone()[0]
                self.assertLess(size, 1024 * 1024)
            finally:
                domain.close()

    def test_tiny_budget_refuses_the_first_change_before_it_commits(self) -> None:
        tiny = self.fixture.engine(record_budget=300)
        result = self.run_ops(
            fx.op("d1", "mkdir", str(self.seat / "d1"), {"mode": "0755"}),
            fx.op("d2", "mkdir", str(self.seat / "d2"), {"mode": "0755"}),
            engine=tiny,
        )
        # the 4 KiB evidence reserve refuses d1 before it commits
        self.assertEqual(result.mutations, ())
        self.assertFalse((self.seat / "d1").exists())
        self.assertFalse(result.ok)


class CatchAllTests(_Case):
    """Review R2: a non-HostControl exception neither escapes run() nor
    drops an earlier committed mutation."""

    def test_type_error_in_a_later_operation_keeps_the_earlier_mutation(self) -> None:
        real = engine_module.ExecutorEngine._dispatch
        calls = {"n": 0}

        def flaky(self_engine, operation, *, verify):
            calls["n"] += 1
            if calls["n"] == 2:
                raise TypeError("injected")
            return real(self_engine, operation, verify=verify)

        with mock.patch.object(engine_module.ExecutorEngine, "_dispatch", flaky):
            result = self.run_ops(
                fx.op("d", "mkdir", str(self.seat / "d"), {"mode": "0755"}),
                fx.op("d2", "mkdir", str(self.seat / "d2"), {"mode": "0755"}),
            )
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], ["d"])
        self.assertIn("TypeError", result.evidence[-1].detail)

    def test_type_error_after_commit_keeps_the_mutation(self) -> None:
        with mock.patch.object(engine_module, "_snapshot_fingerprint", side_effect=TypeError("injected")):
            result = self.run_ops(fx.op("d", "mkdir", str(self.seat / "d"), {"mode": "0755"}))
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], ["d"])
        self.assertTrue(result.mutations[0].changed)
        self.assertTrue((self.seat / "d").is_dir())

    # -- T-2917 5d.1, carry-in N3: the error path itself never drops one --

    def test_n3_unprintable_exception_keeps_the_committed_mutation(self) -> None:
        class Unprintable(RuntimeError):
            def __str__(self) -> str:
                raise ValueError("__str__ exploded")

        real = engine_module.ExecutorEngine._dispatch
        calls = {"n": 0}

        def flaky(self_engine, operation, *, verify):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Unprintable()
            return real(self_engine, operation, verify=verify)

        with mock.patch.object(engine_module.ExecutorEngine, "_dispatch", flaky):
            result = self.run_ops(
                fx.op("d", "mkdir", str(self.seat / "d"), {"mode": "0755"}),
                fx.op("d2", "mkdir", str(self.seat / "d2"), {"mode": "0755"}),
            )
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], ["d"])
        self.assertIn("Unprintable", result.evidence[-1].detail)
        self.assertIn("unprintable", result.evidence[-1].detail)
        self.assertTrue((self.seat / "d").is_dir())

    def test_n3_record_size_failure_keeps_the_committed_mutation(self) -> None:
        real = engine_module.record_size

        def sizing(mutation, evidence):
            # the pre-commit projection (no evidence yet) still works; the
            # run loop's sizing of the committed record fails
            if evidence is not None:
                raise TypeError("injected sizing failure")
            return real(mutation, evidence)

        with mock.patch.object(engine_module, "record_size", side_effect=sizing):
            result = self.run_ops(
                fx.op("d", "mkdir", str(self.seat / "d"), {"mode": "0755"}),
                fx.op("d2", "mkdir", str(self.seat / "d2"), {"mode": "0755"}),
            )
        self.assertEqual([m.operation_id for m in result.mutations], ["d", "d2"])
        self.assertTrue(all(m.changed for m in result.mutations))
        # and the result still encodes with every mutation
        raw = encode_result(operation_handle=fx.OPERATION_HANDLE, spec_digest="cd" * 32, result=result)
        self.assertEqual([m["operation_id"] for m in json.loads(raw)["mutations"]], ["d", "d2"])


class ParseSpecBoundsTests(_Case):
    """T-2917 5d.1, carry-in N1: the executor's spec parse applies the
    broker record decoders' bounds through the SAME predicate."""

    def parse_ops(self, *operations):
        return fx.parsed(self.fixture, tuple(operations))

    def test_n1_overlong_operation_id_is_invalid_spec_before_any_run(self) -> None:
        target = self.seat / "made"
        with self.assertRaises(InvalidSpec):
            self.parse_ops(fx.op("x" * 129, "mkdir", str(target), {"mode": "0755"}))
        self.assertFalse(target.exists())
        # the boundary itself parses (and runs)
        result = self.fixture.engine().run(self.parse_ops(fx.op("x" * 128, "mkdir", str(target), {"mode": "0755"})))
        self.assertTrue(result.ok, result.evidence)

    def test_n1_target_over_1024_is_invalid_spec(self) -> None:
        long_target = str(self.seat) + "/" + "a" * (1025 - len(str(self.seat)) - 1 + 1)
        self.assertGreater(len(long_target), 1024)
        with self.assertRaises(InvalidSpec):
            self.parse_ops(fx.op("w", "mkdir", long_target, {"mode": "0755"}))

    def test_n1_parse_bounds_equal_the_broker_decoder(self) -> None:
        from tools.host_control.contracts import HostControlError, _mutation_from_dict

        base = fx.spec_payload(self.fixture, (fx.op("id", "mkdir", str(self.seat / "d"), {"mode": "0755"}),))["operations"][0]
        for field, lengths in (
            ("operation_id", (1, 127, 128, 129, 300)),
            ("kind", (1, 127, 128, 129)),
            ("owner", (1, 128, 129)),
            ("target", (1, 1023, 1024, 1025, 2000)),
        ):
            for length in lengths:
                with self.subTest(field=field, length=length):
                    raw = dict(base, **{field: "a" * length})
                    try:
                        engine_module._operation(raw)
                        executor_accepts = True
                    except InvalidSpec:
                        executor_accepts = False
                    mutation = {
                        "operation_id": raw["operation_id"],
                        "kind": raw["kind"],
                        "target": raw["target"],
                        "owner": raw["owner"],
                        "changed": True,
                        "before": {},
                        "after_fingerprint": None,
                    }
                    try:
                        _mutation_from_dict(mutation)
                        broker_accepts = True
                    except HostControlError:
                        broker_accepts = False
                    self.assertEqual(executor_accepts, broker_accepts)


if __name__ == "__main__":  # must stay LAST
    unittest.main()
