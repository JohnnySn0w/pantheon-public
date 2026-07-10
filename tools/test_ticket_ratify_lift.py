"""Tests for T-0491: auto-lift of ratified doc Status field on ratify-resolve.

Unit tests (Layer-2): mock subprocess.run, patch REPO_ROOT + TICKETS_ROOT,
exercise _lift_ratifiable_doc_status and the resolve path in-process.

Layer-3 integration test: real tmp git repo via _test_fixtures, invoke
cmd_resolve in-process against the tmp tree, assert the committed file content
via git show HEAD:<path>.

Run from repo root:
    python3 -m unittest tools.test_ticket_ratify_lift -v

HERMES discipline: the trigger byte sequence is never written contiguously
in this source file. See HERME + S discipline in test_ticket.py module docstring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket


# ---------------------------------------------------------------------------
# Helpers — shared across unit tests
# ---------------------------------------------------------------------------


def _write_ticket_file(
    path: Path,
    *,
    tid: str,
    state: str = "active",
    target: str = "operator",
    intent: str = "ratify",
    ratifiable_files: list[str] | None = None,
    labels: list[str] | None = None,
) -> None:
    """Write a minimal ticket file with ratify frontmatter."""
    rf_list = ratifiable_files or []
    rf_yaml = "[" + ", ".join(rf_list) + "]"
    labels_yaml = "[" + ", ".join(labels or ["ratification"]) + "]"
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-05-20T10:00:00Z",
        "requester: orchestrator",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
        f"labels: {labels_yaml}",
        f"intent: {intent}",
        f"ratifiable_files: {rf_yaml}",
        "---",
        "",
        f"# Test ratify ticket {tid}",
        "",
        "## Ask",
        "",
        "Ratify the doc.",
        "",
        "## Transition log",
        "",
        f"- 2026-05-20T10:00:00Z [orchestrator] filed in inbox",
        f"- 2026-05-20T10:01:00Z [orchestrator] claimed",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines), encoding="utf-8")


def _write_doc(path: Path, status_line: str, extra_body: str = "") -> None:
    """Write a minimal ratifiable doc with the given Status line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Test Doc\n\n**Status:** {status_line}\n\n## Body\n\n{extra_body}Content here.\n"
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit test base: mocks subprocess + REPO_ROOT + TICKETS_ROOT (TransitionTest
# pattern from test_ticket.py)
# ---------------------------------------------------------------------------


class RatifyLiftUnitBase(unittest.TestCase):
    """Base class with mock setup matching TransitionTest in test_ticket.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        self._tickets_patch.start()

        # Minimal README.md so _rebuild_readme_table doesn't fail.
        readme = self.root / "tickets" / "README.md"
        readme.write_text(
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
            "## Related\n\n"
            "- Link\n",
            encoding="utf-8",
        )

        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "add"] and "--" in argv:
                idx = argv.index("--")
                paths = argv[idx + 1:]
                missing = [p for p in paths if not (self.root / p).exists()]
                if missing:
                    return MagicMock(
                        returncode=128,
                        stdout="",
                        stderr=f"fatal: pathspec '{missing[0]}' did not match any files\n",
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                self.assertIn("--no-renames", argv)
                staged_set: set[str] = set()
                for prior in self.mock_run.call_args_list:
                    pa = prior.args[0]
                    if pa[:2] == ["git", "mv"] and len(pa) >= 4:
                        staged_set.add(pa[2])
                        staged_set.add(pa[3])
                    elif pa[:2] == ["git", "add"] and "--" in pa:
                        i = pa.index("--")
                        for p in pa[i + 1:]:
                            if (self.root / p).exists():
                                staged_set.add(p)
                    elif pa[:2] == ["git", "commit"]:
                        staged_set.clear()
                staged = sorted(staged_set)
                return MagicMock(
                    returncode=0,
                    stdout="\n".join(staged) + ("\n" if staged else ""),
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

    def tearDown(self) -> None:
        self._sub_patch.stop()
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def _make_resolve_args(self, tid: str, summary: str | None = None) -> MagicMock:
        args = MagicMock()
        args.ticket = tid
        args.with_summary = summary
        args.no_push = True  # avoid push in unit tests
        return args


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestRatifyLiftStatusLine(unittest.TestCase):
    """Direct unit tests of _lift_ratifiable_doc_status helper."""

    def _call(self, doc_content: str, repo_root: Path) -> tuple[str | None, list[str]]:
        """Call _lift_ratifiable_doc_status on a doc with the given content."""
        p = repo_root / "adr" / "test-doc.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(doc_content, encoding="utf-8")
        warnings: list[str] = []
        result = ticket._lift_ratifiable_doc_status(p, repo_root, warnings)
        return result, warnings

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lifts_basic_status_line(self) -> None:
        """Plain Status line gets Ratified prefix."""
        content = "# Doc\n\n**Status:** Drafted 2026-05-01.\n\n## Body\n"
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("**Ratified", result)
        self.assertIn("by the operator.", result)
        self.assertEqual(warnings, [])

    def test_lifts_and_strips_awaiting_clause(self) -> None:
        """Awaiting the operator ratification clause is stripped after Ratified prefix."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator ratification.\n\n"
            "## Body\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("**Ratified", result)
        self.assertNotIn("Awaiting the operator ratification", result)
        # The drafted history should be preserved
        self.assertIn("Drafted 2026-05-01", result)

    def test_strips_awaiting_ratification_variant(self) -> None:
        """'Awaiting ratification.' (no the operator) is also stripped."""
        content = (
            "# Doc\n\n"
            "**Status:** Authored 2026-05-10. Awaiting ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertNotIn("Awaiting ratification", result)

    def test_strips_bold_awaiting_clause(self) -> None:
        """**Awaiting ... ratification** (bold markdown) is stripped."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. **Awaiting the operator ratification.**\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertNotIn("Awaiting", result)

    def test_idempotent_already_ratified(self) -> None:
        """Status already starts with Ratified → no change (returns None)."""
        content = (
            "# Doc\n\n"
            "**Status:** **Ratified 2026-05-19 by the operator.** Drafted 2026-05-01.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNone(result)
        self.assertEqual(warnings, [])

    def test_status_line_not_found_warns(self) -> None:
        """Doc without **Status:** line → None result + warning."""
        content = "# Doc\n\nNo status here.\n\n## Body\n"
        result, warnings = self._call(content, self.root)
        self.assertIsNone(result)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Status", warnings[0])

    def test_first_status_line_only(self) -> None:
        """Only the first **Status:** line is rewritten."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01.\n\n"
            "## Section\n\n"
            "**Status:** Some section status.\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        # First line rewritten; second unchanged
        lines = result.splitlines()
        status_lines = [l for l in lines if "**Status:**" in l]
        self.assertEqual(len(status_lines), 2)
        self.assertIn("Ratified", status_lines[0])
        self.assertNotIn("Ratified", status_lines[1])

    def test_preserves_drafted_history(self) -> None:
        """Authored/Amended history after the awaiting clause stays intact."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01 (T-0300). "
            "Amended 2026-05-10 (T-0400). "
            "Awaiting the operator ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("Drafted 2026-05-01", result)
        self.assertIn("Amended 2026-05-10", result)
        self.assertNotIn("Awaiting", result)


class TestRatifyLiftViaResolve(RatifyLiftUnitBase):
    """Integration-unit tests: resolve a ratify ticket and check doc is staged."""

    def test_ratify_resolve_lifts_status_in_same_repo_doc(self) -> None:
        """Resolving a ratify ticket rewrites the same-repo doc's Status line.

        Also asserts that 'auto-unblock cascade' does NOT appear on stdout
        when there are no real cascade dependants (Defect 1 regression guard).
        """
        import io
        import contextlib
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0491"
            doc_rel = "adr/test-adr.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-01. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            args = self._make_resolve_args(tid, "Ratified.")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)
            stdout_out = stdout_buf.getvalue()

            # Doc must be rewritten with Ratified prefix and awaiting stripped
            new_content = doc_path.read_text(encoding="utf-8")
            self.assertIn("**Ratified", new_content)
            self.assertIn("by the operator.", new_content)
            self.assertNotIn("Awaiting the operator ratification", new_content)
            self.assertIn("Drafted 2026-05-01", new_content)

            # Doc must have been staged (git add called with doc_rel)
            add_calls = [
                c.args[0] for c in self.mock_run.call_args_list
                if c.args[0][:2] == ["git", "add"]
            ]
            staged_paths: set[str] = set()
            for argv in add_calls:
                idx = argv.index("--")
                staged_paths.update(argv[idx + 1:])
            self.assertIn(doc_rel, staged_paths)

            # Regression guard for Defect 1: no cascade dependants → no cascade msg.
            self.assertNotIn(
                "auto-unblock cascade", stdout_out,
                "spurious cascade output when no real dependants were unblocked"
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_ratify_lift_plus_real_cascade_both_appear(self) -> None:
        """Ratify-lift WITH a real cascade dependant: cascade message DOES appear.

        Positive regression guard: over-correcting Defect 1 must not suppress
        legitimate cascade reporting when there ARE real depends_on dependants.
        """
        import io
        import contextlib
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0498"
            doc_rel = "adr/test-adr-cascade.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-01. Awaiting ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            # Write a dependant ticket in inbox/ that depends on T-0498.
            dep_tid = "T-0499"
            dep_fm = (
                "---\n"
                f"id: {dep_tid}\n"
                "created: 2026-05-20T10:00:00Z\n"
                "requester: orchestrator\n"
                "target: recovery\n"
                "state: inbox\n"
                "parent_ticket: null\n"
                f"depends_on: [{tid}]\n"
                "labels: []\n"
                "intent: action\n"
                "ratifiable_files: []\n"
                "---\n\n"
                f"# Dependent ticket {dep_tid}\n\n"
                "## Ask\n\nWait for T-0498.\n\n"
                "## Transition log\n\n"
                f"- 2026-05-20T10:00:00Z [orchestrator] filed in inbox\n"
            )
            dep_path = self.root / "tickets" / "inbox" / f"{dep_tid}.md"
            dep_path.write_text(dep_fm, encoding="utf-8")

            args = self._make_resolve_args(tid, "Ratified.")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)
            stdout_out = stdout_buf.getvalue()

            # Doc lifted
            new_content = doc_path.read_text(encoding="utf-8")
            self.assertIn("**Ratified", new_content)

            # Cascade message MUST still appear (real dependant was unblocked)
            self.assertIn(
                "auto-unblock cascade", stdout_out,
                "cascade message missing even though a real dependant was unblocked"
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_reject_resolve_does_not_lift_doc_status(self) -> None:
        """T-0641 regression: a ratify-intent ticket resolved with a 'Rejected: ...'
        summary (pantheon-pwa's /reject route → shellResolveTicket --with 'Rejected: <reason>')
        MUST NOT auto-lift the artifact's Status line. Without this guard the
        T-0491 lift fired on rejected ratify tickets, surfaced by the T-0637
        smoke test 2026-05-27: dummy ratification target got 'Ratified by the operator'
        status even though the operator rejected.
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0997"  # synthetic id not used elsewhere in this file
            doc_rel = "adr/test-adr-reject.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-27. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            original_content = doc_path.read_text(encoding="utf-8")

            args = self._make_resolve_args(tid, "Rejected: out of scope")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            # Doc Status line must be UNTOUCHED (no lift on Reject).
            self.assertEqual(doc_path.read_text(encoding="utf-8"), original_content)

            # Ticket still resolved (moved to done).
            self.assertTrue(
                (self.root / "tickets" / "done" / f"{tid}.md").exists()
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_ratify_resolve_with_leading_whitespace_still_lifts(self) -> None:
        """Sanity guard for the new gate: a "Ratified." summary with leading
        whitespace must still trigger the lift (the gate uses lstrip before the
        'Rejected:' prefix check, so 'Ratified.' with leading whitespace passes).
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0998"
            doc_rel = "adr/test-adr-lstrip.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-27. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            args = self._make_resolve_args(tid, "   Ratified.")  # leading spaces
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            new_content = doc_path.read_text(encoding="utf-8")
            self.assertIn("**Ratified", new_content)
            self.assertIn("by the operator.", new_content)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_non_ratify_resolve_does_not_touch_doc(self) -> None:
        """Plain (non-ratify intent) resolve does not write any doc file."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0492"
            doc_rel = "adr/test-adr2.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-01.")

            # Non-ratify ticket (intent=action, no ratifiable_files)
            fm = (
                "---\n"
                f"id: {tid}\n"
                "created: 2026-05-20T10:00:00Z\n"
                "requester: orchestrator\n"
                "target: recovery\n"
                "state: active\n"
                "parent_ticket: null\n"
                "depends_on: []\n"
                "labels: []\n"
                "intent: action\n"
                "ratifiable_files: []\n"
                "---\n\n"
                f"# Non-ratify ticket {tid}\n\n"
                "## Ask\n\nDo something.\n\n"
                "## Transition log\n\n"
                "- 2026-05-20T10:00:00Z [orchestrator] filed in inbox\n"
                "- 2026-05-20T10:01:00Z [orchestrator] claimed\n"
            )
            p = self.root / "tickets" / "active" / f"{tid}.md"
            p.write_text(fm, encoding="utf-8")

            original_content = doc_path.read_text(encoding="utf-8")

            args = self._make_resolve_args(tid, "done")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            # Doc must be untouched
            self.assertEqual(doc_path.read_text(encoding="utf-8"), original_content)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_cross_repo_entry_warns_and_resolves(self) -> None:
        """Cross-repo ratifiable_files entry: warns to stderr, ticket still resolves."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0493"
            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=["revenue-project:src/some/doc.md"],
            )

            import io
            import contextlib
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                args = self._make_resolve_args(tid, "Ratified.")
                rc = ticket.cmd_resolve(args)

            self.assertEqual(rc, 0)
            stderr_out = stderr_buf.getvalue()
            self.assertIn("revenue-project", stderr_out)
            self.assertIn("skip", stderr_out.lower())
            # Ticket moved to done
            self.assertTrue(
                (self.root / "tickets" / "done" / f"{tid}.md").exists()
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_status_line_not_found_warns_and_resolves(self) -> None:
        """Doc exists but has no **Status:** line: warn, still resolve."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0494"
            doc_rel = "adr/no-status.md"
            doc_path = self.root / doc_rel
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Doc\n\nNo status line here.\n", encoding="utf-8")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            import io
            import contextlib
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                args = self._make_resolve_args(tid, "Ratified.")
                rc = ticket.cmd_resolve(args)

            self.assertEqual(rc, 0)
            self.assertIn("Status", stderr_buf.getvalue())
            self.assertTrue(
                (self.root / "tickets" / "done" / f"{tid}.md").exists()
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_idempotent_already_ratified_no_op(self) -> None:
        """Doc whose Status already starts with Ratified: no change, clean resolve."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0495"
            doc_rel = "adr/already-ratified.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "**Ratified 2026-05-19 by the operator.** Drafted 2026-05-01.")
            original_content = doc_path.read_text(encoding="utf-8")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            args = self._make_resolve_args(tid, "Ratified.")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            # Content must be identical — no-op
            self.assertEqual(doc_path.read_text(encoding="utf-8"), original_content)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_multi_file_ratifiable_files_all_lifted(self) -> None:
        """Multiple same-repo ratifiable_files: each doc's Status is lifted."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0496"
            doc_rel_a = "adr/multi-a.md"
            doc_rel_b = "sop/standard/multi-b.md"
            doc_a = self.root / doc_rel_a
            doc_b = self.root / doc_rel_b
            _write_doc(doc_a, "Drafted 2026-05-01. Awaiting ratification.")
            _write_doc(doc_b, "Authored 2026-05-05. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[
                    f"pantheon-config:{doc_rel_a}",
                    f"pantheon-config:{doc_rel_b}",
                ],
            )

            args = self._make_resolve_args(tid, "Ratified.")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            for doc_path, rel in [(doc_a, doc_rel_a), (doc_b, doc_rel_b)]:
                content = doc_path.read_text(encoding="utf-8")
                self.assertIn("**Ratified", content, msg=f"Missing Ratified in {rel}")
                self.assertIn("by the operator.", content, msg=f"Missing 'by the operator.' in {rel}")
                self.assertNotIn("Awaiting", content, msg=f"Awaiting still in {rel}")
        finally:
            del os.environ["PANTHEON_ACTOR"]


# ---------------------------------------------------------------------------
# Layer-3 integration test: real tmp git repo, assert committed file content
# ---------------------------------------------------------------------------


class TestRatifyLiftLayer3(unittest.TestCase):
    """Layer-3: real tmp git repo. Asserts committed content via git show HEAD."""

    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env, snapshot_real_repo
        self._real_snapshot = snapshot_real_repo()
        self._env = setup_integration_env()

        repo = self._env.repo_path
        # Create tickets dirs
        for s in ticket.STATES:
            (repo / "tickets" / s).mkdir(parents=True, exist_ok=True)

        # Minimal README
        readme = repo / "tickets" / "README.md"
        readme.write_text(
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
            "## Related\n\n"
            "- Link\n",
            encoding="utf-8",
        )
        # Commit the README so git operations see a clean tree
        _git_setup(repo, ["tickets/README.md"], "test: add tickets dirs")

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env, assert_real_repo_untouched
        teardown_integration_env(self._env)
        assert_real_repo_untouched(self._real_snapshot)

    def test_layer3_ratify_resolve_lifts_status_committed(self) -> None:
        """End-to-end: cmd_resolve on a ratify ticket commits the doc lift.

        Layer-3: real tmp git repo; cmd_resolve runs in-process with
        REPO_ROOT, TICKETS_ROOT, and _git_utils._git all patched to operate
        in the tmp tree.  Assert committed content via git show HEAD:<path>.

        Note on cwd: _git()'s default cwd parameter is baked at import time
        (Python default-arg evaluation).  We patch _git_utils._git with a
        wrapper that forces cwd=repo so real git invocations hit the tmp repo.
        """
        import functools

        repo = self._env.repo_path

        # Write the ratifiable doc in the tmp repo
        doc_rel = "adr/ORG-999-test.md"
        doc_path = repo / doc_rel
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(
            "# ORG-999 Test\n\n"
            "**Status:** Drafted 2026-05-01 by Orchestrator. Awaiting the operator ratification.\n\n"
            "## Context\n\nTest doc for T-0491 layer-3 fixture.\n",
            encoding="utf-8",
        )
        _git_setup(repo, [doc_rel], "test: add ratifiable doc")

        # Write + commit the ticket in active/
        tid = "T-0991"
        ticket_rel = f"tickets/active/{tid}.md"
        ticket_path = repo / ticket_rel
        _write_ticket_file(
            ticket_path,
            tid=tid,
            ratifiable_files=["pantheon-config:" + doc_rel],
        )
        _git_setup(repo, [ticket_rel], f"test: add {tid}")

        # Wrap _git to always use the tmp repo cwd (baked-in default override).
        real_git_fn = _git_utils._git

        def _git_in_tmp(*args: str, cwd=None) -> object:
            return real_git_fn(*args, cwd=repo)

        # Patch REPO_ROOT, TICKETS_ROOT, and _git to operate in the tmp repo.
        with (
            patch.object(ticket, "REPO_ROOT", repo),
            patch.object(_git_utils, "REPO_ROOT", repo),
            patch.object(ticket, "TICKETS_ROOT", repo / "tickets"),
            patch.object(_git_utils, "_git", _git_in_tmp),
            patch.object(ticket, "_git", _git_in_tmp),
        ):
            os.environ["PANTHEON_ACTOR"] = "orchestrator"
            try:
                args = MagicMock()
                args.ticket = tid
                args.with_summary = "Ratified."
                args.no_push = True
                rc = ticket.cmd_resolve(args)
            finally:
                del os.environ["PANTHEON_ACTOR"]

        self.assertEqual(rc, 0)

        # Assert via git show HEAD:<doc_rel> that the committed content has Ratified
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{doc_rel}"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, f"git show failed: {result.stderr}")
        committed_content = result.stdout
        self.assertIn("**Ratified", committed_content)
        self.assertIn("by the operator.", committed_content)
        self.assertNotIn("Awaiting the operator ratification", committed_content)
        # History preserved
        self.assertIn("Drafted 2026-05-01", committed_content)


def _git_setup(repo: Path, files: list[str], message: str) -> None:
    """Stage and commit the given relative file paths in the tmp repo."""
    subprocess.run(["git", "-C", str(repo), "add"] + files, check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True,
    )


if __name__ == "__main__":
    unittest.main()
