"""Credential broker contract tests (ORG-051, T-2380)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.credential_broker import (
    CredentialPolicyError,
    CredentialRef,
    CredentialValidationError,
    HostFileConstraints,
    HostFileCredentialBroker,
    LeaseRequest,
    ProviderCapabilities,
    SecretSource,
)


TEST_CONSUMER_FEATURE_FLAG = "PANTHEON_TEST_CREDENTIAL_CONSUMER"


def feature_flagged_test_consumer(
    broker: HostFileCredentialBroker,
    request: LeaseRequest,
) -> bytes:
    if os.environ.get(TEST_CONSUMER_FEATURE_FLAG) != "1":
        raise RuntimeError("test credential consumer is feature-flag disabled")
    lease = broker.issue(request)
    try:
        return os.read(lease.fd, 1024)
    finally:
        broker.revoke(lease.lease_id)


class CredentialBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.secret = self.root / "token"
        self.secret.write_bytes(b"test-secret-value")
        self.secret.chmod(0o600)
        self.now = 1000.0
        self.parent_stat = self.root.stat()
        self.credential_ref = CredentialRef(
            credential_id="github/pantheon/test",
            kind="bearer-token",
            provider="host-file",
            location=self.secret,
            owner_uid=os.geteuid(),
            owner_gid=self.secret.stat().st_gid,
            roles=("recovery",),
            instances=("test",),
            services=("github",),
            actions=("read",),
            host_file=HostFileConstraints(
                parent_dir=self.root,
                parent_owner_uid=self.parent_stat.st_uid,
                parent_owner_gid=self.parent_stat.st_gid,
                parent_mode=stat.S_IMODE(self.parent_stat.st_mode),
            ),
        )
        self.audit = self.root / "state" / "audit.jsonl"
        self.broker = HostFileCredentialBroker(
            (self.credential_ref,),
            audit_path=self.audit,
            clock=lambda: self.now,
            max_ttl_seconds=300,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **changes) -> LeaseRequest:
        values = {
            "credential_id": self.credential_ref.credential_id,
            "caller_uid": os.geteuid(),
            "role": "recovery",
            "instance": "test",
            "service": "github",
            "action": "read",
            "ttl_seconds": 60,
        }
        values.update(changes)
        return LeaseRequest(**values)

    def make_ref(self, **changes) -> CredentialRef:
        values = {
            "credential_id": "github/pantheon/other",
            "kind": "bearer-token",
            "provider": "host-file",
            "location": self.secret,
            "owner_uid": os.geteuid(),
            "owner_gid": self.secret.stat().st_gid,
            "roles": ("recovery",),
            "instances": ("test",),
            "services": ("github",),
            "actions": ("read",),
            "host_file": self.credential_ref.host_file,
        }
        values.update(changes)
        return CredentialRef(**values)

    def audit_records(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.audit.read_text(encoding="utf-8").splitlines()
        ]

    def replacement_source(self, payload: bytes = b"replacement-secret") -> int:
        source_path = self.root / "replacement"
        source_path.write_bytes(payload)
        return os.open(source_path, os.O_RDONLY)

    def test_instance_scope_mismatch_is_rejected(self) -> None:
        for request in (
            self.request(caller_uid=os.geteuid() + 1),
            self.request(role="orchestrator"),
            self.request(instance="prod"),
            self.request(service="telegram"),
            self.request(action="write"),
            self.request(ttl_seconds=301),
        ):
            with self.subTest(request=request):
                with self.assertRaises(CredentialPolicyError):
                    self.broker.issue(request)

    def test_opened_fd_validates_type_owner_group_mode_and_parent(self) -> None:
        lease = self.broker.issue(self.request())
        try:
            self.assertEqual(os.read(lease.fd, 1024), b"test-secret-value")
            self.assertNotIn("test-secret-value", repr(lease))
        finally:
            self.broker.revoke(lease.lease_id)
        self.assertEqual(stat.S_IMODE(self.audit.stat().st_mode), 0o600)

        self.secret.chmod(0o640)
        with self.assertRaisesRegex(CredentialValidationError, "mode"):
            self.broker.issue(self.request())
        self.secret.chmod(0o600)

        link = self.root / "link"
        link.symlink_to(self.secret)
        linked_ref = self.make_ref(
            credential_id="github/pantheon/linked",
            location=link,
            host_file=HostFileConstraints(
                parent_dir=self.root,
                parent_owner_uid=self.parent_stat.st_uid,
                parent_owner_gid=self.parent_stat.st_gid,
                parent_mode=stat.S_IMODE(self.parent_stat.st_mode),
            ),
        )
        linked = HostFileCredentialBroker(
            (linked_ref,),
            audit_path=self.audit,
            clock=lambda: self.now,
        )
        with self.assertRaisesRegex(CredentialValidationError, "symlink"):
            linked.issue(
                LeaseRequest(
                    credential_id=linked_ref.credential_id,
                    caller_uid=os.geteuid(),
                    role="recovery",
                    instance="test",
                    service="github",
                    action="read",
                    ttl_seconds=1,
                )
            )

        wrong_parent = self.root / "wrong-parent"
        wrong_parent.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "parent constraint"):
            self.make_ref(
                credential_id="github/pantheon/wrong-parent",
                host_file=HostFileConstraints(
                    parent_dir=wrong_parent,
                    parent_owner_uid=self.parent_stat.st_uid,
                    parent_owner_gid=self.parent_stat.st_gid,
                ),
            )

    def test_wrong_provider_and_sensitive_metadata_are_rejected(self) -> None:
        with self.assertRaisesRegex(CredentialValidationError, "provider"):
            HostFileCredentialBroker(
                (
                    self.make_ref(
                        credential_id="github/pantheon/wrong-provider",
                        provider="systemd-credential",
                    ),
                ),
                audit_path=self.audit,
                clock=lambda: self.now,
            )
        with self.assertRaisesRegex(CredentialValidationError, "kind"):
            HostFileCredentialBroker(
                (
                    self.make_ref(
                        credential_id="github/pantheon/wrong-kind",
                        kind="ssh",
                    ),
                ),
                audit_path=self.audit,
                clock=lambda: self.now,
            )
        with self.assertRaisesRegex(ValueError, "bounded non-secret identifier"):
            self.request(role="token=secret-value")
        with self.assertRaisesRegex(ValueError, "bounded non-secret identifier"):
            self.make_ref(credential_id="github/pantheon/secret token")
        lease = self.broker.issue(self.request())
        self.broker.revoke(lease.lease_id)
        audit_text = self.audit.read_text(encoding="utf-8")
        self.assertNotIn("test-secret-value", audit_text)
        self.assertNotIn("secret-value", audit_text)

    def test_duplicate_fd_documents_cooperative_revocation_boundary(self) -> None:
        lease = self.broker.issue(self.request())
        duplicate_fd = os.dup(lease.fd)
        try:
            self.assertEqual(lease.provider, "host-file")
            self.assertEqual(lease.revocation_strength, "cooperative-host-fd")
            self.broker.revoke(lease.lease_id)
            with self.assertRaises(OSError):
                os.fstat(lease.fd)
            self.assertEqual(os.read(duplicate_fd, 1024), b"test-secret-value")
        finally:
            os.close(duplicate_fd)
        self.assertEqual(
            self.audit_records()[-1]["revocation_strength"],
            "cooperative-host-fd",
        )

    def test_rotation_verifier_failure_preserves_old_material(self) -> None:
        source_fd = self.replacement_source()

        def reject_replacement(_ref: CredentialRef, fd: int) -> bool:
            self.assertEqual(os.read(fd, 1024), b"replacement-secret")
            return False

        try:
            with self.assertRaisesRegex(CredentialValidationError, "verifier"):
                self.broker.rotate(
                    self.credential_ref.credential_id,
                    SecretSource(source_fd),
                    reject_replacement,
                )
        finally:
            os.close(source_fd)
        self.assertEqual(self.secret.read_bytes(), b"test-secret-value")
        audit_text = self.audit.read_text(encoding="utf-8")
        self.assertIn('"outcome":"failed"', audit_text)
        self.assertNotIn("replacement-secret", audit_text)

    def test_concurrent_issue_revoke_and_reap_are_serialized(self) -> None:
        verifier_entered = threading.Event()
        verifier_release = threading.Event()
        contender_started = threading.Event()
        contender_finished = threading.Event()
        results: list[str] = []
        source_fd = self.replacement_source()
        first_lease = self.broker.issue(self.request(ttl_seconds=1))
        self.now = 1002.0

        def blocking_verifier(_ref: CredentialRef, fd: int) -> bool:
            self.assertEqual(os.read(fd, 1024), b"replacement-secret")
            verifier_entered.set()
            self.assertTrue(verifier_release.wait(timeout=5))
            return True

        def rotate() -> None:
            try:
                self.broker.rotate(
                    self.credential_ref.credential_id,
                    SecretSource(source_fd),
                    blocking_verifier,
                )
                results.append("rotate")
            finally:
                os.close(source_fd)

        def contend() -> None:
            contender_started.set()
            lease = self.broker.issue(self.request())
            self.broker.revoke(lease.lease_id)
            self.broker.reap_expired()
            results.append("contender")
            contender_finished.set()

        rotator = threading.Thread(target=rotate)
        rotator.start()
        self.assertTrue(verifier_entered.wait(timeout=5))
        lock_available = self.broker._lock.acquire(blocking=False)  # noqa: SLF001
        if lock_available:
            self.broker._lock.release()  # noqa: SLF001
        self.assertFalse(
            lock_available,
            "rotate must hold the broker lock while verifier inspects staged fd",
        )
        contender = threading.Thread(target=contend)
        contender.start()
        self.assertTrue(contender_started.wait(timeout=5))
        self.assertFalse(contender_finished.is_set())
        verifier_release.set()
        rotator.join(timeout=5)
        contender.join(timeout=5)
        self.assertFalse(rotator.is_alive())
        self.assertFalse(contender.is_alive())
        self.assertEqual(results, ["rotate", "contender"])
        self.assertEqual(self.secret.read_bytes(), b"replacement-secret")
        with self.assertRaises(OSError):
            os.fstat(first_lease.fd)

    def test_feature_flagged_consumer_reads_via_lease(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "feature-flag disabled"):
            feature_flagged_test_consumer(
                self.broker,
                self.request(),
            )
        with patch.dict(os.environ, {TEST_CONSUMER_FEATURE_FLAG: "1"}):
            self.assertEqual(
                feature_flagged_test_consumer(self.broker, self.request()),
                b"test-secret-value",
            )
        self.assertEqual(self.audit_records()[-1]["event"], "revoke")

    def test_expired_ref_verify_and_issue_refuse_before_material_release(self) -> None:
        missing = self.root / "missing-token"
        expired_ref = self.make_ref(
            credential_id="github/pantheon/expired",
            location=missing,
            expires_at=self.now - 1,
        )
        broker = HostFileCredentialBroker(
            (expired_ref,),
            audit_path=self.audit,
            clock=lambda: self.now,
            max_ttl_seconds=300,
        )
        status = broker.verify(expired_ref)
        self.assertFalse(status.available)
        self.assertEqual(status.errors, ("credential reference has expired",))
        self.assertIsNone(status.owner_uid)
        with self.assertRaisesRegex(CredentialValidationError, "expired"):
            broker.issue(
                self.request(
                    credential_id=expired_ref.credential_id,
                )
            )
        audit_text = self.audit.read_text(encoding="utf-8")
        self.assertNotIn("test-secret-value", audit_text)
        self.assertNotIn("missing-token", audit_text)

    def test_host_file_capability_override_cannot_claim_strong_revocation(self) -> None:
        with self.assertRaisesRegex(ValueError, "host-file.*cooperative"):
            ProviderCapabilities(
                provider="host-file",
                supported_kinds=("bearer-token",),
                lease_delivery=("fd",),
                revocation_strength="strong-provider",
            )

    def test_host_file_broker_rejects_alias_strong_provider_with_duplicate_fd(
        self,
    ) -> None:
        alias_capability = ProviderCapabilities(
            provider="host-file-alias",
            supported_kinds=("bearer-token",),
            lease_delivery=("fd",),
            revocation_strength="strong-provider",
        )
        alias_ref = self.make_ref(
            credential_id="github/pantheon/alias",
            provider="host-file-alias",
        )
        source_fd = os.open(self.secret, os.O_RDONLY)
        duplicate_fd = os.dup(source_fd)
        try:
            with self.assertRaisesRegex(ValueError, "host-file cooperative"):
                HostFileCredentialBroker(
                    (alias_ref,),
                    audit_path=self.audit,
                    clock=lambda: self.now,
                    max_ttl_seconds=300,
                    capabilities=(alias_capability,),
                )
            self.assertEqual(os.read(duplicate_fd, 1024), b"test-secret-value")
        finally:
            os.close(source_fd)
            os.close(duplicate_fd)

    def test_expired_lease_is_closed_and_revoked(self) -> None:
        lease = self.broker.issue(self.request(ttl_seconds=10))
        self.now = 1011.0
        self.assertEqual(self.broker.reap_expired(), (lease.lease_id,))
        with self.assertRaises(OSError):
            os.fstat(lease.fd)
        self.assertEqual(self.audit_records()[-1]["event"], "revoke")

    def test_rotation_success_replaces_value_without_auditing_it(self) -> None:
        source_fd = self.replacement_source()

        def accept_replacement(_ref: CredentialRef, fd: int) -> bool:
            self.assertEqual(os.read(fd, 1024), b"replacement-secret")
            return True

        try:
            self.broker.rotate(
                self.credential_ref.credential_id,
                SecretSource(source_fd),
                accept_replacement,
            )
        finally:
            os.close(source_fd)
        self.assertEqual(self.secret.read_bytes(), b"replacement-secret")
        audit_text = self.audit.read_text(encoding="utf-8")
        self.assertNotIn("replacement-secret", audit_text)
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
