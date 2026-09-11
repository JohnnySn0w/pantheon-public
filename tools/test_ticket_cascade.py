"""Unit tests for `tools/ticket.py`'s `cascade` subcommand and
`tools/cascade_diff_scan.py` (T-3289: ORG-013 cascade-on-PR-merge trigger).

Run from repo root:
    python3 -m unittest tools.test_ticket_cascade -v

Closes T-3288 (ORG-013 cascade-unblock silently misses on PR-squash-merge):
`ticket.py resolve` fires the cascade via `_do_transition`'s
cascade_callback hook; a ticket resolved via a GitHub squash-merge never
calls `ticket.py resolve`, so the cascade never fires and dependants keep
stale `depends_on:` refs indefinitely. `ticket.py cascade --tid <T-NNNN>`
(TestCmdCascade*) is the standalone trigger the mechanism was missing;
`tools/cascade_diff_scan.py` (TestExtractResolvedTids / TestRunCascadeFor
Tids) is the diff-walk `.github/workflows/cascade-on-merge.yml` uses to
find which tids to invoke it for, on every push to main.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, cascade_diff_scan, ticket


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str,
    requester: str = "orchestrator",
    target: str = "recovery",
    created: str = "2026-08-26T22:30:00Z",
    title: str = "Example ticket",
    depends_on: list[str] | None = None,
) -> None:
    """Write a minimal well-formed ticket file for tests. Trimmed copy of
    tools/test_ticket.py's `_write_ticket` — kept local per this repo's flat
    test-file convention (no cross-test-file coupling)."""
    fm_lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        f"depends_on: [{', '.join(depends_on)}]" if depends_on else "depends_on: []",
        "---",
    ]
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


# ---------------------------------------------------------------------------
# ticket.py cascade — CLI subcommand (acceptance tests 1-4)
# ---------------------------------------------------------------------------


class _CascadeCliFixture(unittest.TestCase):
    """Shared fixture: patches TICKETS_ROOT/REPO_ROOT to a tempdir and mocks
    `tools._git_utils.subprocess.run` so `cmd_cascade` runs with no real git
    or network calls. Trimmed sibling of tools/test_ticket.py's
    TestFullLifecycleGitCommands fixture — cascade never does `git mv` (only
    frontmatter edits, same shape as `block --clear-dep`), so no mv-handling
    branch is needed in the fake `git` responder.
    """

    def setUp(self) -> None:
        # T-1816/T-1931: reset the module-global _git_common_dir cache so
        # this class's mocked-empty-stdout rev-parse responses can't poison
        # a later test's real resolution, and so an earlier real resolution
        # can't short-circuit this class's mocked path. Same reset
        # tools/test_ticket.py::TestFullLifecycleGitCommands performs.
        _git_utils._git_common_dir_cache = None
        self.addCleanup(setattr, _git_utils, "_git_common_dir_cache", None)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self.addCleanup(self._repo_patch.stop)
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self.addCleanup(self._gu_repo_patch.stop)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        self._tickets_patch.start()
        self.addCleanup(self._tickets_patch.stop)

        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

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
        self.addCleanup(self._sub_patch.stop)
        self.mock_run.side_effect = self._fake_run

    def _fake_run(self, argv, **kwargs):
        if argv[:2] == ["git", "add"] and "--" in argv:
            idx = argv.index("--")
            paths = argv[idx + 1:]
            missing = [p for p in paths if not (self.root / p).exists()]
            if missing:
                return MagicMock(
                    returncode=128,
                    stdout="",
                    stderr=(
                        "fatal: pathspec '" + missing[0]
                        + "' did not match any files\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[:2] == ["git", "diff"]:
            # Reflect what real git would report from the index (post `git
            # add`, pre `git commit`). Walks call history like the sibling
            # fixture in tools/test_ticket.py does.
            staged_set: set[str] = set()
            for prior in self.mock_run.call_args_list:
                pa = prior.args[0]
                if pa[:2] == ["git", "add"] and "--" in pa:
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

    def _cascade_args(self, tid: str, no_push: bool = False) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.tid = tid
        ns.no_push = no_push
        return ns

    def _commit_messages(self) -> list[str]:
        return [
            c.args[0][3]
            for c in self.mock_run.call_args_list
            if c.args[0][:3] == ["git", "commit", "-m"]
        ]

    def _commit_count(self) -> int:
        return sum(
            1 for c in self.mock_run.call_args_list if c.args[0][:2] == ["git", "commit"]
        )


class TestCmdCascadeHappyPath(_CascadeCliFixture):
    """Acceptance 1: single-dep-done fixture clears the dep and commits with
    the 'cascade: <tid> → N dependant updated'-style subject."""

    def test_clears_dep_and_commits_with_expected_subject(self) -> None:
        _write_ticket(self.root / "tickets" / "done" / "T-9099.md", tid="T-9099", state="done")
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9101.md", tid="T-9101", state="inbox",
            depends_on=["T-9099"],
        )

        rc = ticket.cmd_cascade(self._cascade_args("T-9099"))

        self.assertEqual(rc, 0)
        text = (self.root / "tickets" / "inbox" / "T-9101.md").read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields["depends_on"], [])
        msgs = self._commit_messages()
        self.assertEqual(len(msgs), 1)
        self.assertTrue(
            msgs[0].startswith("cascade: T-9099 → 1 dependant updated"),
            f"unexpected commit subject: {msgs[0]!r}",
        )


class TestCmdCascadeMultiDepMixedState(_CascadeCliFixture):
    """Acceptance 2: a dependant with a still-not-done sibling dep is left
    untouched, and no commit is created."""

    def test_skips_dependant_with_unresolved_sibling_dep_no_commit(self) -> None:
        _write_ticket(self.root / "tickets" / "done" / "T-9199.md", tid="T-9199", state="done")
        _write_ticket(self.root / "tickets" / "inbox" / "T-9198.md", tid="T-9198", state="inbox")
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9202.md", tid="T-9202", state="inbox",
            depends_on=["T-9199", "T-9198"],
        )

        rc = ticket.cmd_cascade(self._cascade_args("T-9199"))

        self.assertEqual(rc, 0)
        text = (self.root / "tickets" / "inbox" / "T-9202.md").read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields["depends_on"], ["T-9199", "T-9198"])
        self.assertEqual(self._commit_count(), 0)


class TestCmdCascadeFailClosed(_CascadeCliFixture):
    """Acceptance 3: an unresolvable tid (not found anywhere, or found but
    not in done/) refuses with a typed error and touches nothing."""

    def test_unknown_tid_raises_ticket_error(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_cascade(self._cascade_args("T-9797"))
        self.assertEqual(self._commit_count(), 0)

    def test_tid_not_in_done_raises_ticket_error(self) -> None:
        _write_ticket(self.root / "tickets" / "inbox" / "T-9696.md", tid="T-9696", state="inbox")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_cascade(self._cascade_args("T-9696"))
        self.assertEqual(self._commit_count(), 0)

    def test_malformed_tid_raises_ticket_error(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_cascade(self._cascade_args("not-a-tid"))
        self.assertEqual(self._commit_count(), 0)


class TestCmdCascadeIdempotent(_CascadeCliFixture):
    """Acceptance 4: running cascade twice on the same already-cleared
    fixture produces "no cascade updates" and no second commit."""

    def test_second_run_no_cascade_updates_no_commit(self) -> None:
        _write_ticket(self.root / "tickets" / "done" / "T-9299.md", tid="T-9299", state="done")
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9301.md", tid="T-9301", state="inbox",
            depends_on=["T-9299"],
        )

        rc1 = ticket.cmd_cascade(self._cascade_args("T-9299"))
        self.assertEqual(rc1, 0)
        self.assertEqual(self._commit_count(), 1)

        rc2 = ticket.cmd_cascade(self._cascade_args("T-9299"))
        self.assertEqual(rc2, 0)
        self.assertEqual(self._commit_count(), 1)  # unchanged — no second commit


# ---------------------------------------------------------------------------
# tools/cascade_diff_scan.py — GHA diff-walk (acceptance tests 5-7)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "test@example.com"], root)
    _run_git(["config", "user.name", "Test"], root)


class TestExtractResolvedTids(unittest.TestCase):
    """Acceptance 5: real tmpdir git repo, real `git log --diff-filter=R
    --name-status --format=""` output fed into extract_resolved_tids (D11:
    real-surface validation, not a hand-invented name-status fixture)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _init_repo(self.root)

    def _commit_all(self, message: str) -> None:
        _run_git(["add", "-A"], self.root)
        _run_git(["commit", "-q", "-m", message], self.root)

    def _rename_git(self, old_rel: str, new_rel: str) -> None:
        old_path = self.root / old_rel
        new_path = self.root / new_rel
        new_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["mv", old_rel, new_rel], self.root)
        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())

    def _name_status_since(self, before_sha: str) -> str:
        res = _run_git(
            ["log", "--diff-filter=R", "--name-status", "--format=", f"{before_sha}..HEAD"],
            self.root,
        )
        return res.stdout

    def test_identifies_inbox_and_active_resolves_skips_others(self) -> None:
        (self.root / "tickets" / "inbox").mkdir(parents=True)
        (self.root / "tickets" / "active").mkdir(parents=True)
        (self.root / "tickets" / "T-A.md").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "tickets" / "inbox" / "T-9401.md").write_text("A\n", encoding="utf-8")
        (self.root / "tickets" / "active" / "T-9402.md").write_text("B\n", encoding="utf-8")
        (self.root / "tickets" / "inbox" / "T-9403.md").write_text("C\n", encoding="utf-8")
        (self.root / "notes.txt").write_text("unrelated\n", encoding="utf-8")
        self._commit_all("seed tickets")
        before = _run_git(["rev-parse", "HEAD"], self.root).stdout.strip()

        # Simulate a squash-merge landing several ops in one commit:
        #   inbox -> done   (should be captured)
        #   active -> done  (should be captured)
        #   inbox -> active (a claim — must be skipped, not a resolve)
        #   an unrelated file rename (must be skipped)
        self._rename_git("tickets/inbox/T-9401.md", "tickets/done/T-9401.md")
        self._rename_git("tickets/active/T-9402.md", "tickets/done/T-9402.md")
        self._rename_git("tickets/inbox/T-9403.md", "tickets/active/T-9403.md")
        self._rename_git("notes.txt", "renamed-notes.txt")
        self._commit_all("squash-merge: resolve T-9401, T-9402; claim T-9403")

        name_status = self._name_status_since(before)
        tids = cascade_diff_scan.extract_resolved_tids(name_status)

        self.assertEqual(sorted(tids), ["T-9401", "T-9402"])

    def test_empty_input_no_tids(self) -> None:
        self.assertEqual(cascade_diff_scan.extract_resolved_tids(""), [])


class TestRunCascadeForTids(unittest.TestCase):
    """Acceptance 6: a single push containing a mixed inbox->done /
    active->done batch invokes `ticket.py cascade --tid <tid>` for every
    tid found, in order. Mocks subprocess.run for the invocation step
    itself — a real end-to-end `ticket.py cascade` run is structurally
    refused by `_assert_cli_checkout_allowed` from this `agent/*`-branch
    worktree, and TestCmdCascade* above already covers cascade's real
    logic; this only needs to verify the glue's invocation shape/order."""

    def test_invokes_cascade_for_every_tid_in_order(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return MagicMock(returncode=0)

        with patch("tools.cascade_diff_scan.subprocess.run", side_effect=fake_run):
            results = cascade_diff_scan.run_cascade_for_tids(
                ["T-9501", "T-9502", "T-9503"], repo_root=Path("/fake/repo")
            )

        self.assertEqual(results, [("T-9501", 0), ("T-9502", 0), ("T-9503", 0)])
        self.assertEqual(len(calls), 3)
        for expected_tid, argv in zip(["T-9501", "T-9502", "T-9503"], calls):
            self.assertEqual(argv[-2:], ["--tid", expected_tid])
            self.assertEqual(argv[-3], "cascade")
            self.assertTrue(argv[-4].endswith("ticket.py"))

    def test_one_failure_does_not_block_the_rest(self) -> None:
        def fake_run(argv, **kwargs):
            tid = argv[-1]
            return MagicMock(returncode=1 if tid == "T-9602" else 0)

        with patch("tools.cascade_diff_scan.subprocess.run", side_effect=fake_run):
            results = cascade_diff_scan.run_cascade_for_tids(
                ["T-9601", "T-9602", "T-9603"], repo_root=Path("/fake/repo")
            )

        self.assertEqual(results, [("T-9601", 0), ("T-9602", 1), ("T-9603", 0)])


class TestCascadeDiffScanMain(unittest.TestCase):
    """CLI entry-point exit-code contract, mirrors adr_scoping_lint's
    TestMain shape."""

    def test_main_returns_zero_when_no_tids_found(self) -> None:
        import io

        with patch("sys.stdin", io.StringIO("")):
            rc = cascade_diff_scan.main()
        self.assertEqual(rc, 0)

    def test_main_returns_zero_when_all_cascades_succeed(self) -> None:
        import io

        def fake_run(argv, **kwargs):
            return MagicMock(returncode=0)

        text = "R094\ttickets/inbox/T-9701.md\ttickets/done/T-9701.md\n"
        with patch("sys.stdin", io.StringIO(text)), \
                patch("tools.cascade_diff_scan.subprocess.run", side_effect=fake_run):
            rc = cascade_diff_scan.main()
        self.assertEqual(rc, 0)

    def test_main_returns_one_when_any_cascade_fails(self) -> None:
        import io

        def fake_run(argv, **kwargs):
            return MagicMock(returncode=1)

        text = "R094\ttickets/inbox/T-9702.md\ttickets/done/T-9702.md\n"
        with patch("sys.stdin", io.StringIO(text)), \
                patch("tools.cascade_diff_scan.subprocess.run", side_effect=fake_run):
            rc = cascade_diff_scan.main()
        self.assertEqual(rc, 1)


class TestWorkflowNoSelfRetrigger(unittest.TestCase):
    """Acceptance 7: the cascade subcommand's OWN commit only edits
    depends_on frontmatter in place (no `git mv`) — a real git repo proves
    that produces zero rename ops, so the workflow's own commit can never
    feed itself a resolved tid (no infinite loop)."""

    def test_frontmatter_only_commit_yields_zero_rename_ops(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _init_repo(root)

        (root / "tickets" / "inbox").mkdir(parents=True)
        ticket_path = root / "tickets" / "inbox" / "T-9801.md"
        ticket_path.write_text("depends_on: [T-9800]\n", encoding="utf-8")
        _run_git(["add", "-A"], root)
        _run_git(["commit", "-q", "-m", "seed"], root)
        before = _run_git(["rev-parse", "HEAD"], root).stdout.strip()

        # Cascade's own commit: content edit, no rename.
        ticket_path.write_text("depends_on: []\n", encoding="utf-8")
        _run_git(["add", "-A"], root)
        _run_git(
            ["commit", "-q", "-m", "cascade: T-9800 → 1 dependant updated"], root
        )

        res = _run_git(
            ["log", "--diff-filter=R", "--name-status", "--format=", f"{before}..HEAD"],
            root,
        )
        tids = cascade_diff_scan.extract_resolved_tids(res.stdout)
        self.assertEqual(tids, [])


if __name__ == "__main__":
    unittest.main()
