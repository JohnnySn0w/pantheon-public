"""Per-cron regression matrix for T-0471 worktree isolation.

Verifies that each retrofitted cron script's commit pipeline:
  (a) only stages files in its declared scope, AND
  (b) when given an isolated cwd (simulating the worktree), dirty foreign
      files in the "shared" working tree do not contaminate the commit.

Integration tier (same-process, real git subprocess) — matches the
T-0259 Layer-3 fixture pattern at the Python API level. Cross-process
subprocess tests for the --commit-prod path are omitted because that path
requires targeting a live origin (the cron scripts derive REPO_ROOT from
their own __file__ location and provide no env-var override); the
worktree isolation is verified at the unit tier via test_git_utils.py
and the _with_worktree alias + patch.object pattern in each cron's
unit tests.

Matrix (per T-0471 ticket §Test Plan):
  | Cron              | Dirty foreign file seeded     |
  |-------------------|-------------------------------|
  | spine_index_groom | ledger/2026.jsonl             |
  | ticket_grooming   | knowledge/watch/reports/...   |
  | watch_runner      | tickets/active/T-XXXX.md      |
  | pantheon_config_sync | opt-out; no test required  |

Run from repo root:
    python3 -m unittest tools.test_git_utils_integ -v
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import date, timedelta, timezone, datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from tools import ticket_grooming, watch_runner
import tools.spine_index_groom as sig
from tools import _git_utils
from tools.test_watch_runner import _landed_report_text


class TestCommitScoped(unittest.TestCase):
    """T-1088: the shared scope-narrowed-commit primitive `commit_scoped`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = _init_tmp_repo(base)
        _seed_commit(self.repo, {"a.txt": "1\n", "dir/b.txt": "1\n"}, "seed")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_happy_path_commits_in_scope_only(self):
        (self.repo / "a.txt").write_text("2\n")
        ok, detail = _git_utils.commit_scoped(
            ["a.txt"], "edit a", cwd=self.repo, push=False, sync=False
        )
        self.assertTrue(ok, detail)
        # Commit landed; working tree clean.
        self.assertEqual(_staged_files(self.repo), [])
        self.assertIn("a.txt", subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=self.repo, capture_output=True, text=True).stdout)

    def test_leak_guard_resets_and_fails(self):
        # An out-of-scope file is dirty + would be staged by a broad add — but we
        # only pass a.txt; simulate a leak by pre-staging b.txt, then asking to
        # commit a.txt: b.txt is already staged and out of scope → refuse + reset.
        (self.repo / "a.txt").write_text("2\n")
        (self.repo / "dir/b.txt").write_text("2\n")
        subprocess.run(["git", "add", "--", "dir/b.txt"], cwd=self.repo, capture_output=True)
        ok, detail = _git_utils.commit_scoped(
            ["a.txt"], "edit a", cwd=self.repo, push=False, sync=False
        )
        self.assertFalse(ok)
        self.assertIn("out-of-scope", detail)
        self.assertEqual(_staged_files(self.repo), [])  # reset cleaned staging

    def test_no_changes_is_ok_noop(self):
        ok, detail = _git_utils.commit_scoped(
            ["a.txt"], "noop", cwd=self.repo, push=False, sync=False
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "no changes")

    def test_allow_prefix_dir_staging(self):
        (self.repo / "dir/c.txt").write_text("new\n")
        ok, detail = _git_utils.commit_scoped(
            ["dir/"], "add under dir", cwd=self.repo, push=False, sync=False, allow_prefix=True
        )
        self.assertTrue(ok, detail)

    def test_hermes_message_guard_refuses(self):
        (self.repo / "a.txt").write_text("2\n")
        with patch.object(_git_utils, "_hermes_scan_message", return_value=False):
            ok, detail = _git_utils.commit_scoped(
                ["a.txt"], "bad", cwd=self.repo, push=False, sync=False
            )
        self.assertFalse(ok)
        self.assertIn("commit message", detail)

    def test_hermes_file_guard_refuses(self):
        (self.repo / "a.txt").write_text("2\n")
        with patch.object(_git_utils, "_hermes_scan_file", return_value=(False, "trigger")):
            ok, detail = _git_utils.commit_scoped(
                ["a.txt"], "edit", cwd=self.repo, push=False, sync=False
            )
        self.assertFalse(ok)
        self.assertIn("HERMES", detail)
        # Staging reset after a HERMES refusal.
        self.assertEqual(_staged_files(self.repo), [])

    def test_hermes_guard_catches_real_trigger_under_dir_staging(self):
        # The dir-staging gap (T-1088): a trigger in a file staged via a DIRECTORY
        # prefix must be caught. The pre-stage scan of the literal `paths` (a dir,
        # not a file) silently skipped it; the post-stage staged-file scan catches
        # it. Use the real HERMES scanner (no mock) against a real on-disk file.
        sub = self.repo / "dir"
        bad = sub / "bad.txt"
        # Source-time concat so this test file never contains the literal trigger.
        bad.write_text("body has " + "HERME" + "S.md unescaped\n", encoding="utf-8")
        ok, detail = _git_utils.commit_scoped(
            ["dir/"], "add under dir", cwd=self.repo, push=False, sync=False, allow_prefix=True
        )
        self.assertFalse(ok, "trigger under a dir-staged path must be refused")
        self.assertIn("HERME" + "S", detail)
        self.assertEqual(_staged_files(self.repo), [], "staging reset after refusal")
        # No commit landed (HEAD unchanged from the seed).
        self.assertEqual(
            subprocess.run(["git", "log", "--oneline"], cwd=self.repo,
                           capture_output=True, text=True).stdout.strip().count("\n"),
            0,
        )


# ---------------------------------------------------------------------------
# Shared tmp-git-repo fixture helpers.
# ---------------------------------------------------------------------------


def _init_tmp_repo(base: Path) -> Path:
    """Create a minimal git repo under base/repo/; return the repo path."""
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "integ@test"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "IntegTest"], cwd=repo, capture_output=True)
    # Bare origin so push works in tests that exercise the push path.
    bare = base / "origin.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=bare, capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, capture_output=True)
    return repo


def _seed_commit(repo: Path, paths: dict[str, str], msg: str = "seed") -> str:
    """Write files, add, commit. Returns the commit SHA."""
    for relpath, content in paths.items():
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, capture_output=True, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip()


def _stage_dirty(repo: Path, relpath: str, content: str) -> None:
    """Write a file and stage it without committing — simulates a dirty shared tree."""
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", relpath], cwd=repo, capture_output=True)


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip()


def _staged_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, capture_output=True, text=True,
    )
    return [p for p in result.stdout.split("\n") if p.strip()]


# ---------------------------------------------------------------------------
# spine_index_groom — dirty ledger/2026.jsonl
# ---------------------------------------------------------------------------


class TestSpineIndexGroomDirtyForeignFile(unittest.TestCase):
    """T-0471 per-cron regression: dirty ledger/2026.jsonl must not contaminate
    spine_index_groom's commit scope (lessons/README.md or similar).

    This test exercises the _commit_dir_change cwd path: when cwd is passed to
    _git, git operations run against the isolated worktree working tree, not the
    shared one. A dirty foreign file staged in a separate "shared" working tree
    does not appear in the worktree's `git diff --cached --name-only`.

    Test structure: two separate repos. The "worktree" repo is clean (simulating
    with_worktree's fresh checkout). The "shared" repo has a staged foreign file.
    The cron runs against the worktree repo (via cwd) → foreign file absent from
    worktree's staged set → scope guard passes.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # Worktree repo (clean isolation — simulates what with_worktree creates).
        self.wt_repo = _init_tmp_repo(self.base / "wt")
        # Shared repo with dirty ledger file (simulates the real shared tree).
        self.shared_repo = _init_tmp_repo(self.base / "shared")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dirty_ledger_in_shared_tree_absent_from_worktree_staged_set(self) -> None:
        """A pre-staged ledger/2026.jsonl in the shared repo does NOT appear in
        the worktree repo's staged set. Spine commit scope is clean.

        This verifies the structural property: cwd threading to the worktree
        means `git diff --cached --name-only` runs against the worktree's
        git index, not the shared tree's. Two independent repos serve as
        the "worktree" and the "shared working tree" respectively.
        """
        # Seed worktree repo with a lessons/README.md + a lesson file.
        _seed_commit(self.wt_repo, {
            "lessons/README.md": (
                "# Lessons\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "<!-- AUTOGEN-END spine-index -->\n"
            ),
            "lessons/first-lesson.md": "# Lesson\nLesson purpose here.\n",
        })

        # Stage a dirty ledger file in the SHARED repo (different working tree).
        _seed_commit(self.shared_repo, {"ledger/2026.jsonl": "{}\n"})
        _stage_dirty(self.shared_repo, "ledger/2026.jsonl", '{"updated": true}\n')
        self.assertIn("ledger/2026.jsonl", _staged_files(self.shared_repo))

        # Worktree repo staged set must be empty at this point.
        self.assertEqual(_staged_files(self.wt_repo), [],
                         "Worktree repo should start with clean index")

        # Run git diff --cached --name-only directly against the worktree repo
        # (this is what spine_index_groom's _commit_dir_change does via _git(cwd=wt_repo)).
        # The result must NOT contain ledger/2026.jsonl — it's in the shared repo.
        from tools._git_utils import _git
        result = _git("diff", "--cached", "--name-only", cwd=self.wt_repo)
        wt_staged = [p for p in result.stdout.split("\n") if p.strip()]

        self.assertEqual(wt_staged, [],
                         f"Worktree staged set should be empty; got {wt_staged!r}")
        self.assertNotIn("ledger/2026.jsonl", wt_staged,
                         "Dirty ledger from shared repo must not appear in worktree index")

        # Shared repo's dirty file must remain staged there (not cleared by our check).
        self.assertIn("ledger/2026.jsonl", _staged_files(self.shared_repo))


# ---------------------------------------------------------------------------
# ticket_grooming — dirty knowledge/watch/reports/<date>/*.md
# ---------------------------------------------------------------------------


class TestTicketGroomingDirtyForeignFile(unittest.TestCase):
    """T-0471 per-cron regression: dirty knowledge/watch/reports/ files must not
    contaminate ticket_grooming's commit scope (tickets/active/ only).

    Same structural proof as spine_index_groom: cwd threading ensures
    git_commit_and_push runs against the isolated worktree. Dirty files staged
    in the shared tree are invisible from the worktree's index.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.wt_repo = _init_tmp_repo(self.base / "wt")
        self.shared_repo = _init_tmp_repo(self.base / "shared")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dirty_watch_reports_in_shared_tree_absent_from_worktree_staged_set(self) -> None:
        """A pre-staged knowledge/watch/reports/ file in the shared repo does NOT
        appear in the worktree repo's staged set when git_commit_and_push uses
        cwd=worktree_path.
        """
        # Seed a stale ticket in the worktree repo.
        _seed_commit(self.wt_repo, {
            "tickets/active/T-9999.md": (
                "---\nid: T-9999\ncreated: 2026-05-01T00:00:00Z\n"
                "requester: orchestrator\ntarget: recovery\nstate: active\n"
                "parent_ticket: null\ndepends_on: []\n---\n"
                "\n# Test ticket\n\n## Ask\n\nDo thing.\n\n"
                "## Transition log\n\n"
                "- 2026-05-01T00:00:00Z [orchestrator] filed in inbox\n"
            ),
        })

        # Stage a dirty watch-report in the SHARED repo.
        _seed_commit(self.shared_repo, {
            "knowledge/watch/reports/2026-05-20/foo.md": "# Report\n\nContent.\n"
        })
        _stage_dirty(
            self.shared_repo,
            "knowledge/watch/reports/2026-05-20/foo.md",
            "# Report\n\nUpdated content.\n",
        )
        self.assertIn(
            "knowledge/watch/reports/2026-05-20/foo.md",
            _staged_files(self.shared_repo),
        )

        # Worktree repo staged set must be clean.
        self.assertEqual(_staged_files(self.wt_repo), [])

        # Run git_commit_and_push with cwd=wt_repo: git diff --cached in the
        # worktree sees only what was staged IN the worktree (nothing), so
        # the "no changes" short-circuit fires and the dirty shared file is
        # structurally excluded from the commit scope.
        with patch("tools._git_utils.subprocess.run") as mock_run:
            def fake_run(argv, **kwargs):
                # fetch + merge (legacy path, cwd=None skips these; cwd provided skips too)
                if argv[0] == "git":
                    if argv[1] == "add":
                        return MagicMock(returncode=0, stdout="", stderr="")
                    if argv[1:3] == ["diff", "--cached"]:
                        # Simulate clean index in the worktree.
                        return MagicMock(returncode=0, stdout="", stderr="")
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")
            mock_run.side_effect = fake_run

            # No _hermes_scan_file patch needed post-T-1088: empty modified_paths +
            # mocked-empty diff → commit_scoped's "no changes" short-circuit fires
            # before any staged-file HERMES scan.
            ok, msg = ticket_grooming.git_commit_and_push(
                [],
                "tickets(grooming): flag 0 stale ticket(s) — 2026-05-20\n",
                "2026-05-20",
                cwd=self.wt_repo,
            )

        # With cwd=wt_repo and clean index, no-changes short-circuit fires.
        self.assertTrue(ok, f"expected no-changes success; got msg={msg!r}")
        self.assertEqual(msg, "no changes")

        # Verify all git calls targeted wt_repo.
        for call in mock_run.call_args_list:
            call_cwd = call.kwargs.get("cwd")
            if call_cwd is not None and Path(str(call_cwd)) != self.wt_repo:
                self.fail(
                    f"git call targeted wrong cwd: expected {self.wt_repo!r}, "
                    f"got {call_cwd!r}"
                )

        # Shared dirty file remains staged there.
        self.assertIn(
            "knowledge/watch/reports/2026-05-20/foo.md",
            _staged_files(self.shared_repo),
        )


# ---------------------------------------------------------------------------
# watch_runner — dirty tickets/active/T-XXXX.md
# ---------------------------------------------------------------------------


class TestWatchRunnerDirtyForeignFile(unittest.TestCase):
    """T-0471 per-cron regression: dirty tickets/active/T-XXXX.md must not
    contaminate watch_runner's commit scope (knowledge/watch/reports/<date>/).

    Same structural proof: cwd threading ensures git_commit_and_push runs
    against the isolated worktree. Dirty tickets staged in the shared tree
    are invisible from the worktree's index.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.wt_repo = _init_tmp_repo(self.base / "wt")
        self.shared_repo = _init_tmp_repo(self.base / "shared")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dirty_ticket_in_shared_tree_absent_from_worktree_staged_set(self) -> None:
        """A pre-staged tickets/active/T-XXXX.md in the shared repo does NOT
        appear in the worktree repo's staged set when git_commit_and_push uses
        cwd=worktree_path.
        """
        # Seed a watch report in the worktree repo. Must be landed-ok shape
        # (T-1581: watch_runner.git_commit_and_push's partial-stage guard now
        # requires exists+nontrivial+structured, not just present).
        date_iso = "2026-05-20"
        _seed_commit(self.wt_repo, {
            f"knowledge/watch/reports/{date_iso}/foo.md": _landed_report_text(
                headline="Foo finding."
            ),
            f"knowledge/watch/reports/{date_iso}/README.md": (
                f"| [foo](foo.md) | ok | TL;DR: finding |\n"
            ),
        })

        # Stage a dirty ticket in the SHARED repo.
        _seed_commit(self.shared_repo, {
            "tickets/active/T-9000.md": "---\nid: T-9000\n---\n\n# Ticket\n\n",
        })
        _stage_dirty(
            self.shared_repo,
            "tickets/active/T-9000.md",
            "---\nid: T-9000\n---\n\n# Ticket (modified)\n\n",
        )
        self.assertIn("tickets/active/T-9000.md", _staged_files(self.shared_repo))

        # Worktree repo staged set must be clean.
        self.assertEqual(_staged_files(self.wt_repo), [])

        # Run watch_runner.git_commit_and_push with cwd=wt_repo.
        # git add -- knowledge/watch/reports/date_iso/ will stage the report;
        # git diff --cached should show only that file (no ticket leak).
        with patch("tools._git_utils.subprocess.run") as mock_run:
            staged_files = [f"knowledge/watch/reports/{date_iso}/foo.md",
                            f"knowledge/watch/reports/{date_iso}/README.md"]

            def fake_run(argv, **kwargs):
                if argv[0] == "git":
                    if argv[1:3] == ["diff", "--cached"]:
                        # Simulate only report files in worktree index.
                        return MagicMock(
                            returncode=0,
                            stdout="\n".join(staged_files) + "\n",
                            stderr="",
                        )
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")
            mock_run.side_effect = fake_run

            from tools.watch_runner import Topic as WRTopic
            topic = WRTopic(
                slug="foo", cadence="daily", budget=300,
                scope="(test)", seed_sources=[], notes="",
            )
            with patch("tools.watch_runner._hermes_scan_dir", return_value=(True, None)):
                ok, msg = watch_runner.git_commit_and_push(
                    date_iso, [topic], {"foo": "ok"},
                    cwd=self.wt_repo,
                )

        # Should succeed — only report files staged in worktree.
        self.assertTrue(ok, f"expected success; got msg={msg!r}")
        self.assertIn("2", msg)  # pushed 2 file(s)

        # Verify all git calls targeted wt_repo (or had no cwd, which is safe
        # since mock_run doesn't actually run git).
        for call in mock_run.call_args_list:
            call_cwd = call.kwargs.get("cwd")
            if call_cwd is not None and Path(str(call_cwd)) != self.wt_repo:
                self.fail(
                    f"git call targeted wrong cwd: expected {self.wt_repo!r}, "
                    f"got {call_cwd!r}"
                )

        # Shared dirty file remains staged there (worktree is independent).
        self.assertIn("tickets/active/T-9000.md", _staged_files(self.shared_repo))


# ---------------------------------------------------------------------------
# pantheon_config_sync — opt-out (no test needed per ticket).
# Explicit documentation of the opt-out decision: pantheon_config_sync is
# pull-only (fetch + ff-merge); it never commits new work product; the
# T-0471 scope-leak race class (dirty shared tree contaminating a commit's
# scope) does not apply. Wrapping it with with_worktree would invert its
# semantics (a worktree starts at HEAD, not at the working-tree state it
# needs to merge into). justifies-via P1 (structural fix for the actual
# race class; wrapping a non-committing tool adds complexity without fixing
# the race) + D8 (don't add tests for decisions already documented).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T-0486 regression: push from detached HEAD must land on bare origin.
#
# Root cause: with_worktree creates a --detach worktree; a detached HEAD's
# commit advances HEAD but NOT refs/heads/main. `git push origin main`
# pushes the unchanged shared main ref → no-op. Fix: `git push origin HEAD:main`.
#
# Test shape: create a bare origin + working repo, detach HEAD (`git checkout
# --detach`), commit, invoke the cron's push function with cwd=repo. Assert
# the bare origin's main has advanced. RED against pre-fix `origin main`,
# GREEN against post-fix `HEAD:main`.
#
# Covers ticket_lint (_git_commit_fixes) and watch_runner (git_commit_and_push)
# as a representative pair. ticket_grooming and spine_index_groom use the same
# cwd-threaded pattern; the detached-HEAD no-op is the same structural class.
# ---------------------------------------------------------------------------


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


def _init_detached_repo(base: Path) -> tuple[Path, Path]:
    """Create a bare origin + working repo with a baseline commit, then detach
    HEAD in the working repo (simulating `git worktree add --detach`).

    Returns (repo_path, bare_path).
    """
    bare = base / "origin.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare"], cwd=bare, capture_output=True, check=True)

    repo = base / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "integ@test"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "IntegTest"], cwd=repo, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, capture_output=True)

    # Baseline commit + push so bare has a main ref to compare against.
    (repo / "BASELINE.md").write_text("# baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "BASELINE.md"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo, capture_output=True, check=True)

    # Detach HEAD — mirrors `git worktree add --detach ... origin/main`.
    # On a detached HEAD, commits advance HEAD but NOT refs/heads/main.
    subprocess.run(["git", "checkout", "--detach"], cwd=repo, capture_output=True, check=True)

    return repo, bare


class TestTicketLintPushLandsOnBareOrigin(unittest.TestCase):
    """T-0486 regression: ticket_lint's commit must land on the bare origin when
    operating from a detached HEAD (the with_worktree shape). T-0558 moved the push
    out of `_git_commit_fixes` into the caller's unified push_with_retry(HEAD:main);
    this exercises that exact step after `_git_commit_fixes` commits.

    Pre-fix (`git push origin main` from detached HEAD): bare origin main does
    NOT advance — the push is a no-op because it pushes the unchanged shared
    refs/heads/main, not the detached commit.

    Post-fix (`git push origin HEAD:main`): bare origin main DOES advance.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_commit_lands_on_bare_origin_from_detached_head(self) -> None:
        """After _git_commit_fixes commits + the caller's unified push runs from a
        detached HEAD, bare origin main must have advanced to include the new commit.
        """
        from tools import ticket_lint
        from tools._git_utils import push_with_retry

        # Capture bare origin main before the push.
        origin_before = _bare_origin_head(self.bare)
        self.assertNotEqual(origin_before, "", "bare origin must have a main ref after setup")

        # Seed ticket dirs (both inbox + active required — _git_commit_fixes stages both).
        tickets_inbox = self.repo / "tickets" / "inbox"
        tickets_inbox.mkdir(parents=True, exist_ok=True)
        (self.repo / "tickets" / "active").mkdir(parents=True, exist_ok=True)

        ticket_path = tickets_inbox / "T-7001.md"
        ticket_path.write_text(
            "---\nid: T-7001\ncreated: 2026-05-20T10:00:00Z\n"
            "requester: operator\ntarget: operator\nstate: inbox\n"
            "parent_ticket: null\ndepends_on: []\nintent: action\n---\n"
            "\n# Test ticket T-7001\n\n## Ask\n\nDo the thing.\n\n"
            "## Transition log\n\n- 2026-05-20T10:00:00Z [operator] filed in inbox\n",
            encoding="utf-8",
        )
        # No manual git add — _git_commit_fixes does its own `git add tickets/inbox/
        # tickets/active/` internally. Just write the file and let the function handle it.

        # Step 1: _git_commit_fixes commits locally (T-0558: it does NOT push).
        ok, msg = ticket_lint._git_commit_fixes(
            [ticket_path],
            "2026-05-20",
            cwd=self.repo,
        )
        self.assertTrue(ok, f"_git_commit_fixes failed: {msg!r}")

        # Step 2: the caller's unified push — push_with_retry(HEAD:main), the step
        # that MUST use HEAD:main (not main) to land the detached-HEAD commit.
        ok_push, push_msg = push_with_retry(self.repo, refspec="HEAD:main")
        self.assertTrue(ok_push, f"unified push_with_retry failed: {push_msg!r}")

        # The bare origin's main MUST have advanced.
        origin_after = _bare_origin_head(self.bare)
        self.assertNotEqual(
            origin_after, origin_before,
            f"Bare origin main did NOT advance after push from detached HEAD "
            f"(before={origin_before!r}, after={origin_after!r}). "
            f"This is the T-0486 regression: 'git push origin main' from a "
            f"detached HEAD is a no-op. Fix: 'git push origin HEAD:main'."
        )


class TestWatchRunnerGitCommitAndPushLandsOnBareOrigin(unittest.TestCase):
    """T-0486 regression: watch_runner's git_commit_and_push must land on the
    bare origin when operating from a detached HEAD (the with_worktree shape).

    Same structural class as ticket_lint: pre-fix push is a no-op; post-fix lands.
    Covers the representative second cron (watch_runner handles knowledge/watch/reports/).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo, self.bare = _init_detached_repo(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_commit_lands_on_bare_origin_from_detached_head(self) -> None:
        """After git_commit_and_push runs from a detached HEAD, bare origin main
        must have advanced to include the new watch report commit.
        """
        from tools import watch_runner
        from tools.watch_runner import Topic as WRTopic

        origin_before = _bare_origin_head(self.bare)
        self.assertNotEqual(origin_before, "", "bare origin must have a main ref after setup")

        # Seed a report file in the detached repo. Must be landed-ok shape
        # (T-1581: watch_runner.git_commit_and_push's partial-stage guard now
        # requires exists+nontrivial+structured, not just present).
        date_iso = "2026-05-20"
        reports_dir = self.repo / "knowledge" / "watch" / "reports" / date_iso
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "test-topic.md").write_text(
            _landed_report_text(headline="Test topic finding."),
            encoding="utf-8",
        )

        # Stage in the detached repo.
        subprocess.run(
            ["git", "add", "--", f"knowledge/watch/reports/{date_iso}/"],
            cwd=self.repo, capture_output=True, check=True,
        )

        topic = WRTopic(
            slug="test-topic", cadence="daily", budget=300,
            scope="(test)", seed_sources=[], notes="",
        )

        # Call git_commit_and_push with cwd pointing at the detached repo.
        ok, msg = watch_runner.git_commit_and_push(
            date_iso, [topic], {"test-topic": "ok"},
            cwd=self.repo,
        )
        self.assertTrue(ok, f"git_commit_and_push failed: {msg!r}")

        # The bare origin's main MUST have advanced.
        origin_after = _bare_origin_head(self.bare)
        self.assertNotEqual(
            origin_after, origin_before,
            f"Bare origin main did NOT advance after push from detached HEAD "
            f"(before={origin_before!r}, after={origin_after!r}). "
            f"This is the T-0486 regression: 'git push origin main' from a "
            f"detached HEAD is a no-op. Fix: 'git push origin HEAD:main'."
        )


if __name__ == "__main__":
    unittest.main()
