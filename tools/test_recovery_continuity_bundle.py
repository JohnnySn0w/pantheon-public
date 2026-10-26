#!/usr/bin/env python3
"""Hermetic tests for tools/recovery_continuity_bundle.py (T-3178 gate G3).

No dependency on the live repo's file content or modes: synthetic source files
are staged into a temp continuity root, so the test exercises the
stage -> author-descriptor -> resolve -> checkpoint -> readback logic in
isolation (a live-file probe would be non-hermetic and seat-luck-green).
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools import recovery_continuity_bundle as wcb
from tools import persona_continuity as pc


class RecoveryContinuityBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_dir = self.root / "synthetic-sources"
        self.source_dir.mkdir()
        # One synthetic non-empty file per canonical source, keyed by label.
        self._paths: dict[str, Path] = {}
        for src in wcb.CANONICAL_SOURCES:
            path = self.source_dir / f"{src.label}.src"
            path.write_text(f"synthetic canonical content for {src.label} ({src.kind})\n")
            self._paths[src.label] = path

    def _read_source(self, src: wcb.CanonicalSource) -> bytes:
        return self._paths[src.label].read_bytes()

    def _build(self, continuity_root: Path) -> wcb.BundleArtifacts:
        return wcb.build_bundle(
            continuity_root=continuity_root,
            sources=wcb.CANONICAL_SOURCES,
            read_source=self._read_source,
            pinned_revision="deadbeef" * 5,
        )

    def test_build_produces_resolvable_bundle_and_verified_checkpoint(self) -> None:
        cont = self.root / "continuity"
        artifacts = self._build(cont)

        # Bundle descriptor loads and the checkpoint reads back matching it.
        manifest = pc.load_continuity_bundle(artifacts.bundle_path, descriptor_root=artifacts.descriptor_root)
        self.assertEqual(manifest.persona_id, "recovery")
        self.assertEqual(manifest.descriptor_sha256, artifacts.descriptor_sha256)
        envelope = pc.load_checkpoint_envelope(artifacts.checkpoint_path, checkpoint_root=artifacts.checkpoint_root)
        self.assertEqual(envelope.descriptor_sha256, manifest.descriptor_sha256)
        self.assertEqual(envelope.persona_id, "recovery")
        self.assertEqual(envelope.payload_sha256, artifacts.payload_sha256)

        # A full resolve against the staged sources succeeds with no omissions
        # (every required canonical ref resolved -- the fire's hydration input).
        policy = pc.load_source_policy_catalog(artifacts.descriptor_root / "policy.json")
        store = pc.ContinuitySourceStore(
            repo_root=artifacts.continuity_root / "sources" / "repo",
            state_root=artifacts.continuity_root / "sources" / "state",
            source_policy=policy,
        )
        actor = policy.actor_context("recovery", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        from datetime import datetime, timezone
        view = pc.ContinuityResolver(store, checkpoint_root=artifacts.checkpoint_root).resolve(
            manifest, purpose="hydrate-runtime", actor=actor,
            as_of=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(len(view.omissions), 0)
        kinds = {e.kind for e in view.source_evidence}
        for required in ("identity", "role", "authority", "capability", "runtime", "goal", "memory"):
            self.assertIn(required, kinds)

    def test_staged_tree_modes_satisfy_resolver_constraints(self) -> None:
        cont = self.root / "continuity"
        artifacts = self._build(cont)
        # Staged source files must be non-group/other-writable (resolver rejects mode & 0o022).
        for base in ("sources/repo", "sources/state"):
            for path in (artifacts.continuity_root / base).rglob("*"):
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertEqual(mode & 0o022, 0, f"{path} is group/other-writable: {mode:04o}")

    def test_readback_inventory_records_pins(self) -> None:
        import json
        cont = self.root / "continuity"
        artifacts = self._build(cont)
        inv = json.loads(artifacts.inventory_path.read_bytes())
        self.assertEqual(inv["pinned_revision"], "deadbeef" * 5)
        self.assertEqual(inv["descriptor_sha256"], artifacts.descriptor_sha256)
        self.assertEqual(inv["checkpoint"]["payload_sha256"], artifacts.payload_sha256)
        self.assertEqual(len(inv["sources"]), len(wcb.CANONICAL_SOURCES))
        # The memory source is marked private+redacted.
        mem = [s for s in inv["sources"] if s["kind"] == "memory"]
        self.assertTrue(mem and mem[0]["redacted"] and mem[0]["privacy"] == "private")

    def test_force_rebuild_is_idempotent(self) -> None:
        cont = self.root / "continuity"
        a1 = self._build(cont)
        a2 = self._build(cont)  # build_bundle rmtrees + rebuilds
        self.assertEqual(a1.descriptor_sha256, a2.descriptor_sha256)

    def test_rebuild_preserves_switch_history(self) -> None:
        # Append-only: the coordinator's switch/ transition+receipt history must
        # survive a rebuild (consultant T-3178 finding 5).
        cont = self.root / "continuity"
        self._build(cont)
        marker = cont / "switch" / "recovery" / "transitions" / "keepme.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"kept": true}')
        self._build(cont)  # rebuild
        self.assertTrue(marker.exists(), "rebuild destroyed switch history")

    def test_provenance_recorded_in_inventory(self) -> None:
        import json
        cont = self.root / "continuity"
        artifacts = self._build(cont)
        inv = json.loads(artifacts.inventory_path.read_bytes())
        self.assertEqual(inv["provenance"]["repo_sources"], "pinned-git-object")
        self.assertEqual(inv["checkpoint"]["kind"], "canonical-fixture-pending-live-capture")

    def test_empty_source_is_rejected(self) -> None:
        # A canonical source that resolves to empty bytes must fail loudly
        # (an empty identity/authority file would silently degrade hydration).
        self._paths[wcb.CANONICAL_SOURCES[0].label].write_text("")
        with self.assertRaises(ValueError):
            self._build(self.root / "continuity")


if __name__ == "__main__":
    unittest.main()
