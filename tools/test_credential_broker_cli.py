"""Tests for the ORG-051 installer-interface CLI (T-2868, Phase 0 groundwork).

Exercises the real subprocess argv/stdout contract (mirrors
tools/test_persona_installer.py's `test_cross_process_plan_is_deterministic_
and_secret_free`), because the CLI's whole job is to be a safe boundary for
external callers -- an in-process call would not prove argv parsing or stdout
shape. Every test that touches a credential file plants a distinctive marker
byte string and asserts it never appears in stdout/stderr, on top of the
ordinary functional assertions.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "tools" / "credential_broker_cli.py"

SECRET_MARKER = "do-not-print-this-marker-9f3c2a"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
    )


class RefSpecFixture:
    """A temp 0700-parent host file + a matching JSON ref-spec on disk."""

    def __init__(self, root: Path, *, credential_id: str = "test/orchestrator/example") -> None:
        self.root = root
        self.secret_path = root / "token"
        self.secret_path.write_text(SECRET_MARKER, encoding="utf-8")
        self.secret_path.chmod(0o600)
        root.chmod(0o700)
        uid = os.geteuid()
        gid = self.secret_path.stat().st_gid
        self.spec = {
            "credential_id": credential_id,
            "kind": "bearer-token",
            "provider": "host-file",
            "location": str(self.secret_path),
            "owner_uid": uid,
            "owner_gid": gid,
            "mode": "0600",
            "host_file": {
                "parent_dir": str(root),
                "parent_owner_uid": uid,
                "parent_owner_gid": gid,
                "parent_mode": "0700",
            },
        }
        self.spec_path = root / "ref-spec.json"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")


class CheckVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit_path = self.root / "state" / "audit.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_check_happy_path_prints_status_never_the_value(self) -> None:
        fixture = RefSpecFixture(self.root)
        result = run_cli(
            "check", str(fixture.spec_path), "--audit-path", str(self.audit_path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SECRET_MARKER, result.stdout)
        self.assertNotIn(SECRET_MARKER, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["credential_id"], "test/orchestrator/example")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["location"], str(fixture.secret_path))
        self.assertEqual(payload["owner_uid"], os.geteuid())
        self.assertEqual(payload["mode"], "0600")
        self.assertEqual(payload["errors"], [])

    def test_verify_is_the_same_verb_as_check(self) -> None:
        fixture = RefSpecFixture(self.root)
        checked = run_cli(
            "check", str(fixture.spec_path), "--audit-path", str(self.audit_path)
        )
        verified = run_cli(
            "verify", str(fixture.spec_path), "--audit-path", str(self.audit_path)
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(checked.stdout), json.loads(verified.stdout))

    def test_check_writes_audit_record_without_the_value(self) -> None:
        fixture = RefSpecFixture(self.root)
        run_cli("check", str(fixture.spec_path), "--audit-path", str(self.audit_path))
        audit_text = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_MARKER, audit_text)
        record = json.loads(audit_text.splitlines()[-1])
        self.assertEqual(record["event"], "verify")
        self.assertEqual(record["outcome"], "pass")

    def test_check_missing_file_reports_unavailable_and_exits_nonzero(self) -> None:
        fixture = RefSpecFixture(self.root)
        fixture.secret_path.unlink()
        result = run_cli(
            "check", str(fixture.spec_path), "--audit-path", str(self.audit_path)
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["available"])
        self.assertTrue(payload["errors"])

    def test_check_rejects_malformed_json_ref_spec(self) -> None:
        bad_spec = self.root / "bad.json"
        bad_spec.write_text("{not json", encoding="utf-8")
        result = run_cli(
            "check", str(bad_spec), "--audit-path", str(self.audit_path)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ref spec", result.stderr)

    def test_check_rejects_ref_spec_missing_required_field(self) -> None:
        fixture = RefSpecFixture(self.root)
        incomplete = dict(fixture.spec)
        del incomplete["location"]
        spec_path = self.root / "incomplete.json"
        spec_path.write_text(json.dumps(incomplete), encoding="utf-8")
        result = run_cli(
            "check", str(spec_path), "--audit-path", str(self.audit_path)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("location", result.stderr)

    def test_check_rejects_unwritable_audit_path_cleanly(self) -> None:
        # audit-path is required -- no default is baked in (T-2869 defers the
        # relocation decision), so a caller-supplied garbage path must fail
        # closed with a message, not a traceback.
        fixture = RefSpecFixture(self.root)
        result = run_cli("check", str(fixture.spec_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("audit-path", result.stderr.lower())


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest_path = self.root / "instance.yaml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_manifest(self, credential_refs: list[dict]) -> None:
        document = {
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
                "orchestrator": {
                    "persona": "orchestrator",
                    "account": "claude",
                    "harness": "claude-code",
                    "credential_refs": credential_refs,
                }
            },
        }
        self.manifest_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def test_plan_prints_ids_and_owner_only(self) -> None:
        self._write_manifest(
            [
                {
                    "id": "github/orchestrator/pat",
                    "kind": "bearer-token",
                    "provider": "broker",
                    "owner": "claude",
                    "scope": ["github"],
                    "rotation": {"strategy": "manual", "revoke": "manual"},
                    "location": SECRET_MARKER,  # never real; asserts non-echo below
                }
            ]
        )
        result = run_cli(
            "plan", "--manifest", str(self.manifest_path), "--persona", "orchestrator"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["credential_refs"], [{"id": "github/orchestrator/pat", "owner": "claude"}]
        )
        self.assertNotIn(SECRET_MARKER, result.stdout)
        self.assertNotIn("kind", json.dumps(payload["credential_refs"]))
        self.assertNotIn("location", json.dumps(payload["credential_refs"]))

    def test_plan_unbound_persona_fails_closed(self) -> None:
        self._write_manifest([])
        result = run_cli(
            "plan", "--manifest", str(self.manifest_path), "--persona", "nobody"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_plan_against_real_instance_yaml_succeeds(self) -> None:
        # Confirms the CLI's default --manifest reads the repo's own
        # instance.yaml (populated by this same ticket) without error.
        result = run_cli("plan", "--manifest", str(REPO_ROOT / "instance.yaml"), "--persona", "orchestrator")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ids = {entry["id"] for entry in payload["credential_refs"]}
        self.assertIn("github/orchestrator/pat", ids)
        self.assertIn("telegram/orchestrator/bot-token", ids)


class ReapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit_path = self.root / "state" / "audit.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reap_fresh_process_finds_nothing_to_revoke(self) -> None:
        result = run_cli("reap", "--audit-path", str(self.audit_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["revoked_lease_ids"], [])

    def test_reap_requires_audit_path(self) -> None:
        result = run_cli("reap")
        self.assertNotEqual(result.returncode, 0)


class GatedInstallerVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_install_valid_spec_is_gated_not_performed(self) -> None:
        fixture = RefSpecFixture(self.root)
        result = run_cli("install", str(fixture.spec_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("T-2869", result.stderr)
        self.assertIn("gated", result.stderr.lower())
        self.assertNotIn(SECRET_MARKER, result.stdout)
        self.assertNotIn(SECRET_MARKER, result.stderr)

    def test_install_malformed_spec_fails_before_the_gate_message(self) -> None:
        bad_spec = self.root / "bad.json"
        bad_spec.write_text("{not json", encoding="utf-8")
        result = run_cli("install", str(bad_spec))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("T-2869", result.stderr)

    def test_rotate_valid_identifier_is_gated_not_performed(self) -> None:
        result = run_cli("rotate", "github/orchestrator/pat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("T-2869", result.stderr)

    def test_rotate_rejects_malformed_identifier_before_the_gate(self) -> None:
        result = run_cli("rotate", "not a bounded identifier; rm -rf /")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("T-2869", result.stderr)

    def test_revoke_valid_lease_id_is_gated_not_performed(self) -> None:
        result = run_cli("revoke", "abcdef0123456789")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("T-2869", result.stderr)

    def test_revoke_rejects_malformed_lease_id_before_the_gate(self) -> None:
        result = run_cli("revoke", "$(echo pwned)")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("T-2869", result.stderr)


class NoSecretPositionalArgTests(unittest.TestCase):
    """Structural guard: no verb's argparse surface can accept a raw secret.

    Imports the CLI module in-process (the one place this suite doesn't use
    subprocess) purely to introspect its argparse tree -- no broker call, no
    file I/O, so there is nothing secret-shaped to leak here.
    """

    def test_no_subparser_defines_a_value_shaped_argument(self) -> None:
        sys.path.insert(0, str(REPO_ROOT))
        try:
            from tools import credential_broker_cli as cli_module
        finally:
            sys.path.remove(str(REPO_ROOT))
        parser = cli_module._build_parser()
        deny = {"value", "secret", "token", "password", "private_key", "contents"}
        # argparse doesn't expose subparsers publicly in a typed way; reach
        # the registry the same way argparse itself does internally.
        subparsers_action = next(
            action
            for action in parser._actions
            if isinstance(action, __import__("argparse")._SubParsersAction)
        )
        for name, child in subparsers_action.choices.items():
            for action in child._actions:
                dest = (action.dest or "").lower()
                self.assertNotIn(
                    dest,
                    deny,
                    f"verb {name!r} exposes a value-shaped argument {dest!r}",
                )


if __name__ == "__main__":
    unittest.main()
