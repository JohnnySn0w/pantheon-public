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


class TestValidateSemanticDrift(_PriorityListValidateTestBase):
    """T-3429 / ORG-078 §Priority-list-drift-hygiene fourth heuristic:
    novel-arc-attach event detector.

    Fires ONLY when (recent novel-arc-attach >= 1) AND (historical arc-count
    <= arc_threshold). The threshold structurally excludes cross-cutting
    design labels from firing.
    """

    _OLD = "2026-05-01T00:00:00Z"    # >30 days before RECENT_NOW → historical
    _NEW = "2026-09-01T00:00:00Z"    # within 30 days of RECENT_NOW → recent
    _RECENT_NOW = "2026-09-15T00:00:00Z"

    def _now(self):
        from datetime import datetime as _dt
        return _dt.fromisoformat(self._RECENT_NOW.replace("Z", "+00:00"))

    def test_narrow_arc_plus_novel_recent_fires(self) -> None:
        # Historical: T-1000 root, T-1001 child. One arc, one label.
        # Recent: T-2000 with same label but NO parent → new root = new arc.
        self._set_priority_list("priority-1:\n  - narrow-label\n")
        self._seed(tid="T-1000", labels=["narrow-label"], created=self._OLD)
        self._seed(tid="T-1001", labels=["narrow-label"], created=self._OLD, parent_ticket="T-1000")
        self._seed(tid="T-2000", labels=["narrow-label"], created=self._NEW)
        with patch("tools.ticket.datetime") as dt:
            dt.now.return_value = self._now()
            dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            rc, _, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("narrow-label", err)
        self.assertIn("semantic-drift", err)
        self.assertIn("T-2000", err)

    def test_wide_arc_set_never_fires_even_with_novel_recent(self) -> None:
        # Historical: 4 distinct arc-roots (above default arc_threshold=3).
        # Recent: novel arc attaches — but should NOT fire because the label
        # is empirically cross-cutting (this is the T-3424 false-fire case).
        self._set_priority_list("priority-1:\n  - broad-label\n")
        for tid in ("T-1000", "T-1001", "T-1002", "T-1003"):
            self._seed(tid=tid, labels=["broad-label"], created=self._OLD)
        self._seed(tid="T-2000", labels=["broad-label"], created=self._NEW)
        with patch("tools.ticket.datetime") as dt:
            dt.now.return_value = self._now()
            dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            rc, _, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("semantic-drift", err)

    def test_narrow_arc_with_no_recent_novel_does_not_fire(self) -> None:
        # Historical: 1 arc (root T-1000).
        # Recent: T-2000 whose parent is T-1000 → same arc-root as historical.
        # No novel attach → no warning.
        self._set_priority_list("priority-1:\n  - stable-label\n")
        self._seed(tid="T-1000", labels=["stable-label"], created=self._OLD)
        self._seed(tid="T-2000", labels=["stable-label"], created=self._NEW, parent_ticket="T-1000")
        with patch("tools.ticket.datetime") as dt:
            dt.now.return_value = self._now()
            dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            rc, _, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("semantic-drift", err)

    def test_narrow_arc_with_only_historical_never_fires(self) -> None:
        # Historical: 1 arc; NO recent tickets carry the label. Zero novel.
        self._set_priority_list("priority-1:\n  - dormant-label\n")
        self._seed(tid="T-1000", labels=["dormant-label"], created=self._OLD)
        # A recent ticket exists but doesn't carry the label.
        self._seed(tid="T-2000", labels=[], created=self._NEW)
        with patch("tools.ticket.datetime") as dt:
            dt.now.return_value = self._now()
            dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            rc, _, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("semantic-drift", err)

    def test_dangling_parent_ref_clusters_under_named_ancestor(self) -> None:
        # T-1000 historical, references parent T-999 which is NOT on disk.
        # _parent_chain_root returns "T-999" as the root (dangling but named).
        # T-2000 recent, same dangling parent → same root → NOT novel.
        # T-2001 recent, no parent → different root → NOVEL.
        self._set_priority_list("priority-1:\n  - orphan-label\n")
        self._seed(tid="T-1000", labels=["orphan-label"], created=self._OLD, parent_ticket="T-999")
        self._seed(tid="T-2000", labels=["orphan-label"], created=self._NEW, parent_ticket="T-999")
        self._seed(tid="T-2001", labels=["orphan-label"], created=self._NEW)
        with patch("tools.ticket.datetime") as dt:
            dt.now.return_value = self._now()
            dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            rc, _, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("T-2001", err)
        self.assertNotIn("T-2000", err)


class TestExpectedBroad(_PriorityListValidateTestBase):
    """T-6239: optional `expected-broad:` exemption + young-label skip."""

    _OLD = "2026-05-01T00:00:00Z"
    _NEW = "2026-09-01T00:00:00Z"
    _RECENT_NOW = "2026-09-15T00:00:00Z"

    def _validate_at_now(self):
        from datetime import datetime as _dt
        with patch("tools.ticket.datetime") as dt:
            dt.now.return_value = _dt.fromisoformat(
                self._RECENT_NOW.replace("Z", "+00:00")
            )
            dt.fromisoformat = _dt.fromisoformat
            return self._run_validate()

    def _seed_busy(self, label: str) -> None:
        for i in range(ticket.PRIORITY_LIST_HIGH_VOLUME_THRESHOLD + 1):
            self._seed(tid=f"T-1{i:03d}", labels=[label])

    # -- parser --
    def test_parse_without_key_returns_empty_exemptions(self) -> None:
        text = _wrap("priority-1:\n  - alpha\n")
        self.assertEqual(priority_list_parse.parse_expected_broad(text), {})
        self.assertEqual(priority_list_parse.parse(text), {"alpha": 1})

    def test_parse_with_key_is_not_a_tier_and_leaves_rank_map(self) -> None:
        text = _wrap(
            "priority-1:\n  - alpha\nexpected-broad:\n  alpha: project label\n"
        )
        self.assertEqual(priority_list_parse.parse(text), {"alpha": 1})
        self.assertEqual(
            priority_list_parse.parse_expected_broad(text),
            {"alpha": "project label"},
        )

    def test_parse_expected_broad_bad_shape_is_schema_error(self) -> None:
        for body in ("expected-broad:\n  - alpha\n", "expected-broad:\n  alpha: 3\n"):
            with self.assertRaises(priority_list_parse.PriorityListParseError) as cm:
                priority_list_parse.parse_expected_broad(_wrap(body))
            self.assertTrue(str(cm.exception).startswith("schema:"))

    # -- exemptions --
    def test_high_volume_exempted(self) -> None:
        self._set_priority_list(
            "priority-1:\n  - busy-label\nexpected-broad:\n  busy-label: project label\n"
        )
        self._seed_busy("busy-label")
        rc, out, err = self._run_validate()
        self.assertEqual(rc, 0)
        self.assertNotIn("high-volume", err)

    def test_semantic_drift_exempted(self) -> None:
        self._set_priority_list(
            "priority-1:\n  - narrow-label\nexpected-broad:\n  narrow-label: arc family\n"
        )
        self._seed(tid="T-1000", labels=["narrow-label"], created=self._OLD)
        self._seed(tid="T-2000", labels=["narrow-label"], created=self._NEW)
        rc, _, err = self._validate_at_now()
        self.assertEqual(rc, 0)
        self.assertNotIn("semantic-drift", err)

    def test_dead_label_still_warns_when_exempt(self) -> None:
        self._set_priority_list(
            "priority-1:\n  - ghost\nexpected-broad:\n  ghost: reason\n"
        )
        rc, _, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("probably-dead", err)

    def test_stale_exemption_warns(self) -> None:
        self._set_priority_list(
            "priority-1:\n  - alpha\nexpected-broad:\n  gone-label: reason\n"
        )
        self._seed(tid="T-0001", labels=["alpha"])
        rc, _, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("gone-label", err)
        self.assertIn("stale exemption", err)

    def test_absent_key_output_unchanged(self) -> None:
        self._set_priority_list("priority-1:\n  - busy-label\n")
        self._seed_busy("busy-label")
        rc, _, err = self._run_validate()
        self.assertEqual(rc, 2)
        self.assertIn("high-volume", err)
        self.assertNotIn("stale exemption", err)

    # -- young-label skip --
    def test_young_label_with_empty_history_does_not_fire(self) -> None:
        self._set_priority_list("priority-1:\n  - young-label\n")
        self._seed(tid="T-2000", labels=["young-label"], created=self._NEW)
        self._seed(tid="T-2001", labels=["young-label"], created=self._NEW)
        rc, _, err = self._validate_at_now()
        self.assertEqual(rc, 0)
        self.assertNotIn("semantic-drift", err)

    def test_label_with_history_still_fires(self) -> None:
        self._set_priority_list("priority-1:\n  - narrow-label\n")
        self._seed(tid="T-1000", labels=["narrow-label"], created=self._OLD)
        self._seed(tid="T-2000", labels=["narrow-label"], created=self._NEW)
        rc, _, err = self._validate_at_now()
        self.assertEqual(rc, 2)
        self.assertIn("semantic-drift", err)


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
# _staged_changes() (main()'s single source for the staged list, T-4451) rather
# than spinning up a real git repo.
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
        with patch.object(self.hook, "_staged_changes", return_value=[("rulebook/priority-list.md", None)]), \
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
        with patch.object(self.hook, "_staged_changes", return_value=[("rulebook/priority-list.md", None)]), \
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
