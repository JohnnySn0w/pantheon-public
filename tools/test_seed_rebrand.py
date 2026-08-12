#!/usr/bin/env python3
"""Unittest coverage for tools/seed_rebrand.py (T-2072) — the seed rebrand tool.

Focuses on the boundary correctness that bit the first hand-rolled version: a
keep-token (`pantheon-config`) must be protected, but a SUPERSTRING identifier
(`pantheon-config-sync`, a service) must still rename. unittest (pytest absent).
"""
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import seed_rebrand as sr


class ContentRebrandTest(unittest.TestCase):
    def _rules(self, brand="wish", hub=None):
        return sr.build_rules(brand, hub)

    def test_generic_prefix_renames(self):
        out = sr.rebrand_text("pantheon-grooming and pantheon_helper",
                              self._rules(), sr.DEFAULT_KEEP)
        self.assertEqual(out, "wish-grooming and wish_helper")

    def test_keep_list_protected(self):
        for tok in ("pantheon-config", "pantheon-pwa", "pantheon-public", "pantheon-bridge"):
            out = sr.rebrand_text(f"ref {tok} here", self._rules(), sr.DEFAULT_KEEP)
            self.assertIn(tok, out, f"{tok} must survive the rename")

    def test_boundary_superstring_still_renames(self):
        # THE bug: protecting pantheon-config must NOT protect pantheon-config-sync.
        out = sr.rebrand_text("pantheon-config-sync restarts pantheon-config",
                              self._rules(), sr.DEFAULT_KEEP)
        self.assertEqual(out, "wish-config-sync restarts pantheon-config")

    def test_dotted_keep_still_protected(self):
        # pantheon-config.md (the hub file) stays; boundary is the '.'
        out = sr.rebrand_text("see pantheon-config.md", self._rules(), sr.DEFAULT_KEEP)
        self.assertEqual(out, "see pantheon-config.md")

    def test_rename_hub_overrides_config_protection(self):
        # merged seed+hub fork: pantheon-config -> the hub brand, generic still applies
        out = sr.rebrand_text("pantheon-config hosts pantheon-grooming",
                              self._rules(hub="wish-factory"),
                              keep=[k for k in sr.DEFAULT_KEEP if k != "pantheon-config"])
        self.assertEqual(out, "wish-factory hosts wish-grooming")


class FilenameRebrandTest(unittest.TestCase):
    def test_unit_file_renames(self):
        self.assertEqual(sr.rename_basename("pantheon-grooming.service", "wish", None, sr.DEFAULT_KEEP),
                         "wish-grooming.service")
        self.assertEqual(sr.rename_basename("pantheon-context-fill-monitor@.timer", "wish", None, sr.DEFAULT_KEEP),
                         "wish-context-fill-monitor@.timer")

    def test_keep_file_protected(self):
        self.assertEqual(sr.rename_basename("pantheon-config.md", "wish", None, sr.DEFAULT_KEEP),
                         "pantheon-config.md")

    def test_superstring_file_still_renames(self):
        self.assertEqual(sr.rename_basename("pantheon-config-sync.service", "wish", None, sr.DEFAULT_KEEP),
                         "wish-config-sync.service")

    def test_underscore_prefix(self):
        self.assertEqual(sr.rename_basename("pantheon_helper.py", "wish", None, sr.DEFAULT_KEEP),
                         "wish_helper.py")


class EndToEndDryRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "instance.yaml").write_text("instance: {}\n")
        (self.root / "tools").mkdir()
        (self.root / "tools" / "pantheon-grooming.service").write_text("runs pantheon-grooming; keep pantheon-pwa\n")
        (self.root / "tools" / "pantheon-config-sync.service").write_text("the pantheon-config-sync service\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_changes_nothing(self):
        report = sr.run(self.root, "wish", apply=False)
        # dry-run: files unchanged on disk
        self.assertIn("runs pantheon-grooming", (self.root / "tools" / "pantheon-grooming.service").read_text())
        self.assertTrue((self.root / "tools" / "pantheon-grooming.service").exists())
        # but the report plans the changes
        self.assertTrue(any("pantheon-grooming.service" in s for s, d in report["renamed"]))
        self.assertTrue(any("config-sync" in d for s, d in report["renamed"]))

    def test_apply_renames_and_protects(self):
        report = sr.run(self.root, "wish", apply=True)  # tmp dir is not a git tree -> plain rename
        self.assertTrue((self.root / "tools" / "wish-grooming.service").exists())
        self.assertTrue((self.root / "tools" / "wish-config-sync.service").exists())
        self.assertFalse((self.root / "tools" / "pantheon-grooming.service").exists())
        content = (self.root / "tools" / "wish-grooming.service").read_text()
        self.assertIn("runs wish-grooming", content)
        self.assertIn("keep pantheon-pwa", content)  # keep-list survived


class CorpusProtectionTest(unittest.TestCase):
    """§2: the design-history corpus is skipped by default (content + filenames)."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "instance.yaml").write_text("instance: {}\n")
        for d in ("knowledge", "lessons", "adr", "bugs"):
            (self.root / d).mkdir()
        # corpus prose that references the framework by name (design history)
        (self.root / "knowledge" / "history.md").write_text("pantheon-grooming was designed here\n")
        (self.root / "adr" / "ORG-020-restart-for-all-pantheon-agents.md").write_text(
            "see [[ORG-020]] and pantheon-grooming\n")
        # a normal (non-corpus) file that MUST rebrand
        (self.root / "tools").mkdir()
        (self.root / "tools" / "pantheon-grooming.service").write_text("runs pantheon-grooming\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_corpus_skipped_by_default(self):
        report = sr.run(self.root, "wish", apply=True)
        # corpus content untouched, corpus ADR filename untouched
        self.assertIn("pantheon-grooming", (self.root / "knowledge" / "history.md").read_text())
        self.assertTrue((self.root / "adr" / "ORG-020-restart-for-all-pantheon-agents.md").exists())
        self.assertIn("[[ORG-020]]", (self.root / "adr" / "ORG-020-restart-for-all-pantheon-agents.md").read_text())
        # but the non-corpus service DID rebrand
        self.assertTrue((self.root / "tools" / "wish-grooming.service").exists())
        # report must not list any corpus path
        self.assertFalse(any(c.startswith(("knowledge/", "adr/", "lessons/", "bugs/"))
                             for c in report["content_changed"]))

    def test_include_corpus_opts_in(self):
        report = sr.run(self.root, "wish", apply=True, include_corpus=True)
        self.assertIn("wish-grooming", (self.root / "knowledge" / "history.md").read_text())
        # ADR filename still NOT renamed (it doesn't START with pantheon-; the ^anchor protects it)
        self.assertTrue((self.root / "adr" / "ORG-020-restart-for-all-pantheon-agents.md").exists())
        # ...but its inline pantheon-grooming ref was rewritten under --include-corpus
        self.assertIn("wish-grooming", (self.root / "adr" / "ORG-020-restart-for-all-pantheon-agents.md").read_text())


class SelfExclusionTest(unittest.TestCase):
    """§5: the tool must not rewrite its own source (idempotence for reconcile)."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "instance.yaml").write_text("instance: {}\n")
        (self.root / "tools").mkdir()
        # a stand-in for the tool's own source, containing the tokens it replaces
        (self.root / "tools" / "seed_rebrand.py").write_text(
            'DEFAULT_KEEP = ["pantheon-config"]\n# renames pantheon-grooming\n')
        (self.root / "tools" / "test_seed_rebrand.py").write_text("assert 'pantheon-grooming'\n")
        (self.root / "tools" / "other.py").write_text("runs pantheon-grooming\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_tool_source_untouched(self):
        report = sr.run(self.root, "wish", apply=True)
        self.assertIn("pantheon-grooming", (self.root / "tools" / "seed_rebrand.py").read_text())
        self.assertIn("pantheon-grooming", (self.root / "tools" / "test_seed_rebrand.py").read_text())
        # a normal file still rebrands
        self.assertIn("wish-grooming", (self.root / "tools" / "other.py").read_text())
        self.assertNotIn("tools/seed_rebrand.py", report["content_changed"])


class VerifyTest(unittest.TestCase):
    """§6: post-run verification surfaces split-brain, prefix-overlap, protected hits."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "instance.yaml").write_text("instance: {}\n")
        (self.root / "tools").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_split_brain_state_dir_warns(self):
        # some refs on wish, some still pantheon, for the SAME state root
        (self.root / "tools" / "a.py").write_text(
            'x = Path.home() / ".local" / "state" / "wish"\n'
            'y = "~/.local/state/pantheon/autonomy_off"\n')
        report = sr.run(self.root, "wish", apply=False)
        self.assertTrue(any("split-brain on the state dir" in w for w in report["verify"]["warnings"]))

    def test_no_split_brain_when_consistent(self):
        # bare pantheon state dir untouched by the tool -> both shapes on pantheon -> no warn
        (self.root / "tools" / "a.py").write_text(
            'x = Path.home() / ".local" / "state" / "pantheon"\n'
            'y = "~/.local/state/pantheon/autonomy_off"\n')
        report = sr.run(self.root, "wish", apply=True)
        self.assertFalse(any("split-brain" in w for w in report["verify"]["warnings"]))

    def test_prefix_overlap_noted_with_hub(self):
        report = sr.run(self.root, "wish", rename_hub="wish-factory", apply=False,
                        keep=[k for k in sr.DEFAULT_KEEP if k != "pantheon-config"])
        self.assertTrue(any("is a prefix of" in n for n in report["verify"]["notes"]))

    def test_dynamic_env_var_warns(self):
        (self.root / "tools" / "a.py").write_text('v = os.environ.get(f"PANTHEON_{name}")\n')
        report = sr.run(self.root, "wish", apply=False)
        self.assertTrue(any("dynamically-constructed" in w for w in report["verify"]["warnings"]))

    def test_protected_hits_reported(self):
        (self.root / "tools" / "a.py").write_text("ref pantheon-config and pantheon-pwa\n")
        report = sr.run(self.root, "wish", apply=False)
        hits = report["verify"]["protected_hits"]
        self.assertEqual(hits["pantheon-config"], 1)
        self.assertEqual(hits["pantheon-pwa"], 1)


if __name__ == "__main__":
    unittest.main()
