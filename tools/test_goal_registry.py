"""Tests for the canonical goal-key registry (T-5679)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools import goal_registry as gr
from tools import ticket_purpose as tp


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "goal_registry"

# The north-star keys ratified for T-5679 (orchestrator, 2026-10-27).
EXPECTED_REPO_KEYS = frozenset({
    "grand-project",
    "echo-prismatic",
    "revenue-project",
    "pantheon",
    "pantheon-pwa",
    "vibecamp-scheduler",
    "vibecamp-trials-tracker",
    "personal-site",
})


class GoalRegistryParserTests(unittest.TestCase):
    def test_happy_path_entries_keys_headings_and_lines(self) -> None:
        path = FIXTURES / "goal_headings_ok.md"
        entries = gr.goal_entries(REPO_ROOT, path=path)
        self.assertEqual(
            [(entry.key, entry.line) for entry in entries],
            [("root-project", 8), ("alpha-goal", 13), ("beta-site", 18), ("gamma-site", 19)],
        )
        self.assertEqual(entries[0].heading, "⭐ THE ROOT PROJECT — a.k.a. ROOT")
        self.assertEqual(entries[2].heading, entries[3].heading)
        self.assertEqual(
            gr.load_goal_keys(REPO_ROOT, path=path),
            frozenset({"root-project", "alpha-goal", "beta-site", "gamma-site"}),
        )
        self.assertEqual(
            gr.registry_status(REPO_ROOT, path=path),
            (frozenset({"root-project", "alpha-goal", "beta-site", "gamma-site"}), None),
        )

    def test_repo_root_resolves_canonical_relpath(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            target = repo / gr.GOALS_RELPATH
            target.parent.mkdir(parents=True)
            target.write_bytes((FIXTURES / "goal_headings_ok.md").read_bytes())
            self.assertEqual(gr.goals_path(repo), target)
            self.assertIn("alpha-goal", gr.load_goal_keys(repo))

    def test_duplicate_key_fails_loudly_naming_both_lines(self) -> None:
        with self.assertRaises(gr.GoalRegistryError) as ctx:
            gr.load_goal_keys(REPO_ROOT, path=FIXTURES / "goal_headings_dup.md")
        message = str(ctx.exception)
        self.assertIn("duplicate goal key 'alpha-goal'", message)
        self.assertIn(":7:", message)
        self.assertIn("line 4", message)
        self.assertNotIsInstance(ctx.exception, gr.GoalRegistryMissing)

    def test_marker_not_directly_under_heading_fails_loudly(self) -> None:
        with self.assertRaises(gr.GoalRegistryError) as ctx:
            gr.load_goal_keys(REPO_ROOT, path=FIXTURES / "goal_headings_misplaced.md")
        self.assertIn("not directly under", str(ctx.exception))

    def test_marker_before_any_heading_fails_loudly(self) -> None:
        with self.assertRaises(gr.GoalRegistryError):
            gr.parse_goal_entries("<!-- goal-key: alpha-goal -->\n## alpha-goal\n")

    def test_malformed_markers_fail_loudly(self) -> None:
        malformed = (
            "<!-- goal-key: Alpha-Goal -->",  # uppercase
            "<!-- goal-key: alpha_goal -->",  # underscore
            "<!-- goal-key: -alpha -->",  # leading hyphen
            "<!-- goal-key: alpha--goal -->",  # empty segment
            "<!--goal-key: alpha-goal-->",  # spacing
            "<!-- goal-key:alpha-goal -->",  # spacing
            "<!-- goal-key: alpha-goal beta -->",  # two tokens
            "<!-- goal-key: -->",  # empty
            "<!-- goalkey: alpha-goal -->",  # near-miss spelling
            "<!-- GOAL-KEY: alpha-goal -->",  # case
            "<!-- goal-key: alpha-goal --> trailing",  # trailing text
        )
        for marker in malformed:
            with self.subTest(marker=marker):
                with self.assertRaises(gr.GoalRegistryError) as ctx:
                    gr.parse_goal_entries(f"## alpha-goal — heading\n{marker}\n")
                self.assertIn("malformed goal-key marker", str(ctx.exception))

    def test_missing_file_is_a_named_error_not_a_silent_empty_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with self.assertRaises(gr.GoalRegistryMissing) as ctx:
                gr.load_goal_keys(repo)
            self.assertIn("goal registry missing", str(ctx.exception))
            keys, error = gr.registry_status(repo)
            self.assertEqual(keys, frozenset())
            self.assertIsNotNone(error)
            self.assertIn(str(gr.goals_path(repo)), error)

    def test_registry_status_carries_parse_errors(self) -> None:
        keys, error = gr.registry_status(REPO_ROOT, path=FIXTURES / "goal_headings_dup.md")
        self.assertEqual(keys, frozenset())
        self.assertIn("duplicate goal key", error or "")

    def test_file_with_no_markers_is_an_error_not_an_empty_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            target = repo / gr.GOALS_RELPATH
            target.parent.mkdir(parents=True)
            target.write_text("# Goals\n\n## alpha-goal — no marker\n", encoding="utf-8")
            keys, error = gr.registry_status(repo)
            self.assertEqual(keys, frozenset())
            self.assertIn("no goal-key markers found", error or "")

    def test_heading_without_marker_is_not_an_error_for_the_parser(self) -> None:
        # Coverage of every north-star heading is pinned by the repo format
        # test below, not by the parser (prose headings may be added freely).
        self.assertEqual(gr.parse_goal_entries("## prose heading\n\ntext\n"), ())

    def test_goal_key_regex_matches_ticket_purpose(self) -> None:
        self.assertEqual(gr.GOAL_KEY_RE.pattern, tp.GOAL_KEY_RE.pattern)


class RepoGoalsFormatPinTests(unittest.TestCase):
    """Pins the marker format against the repo's own tracked goals.md."""

    MISSING_MARKERS_HINT = (
        "T-5679: the tracked goals.md does not carry the ratified goal-key "
        "markers yet. Apply the marker-only diff (one '<!-- goal-key: <key> -->' "
        "line directly under each north-star '## ' heading) as the HO-ratified "
        "hand-curated edit; this test is expected to fail until then."
    )

    def setUp(self) -> None:
        self.path = gr.goals_path(REPO_ROOT)
        self.assertTrue(self.path.is_file(), f"tracked goals registry missing: {self.path}")
        try:
            self.entries = gr.goal_entries(REPO_ROOT)
        except gr.GoalRegistryError as exc:
            self.fail(f"{self.MISSING_MARKERS_HINT} ({exc})")

    def test_every_north_star_heading_has_a_marker(self) -> None:
        headings = [
            line[3:].strip()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ]
        self.assertTrue(headings, "goals.md has no '## ' north-star headings")
        marked = {entry.heading for entry in self.entries}
        unmarked = [heading for heading in headings if heading not in marked]
        self.assertEqual(unmarked, [], self.MISSING_MARKERS_HINT)

    def test_keys_unique_and_slug_shaped(self) -> None:
        keys = [entry.key for entry in self.entries]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertRegex(key, r"^[a-z0-9-]+$")
            self.assertRegex(key, gr.GOAL_KEY_RE)

    def test_load_goal_keys_returns_exactly_the_ratified_keys(self) -> None:
        self.assertEqual(gr.load_goal_keys(REPO_ROOT), EXPECTED_REPO_KEYS, self.MISSING_MARKERS_HINT)


if __name__ == "__main__":
    unittest.main()
