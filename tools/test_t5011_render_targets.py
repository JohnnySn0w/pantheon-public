"""T-5011 PR-C: the SOP-skill render-target registry (T-4924 plan §2.4, §2.5 gaps 1-3).

claude/codex output must not move; hermes is opt-in, renders into its category
layout under --hermes-skills-root, and stamps its bindings seat-locally; an
optional `harnesses:` list on an entry narrows where it renders.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path, PurePath

import yaml

from tools.harness_adapter import hermes_render
from tools.persona_orientation import render
from tools.test_persona_orientation_sop_skills import ENTRY, FixtureRepo, REPO_ROOT, split_frontmatter

FORBIDDEN = "HERME" + "S.md"  # never contiguous in source


class FixtureWithHermes(FixtureRepo):
    def setUp(self):
        super().setUp()
        self.hermes_root = self.tmp / "hermes-skills"

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(render, "repo_root", return_value=self.root), \
                mock.patch.object(render, "home_root", return_value=self.home):
            try:
                rc = render.main(list(args))
            except SystemExit as exc:  # argparse
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def hermes_skill(self, slug: str) -> Path:
        return self.hermes_root / "pantheon" / f"pantheon-sop-{slug}" / "SKILL.md"

    def hermes_bindings(self) -> Path:
        return self.home / ".local" / "state" / "pantheon" / "hermes-render" / "sop-skill-bindings.yaml"


class RegistryTests(unittest.TestCase):
    def test_all_is_claude_then_codex(self):
        self.assertEqual(render.expand_harness("all"), ("claude", "codex"))
        self.assertEqual(render.expand_harness("all"), render.HARNESSES)
        self.assertEqual(list(render.RENDER_TARGETS), ["claude", "codex", "hermes"])
        self.assertFalse(render.RENDER_TARGETS["hermes"].in_all)

    def test_single_and_unknown(self):
        for name in ("claude", "codex", "hermes"):
            self.assertEqual(render.expand_harness(name), (name,))
        with self.assertRaises(ValueError):
            render.expand_harness("other")

    def test_hermes_constants_match_the_adapter_target(self):
        target = hermes_render.HermesRenderTarget()
        self.assertEqual(render.HERMES_SKILLS_ROOT, hermes_render.SKILLS_ROOT)
        self.assertEqual(render.HERMES_SKILLS_ROOT, target.install_path("skill-root", account_home=Path("/h")))
        self.assertEqual(render.HERMES_SKILL_CATEGORY, hermes_render.SKILL_CATEGORY)
        self.assertEqual(PurePath(render.HERMES_BINDINGS_RELDIR), hermes_render.BINDINGS_RELDIR)
        self.assertEqual(
            PurePath(render.RENDER_TARGETS["hermes"].skill_relpath("demo")), target.skill_relpath("demo")
        )
        self.assertEqual(render.RENDER_TARGETS["hermes"].frontmatter(dict(ENTRY)), target.skill_frontmatter(ENTRY))
        self.assertEqual(
            render.RENDER_TARGETS["hermes"].bindings_path(Path("/r"), render.RenderContext(Path("/h"), Path("/c"))).parent,
            target.bindings_dir(repo_root=Path("/r"), account_home=Path("/h")),
        )

    def test_claude_and_codex_targets_wrap_the_existing_functions(self):
        ctx = render.RenderContext(Path("/h"), Path("/c"))
        self.assertEqual(render.RENDER_TARGETS["claude"].skill_root(ctx), Path("/h/.claude/skills"))
        self.assertEqual(render.RENDER_TARGETS["codex"].skill_root(ctx), Path("/c/skills"))
        for name in ("claude", "codex"):
            self.assertEqual(render.RENDER_TARGETS[name].bindings_path(Path("/r"), ctx), Path("/r") / render.SOP_SKILL_BINDINGS)
            self.assertEqual(render.RENDER_TARGETS[name].frontmatter(dict(ENTRY)), render.SOP_ADAPTERS[name][0](dict(ENTRY)))


class HermesRenderTests(FixtureWithHermes):
    def test_hermes_category_layout_and_seat_local_bindings(self):
        repo_bindings = self.root / render.SOP_SKILL_BINDINGS
        rc, out, err = self.run_cli("--harness", "hermes", "--hermes-skills-root", str(self.hermes_root))
        self.assertEqual(rc, 0, err)
        skill = self.hermes_skill("demo")
        self.assertTrue(skill.is_file())
        front, _ = split_frontmatter(skill.read_text(encoding="utf-8"))
        self.assertEqual(front, {"name": "pantheon-sop-demo", "description": ENTRY["description"]})
        self.assertTrue(self.hermes_bindings().is_file())
        self.assertFalse(repo_bindings.exists(), "hermes must never stamp the repo")
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.codex / "skills").exists())
        # persona skills are claude-only
        self.assertNotIn("pantheon-persona-orientation", out)
        self.assertNotIn(FORBIDDEN, skill.read_text(encoding="utf-8"))

    def test_hermes_check_and_drift(self):
        args = ("--harness", "hermes", "--hermes-skills-root", str(self.hermes_root))
        self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertEqual(self.run_cli("--check", *args)[0], 0)
        self.hermes_skill("demo").write_text("tampered\n", encoding="utf-8")
        rc, _, err = self.run_cli("--check", *args)
        self.assertEqual(rc, 2)
        self.assertIn("STALE", err)

    def test_hermes_orphan_in_category_dir_is_found_and_pruned(self):
        args = ("--harness", "hermes", "--hermes-skills-root", str(self.hermes_root))
        self.run_cli(*args)
        orphan = self.hermes_skill("gone")
        orphan.parent.mkdir(parents=True)
        shutil.copy(self.hermes_skill("demo"), orphan)
        rc, _, err = self.run_cli("--check", *args)
        self.assertEqual(rc, 2)
        self.assertIn("ORPHAN", err)
        self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertFalse(orphan.exists())

    def test_all_never_touches_hermes(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = render.run_sop_skills(
                self.root, self.home, "dry-run", render.expand_harness("all"), self.codex, self.hermes_root
            )
        self.assertEqual(rc, 0)
        out = out.getvalue()
        self.assertNotIn(str(self.hermes_root), out)


class HarnessesSelectionTests(FixtureWithHermes):
    def test_entry_renders_only_for_listed_harnesses(self):
        self.write_manifest([dict(ENTRY, harnesses=["hermes"])], [])
        self.assertEqual(self.run_sop("write")[0], 0)
        self.assertFalse(self.claude_skill("pantheon-sop-demo").exists())
        self.assertFalse(self.codex_skill("pantheon-sop-demo").exists())
        self.assertEqual(self.run_cli("--harness", "hermes", "--hermes-skills-root", str(self.hermes_root))[0], 0)
        self.assertTrue(self.hermes_skill("demo").is_file())

    def test_deselected_entry_becomes_an_orphan_for_that_harness(self):
        self.assertEqual(self.run_sop("write")[0], 0)
        self.assertTrue(self.codex_skill("pantheon-sop-demo").is_file())
        self.write_manifest([dict(ENTRY, harnesses=["claude"])], [])
        rc, _, err = self.run_sop("check")
        self.assertEqual(rc, 2)
        self.assertIn(str(self.codex / "skills" / "pantheon-sop-demo"), err)
        self.assertEqual(self.run_sop("write")[0], 0)
        self.assertTrue(self.claude_skill("pantheon-sop-demo").is_file())
        self.assertFalse(self.codex_skill("pantheon-sop-demo").exists())

    def test_absent_key_is_every_harness(self):
        self.assertTrue(render.entry_selects(dict(ENTRY), "hermes"))
        self.assertTrue(render.entry_selects(dict(ENTRY), "claude"))

    def test_invalid_selection_is_refused(self):
        for bad in ([], ["other"], "claude", ["claude", "claude"]):
            with self.subTest(bad=bad):
                self.write_manifest([dict(ENTRY, harnesses=bad)], [])
                with self.assertRaises(ValueError):
                    render.load_sop_skills_manifest(self.root)


class RealRepoTests(unittest.TestCase):
    def test_no_live_entry_uses_harnesses(self):
        manifest = render.load_sop_skills_manifest(REPO_ROOT)
        self.assertFalse([e["slug"] for e in manifest["skills"] if "harnesses" in e])

    def test_write_mode_bindings_are_unchanged(self):
        """The real repo's stamps, rewritten through the generalized writer at the
        default (repo) path, come back byte-identical."""
        _, bindings = render.render_sop_skills(REPO_ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / render.SOP_SKILL_BINDINGS
            target.parent.mkdir(parents=True)
            original = (REPO_ROOT / render.SOP_SKILL_BINDINGS).read_bytes()
            target.write_bytes(original)
            ctx = render.RenderContext(root / "home", root / "codex")
            paths = {render.RENDER_TARGETS[h].bindings_path(root, ctx) for h in ("claude", "codex")}
            self.assertEqual(paths, {target})
            render.write_sop_skill_bindings(root, bindings, target)
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
