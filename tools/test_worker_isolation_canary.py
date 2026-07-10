"""Canary tests for worker isolation lockdown (T-0357).

Verifies that claude-config agent personas (implementer.md + reviewer.md)
contain the required cwd pre-flight guard text. If these tests fail, the
persona files have lost the bail logic — regenerate or restore from T-0357.

Run from repo root:
    python3 -m unittest tools.test_worker_isolation_canary -v

Design note: tests read the persona files from the claude-config submodule
path inside echo-prismatic. We locate claude-config via:
  1. /home/claude/claude-config (canonical VPS path — preferred)
  2. /home/claude/echo-prismatic/claude-config (submodule fallback)
If neither exists, tests skip with an informative message (CI without the
repo checked out).
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


def _find_claude_config() -> Path | None:
    """Return the first existing claude-config agents directory, or None."""
    candidates = [
        Path("/home/claude/claude-config/agents"),
        Path("/home/claude/echo-prismatic/claude-config/agents"),
    ]
    for candidate in candidates:
        # T-0891: is_dir() raises PermissionError (not False) when the path is
        # inside another agent's 700-mode home — e.g. Recovery (claude-fallback)
        # statting /home/claude/... while running the full-discover pre-commit
        # gate. Treat any access error as "not found" so the class cleanly SKIPS
        # on accounts that can't reach it, rather than crashing collection.
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


@unittest.skipUnless(_find_claude_config() is not None, "claude-config not found; skip")
class TestPersonaCwdGuard(unittest.TestCase):
    """Structural checks: persona files contain required cwd-guard text."""

    @classmethod
    def setUpClass(cls) -> None:
        agents_dir = _find_claude_config()
        cls.implementer = (agents_dir / "implementer.md").read_text()
        cls.reviewer = (agents_dir / "reviewer.md").read_text()

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
