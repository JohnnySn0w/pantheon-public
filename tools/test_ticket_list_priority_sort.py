"""Integration tests for `tools/ticket.py` `cmd_list`'s priority-sort path
(T-3020 BT3, ORG-078 §3/§4 + §Priority-inversion).

Self-contained (mirrors tools/test_priority_list_parse.py's `_wrap` style
and tools/test_ticket.py's `_write_ticket` shape, extended with
`retargeted_at` for ORG-078 §3's secondary sort key) rather than importing
from tools/test_ticket.py, matching this tree's existing per-BT test-file
isolation convention.

Run from repo root:
    python3 -m unittest tools.test_ticket_list_priority_sort -v
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import priority_list_parse, ticket


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
    depends_on: list[str] | None = None,
    retargeted_at: str | None = None,
    tier_rank: int | str | None = None,
) -> None:
    """Write a well-formed ticket file for tests."""
    fm_lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        f"requester: {requester}",
        f"target: {target}",
    ]
    if retargeted_at:
        fm_lines.append(f"retargeted_at: {retargeted_at}")
    fm_lines.append(f"state: {state}")
    fm_lines.append("parent_ticket: null")
    fm_lines.append(
        f"depends_on: [{', '.join(depends_on)}]" if depends_on else "depends_on: []"
    )
    if labels:
        fm_lines.append(f"labels: [{', '.join(labels)}]")
    if tier_rank is not None:
        fm_lines.append(f"tier_rank: {tier_rank}")
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


class _PriorityListTestBase(unittest.TestCase):
    """Shared fixture: tmp tickets/ tree + tmp priority-list.md + tmp
    telemetry sink, all patched onto module globals so tests never touch
    the real repo's rulebook/priority-list.md or the real user's homedir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets_root = self.root / "tickets"
        for s in ticket.STATES:
            (self.tickets_root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.tickets_root)
        self._tickets_patch.start()

        self.health_path = self.root / "priority_list_health.jsonl"
        self._health_patch = patch.object(ticket, "PRIORITY_LIST_HEALTH_PATH", self.health_path)
        self._health_patch.start()

        self.priority_list_path = self.root / "priority-list.md"
        self._priority_list_patch = patch.object(
            priority_list_parse, "DEFAULT_PRIORITY_LIST_PATH", self.priority_list_path
        )
        self._priority_list_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._health_patch.stop()
        self._priority_list_patch.stop()
        self._tmp.cleanup()

    def _set_priority_list(self, yaml_body: str) -> None:
        self.priority_list_path.write_text(_wrap(yaml_body), encoding="utf-8")

    def _seed(self, **kwargs) -> None:
        tid = kwargs["tid"]
        state = kwargs.pop("state", "inbox")
        _write_ticket(self.tickets_root / state / f"{tid}.md", state=state, **kwargs)

    def _run_list(self, **kwargs) -> tuple[int, str, str]:
        ns = MagicMock()
        ns.state = kwargs.get("state")
        ns.target = kwargs.get("target")
        ns.label = kwargs.get("label")
        ns.show_unblocks = kwargs.get("show_unblocks", False)
        ns.sort = kwargs.get("sort", "priority")
        ns.explain_priority = kwargs.get("explain_priority", None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ticket.cmd_list(ns)
        return rc, out.getvalue(), err.getvalue()

    def _health_lines(self) -> list[dict]:
        if not self.health_path.exists():
            return []
        return [json.loads(line) for line in self.health_path.read_text().splitlines() if line]

    @staticmethod
    def _line_index(printed: str, tid: str) -> int:
        for i, line in enumerate(printed.splitlines()):
            if line.startswith(tid):
                return i
        raise AssertionError(f"{tid} not found in printed output:\n{printed}")


class TestCeilingRank(_PriorityListTestBase):
    def test_multi_label_ticket_picks_highest_priority_match(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\npriority-2:\n  - beta\n")
        self._seed(tid="T-0001", labels=["beta", "alpha"])  # ceiling: rank 1
        self._seed(tid="T-0002", labels=["beta"])  # rank 2
        rc, out, err = self._run_list()
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertLess(self._line_index(out, "T-0001"), self._line_index(out, "T-0002"))


class TestSecondarySort(_PriorityListTestBase):
    def test_retargeted_at_secondary_sort_within_same_rank(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(tid="T-0001", labels=["alpha"], retargeted_at="2026-08-20T00:00:00Z")
        self._seed(tid="T-0002", labels=["alpha"], retargeted_at="2026-08-10T00:00:00Z")
        rc, out, _ = self._run_list()
        self.assertEqual(rc, 0)
        # T-0002 retargeted EARLIER -> sorts first despite the higher ticket id.
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))


class TestTierRank(_PriorityListTestBase):
    def test_explicit_rank_sorts_before_unranked_peer_then_fifo(self) -> None:
        self._set_priority_list("priority-2:\n  - echo\n")
        self._seed(
            tid="T-0001", labels=["echo"], tier_rank=10,
            created="2026-08-20T00:00:00Z",
        )
        self._seed(
            tid="T-0002", labels=["echo"],
            created="2026-08-10T00:00:00Z",
        )
        self._seed(
            tid="T-0003", labels=["echo"], tier_rank=20,
            created="2026-08-01T00:00:00Z",
        )

        rc, out, err = self._run_list()

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertLess(self._line_index(out, "T-0001"), self._line_index(out, "T-0003"))
        self.assertLess(self._line_index(out, "T-0003"), self._line_index(out, "T-0002"))

    def test_label_tier_remains_authoritative_over_extreme_ticket_rank(self) -> None:
        self._set_priority_list("priority-1:\n  - p1\npriority-2:\n  - p2\n")
        self._seed(tid="T-0001", labels=["p1"], created="2026-08-20T00:00:00Z")
        self._seed(
            tid="T-0002", labels=["p2"], tier_rank=-999999,
            created="2026-08-10T00:00:00Z",
        )

        rc, out, _ = self._run_list()

        self.assertEqual(rc, 0)
        self.assertLess(self._line_index(out, "T-0001"), self._line_index(out, "T-0002"))

    def test_absent_or_invalid_rank_is_inert_and_fifo_still_breaks_tie(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(
            tid="T-0001", labels=["alpha"], tier_rank="not-an-int",
            created="2026-08-20T00:00:00Z",
        )
        self._seed(
            tid="T-0002", labels=["alpha"],
            created="2026-08-10T00:00:00Z",
        )
        self._seed(
            tid="T-0003", labels=["alpha"], tier_rank="9" * 4301,
            created="2026-08-01T00:00:00Z",
        )

        rc, out, err = self._run_list()

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertLess(self._line_index(out, "T-0003"), self._line_index(out, "T-0002"))
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))

    def test_explain_reports_ticket_rank_and_complete_sort_key(self) -> None:
        self._set_priority_list("priority-2:\n  - echo\n")
        self._seed(tid="T-0001", labels=["echo"], tier_rank=10)

        rc, out, err = self._run_list(explain_priority="T-0001")

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("tier_rank: 10", out)
        self.assertIn(
            "(effective_rank, tier_rank, effective_queue_ts, ticket_id)", out
        )

    def test_created_fallback_when_never_retargeted(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(tid="T-0001", labels=["alpha"], created="2026-08-20T00:00:00Z")
        self._seed(tid="T-0002", labels=["alpha"], created="2026-08-10T00:00:00Z")
        rc, out, _ = self._run_list()
        self.assertEqual(rc, 0)
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))


class TestTieBreak(_PriorityListTestBase):
    def test_ticket_id_tie_break_overrides_old_state_grouping(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        same_ts = "2026-08-15T00:00:00Z"
        # Deliberately place the LOWER id in a state iterated AFTER the
        # HIGHER id's state (STATES = inbox, active, done) — under the old
        # state-grouped listing behavior T-0005 (inbox) would print before
        # T-0002 (active); ORG-078's flat total order must invert that.
        self._seed(tid="T-0005", state="inbox", labels=["alpha"], created=same_ts)
        self._seed(tid="T-0002", state="active", labels=["alpha"], created=same_ts)
        rc, out, _ = self._run_list()
        self.assertEqual(rc, 0)
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0005"))


class TestDependencyInheritance(_PriorityListTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._set_priority_list("priority-1:\n  - p1\n")
        # T-0010 (P1) depends_on T-0011 depends_on T-0012 — transitive chain.
        self._seed(tid="T-0010", labels=["p1"], depends_on=["T-0011"])
        self._seed(tid="T-0011", depends_on=["T-0012"])
        self._seed(tid="T-0012")
        self._seed(tid="T-0099")  # unrelated control — stays at the tail

    def test_direct_and_transitive_promotion(self) -> None:
        rc, out, err = self._run_list()
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        # Both prereqs promoted ahead of the unrelated tail ticket.
        self.assertLess(self._line_index(out, "T-0011"), self._line_index(out, "T-0099"))
        self.assertLess(self._line_index(out, "T-0012"), self._line_index(out, "T-0099"))

    def test_explain_priority_direct_promotion(self) -> None:
        rc, out, err = self._run_list(explain_priority="T-0011")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("own_rank: ∞", out)
        self.assertIn("effective_rank: 1", out)
        self.assertIn("T-0011 <- T-0010", out)

    def test_explain_priority_transitive_promotion_cites_direct_elevator(self) -> None:
        rc, out, err = self._run_list(explain_priority="T-0012")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("effective_rank: 1", out)
        # Chain must walk the FULL path, not just cite the ultimate P1 source.
        self.assertIn("T-0012 <- T-0011 <- T-0010", out)

    def test_explain_priority_unelevated_ticket(self) -> None:
        rc, out, err = self._run_list(explain_priority="T-0099")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("own_rank: ∞", out)
        self.assertIn("own rank stands", out)

    def test_telemetry_counters(self) -> None:
        self._run_list()
        lines = self._health_lines()
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertTrue(entry["parse_ok"])
        self.assertIsNone(entry["parse_fail_class"])
        # ranked: T-0010 (own) + T-0011 + T-0012 (both inherited) = 3.
        self.assertEqual(entry["n_tickets_ranked"], 3)
        # tail: only T-0099.
        self.assertEqual(entry["n_tickets_tail_fifo"], 1)
        # promotions: T-0011 + T-0012 (strict improvement over their own inf rank).
        self.assertEqual(entry["n_dep_inherit_promotions"], 2)
        # labels-seen: only T-0010 carries a matching label.
        self.assertEqual(entry["n_ticket_labels_seen"], 1)


class TestSortBypass(_PriorityListTestBase):
    def test_sort_created_ignores_rank_entirely(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        # T-0001: later created, but carries the P1 label.
        self._seed(tid="T-0001", labels=["alpha"], created="2026-08-20T00:00:00Z")
        # T-0002: earlier created, no label at all.
        self._seed(tid="T-0002", created="2026-08-10T00:00:00Z")
        rc, out, err = self._run_list(sort="created")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))
        # Bypass mode never touches the priority-list parse -> no telemetry.
        self.assertEqual(self._health_lines(), [])

    def test_sort_retargeted_ignores_rank_entirely(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(tid="T-0001", labels=["alpha"], retargeted_at="2026-08-20T00:00:00Z")
        self._seed(tid="T-0002", retargeted_at="2026-08-10T00:00:00Z")
        rc, out, _ = self._run_list(sort="retargeted")
        self.assertEqual(rc, 0)
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))
        self.assertEqual(self._health_lines(), [])

    def test_sort_created_ignores_tier_rank(self) -> None:
        self._set_priority_list("priority-1:\n  - alpha\n")
        self._seed(
            tid="T-0001", labels=["alpha"], tier_rank=-100,
            created="2026-08-20T00:00:00Z",
        )
        self._seed(
            tid="T-0002", labels=["alpha"],
            created="2026-08-10T00:00:00Z",
        )

        rc, out, _ = self._run_list(sort="created")

        self.assertEqual(rc, 0)
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))
        self.assertEqual(self._health_lines(), [])


class TestParseFailureFallback(_PriorityListTestBase):
    def test_missing_file_falls_back_to_created_fifo(self) -> None:
        # Never write self.priority_list_path -> file-not-found.
        self._seed(tid="T-0001", created="2026-08-20T00:00:00Z")
        self._seed(tid="T-0002", created="2026-08-10T00:00:00Z")
        rc, out, err = self._run_list()
        self.assertEqual(rc, 0)
        self.assertIn("warning: priority-list parse failed (file-not-found)", err)
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))
        entry = self._health_lines()[0]
        self.assertFalse(entry["parse_ok"])
        self.assertEqual(entry["parse_fail_class"], "file-not-found")

    def test_malformed_yaml_falls_back_and_never_crashes(self) -> None:
        self.priority_list_path.write_text(
            "# preamble\n\n<!-- PRIORITY-LIST-START -->\nno fence here\n"
            "<!-- PRIORITY-LIST-END -->\n",
            encoding="utf-8",
        )
        self._seed(tid="T-0001", created="2026-08-20T00:00:00Z")
        self._seed(tid="T-0002", created="2026-08-10T00:00:00Z")
        rc, out, err = self._run_list()
        self.assertEqual(rc, 0)
        self.assertIn("warning: priority-list parse failed (fence-missing)", err)
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))


class TestEmptyPriorityList(_PriorityListTestBase):
    def test_empty_ranked_labels_all_tail_fifo_no_crash(self) -> None:
        self._set_priority_list("{}")
        self._seed(tid="T-0001", labels=["whatever"], created="2026-08-20T00:00:00Z")
        self._seed(tid="T-0002", created="2026-08-10T00:00:00Z")
        rc, out, err = self._run_list()
        self.assertEqual(rc, 0)
        # A legal "no priorities defined" state, not a parse failure — no
        # stderr warning fires (matches priority_list_parse's own framing of
        # an empty region as a legal return, not an error).
        self.assertEqual(err, "")
        self.assertIn("T-0001", out)
        self.assertIn("T-0002", out)
        # Uniformly tail -> degenerates to FIFO-by-effective_queue_ts.
        self.assertLess(self._line_index(out, "T-0002"), self._line_index(out, "T-0001"))
        entry = self._health_lines()[0]
        self.assertTrue(entry["parse_ok"])
        self.assertEqual(entry["n_tickets_ranked"], 0)
        self.assertEqual(entry["n_tickets_tail_fifo"], 2)


if __name__ == "__main__":
    unittest.main()
