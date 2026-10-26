"""Tests for the SOP-skill render path in tools/persona_orientation/ (T-4465).

sop/standard/<x>.md rendered as an auto-triggering skill for the Claude and Codex
harnesses, stamped into tools/persona_orientation/sop-skill-bindings.yaml, with
render.py --check / check_stale.py catching drift.

Every output goes to a temp dir: each test class points HOME and CODEX_HOME at
temp dirs, so even a default-path write could never reach a real ~/.claude/skills
or ~/.codex/skills. Fixture repos are throwaway temp git repos; the real tree is
only ever READ (the real-repo gates at the bottom).
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools.persona_orientation import check_stale, render

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = "HERME" + "S.md"  # never contiguous in source

DEMO_SOP = """\
---
title: Demo procedure
status: Adopted 2026-01-01 -- front matter that must NOT reach the skill
---

# Demo procedure

Intro line. See [`other.md`](other.md).

## When to use this

- when demoing

## Step one

do the thing: `run it`

## Rationale

why it is this way
"""

ENTRY = {
    "slug": "demo",
    "sop": "sop/standard/demo-procedure.md",
    "description": "Use when demoing: something, anything 'quoted' and colon: here.",
    "short_description": "Demo procedure",
}


def split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), text[:40]
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end]), text[end + 5:]


def sop_stamp_problems(root: Path) -> list[str]:
    """The CI gate on the SOP-skill stamps, shared by the real-repo test and the squash
    simulation; [] means it holds. A stamp holds iff its blob exists in this repo AND is
    the SOP's blob at HEAD. The stamp's git_sha is provenance only and is deliberately NOT
    asserted reachable: a squash merge drops the PR branch's commits, so an SOP edit and
    its re-stamp in one PR would otherwise go red on main (T-4465 REBOUND; the persona
    stamps keep their own reachability test, unchanged)."""
    stamps = yaml.safe_load((root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8"))
    if not stamps:
        return ["sop-skill-bindings.yaml is empty -- run render.py first"]
    problems = []
    for rel_path, stamp in stamps.items():
        if not (root / rel_path).exists():
            problems.append(f"{rel_path}: stamped SOP does not exist")
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
            problems.append(f"{rel_path}: stamp is not the SOP at HEAD")
    status = check_stale.check_sop_skills(root)["status"]
    if status != "fresh":
        problems.append(f"check_sop_skills status {status}")
    return problems


def assert_sop_stamps_hold(tc: unittest.TestCase, root: Path) -> None:
    tc.assertEqual(sop_stamp_problems(root), [])


class TempHomes(unittest.TestCase):
    """Isolation: HOME and CODEX_HOME never point at a real directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.codex = self.tmp / "codexhome"
        self.home.mkdir()
        self.codex.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.codex)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def claude_skill(self, name: str) -> Path:
        return self.home / ".claude" / "skills" / name / "SKILL.md"

    def codex_skill(self, name: str) -> Path:
        return self.codex / "skills" / name / "SKILL.md"


class FixtureRepo(TempHomes):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "repo"
        (self.root / "sop" / "standard").mkdir(parents=True)
        (self.root / "tools" / "persona_orientation").mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        (self.root / "sop" / "standard" / "demo-procedure.md").write_text(DEMO_SOP, encoding="utf-8")
        self.write_manifest([dict(ENTRY)], [])
        self.commit("init")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, msg: str) -> None:
        self.git("add", ".")
        self.git("commit", "-q", "-m", msg)

    def write_manifest(self, skills: list[dict], pending: list[dict]) -> None:
        (self.root / render.SOP_SKILLS_MANIFEST).write_text(
            yaml.safe_dump({"version": 1, "skills": skills, "pending": pending}, sort_keys=False),
            encoding="utf-8",
        )

    def run_sop(self, mode: str, harnesses=("claude", "codex")) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = render.run_sop_skills(self.root, self.home, mode, harnesses, self.codex)
        return rc, out.getvalue(), err.getvalue()

    def add_sop(self, name: str, text: str) -> None:
        (self.root / "sop" / "standard" / name).write_text(text, encoding="utf-8")
        self.commit(f"add {name}")

    def edit_sop_and_commit(self) -> None:
        path = self.root / "sop" / "standard" / "demo-procedure.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n## Step two\n\nnew step\n", encoding="utf-8")
        self.commit("edit sop")


class ManifestValidation(FixtureRepo):
    def load_with(self, skills=None, pending=None) -> dict:
        self.write_manifest(skills if skills is not None else [dict(ENTRY)], pending or [])
        return render.load_sop_skills_manifest(self.root)

    def test_valid_manifest_loads(self):
        manifest = self.load_with(pending=[{"slug": "later", "awaiting": "an SOP nobody wrote yet"}])
        self.assertEqual([e["slug"] for e in manifest["skills"]], ["demo"])
        self.assertEqual([e["slug"] for e in manifest["pending"]], ["later"])

    def test_bad_entries_are_rejected(self):
        bad = {
            "uppercase slug": dict(ENTRY, slug="Demo"),
            "underscore slug": dict(ENTRY, slug="de_mo"),
            "name over 64 chars": dict(ENTRY, slug="x" * 52),
            "multiline description": dict(ENTRY, description="line one\nline two"),
            "empty description": dict(ENTRY, description="  "),
            "description over 1024": dict(ENTRY, description="a" * 1025),
            "multiline short_description": dict(ENTRY, short_description="a\nb"),
            "sop outside sop/standard": dict(ENTRY, sop="notes/x.md"),
            "sop in an unlisted sop subdir": dict(ENTRY, sop="sop/other/x.md"),
            "sop file missing": dict(ENTRY, sop="sop/standard/nope.md"),
            "unknown extract": dict(ENTRY, body={"extract": "whole-lot"}),
            "heading-region without headings": dict(ENTRY, body={"extract": "heading-region"}),
        }
        for label, entry in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.load_with([entry])

    def test_duplicate_slug_across_skills_and_pending_is_rejected(self):
        with self.assertRaises(ValueError):
            self.load_with(pending=[{"slug": "demo", "awaiting": "x"}])
        with self.assertRaises(ValueError):
            self.load_with([dict(ENTRY), dict(ENTRY)])

    def test_pending_entry_needs_slug_and_awaiting_but_not_an_existing_sop(self):
        self.load_with(pending=[{"slug": "later", "awaiting": "x", "sop": "sop/standard/not-yet.md"}])
        with self.assertRaises(ValueError):
            self.load_with(pending=[{"slug": "later"}])


class RemediationSops(FixtureRepo):
    """T-4782: sop/remediation/*.md renders like sop/standard/*.md."""

    def test_remediation_path_is_accepted_and_links_resolve_against_its_dir(self):
        path = self.root / "sop" / "remediation" / "fix-it.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Fix it\n\n## Step\n\ndo it\n", encoding="utf-8")
        self.commit("add remediation sop")
        entry = dict(ENTRY, slug="fix-it", sop="sop/remediation/fix-it.md")
        self.write_manifest([entry], [])
        manifest = render.load_sop_skills_manifest(self.root)
        self.assertEqual(manifest["skills"][0]["sop"], "sop/remediation/fix-it.md")
        text = render.render_sop_skill(render.SourceCache(self.root), entry, "claude")
        self.assertIn("Source of truth: `sop/remediation/fix-it.md`", text)
        self.assertIn("resolve against `sop/remediation/`", text)
        self.assertNotIn("resolve against `sop/standard/`", text)


class RenderOne(FixtureRepo):
    def render(self, harness: str, entry: dict | None = None) -> str:
        return render.render_sop_skill(render.SourceCache(self.root), entry or dict(ENTRY), harness)

    def test_claude_skill_shape(self):
        meta, body = split_frontmatter(self.render("claude"))
        self.assertEqual(meta, {"name": "pantheon-sop-demo", "description": ENTRY["description"]})
        self.assertIn(render.GENERATED_MARKER, body)
        self.assertIn("sop/standard/demo-procedure.md", body)
        self.assertIn("# Demo procedure", body)
        self.assertIn("do the thing: `run it`", body)
        self.assertNotIn("front matter that must NOT reach", body)
        self.assertIn("relative links", body.lower())

    def test_codex_skill_adds_short_description_only(self):
        claude_meta, claude_body = split_frontmatter(self.render("claude"))
        codex_meta, codex_body = split_frontmatter(self.render("codex"))
        self.assertEqual(codex_meta["name"], claude_meta["name"])
        self.assertEqual(codex_meta["description"], claude_meta["description"])
        self.assertEqual(codex_meta["metadata"], {"short-description": "Demo procedure"})
        self.assertNotIn("metadata", claude_meta)
        self.assertEqual(codex_body, claude_body)

    def test_codex_without_short_description_omits_metadata(self):
        entry = {k: v for k, v in ENTRY.items() if k != "short_description"}
        meta, _ = split_frontmatter(self.render("codex", entry))
        self.assertNotIn("metadata", meta)

    def test_sop_without_front_matter_renders_whole(self):
        self.add_sop("plain.md", "# Plain\n\nbody\n")
        _, body = split_frontmatter(self.render("claude", dict(ENTRY, sop="sop/standard/plain.md")))
        self.assertIn("# Plain\n\nbody", body)

    def test_heading_region_excerpt_keeps_named_sections_and_points_at_full_sop(self):
        entry = dict(ENTRY, body={"extract": "heading-region", "headings": ["## When to use this", "## Step one"]})
        _, body = split_frontmatter(self.render("claude", entry))
        self.assertIn("when demoing", body)
        self.assertIn("do the thing", body)
        self.assertNotIn("why it is this way", body)
        self.assertIn("Excerpt", body)
        self.assertIn("Full SOP: `sop/standard/demo-procedure.md`", body)

    def test_heading_region_is_not_cut_by_a_comment_line_inside_a_code_fence(self):
        scripted = "# Scripted\n\n## Run it\n\n```bash\n# phase 0: a shell comment, not a heading\nmake go\n```\n\nafter fence\n\n## Next\n\nother\n"
        self.add_sop("scripted.md", scripted)
        entry = dict(ENTRY, sop="sop/standard/scripted.md", body={"extract": "heading-region", "headings": ["## Run it"]})
        _, body = split_frontmatter(self.render("claude", entry))
        self.assertIn("# phase 0: a shell comment", body)
        self.assertIn("make go", body)
        self.assertIn("after fence", body)
        self.assertNotIn("## Next", body)

    def test_heading_region_with_missing_heading_fails_loudly(self):
        entry = dict(ENTRY, body={"extract": "heading-region", "headings": ["## No such section"]})
        with self.assertRaises(ValueError):
            self.render("claude", entry)

    def test_description_with_yaml_specials_round_trips(self):
        entry = dict(ENTRY, description="Use when: 'x' # not a comment, [a] {b} @c & *d | e > f")
        meta, _ = split_frontmatter(self.render("claude", entry))
        self.assertEqual(meta["description"], entry["description"])

    def test_render_is_idempotent(self):
        self.assertEqual(self.render("claude"), self.render("claude"))
        self.assertEqual(self.render("codex"), self.render("codex"))


class OutputPaths(TempHomes):
    def test_claude_under_home_codex_under_codex_home(self):
        artifacts = {"sop:demo:claude": "x", "sop:demo:codex": "y"}
        paths = render.sop_output_paths(self.home, self.codex, artifacts)
        self.assertEqual(paths["sop:demo:claude"], self.claude_skill("pantheon-sop-demo"))
        self.assertEqual(paths["sop:demo:codex"], self.codex_skill("pantheon-sop-demo"))

    def test_default_codex_home_follows_env_then_home(self):
        self.assertEqual(render.default_codex_home(self.home), self.codex)
        with mock.patch.dict(os.environ):
            os.environ.pop("CODEX_HOME")
            self.assertEqual(render.default_codex_home(self.home), self.home / ".codex")


class RunSopSkills(FixtureRepo):
    def test_write_renders_both_harnesses_and_stamps_only_the_sop_bindings(self):
        rc, out, _ = self.run_sop("write")
        self.assertEqual(rc, 0)
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file())
        self.assertTrue(self.codex_skill("pantheon-sop-demo").is_file())
        stamps = yaml.safe_load((self.root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8"))
        self.assertEqual(set(stamps), {"sop/standard/demo-procedure.md"})
        stamp = stamps["sop/standard/demo-procedure.md"]
        self.assertEqual(set(stamp), {"git_sha", "git_blob", "sha256", "byte_length"})
        self.assertEqual(stamp["git_sha"], self.git("rev-parse", "HEAD"))
        self.assertFalse((self.root / "rulebook").exists(), "persona bindings must never be touched")

    def test_write_twice_is_byte_identical(self):
        self.run_sop("write")
        first = (self.claude_skill("pantheon-sop-demo").read_bytes(),
                 (self.root / render.SOP_SKILL_BINDINGS).read_bytes())
        self.run_sop("write")
        second = (self.claude_skill("pantheon-sop-demo").read_bytes(),
                  (self.root / render.SOP_SKILL_BINDINGS).read_bytes())
        self.assertEqual(first, second)

    def test_dry_run_prints_and_writes_nothing(self):
        rc, out, _ = self.run_sop("dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("pantheon-sop-demo", out)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.codex.iterdir()), [])
        self.assertFalse((self.root / render.SOP_SKILL_BINDINGS).exists())

    def test_check_passes_after_write(self):
        self.run_sop("write")
        rc, _, _ = self.run_sop("check")
        self.assertEqual(rc, 0)

    def test_check_fails_after_an_sop_edit_and_names_both_artifacts(self):
        self.run_sop("write")
        self.edit_sop_and_commit()
        rc, _, err = self.run_sop("check")
        self.assertEqual(rc, 2)
        self.assertIn(str(self.claude_skill("pantheon-sop-demo")), err)
        self.assertIn(str(self.codex_skill("pantheon-sop-demo")), err)
        self.assertEqual(check_stale.check_sop_skills(self.root)["status"], "stale")
        # re-render restores both gates
        self.run_sop("write")
        self.assertEqual(self.run_sop("check")[0], 0)
        self.assertEqual(check_stale.check_sop_skills(self.root)["status"], "fresh")

    def test_check_fails_on_hand_edited_artifact_and_on_missing_artifact(self):
        self.run_sop("write")
        skill = self.claude_skill("pantheon-sop-demo")
        skill.write_text(skill.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
        self.assertEqual(self.run_sop("check")[0], 2)
        skill.unlink()
        self.assertEqual(self.run_sop("check")[0], 2)

    def test_harness_selection_scopes_what_is_written_and_checked(self):
        self.run_sop("write", ("claude",))
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file())
        self.assertFalse((self.codex / "skills").exists(), "claude-only run must not touch CODEX_HOME")
        self.assertEqual(self.run_sop("check", ("claude",))[0], 0)
        self.assertEqual(self.run_sop("check", ("codex",))[0], 2)
        self.assertEqual(self.run_sop("check", ("claude", "codex"))[0], 2)

    def test_pending_hooks_are_reported_never_rendered(self):
        self.write_manifest([dict(ENTRY)], [{"slug": "later", "awaiting": "merge-flow SOP"}])
        self.commit("add pending")
        rc, out, _ = self.run_sop("write")
        self.assertEqual(rc, 0)
        self.assertIn("1 pending", out)
        self.assertFalse(self.claude_skill("pantheon-sop-later").exists())
        self.assertFalse(self.codex_skill("pantheon-sop-later").exists())

    def test_orphans_are_flagged_by_check_and_pruned_by_write_but_only_ours(self):
        self.run_sop("write")
        ours = self.claude_skill("pantheon-sop-demo")
        theirs = self.claude_skill("pantheon-sop-handwritten")
        theirs.parent.mkdir(parents=True)
        theirs.write_text("---\nname: pantheon-sop-handwritten\ndescription: x\n---\nhand made\n", encoding="utf-8")
        role = self.claude_skill("pantheon-role-implementer")
        role.parent.mkdir(parents=True)
        role.write_text(render.GENERATED_MARKER + " role skill -->\n", encoding="utf-8")

        self.write_manifest([], [])  # the demo entry is retired
        rc, _, err = self.run_sop("check")
        self.assertEqual(rc, 2)
        self.assertIn("ORPHAN", err)
        self.assertIn(str(ours.parent), err)
        self.assertNotIn("handwritten", err)

        self.assertEqual(self.run_sop("write")[0], 0)
        self.assertFalse(ours.exists())
        self.assertFalse(self.codex_skill("pantheon-sop-demo").exists())
        self.assertTrue(theirs.is_file(), "a hand-written skill (no GENERATED marker) must survive")
        self.assertTrue(role.is_file(), "only the pantheon-sop-* prefix is ever pruned")
        self.assertEqual(self.run_sop("check")[0], 0)

    def test_symlinked_pantheon_sop_entries_are_never_followed_by_prune(self):
        # T-4465 REBOUND: ~/.claude/skills really holds symlinks into claude-config/skills/,
        # and a prune that followed one deleted the link TARGET's SKILL.md.
        self.run_sop("write")
        marker_text = render.GENERATED_MARKER + " -->\nfile outside the skills dir\n"
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(marker_text, encoding="utf-8")
        linked_dir = self.claude_skill("pantheon-sop-linked").parent
        linked_dir.symlink_to(outside, target_is_directory=True)

        outside_file = self.tmp / "outside2"
        outside_file.mkdir()
        (outside_file / "SKILL.md").write_text(marker_text, encoding="utf-8")
        real_dir_linked_file = self.claude_skill("pantheon-sop-filelink").parent
        real_dir_linked_file.mkdir(parents=True)
        (real_dir_linked_file / "SKILL.md").symlink_to(outside_file / "SKILL.md")

        dangling = self.claude_skill("pantheon-sop-dangling").parent
        dangling.symlink_to(self.tmp / "nowhere", target_is_directory=True)

        rc, out, err = self.run_sop("check")
        self.assertEqual(rc, 0, err)  # links are not ours: neither drift nor orphan
        self.assertNotIn("ORPHAN", err)
        for link in (linked_dir, real_dir_linked_file, dangling):
            self.assertIn(str(link), out)
        self.assertIn("not ours, skipped", out)

        self.assertEqual(self.run_sop("write")[0], 0)
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), marker_text)
        self.assertEqual((outside_file / "SKILL.md").read_text(encoding="utf-8"), marker_text)
        self.assertTrue(linked_dir.is_symlink())
        self.assertTrue((real_dir_linked_file / "SKILL.md").is_symlink())
        self.assertTrue(dangling.is_symlink())

    def test_write_never_follows_a_symlinked_active_slug_dir(self):
        # T-4488: the WRITE path wrote SKILL.md through a symlinked active pantheon-sop-<slug>.
        self.add_sop("other-procedure.md", DEMO_SOP)
        self.write_manifest([dict(ENTRY), dict(ENTRY, slug="other", sop="sop/standard/other-procedure.md")], [])
        self.commit("two entries")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("not ours\n", encoding="utf-8")
        linked_dir = self.claude_skill("pantheon-sop-demo").parent
        linked_dir.parent.mkdir(parents=True)
        linked_dir.symlink_to(outside, target_is_directory=True)

        rc, out, err = self.run_sop("write")
        self.assertEqual(rc, 0, err)
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), "not ours\n")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["SKILL.md"])
        self.assertTrue(linked_dir.is_symlink())
        self.assertIn("never written", out)
        self.assertIn(str(linked_dir), out)
        self.assertTrue(self.claude_skill("pantheon-sop-other").is_file())
        self.assertTrue(self.codex_skill("pantheon-sop-demo").is_file())

        rc, out, err = self.run_sop("check")
        self.assertEqual(rc, 0, err)  # skipped, not stale: write would never fix it
        self.assertIn(str(linked_dir), out)

    def test_write_never_follows_a_symlinked_active_skill_md(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("not ours\n", encoding="utf-8")
        real_dir = self.claude_skill("pantheon-sop-demo").parent
        real_dir.mkdir(parents=True)
        (real_dir / "SKILL.md").symlink_to(outside / "SKILL.md")

        rc, out, err = self.run_sop("write")
        self.assertEqual(rc, 0, err)
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), "not ours\n")
        self.assertTrue((real_dir / "SKILL.md").is_symlink())
        self.assertIn("never written", out)
        self.assertIn(str(real_dir / "SKILL.md"), out)
        self.assertTrue(self.codex_skill("pantheon-sop-demo").is_file())

    def test_write_without_links_emits_no_skip_note(self):
        rc, out, _ = self.run_sop("write")
        self.assertEqual(rc, 0)
        self.assertNotIn("never written", out)
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file())

    def test_prune_keeps_a_directory_that_holds_other_files(self):
        self.run_sop("write")
        ours = self.claude_skill("pantheon-sop-demo")
        (ours.parent / "notes.txt").write_text("mine", encoding="utf-8")
        self.write_manifest([], [])
        self.run_sop("write")
        self.assertFalse(ours.exists())
        self.assertTrue((ours.parent / "notes.txt").is_file())

    def test_no_manifest_means_nothing_to_do(self):
        (self.root / render.SOP_SKILLS_MANIFEST).unlink()
        self.assertEqual(self.run_sop("write")[0], 0)
        self.assertEqual(self.run_sop("check")[0], 0)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_run_codex_only_skips_persona_skills(self):
        # The fixture has no persona manifest; a codex-only run must not need one.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = render.run(self.root, self.home, "write", "codex", self.codex)
        self.assertEqual(rc, 0)
        self.assertTrue(self.codex_skill("pantheon-sop-demo").is_file())
        self.assertFalse(self.claude_skill("pantheon-sop-demo").exists())


class CheckStaleSopSkills(FixtureRepo):
    def test_constants_match_the_renderer(self):
        self.assertEqual(check_stale.SOP_SKILLS_MANIFEST, render.SOP_SKILLS_MANIFEST)
        self.assertEqual(check_stale.SOP_SKILL_BINDINGS, render.SOP_SKILL_BINDINGS)

    def test_not_configured_without_a_manifest(self):
        (self.root / render.SOP_SKILLS_MANIFEST).unlink()
        self.assertEqual(check_stale.check_sop_skills(self.root)["status"], "not-configured")

    def test_manifest_without_bindings_fails_closed(self):
        self.assertEqual(check_stale.check_sop_skills(self.root)["status"], "missing-bindings")

    def test_entry_with_no_stamp_is_stale(self):
        self.run_sop("write")
        self.add_sop("second.md", "# Second\n")
        self.write_manifest([dict(ENTRY), dict(ENTRY, slug="second", sop="sop/standard/second.md")], [])
        self.commit("second")
        report = check_stale.check_sop_skills(self.root)
        self.assertEqual(report["status"], "stale")
        self.assertEqual([d["path"] for d in report["drifted"]], ["sop/standard/second.md"])
        self.assertIn("no stamp", report["drifted"][0]["reason"])

    def test_stamp_for_a_retired_entry_is_stale_until_rerender(self):
        self.run_sop("write")
        self.write_manifest([], [])
        report = check_stale.check_sop_skills(self.root)
        self.assertEqual(report["status"], "stale")
        self.assertIn("no longer", report["drifted"][0]["reason"])

    def test_combined_check_includes_sop_skills_and_keeps_persona_semantics(self):
        self.run_sop("write")
        # persona bindings absent -> the combined check still fails closed as before
        self.assertEqual(check_stale.check(self.root)["status"], "missing-bindings")
        persona = self.root / "rulebook" / "persona-orientation"
        persona.mkdir(parents=True)
        (persona / "source-bindings.yaml").write_text("{}\n", encoding="utf-8")
        self.assertEqual(check_stale.check(self.root)["status"], "fresh")
        self.edit_sop_and_commit()
        combined = check_stale.check(self.root)
        self.assertEqual(combined["status"], "stale")
        self.assertEqual(check_stale.main(["--repo-root", str(self.root)]), 2)


class SquashMergedStamps(FixtureRepo):
    """T-4465 REBOUND: an SOP edit and its re-stamp in ONE PR, squash-merged. The squash
    drops the branch's commits, so the stamp's git_sha is no longer an ancestor of main;
    the gates must hold anyway (blob exists + blob == HEAD's blob)."""

    def test_edit_plus_restamp_in_one_pr_holds_after_a_squash_merge(self):
        base = self.git("branch", "--show-current")
        self.run_sop("write")
        self.commit("stamps on main")
        self.git("checkout", "-q", "-b", "feature")
        self.edit_sop_and_commit()  # the PR edits the SOP ...
        edit_sha = self.git("rev-parse", "HEAD")
        self.run_sop("write")  # ... and re-stamps in the same PR
        self.commit("restamp")
        assert_sop_stamps_hold(self, self.root)  # green on the branch
        stamp = yaml.safe_load((self.root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8"))
        stamp_sha = stamp["sop/standard/demo-procedure.md"]["git_sha"]
        self.assertEqual(stamp_sha, edit_sha)

        self.git("checkout", "-q", base)
        self.git("merge", "--squash", "feature")
        self.git("commit", "-q", "-m", "squash merge of feature")

        # The precondition that broke the old gate: the stamped sha is gone from main.
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", stamp_sha, "HEAD"], cwd=self.root, capture_output=True,
        )
        self.assertEqual(ancestry.returncode, 1)
        assert_sop_stamps_hold(self, self.root)
        self.assertEqual(check_stale.check_sop_skills(self.root)["status"], "fresh")

        # CI's view: a clone of main that never fetched the feature branch's commits.
        clone = self.tmp / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--single-branch", "--branch", base, f"file://{self.root}", str(clone)],
            check=True,
        )
        gone = subprocess.run(["git", "cat-file", "-e", f"{stamp_sha}^{{commit}}"], cwd=clone, capture_output=True)
        self.assertNotEqual(gone.returncode, 0, "the clone must not contain the squashed-away commit")
        assert_sop_stamps_hold(self, clone)
        self.assertEqual(check_stale.check_sop_skills(clone)["status"], "fresh")

    def test_rerender_after_a_squash_merge_leaves_the_stamps_untouched(self):
        # T-4669: the post-merge seat re-render used to re-stamp git_sha to the squash commit
        # (`git log -1 -- <sop>` on main), leaving a tracked edit in every seat clone.
        base = self.git("branch", "--show-current")
        self.run_sop("write")
        self.commit("stamps on main")
        self.git("checkout", "-q", "-b", "feature")
        self.edit_sop_and_commit()
        self.run_sop("write")
        self.commit("restamp")
        self.git("checkout", "-q", base)
        self.git("merge", "--squash", "feature")
        self.git("commit", "-q", "-m", "squash merge of feature")
        before = (self.root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8")

        self.run_sop("write")  # what each seat runs after the merge

        self.assertEqual((self.root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8"), before)
        self.assertEqual(self.git("status", "--porcelain", "--", render.SOP_SKILL_BINDINGS), "")
        assert_sop_stamps_hold(self, self.root)

    def test_a_real_sop_edit_is_still_restamped(self):
        self.run_sop("write")
        self.commit("stamps")
        old = yaml.safe_load((self.root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8"))
        self.edit_sop_and_commit()
        self.run_sop("write")
        new = yaml.safe_load((self.root / render.SOP_SKILL_BINDINGS).read_text(encoding="utf-8"))
        path = "sop/standard/demo-procedure.md"
        self.assertNotEqual(new[path]["git_blob"], old[path]["git_blob"])
        self.assertEqual(new[path]["git_sha"], self.git("rev-parse", "HEAD"))
        assert_sop_stamps_hold(self, self.root)

    def test_a_stamp_that_does_not_match_the_sop_still_fails_the_gate(self):
        # Dropping the ancestry assertion must not weaken the freshness half.
        self.run_sop("write")
        self.commit("stamps")
        self.edit_sop_and_commit()  # SOP moved on, stamp did not
        problems = sop_stamp_problems(self.root)
        self.assertTrue(any("not the SOP at HEAD" in p for p in problems), problems)

    def test_a_stamp_whose_blob_is_not_in_the_repo_fails_the_gate(self):
        self.run_sop("write")
        self.commit("stamps")
        bindings = self.root / render.SOP_SKILL_BINDINGS
        stamps = yaml.safe_load(bindings.read_text(encoding="utf-8"))
        stamps["sop/standard/demo-procedure.md"]["git_blob"] = "0" * 40
        bindings.write_text(yaml.safe_dump(stamps), encoding="utf-8")
        problems = sop_stamp_problems(self.root)
        self.assertTrue(any("blob does not exist" in p for p in problems), problems)


class RealRepoGates(TempHomes):
    """Runs against the real committed manifest and stamps: the CI drift gate."""

    def test_sop_skill_stamps_fresh_against_head(self):
        report = check_stale.check_sop_skills(REPO_ROOT)
        self.assertEqual(
            report["status"], "fresh",
            f"SOP-skill drift -- run `python3 tools/persona_orientation/render.py --harness all` "
            f"and commit sop-skill-bindings.yaml (same PR as the SOP edit is fine): {report['drifted']}",
        )

    def test_sop_skill_stamps_resolve_and_match_head(self):
        assert_sop_stamps_hold(self, REPO_ROOT)

    def test_every_manifest_hook_slug_is_active_or_pending(self):
        manifest = render.load_sop_skills_manifest(REPO_ROOT)
        known = {e["slug"] for e in manifest["skills"]} | {e["slug"] for e in manifest["pending"]}
        for slug in (
            "seat-hand-move", "merge-flow", "echo-live-deploy", "ledger-append", "dispatch-staging",
            # T-4490: picked from measured welltrod traffic (sessions per 14d window).
            "mirror-refresh", "ticket-flow", "instance-bringup",
        ):
            self.assertIn(slug, known)

    def test_t4490_welltrod_picks_are_active_skills_on_the_expected_sops(self):
        by_slug = {e["slug"]: e for e in render.load_sop_skills_manifest(REPO_ROOT)["skills"]}
        expected = {
            "mirror-refresh": "sop/standard/refreshing-an-org-039-release-mirror.md",
            "ticket-flow": "sop/standard/ticket-flow.md",
            "instance-bringup": "sop/standard/pantheon-bringup.md",
        }
        for slug, sop in expected.items():
            with self.subTest(slug=slug):
                self.assertIn(slug, by_slug, "T-4490 skill missing from the active skills list")
                self.assertEqual(by_slug[slug]["sop"], sop)

    def test_mirror_refresh_description_scopes_the_seat(self):
        # The SOP is the BUILD leg (recovery); orchestrator only publishes the handed-off bundle. The
        # description is what a non-build seat reads before loading the body.
        by_slug = {e["slug"]: e for e in render.load_sop_skills_manifest(REPO_ROOT)["skills"]}
        self.assertIn("mirror-refresh", by_slug)
        self.assertTrue(by_slug["mirror-refresh"]["description"].startswith("Recovery seat"))
        self.assertIn("orchestrator", by_slug["mirror-refresh"]["description"])

    def test_instance_bringup_renders_an_excerpt_not_the_whole_116kb_sop(self):
        # 121 KB / ~30k tokens per fire for the lowest-traffic pick is the wrong trade (T-4490
        # ruling): the skill carries the human-only pauses, prereqs and failure modes, and
        # points at the full SOP for the procedure.
        by_slug = {e["slug"]: e for e in render.load_sop_skills_manifest(REPO_ROOT)["skills"]}
        self.assertIn("instance-bringup", by_slug)
        self.assertEqual(by_slug["instance-bringup"]["body"]["extract"], "heading-region")
        artifacts, _ = render.render_sop_skills(REPO_ROOT)
        for harness in render.HARNESSES:
            with self.subTest(harness=harness):
                text = artifacts[f"sop:instance-bringup:{harness}"]
                self.assertLess(len(text.encode("utf-8")), 20_000)
                self.assertIn("> Excerpt of `sop/standard/pantheon-bringup.md`", text)
                self.assertIn("Full SOP: `sop/standard/pantheon-bringup.md`.", text)
                self.assertIn("What only a human can supply", text)

    def test_threat_model_skill_is_active_on_the_expected_sop(self):
        # T-5288: ORG-105 wiring. The skill must fire on security/privacy findings and
        # defer CVE verification to cve-response rather than overlap it.
        by_slug = {e["slug"]: e for e in render.load_sop_skills_manifest(REPO_ROOT)["skills"]}
        self.assertIn("threat-model", by_slug)
        entry = by_slug["threat-model"]
        self.assertEqual(entry["sop"], "sop/standard/applying-the-threat-model.md")
        self.assertIn("security or privacy finding", entry["description"])
        self.assertIn("cve-response", entry["description"].split("Not for", 1)[1])
        self.assertIn("ORG-105", entry["description"])

    def test_threat_model_sop_points_at_org_105_and_cve_response(self):
        text = (REPO_ROOT / "sop/standard/applying-the-threat-model.md").read_text()
        for needle in ("ORG-105", "cve-response", "## When it fires"):
            self.assertIn(needle, text)

    def test_every_active_description_carries_a_not_for_clause(self):
        # The manifest header asks for one: it is what keeps a skill from firing on a
        # neighbouring task that a different SOP (or no SOP) covers.
        for entry in render.load_sop_skills_manifest(REPO_ROOT)["skills"]:
            with self.subTest(slug=entry["slug"]):
                self.assertIn("Not for", entry["description"])

    def test_ho_seat_only_sops_say_so_in_their_description(self):
        # merge-flow and echo-live-deploy are HO-seat procedures (their SOPs' Boundary
        # sections); the description is what a non-HO seat reads before loading the body,
        # so it must tell that seat to hand off rather than act.
        manifest = render.load_sop_skills_manifest(REPO_ROOT)
        by_slug = {e["slug"]: e for e in manifest["skills"]}
        for slug in ("merge-flow", "echo-live-deploy"):
            with self.subTest(slug=slug):
                self.assertIn(slug, by_slug, "activate the hook once its SOP is on main")
                self.assertTrue(by_slug[slug]["description"].startswith("HO seat only:"))

    def test_real_manifest_renders_for_both_harnesses_without_forbidden_bytes(self):
        manifest = render.load_sop_skills_manifest(REPO_ROOT)
        artifacts, bindings = render.render_sop_skills(REPO_ROOT)
        self.assertEqual(len(artifacts), 2 * len(manifest["skills"]))
        self.assertEqual(set(bindings), {e["sop"] for e in manifest["skills"]})
        pending = {e["slug"] for e in manifest["pending"]}
        for key, text in artifacts.items():
            with self.subTest(artifact=key):
                self.assertNotIn(FORBIDDEN, text)
                meta, _ = split_frontmatter(text)
                self.assertTrue(meta["name"].startswith(render.SOP_SKILL_PREFIX))
                self.assertNotIn(meta["name"][len(render.SOP_SKILL_PREFIX):], pending)

    def test_real_manifest_dry_run_writes_nothing_anywhere(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = render.run_sop_skills(REPO_ROOT, self.home, "dry-run", ("claude", "codex"), self.codex)
        self.assertEqual(rc, 0)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.codex.iterdir()), [])


class RoleOrphans(TempHomes):
    """T-4557: generated pantheon-role-* dirs the persona manifest no longer names are
    flagged by --check and pruned by a write render (claude harness only). Temp homes only."""

    SOURCE = "rulebook/fixture.md"

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "repo"
        (self.root / "rulebook" / "persona-orientation").mkdir(parents=True)
        (self.root / self.SOURCE).write_text("fixture body\n", encoding="utf-8")
        self.write_roles(["implementer"])
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "x")

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write_roles(self, roles: list[str]) -> None:
        po = self.root / "rulebook" / "persona-orientation"
        (po / "manifest.yaml").write_text(yaml.safe_dump({
            "version": 1,
            "base_layer": {"sections": [{"id": "inline_one", "extract": "inline", "body": "hi"}]},
            "roles": [{"id": r, "delta_sections": ["d"]} for r in roles],
        }), encoding="utf-8")
        (po / "role-deltas.yaml").write_text(yaml.safe_dump({
            "version": 1,
            "roles": {r: {"d": {"extract": "full-file", "source": self.SOURCE}} for r in roles},
        }), encoding="utf-8")

    def run_persona(self, mode: str, harness: str = "claude") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(render, "write_source_bindings"):
                rc = render.run(self.root, self.home, mode, harness, self.codex)
        return rc, out.getvalue(), err.getvalue()

    def stale_generated(self, name: str) -> Path:
        path = self.claude_skill(name)
        path.parent.mkdir(parents=True)
        path.write_text(render.GENERATED_HEADER + "\nold body\n", encoding="utf-8")
        return path

    def test_retired_generated_role_skill_is_flagged_by_check_and_pruned_by_write(self):
        self.run_persona("write")
        old = self.stale_generated("pantheon-role-orchestrator")
        rc, _, err = self.run_persona("check")
        self.assertEqual(rc, 2)
        self.assertIn("ORPHAN", err)
        self.assertIn(str(old.parent), err)
        self.assertNotIn("pantheon-role-implementer", err)
        self.assertEqual(self.run_persona("write")[0], 0)
        self.assertFalse(old.exists())
        self.assertFalse(old.parent.exists())
        self.assertTrue(self.claude_skill("pantheon-role-implementer").is_file())
        self.assertEqual(self.run_persona("check")[0], 0)

    def test_dry_run_names_the_orphan_and_removes_nothing(self):
        old = self.stale_generated("pantheon-role-old")
        rc, out, _ = self.run_persona("dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would prune orphan", out)
        self.assertTrue(old.is_file())

    def test_hand_written_role_skill_without_marker_is_untouched(self):
        self.run_persona("write")
        mine = self.claude_skill("pantheon-role-mine")
        mine.parent.mkdir(parents=True)
        mine.write_text("---\nname: pantheon-role-mine\n---\nhand made\n", encoding="utf-8")
        self.assertEqual(self.run_persona("check")[0], 0)
        self.run_persona("write")
        self.assertTrue(mine.is_file())

    def test_role_named_by_the_manifest_but_not_rendered_is_never_an_orphan(self):
        self.write_roles(["implementer", "consultant_thing"])
        kept = self.stale_generated("pantheon-role-consultant-thing")
        self.run_persona("write")
        self.assertTrue(kept.is_file())
        self.assertNotIn("ORPHAN", self.run_persona("check")[2])

    def test_symlinked_role_entries_are_reported_never_touched(self):
        self.run_persona("write")
        text = render.GENERATED_HEADER + "\noutside\n"
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(text, encoding="utf-8")
        linked_dir = self.claude_skill("pantheon-role-linked").parent
        linked_dir.symlink_to(outside, target_is_directory=True)
        outside_file = self.tmp / "outside2"
        outside_file.mkdir()
        (outside_file / "SKILL.md").write_text(text, encoding="utf-8")
        file_linked = self.claude_skill("pantheon-role-filelink").parent
        file_linked.mkdir(parents=True)
        (file_linked / "SKILL.md").symlink_to(outside_file / "SKILL.md")
        dangling = self.claude_skill("pantheon-role-dangling").parent
        dangling.symlink_to(self.tmp / "nowhere", target_is_directory=True)

        rc, out, err = self.run_persona("check")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("ORPHAN", err)
        self.assertIn("not ours, skipped", out)
        for link in (linked_dir, file_linked, dangling):
            self.assertIn(str(link), out)
        self.assertEqual(self.run_persona("write")[0], 0)
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), text)
        self.assertEqual((outside_file / "SKILL.md").read_text(encoding="utf-8"), text)
        self.assertTrue(linked_dir.is_symlink())
        self.assertTrue((file_linked / "SKILL.md").is_symlink())
        self.assertTrue(dangling.is_symlink())

    def test_write_never_follows_a_symlinked_active_role_entry(self):
        # T-4488: the role-skill WRITE path has the same hole as the SOP one.
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("not ours\n", encoding="utf-8")
        linked_dir = self.claude_skill("pantheon-role-implementer").parent
        linked_dir.parent.mkdir(parents=True)
        linked_dir.symlink_to(outside, target_is_directory=True)
        outside2 = self.tmp / "outside2"
        outside2.mkdir()
        (outside2 / "SKILL.md").write_text("not ours\n", encoding="utf-8")
        base = self.claude_skill("pantheon-persona-orientation")
        base.parent.mkdir(parents=True)
        base.symlink_to(outside2 / "SKILL.md")

        rc, out, err = self.run_persona("write")
        self.assertEqual(rc, 0, err)
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), "not ours\n")
        self.assertEqual((outside2 / "SKILL.md").read_text(encoding="utf-8"), "not ours\n")
        self.assertTrue(linked_dir.is_symlink())
        self.assertTrue(base.is_symlink())
        self.assertIn("never written", out)
        self.assertIn(str(linked_dir), out)
        self.assertIn(str(base), out)
        rc, out, err = self.run_persona("check")
        self.assertEqual(rc, 0, err)

    def test_prune_keeps_a_directory_that_holds_other_files(self):
        old = self.stale_generated("pantheon-role-old")
        (old.parent / "notes.txt").write_text("mine", encoding="utf-8")
        self.run_persona("write")
        self.assertFalse(old.exists())
        self.assertTrue((old.parent / "notes.txt").is_file())

    def test_codex_skills_dir_is_never_scanned_for_role_orphans(self):
        theirs = self.codex_skill("pantheon-role-old")
        theirs.parent.mkdir(parents=True)
        theirs.write_text(render.GENERATED_HEADER + "\n", encoding="utf-8")
        _, _, err = self.run_persona("check", "all")
        self.assertNotIn("pantheon-role-old", err)
        self.run_persona("write", "all")
        self.assertTrue(theirs.is_file())


class PersonaOutputUntouched(TempHomes):
    def test_render_all_still_yields_only_the_persona_artifacts(self):
        artifacts, bindings = render.render_all(REPO_ROOT)
        self.assertEqual(
            set(artifacts),
            {"base"} | {f"role:{r}" for r in render.CLAUDE_ROLES},
        )
        self.assertFalse(any(k.startswith("sop:") for k in artifacts))
        self.assertNotIn("tools/persona_orientation/sop-skills.yaml", bindings)


class RoleScopedSkills(FixtureRepo):
    """T-5214: `roles:` scopes a skill to seats holding one of the listed role ids."""

    HO = frozenset({"head_engineering_orchestrator", "engineering_orchestrator"})
    OTHER = frozenset({"head_research_orchestrator", "research_orchestrator"})

    def setUp(self):
        super().setUp()
        scoped = {**ENTRY, "slug": "scoped", "roles": ["head_engineering_orchestrator"]}
        self.write_manifest([dict(ENTRY), scoped], [])
        self.commit("scoped")

    def run_as(self, mode: str, roles, harnesses=("claude", "codex")) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = render.run_sop_skills(self.root, self.home, mode, harnesses, self.codex, seat_roles=roles)
        return rc, out.getvalue(), err.getvalue()

    def test_renders_for_a_matching_persona_and_check_is_clean(self):
        self.assertEqual(self.run_as("write", self.HO)[0], 0)
        for skill in (self.claude_skill("pantheon-sop-scoped"), self.codex_skill("pantheon-sop-scoped")):
            self.assertTrue(skill.is_file())
        self.assertEqual(self.run_as("check", self.HO)[0], 0)

    def test_absent_and_pruned_for_a_non_matching_persona_and_check_converges(self):
        self.run_as("write", self.HO)
        hand = self.claude_skill("pantheon-sop-handwritten")
        hand.parent.mkdir(parents=True)
        hand.write_text("---\nname: x\ndescription: y\n---\nhand\n", encoding="utf-8")
        rc, _, err = self.run_as("check", self.OTHER)  # a resident copy is an ORPHAN, not STALE
        self.assertEqual(rc, 2)
        self.assertIn("ORPHAN", err)
        self.assertNotIn("STALE", err)
        self.assertEqual(self.run_as("write", self.OTHER)[0], 0)
        self.assertFalse(self.claude_skill("pantheon-sop-scoped").exists())
        self.assertFalse(self.codex_skill("pantheon-sop-scoped").exists())
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file(), "unscoped skills stay")
        self.assertTrue(hand.is_file(), "hand-written skills are never pruned")
        self.assertEqual(self.run_as("check", self.OTHER)[0], 0)

    def test_fresh_non_matching_seat_never_gets_the_skill_and_check_is_clean(self):
        self.assertEqual(self.run_as("write", self.OTHER)[0], 0)
        self.assertFalse(self.claude_skill("pantheon-sop-scoped").exists())
        self.assertEqual(self.run_as("check", self.OTHER)[0], 0)

    def test_unknown_identity_renders_everything(self):
        self.assertEqual(self.run_as("write", None)[0], 0)
        self.assertTrue(self.claude_skill("pantheon-sop-scoped").is_file())
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file())
        self.assertEqual(self.run_as("check", None)[0], 0)

    def test_stamps_do_not_move_with_the_seat(self):
        bindings = self.root / render.SOP_SKILL_BINDINGS
        self.run_as("write", None)
        full = bindings.read_text(encoding="utf-8")
        self.run_as("write", self.OTHER)
        self.assertEqual(bindings.read_text(encoding="utf-8"), full)

    def test_symlinked_excluded_skill_is_untouched(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        marker_text = render.GENERATED_MARKER + " -->\nfile outside the skills dir\n"
        (outside / "SKILL.md").write_text(marker_text, encoding="utf-8")
        self.run_as("write", self.OTHER, harnesses=("claude",))  # demo is in scope and fresh
        link = self.claude_skill("pantheon-sop-scoped").parent
        link.symlink_to(outside, target_is_directory=True)
        rc, out, err = self.run_as("check", self.OTHER, harnesses=("claude",))
        self.assertEqual(rc, 0, err)
        self.assertIn("not ours, skipped", out)
        self.assertEqual(self.run_as("write", self.OTHER, harnesses=("claude",))[0], 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "SKILL.md").read_text(encoding="utf-8"), marker_text)

    def test_dry_run_prints_only_in_scope_and_names_the_prune(self):
        self.run_as("write", self.HO)
        rc, out, _ = self.run_as("dry-run", self.OTHER)
        self.assertEqual(rc, 0)
        self.assertNotIn("===== " + str(self.claude_skill("pantheon-sop-scoped")), out)
        self.assertIn("would prune orphan", out)
        self.assertTrue(self.claude_skill("pantheon-sop-scoped").is_file())

    def test_manifest_validation_of_roles(self):
        for bad in ([], "head_x", [1], ["a", "a"], [""]):
            with self.subTest(roles=bad):
                self.write_manifest([{**ENTRY, "roles": bad}], [])
                with self.assertRaises(ValueError):
                    render.load_sop_skills_manifest(self.root)
        self.write_manifest([], [{"slug": "p", "awaiting": "x", "roles": ["a"]}])
        with self.assertRaises(ValueError):
            render.load_sop_skills_manifest(self.root)


def _role_map():
    """The same top-level `agent_role_map` module render.resolve_seat_roles imports."""
    import sys

    tools_dir = str(REPO_ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import agent_role_map

    return agent_role_map


class ResolveSeatRoles(unittest.TestCase):
    role_map = staticmethod(_role_map)

    def resolve(self, user: str):
        with mock.patch.object(render.getpass, "getuser", return_value=user):
            return render.resolve_seat_roles()

    def test_known_account_resolves_to_its_persona_roles(self):
        roles, who = self.resolve("claude")  # orchestrator's account
        self.assertIn("head_engineering_orchestrator", roles)
        self.assertIn("orchestrator", who)

    def test_unknown_account_fails_open_with_a_reason(self):
        roles, who = self.resolve("runner")
        self.assertIsNone(roles)
        self.assertIn("not a fleet account", who)

    def test_a_raising_identity_source_fails_open(self):
        agent_role_map = self.role_map()
        with mock.patch.object(agent_role_map, "functional_roles", side_effect=RuntimeError("boom")):
            roles, who = self.resolve("claude")
        self.assertIsNone(roles)
        self.assertIn("boom", who)

    def test_entry_in_scope_truth_table(self):
        scoped = {"roles": ["a"]}
        self.assertTrue(render.entry_in_scope({}, frozenset({"z"})))
        self.assertTrue(render.entry_in_scope(scoped, None))
        self.assertTrue(render.entry_in_scope(scoped, frozenset({"a", "b"})))
        self.assertFalse(render.entry_in_scope(scoped, frozenset({"b"})))


class RealManifestRoles(TempHomes):
    def test_every_roles_id_is_a_known_role(self):
        known = _role_map().known_role_ids()
        for entry in render.load_sop_skills_manifest(REPO_ROOT)["skills"]:
            for role in entry.get("roles", []):
                with self.subTest(slug=entry["slug"], role=role):
                    self.assertIn(role, known)

    def test_a_render_leaves_the_real_bindings_byte_identical(self):
        # Re-stamps a COPY of the committed bindings; the real file is never written.
        src = REPO_ROOT / render.SOP_SKILL_BINDINGS
        before = src.read_bytes()
        _, bindings = render.render_sop_skills(REPO_ROOT)
        copy = self.home / "bindings.yaml"
        copy.write_bytes(before)
        render.write_sop_skill_bindings(REPO_ROOT, bindings, copy)
        self.assertEqual(copy.read_bytes(), before)
        self.assertEqual(src.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
