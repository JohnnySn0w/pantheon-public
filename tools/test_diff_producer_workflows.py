"""T-4227: pin the CI diff producers so every diff-walking check sees every added line.

A diff producer without --text hides a binary-rendered file behind
"Binary files ... differ" (T-4211); textconv/ext-diff drivers could rewrite the
bytes a check scans. A lint whose parser moved into a shared module must also
re-run when that module changes (T-4202).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"

RAW_BYTES_FLAGS = ("--text", "--no-textconv", "--no-ext-diff")

_DIFF_CMD_RE = re.compile(r'diff_cmd=(git [^"]*)"')


def _diff_cmds(workflow: str) -> list[str]:
    return _DIFF_CMD_RE.findall((WORKFLOWS / workflow).read_text(encoding="utf-8"))


def _trigger_paths(workflow: str) -> dict[str, list[str]]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as boolean True.
    triggers = doc.get("on", doc.get(True))
    return {event: spec.get("paths", []) for event, spec in triggers.items()}


class AdrScopingLintWorkflowTests(unittest.TestCase):
    WORKFLOW = "adr-scoping-lint.yml"

    def test_every_diff_producer_emits_raw_bytes(self) -> None:
        cmds = _diff_cmds(self.WORKFLOW)
        self.assertEqual(len(cmds), 3, cmds)  # PR, first-push, and push forms
        for cmd in cmds:
            for flag in RAW_BYTES_FLAGS:
                self.assertIn(flag, cmd.split(), f"{flag} missing from: {cmd}")

    def test_path_triggers_include_the_shared_parser(self) -> None:
        for event, paths in _trigger_paths(self.WORKFLOW).items():
            for needed in ("tools/adr_scoping_lint.py", "tools/hermes_diff_scan.py", "tools/_git_utils.py"):
                self.assertIn(needed, paths, f"{event} trigger lacks {needed}")


class RegistryLintWorkflowTests(unittest.TestCase):
    """T-5073: the ORG-081 D-10 creation check walks a diff through the shared parser."""

    WORKFLOW = "registry-lint.yml"

    def test_every_diff_producer_emits_raw_bytes(self) -> None:
        cmds = _diff_cmds(self.WORKFLOW)
        self.assertEqual(len(cmds), 3, cmds)  # PR, first-push, and push forms
        for cmd in cmds:
            for flag in RAW_BYTES_FLAGS:
                self.assertIn(flag, cmd.split(), f"{flag} missing from: {cmd}")

    def test_path_triggers_include_the_shared_parser(self) -> None:
        for event, paths in _trigger_paths(self.WORKFLOW).items():
            for needed in ("tools/germinate/registry/**", "tools/hermes_diff_scan.py", "tools/_git_utils.py"):
                self.assertIn(needed, paths, f"{event} trigger lacks {needed}")


class HermesAuditWorkflowTests(unittest.TestCase):
    WORKFLOW = "hermes-audit.yml"

    def test_every_diff_producer_emits_raw_bytes(self) -> None:
        cmds = _diff_cmds(self.WORKFLOW)
        self.assertEqual(len(cmds), 2, cmds)  # PR form + push (per-commit log) form
        for cmd in cmds:
            for flag in RAW_BYTES_FLAGS:
                self.assertIn(flag, cmd.split(), f"{flag} missing from: {cmd}")

    def test_push_form_diffs_merges_per_parent_explicitly(self) -> None:
        # `-m` follows the runner's log.diffMerges config (first-parent would drop
        # the other parents); `--diff-merges=separate` does not.
        (push,) = [c for c in _diff_cmds(self.WORKFLOW) if c.startswith("git log")]
        self.assertIn("--diff-merges=separate", push.split())
        self.assertNotIn("-m", push.split())


class NeutralizeSeedHermesTemplateTests(unittest.TestCase):
    """The seed's opt-in hermes-audit template gets the same raw-bytes producers."""

    def test_every_diff_producer_emits_raw_bytes(self) -> None:
        from tools.neutralize_seed import HERMES_WORKFLOW_TEMPLATE

        cmds = _DIFF_CMD_RE.findall(HERMES_WORKFLOW_TEMPLATE)
        self.assertEqual(len(cmds), 3, cmds)
        for cmd in cmds:
            for flag in RAW_BYTES_FLAGS:
                self.assertIn(flag, cmd.split(), f"{flag} missing from: {cmd}")


if __name__ == "__main__":
    unittest.main()
