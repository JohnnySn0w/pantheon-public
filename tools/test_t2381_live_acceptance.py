"""Safety tests for the disposable T-2381 live-acceptance tooling."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools.t2381_live_acceptance import (
    FIXTURE_ACCOUNT,
    FIXTURE_PERSONA,
    FIXTURE_PROBE_PEER,
    FixtureRefused,
    assert_push_incapable,
    check_cleanup_markers,
    marker_document,
    prepare_fixture,
    probe_mailbox_write,
    write_marker,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "tools" / "run_t2381_live_acceptance.sh"


class FixtureSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fixture_persona_cannot_alias_cairn(self) -> None:
        with self.assertRaisesRegex(FixtureRefused, "exactly"):
            marker_document(FIXTURE_ACCOUNT, "cairn", "20260802T000000Z")

    def test_cleanup_refuses_missing_or_mismatched_markers(self) -> None:
        controller = self.root / "controller"
        mailbox = self.root / "mailbox"
        with self.assertRaisesRegex(FixtureRefused, "missing"):
            check_cleanup_markers(
                controller,
                mailbox,
                FIXTURE_ACCOUNT,
                FIXTURE_PERSONA,
                require_root_owner=False,
            )
        write_marker(
            controller,
            FIXTURE_ACCOUNT,
            FIXTURE_PERSONA,
            "20260802T000000Z",
            require_root=False,
        )
        write_marker(
            mailbox,
            FIXTURE_ACCOUNT,
            FIXTURE_PERSONA,
            "20260802T000001Z",
            require_root=False,
        )
        with self.assertRaisesRegex(FixtureRefused, "differ"):
            check_cleanup_markers(
                controller,
                mailbox,
                FIXTURE_ACCOUNT,
                FIXTURE_PERSONA,
                require_root_owner=False,
            )

    def test_cleanup_accepts_only_exact_equal_markers(self) -> None:
        controller = self.root / "controller"
        mailbox = self.root / "mailbox"
        for path in (controller, mailbox):
            write_marker(
                path,
                FIXTURE_ACCOUNT,
                FIXTURE_PERSONA,
                "20260802T000000Z",
                require_root=False,
            )
        value = check_cleanup_markers(
            controller,
            mailbox,
            FIXTURE_ACCOUNT,
            FIXTURE_PERSONA,
            require_root_owner=False,
        )
        self.assertEqual(value["schema"], "pantheon-t2381-live-fixture/v1")

    def test_push_incapable_requires_no_remote_and_no_helper(self) -> None:
        repo = self.root / "repo"
        subprocess.run(("git", "init", str(repo)), check=True, capture_output=True)
        subprocess.run(
            ("git", "-C", str(repo), "remote", "add", "origin", "/tmp/nowhere"),
            check=True,
        )
        with self.assertRaisesRegex(FixtureRefused, "remotes"):
            assert_push_incapable(repo)
        subprocess.run(("git", "-C", str(repo), "remote", "remove", "origin"), check=True)
        subprocess.run(
            ("git", "-C", str(repo), "config", "credential.helper", "store"),
            check=True,
        )
        with self.assertRaisesRegex(FixtureRefused, "credential helper"):
            assert_push_incapable(repo)
        subprocess.run(
            ("git", "-C", str(repo), "config", "--unset-all", "credential.helper"),
            check=True,
        )
        self.assertEqual(assert_push_incapable(repo)["push_capable"], "false")

    def test_prepare_keeps_source_manifest_bytes_unchanged(self) -> None:
        home = self.root / FIXTURE_ACCOUNT
        repo = home / "pantheon-config"
        (repo / "tools/systemd").mkdir(parents=True)
        (repo / "pinned-rules").mkdir()
        source = repo / "instance.yaml"
        source.write_text(
            yaml.safe_dump(
                {
                    "manifest_version": 2,
                    "persona_installer": {
                        "schema": "pantheon-persona-install/v1",
                        "mailbox": {
                            "hub": "/home/the-ancestor/mailbox",
                            "group": "pantheon-mailbox",
                            "probe_peer": "orchestrator",
                        },
                        "security": {
                            "credential_mode": "references-only",
                            "prohibit_auth_copy": True,
                        },
                    },
                    "instance": {"id": "source"},
                    "roles": {
                        "recovery": {
                            "persona": "recovery",
                            "account": "claude-fallback",
                            "harness": "claude-code",
                        }
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        service = repo / "tools/systemd/pantheon-persona-consume@.service"
        service.write_text(
            "[Service]\n"
            "Environment=PANTHEON_INSTANCE_MANIFEST=%h/pantheon-config/instance.yaml\n",
            encoding="utf-8",
        )
        subprocess.run(("git", "init", str(repo)), check=True, capture_output=True)
        before = source.read_bytes()
        fixture = repo / ".t2381-fixture/instance.yaml"
        fake_pwd = mock.Mock(pw_dir=str(home))
        with mock.patch("tools.t2381_live_acceptance.pwd.getpwnam", return_value=fake_pwd):
            evidence = prepare_fixture(
                repo,
                source,
                fixture,
                FIXTURE_PERSONA,
                FIXTURE_ACCOUNT,
            )
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(evidence["source_manifest_unchanged"], "true")
        fixture_doc = yaml.safe_load(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            fixture_doc["roles"]["t2381-fixture-role"]["persona"],
            FIXTURE_PERSONA,
        )
        self.assertEqual(
            fixture_doc["persona_installer"]["mailbox"]["probe_peer"],
            FIXTURE_PROBE_PEER,
        )
        self.assertEqual(evidence["probe_peer"], FIXTURE_PROBE_PEER)

    def test_mailbox_write_probe_can_fail_then_pass(self) -> None:
        peer_tmp = self.root / "peer" / "tmp"
        peer_tmp.mkdir(parents=True)
        peer_tmp.chmod(0o000)
        try:
            with self.assertRaisesRegex(
                FixtureRefused,
                rf"direction=sibling-send path={peer_tmp}",
            ) as caught:
                probe_mailbox_write(peer_tmp, "sibling-send")
            print(f"negative write control observed: {caught.exception}")
        finally:
            peer_tmp.chmod(0o700)
        evidence = probe_mailbox_write(peer_tmp, "sibling-send")
        self.assertEqual(evidence["durable_round_trip"], "true")
        self.assertEqual(list(peer_tmp.iterdir()), [])

    def test_wrapper_cleanup_order_and_no_wildcard_target(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        mailbox_marker = text.index('--path "$MAILBOX_MARKER"')
        peer_marker = text.index('--path "$PEER_MAILBOX_MARKER"')
        useradd = text.index("useradd --create-home")
        disable_linger = text.index('loginctl disable-linger "$ACCOUNT"')
        stop_manager = text.index('systemctl stop "user@${uid}.service"')
        userdel = text.index('userdel --remove "$ACCOUNT"')
        self.assertLess(mailbox_marker, useradd)
        self.assertLess(peer_marker, useradd)
        self.assertLess(disable_linger, stop_manager)
        self.assertLess(stop_manager, userdel)
        self.assertIn('rm -rf --one-file-system -- "$MAILBOX_DIR"', text)
        self.assertIn('rm -rf --one-file-system -- "$PEER_MAILBOX_DIR"', text)
        self.assertNotIn('mailbox/t2381-*', text)
        self.assertNotIn('--groups "pantheon-shared"', text)

    def test_source_guards_use_owner_and_pin_reviewed_head(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("runuser -u codex -- env", text)
        self.assertIn('source_head="$(run_codex_git rev-parse --verify HEAD)"', text)
        self.assertIn('source_head" == "$REVIEWED_REVISION', text)
        self.assertIn("codex could not evaluate the source checkout HEAD", text)
        self.assertNotIn("safe.directory", text)
        self.assertNotIn('git -C "$SOURCE_REPO" branch', text)

    def test_cross_owner_clone_uses_pinned_bundle(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn('run_codex_git bundle create - HEAD >"$SOURCE_BUNDLE"', text)
        self.assertIn(
            'run_fixture git clone --no-hardlinks "$SOURCE_BUNDLE" "$REPO_DIR"',
            text,
        )
        self.assertNotIn(
            'run_fixture git clone --no-hardlinks "$SOURCE_REPO" "$REPO_DIR"',
            text,
        )

    def test_peer_acl_negative_control_precedes_grant_and_positive_probe(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        negative = text.index(
            'probe_fixture_mailbox_write sibling-send "$PEER_MAILBOX_DIR/tmp"'
        )
        grant = text.index("  grant_fixture_peer_acl\n")
        positive = text.index(
            'probe_fixture_mailbox_write sibling-send "$PEER_MAILBOX_DIR/tmp"',
            negative + 1,
        )
        self.assertLess(negative, grant)
        self.assertLess(grant, positive)
        self.assertIn('"u:${ACCOUNT}:rwx,g::---,m::rwx,o::---"', text)
        self.assertIn('"u::rwx,u:${ACCOUNT}:rwx,g::---,m::rwx,o::---"', text)
        self.assertIn("durable-peer-acl-reference.txt", text)
        self.assertIn(
            'PEER_MAILBOX_DIR="/home/the-ancestor/mailbox/t2381-fixture-peer"',
            text,
        )

    def test_write_marker_is_exclusive(self) -> None:
        path = self.root / "marker"
        write_marker(
            path,
            FIXTURE_ACCOUNT,
            FIXTURE_PERSONA,
            "20260802T000000Z",
            require_root=False,
        )
        first = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            write_marker(
                path,
                FIXTURE_ACCOUNT,
                FIXTURE_PERSONA,
                "20260802T000000Z",
                require_root=False,
            )
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), first)


if __name__ == "__main__":
    unittest.main()
