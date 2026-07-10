"""Tests for tools/_pane_predicates.py — pane-context predicate helpers.

Covers:
- path_in_canonical_pantheon_dirs: True/False per documented canonical paths
- path_in_agent_cwd: True/False per agent worktree prefix
- extract_trust_dialog_path: extracts folder path from trust-dialog pane text
- extract_file_dialog_path: extracts file path from create/edit/overwrite dialog text

Dialog text shapes are grounded in the Specialist catalog
(knowledge/watch/reports/2026-05-20/claude-code-wedge-sentinels.md §3.1, §3.3)
and the leaked claude-code TrustDialog.tsx / FileWritePermissionRequest.tsx
source shape confirmed there.
"""

from __future__ import annotations

import unittest

from tools._pane_predicates import (
    extract_file_dialog_path,
    extract_trust_dialog_path,
    path_in_agent_cwd,
    path_in_canonical_pantheon_dirs,
)


class TestPathInCanonicalPantheonDirs(unittest.TestCase):
    """path_in_canonical_pantheon_dirs returns True iff path is under a
    canonical Pantheon dir or a /tmp/<agent>-T-NNNN worktree prefix."""

    # --- True cases (canonical home dirs) ---

    def test_pantheon_config_claude(self):
        self.assertTrue(path_in_canonical_pantheon_dirs("/home/claude/pantheon-config"))

    def test_pantheon_config_claude_subpath(self):
        self.assertTrue(path_in_canonical_pantheon_dirs("/home/claude/pantheon-config/tools"))

    def test_pantheon_config_research(self):
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/home/claude-research/pantheon-config")
        )

    def test_pantheon_config_fallback(self):
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/home/claude-fallback/pantheon-config")
        )

    def test_echo_prismatic(self):
        self.assertTrue(path_in_canonical_pantheon_dirs("/home/claude/echo-prismatic"))

    def test_echo_prismatic_subpath(self):
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/home/claude/echo-prismatic/src/main")
        )

    def test_pantheon_pwa(self):
        self.assertTrue(path_in_canonical_pantheon_dirs("/home/claude/pantheon-pwa"))

    def test_pantheon_pwa_subpath(self):
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/home/claude/pantheon-pwa/public")
        )

    # --- True cases (/tmp worktree prefixes) ---

    def test_tmp_agent_ticket_worktree(self):
        """Standard /tmp/<agent>-T-NNNN pattern is canonical."""
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/tmp/pantheon-config-T-0479")
        )

    def test_tmp_agent_ticket_worktree_subpath(self):
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/tmp/pantheon-config-T-0479/tools/inbox_consumer.py")
        )

    def test_tmp_recovery_worktree(self):
        self.assertTrue(path_in_canonical_pantheon_dirs("/tmp/echo-prismatic-T-0123"))

    def test_tmp_hyphenated_agent(self):
        """claude-research agent worktrees follow the same /tmp/ pattern."""
        self.assertTrue(
            path_in_canonical_pantheon_dirs("/tmp/pantheon-config-T-0001")
        )

    # --- False cases ---

    def test_random_tmp_dir(self):
        self.assertFalse(path_in_canonical_pantheon_dirs("/tmp/random_stuff"))

    def test_tmp_no_ticket_suffix(self):
        """A /tmp path without the T-NNNN ticket suffix is not canonical."""
        self.assertFalse(path_in_canonical_pantheon_dirs("/tmp/pantheon-config-scratch"))

    def test_home_other_user(self):
        self.assertFalse(path_in_canonical_pantheon_dirs("/home/other/pantheon-config"))

    def test_home_claude_other_repo(self):
        """A non-listed repo under /home/claude/ is not canonical."""
        self.assertFalse(path_in_canonical_pantheon_dirs("/home/claude/some-other-repo"))

    def test_root_path(self):
        self.assertFalse(path_in_canonical_pantheon_dirs("/"))

    def test_empty_string(self):
        self.assertFalse(path_in_canonical_pantheon_dirs(""))

    def test_relative_path(self):
        """Relative paths are not canonical (canonical paths are absolute)."""
        self.assertFalse(path_in_canonical_pantheon_dirs("pantheon-config"))


class TestPathInAgentCwd(unittest.TestCase):
    """path_in_agent_cwd returns True iff `path` starts with the agent's
    worktree prefix (canonical home dir or /tmp/... worktree)."""

    def test_orchestrator_home_dir_match(self):
        self.assertTrue(
            path_in_agent_cwd("/home/claude/pantheon-config/tools/foo.py", "orchestrator")
        )

    def test_orchestrator_echo_prismatic(self):
        self.assertTrue(
            path_in_agent_cwd("/home/claude/echo-prismatic/src/main.rs", "orchestrator")
        )

    def test_orchestrator_pantheon_pwa(self):
        self.assertTrue(
            path_in_agent_cwd("/home/claude/pantheon-pwa/index.html", "orchestrator")
        )

    def test_specialist_home_dir_match(self):
        self.assertTrue(
            path_in_agent_cwd(
                "/home/claude-research/pantheon-config/knowledge/watch/reports/2026-05-20/foo.md",
                "specialist",
            )
        )

    def test_recovery_home_dir_match(self):
        self.assertTrue(
            path_in_agent_cwd(
                "/home/claude-fallback/pantheon-config/tools/inbox_consumer.py",
                "recovery",
            )
        )

    def test_orchestrator_tmp_worktree(self):
        """An agent's /tmp worktree is in their cwd for the ticket."""
        self.assertTrue(
            path_in_agent_cwd("/tmp/pantheon-config-T-0479/tools/foo.py", "orchestrator")
        )

    def test_specialist_tmp_worktree(self):
        self.assertTrue(
            path_in_agent_cwd("/tmp/pantheon-config-T-0100/notes.md", "specialist")
        )

    def test_recovery_tmp_worktree(self):
        self.assertTrue(
            path_in_agent_cwd("/tmp/echo-prismatic-T-0200/src/lib.rs", "recovery")
        )

    def test_wrong_agent_home(self):
        """/home/claude/... is NOT in specialist's cwd."""
        self.assertFalse(
            path_in_agent_cwd("/home/claude/pantheon-config/tools/foo.py", "specialist")
        )

    def test_wrong_agent_research_in_orchestrator(self):
        self.assertFalse(
            path_in_agent_cwd(
                "/home/claude-research/pantheon-config/foo.py", "orchestrator"
            )
        )

    def test_non_canonical_path(self):
        self.assertFalse(
            path_in_agent_cwd("/home/other/random/file.py", "orchestrator")
        )

    def test_unknown_agent_returns_false(self):
        """Unknown agent name — no home dirs match — returns False."""
        self.assertFalse(
            path_in_agent_cwd("/home/claude/pantheon-config/foo.py", "unknown_agent")
        )

    def test_tmp_worktree_is_cross_agent(self):
        """/tmp/ worktrees are accessible to all agents (they may be working
        across agents' worktrees). Returns True for any agent."""
        self.assertTrue(
            path_in_agent_cwd("/tmp/pantheon-config-T-0999/foo.py", "orchestrator")
        )
        self.assertTrue(
            path_in_agent_cwd("/tmp/pantheon-config-T-0999/foo.py", "specialist")
        )


class TestExtractTrustDialogPath(unittest.TestCase):
    """extract_trust_dialog_path extracts the folder path from a workspace-trust
    dialog pane text.

    Dialog shape from TrustDialog.tsx (T-0473 §3.1):
      Do you trust the files in this folder?
      /path/to/folder
      ❯ 1. Yes, I trust this folder
        2. No, exit Claude Code
    """

    def test_current_variant_trust_dialog(self):
        pane = (
            " Do you trust the files in this folder?\n"
            " /home/claude/pantheon-config\n"
            " ❯ 1. Yes, I trust this folder\n"
            "   2. No, exit Claude Code\n"
        )
        result = extract_trust_dialog_path(pane)
        self.assertEqual(result, "/home/claude/pantheon-config")

    def test_legacy_variant_trust_dialog(self):
        """Legacy variant (seen in issues #6797, #2147)."""
        pane = (
            " Yes, I trust this folder\n"
            " /home/claude-research/pantheon-config\n"
            " ❯ 1. Trust\n"
            "   2. Do not trust\n"
        )
        result = extract_trust_dialog_path(pane)
        self.assertEqual(result, "/home/claude-research/pantheon-config")

    def test_tmp_worktree_path(self):
        pane = (
            " Do you trust the files in this folder?\n"
            " /tmp/pantheon-config-T-0479\n"
            " ❯ 1. Yes, I trust this folder\n"
            "   2. No, exit Claude Code\n"
        )
        result = extract_trust_dialog_path(pane)
        self.assertEqual(result, "/tmp/pantheon-config-T-0479")

    def test_path_with_trailing_whitespace(self):
        """Path may have leading/trailing whitespace in the pane capture."""
        pane = (
            " Do you trust the files in this folder?\n"
            "   /home/claude/echo-prismatic  \n"
            " ❯ 1. Yes, I trust this folder\n"
        )
        result = extract_trust_dialog_path(pane)
        self.assertEqual(result, "/home/claude/echo-prismatic")

    def test_no_trust_dialog_returns_none(self):
        """Non-trust-dialog pane text returns None."""
        pane = (
            " Do you want to create /home/claude/pantheon-config/new_file.py?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
        )
        result = extract_trust_dialog_path(pane)
        self.assertIsNone(result)

    def test_empty_pane_returns_none(self):
        result = extract_trust_dialog_path("")
        self.assertIsNone(result)

    def test_no_path_in_dialog_returns_none(self):
        """Trust dialog sentinel present but no extractable path."""
        pane = "Do you trust the files in this folder?\n❯ 1. Yes\n"
        result = extract_trust_dialog_path(pane)
        self.assertIsNone(result)


class TestExtractFileDialogPath(unittest.TestCase):
    """extract_file_dialog_path extracts the file path from create/edit/overwrite
    dialog pane text.

    Dialog shapes from FileWritePermissionRequest.tsx / FileEditPermissionRequest.tsx
    (T-0473 §3.3):
      Do you want to create /path/to/file.py?
      Do you want to make this edit to /path/to/file.py?
      Do you want to overwrite /path/to/file.py?
    """

    def test_create_dialog(self):
        pane = (
            " Do you want to create /home/claude/pantheon-config/tools/new_module.py?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
        )
        result = extract_file_dialog_path(pane)
        self.assertEqual(result, "/home/claude/pantheon-config/tools/new_module.py")

    def test_edit_dialog(self):
        pane = (
            " Do you want to make this edit to /home/claude/pantheon-config/tools/inbox_consumer.py?\n"
            " ❯ 1. Yes\n"
            "   2. Yes, and allow Claude to edit its own settings for this session\n"
            "   3. No\n"
        )
        result = extract_file_dialog_path(pane)
        self.assertEqual(
            result, "/home/claude/pantheon-config/tools/inbox_consumer.py"
        )

    def test_overwrite_dialog(self):
        pane = (
            " Do you want to overwrite /tmp/pantheon-config-T-0479/notes.md?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
        )
        result = extract_file_dialog_path(pane)
        self.assertEqual(result, "/tmp/pantheon-config-T-0479/notes.md")

    def test_path_with_spaces_in_dirname(self):
        """Question mark terminates the path — strip it."""
        pane = " Do you want to create /home/claude/pantheon-config/my file.py?\n"
        result = extract_file_dialog_path(pane)
        self.assertEqual(result, "/home/claude/pantheon-config/my file.py")

    def test_no_file_dialog_returns_none(self):
        pane = (
            " Do you trust the files in this folder?\n"
            " /home/claude/pantheon-config\n"
        )
        result = extract_file_dialog_path(pane)
        self.assertIsNone(result)

    def test_empty_pane_returns_none(self):
        result = extract_file_dialog_path("")
        self.assertIsNone(result)

    def test_tmp_worktree_create(self):
        pane = (
            " Do you want to create /tmp/echo-prismatic-T-0200/src/new.rs?\n"
            " ❯ 1. Yes\n"
        )
        result = extract_file_dialog_path(pane)
        self.assertEqual(result, "/tmp/echo-prismatic-T-0200/src/new.rs")


if __name__ == "__main__":
    unittest.main()
