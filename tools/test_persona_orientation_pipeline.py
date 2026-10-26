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


def source_binding_problems(root: Path) -> list[str]:
    """The CI gate on rulebook/persona-orientation/source-bindings.yaml; [] means it
    holds. T-4726: a stamp holds iff its source exists, its blob exists in this repo AND
    is the source's blob at HEAD. git_sha is provenance only and is deliberately NOT
    asserted reachable: a squash merge drops the PR branch's commits, so a source edit
    and its re-stamp in one PR used to go red on main (#770, #785). Same contract as the
    SOP-skill stamps (T-4669, test_persona_orientation_sop_skills.sop_stamp_problems)."""
    path = root / "rulebook" / "persona-orientation" / "source-bindings.yaml"
    if not path.exists():
        return ["source-bindings.yaml missing -- run render.py first"]
    bindings = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not bindings:
        return ["source-bindings.yaml is empty"]
    problems = []
    for rel_path, stamp in bindings.items():
        if not (root / rel_path).exists():
            problems.append(f"{rel_path}: does not exist on disk")
            continue
        blob_there = subprocess.run(
            ["git", "cat-file", "-e", f"{stamp['git_blob']}^{{blob}}"], cwd=root, capture_output=True,
        )
        if blob_there.returncode != 0:
            problems.append(f"{rel_path}: stamped blob does not exist in this repo")
        head_blob = subprocess.run(
            ["git", "rev-parse", f"HEAD:{rel_path}"], cwd=root, capture_output=True, text=True,
        ).stdout.strip()
        if head_blob != stamp["git_blob"]:
            problems.append(f"{rel_path}: stamp is not the source at HEAD")
    for item in check_stale.check_orientation_stamps(root):
        problems.append(f"{item['path']}: {item['reason']}")
    return problems


class ManifestSourceBindingsReachable(unittest.TestCase):
    """Runs against the REAL repo + the actually-committed manifest and
    source-bindings.yaml -- this is the CI freshness gate itself, not a
    fixture test. cwd is this worktree's own root, never the shared
    checkout, per the standing pinned-revision reachability pattern
    (bugs/pinned-git-revision-unreachable-fails-closed-at-activation.md).
    T-4726: the gate checks content (the stamped blob is the source at HEAD),
    not git_sha reachability, so a squash merge cannot red main."""

    def test_manifest_source_bindings_reachable(self):
        self.assertEqual(source_binding_problems(REPO_ROOT), [])


def render_stamp(root: Path, rel_path: str) -> dict:
    cache = render.SourceCache(root)
    cache.text(rel_path)
    return cache.bindings[rel_path]


class ManifestEntriesAllStamped(unittest.TestCase):
    """T-4743: every source the manifest + role-deltas pull in must have a stamp in
    source-bindings.yaml (mirror of the SOP-skill 'no stamp recorded' check), and a
    stamp for a source the manifest no longer pulls in is stale. Temp-dir repo only."""

    SOURCE = "rulebook/fixture.md"
    BINDINGS = "rulebook/persona-orientation/source-bindings.yaml"
    IDENT = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        po = self.root / "rulebook" / "persona-orientation"
        po.mkdir(parents=True)
        (self.root / self.SOURCE).write_text("fixture body\n", encoding="utf-8")
        (po / "manifest.yaml").write_text(yaml.safe_dump({
            "version": 1,
            "base_layer": {"sections": [{"id": "inline_one", "extract": "inline", "body": "hi"}]},
            "roles": [{"id": "implementer", "delta_sections": ["d"]}],
        }), encoding="utf-8")
        (po / "role-deltas.yaml").write_text(yaml.safe_dump({
            "version": 1,
            "roles": {"implementer": {"d": {"extract": "full-file", "source": self.SOURCE}}},
        }), encoding="utf-8")
        self.commit("sources")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *self.IDENT, *args], cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, msg: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def stamp(self) -> None:
        render.write_source_bindings(self.root, render.render_all(self.root)[1])
        self.commit("stamps")

    def test_manifest_entry_with_no_stamp_is_stale(self):
        (self.root / self.BINDINGS).write_text("{}\n", encoding="utf-8")
        self.commit("empty bindings")
        report = check_stale.check(self.root)
        self.assertEqual(report["status"], "stale")
        self.assertEqual([d["path"] for d in report["drifted"]], [self.SOURCE])
        self.assertIn("no stamp", report["drifted"][0]["reason"])
        self.assertTrue(source_binding_problems(self.root))  # empty-bindings guard fires too

    def test_fully_stamped_manifest_is_fresh(self):
        self.stamp()
        self.assertEqual(check_stale.check(self.root)["status"], "fresh")
        self.assertEqual(source_binding_problems(self.root), [])

    def test_stamp_for_a_source_the_manifest_no_longer_names_is_stale(self):
        self.stamp()
        (self.root / "rulebook" / "other.md").write_text("other\n", encoding="utf-8")
        self.commit("other")
        bindings = yaml.safe_load((self.root / self.BINDINGS).read_text(encoding="utf-8"))
        bindings["rulebook/other.md"] = render_stamp(self.root, "rulebook/other.md")
        render.write_source_bindings(self.root, bindings)
        self.commit("stray stamp")
        report = check_stale.check(self.root)
        self.assertEqual(report["status"], "stale")
        self.assertIn("no longer", report["drifted"][0]["reason"])

    def test_real_repo_manifest_is_fully_stamped(self):
        self.assertEqual(check_stale.check_orientation_stamps(REPO_ROOT), [])


class SquashMergedSourceBindings(unittest.TestCase):
    """T-4726: a source edit and its re-stamp in ONE PR, squash-merged. The squash drops
    the branch's commits, so the stamp's git_sha is no longer an ancestor of main; the
    gate must hold anyway, and a post-merge re-render must not touch the stamp."""

    SOURCE = "sop/standard/demo.md"
    BINDINGS = "rulebook/persona-orientation/source-bindings.yaml"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        (self.root / "sop" / "standard").mkdir(parents=True)
        (self.root / "rulebook" / "persona-orientation").mkdir(parents=True)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write_source(self, text: str, msg: str) -> None:
        (self.root / self.SOURCE).write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def stamp(self) -> None:
        """What render.py does for one source: stamp it at its last commit."""
        data = (self.root / self.SOURCE).read_bytes()
        bindings = {
            self.SOURCE: {
                "git_sha": self.git("log", "-1", "--format=%H", "--", self.SOURCE),
                "git_blob": self.git("rev-parse", f"HEAD:{self.SOURCE}"),
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_length": len(data),
            }
        }
        render.write_source_bindings(self.root, bindings)

    def commit_stamp(self, msg: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def test_edit_plus_restamp_in_one_pr_holds_after_a_squash_merge(self):
        self.write_source("# Demo\n\nv1\n", "source v1")
        self.stamp()
        self.commit_stamp("stamps on main")
        self.git("checkout", "-q", "-b", "feature")
        self.write_source("# Demo\n\nv2\n", "source v2")
        edit_sha = self.git("rev-parse", "HEAD")
        self.stamp()
        self.commit_stamp("restamp")
        self.assertEqual(source_binding_problems(self.root), [])
        stamp_sha = yaml.safe_load((self.root / self.BINDINGS).read_text())[self.SOURCE]["git_sha"]
        self.assertEqual(stamp_sha, edit_sha)

        self.git("checkout", "-q", "main")
        self.git("merge", "--squash", "feature")
        self.git("commit", "-q", "-m", "squash merge of feature")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", stamp_sha, "HEAD"], cwd=self.root, capture_output=True,
        )
        self.assertEqual(ancestry.returncode, 1, "precondition: the stamped sha is gone from main")
        self.assertEqual(source_binding_problems(self.root), [])

        # CI's view: a clone of main that never fetched the feature branch.
        clone = self.tmp / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--single-branch", "--branch", "main", f"file://{self.root}", str(clone)],
            check=True,
        )
        gone = subprocess.run(["git", "cat-file", "-e", f"{stamp_sha}^{{commit}}"], cwd=clone, capture_output=True)
        self.assertNotEqual(gone.returncode, 0, "the clone must not contain the squashed-away commit")
        self.assertEqual(source_binding_problems(clone), [])

        # A post-merge re-render keeps the stamp byte-identical (no seat-clone churn).
        before = (self.root / self.BINDINGS).read_text(encoding="utf-8")
        self.stamp()
        self.assertEqual((self.root / self.BINDINGS).read_text(encoding="utf-8"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_real_source_edit_is_restamped_and_a_stale_stamp_fails(self):
        self.write_source("# Demo\n\nv1\n", "source v1")
        self.stamp()
        self.commit_stamp("stamps")
        old_blob = yaml.safe_load((self.root / self.BINDINGS).read_text())[self.SOURCE]["git_blob"]
        self.write_source("# Demo\n\nv2\n", "source v2")
        self.assertIn(f"{self.SOURCE}: stamp is not the source at HEAD", source_binding_problems(self.root))
        self.stamp()
        new_blob = yaml.safe_load((self.root / self.BINDINGS).read_text())[self.SOURCE]["git_blob"]
        self.assertNotEqual(new_blob, old_blob)
        self.commit_stamp("restamp")
        self.assertEqual(source_binding_problems(self.root), [])


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


class ThreatModelWiring(unittest.TestCase):
    """T-5288: ORG-105 is present in the cheat sheet and the reviewer checklists."""

    def test_policy_triggers_carry_a_threat_model_pointer_to_org_105_section_4(self):
        manifest = render.load_manifest(REPO_ROOT)
        cache = render.SourceCache(REPO_ROOT)
        section = next(s for s in manifest["base_layer"]["sections"] if s["id"] == "policy_triggers")
        spec = next((x for x in section["sources"] if x["label"] == "threat-model"), None)
        self.assertIsNotNone(spec, "policy_triggers has no threat-model entry")
        paragraph = render.extract_heading_region_paragraph(
            cache.text(spec["path"]), spec["heading"], spec.get("select_prefix"),
        )
        self.assertIn("ORG-105", paragraph)
        self.assertIn("4", paragraph)

    def test_reviewer_surfaces_carry_the_org_105_chain_line(self):
        skill = (REPO_ROOT / "harness/claude/skills/reviewer-methodology/SKILL.md").read_text()
        self.assertIn("Axis H", skill)
        self.assertIn("ORG-105", skill)
        sop = (REPO_ROOT / "sop/standard/reviewer-methodologies.md").read_text()
        self.assertIn("ORG-105", sop)


class RoleDeltaContent(unittest.TestCase):
    """T-3997 / T-4005 regressions, against the real manifest + sources."""

    COUNT_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}

    def test_sourced_delta_keys_carry_no_count_words(self):
        # A delta key renders verbatim as its section heading; a count baked
        # into it ("three_methodologies") goes stale when the sourced body
        # grows. Inline-only deltas are exempt: key and body are edited together.
        role_deltas = render.load_role_deltas(REPO_ROOT)
        for role_id, deltas in role_deltas["roles"].items():
            for key, spec in deltas.items():
                parts = spec if isinstance(spec, list) else [spec]
                if all(p.get("extract") == "inline" for p in parts):
                    continue
                with self.subTest(role=role_id, key=key):
                    self.assertFalse(self.COUNT_WORDS & set(key.split("_")), key)

    def test_implementer_skill_carries_no_bare_stash_rule(self):
        artifacts, _ = render.render_all(REPO_ROOT)
        self.assertIn("never bare `git stash`", artifacts["role:implementer"])


if __name__ == "__main__":
    unittest.main()
