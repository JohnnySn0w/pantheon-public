"""Tests for _git_utils.push_with_retry — T-0487.

Red-before-green: these tests import push_with_retry which does not yet exist.
They must FAIL before the implementation is added, then PASS after.

Coverage:
  1. test_push_with_retry_success_first_try             — happy path; no retry
  2. test_push_with_retry_non_ff_retries_and_lands      — non-ff → fetch+rebase+retry; bare origin advances
  3. test_push_with_retry_non_ff_conflict_returns_false — rebase conflict → abort + fail-loud + clean state
  4. test_push_with_retry_exhausted_returns_false       — 3 non-ff attempts exhausted → fail-loud with message
  5. test_push_with_retry_non_non_ff_fails_immediately  — auth error → no retry, returns False immediately
  6. test_ticket_lint_push_retries_on_non_ff            — call-site integration: ticket_lint._git_commit_fixes retries

HERMES byte-sequence discipline: the literal trigger never appears contiguously in
this source file. The file names used in test helpers avoid the trigger.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import _git, push_with_retry  # noqa: E402


# ---------------------------------------------------------------------------
# Shared tmp-repo helpers (local to this module — mirrors test_git_utils_integ)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a setup command; raise on failure."""
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"setup command failed: {cmd!r}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _init_detached_repo(base: Path) -> tuple[Path, Path]:
    """Create a bare origin + working repo with a baseline commit, then detach HEAD.

    Returns (repo_path, bare_path). Mirrors the same helper in test_git_utils_integ.
    """
    bare = base / "origin.git"
    bare.mkdir()
    _run(["git", "init", "--bare"], bare)

    repo = base / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "retry-test@example"], repo)
    _run(["git", "config", "user.name", "RetryTest"], repo)
    _run(["git", "remote", "add", "origin", str(bare)], repo)

    # Baseline commit + push so bare has a main ref.
    (repo / "BASELINE.md").write_text("# baseline\n", encoding="utf-8")
    _run(["git", "add", "BASELINE.md"], repo)
    _run(["git", "commit", "-m", "baseline"], repo)
    _run(["git", "push", "origin", "main"], repo)

    # Detach HEAD — mirrors `git worktree add --detach ... origin/main`.
    _run(["git", "checkout", "--detach"], repo)

    return repo, bare


def _bare_origin_head(bare: Path) -> str:
    """Return the SHA that refs/heads/main points to in a bare repo."""
    result = subprocess.run(
        ["git", "ls-remote", str(bare), "refs/heads/main"],
        capture_output=True, text=True, check=False,
    )
    line = result.stdout.strip()
    if not line:
        return ""
    return line.split("\t")[0]


def _make_commit_in_repo(repo: Path, filename: str, content: str, msg: str) -> str:
    """Write a file, git add, git commit. Returns new HEAD SHA."""
    p = repo / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _run(["git", "add", filename], repo)
    _run(["git", "commit", "-m", msg], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip()


def _advance_bare_via_clone(base: Path, bare: Path, filename: str, content: str) -> str:
    """Push a competing commit to bare via a fresh clone. Returns the new bare HEAD SHA.

    This simulates another actor (HO, Specialist, another cron) pushing to origin/main
    between when our worktree committed and when it pushes.
    """
    clone = base / "competing_clone"
    clone.mkdir(exist_ok=True)
    _run(["git", "clone", str(bare), str(clone)], base)
    _run(["git", "config", "user.email", "competitor@example"], clone)
    _run(["git", "config", "user.name", "Competitor"], clone)
    # Ensure the clone has origin/main tracked and checked out.
    # The bare repo's HEAD may point to a nonexistent ref (git init --bare
    # defaults to master, but we pushed main). Explicitly fetch + checkout main.
    _run(["git", "fetch", "origin", "main"], clone)
    _run(["git", "checkout", "-B", "main", "origin/main"], clone)
    _make_commit_in_repo(clone, filename, content, "competing commit")
    _run(["git", "push", "origin", "main"], clone)
    return _bare_origin_head(bare)


# ---------------------------------------------------------------------------
# 1. Happy path — success on first attempt
# ---------------------------------------------------------------------------


class TestPushWithRetrySuccessFirstTry(unittest.TestCase):
    """push_with_retry returns (True, ...) when the first push succeeds.
    No retry loop executed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_with_retry_success_first_try(self) -> None:
        # Make a commit in the detached repo.
        _make_commit_in_repo(self.repo, "notes/retry-test.md", "# note\n", "test commit")

        origin_before = _bare_origin_head(self.bare)

        ok, msg = push_with_retry(self.repo)

        self.assertTrue(ok, f"expected success on first try; got msg={msg!r}")
        origin_after = _bare_origin_head(self.bare)
        self.assertNotEqual(
            origin_after, origin_before,
            "bare origin main must have advanced after successful push",
        )


# ---------------------------------------------------------------------------
# 2. Non-ff → fetch + rebase + retry; bare origin has BOTH commits after landing
# ---------------------------------------------------------------------------


class TestPushWithRetryNonFfRetriesAndLands(unittest.TestCase):
    """Core red-before-green test: a competing push to origin/main after the worktree
    commits causes the first push attempt to be rejected (non-fast-forward). The retry
    logic fetches origin/main, rebases the worktree commit onto it, and retries the push.
    Bare origin main must end up with BOTH the competing commit AND the worktree commit.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_with_retry_non_ff_retries_and_lands(self) -> None:
        # 1. Make a worktree commit (no file overlap with the competing commit).
        _make_commit_in_repo(
            self.repo, "notes/worktree-note.md", "# worktree\n", "worktree commit"
        )
        worktree_content_marker = "worktree"

        origin_before = _bare_origin_head(self.bare)

        # 2. Advance bare origin via a competing commit (different file — no conflict).
        competing_sha = _advance_bare_via_clone(
            self.base, self.bare,
            "notes/competing-note.md", "# competing\n",
        )
        self.assertNotEqual(
            competing_sha, origin_before,
            "bare origin must have advanced via competing clone",
        )

        # 3. Now push_with_retry from the worktree. The first push attempt will be
        #    non-ff (worktree diverged from the now-advanced origin). The retry must
        #    fetch + rebase + push successfully.
        ok, msg = push_with_retry(self.repo)

        self.assertTrue(ok, f"expected retry to succeed; got msg={msg!r}")

        origin_final = _bare_origin_head(self.bare)
        self.assertNotEqual(
            origin_final, competing_sha,
            "bare origin must have advanced beyond the competing commit after push_with_retry",
        )

        # Verify the final bare repo contains BOTH commits (competing + worktree).
        # Clone the bare and check file presence. The bare was initialized with
        # `git init --bare` (HEAD defaults to master, which is nonexistent), so
        # `git clone` warns and leaves the working tree empty; explicitly checkout
        # main after cloning to populate the working tree.
        verify_clone = self.base / "verify_clone"
        verify_clone.mkdir(exist_ok=True)
        _run(["git", "clone", str(self.bare), str(verify_clone)], self.base)
        _run(["git", "checkout", "main"], verify_clone)
        self.assertTrue(
            (verify_clone / "notes" / "competing-note.md").exists(),
            "competing commit's file must be present (rebase stacked on top, not replaced)",
        )
        self.assertTrue(
            (verify_clone / "notes" / "worktree-note.md").exists(),
            "worktree commit's file must be present (both commits landed)",
        )
        content = (verify_clone / "notes" / "worktree-note.md").read_text()
        self.assertIn(
            worktree_content_marker, content,
            "worktree file content must be intact after rebase",
        )


# ---------------------------------------------------------------------------
# 3. Rebase conflict → abort + fail-loud + clean state (Option B)
# ---------------------------------------------------------------------------


class TestPushWithRetryNonFfConflictReturnsFalse(unittest.TestCase):
    """When the rebase conflicts (two commits edited the same file), push_with_retry
    must abort the rebase, leave the repo in a clean state (no partial rebase),
    and return (False, ...) — Option B.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_with_retry_non_ff_conflict_returns_false(self) -> None:
        # Make a worktree commit that edits BASELINE.md (same file as the competing commit).
        (self.repo / "BASELINE.md").write_text("# worktree version\n", encoding="utf-8")
        _run(["git", "add", "BASELINE.md"], self.repo)
        _run(["git", "commit", "-m", "worktree edits BASELINE"], self.repo)

        # Advance bare origin with a competing commit that ALSO edits BASELINE.md.
        _advance_bare_via_clone(
            self.base, self.bare,
            "BASELINE.md", "# competing version\n",
        )

        ok, msg = push_with_retry(self.repo)

        self.assertFalse(ok, f"expected (False, ...) on rebase conflict; got ok={ok!r}, msg={msg!r}")
        self.assertIn("conflict", msg.lower(),
                      f"message must mention 'conflict'; got {msg!r}")

        # Verify the repo is in a clean state — no in-progress rebase.
        rebase_merge = self.repo / ".git" / "rebase-merge"
        rebase_apply = self.repo / ".git" / "rebase-apply"
        self.assertFalse(
            rebase_merge.exists() or rebase_apply.exists(),
            "repo must not have a stale in-progress rebase after conflict (Option B: abort + clean state)",
        )

        # The repo's working tree must be clean (no uncommitted conflicts left).
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.repo,
            capture_output=True, text=True, check=False,
        )
        # Status should be empty (clean) or only show the detached HEAD situation.
        # The specific check: no merge-conflict markers (UU lines).
        conflict_lines = [l for l in status.stdout.splitlines() if l.startswith("UU")]
        self.assertEqual(
            conflict_lines, [],
            f"no conflict markers expected in working tree after abort; got {status.stdout!r}",
        )


# ---------------------------------------------------------------------------
# 4. All attempts exhausted — fail-loud after 3 tries
# ---------------------------------------------------------------------------


class TestPushWithRetryExhaustedReturnsFalse(unittest.TestCase):
    """When origin/main keeps advancing after every fetch (sustained contention),
    push_with_retry must fail after exhausting its attempt budget and return
    (False, ...) with an informative message.

    Uses mock to simulate a push that always fails non-ff even after rebase.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_with_retry_exhausted_returns_false(self) -> None:
        from unittest.mock import patch, MagicMock

        _make_commit_in_repo(self.repo, "notes/test.md", "# test\n", "worktree commit")

        push_attempt_count = {"count": 0}

        original_git = _git

        def fake_git(*args, cwd=None, **kwargs):
            if args and args[0] == "push":
                push_attempt_count["count"] += 1
                # Always return a non-ff rejection — simulates infinite contention.
                m = MagicMock()
                m.returncode = 1
                m.stdout = ""
                m.stderr = "error: failed to push some refs\n! [rejected] main -> main (non-fast-forward)"
                return m
            # All other git calls (fetch, rebase, etc.) succeed.
            return original_git(*args, cwd=cwd, **kwargs)

        with patch("tools._git_utils._git", side_effect=fake_git):
            ok, msg = push_with_retry(self.repo, attempts=3)

        self.assertFalse(ok, f"expected (False, ...) after exhausted attempts; got ok={ok!r}")
        self.assertEqual(
            push_attempt_count["count"], 3,
            f"expected exactly 3 push attempts; got {push_attempt_count['count']}",
        )
        # Message should indicate exhaustion.
        self.assertTrue(
            "attempt" in msg.lower() or "failed" in msg.lower() or "push" in msg.lower(),
            f"message must indicate push failure; got {msg!r}",
        )


# ---------------------------------------------------------------------------
# 5. Non-non-ff failure → no retry; immediate fail-loud
# ---------------------------------------------------------------------------


class TestPushWithRetryNonNonFfFailsImmediately(unittest.TestCase):
    """An auth/permission/network error must NOT be retried. push_with_retry must
    return (False, ...) immediately after 1 attempt (not 3).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_push_with_retry_non_non_ff_fails_immediately(self) -> None:
        from unittest.mock import patch, MagicMock

        _make_commit_in_repo(self.repo, "notes/test.md", "# test\n", "worktree commit")

        push_attempt_count = {"count": 0}
        original_git = _git

        def fake_git(*args, cwd=None, **kwargs):
            if args and args[0] == "push":
                push_attempt_count["count"] += 1
                m = MagicMock()
                m.returncode = 128
                m.stdout = ""
                m.stderr = "fatal: Authentication failed for 'https://github.com/example/repo.git'"
                return m
            return original_git(*args, cwd=cwd, **kwargs)

        with patch("tools._git_utils._git", side_effect=fake_git):
            ok, msg = push_with_retry(self.repo, attempts=3)

        self.assertFalse(ok, f"expected (False, ...) on auth failure; got ok={ok!r}")
        self.assertEqual(
            push_attempt_count["count"], 1,
            f"auth failure must not be retried; expected 1 push attempt, got {push_attempt_count['count']}",
        )


# ---------------------------------------------------------------------------
# 6. Integration: ticket_lint._git_commit_fixes retries on non-ff
# ---------------------------------------------------------------------------


class TestTicketLintPushRetriesOnNonFf(unittest.TestCase):
    """Call-site integration test for ticket_lint's commit-then-unified-push flow.

    T-0558 moved the push out of `_git_commit_fixes` into a single unified
    `push_with_retry(HEAD:main)` fired by the caller (`_do_lint`). This test
    mirrors that exact two-step (`_git_commit_fixes` commits → caller pushes via
    push_with_retry) and asserts a non-ff competing commit landing between the two
    is handled gracefully — the lint commit lands via rebase + retry.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ticket_lint_push_retries_on_non_ff(self) -> None:
        from tools import ticket_lint

        # Seed ticket dirs.
        tickets_inbox = self.repo / "tickets" / "inbox"
        tickets_inbox.mkdir(parents=True, exist_ok=True)
        (self.repo / "tickets" / "active").mkdir(parents=True, exist_ok=True)

        ticket_path = tickets_inbox / "T-8001.md"
        ticket_path.write_text(
            "---\nid: T-8001\ncreated: 2026-05-20T10:00:00Z\n"
            "requester: operator\ntarget: operator\nstate: inbox\n"
            "parent_ticket: null\ndepends_on: []\nintent: action\n---\n"
            "\n# Test ticket T-8001\n\n## Ask\n\nDo the thing.\n\n"
            "## Transition log\n\n- 2026-05-20T10:00:00Z [operator] filed in inbox\n",
            encoding="utf-8",
        )

        origin_before = _bare_origin_head(self.bare)

        # Advance bare origin via a competing commit (no file overlap with tickets/).
        _advance_bare_via_clone(
            self.base, self.bare,
            "notes/competing-lint.md", "# competing note\n",
        )

        # Step 1: _git_commit_fixes commits locally (T-0558: it does NOT push).
        ok, msg = ticket_lint._git_commit_fixes(
            [ticket_path],
            "2026-05-20",
            cwd=self.repo,
        )
        self.assertTrue(ok, f"_git_commit_fixes failed: {msg!r}")

        # Step 2: the caller (_do_lint) fires the unified push; exercise that exact
        # call — push_with_retry(HEAD:main), which rebases over the non-ff and retries.
        ok_push, push_msg = push_with_retry(self.repo, refspec="HEAD:main")
        self.assertTrue(ok_push, f"unified push_with_retry on non-ff failed: {push_msg!r}")

        origin_after = _bare_origin_head(self.bare)
        self.assertNotEqual(
            origin_after, origin_before,
            "bare origin main must have advanced after push via retry",
        )

        # Verify both commits are in the final bare repo.
        # Explicit checkout main: bare's HEAD defaults to nonexistent master
        # so git clone warns and leaves working tree empty; checkout main after.
        verify = self.base / "verify_lint"
        verify.mkdir(exist_ok=True)
        _run(["git", "clone", str(self.bare), str(verify)], self.base)
        _run(["git", "checkout", "main"], verify)
        self.assertTrue(
            (verify / "notes" / "competing-lint.md").exists(),
            "competing commit file must be in final repo (rebase stacked on top)",
        )
        self.assertTrue(
            (verify / "tickets" / "inbox" / "T-8001.md").exists(),
            "ticket_lint commit must be in final repo",
        )


# ---------------------------------------------------------------------------
# 7. conflict_resolver resolves a generated-file conflict and lands (T-0846)
# ---------------------------------------------------------------------------


class TestPushWithRetryResolverResolvesGeneratedConflict(unittest.TestCase):
    """A rebase conflict on a GENERATED file (e.g. the ticket board) is resolved
    by a caller-supplied conflict_resolver that regenerates it; push_with_retry
    then completes the rebase and lands both commits. T-0846."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)
        # Seed a "generated board" file in the baseline so both sides edit it.
        # (Re-attach to main briefly to push the seed, then detach again.)
        _run(["git", "checkout", "-B", "main"], self.repo)
        (self.repo / "board.txt").write_text("BOARD v0\n", encoding="utf-8")
        _run(["git", "add", "board.txt"], self.repo)
        _run(["git", "commit", "-m", "seed board"], self.repo)
        _run(["git", "push", "origin", "main"], self.repo)
        _run(["git", "checkout", "--detach"], self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolver_resolves_board_conflict_and_lands(self) -> None:
        # Worktree commit: a real change + a board edit (mimics a ticket op).
        (self.repo / "notes").mkdir(exist_ok=True)
        (self.repo / "notes" / "ours.md").write_text("# ours\n", encoding="utf-8")
        (self.repo / "board.txt").write_text("BOARD ours\n", encoding="utf-8")
        _run(["git", "add", "notes/ours.md", "board.txt"], self.repo)
        _run(["git", "commit", "-m", "worktree op + board"], self.repo)

        # Competing push ALSO edits board.txt → guarantees a board rebase conflict.
        _advance_bare_via_clone(self.base, self.bare, "board.txt", "BOARD theirs\n")

        def board_resolver(unmerged, cwd):
            # Only resolve when the SOLE conflict is the generated board.
            if set(unmerged) != {"board.txt"}:
                return False
            subprocess.run(["git", "checkout", "--theirs", "--", "board.txt"],
                           cwd=str(cwd), capture_output=True, text=True, check=False)
            (Path(cwd) / "board.txt").write_text("BOARD regenerated\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "board.txt"],
                           cwd=str(cwd), capture_output=True, text=True, check=False)
            return True

        ok, msg = push_with_retry(self.repo, conflict_resolver=board_resolver)
        self.assertTrue(ok, f"resolver should let the push land; got msg={msg!r}")

        # No stale in-progress rebase.
        self.assertFalse((self.repo / ".git" / "rebase-merge").exists()
                         or (self.repo / ".git" / "rebase-apply").exists(),
                         "no in-progress rebase should remain")

        verify = self.base / "verify_resolver"
        verify.mkdir(exist_ok=True)
        _run(["git", "clone", str(self.bare), str(verify)], self.base)
        _run(["git", "checkout", "main"], verify)
        self.assertTrue((verify / "notes" / "ours.md").exists(),
                        "worktree commit must land")
        self.assertEqual((verify / "board.txt").read_text(), "BOARD regenerated\n",
                         "board must be the resolver-regenerated content")


# ---------------------------------------------------------------------------
# 8. conflict_resolver DECLINES a real conflict → still aborts + fails (T-0846)
# ---------------------------------------------------------------------------


class TestPushWithRetryResolverDeclinesRealConflict(unittest.TestCase):
    """A resolver that only handles the generated board must NOT mask a real
    conflict on another file: it returns False, push_with_retry aborts the
    rebase and fails loudly with clean state. T-0846 (safety property)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolver_declines_non_board_conflict(self) -> None:
        # Both sides edit BASELINE.md → a REAL conflict the board resolver won't touch.
        (self.repo / "BASELINE.md").write_text("# ours\n", encoding="utf-8")
        _run(["git", "add", "BASELINE.md"], self.repo)
        _run(["git", "commit", "-m", "worktree edits BASELINE"], self.repo)
        _advance_bare_via_clone(self.base, self.bare, "BASELINE.md", "# theirs\n")

        calls = {"n": 0}

        def board_resolver(unmerged, cwd):
            calls["n"] += 1
            return set(unmerged) == {"board.txt"}  # declines BASELINE.md

        ok, msg = push_with_retry(self.repo, conflict_resolver=board_resolver)
        self.assertFalse(ok, "a real conflict must still fail even with a resolver")
        self.assertIn("conflict", msg.lower())
        self.assertGreaterEqual(calls["n"], 1, "resolver should have been consulted")
        self.assertFalse((self.repo / ".git" / "rebase-merge").exists()
                         or (self.repo / ".git" / "rebase-apply").exists(),
                         "rebase must be aborted to clean state")


# ---------------------------------------------------------------------------
# 9. A rebase stranded by the pre-push freshness hook (T-4380)
# ---------------------------------------------------------------------------
#
# On an ATTACHED branch (the shared checkout), `.githooks/pre-push` runs `git pull
# --rebase --autostash` when the clone is behind. On a conflict the hook exits 1
# WITHOUT aborting, leaving `.git/rebase-merge`. The failure carries none of the
# non-ff markers, so push_with_retry used to call it "non-retryable" and return,
# stranding the checkout mid-rebase and never consulting the conflict_resolver.

_HOOKS_DIR = Path(__file__).resolve().parent.parent / ".githooks"


def _init_attached_repo_with_hook(base: Path) -> tuple[Path, Path]:
    """Bare origin + a clone ATTACHED to main with the real pre-push hook wired,
    seeded with shared.txt. Returns (repo, bare)."""
    bare = base / "origin.git"
    bare.mkdir()
    _run(["git", "init", "--bare", "-b", "main"], bare)
    repo = base / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "hook-test@example"], repo)
    _run(["git", "config", "user.name", "HookTest"], repo)
    _run(["git", "remote", "add", "origin", str(bare)], repo)
    (repo / "shared.txt").write_text("v0\n", encoding="utf-8")
    _run(["git", "add", "shared.txt"], repo)
    _run(["git", "commit", "-m", "seed"], repo)
    _run(["git", "push", "origin", "main"], repo)
    _run(["git", "config", "core.hooksPath", str(_HOOKS_DIR)], repo)
    return repo, bare


def _rebase_dirs_present(repo: Path) -> bool:
    return (repo / ".git" / "rebase-merge").exists() or (repo / ".git" / "rebase-apply").exists()


def _current_branch(repo: Path) -> str:
    r = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=str(repo),
                       capture_output=True, text=True)
    return r.stdout.strip()


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()


class TestPushWithRetryHookStrandedRebase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_attached_repo_with_hook(self.base)
        # A local commit and a competing origin commit that conflict on shared.txt.
        _make_commit_in_repo(self.repo, "shared.txt", "ours\n", "local edits shared")
        self.local_sha = _head(self.repo)
        _advance_bare_via_clone(self.base, self.bare, "shared.txt", "theirs\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolver_drives_the_hooks_stranded_rebase_to_completion(self) -> None:
        calls = {"n": 0}

        def resolver(unmerged, cwd):
            calls["n"] += 1
            if set(unmerged) != {"shared.txt"}:
                return False
            (Path(cwd) / "shared.txt").write_text("resolved\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "shared.txt"], cwd=str(cwd),
                           capture_output=True, text=True, check=False)
            return True

        ok, msg = push_with_retry(self.repo, conflict_resolver=resolver)
        self.assertTrue(ok, f"resolver must let the hook-stranded rebase land; got {msg!r}")
        self.assertEqual(calls["n"], 1)
        self.assertFalse(_rebase_dirs_present(self.repo))
        self.assertEqual(_current_branch(self.repo), "main")
        verify = self.base / "verify"
        _run(["git", "clone", str(self.bare), str(verify)], self.base)
        self.assertEqual((verify / "shared.txt").read_text(), "resolved\n")

    def _assert_aborted_loudly(self, ok: bool, msg: str) -> None:
        self.assertFalse(ok)
        self.assertFalse(_rebase_dirs_present(self.repo), "must never be left mid-rebase")
        self.assertEqual(_current_branch(self.repo), "main", "branch must be restored")
        self.assertEqual(_head(self.repo), self.local_sha, "pre-rebase HEAD must be restored")
        # The exact `git log --oneline` line, 4-space indented — not merely the sha or
        # subject, which git's own "could not apply <sha>... <subject>" stderr
        # tail would also satisfy.
        short = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(self.repo),
                               capture_output=True, text=True).stdout.strip()
        self.assertIn(f"    {short} local edits shared", msg, "the unpushed commit must be listed")
        self.assertIn("DID NOT LAND", msg)
        self.assertIn("reset --hard", msg, "the manual recovery must be spelled out")
        self.assertNotEqual(_bare_origin_head(self.bare), self.local_sha)
        # git's own `hint: ... run "git rebase --continue"` (the hook re-prints it with
        # a `pre-push: ` prefix) contradicts an ABORTED message; the useful lines stay.
        self.assertNotIn("hint:", msg)
        self.assertIn("Merge conflict in shared.txt", msg)

    def test_resolver_declining_aborts_and_names_the_commit(self) -> None:
        ok, msg = push_with_retry(self.repo, conflict_resolver=lambda unmerged, cwd: False)
        self._assert_aborted_loudly(ok, msg)

    def test_no_resolver_aborts_and_names_the_commit(self) -> None:
        ok, msg = push_with_retry(self.repo)
        self._assert_aborted_loudly(ok, msg)

    def test_last_attempt_does_not_resolve_because_no_push_would_follow(self) -> None:
        calls = {"n": 0}

        def resolver(unmerged, cwd):
            calls["n"] += 1
            return True

        ok, msg = push_with_retry(self.repo, attempts=1, conflict_resolver=resolver)
        self._assert_aborted_loudly(ok, msg)
        self.assertEqual(calls["n"], 0, "a resolved rebase with no push left would be a silent unpushed commit")

    def test_failed_abort_is_reported_as_still_mid_rebase(self) -> None:
        """If `git rebase --abort` itself fails, the checkout really IS stranded; the
        message must say so and give the manual command, never claim it was restored."""
        import tools._git_utils as gu

        real_git = gu._git

        def flaky_git(*args, cwd=gu.REPO_ROOT):
            if args[:2] == ("rebase", "--abort"):
                return subprocess.CompletedProcess(list(args), 1, "", "simulated abort failure")
            return real_git(*args, cwd=cwd)

        with unittest.mock.patch.object(gu, "_git", flaky_git):
            ok, msg = push_with_retry(self.repo)
        self.assertFalse(ok)
        self.assertTrue(_rebase_dirs_present(self.repo), "fixture: the abort was suppressed")
        self.assertIn("STILL MID-REBASE", msg)
        self.assertIn("DID NOT LAND", msg)
        self.assertIn("rebase --abort", msg, "the manual command must be spelled out")
        self.assertNotIn("branch 'main' restored", msg)
        _run(["git", "rebase", "--abort"], self.repo)  # test cleanup


def _rebase_state_present_at(cwd: Path) -> bool:
    """Independent of the code under test: ask git where THIS checkout keeps its
    rebase state (per-worktree in a linked worktree) and look for the directories."""
    for kind in ("rebase-merge", "rebase-apply"):
        r = subprocess.run(["git", "rev-parse", "--git-path", kind], cwd=str(cwd),
                           capture_output=True, text=True)
        p = Path(r.stdout.strip())
        if r.returncode == 0 and (p if p.is_absolute() else cwd / p).exists():
            return True
    return False


class TestPushWithRetryHookStrandedRebaseInLinkedWorktree(unittest.TestCase):
    """The harness worktrees and submodules carry a `.git` FILE, not a directory. The
    fix must cover that shape: `_is_checkout_root` must accept it, and the rebase
    state is then read from the worktree's private git dir (T-4380)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        main, self.bare = _init_attached_repo_with_hook(self.base)
        _run(["git", "branch", "feature"], main)
        _run(["git", "push", "origin", "feature"], main)
        self.wt = self.base / "linked-wt"
        _run(["git", "worktree", "add", str(self.wt), "feature"], main)
        self.assertTrue((self.wt / ".git").is_file(), "fixture: a linked worktree's .git is a file")
        _make_commit_in_repo(self.wt, "shared.txt", "ours\n", "local edits shared")
        self.local_sha = _head(self.wt)
        rival = self.base / "rival"
        _run(["git", "clone", str(self.bare), str(rival)], self.base)
        _run(["git", "config", "user.email", "rival@example"], rival)
        _run(["git", "config", "user.name", "Rival"], rival)
        _run(["git", "checkout", "-B", "feature", "origin/feature"], rival)
        _make_commit_in_repo(rival, "shared.txt", "theirs\n", "rival edits shared")
        _run(["git", "push", "origin", "feature"], rival)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolver_lands_the_hooks_stranded_rebase_in_a_linked_worktree(self) -> None:
        def resolver(unmerged, cwd):
            if set(unmerged) != {"shared.txt"}:
                return False
            (Path(cwd) / "shared.txt").write_text("resolved\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "shared.txt"], cwd=str(cwd),
                           capture_output=True, text=True, check=False)
            return True

        ok, msg = push_with_retry(self.wt, refspec="HEAD:feature", conflict_resolver=resolver)
        self.assertTrue(ok, f"the fix must reach a `.git`-file checkout; got {msg!r}")
        self.assertFalse(_rebase_state_present_at(self.wt))
        self.assertEqual(_current_branch(self.wt), "feature")
        verify = self.base / "verify"
        _run(["git", "clone", "--branch", "feature", str(self.bare), str(verify)], self.base)
        self.assertEqual((verify / "shared.txt").read_text(), "resolved\n")

    def test_no_resolver_aborts_loudly_and_restores_the_branch_in_a_linked_worktree(self) -> None:
        ok, msg = push_with_retry(self.wt, refspec="HEAD:feature")
        self.assertFalse(ok)
        self.assertFalse(_rebase_state_present_at(self.wt), "must never be left mid-rebase")
        self.assertEqual(_current_branch(self.wt), "feature", "branch must be restored")
        self.assertEqual(_head(self.wt), self.local_sha)
        self.assertIn("DID NOT LAND", msg)
        self.assertIn("local edits shared", msg)


class TestPushWithRetryNoCheckoutNoProbe(unittest.TestCase):
    """A cwd that is not a checkout root (no `.git` entry of its own) has no
    established rebase state, so push_with_retry must neither probe for one (no extra
    git spawns — the consumer tests that pin exact git-call sequences run against
    exactly such cwds) nor claim ownership of one after a failed push (T-4380: act
    only on a rebase PROVEN to have appeared during this call)."""

    def test_no_rebase_probe_and_no_rebase_abort_outside_a_checkout(self) -> None:
        import tools._git_utils as gu

        real_git = gu._git
        seen: list[tuple] = []

        def recording_git(*args, cwd=gu.REPO_ROOT):
            seen.append(args)
            return real_git(*args, cwd=cwd)

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(gu, "_git", recording_git):
                ok, msg = push_with_retry(Path(tmp))
        self.assertFalse(ok)
        self.assertIn("non-retryable", msg)
        # T-3600's read-only branch-safety probe (symbolic-ref/log) is not ours.
        probes = [a for a in seen if a[0] not in ("symbolic-ref", "log")]
        self.assertEqual([a[0] for a in probes], ["push"], f"unexpected git calls: {probes!r}")


class TestPushWithRetryLeavesForeignRebaseAlone(unittest.TestCase):
    """A rebase that was ALREADY in progress at entry is somebody else's work
    (a human mid-resolution): push_with_retry must not abort it (T-4380)."""

    def test_rebase_present_at_entry_is_not_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, bare = _init_attached_repo_with_hook(base)
            _make_commit_in_repo(repo, "shared.txt", "ours\n", "local edits shared")
            _advance_bare_via_clone(base, bare, "shared.txt", "theirs\n")
            _run(["git", "fetch", "origin", "main"], repo)
            r = subprocess.run(["git", "rebase", "origin/main"], cwd=str(repo),
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertTrue(_rebase_dirs_present(repo), "fixture: a human's rebase is in progress")
            # T-4422: with a rebase at entry the push is not attempted at all, so even a
            # remote that would fail for an unrelated reason is never contacted.
            _run(["git", "remote", "set-url", "origin", str(base / "does-not-exist.git")], repo)
            ok, msg = push_with_retry(repo)
            self.assertFalse(ok)
            self.assertNotIn("non-retryable", msg)
            self.assertIn("push refused", msg)
            self.assertIn("no push, fetch, rebase, continue or abort was run", msg)
            self.assertTrue(_rebase_dirs_present(repo), "a foreign in-progress rebase must be left untouched")

    def test_non_ff_rejection_with_a_rebase_at_entry_runs_no_rebase_at_all(self) -> None:
        """The pre-existing non-ff path (fetch, `git rebase FETCH_HEAD`, resolver /
        abort) used to abort a person's in-progress rebase. With a rebase present at
        entry, NO rebase / continue / abort may run on any path: the call returns a
        loud (False, msg) naming the rebase and leaves it exactly as it found it."""
        import tools._git_utils as gu

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, bare = _init_attached_repo_with_hook(base)
            _run(["git", "config", "--unset", "core.hooksPath"], repo)  # plain non-ff, no hook
            _make_commit_in_repo(repo, "shared.txt", "ours\n", "local edits shared")
            _advance_bare_via_clone(base, bare, "shared.txt", "theirs\n")
            _run(["git", "fetch", "origin", "main"], repo)
            r = subprocess.run(["git", "rebase", "origin/main"], cwd=str(repo),
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertTrue(_rebase_dirs_present(repo), "fixture: a human's rebase is in progress")
            # Origin moves on past what that rebase is based on, so the push is a real
            # non-ff rejection (HEAD is the rebase's `onto`, now behind origin).
            rival = base / "rival"
            _run(["git", "clone", str(bare), str(rival)], base)
            _run(["git", "config", "user.email", "rival@example"], rival)
            _run(["git", "config", "user.name", "Rival"], rival)
            _make_commit_in_repo(rival, "other.txt", "unrelated\n", "rival adds other")
            _run(["git", "push", "origin", "main"], rival)
            head_before = _head(repo)
            unmerged_before = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                                             capture_output=True, text=True).stdout

            real_git = gu._git
            seen: list[tuple] = []
            resolver_calls = {"n": 0}

            def recording_git(*args, cwd=gu.REPO_ROOT):
                seen.append(args)
                return real_git(*args, cwd=cwd)

            def resolver(unmerged, cwd):
                resolver_calls["n"] += 1
                return True

            with unittest.mock.patch.object(gu, "_git", recording_git):
                ok, msg = push_with_retry(repo, conflict_resolver=resolver)

            self.assertFalse(ok)
            self.assertIn("ALREADY IN PROGRESS", msg)
            self.assertIn("rebase-merge", msg, "the message must name the rebase")
            self.assertIn("DID NOT LAND", msg)
            self.assertEqual([a for a in seen if a[0] == "rebase"], [], "no rebase/continue/abort may run")
            # T-4422 (and T-4380 r2 mutant N12): nothing touches the remote either.
            self.assertEqual([a for a in seen if a[0] == "fetch"], [], "no fetch before/after the foreign guard")
            self.assertEqual([a for a in seen if a[0] == "push"], [], "the half-rebased HEAD must not be pushed")
            self.assertEqual(_head(bare), _head(rival), "origin must be exactly as the rival left it")
            self.assertEqual(resolver_calls["n"], 0, "the resolver must not be handed a person's rebase")
            self.assertTrue(_rebase_dirs_present(repo), "the person's rebase must survive untouched")
            self.assertEqual(_head(repo), head_before)
            self.assertEqual(
                subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                               capture_output=True, text=True).stdout,
                unmerged_before, "the working tree / index must be exactly as found",
            )



class TestStderrTailHintFilter(unittest.TestCase):
    """T-4380 r2 surviving mutants N3/N7/N8 (pinned in T-4422): git's `hint:` advice is
    dropped per LINE by prefix (bare or `pre-push:`-prefixed), on a base with no
    trailing newline, from both the foreign-rebase and the STILL-MID-REBASE messages."""

    DETAIL = (
        "error: failed to push some refs\n"
        "hint: Updates were rejected because the tip is behind\n"
        "pre-push: hint: run git rebase --continue\n"
        "error: see the note, hint: inline text stays"  # no trailing newline
    )

    def _check(self, msg: str) -> None:
        self.assertIn("error: failed to push some refs", msg)
        self.assertIn("error: see the note, hint: inline text stays", msg, "only a hint: PREFIX drops a line")
        self.assertNotIn("Updates were rejected", msg)
        self.assertNotIn("run git rebase --continue\n", msg + "\n")

    def test_tail_filters_by_line_prefix_without_trailing_newline(self) -> None:
        from tools._git_utils import _stderr_tail
        out = _stderr_tail(self.DETAIL)
        self.assertEqual(out.splitlines(), [
            "error: failed to push some refs",
            "error: see the note, hint: inline text stays",
        ])

    def test_foreign_rebase_message_drops_hints(self) -> None:
        from tools._git_utils import _foreign_rebase_message
        self._check(_foreign_rebase_message(Path("/x"), "rebase-merge", self.DETAIL))

    def test_still_mid_rebase_message_drops_hints(self) -> None:
        from tools._git_utils import _stranded_rebase_abort_message
        msg = _stranded_rebase_abort_message(Path("/x"), "HEAD:main", "main", self.DETAIL, aborted=False)
        self.assertIn("STILL MID-REBASE", msg)
        self._check(msg)


# T-4940: isolate the seat's state dir for plain-unittest runs (pytest's conftest
# _isolate_state_dir already does this per test). See tools/_seat_state_isolation.py.
try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover - run from tools/
    import _seat_state_isolation  # type: ignore[no-redef]


def setUpModule() -> None:
    _seat_state_isolation.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()


if __name__ == "__main__":
    unittest.main()
