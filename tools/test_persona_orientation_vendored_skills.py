"""Tests for the role-gated vendored-skill render (T-6116): tools/persona_orientation/vendored_skills.py.

Every write goes to a temp root + temp HOME; the real ~/.claude is never touched. The real-repo
gates at the bottom only READ the committed manifest, bindings and vendored tree.
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

from tools.persona_orientation import render, vendored_skills as vs

REPO_ROOT = Path(__file__).resolve().parents[1]
HEAD = frozenset({"engineering_orchestrator", "head_engineering_orchestrator"})
REGULAR = frozenset({"engineering_orchestrator"})

SKILL = "---\nname: demo\ndescription: demo skill\nallowed-tools: Read Grep Glob\n---\n\n# Demo\n\nbody\n"
REF = "# ref\n\nreference text \u2014 with unicode\n"


def make_root(tmp: Path, roles=("head_engineering_orchestrator",)) -> Path:
    root = tmp / "repo"
    src = root / "skills" / "vendored" / "up" / "demo"
    (src / "references").mkdir(parents=True)
    (src / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (src / "references" / "a.md").write_text(REF, encoding="utf-8")
    entry = {"name": "demo", "source": "skills/vendored/up/demo", "harnesses": ["claude"]}
    if roles:
        entry["roles"] = list(roles)
    (root / "tools" / "persona_orientation").mkdir(parents=True)
    (root / vs.MANIFEST).write_text(yaml.safe_dump({"skills": [entry]}), encoding="utf-8")
    return root


def run(root, home, mode, roles, stamp=True):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = vs.run_vendored(root, home, mode, roles, stamp)
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.home = self.tmp / "home"
        (self.home / ".claude" / "skills").mkdir(parents=True)
        self.root = make_root(self.tmp)
        self.dest = self.home / ".claude" / "skills" / "demo"

    def stamp(self):
        vs.write_bindings(self.root, vs.compute_bindings(self.root, vs.load_manifest(self.root)))


class RenderScope(Base):
    def test_renders_for_head_engineering_orchestrator(self):
        rc, out, _ = run(self.root, self.home, "write", HEAD)
        self.assertEqual(rc, 0, out)
        skill = (self.dest / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(vs.GENERATED_MARKER, skill)
        self.assertIn("vendored-skills.yaml", skill)
        # marker lands after the frontmatter; source text otherwise intact
        self.assertTrue(skill.startswith(SKILL.split("\n---\n")[0] + "\n---\n"))
        expected = SKILL.replace("\n---\n", "\n---\n\n" + vs.generated_line(vs.load_manifest(self.root)[0]) + "\n", 1)
        self.assertEqual(skill, expected)
        self.assertEqual((self.dest / "references" / "a.md").read_bytes(),
                         (self.root / "skills/vendored/up/demo/references/a.md").read_bytes())

    def test_source_skill_md_untouched(self):
        before = (self.root / "skills/vendored/up/demo/SKILL.md").read_bytes()
        run(self.root, self.home, "write", HEAD)
        self.assertEqual((self.root / "skills/vendored/up/demo/SKILL.md").read_bytes(), before)
        self.assertNotIn(vs.GENERATED_MARKER.encode(), before)

    def test_not_for_regular_engineering_orchestrator(self):
        rc, _, _ = run(self.root, self.home, "write", REGULAR)
        self.assertEqual(rc, 0)
        self.assertFalse(self.dest.exists())

    def test_unknown_identity_renders_nothing_and_prunes_nothing(self):
        run(self.root, self.home, "write", HEAD)
        rc, out, _ = run(self.root, self.home, "write", None)
        self.assertEqual(rc, 0)
        self.assertIn("fail-closed", out)
        self.assertTrue((self.dest / "SKILL.md").is_file(), "unknown identity must not prune")
        other = self.tmp / "home2"
        (other / ".claude" / "skills").mkdir(parents=True)
        run(self.root, other, "write", None)
        self.assertFalse((other / ".claude" / "skills" / "demo").exists())

    def test_entry_without_roles_renders_everywhere_even_unknown_identity(self):
        root = make_root(self.tmp / "fleet", roles=())
        home = self.tmp / "h3"
        (home / ".claude" / "skills").mkdir(parents=True)
        run(root, home, "write", None)
        self.assertTrue((home / ".claude" / "skills" / "demo" / "SKILL.md").is_file())

    def test_prune_removes_generated_copy_when_role_drops(self):
        run(self.root, self.home, "write", HEAD)
        self.assertTrue((self.dest / "references" / "a.md").is_file())
        rc, out, _ = run(self.root, self.home, "write", REGULAR)  # seat no longer qualifies
        self.assertEqual(rc, 0)
        self.assertFalse(self.dest.exists(), out)

    def test_prune_when_manifest_entry_reverted(self):
        run(self.root, self.home, "write", HEAD)
        other = {"name": "other", "source": "skills/vendored/up/other", "harnesses": ["claude"], "roles": ["head_engineering_orchestrator"]}
        o = self.root / "skills/vendored/up/other"
        o.mkdir(parents=True)
        (o / "SKILL.md").write_text("---\nname: other\n---\nx\n")
        (self.root / vs.MANIFEST).write_text(yaml.safe_dump({"skills": [other]}))
        self.stamp()
        run(self.root, self.home, "write", HEAD)  # `demo` entry reverted -> its copy is pruned
        self.assertFalse(self.dest.exists())
        self.assertTrue((self.home / ".claude" / "skills" / "other" / "SKILL.md").is_file())


class CheckSemantics(Base):
    def test_check_clean_after_render(self):
        run(self.root, self.home, "write", HEAD)
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 0)

    def test_check_exit2_when_qualifying_seat_missing_copy(self):
        self.stamp()
        rc, _, err = run(self.root, self.home, "check", HEAD)
        self.assertEqual(rc, 2)
        self.assertIn("missing", err)

    def test_check_exit0_on_non_qualifying_seat_expected_absent(self):
        self.stamp()
        self.assertEqual(run(self.root, self.home, "check", REGULAR)[0], 0)
        self.assertEqual(run(self.root, self.home, "check", None)[0], 0)

    def test_check_exit2_on_drifted_copy(self):
        run(self.root, self.home, "write", HEAD)
        (self.dest / "references" / "a.md").write_text("tampered\n", encoding="utf-8")
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 2)

    def test_check_exit2_on_extra_file(self):
        run(self.root, self.home, "write", HEAD)
        (self.dest / "references" / "extra.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 2)

    def test_check_exit2_on_orphan_when_role_dropped(self):
        run(self.root, self.home, "write", HEAD)
        rc, _, err = run(self.root, self.home, "check", REGULAR)
        self.assertEqual(rc, 2)
        self.assertIn("ORPHAN", err)

    def test_no_stamp_write_still_writes_copies(self):
        self.stamp()
        rc, _, _ = run(self.root, self.home, "write", HEAD, stamp=False)
        self.assertEqual(rc, 0)
        self.assertTrue((self.dest / "SKILL.md").is_file())
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 0)

    def test_write_heals_drift_and_extra(self):
        run(self.root, self.home, "write", HEAD)
        (self.dest / "references" / "a.md").write_text("tampered\n", encoding="utf-8")
        (self.dest / "references" / "extra.md").write_text("x\n", encoding="utf-8")
        run(self.root, self.home, "write", HEAD)
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 0)
        self.assertFalse((self.dest / "references" / "extra.md").exists())

    def test_dry_run_writes_nothing(self):
        self.stamp()
        rc, out, _ = run(self.root, self.home, "dry-run", HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("SKILL.md", out)
        self.assertFalse(self.dest.exists())


class Bindings(Base):
    def test_unstamped_source_refused(self):
        rc, _, err = run(self.root, self.home, "write", HEAD, stamp=False)
        self.assertEqual(rc, 2)
        self.assertIn("UNSTAMPED", err)
        self.assertFalse(self.dest.exists())

    def test_source_drift_from_stamp_refused(self):
        self.stamp()
        (self.root / "skills/vendored/up/demo/references/a.md").write_text("changed\n", encoding="utf-8")
        rc, _, err = run(self.root, self.home, "write", HEAD, stamp=False)
        self.assertEqual(rc, 2)
        self.assertIn("differs from its stamp", err)
        self.assertFalse(self.dest.exists())
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 2)

    def test_refused_source_does_not_get_installed_copy_pruned(self):
        run(self.root, self.home, "write", HEAD)
        (self.root / "skills/vendored/up/demo/references/a.md").write_text("changed\n", encoding="utf-8")
        run(self.root, self.home, "write", HEAD, stamp=False)
        self.assertTrue((self.dest / "SKILL.md").is_file())

    def test_stamp_mode_restamps(self):
        (self.root / "skills/vendored/up/demo/references/a.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual(run(self.root, self.home, "write", HEAD)[0], 0)
        self.assertIn("a.md", (self.root / vs.BINDINGS).read_text())


class Safety(Base):
    def test_symlinked_dir_of_same_name_untouched(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "SKILL.md").write_text("mine\n")
        self.dest.symlink_to(target)
        rc, out, _ = run(self.root, self.home, "write", HEAD)
        self.assertEqual(rc, 0)
        self.assertIn("skipped", out)
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual((target / "SKILL.md").read_text(), "mine\n")
        self.assertEqual(run(self.root, self.home, "check", HEAD)[0], 0)
        run(self.root, self.home, "write", REGULAR)
        self.assertTrue(self.dest.is_symlink(), "never pruned")

    def test_hand_written_dir_of_same_name_untouched(self):
        self.dest.mkdir()
        (self.dest / "SKILL.md").write_text("---\nname: demo\n---\nhand written\n")
        run(self.root, self.home, "write", HEAD)
        self.assertEqual((self.dest / "SKILL.md").read_text(), "---\nname: demo\n---\nhand written\n")
        run(self.root, self.home, "write", REGULAR)
        self.assertTrue((self.dest / "SKILL.md").is_file())

    def test_generated_sop_skill_is_not_ours_to_prune(self):
        sop = self.home / ".claude" / "skills" / "pantheon-sop-x"
        sop.mkdir()
        (sop / "SKILL.md").write_text(f"{vs.GENERATED_MARKER} from tools/persona_orientation/sop-skills.yaml + sop/standard/x.md. -->\n")
        run(self.root, self.home, "write", REGULAR)
        self.assertTrue((sop / "SKILL.md").is_file())

    def test_non_markdown_source_file_refused(self):
        (self.root / "skills/vendored/up/demo/run.sh").write_text("echo hi\n")
        with self.assertRaises(ValueError):
            vs.source_files(self.root, vs.load_manifest(self.root)[0])

    def test_symlink_in_source_refused(self):
        (self.root / "skills/vendored/up/demo/link.md").symlink_to(self.root / "skills/vendored/up/demo/SKILL.md")
        with self.assertRaises(ValueError):
            vs.source_files(self.root, vs.load_manifest(self.root)[0])

    def test_manifest_validation(self):
        bad = [
            {"name": "demo", "source": "skills/other/up/demo"},
            {"name": "demo", "source": "skills/vendored/up/../up/demo"},
            {"name": "demo", "source": "skills/vendored/up/demo", "roles": []},
            {"name": "demo", "source": "skills/vendored/up/demo", "harnesses": ["codex"]},
            {"name": "demo", "source": "skills/vendored/up/demo", "extra": 1},
            {"name": "Demo", "source": "skills/vendored/up/Demo"},
        ]
        for entry in bad:
            (self.root / vs.MANIFEST).write_text(yaml.safe_dump({"skills": [entry]}))
            with self.assertRaises(ValueError, msg=str(entry)):
                vs.load_manifest(self.root)


class ThroughRenderRun(Base):
    """render.run wires the vendored pass in for the claude harness only."""

    def _run(self, harness, mode, roles):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = render.run(self.root, self.home, mode, harness, codex_home=self.tmp / "codex", seat_roles=roles)
        return rc

    def test_claude_harness_renders_vendored(self):
        self.stamp()
        # fixture root has no persona manifest: run only the vendored pass through the module hook
        self.assertEqual(render._vendored_module().run_vendored(self.root, self.home, "write", HEAD, False), 0)
        self.assertTrue((self.dest / "SKILL.md").is_file())


class SeedShape(unittest.TestCase):
    """A public seed has render.py and vendored_skills.py but no manifest and no skills/vendored/."""

    def test_no_manifest_noops(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = Path(d) / "seed", Path(d) / "home"
            (root / "tools" / "persona_orientation").mkdir(parents=True)
            home.mkdir()
            self.assertEqual(vs.load_manifest(root), [])
            for mode in ("write", "check", "dry-run"):
                self.assertEqual(vs.run_vendored(root, home, mode, HEAD, True), 0, mode)
            self.assertFalse(list(home.iterdir()))
            self.assertFalse((root / vs.BINDINGS).exists(), "must not stamp anything either")

    def test_manifest_present_but_vendored_tree_absent_noops(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = Path(d) / "seed", Path(d) / "home"
            (root / "tools" / "persona_orientation").mkdir(parents=True)
            home.mkdir()
            (root / vs.MANIFEST).write_text(yaml.safe_dump({"skills": [
                {"name": "x", "source": "skills/vendored/up/x", "roles": ["head_engineering_orchestrator"]}]}))
            self.assertEqual(vs.load_manifest(root), [])
            self.assertEqual(vs.run_vendored(root, home, "write", HEAD, True), 0)

    def test_render_run_hook_noops_without_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = Path(d) / "seed", Path(d) / "home"
            (root / "tools").mkdir(parents=True)
            home.mkdir()
            self.assertEqual(render._vendored_module().run_vendored(root, home, "check", HEAD, False), 0)


class RealHomeGuard(Base):
    """The guard must trip BEFORE any write; pwd home is patched to a tmp dir so nothing real is ever at risk."""

    def setUp(self):
        super().setUp()
        self.fake_real = self.tmp / "fakereal"
        (self.fake_real / ".claude" / "skills").mkdir(parents=True)
        p = mock.patch.object(vs, "_real_home", return_value=self.fake_real.resolve())
        p.start()
        self.addCleanup(p.stop)

    def test_pytest_write_under_real_home_raises_and_writes_nothing(self):
        self.stamp()
        with self.assertRaises(vs.UnsafeWriteTarget):
            vs.run_vendored(self.root, self.fake_real, "write", HEAD, False)
        self.assertEqual(list((self.fake_real / ".claude" / "skills").iterdir()), [])

    def test_pytest_write_under_any_home_path_raises(self):
        with self.assertRaises(vs.UnsafeWriteTarget):
            vs.assert_safe_write_target(self.root, Path("/home/nobody-such-user/x"))

    def _git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=self.root, check=True, capture_output=True)

    def _init_repo(self, branch="main"):
        self._git("init", "-q", "-b", branch)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "i")

    def _no_pytest_marker(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("PYTEST_CURRENT_TEST", None)

    def test_live_home_write_allowed_only_on_positive_main(self):
        self._no_pytest_marker()
        self._init_repo("main")
        vs.assert_safe_write_target(self.root, self.fake_real)  # no raise

    def test_detached_head_refuses(self):
        self._no_pytest_marker()
        self._init_repo("main")
        self._git("checkout", "-q", "--detach")
        self.assertEqual(vs._current_branch(self.root), "")
        with self.assertRaises(vs.UnsafeWriteTarget):
            vs.assert_safe_write_target(self.root, self.fake_real)

    def test_git_error_refuses(self):
        self._no_pytest_marker()  # self.root is not a git repo here
        with self.assertRaises(vs.UnsafeWriteTarget):
            vs.assert_safe_write_target(self.root, self.fake_real)
        with mock.patch.object(vs.subprocess, "run", side_effect=OSError("no git")):
            with self.assertRaises(vs.UnsafeWriteTarget):
                vs.assert_safe_write_target(self.root, self.fake_real)

    def test_other_branches_refuse(self):
        self._no_pytest_marker()
        self._init_repo("main")
        for name in ("agent/T-1", "feature/x", "master"):
            self._git("checkout", "-q", "-B", name)
            with self.assertRaises(vs.UnsafeWriteTarget, msg=name):
                vs.assert_safe_write_target(self.root, self.fake_real)

    def test_main_branch_still_refused_under_pytest_marker(self):
        self._init_repo("main")  # PYTEST_CURRENT_TEST is set by pytest itself
        with self.assertRaises(vs.UnsafeWriteTarget):
            vs.assert_safe_write_target(self.root, self.fake_real)

    def test_non_live_target_unrestricted_even_off_main(self):
        self._no_pytest_marker()
        self._init_repo("main")
        self._git("checkout", "-q", "-b", "agent/T-1")
        vs.assert_safe_write_target(self.root, self.home)  # tmp home, not live

    def _render_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(render, "repo_root", return_value=self.root), \
                mock.patch.object(render, "home_root", return_value=self.fake_real), \
                mock.patch.object(render, "resolve_seat_roles", return_value=(HEAD, "fixture")), \
                mock.patch.object(render, "run_persona", return_value=0), \
                mock.patch.object(render, "run_sop_skills", return_value=0):
            return render.main(list(args))

    def test_render_main_script_path_goes_through_guard(self):
        self.stamp()
        self._no_pytest_marker()
        self._init_repo("main")
        self._git("checkout", "-q", "-b", "agent/T-9")
        with self.assertRaises(vs.UnsafeWriteTarget):
            self._render_main("--no-stamp", "--harness", "claude")
        self.assertEqual(list((self.fake_real / ".claude" / "skills").iterdir()), [])
        self.assertEqual(self._render_main("--check", "--harness", "claude"), 2)  # check never raises

    def test_production_path_main_checkout_no_stamp_renders(self):
        """What inbox_consumer does on a seat: render.py --no-stamp from the main checkout."""
        self.stamp()
        self._no_pytest_marker()
        self._init_repo("main")
        self.assertEqual(self._render_main("--no-stamp", "--harness", "claude"), 0)
        self.assertTrue((self.fake_real / ".claude" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertEqual(self._render_main("--check", "--harness", "claude"), 0)

    def test_check_and_dry_run_never_trip_the_guard(self):
        self.stamp()
        self.assertEqual(vs.run_vendored(self.root, self.fake_real, "dry-run", HEAD, False), 0)
        self.assertEqual(vs.run_vendored(self.root, self.fake_real, "check", HEAD, False), 2)  # missing copy, no raise

    def test_tmp_home_write_still_allowed(self):
        self.assertEqual(run(self.root, self.home, "write", HEAD)[0], 0)


@unittest.skipUnless((REPO_ROOT / vs.VENDORED_ROOT).is_dir(), "skills/vendored/ is absent (public seed)")
class RealRepo(unittest.TestCase):
    def test_real_manifest_valid_and_gated_to_orchestrator_role(self):
        entries = vs.load_manifest(REPO_ROOT)
        self.assertEqual(len(entries), 6)
        for e in entries:
            self.assertEqual(e["roles"], ["head_engineering_orchestrator"], e["name"])
            self.assertEqual(e["harnesses"], ["claude"])

    def test_real_bindings_match_tree(self):
        bindings = vs.load_bindings(REPO_ROOT)
        for e in vs.load_manifest(REPO_ROOT):
            self.assertIsNone(vs.binding_problem(REPO_ROOT, e, bindings), e["name"])
        self.assertEqual(set(bindings), {e["name"] for e in vs.load_manifest(REPO_ROOT)})

    def test_real_sources_render_and_skill_names_match(self):
        for e in vs.load_manifest(REPO_ROOT):
            files = vs.render_entry(REPO_ROOT, e)
            self.assertIn("SKILL.md", files)

    def test_gated_role_resolves_to_orchestrator_only(self):
        from tools import agent_role_map
        self.assertIn("head_engineering_orchestrator", agent_role_map.functional_roles("orchestrator"))
        for p in ("artisan", "mason", "recovery", "specialist"):
            self.assertNotIn("head_engineering_orchestrator", agent_role_map.functional_roles(p), p)


if __name__ == "__main__":
    unittest.main()
