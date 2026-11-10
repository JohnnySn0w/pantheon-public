"""Canary tests for worker isolation lockdown (T-0357).

Verifies that claude-config agent personas (implementer.md + reviewer.md)
contain the required cwd pre-flight guard text. If these tests fail, the
persona files have lost the bail logic — regenerate or restore from T-0357.

Run from repo root:
    python3 -m unittest tools.test_worker_isolation_canary -v

Design note: tests read the persona files from this repo's in-repo harness
(harness/claude/agents/, ORG-091 / T-4750), so they always run. Until T-4763
they looked for a ~/claude-config clone and skipped when none was readable
(T-1294's CI skip).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

# Canonical main paths that the pre-flight guard must recognise.
# Mirrors the list in the persona files.
CANONICAL_MAIN_PATHS = [
    "/home/claude/pantheon-config",
    "/home/claude/echo-prismatic",
    "/home/claude/revenue-project",
    "/home/claude/pantheon-pwa",
    "/home/claude/pantheon-bridge",
]

# Required phrases in the persona files (exact substrings).
BAIL_PHRASE = "main-path discipline violation"
POSITIVE_SHAPE_PHRASE = "/tmp/"
FIRST_ACTION_PHRASE = "git rev-parse --show-toplevel"
PREFLIGHT_SECTION = "Pre-flight: cwd discipline"


AGENTS_DIR = Path(__file__).resolve().parent.parent / "harness" / "claude" / "agents"
DISPATCH_SOP = (
    Path(__file__).resolve().parent.parent
    / "sop"
    / "standard"
    / "staging-a-worker-dispatch.md"
)


class TestPersonaCwdGuard(unittest.TestCase):
    """Structural checks: persona files contain required cwd-guard text."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.implementer = (AGENTS_DIR / "implementer.md").read_text()
        cls.reviewer = (AGENTS_DIR / "reviewer.md").read_text()
        cls.dispatch_sop = DISPATCH_SOP.read_text()

    # --- implementer.md checks ---

    def test_implementer_has_preflight_section(self) -> None:
        self.assertIn(
            PREFLIGHT_SECTION,
            self.implementer,
            "implementer.md missing 'Pre-flight: cwd discipline' section",
        )

    def test_implementer_has_first_action_command(self) -> None:
        self.assertIn(
            FIRST_ACTION_PHRASE,
            self.implementer,
            "implementer.md missing 'git rev-parse --show-toplevel' first-action command",
        )

    def test_implementer_has_bail_phrase(self) -> None:
        self.assertIn(
            BAIL_PHRASE,
            self.implementer,
            f"implementer.md missing bail phrase '{BAIL_PHRASE}'",
        )

    def test_implementer_has_positive_shape(self) -> None:
        self.assertIn(
            POSITIVE_SHAPE_PHRASE,
            self.implementer,
            "implementer.md missing positive cwd shape ('/tmp/')",
        )

    def test_implementer_lists_all_canonical_paths(self) -> None:
        missing = [p for p in CANONICAL_MAIN_PATHS if p not in self.implementer]
        self.assertEqual(
            missing,
            [],
            f"implementer.md missing canonical paths: {missing}",
        )

    # --- reviewer.md checks ---

    def test_reviewer_has_preflight_section(self) -> None:
        self.assertIn(
            PREFLIGHT_SECTION,
            self.reviewer,
            "reviewer.md missing 'Pre-flight: cwd discipline' section",
        )

    def test_reviewer_has_first_action_command(self) -> None:
        self.assertIn(
            FIRST_ACTION_PHRASE,
            self.reviewer,
            "reviewer.md missing 'git rev-parse --show-toplevel' first-action command",
        )

    def test_reviewer_has_bail_phrase(self) -> None:
        self.assertIn(
            BAIL_PHRASE,
            self.reviewer,
            f"reviewer.md missing bail phrase '{BAIL_PHRASE}'",
        )

    def test_reviewer_has_positive_shape(self) -> None:
        self.assertIn(
            POSITIVE_SHAPE_PHRASE,
            self.reviewer,
            "reviewer.md missing positive cwd shape ('/tmp/')",
        )

    def test_reviewer_lists_all_canonical_paths(self) -> None:
        missing = [p for p in CANONICAL_MAIN_PATHS if p not in self.reviewer]
        self.assertEqual(
            missing,
            [],
            f"reviewer.md missing canonical paths: {missing}",
        )

    def test_reviewer_uses_operator_directed_fable_model(self) -> None:
        frontmatter = self.reviewer.split("---", 2)[1]
        self.assertIn("\nmodel: claude-fable-5-1\n", frontmatter)
        self.assertNotIn("\nmodel: sonnet\n", frontmatter)

    def test_dispatch_fallback_preserves_reviewer_fable_exception(self) -> None:
        self.assertIn("Reviewers run on Fable 5.1", self.dispatch_sop)
        self.assertIn(
            'pass `model: "claude-fable-5-1"` for reviewer Agent calls',
            self.dispatch_sop,
        )
        self.assertIn(
            '`model: "sonnet"` for all other worker Agent calls',
            self.dispatch_sop,
        )


class TestCanonicalPathList(unittest.TestCase):
    """Unit-level: the canonical path list itself is structurally correct."""

    def test_all_canonical_paths_under_home_claude(self) -> None:
        for path in CANONICAL_MAIN_PATHS:
            self.assertTrue(
                path.startswith("/home/claude/"),
                f"Canonical path not under /home/claude/: {path}",
            )

    def test_tmp_not_in_canonical_paths(self) -> None:
        for path in CANONICAL_MAIN_PATHS:
            self.assertNotIn(
                "/tmp/",
                path,
                f"/tmp/ path incorrectly listed as canonical main path: {path}",
            )

    def test_bail_phrase_contains_discipline_violation(self) -> None:
        """Smoke: bail phrase shape matches what persona docs must say."""
        self.assertIn("discipline violation", BAIL_PHRASE)
        self.assertIn("main-path", BAIL_PHRASE)


if __name__ == "__main__":
    unittest.main()
