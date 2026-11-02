"""T-3316: the pwa-release-mirror option-2 scrub (the operator's T-2131 ruling)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pwa_mirror_scrub as s  # noqa: E402


class TestPersonaRule(unittest.TestCase):
    def test_ruling_names_map_verbatim(self):
        self.assertEqual(s.scrub_text("Orchestrator, Specialist and Recovery")[0],
                         "Orchestrator, Researcher and Recovery")

    def test_case_forms_and_identifier_components(self):
        text = "orchestrator ORCHESTRATOR recovery_seat kino-recovery specialist's orchestratorSeat RECOVERY_DIR"
        self.assertEqual(s.scrub_text(text)[0],
                         "orchestrator ORCHESTRATOR recovery_seat kino-recovery "
                         "researcher's orchestratorSeat RECOVERY_DIR")

    def test_other_personas_use_the_seed_vocabulary(self):
        self.assertEqual(s.scrub_text("mason artisan archivist consultant watchman")[0],
                         "mason artisan archivist consultant watchman")

    def test_letter_embedded_words_are_untouched(self):
        for w in ("toward", "forward", "award", "warden", "Wardrobe", "artisans", "steward"):
            self.assertEqual(s.scrub_text(w)[0], w)


class TestDropRules(unittest.TestCase):
    def test_commit_ref_drops_the_sha(self):
        self.assertEqual(s.scrub_text("drafted at pantheon-config@3a0160c (2026-05-12).")[0],
                         "drafted at pantheon-config (2026-05-12).")

    def test_msg_refs_drop_with_their_joiners(self):
        cases = {
            "Per Operator msg_1179 — three-step": "Per Operator — three-step",
            "(Operator, Telegram msg_1834 + msg_1836, 2026-05-21)": "(Operator, Telegram, 2026-05-21)",
            "- msg_1422 + msg_1424 + msg_1425 — Operator's originating ask": "- Operator's originating ask",
            "## Ratification answers (Operator, 2026-05-05 msg_662)": "## Ratification answers (Operator, 2026-05-05)",
            "(Telegram msg_662). Post": "(Telegram). Post",
        }
        for src, want in cases.items():
            self.assertEqual(s.scrub_text(src)[0], want, src)

    def test_retired_role_label_maps_but_role_ids_do_not(self):
        self.assertEqual(s.scrub_text("author: orchestrator; role:head_engineering_orchestrator")[0],
                         "author: orchestrator; role:head_engineering_orchestrator")

    def test_ticket_and_adr_ids_are_kept(self):
        text = "T-1987 ADR-050 ORG-006"
        self.assertEqual(s.scrub_text(text)[0], text)

    def test_common_word_swarm_is_not_the_hostname(self):
        self.assertEqual(s.scrub_text("swarm-wide capacity events")[0], "swarm-wide capacity events")

    def test_layout_outside_dropped_refs_is_untouched(self):
        # Tidying must never join lines or collapse alignment: multi-line calls,
        # aligned columns and box art in a file that also has a persona name.
        text = ("const x = f(\n  orchestrator,\n  recovery\n);\n"
                "│ Telegram: [● ok]    │\n"
                "| a  | b   |\n"
                "see msg_12 here\n")
        want = ("const x = f(\n  orchestrator,\n  recovery\n);\n"
                "│ Telegram: [● ok]    │\n"
                "| a  | b   |\n"
                "see here\n")
        self.assertEqual(s.scrub_text(text)[0], want)

    def test_idempotent(self):
        once = s.scrub_text("Orchestrator msg_1 pantheon-config@abcdef1 recovery")[0]
        self.assertEqual(s.scrub_text(once)[0], once)


class TestTree(unittest.TestCase):
    def test_renames_persona_basenames_and_skips_git(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tests").mkdir()
            (root / "tests" / "health_dropbox_recovery.test.ts").write_text("recovery\n")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("recovery\n")
            report = s.scrub_tree(root)
            self.assertTrue((root / "tests" / "health_dropbox_recovery.test.ts").exists())
            self.assertEqual((root / "tests" / "health_dropbox_recovery.test.ts").read_text(), "recovery\n")
            self.assertEqual((root / ".git" / "config").read_text(), "recovery\n")
            self.assertEqual(report["renamed"], 1)

    def test_counts_report_zero_after_scrub(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.md").write_text("Orchestrator msg_9 pantheon-config@abcdef1\n")
            s.scrub_tree(root)
            self.assertEqual(s.count_classes(root), {k: 0 for k in s.CLASS_PATTERNS})


class TestFixups(unittest.TestCase):
    def test_fixup_applies_once(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "t.ts").write_text('a("x"); a("x2");')
            self.assertEqual(s.apply_fixups(root, [("t.ts", 'a("x");', 'a("y");', 1)]), 1)
            self.assertEqual((root / "t.ts").read_text(), 'a("y"); a("x2");')

    def test_fixup_fails_closed_when_upstream_moved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "t.ts").write_text("changed upstream")
            with self.assertRaises(s.FixupError):
                s.apply_fixups(root, [("t.ts", 'a("x");', 'a("y");', 1)])
            with self.assertRaises(s.FixupError):
                s.apply_fixups(root, [("missing.ts", "a", "b", 1)])


if __name__ == "__main__":
    unittest.main()
