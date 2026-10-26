"""T-5406: `ticket.py file` must make every tracked-file mutation, the pull, and
the push-failure undo inside the SAME git_mutation_lock span that commits.

Before the fix cmd_file pulled, allocated, wrote (and, for `--impl-existing`,
edited a TRACKED ticket in place) under only the allocation lock, released, then
re-took git_mutation_lock to commit. In that gap another lock holder could
`pull --rebase`, which fails "cannot rebase: You have unstaged changes" and
leaves the shared checkout wedged. The push-failure undo (`reset HEAD~1`) also
ran after the lock was released.

Determinism: no sleeps or threads. `held()` tries to take the SAME lockfile with
a short timeout from a second open file description; flock self-contends across
descriptions even in one process, so it raises iff the lock is currently held.
Probes are planted at the exact seams where the old code had a gap.
"""

from __future__ import annotations

from unittest.mock import patch

from tools import _git_utils, ticket
from tools.test_ticket_file_overwrite_race import _RepoCase, _run

_resolve_patch = patch("tools.ticket_lint.ratifiable_path_status", return_value=True)


def setUpModule() -> None:
    _resolve_patch.start()  # T-5014 file-time existence check is orthogonal here


def tearDownModule() -> None:
    _resolve_patch.stop()


def held() -> bool:
    try:
        with _git_utils.git_mutation_lock(timeout_secs=0.2, label="probe"):
            return False
    except _git_utils.GitMutationLockTimeout:
        return True


class TestFileLockSpan(_RepoCase):
    def probe(self, name: str, log: list) -> None:
        log.append((name, held()))

    def wrap(self, attr: str, log: list, name: str | None = None):
        real = getattr(ticket, attr)

        def wrapper(*a, **k):
            self.probe(name or attr, log)
            return real(*a, **k)

        return patch.object(ticket, attr, side_effect=wrapper)

    def test_pull_and_commit_run_under_git_lock(self) -> None:
        log: list = []
        with self.wrap("_pull_if_needed", log), self.wrap("_commit_and_push", log):
            self.assertEqual(ticket.cmd_file(self.file_args()), 0)
        self.assertEqual(log, [("_pull_if_needed", True), ("_commit_and_push", True)])
        self.assertFalse(held(), "git lock leaked after cmd_file")

    def test_alloc_lock_nested_inside_git_lock(self) -> None:
        log: list = []
        with self.wrap("_next_ticket_id", log):
            self.assertEqual(ticket.cmd_file(self.file_args()), 0)
        self.assertEqual(log, [("_next_ticket_id", True)])

    def test_push_failure_undo_runs_under_git_lock_and_retry_succeeds(self) -> None:
        log: list = []
        real_git = ticket._git
        pushes = iter([(False, "non-fast-forward"), (True, "ok")])

        def spy_git(*a, **k):
            if a[:2] == ("reset", "HEAD~1"):
                log.append(("undo-reset", held()))
            return real_git(*a, **k)

        with patch.object(ticket, "_pull_if_needed"), \
             patch.object(ticket, "_git", side_effect=spy_git), \
             patch.object(ticket, "push_with_retry", side_effect=lambda *a, **k: next(pushes)):
            self.assertEqual(ticket.cmd_file(self.file_args(no_push=False)), 0)
        self.assertEqual(log, [("undo-reset", True)])
        # attempt 1's T-0001 was undone, attempt 2 re-allocated and committed once
        self.assertEqual(
            _run(self.root, "log", "--format=%s", "-1").count("T-"), 1)
        self.assertFalse(held())


class TestImplExistingEditUnderLock(_RepoCase):
    """T-5291 shape: a tracked existing impl ticket edited in place by `file`."""

    EXISTING = "T-0050"

    def setUp(self) -> None:
        super().setUp()
        (self.inbox() / f"{self.EXISTING}.md").write_text(
            f"---\nid: {self.EXISTING}\ncreated: 2026-10-20T00:00:00Z\n"
            "requester: orchestrator\ntarget: role:system_orchestrator\nstate: inbox\n"
            "parent_ticket: null\ndepends_on: []\n---\n\n# existing impl\n\n"
            "## Ask\n\nbuild it\n\n## Transition log\n\n"
            "- 2026-10-20T00:00:00Z [orchestrator] filed in inbox\n",
            encoding="utf-8",
        )
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "seed existing impl")
        _run(self.root, "push", "origin", "main")

    def ratify_args(self):
        return self.file_args(
            target="operator", intent=None, impl_existing=self.EXISTING,
            ratifiable_file=["pantheon-config:sop/standard/x.md"],
        )

    def test_tracked_edit_is_made_and_committed_under_one_lock_span(self) -> None:
        seen: list = []
        real = ticket._link_existing_impl_to_ratify

        def spy(impl_tid, ratify_tid):
            out = real(impl_tid, ratify_tid)
            porcelain = _run(self.root, "status", "--porcelain",
                             "--", f"tickets/inbox/{self.EXISTING}.md")
            # after the edit the tracked file IS modified; the lock must be held
            seen.append((porcelain.startswith(" M"), held()))
            return out

        with patch.object(ticket, "_link_existing_impl_to_ratify", side_effect=spy):
            self.assertEqual(ticket.cmd_file(self.ratify_args()), 0)
        self.assertEqual(seen, [(True, True)])
        text = (self.inbox() / f"{self.EXISTING}.md").read_text(encoding="utf-8")
        self.assertIn("depends_on: [T-0051]", text)
        self.assertFalse(held())
