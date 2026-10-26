"""T-4885: `ticket.py file` must never silently overwrite another agent's ticket.

Root cause (2026-10-12, T-4882): cmd_file allocated the id BEFORE its pull-first
step and then wrote with a plain overwrite, so an upstream same-id file brought
in by the pull was clobbered and the commit became a MODIFICATION that pushed
as a fast-forward (the T-0786 non-ff re-allocation never fired).

Fixtures: a real temp git repo + local bare origin, every ticket.py git call
re-pointed at the temp clone. No network, no live mailbox, no real remote.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket


_resolve_patch = patch("tools.ticket_lint.ratifiable_path_status", return_value=True)


def setUpModule() -> None:
    # T-5014: fixtures attach a synthetic ratifiable path; skip the file-time
    # existence check (it is covered by test_ticket.TestOperatorAttachmentGate).
    _resolve_patch.start()


def tearDownModule() -> None:
    _resolve_patch.stop()

FOREIGN = (
    "---\nid: T-FOREIGN\ncreated: 2026-10-12T21:13:34Z\nrequester: orchestrator\n"
    "target: recovery\nstate: inbox\n---\n\n# someone else's freshly filed ticket\n"
)


def _run(cwd: Path, *argv: str) -> str:
    res = subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    )
    return res.stdout


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        origin = base / "origin.git"
        self.root = base / "work"
        _run(base, "init", "--bare", "-b", "main", str(origin))
        _run(base, "clone", str(origin), str(self.root))
        _run(self.root, "config", "user.email", "t@example.invalid")
        _run(self.root, "config", "user.name", "t")
        _run(self.root, "checkout", "-b", "main")
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True)
            (self.root / "tickets" / s / ".gitkeep").write_text("")
        (self.root / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n\n## Related\n\n- Link\n",
            encoding="utf-8",
        )
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "init")
        _run(self.root, "push", "origin", "main")

        root = self.root
        real_git = _git_utils._git

        def _git_in_root(*a, cwd=None):
            return real_git(*a, cwd=cwd if cwd is not None else root)

        self._patches = [
            patch.object(ticket, "REPO_ROOT", root),
            patch.object(ticket, "TICKETS_ROOT", root / "tickets"),
            patch.object(_git_utils, "REPO_ROOT", root),
            patch.object(ticket, "_git", _git_in_root),
            patch.dict(os.environ, {
                "PANTHEON_ACTOR": "orchestrator",
                "PANTHEON_TICKET_ALLOC_LOCKFILE": str(base / "alloc.lock"),
            }),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def inbox(self) -> Path:
        return self.root / "tickets" / "inbox"

    def file_args(self, **over) -> MagicMock:
        a = MagicMock()
        a.requester = "orchestrator"
        a.target = "role:system_orchestrator"
        a.title = "Overwrite race test ticket"
        a.labels = None
        a.depends_on = None
        a.stale_after = None
        a.not_before = None
        a.ratifiable_file = None
        a.ask = "test"
        a.context = "ctx"
        a.parent_ticket = None
        a.no_push = True
        a.spawn_impl = False
        a.terminal = False
        a.impl_title = None
        a.impl_ask = None
        a.appetite = None
        a.bet_ref = None
        a.impl_target = None
        a.impl_existing = None  # T-5291
        a.orphan = "test fixture: overwrite race mechanics, not DAG shape"
        # T-5014: gate-orthogonal fixture; operator-without-files gets an explicit no-file reason.
        a.no_file = (
            "test fixture: T-5014 gate is orthogonal to this test"
            if getattr(a, "target", None) == "operator" and not getattr(a, "ratifiable_file", None)
            else None
        )
        a.intent = None
        for k, v in over.items():
            setattr(a, k, v)
        return a

    def drop_foreign_after_alloc(self, on_calls: set[int] | None):
        """Wrap _next_ticket_id: after the real allocation on the chosen call
        numbers (None = every call), land a foreign file at the id just handed
        out -- an upstream same-id file arriving between allocation and write."""
        real = ticket._next_ticket_id
        calls = {"n": 0}
        foreign: dict[str, bytes] = {}
        root = self.root

        def wrapper() -> str:
            calls["n"] += 1
            tid = real()
            if on_calls is None or calls["n"] in on_calls:
                p = root / "tickets" / "inbox" / f"{tid}.md"
                data = FOREIGN.replace("T-FOREIGN", tid).encode()
                p.write_bytes(data)
                foreign[str(p)] = data
            return tid

        return wrapper, foreign

    def assertForeignIntact(self, foreign: dict[str, bytes]) -> None:
        self.assertTrue(foreign)
        for path, data in foreign.items():
            self.assertTrue(Path(path).exists(), f"foreign file deleted: {path}")
            self.assertEqual(Path(path).read_bytes(), data, f"foreign file modified: {path}")


class TestFileNeverOverwrites(_RepoCase):
    def test_pull_runs_before_allocation(self) -> None:
        order: list[str] = []
        real_pull, real_next = ticket._pull_if_needed, ticket._next_ticket_id
        with patch.object(ticket, "_pull_if_needed",
                          side_effect=lambda *a, **k: (order.append("pull"), real_pull(*a, **k))[1]), \
             patch.object(ticket, "_next_ticket_id",
                          side_effect=lambda: (order.append("alloc"), real_next())[1]):
            self.assertEqual(ticket.cmd_file(self.file_args()), 0)
        self.assertEqual(order[:2], ["pull", "alloc"], order)

    def test_upstream_same_id_between_alloc_and_write_keeps_both(self) -> None:
        wrapper, foreign = self.drop_foreign_after_alloc({1})
        with patch.object(ticket, "_next_ticket_id", wrapper):
            self.assertEqual(ticket.cmd_file(self.file_args()), 0)
        self.assertForeignIntact(foreign)
        names = sorted(p.name for p in self.inbox().glob("T-*.md"))
        self.assertEqual(len(names), 2, names)
        self.assertEqual(len(set(names)), 2)
        # the new ticket landed as an ADD, never a modification
        status = _run(self.root, "show", "--name-status", "--format=", "HEAD")
        ours = [n for n in names if f"tickets/inbox/{n}" in status]
        self.assertEqual(len(ours), 1, status)
        self.assertIn(f"A\ttickets/inbox/{ours[0]}", status)

    def test_spawn_impl_child_collision_keeps_foreign_and_orphans_nothing(self) -> None:
        # call 1 = parent alloc, call 2 = child alloc (collides), 3/4 = retry.
        wrapper, foreign = self.drop_foreign_after_alloc({2})
        args = self.file_args(
            target="operator", intent=None, spawn_impl=True,
            impl_title="impl child", impl_target="role:system_orchestrator",
            ratifiable_file=["pantheon-config:sop/standard/x.md"],
        )
        with patch.object(ticket, "_next_ticket_id", wrapper):
            self.assertEqual(ticket.cmd_file(args), 0)
        self.assertForeignIntact(foreign)
        names = sorted(p.name for p in self.inbox().glob("T-*.md"))
        # foreign + parent + child; the aborted attempt's parent is not orphaned.
        self.assertEqual(len(names), 3, names)

    def test_persistent_collision_exhausts_retries_without_touching_foreign(self) -> None:
        wrapper, foreign = self.drop_foreign_after_alloc(None)
        with patch.object(ticket, "_next_ticket_id", wrapper):
            with self.assertRaises(ticket.TicketError):
                ticket.cmd_file(self.file_args())
        self.assertForeignIntact(foreign)
        self.assertEqual(len(foreign), ticket._MAX_FILE_RETRIES)
        self.assertEqual(len(list(self.inbox().glob("T-*.md"))), ticket._MAX_FILE_RETRIES)


class TestImplExistingRebind(_RepoCase):
    """T-5291: `file --impl-existing` on a real temp repo. The existing impl
    ticket is MODIFIED in the parent's atomic commit, and every re-allocation
    (same-id collision or push race) re-binds its depends_on to the FINAL tid."""

    EXISTING = "T-0050"

    def setUp(self) -> None:
        super().setUp()
        self.existing = self.inbox() / f"{self.EXISTING}.md"
        self.existing.write_text(
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
        self.seed_bytes = self.existing.read_bytes()

    def ratify_args(self, **over) -> MagicMock:
        return self.file_args(
            target="operator", intent=None, impl_existing=self.EXISTING,
            ratifiable_file=["pantheon-config:sop/standard/x.md"], **over,
        )

    def assertBoundTo(self, tid: str) -> None:
        text = self.existing.read_text(encoding="utf-8")
        self.assertIn(f"depends_on: [{tid}]", text)
        self.assertEqual(text.count("gated on ratify"), 1, text)
        status = _run(self.root, "show", "--name-status", "--format=", "HEAD")
        self.assertIn(f"A\ttickets/inbox/{tid}.md", status)
        self.assertIn(f"M\ttickets/inbox/{self.EXISTING}.md", status)
        dirty = _run(self.root, "status", "--porcelain", "--", str(self.existing))
        self.assertEqual(dirty.strip(), "", "existing ticket left dirty")

    def test_happy_path_one_commit(self) -> None:
        self.assertEqual(ticket.cmd_file(self.ratify_args()), 0)
        self.assertBoundTo("T-0051")

    def test_same_id_collision_rebinds_to_fresh_tid(self) -> None:
        wrapper, foreign = self.drop_foreign_after_alloc({1})
        with patch.object(ticket, "_next_ticket_id", wrapper):
            self.assertEqual(ticket.cmd_file(self.ratify_args()), 0)
        self.assertForeignIntact(foreign)
        self.assertBoundTo("T-0052")
        self.assertNotIn("T-0051", self.existing.read_text(encoding="utf-8"))

    def test_push_race_restores_then_rebinds_to_fresh_tid(self) -> None:
        # attempt 1: T-0051 commits, push loses the race -> reset + restore.
        # attempt 2: T-0051 collides with a foreign file -> re-allocate.
        # attempt 3: T-0052 commits + pushes. The edit must name ONLY T-0052.
        wrapper, foreign = self.drop_foreign_after_alloc({2})
        pushes = iter([(False, "non-fast-forward"), (True, "ok")])
        # no_push=False only so _commit_and_push reaches the (stubbed) push; the
        # pull-first step is not under test and is stubbed out (T-4934 guard).
        with patch.object(ticket, "_next_ticket_id", wrapper), \
             patch.object(ticket, "_pull_if_needed"), \
             patch.object(ticket, "push_with_retry", side_effect=lambda *a, **k: next(pushes)):
            self.assertEqual(ticket.cmd_file(self.ratify_args(no_push=False)), 0)
        self.assertForeignIntact(foreign)
        self.assertBoundTo("T-0052")
        self.assertNotIn("T-0051", self.existing.read_text(encoding="utf-8"))

    def test_cycle_refusal_leaves_no_orphan_parent_and_existing_untouched(self) -> None:
        args = self.ratify_args(depends_on=self.EXISTING, orphan=None)
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_file(args)
        self.assertIn("cycle", str(cm.exception))
        self.assertEqual(sorted(p.name for p in self.inbox().glob("T-*.md")),
                         [f"{self.EXISTING}.md"])
        self.assertEqual(self.existing.read_bytes(), self.seed_bytes)

    # --- T-5291 r1: every failure path leaves the existing ticket untouched ---

    def assertExistingPristine(self) -> None:
        self.assertEqual(self.existing.read_bytes(), self.seed_bytes)
        porcelain = _run(self.root, "status", "--porcelain", "--untracked-files=all",
                         "--", "tickets/inbox")
        self.assertEqual(porcelain.strip(), "", f"tickets/inbox left dirty: {porcelain!r}")
        self.assertEqual(_run(self.root, "diff", "--cached", "--name-only").strip(), "")

    def test_non_push_commit_failure_restores_existing_and_unstages(self) -> None:
        root = self.root

        def stage_then_refuse(paths, *a, **k):
            # stage like the real guard does, then refuse (HERMES-style)
            _run(root, "add", "--", *[str(p_) for p_ in paths])
            raise ticket.TicketRefusal("HERMES guard refused: test")

        with patch.object(ticket, "_stage_and_commit_scoped", side_effect=stage_then_refuse):
            with self.assertRaises(ticket.TicketRefusal) as cm:
                ticket.cmd_file(self.ratify_args())
        self.assertIn("HERMES", str(cm.exception))
        # README.md regeneration is pre-existing behaviour outside T-5291's scope.
        _run(self.root, "reset", "-q", "HEAD", "--", "tickets/README.md")
        _run(self.root, "checkout", "--", "tickets/README.md")
        self.assertExistingPristine()

    def test_post_commit_failure_never_reverts_the_commit(self) -> None:
        # T-5291 r2: an exception AFTER a successful commit (e.g. BrokenPipeError
        # from the near-duplicate warning) must not undo the landed edit.
        with patch.object(ticket, "_warn_near_duplicates", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                ticket.cmd_file(self.ratify_args())
        self.assertTrue((self.inbox() / "T-0051.md").exists())
        self.assertBoundTo("T-0051")

    def test_push_exhaustion_leaves_existing_pristine(self) -> None:
        with patch.object(ticket, "_pull_if_needed"), \
             patch.object(ticket, "push_with_retry", return_value=(False, "non-fast-forward")):
            with self.assertRaises(ticket.TicketError) as cm:
                ticket.cmd_file(self.ratify_args(no_push=False))
        self.assertIn("git push failed", str(cm.exception))
        self.assertExistingPristine()

    def test_in_lock_recheck_refuses_when_existing_moved_to_done(self) -> None:
        done = self.root / "tickets" / "done" / f"{self.EXISTING}.md"

        def pull_closes_it(*a, **k):
            if self.existing.exists():
                self.existing.rename(done)

        with patch.object(ticket, "_pull_if_needed", side_effect=pull_closes_it):
            with self.assertRaises(ticket.TicketRefusal) as cm:
                ticket.cmd_file(self.ratify_args())
        self.assertIn("moved to `done`", str(cm.exception))
        self.assertEqual(list(self.inbox().glob("T-*.md")), [])  # no orphan parent
        self.assertEqual(done.read_bytes(), self.seed_bytes)


class TestExclusiveWrite(unittest.TestCase):
    def test_refuses_existing_path_and_leaves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "T-0001.md"
            p.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                ticket._write_new_ticket_exclusive(p, "new")
            self.assertEqual(p.read_bytes(), b"original")

    def test_creates_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "T-0001.md"
            ticket._write_new_ticket_exclusive(p, "new")
            self.assertEqual(p.read_text(), "new")


class TestAddAssertion(_RepoCase):
    def _commit_existing(self) -> Path:
        p = self.inbox() / "T-0001.md"
        p.write_text("one\n")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "seed")
        return p

    def test_modification_of_required_add_is_refused_and_index_clean(self) -> None:
        p = self._commit_existing()
        p.write_text("two\n")
        with self.assertRaises(ticket.TicketRefusal) as ctx:
            ticket._stage_and_commit_scoped(
                [p], "msg", repo_root=self.root, require_added=[p]
            )
        self.assertIn("T-0001", str(ctx.exception))
        self.assertEqual(_run(self.root, "diff", "--cached", "--name-only").strip(), "")
        self.assertEqual(_run(self.root, "log", "--format=%s", "-1").strip(), "seed")

    def test_true_add_passes(self) -> None:
        p = self.inbox() / "T-0002.md"
        p.write_text("new\n")
        ticket._stage_and_commit_scoped(
            [p], "msg", repo_root=self.root, require_added=[p]
        )
        self.assertEqual(_run(self.root, "log", "--format=%s", "-1").strip(), "msg")

    def test_default_none_keeps_modification_commits_working(self) -> None:
        p = self._commit_existing()
        p.write_text("two\n")
        ticket._stage_and_commit_scoped([p], "edit", repo_root=self.root)
        self.assertEqual(_run(self.root, "log", "--format=%s", "-1").strip(), "edit")


if __name__ == "__main__":  # keep last (main-guard lint)
    unittest.main()
