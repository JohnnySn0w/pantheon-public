"""Hermetic tests for the T-3738 retained ORG-083 pantheon-config evidence record.

Repo files only; no network; no cwd dependence; time only via explicit NOW.
The validator's authority lookup needs the evidence revision's git objects; that
one test skips with a typed reason when the object is absent (shallow clone).
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "knowledge/episodic/2026-11-01-t3738-pantheon-config-org083-evidence"
INPUT = EVIDENCE_DIR / "input.yaml"
CANONICAL = EVIDENCE_DIR / "canonical.json"
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
AFTER_RETENTION = datetime(2027, 1, 2, tzinfo=timezone.utc)


def _mod():
    return importlib.import_module("tools.repository_evidence")


def _revision_available(revision: str) -> bool:
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
    )
    return r.returncode == 0


class RetainedEvidenceTests(unittest.TestCase):
    def test_canonical_bytes_match_input(self) -> None:
        mod = _mod()
        data = mod.load_repository_evidence(INPUT)
        self.assertEqual(mod.canonical_repository_evidence_bytes(data), CANONICAL.read_bytes())

    def test_schema_parity_and_committed_schema(self) -> None:
        mod = _mod()
        from tools.test_repository_evidence import _schema_subset_errors

        schema = mod.generate_repository_evidence_schema()
        committed = (REPO_ROOT / "schemas/repository-evidence-contract.schema.json").read_bytes()
        self.assertEqual(mod._canonical_json_bytes(schema), committed)
        for data in (mod.load_repository_evidence(INPUT), json.loads(CANONICAL.read_text(encoding="utf-8"))):
            self.assertEqual(_schema_subset_errors(schema, data, root_schema=schema), [])

    def test_artifacts_digest_size_and_receipts(self) -> None:
        mod = _mod()
        data = mod.load_repository_evidence(INPUT)
        artifacts = {a["artifact_id"]: a for a in data["artifact_records"]}
        self.assertGreaterEqual(len(artifacts), 3)
        for a in artifacts.values():
            self.assertEqual(a["storage_locator"]["kind"], "repo_path")
            content = (REPO_ROOT / a["storage_locator"]["value"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), a["digest"])
            self.assertEqual(len(content), a["size"])
        for ret in data["retention_records"]:
            art = artifacts[ret["artifact_id"]]
            self.assertEqual(ret["digest"], art["digest"])
            for rc in ret["retrieval_receipts"]:
                self.assertEqual(rc["observed_digest"], art["digest"])
                self.assertEqual(rc["observed_size"], art["size"])
            self.assertEqual(ret["expiry_action"], "retain_tombstone")
            self.assertEqual(ret["retention_end"]["value"], "2026-12-31T00:00:00Z")

    def test_dispositions_are_explicit_and_non_promoting(self) -> None:
        mod = _mod()
        c = mod.load_repository_evidence(INPUT)
        self.assertEqual(c["maturity"], "inventory_only")
        self.assertEqual({d["dimension"] for d in c["dimension_dispositions"]}, set(mod.DIMENSIONS))
        self.assertTrue(all(d["status"] == "deferred" for d in c["dimension_dispositions"]))
        self.assertEqual({b["boundary_kind"] for b in c["boundary_records"]}, set(mod.BOUNDARY_KINDS))
        self.assertEqual(c["test_surface_gaps"], [])
        self.assertEqual(c["quantitative_measurements"], [])
        self.assertEqual(c["repo_evidence_owner_role"], "head-engineering-orchestrator")
        status = {b["boundary_kind"]: b["status"] for b in c["boundary_records"]}
        self.assertEqual(status.pop("shell_suite"), "exercised")
        self.assertEqual(set(status.values()), {"not_exercised"})

    def test_validator_derives_inventory_only_and_expiry_flags(self) -> None:
        mod = _mod()
        data = mod.load_repository_evidence(INPUT)
        rev = data["evidence_revision"]
        if not _revision_available(rev):
            self.skipTest(f"evidence revision {rev} not present in this (shallow?) clone")
        result = mod.check_repository_evidence(INPUT, repo_root=REPO_ROOT, now=NOW)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.derived_maturity, "inventory_only")
        late = mod.check_repository_evidence(INPUT, repo_root=REPO_ROOT, now=AFTER_RETENTION)
        self.assertIn("retention_expired", {i.code for i in late.issues})
        self.assertEqual(late.derived_maturity, "inventory_only")


if __name__ == "__main__":
    unittest.main()
