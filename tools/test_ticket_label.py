"""Unit tests for the `ticket.py label` verb + backfill-audit (T-3020 BT5).

Run from repo root:
    python3 -m pytest tools/test_ticket_label.py -q
"""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _git_utils, ticket


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str,
    target: str = "recovery",
    created: str = "2026-05-02T01:30:00Z",
    title: str = "Example ticket",
    labels: list[str] | None = None,
    depends_on: list[str] | None = None,
    parent_ticket: str | None = None,
    ask: str = "",
    context: str = "",
    project_id: str | None = None,
    nearest_intent_ref: str | None = None,
) -> Path:
    """Write a well-formed ticket file for tests. Local copy of
    test_ticket.py's `_write_ticket` extended with `parent_ticket` support
    (needed for parent-chain backfill-audit tests), and with `project_id` /
    `nearest_intent_ref` (T-4090: needed for the label-remove justification
    guard tests; both stay absent from frontmatter -- not merely null --
    when not passed, matching every other pre-T-4090 caller unchanged)."""
    fm_lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        "requester: orchestrator",
        f"target: {target}",
        f"state: {state}",
        f"parent_ticket: {parent_ticket}" if parent_ticket else "parent_ticket: null",
        f"depends_on: [{', '.join(depends_on)}]" if depends_on else "depends_on: []",
    ]
    if project_id is not None:
        fm_lines.append(f"project_id: {project_id}")
    if nearest_intent_ref is not None:
        fm_lines.append(f"nearest_intent_ref: {nearest_intent_ref}")
    if labels:
        fm_lines.append(f"labels: [{', '.join(labels)}]")
    fm_lines.append("---")

    body_lines = [
        "",
        f"# {title}",
        "",
        "## Ask",
        "",
        ask,
        "## Context",
        "",
        context,
        "## Transition log",
        "",
        f"- {created} [orchestrator] filed in inbox",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines + body_lines), encoding="utf-8")
    return path


class _LabelTestBase(unittest.TestCase):
    """Shared fixture: temp ticket tree + mocked pull/commit (matches
    TestCmdBlock's pattern in test_ticket.py)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str = "active", **kw) -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state, **kw)
        return path

    @staticmethod
    def _args(tid: str, labels: list[str], no_push: bool = True) -> argparse.Namespace:
        return argparse.Namespace(ticket=tid, labels=labels, no_push=no_push)


class TestLabelAdd(_LabelTestBase):
    def test_add_new_label_commits_once(self) -> None:
        path = self._seed("T-9001", "active")
        rc = ticket.cmd_label_add(self._args("T-9001", ["concordance"]))
        self.assertEqual(rc, 0)
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["concordance"])
        self.mock_commit.assert_called_once()

    def test_add_full_noop_when_all_already_present(self) -> None:
        path = self._seed("T-9002", "active", labels=["concordance"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = ticket.cmd_label_add(self._args("T-9002", ["concordance"]))
        self.assertEqual(rc, 0)
        self.mock_commit.assert_not_called()
        self.assertIn("already-present", buf.getvalue())
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["concordance"])

    def test_add_mixed_new_and_present_adds_only_new(self) -> None:
        path = self._seed("T-9003", "active", labels=["concordance"])
        rc = ticket.cmd_label_add(self._args("T-9003", ["concordance", "org-065"]))
        self.assertEqual(rc, 0)
        self.mock_commit.assert_called_once()
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["concordance", "org-065"])

    def test_add_refused_on_done(self) -> None:
        self._seed("T-9004", "done")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_label_add(self._args("T-9004", ["concordance"]))
        self.assertIn("done", str(ctx.exception))
        self.mock_commit.assert_not_called()

    def test_add_dedup_does_not_duplicate(self) -> None:
        path = self._seed("T-9005", "active", labels=["concordance"])
        ticket.cmd_label_add(self._args("T-9005", ["concordance", "concordance"]))
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"].count("concordance"), 1)

    def test_add_transactional_invariant_frontmatter_and_log_in_one_write(self) -> None:
        path = self._seed("T-9006", "active")
        ticket.cmd_label_add(self._args("T-9006", ["org-065"]))
        self.mock_commit.assert_called_once()
        # The single write that preceded the (mocked) commit call must carry
        # BOTH the frontmatter change and the transition-log entry.
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["org-065"])
        self.assertIn("labels added", body)
        self.assertIn("org-065", body)
        # And that write must be exactly the one staged into the mocked commit.
        staged_paths = self.mock_commit.call_args[0][0]
        self.assertEqual(staged_paths, [path])


class TestLabelRemove(_LabelTestBase):
    def test_remove_present_label_commits_once(self) -> None:
        path = self._seed("T-9010", "active", labels=["concordance", "org-065"])
        rc = ticket.cmd_label_remove(self._args("T-9010", ["org-065"]))
        self.assertEqual(rc, 0)
        self.mock_commit.assert_called_once()
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["concordance"])

    def test_remove_all_absent_is_noop_exit0(self) -> None:
        path = self._seed("T-9011", "active", labels=["concordance"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = ticket.cmd_label_remove(self._args("T-9011", ["org-065"]))
        self.assertEqual(rc, 0)
        self.mock_commit.assert_not_called()
        self.assertIn("no change", buf.getvalue())
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["concordance"])

    def test_remove_partial_absent_raises_naming_the_miss(self) -> None:
        self._seed("T-9012", "active", labels=["concordance"])
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_label_remove(self._args("T-9012", ["concordance", "org-065"]))
        self.assertIn("org-065", str(ctx.exception))
        self.mock_commit.assert_not_called()

    def test_remove_refused_on_done(self) -> None:
        self._seed("T-9013", "done", labels=["concordance"])
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_label_remove(self._args("T-9013", ["concordance"]))
        self.assertIn("done", str(ctx.exception))
        self.mock_commit.assert_not_called()

    def test_remove_transactional_invariant_frontmatter_and_log_in_one_write(self) -> None:
        path = self._seed("T-9014", "active", labels=["concordance", "org-065"])
        ticket.cmd_label_remove(self._args("T-9014", ["org-065"]))
        self.mock_commit.assert_called_once()
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["concordance"])
        self.assertIn("labels removed", body)
        self.assertIn("org-065", body)
        staged_paths = self.mock_commit.call_args[0][0]
        self.assertEqual(staged_paths, [path])

    def test_remove_refused_when_it_would_unjustify_project_id(self) -> None:
        """T-4090: the label is the ONLY thing justifying this ticket's
        project_id (no nearest_intent_ref chain) -- removing it must be
        refused, not silently land an unjustified project_id (T-4080)."""
        path = self._seed(
            "T-9015", "active",
            project_id="alpha-project", labels=["alpha-project", "concordance"],
        )
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_label_remove(self._args("T-9015", ["alpha-project"]))
        self.assertIn("T-9015", str(ctx.exception))
        self.assertIn("alpha-project", str(ctx.exception))
        self.mock_commit.assert_not_called()
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["labels"], ["alpha-project", "concordance"])

    def test_remove_allowed_when_project_id_remains_justified(self) -> None:
        """Companion true-negative: project_id is justified via a
        ticket:-chain hop to a same-project parent, not via the label being
        removed -- removal must succeed normally."""
        self._seed("T-9016", "active", project_id="alpha-project", labels=["alpha-project"])
        path = self._seed(
            "T-9017", "active",
            project_id="alpha-project", nearest_intent_ref="ticket:T-9016",
            labels=["concordance"],
        )
        rc = ticket.cmd_label_remove(self._args("T-9017", ["concordance"]))
        self.assertEqual(rc, 0)
        self.mock_commit.assert_called_once()
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields.get("labels"), [])


class TestRefuseIfLabelRemovalUnjustifiesProject(_LabelTestBase):
    """T-4090 REBOUND-1 finding 1: unit coverage for the helper itself,
    called directly rather than through `cmd_label_remove` -- that command
    independently refuses ALL label removals on `done/` tickets before this
    guard is ever reached (a separate, pre-existing check), so the
    done-state early return promised by this helper's docstring can only be
    observed by exercising the helper in isolation."""

    def test_done_state_exempt_from_project_id_justification_refusal(self) -> None:
        """(a) A done-state candidate that would otherwise leave project_id
        unjustified must NOT raise -- mirrors ticket_purpose.py's
        validate_intent_link done-state exemption ("historic closure remains
        readable and quiet")."""
        new_fields = {
            "project_id": "alpha-project",
            "labels": ["concordance"],  # the justifying label already gone
            "nearest_intent_ref": None,
            "state": "done",
        }
        # Must not raise.
        ticket._refuse_if_label_removal_unjustifies_project("T-9018", new_fields)

    def test_non_done_state_still_refuses(self) -> None:
        """(b) Companion true-negative: the identical shape on a non-done
        ticket still refuses -- the done-state early return must not widen
        beyond `state == "done"`."""
        new_fields = {
            "project_id": "alpha-project",
            "labels": ["concordance"],
            "nearest_intent_ref": None,
            "state": "active",
        }
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket._refuse_if_label_removal_unjustifies_project("T-9019", new_fields)
        self.assertIn("T-9019", str(ctx.exception))
        self.assertIn("alpha-project", str(ctx.exception))


class TestLabelList(_LabelTestBase):
    def test_list_prints_one_per_line_no_side_effects(self) -> None:
        self._seed("T-9020", "active", labels=["concordance", "org-065"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ticket.cmd_label_list(argparse.Namespace(ticket="T-9020"))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines(), ["concordance", "org-065"])
        self.mock_commit.assert_not_called()
        self.mock_pull.assert_not_called()

    def test_list_empty_labels_prints_nothing(self) -> None:
        self._seed("T-9021", "active")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ticket.cmd_label_list(argparse.Namespace(ticket="T-9021"))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")


class TestLabelBackfillAudit(_LabelTestBase):
    def _run_audit(self, scope: str | None = None, known_labels: dict | None = None) -> list[str]:
        args = argparse.Namespace(scope=scope)
        parse_patch = patch.object(
            ticket.priority_list_parse, "parse_file",
            return_value=known_labels if known_labels is not None else {},
        )
        buf = io.StringIO()
        with parse_patch, contextlib.redirect_stdout(buf):
            rc = ticket.cmd_label_backfill_audit(args)
        self.assertEqual(rc, 0)
        return buf.getvalue().splitlines()

    def test_parent_chain_suggestion(self) -> None:
        self._seed("T-9030", "active", labels=["t-3020-arc"])
        self._seed("T-9031", "active", parent_ticket="T-9030")
        lines = self._run_audit()
        matching = [l for l in lines if l.startswith("T-9031:")]
        self.assertEqual(len(matching), 1)
        self.assertIn("t-3020-arc", matching[0])
        self.assertIn("parent-chain", matching[0])

    def test_dep_chain_suggestion(self) -> None:
        # T-3070 is a dependency of T-3071 (T-3071 depends_on T-3070).
        # T-3071 carries an inheritable arc label -> T-3070 is suggested it.
        self._seed("T-3070", "active")
        self._seed("T-3071", "active", labels=["t-3071-arc"], depends_on=["T-3070"])
        lines = self._run_audit()
        matching = [l for l in lines if l.startswith("T-3070:")]
        self.assertEqual(len(matching), 1)
        self.assertIn("t-3071-arc", matching[0])
        self.assertIn("dep-chain", matching[0])
        # The reverse direction must NOT fire: T-3071 already carries its own
        # label and has no suggestions sourced from T-3070 (which has none).
        self.assertFalse(any(l.startswith("T-3071:") for l in lines))

    def test_body_pattern_suggestion(self) -> None:
        self._seed(
            "T-9040", "active",
            title="Some ticket", ask="Continuing the concordance work from last week.",
        )
        lines = self._run_audit(known_labels={"concordance": 3})
        matching = [l for l in lines if l.startswith("T-9040:")]
        self.assertEqual(len(matching), 1)
        self.assertIn("concordance", matching[0])
        self.assertIn("body-pattern:concordance", matching[0])

    def test_no_false_positive_when_label_already_present(self) -> None:
        self._seed("T-9050", "active", labels=["t-3020-arc"])
        self._seed("T-9051", "active", parent_ticket="T-9050", labels=["t-3020-arc"])
        lines = self._run_audit()
        self.assertFalse(any(l.startswith("T-9051:") for l in lines))

    def test_per_ticket_label_never_inherited(self) -> None:
        self._seed("T-9060", "active", labels=["t-3020-arc", "interactive-required"])
        self._seed("T-9061", "active", parent_ticket="T-9060")
        lines = self._run_audit()
        matching = [l for l in lines if l.startswith("T-9061:")]
        self.assertEqual(len(matching), 1)
        self.assertIn("t-3020-arc", matching[0])
        self.assertNotIn("interactive-required", matching[0])

    def test_scope_filters_to_requested_state(self) -> None:
        self._seed("T-9070", "active", labels=["t-3020-arc"])
        self._seed("T-9071", "done", parent_ticket="T-9070")
        # Default scope (inbox+active) must not see the done-state child.
        self.assertFalse(any(l.startswith("T-9071:") for l in self._run_audit()))
        # Explicit --scope done must see it.
        self.assertTrue(any(l.startswith("T-9071:") for l in self._run_audit(scope="done")))

    def test_parse_error_degrades_gracefully_not_crash(self) -> None:
        self._seed("T-9080", "active", ask="Continuing the concordance work.")
        args = argparse.Namespace(scope=None)
        with patch.object(
            ticket.priority_list_parse, "parse_file",
            side_effect=ticket.priority_list_parse.PriorityListParseError("sentinel-missing: x"),
        ):
            rc = ticket.cmd_label_backfill_audit(args)
        self.assertEqual(rc, 0)

    def test_no_suggestions_prints_informational_line(self) -> None:
        self._seed("T-9090", "active")
        lines = self._run_audit()
        self.assertEqual(lines, ["backfill-audit: no missing-label suggestions"])


if __name__ == "__main__":
    unittest.main()
