"""Unit tests for `tools/ticket.py`'s `cascade` subcommand (T-3289: ORG-013 cascade
trigger; T-4186: the level-triggered sweep in tools/cascade_sweep.py now drives it, and the
diff-walk workflow + tools/cascade_diff_scan.py this file once also covered are retired).

Run from repo root:
    python3 -m unittest tools.test_ticket_cascade -v

Closes T-3288 (ORG-013 cascade-unblock silently misses on PR-squash-merge):
`ticket.py resolve` fires the cascade via `_do_transition`'s
cascade_callback hook; a ticket resolved via a GitHub squash-merge never
calls `ticket.py resolve`, so the cascade never fires and dependants keep
stale `depends_on:` refs indefinitely. `ticket.py cascade --tid <T-NNNN>`
(TestCmdCascade*) is the standalone trigger the mechanism was missing.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket


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
    """Acceptance 2 (T-3933 update): a dependant with a still-not-done sibling
    dep gets the resolved dep STRIPPED (partial-strip, always fires) but is
    NOT fully unblocked — the still-open sibling remains as a live gate. A
    commit IS created (the frontmatter changed), distinct from full unblock.
    """

    def test_partial_strip_removes_resolved_dep_leaves_sibling_gate(self) -> None:
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
        self.assertEqual(fields["depends_on"], ["T-9198"],
                         "resolved dep T-9199 must be stripped even though T-9198 still gates")
        self.assertNotIn("auto-unblock", text)
        self.assertIn("[cascade]", text)
        self.assertEqual(self._commit_count(), 1)
        msgs = self._commit_messages()
        self.assertTrue(
            msgs[0].startswith("cascade: T-9199 → 1 dependant updated"),
            f"unexpected commit subject: {msgs[0]!r}",
        )


class TestCmdCascadeLegAwareCommitMessage(_CascadeCliFixture):
    """T-3941: cmd_cascade's commit-message BODY must distinguish partial-strip
    (stale-done ref pruned; still gated) from auto-unblock (fully deliverable).
    Prior wording ("Auto-unblock cascade per ORG-013") claimed unblock even
    when only the partial-strip leg fired — cosmetic drift, not correctness.
    """

    def _body_of(self, subject_startswith: str) -> str:
        for msg in self._commit_messages():
            if msg.startswith(subject_startswith):
                return msg
        raise AssertionError(
            f"no commit whose subject starts with {subject_startswith!r}; "
            f"got: {self._commit_messages()!r}"
        )

    def test_message_says_partial_strip_only_when_no_full_unblock_fires(self) -> None:
        _write_ticket(self.root / "tickets" / "done" / "T-9299.md", tid="T-9299", state="done")
        _write_ticket(self.root / "tickets" / "inbox" / "T-9298.md", tid="T-9298", state="inbox")
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9302.md", tid="T-9302", state="inbox",
            depends_on=["T-9299", "T-9298"],
        )

        rc = ticket.cmd_cascade(self._cascade_args("T-9299"))

        self.assertEqual(rc, 0)
        body = self._body_of("cascade: T-9299")
        self.assertIn("1 partial-strip", body)
        self.assertNotIn("auto-unblock (fully deliverable", body)
        self.assertNotIn("Auto-unblock cascade per ORG-013", body)  # retired wording

    def test_message_says_auto_unblock_only_when_all_fires_are_full(self) -> None:
        _write_ticket(self.root / "tickets" / "done" / "T-9399.md", tid="T-9399", state="done")
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9402.md", tid="T-9402", state="inbox",
            depends_on=["T-9399"],
        )

        rc = ticket.cmd_cascade(self._cascade_args("T-9399"))

        self.assertEqual(rc, 0)
        body = self._body_of("cascade: T-9399")
        self.assertIn("1 auto-unblock (fully deliverable", body)
        self.assertNotIn("partial-strip", body)

    def test_message_lists_both_when_fires_mix_across_dependants(self) -> None:
        _write_ticket(self.root / "tickets" / "done" / "T-9499.md", tid="T-9499", state="done")
        # dependant 1: only depends on the resolved tid — full unblock
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9501.md", tid="T-9501", state="inbox",
            depends_on=["T-9499"],
        )
        # dependant 2: also depends on a still-open sibling — partial-strip
        _write_ticket(self.root / "tickets" / "inbox" / "T-9498.md", tid="T-9498", state="inbox")
        _write_ticket(
            self.root / "tickets" / "inbox" / "T-9502.md", tid="T-9502", state="inbox",
            depends_on=["T-9499", "T-9498"],
        )

        rc = ticket.cmd_cascade(self._cascade_args("T-9499"))

        self.assertEqual(rc, 0)
        body = self._body_of("cascade: T-9499")
        self.assertIn("1 partial-strip", body)
        self.assertIn("1 auto-unblock (fully deliverable", body)


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


class TestCmdCascadeRefusesDroppedParent(_CascadeCliFixture):
    """T-4186 ruling 3 / ORG-089: `dropped != resolved`. `cmd_cascade` used to check only that the
    parent is in done/, and `_cascade_unblock_dependants` strips the parent from every dependant
    UNCONDITIONALLY, so `ticket.py cascade --tid <dropped-tid>` silently removed a dropped bet
    from its dependants' depends_on and un-gated work whose dependency never shipped."""

    def _drop(self, tid: str) -> None:
        path = self.root / "tickets" / "done" / f"{tid}.md"
        _write_ticket(path, tid=tid, state="done")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("state: done\n", "state: done\nsubstate: dropped\n", 1),
                        encoding="utf-8")

    def test_dropped_parent_is_refused_and_dependants_keep_their_gate(self) -> None:
        self._drop("T-9899")
        dep = self.root / "tickets" / "inbox" / "T-9901.md"
        _write_ticket(dep, tid="T-9901", state="inbox", depends_on=["T-9899"])
        before = dep.read_text(encoding="utf-8")

        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_cascade(self._cascade_args("T-9899"))

        self.assertIn("dropped", str(ctx.exception))
        self.assertEqual(dep.read_text(encoding="utf-8"), before)  # untouched
        self.assertEqual(self._commit_count(), 0)

    # The two guards in cmd_cascade MASK EACH OTHER: with both present, the test above is green
    # whichever one is deleted (the survivor raises the same TicketError). So each is pinned by
    # an observable only IT can produce. (T-4186 r2, reviewer finding.)

    def test_pre_lock_guard_refuses_before_the_lock_or_the_pull_is_touched(self) -> None:
        """A dropped tid must be refused BEFORE `git_mutation_lock` and `_pull_if_needed`: a
        fail-fast caller error must not take the checkout-wide lock or hit the network. Deleting
        only the pre-lock call still raises (the in-lock guard catches it) -- but only after
        the lock was taken and the pull ran, which is what this asserts never happens."""
        self._drop("T-9899")
        _write_ticket(self.root / "tickets" / "inbox" / "T-9901.md", tid="T-9901", state="inbox",
                      depends_on=["T-9899"])
        with patch.object(ticket, "_pull_if_needed") as pull, \
                patch.object(ticket, "git_mutation_lock") as lock:
            with self.assertRaises(ticket.TicketError) as ctx:
                ticket.cmd_cascade(self._cascade_args("T-9899"))
        self.assertIn("dropped", str(ctx.exception))
        pull.assert_not_called()
        lock.assert_not_called()

    def test_in_lock_guard_catches_a_drop_that_lands_via_the_pull(self) -> None:
        """The pre-lock check proves nothing by the time the lock is held (T-2140): the pull can
        bring in another seat's transition of this very tid. Simulate a `substate: dropped` version
        of the done/ file arriving mid-flight. Deleting only the in-lock call lets the cascade run
        and strip the dropped bet, so this asserts: TicketError, dependant untouched, 0 commits."""
        _write_ticket(self.root / "tickets" / "done" / "T-9899.md", tid="T-9899", state="done")
        dep = self.root / "tickets" / "inbox" / "T-9901.md"
        _write_ticket(dep, tid="T-9901", state="inbox", depends_on=["T-9899"])
        before = dep.read_text(encoding="utf-8")
        done = self.root / "tickets" / "done" / "T-9899.md"

        def concurrent_drop_lands(no_push: bool) -> None:
            text = done.read_text(encoding="utf-8")
            done.write_text(text.replace("state: done\n", "state: done\nsubstate: dropped\n", 1),
                            encoding="utf-8")

        with patch.object(ticket, "_pull_if_needed", side_effect=concurrent_drop_lands) as pull:
            with self.assertRaises(ticket.TicketError) as ctx:
                ticket.cmd_cascade(self._cascade_args("T-9899"))

        pull.assert_called_once()  # the pre-lock guard passed: it really was shipped when checked
        self.assertIn("dropped", str(ctx.exception))
        self.assertEqual(dep.read_text(encoding="utf-8"), before)
        self.assertEqual(self._commit_count(), 0)

    def test_shipped_parent_still_cascades(self) -> None:
        # The guard must not over-refuse: a done/ ticket with no substate is shipped.
        _write_ticket(self.root / "tickets" / "done" / "T-9799.md", tid="T-9799", state="done")
        _write_ticket(self.root / "tickets" / "inbox" / "T-9801.md", tid="T-9801", state="inbox",
                      depends_on=["T-9799"])
        self.assertEqual(ticket.cmd_cascade(self._cascade_args("T-9799")), 0)
        self.assertEqual(self._commit_count(), 1)

    def test_drop_verb_never_runs_the_cascade_callback(self) -> None:
        """The other half of the ruling: is a DROP already cascading? No -- and this pins it.
        `cmd_drop` calls `_do_transition` without a `cascade_callback`, so dropping a ticket
        never strips its dependants (resolve / close-noodle / close-action are the only
        callers that pass one, and none of them can produce `substate: dropped`)."""
        _write_ticket(self.root / "tickets" / "active" / "T-9899.md", tid="T-9899", state="active")
        _write_ticket(self.root / "tickets" / "inbox" / "T-9901.md", tid="T-9901", state="inbox",
                      depends_on=["T-9899"])
        args = argparse.Namespace(ticket="T-9899", dropped_reason="not worth it", no_push=True)
        with patch.object(ticket, "_do_transition", return_value=0) as transition:
            self.assertEqual(ticket.cmd_drop(args), 0)
        self.assertIsNone(transition.call_args.kwargs.get("cascade_callback"))
        self.assertEqual(transition.call_args.kwargs["field_updates"]["substate"], "dropped")


if __name__ == "__main__":
    unittest.main()
