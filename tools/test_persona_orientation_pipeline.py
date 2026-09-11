"""Tests for tools/persona_orientation/ (T-3339 Phase 1).

Covers the three Phase-1-scoped tests: render idempotency, check_stale drift
detection, and manifest source-binding reachability, plus a REBOUND-round-2
completeness guard on the block-extraction primitive. Tests 1-6 from the
T-3339 plan's Structural test surface (live subagent spawns, project-overlay,
role-boundary enforcement, cross-agent round-trip) are deferred to Phase 3/4
-- they need harness-boundary fixtures beyond Phase 1's landing surface.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tools.persona_orientation import check_stale, render

REPO_ROOT = Path(__file__).resolve().parents[1]


class RenderIdempotent(unittest.TestCase):
    def test_render_idempotent_when_sources_unchanged(self):
        artifacts_a, bindings_a = render.render_all(REPO_ROOT)
        artifacts_b, bindings_b = render.render_all(REPO_ROOT)

        self.assertEqual(set(artifacts_a), set(artifacts_b))
        for key in artifacts_a:
            self.assertEqual(
                artifacts_a[key], artifacts_b[key],
                msg=f"artifact {key!r} differs across two consecutive renders",
            )
        self.assertEqual(bindings_a, bindings_b)


class CheckStaleDetectsDrift(unittest.TestCase):
    """Isolated temp-dir git repo fixture -- never mutates the live tree
    (project convention: temp-dir fixtures only for lifecycle-verb tests)."""

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def _init_repo_with_source(self, root: Path, content: str) -> tuple[Path, str, str]:
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "test")
        (root / "rulebook").mkdir()
        source = root / "rulebook" / "fixture.md"
        source.write_text(content, encoding="utf-8")
        self._git(root, "add", ".")
        self._git(root, "commit", "-q", "-m", "init")
        head = self._git(root, "rev-parse", "HEAD")
        blob = self._git(root, "rev-parse", f"{head}:rulebook/fixture.md")
        return source, head, blob

    def _write_bindings(self, root: Path, rel_path: str, head: str, blob: str, raw: bytes) -> None:
        bindings_dir = root / "rulebook" / "persona-orientation"
        bindings_dir.mkdir(parents=True, exist_ok=True)
        bindings = {
            rel_path: {
                "git_sha": head,
                "git_blob": blob,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "byte_length": len(raw),
            }
        }
        (bindings_dir / "source-bindings.yaml").write_text(
            yaml.safe_dump(bindings, sort_keys=True), encoding="utf-8",
        )

    def test_check_stale_detects_drifted_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, head, blob = self._init_repo_with_source(root, "original content\n")
            self._write_bindings(root, "rulebook/fixture.md", head, blob, source.read_bytes())

            # Fresh before mutation.
            report = check_stale.check(root)
            self.assertEqual(report["status"], "fresh")
            self.assertEqual(
                check_stale.main(["--repo-root", str(root), "--threshold", "0"]), 0,
            )

            # Mutate + commit -> the stamped blob no longer matches HEAD's.
            source.write_text("mutated content\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-q", "-m", "mutate")

            report = check_stale.check(root)
            self.assertEqual(report["status"], "stale")
            self.assertEqual(len(report["drifted"]), 1)
            self.assertEqual(report["drifted"][0]["path"], "rulebook/fixture.md")
            self.assertEqual(
                check_stale.main(["--repo-root", str(root), "--threshold", "0"]), 2,
            )

    def test_check_stale_missing_bindings_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            self.assertEqual(
                check_stale.main(["--repo-root", str(root)]), 2,
            )


class ManifestSourceBindingsReachable(unittest.TestCase):
    """Runs against the REAL repo + the actually-committed manifest and
    source-bindings.yaml -- this is the CI freshness gate itself, not a
    fixture test. cwd is this worktree's own root, never the shared
    checkout, per the standing pinned-revision reachability pattern
    (bugs/pinned-git-revision-unreachable-fails-closed-at-activation.md)."""

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def test_manifest_source_bindings_reachable(self):
        bindings_path = REPO_ROOT / "rulebook" / "persona-orientation" / "source-bindings.yaml"
        self.assertTrue(bindings_path.exists(), "source-bindings.yaml missing -- run render.py first")

        with open(bindings_path, encoding="utf-8") as f:
            bindings = yaml.safe_load(f)
        self.assertTrue(bindings, "source-bindings.yaml is empty")

        for rel_path, stamp in bindings.items():
            with self.subTest(source=rel_path):
                self.assertTrue(
                    (REPO_ROOT / rel_path).exists(),
                    f"{rel_path} does not exist on disk",
                )
                git_sha = stamp["git_sha"]

                # Fails if git_sha doesn't exist / isn't reachable from HEAD.
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", git_sha, "HEAD"],
                    cwd=REPO_ROOT, check=True,
                )

                # Fails if git_sha:path doesn't resolve to the stamped blob.
                resolved = self._git("rev-parse", f"{git_sha}:{rel_path}")
                self.assertEqual(resolved, stamp["git_blob"])


class BlockExtractionCompleteness(unittest.TestCase):
    """Guards against T-3339 Phase 1 REBOUND round 1's defect class: a
    ratified source separates its lead sentence from a bulleted/numbered
    continuation with a blank line, and the block-selection primitive
    (render._select_block / render._blocks_after) silently drops the
    continuation, keeping only the lead sentence. D14, the mcp-adoption
    policy trigger, and the goal-mapping policy trigger were all truncated
    this way -- each ended in a dangling ':' with no following content.

    Runs against the real manifest + live source files (not a fixture) --
    checks the extraction primitive's own output per entry, not the fully
    assembled markdown artifact, so an unrelated ':' elsewhere in the
    rendered skill (e.g. a "Full: `path`." pointer line) can't produce a
    false positive."""

    def test_block_extraction_does_not_truncate_before_trailing_colon(self):
        manifest = render.load_manifest(REPO_ROOT)
        cache = render.SourceCache(REPO_ROOT)

        for section in manifest["base_layer"]["sections"]:
            mode = section["extract"]
            if mode == "heading-list":
                for entry_id, title, paragraph in render.extract_heading_list(cache, section["sources"]):
                    with self.subTest(section=section["id"], entry=entry_id):
                        self.assertFalse(
                            paragraph.rstrip().endswith(":"),
                            f"{entry_id} ({title}) extraction ends in a dangling "
                            "colon -- a list continuation was likely truncated",
                        )
            elif mode == "heading-region-paragraph":
                for spec in section["sources"]:
                    text = cache.text(spec["path"])
                    paragraph = render.extract_heading_region_paragraph(
                        text, spec["heading"], spec.get("select_prefix"),
                    )
                    with self.subTest(section=section["id"], label=spec["label"]):
                        self.assertFalse(
                            paragraph.rstrip().endswith(":"),
                            f"{spec['label']} extraction ends in a dangling colon "
                            "-- a list continuation was likely truncated",
                        )


if __name__ == "__main__":
    unittest.main()
