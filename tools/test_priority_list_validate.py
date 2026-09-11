"""Tests for `ticket.py priority-list validate` (T-3020 BT4, ORG-078
§Priority-list-drift-hygiene) + its pre-commit hook wiring.

Self-contained (mirrors `test_ticket_list_priority_sort.py`'s `_wrap`/
`_write_ticket`/patch-module-globals fixture style) rather than importing
from `test_ticket.py`, matching this tree's per-BT test-file isolation
convention.

Run from repo root:
    python3 -m unittest tools.test_priority_list_validate -v
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import priority_list_parse, ticket

REPO_ROOT = Path(__file__).resolve().parent.parent
PRE_COMMIT_HOOK = REPO_ROOT / ".githooks" / "pre-commit"


def _wrap(yaml_body: str) -> str:
    """Sentinel + ```yaml``` fence-wrapped synthetic priority-list.md text."""
    return (
        "# preamble\n\n"
        "<!-- PRIORITY-LIST-START -->\n"
        "```yaml\n"
        f"{yaml_body}\n"
        "```\n"
        "<!-- PRIORITY-LIST-END -->\n\n"
    )


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str,
    requester: str = "orchestrator",
    target: str = "recovery",
    created: str = "2026-05-02T01:30:00Z",
    title: str = "Example ticket",
    labels: list[str] | None = None,
    parent_ticket: str | None = None,
) -> None:
    fm_lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
        f"parent_ticket: {parent_ticket if parent_ticket else 'null'}",
        "depends_on: []",
    ]
    if labels:
        fm_lines.append(f"labels: [{', '.join(labels)}]")
    fm_lines.append("---")
    body_lines = [
        "",
        f"# {title}",
        "",
        "## Ask",
        "",
        "## Context",
        "",
        "## Transition log",
        "",
        f"- {created} [orchestrator] filed in inbox",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines + body_lines), encoding="utf-8")


class _PriorityListValidateTestBase(unittest.TestCase):
    """Shared fixture: tmp tickets/ tree + tmp priority-list.md, patched onto
    module globals so tests never touch the real repo's rulebook/priority-list.md."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets_root = self.root / "tickets"
        for s in ticket.STATES:
            (self.tickets_root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.tickets_root)
        self._tickets_patch.start()

        self.priority_list_path = self.root / "priority-list.md"
        self._priority_list_patch = patch.object(
            priority_list_parse, "DEFAULT_PRIORITY_LIST_PATH", self.priority_list_path
        )
        self._priority_list_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._priority_list_patch.stop()
        self._tmp.cleanup()

    def _set_priority_list(self, yaml_body: str) -> None:
        self.priority_list_path.write_text(_wrap(yaml_body), encoding="utf-8")

    def _set_priority_list_raw(self, text: str) -> None:
        self.priority_list_path.write_text(text, encoding="utf-8")

    def _seed(self, **kwargs) -> None:
        tid = kwargs["tid"]
        state = kwargs.pop("state", "inbox")
        _write_ticket(self.tickets_root / state / f"{tid}.md", state=state, **kwargs)

    def _run_validate(self) -> tuple[int, str, str]:
        ns = MagicMock()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ticket.cmd_priority_list_validate(ns)
        return rc, out.getvalue(), err.getvalue()


class TestValidateHealthy(_PriorityListValidateTestBase):
    def test_healthy_list_all_labels_used_exits_0(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\npriority-2:\n  - beta\n")
        self._seed(tid="T-0001", labels=["alpha"])
        self._seed(tid="T-0002", labels=["beta"])
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertIn("ok:", out)
        self.assertEqual(err, "")


class TestValidateParseErrors(_PriorityListValidateTestBase):
    def test_missing_sentinels_exits_1(self) -> None:
        self._set_priority_list_raw("# no sentinels here at all\n")
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 1)
        self.assertIn("sentinel-missing", err)

    def test_unparseable_yaml_exits_1_with_yaml_parse_fail_class(self) -> None:
        self._set_priority_list_raw(
            "<!-- PRIORITY-LIST-START -->\n"
            "```yaml\n"
            "priority-1: [\n"
            "```\n"
            "<!-- PRIORITY-LIST-END -->\n"
        )
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 1)
        self.assertIn("yaml-parse-fail", err)


class TestValidateDeadLabels(_PriorityListValidateTestBase):
    def test_label_in_zero_tickets_warns_exit_2(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - ghost-label\n")
        self._seed(tid="T-0001", labels=["alpha"])
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("ghost-label", err)
        self.assertIn("probably-dead", err)

    def test_label_on_single_open_ticket_is_not_dead(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - dev-only\n")
        self._seed(tid="T-0001", labels=["alpha"])
        self._seed(tid="T-0002", state="active", labels=["dev-only"])
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("dev-only", err)

    def test_label_only_on_done_ticket_is_not_dead(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - archived-label\n")
        self._seed(tid="T-0001", labels=["alpha"])
        self._seed(tid="T-0002", state="done", labels=["archived-label"])
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("archived-label", err)


# ---------------------------------------------------------------------------
# T-3080 / ORG-078 §Priority-list-drift-hygiene: high-volume heuristic +
# sample-matching output + threshold configurability. Semantic-drift check
# deferred per T-3424 (first-cut static-fanout implementation false-fired
# on legitimate cross-cutting labels; needs proper design pass).
# ---------------------------------------------------------------------------


class TestValidateHighVolume(_PriorityListValidateTestBase):
    def test_label_over_threshold_warns_exit_2(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - busy-label\n")
        self._seed(tid="T-0001", labels=["alpha"])
        n = ticket.PRIORITY_LIST_HIGH_VOLUME_THRESHOLD + 1
        for i in range(n):
            self._seed(tid=f"T-1{i:03d}", labels=["busy-label"])
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("busy-label", err)
        self.assertIn(str(n), err)
        self.assertIn("high-volume", err)

    def test_label_at_or_under_threshold_not_flagged(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - quiet-label\n")
        self._seed(tid="T-0001", labels=["alpha"])
        n = ticket.PRIORITY_LIST_HIGH_VOLUME_THRESHOLD
        for i in range(n):
            self._seed(tid=f"T-1{i:03d}", labels=["quiet-label"])
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("high-volume", err)


class TestValidateSampleMatching(_PriorityListValidateTestBase):
    def test_sample_output_includes_ids_and_titles(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(tid="T-0001", labels=["alpha"], title="First alpha ticket")
        self._seed(tid="T-0002", labels=["alpha"], title="Second alpha ticket")
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertIn("sample matching tickets per label:", out)
        self.assertIn("T-0001: First alpha ticket", out)
        self.assertIn("T-0002: Second alpha ticket", out)

    def test_sample_output_truncates_and_marks_empty(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - ghost\n")
        n = ticket.PRIORITY_LIST_SAMPLE_SIZE + 2
        for i in range(n):
            self._seed(
                tid=f"T-5{i:03d}", labels=["alpha"], title=f"alpha ticket {i}",
            )
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 2)  # ghost is probably-dead
        shown = [
            line for line in out.splitlines()
            if line.strip().startswith("T-5") and ":" in line
        ]
        self.assertEqual(len(shown), ticket.PRIORITY_LIST_SAMPLE_SIZE)
        self.assertIn("(no matching tickets)", out)


class TestValidateThresholdConfigurability(_PriorityListValidateTestBase):
    def test_high_volume_threshold_is_read_live_from_module_constant(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - twice-label\n")
        self._seed(tid="T-0001", labels=["alpha"])
        self._seed(tid="T-6000", labels=["twice-label"])
        self._seed(tid="T-6001", labels=["twice-label"])
        # 2 tickets is well under the default (30) — confirm no warning yet.
        rc, _, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("high-volume", err)
        # Lower the module constant and re-run: same corpus now warns.
        with patch.object(ticket, "PRIORITY_LIST_HIGH_VOLUME_THRESHOLD", 1):
            rc, _, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("twice-label", err)
        self.assertIn("high-volume", err)


# ---------------------------------------------------------------------------
# Pre-commit hook integration (T-3020 BT4): load .githooks/pre-commit as a
# module (mirrors test_hermes_hook.py's _load_pre_commit_hook_module) and
# exercise its _priority_list_validate_findings wiring directly, mocking
# _staged_files() rather than spinning up a real git repo.
# ---------------------------------------------------------------------------


def _load_pre_commit_hook_module():
    loader = importlib.machinery.SourceFileLoader(
        "_pre_commit_hook_under_test_plv", str(PRE_COMMIT_HOOK)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestPreCommitHookWiring(_PriorityListValidateTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.hook = _load_pre_commit_hook_module()

    def test_not_staged_is_cheap_skip(self) -> None:
        findings = self.hook._priority_list_validate_findings(["some/other/file.py"])
        self.assertEqual(findings, [])

    def test_staged_and_healthy_returns_no_blocking_findings(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(tid="T-0001", labels=["alpha"])
        findings = self.hook._priority_list_validate_findings(
            ["rulebook/priority-list.md"]
        )
        self.assertEqual(findings, [])

    def test_staged_and_parse_broken_blocks(self) -> None:
        self._set_priority_list_raw("# no sentinels\n")
        findings = self.hook._priority_list_validate_findings(
            ["rulebook/priority-list.md"]
        )
        self.assertIsNotNone(findings)
        self.assertTrue(findings)
        self.assertTrue(any("sentinel-missing" in f for f in findings))

    def test_staged_and_dead_label_does_not_block(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - ghost\n")
        self._seed(tid="T-0001", labels=["alpha"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            findings = self.hook._priority_list_validate_findings(
                ["rulebook/priority-list.md"]
            )
        self.assertEqual(findings, [])  # warnings never block
        self.assertIn("ghost", err.getvalue())

    def test_main_refuses_commit_on_parse_failure(self) -> None:
        self._set_priority_list_raw("# no sentinels\n")
        with patch.object(self.hook, "_staged_files", return_value=["rulebook/priority-list.md"]), \
             patch.object(self.hook, "_read_staged_blob", return_value=b""), \
             patch.object(self.hook, "_warn_directive_drift"), \
             patch.object(self.hook, "_hook_allowlist_drift", return_value=None), \
             patch.object(self.hook, "_main_guard_lint_findings", return_value=[]), \
             patch.object(self.hook, "_ledger_validity_findings", return_value=[]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self.hook.main()
        self.assertEqual(rc, 1)
        self.assertIn("priority-list validation failure", err.getvalue())

    def test_main_allows_commit_on_dead_label_warning_only(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n  - ghost\n")
        self._seed(tid="T-0001", labels=["alpha"])
        with patch.object(self.hook, "_staged_files", return_value=["rulebook/priority-list.md"]), \
             patch.object(self.hook, "_read_staged_blob", return_value=b""), \
             patch.object(self.hook, "_warn_directive_drift"), \
             patch.object(self.hook, "_hook_allowlist_drift", return_value=None), \
             patch.object(self.hook, "_main_guard_lint_findings", return_value=[]), \
             patch.object(self.hook, "_ledger_validity_findings", return_value=[]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self.hook.main()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
