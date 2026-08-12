"""Tests for .githooks/pre-push.

Run from repo root:
    python3 -m unittest tools.test_pre_push_hook -v

Covers:
  - Hook skips rebase when local is up-to-date with origin
  - Hook rebases cleanly when local is behind with no conflict
  - Hook blocks push (exit 1) when rebase hits a conflict
  - Hook warns + exits 0 on fetch failure (fail-open)
  - Hook skips check entirely on detached HEAD
  - install_hooks.sh sets pre-push executable idempotently
  - install_hooks.sh does not overwrite user modifications to pre-push

Fixture-string note: the HERMES trigger byte-sequence is never written
contiguously in this source file. Any reference uses the discipline-escaped
form with a literal backslash before the dot (per CLAUDE.md byte-sequence
discipline).

Source-cleanliness verification: grep -cF for the unescaped trigger
against this file must return 0.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve repo root and hook path.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".githooks"
PRE_PUSH_HOOK = HOOKS_DIR / "pre-push"
INSTALL_HOOKS_SH = REPO_ROOT / "tools" / "install_hooks.sh"


# ---------------------------------------------------------------------------
# Helpers for building synthetic git repos.
# (Pattern lifted from tools/test_hermes_hook.py TestGitSmoke.)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path, check: bool = False,
         env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    run_env["GIT_AUTHOR_NAME"] = "Test"
    run_env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    run_env["GIT_COMMITTER_NAME"] = "Test"
    run_env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    if env:
        run_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env=run_env,
    )


def _commit_file(repo: Path, filename: str, content: str,
                 message: str) -> None:
    """Write a file, stage it, and commit."""
    (repo / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=repo, check=True)
    _git("commit", "-m", message, cwd=repo, check=True)


def _invoke_hook(repo: Path, remote: str, url: str,
                 env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the pre-push hook as git would: pass REMOTE and URL as $1/$2,
    and stdin is the refs being pushed (empty stdin is valid for our tests).
    Hook receives: argv[1]=remote, argv[2]=url.
    """
    run_env = os.environ.copy()
    run_env["GIT_DIR"] = str(repo / ".git")
    run_env["GIT_AUTHOR_NAME"] = "Test"
    run_env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    run_env["GIT_COMMITTER_NAME"] = "Test"
    run_env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(PRE_PUSH_HOOK), remote, url],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        input="",
        env=run_env,
    )


def _make_origin_and_clone(tmpdir: Path) -> tuple[Path, Path]:
    """Create a bare origin repo and a full clone with one shared commit.

    Returns (origin_path, clone_path).
    """
    origin = tmpdir / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmpdir, check=True)

    # Create a working intermediary to make the initial commit.
    staging = tmpdir / "staging"
    staging.mkdir()
    _git("init", "-b", "main", cwd=staging, check=True)
    _git("remote", "add", "origin", str(origin), cwd=staging, check=True)
    _commit_file(staging, "base.txt", "base content\n", "initial commit")
    _git("push", "-u", "origin", "main", cwd=staging, check=True)

    # Clone origin into the working local repo.
    clone = tmpdir / "local"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "Test",
             "GIT_AUTHOR_EMAIL": "test@example.com",
             "GIT_COMMITTER_NAME": "Test",
             "GIT_COMMITTER_EMAIL": "test@example.com"},
    )

    return origin, clone


# ---------------------------------------------------------------------------
# Hook behavior tests (5).
# ---------------------------------------------------------------------------


class TestPrePushHookBehavior(unittest.TestCase):

    def test_pre_push_hook_skips_when_local_up_to_date(self) -> None:
        """Hook exits 0 and does not rebase when local == origin tip."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)

            # Both sides are in sync after clone — no divergence.
            result = _invoke_hook(clone, "origin", str(origin))

        self.assertEqual(
            result.returncode, 0,
            f"hook should exit 0 when up-to-date; stderr={result.stderr!r}",
        )
        self.assertNotIn(
            "rebasing", result.stderr.lower(),
            "no rebase should occur when local is up-to-date",
        )

    def test_pre_push_hook_rebases_when_local_behind_with_no_conflict(self) -> None:
        """Hook exits 0 and rebases when origin is ahead with non-conflicting commit."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)

            # Add a commit directly to origin (simulating another writer pushing).
            staging2 = tmpdir / "staging2"
            subprocess.run(
                ["git", "clone", str(origin), str(staging2)],
                capture_output=True, text=True, check=True,
                env={**os.environ,
                     "GIT_AUTHOR_NAME": "Test",
                     "GIT_AUTHOR_EMAIL": "test@example.com",
                     "GIT_COMMITTER_NAME": "Test",
                     "GIT_COMMITTER_EMAIL": "test@example.com"},
            )
            _commit_file(staging2, "file_b.txt", "origin-only content\n",
                         "origin-side commit")
            _git("push", "origin", "main", cwd=staging2, check=True)

            # Add a local-only commit on a different file.
            _commit_file(clone, "file_c.txt", "local-only content\n",
                         "local-side commit")

            # Invoke hook — should rebase local onto origin tip.
            result = _invoke_hook(clone, "origin", str(origin))

            self.assertEqual(
                result.returncode, 0,
                f"hook should exit 0 on clean rebase; stderr={result.stderr!r}",
            )

            # After hook runs, verify both commits exist in local log.
            log_result = _git("log", "--oneline", cwd=clone)
            log = log_result.stdout
            self.assertIn("origin-side commit", log,
                          "origin commit should appear in local log after rebase")
            self.assertIn("local-side commit", log,
                          "local commit should appear in local log after rebase")

    def test_pre_push_hook_blocks_push_when_rebase_has_conflict(self) -> None:
        """Hook exits 1 when rebase produces a conflict; stderr contains hint."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)

            # Both sides modify the same line of shared.txt.
            # Push origin-side change via staging repo.
            staging2 = tmpdir / "staging2"
            subprocess.run(
                ["git", "clone", str(origin), str(staging2)],
                capture_output=True, text=True, check=True,
                env={**os.environ,
                     "GIT_AUTHOR_NAME": "Test",
                     "GIT_AUTHOR_EMAIL": "test@example.com",
                     "GIT_COMMITTER_NAME": "Test",
                     "GIT_COMMITTER_EMAIL": "test@example.com"},
            )
            _commit_file(staging2, "shared.txt", "version-origin\n",
                         "origin modifies shared file")
            _git("push", "origin", "main", cwd=staging2, check=True)

            # Local also modifies the same file — creates a conflict.
            _commit_file(clone, "shared.txt", "version-local\n",
                         "local modifies shared file")

            result = _invoke_hook(clone, "origin", str(origin))

        self.assertEqual(
            result.returncode, 1,
            f"hook should exit 1 on rebase conflict; stderr={result.stderr!r}",
        )
        # The hook should emit a "resolve conflicts" or "rebase FAILED" hint.
        stderr_lower = result.stderr.lower()
        self.assertTrue(
            "resolve conflicts" in stderr_lower or "rebase failed" in stderr_lower,
            f"stderr should contain conflict-resolve hint; got: {result.stderr!r}",
        )

    def test_pre_push_hook_warns_and_continues_on_fetch_failure_fail_open(self) -> None:
        """Hook exits 0 and emits WARNING when fetch fails (network error)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            # Create a simple local repo — no real origin needed.
            repo = tmpdir / "repo"
            repo.mkdir()
            _git("init", cwd=repo, check=True)
            _commit_file(repo, "readme.txt", "hello\n", "initial commit")

            # Point the remote at a path that cannot exist.
            result = _invoke_hook(
                repo, "origin", "file:///nonexistent_path_xyz_does_not_exist"
            )

        self.assertEqual(
            result.returncode, 0,
            f"hook should exit 0 (fail-open) on fetch failure; stderr={result.stderr!r}",
        )
        self.assertIn(
            "WARNING", result.stderr,
            f"hook should emit WARNING on fetch failure; got stderr={result.stderr!r}",
        )

    def test_pre_push_hook_skips_check_on_detached_head(self) -> None:
        """Hook exits 0 immediately on detached HEAD; no fetch attempted.

        Distinguishing detached-HEAD-skip from fetch-fail-open:
          - fail-open path always emits 'WARNING' to stderr
          - detached-HEAD path exits before any fetch → stderr is empty
        Using an unreachable remote ensures: if hook ever reaches the fetch
        step, it will emit WARNING. Absence of WARNING proves the early exit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            repo = tmpdir / "repo"
            repo.mkdir()
            _git("init", cwd=repo, check=True)
            _commit_file(repo, "a.txt", "first\n", "commit one")
            _commit_file(repo, "b.txt", "second\n", "commit two")

            # Detach HEAD by checking out the first commit hash.
            log = _git("log", "--format=%H", cwd=repo)
            commits = log.stdout.strip().splitlines()
            first_commit = commits[-1]  # oldest commit
            _git("checkout", first_commit, cwd=repo, check=True)

            # Use an unreachable remote: if fetch fires, WARNING will appear.
            result = _invoke_hook(
                repo, "origin", "file:///nonexistent_path_detached_head_test"
            )

        self.assertEqual(
            result.returncode, 0,
            f"hook should exit 0 on detached HEAD; stderr={result.stderr!r}",
        )
        self.assertNotIn(
            "WARNING", result.stderr,
            "hook should skip before fetch on detached HEAD — no WARNING expected; "
            f"got stderr={result.stderr!r}",
        )


# ---------------------------------------------------------------------------
# Install + idempotence tests (2).
# ---------------------------------------------------------------------------


class TestInstallHooks(unittest.TestCase):

    def _make_install_fixture(self, tmpdir: Path) -> Path:
        """Create a synthetic git repo with .githooks/pre-push and
        tools/install_hooks.sh copied in.  Returns the repo root path.
        """
        repo = tmpdir / "repo"
        repo.mkdir()
        _git("init", cwd=repo, check=True)

        # Copy the real hook + install script into the fixture.
        hooks_dir = repo / ".githooks"
        hooks_dir.mkdir()
        shutil.copy2(PRE_PUSH_HOOK, hooks_dir / "pre-push")

        # Also copy existing hooks so install_hooks.sh doesn't warn about them.
        for hook_name in ("pre-commit", "commit-msg"):
            src = HOOKS_DIR / hook_name
            if src.exists():
                shutil.copy2(src, hooks_dir / hook_name)

        tools_dir = repo / "tools"
        tools_dir.mkdir()
        shutil.copy2(INSTALL_HOOKS_SH, tools_dir / "install_hooks.sh")

        return repo

    def test_install_hooks_sets_pre_push_executable_idempotently(self) -> None:
        """Running install_hooks.sh twice: pre-push is +x; second run is no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            repo = self._make_install_fixture(tmpdir)

            # First run.
            r1 = subprocess.run(
                ["bash", "tools/install_hooks.sh"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                r1.returncode, 0,
                f"first install run should exit 0; stderr={r1.stderr!r} stdout={r1.stdout!r}",
            )

            # pre-push must be executable after first run.
            pre_push_path = repo / ".githooks" / "pre-push"
            mode = pre_push_path.stat().st_mode
            self.assertTrue(
                mode & stat.S_IXUSR,
                "pre-push must be executable (S_IXUSR) after install",
            )

            # Second run — idempotent.
            r2 = subprocess.run(
                ["bash", "tools/install_hooks.sh"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                r2.returncode, 0,
                f"second install run should exit 0; stderr={r2.stderr!r}",
            )
            self.assertIn(
                "already configured", r2.stdout,
                f"second run should emit 'already configured'; got stdout={r2.stdout!r}",
            )

    def test_install_hooks_does_not_overwrite_existing_user_modifications_to_pre_push(
        self,
    ) -> None:
        """install_hooks.sh preserves user modifications to pre-push content."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            repo = self._make_install_fixture(tmpdir)

            # First run to configure hooksPath.
            subprocess.run(
                ["bash", "tools/install_hooks.sh"],
                cwd=repo, capture_output=True, text=True, check=True,
            )

            # User appends a comment to the hook file.
            pre_push_path = repo / ".githooks" / "pre-push"
            with open(pre_push_path, "a", encoding="utf-8") as f:
                f.write("# user-comment — do not remove\n")

            # Second run.
            subprocess.run(
                ["bash", "tools/install_hooks.sh"],
                cwd=repo, capture_output=True, text=True, check=True,
            )

            # User's comment must still be present.
            content = pre_push_path.read_text(encoding="utf-8")
            self.assertIn(
                "# user-comment — do not remove",
                content,
                "install_hooks.sh must not overwrite hook file content",
            )


# ---------------------------------------------------------------------------
# Self-audit: pre-push hook source must be byte-sequence-clean.
# ---------------------------------------------------------------------------


class TestPrePushHookSourceCleanliness(unittest.TestCase):
    """Verify that pre-push hook source never contains the unescaped HERMES
    trigger as contiguous bytes — same discipline as test_hermes_hook.py's
    TestHookSourceCleanliness.
    """

    def test_pre_push_hook_source_clean(self) -> None:
        trigger = b"HERME" + b"S.md"
        content = PRE_PUSH_HOOK.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            ".githooks/pre-push must not contain the unescaped trigger "
            "(byte-sequence discipline per CLAUDE.md).",
        )

    def test_this_test_file_source_clean(self) -> None:
        trigger = b"HERME" + b"S.md"
        content = Path(__file__).read_bytes()
        self.assertNotIn(
            trigger,
            content,
            "test_pre_push_hook.py must not contain the unescaped trigger.",
        )

    def test_pre_push_hook_is_executable(self) -> None:
        mode = PRE_PUSH_HOOK.stat().st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            ".githooks/pre-push must be executable (chmod +x)",
        )


# ---------------------------------------------------------------------------
# Auto-commit-cron contract gate tests (T-1084).
# The gate runs the repo-content linter and blocks (exit 1) only on a cleanly
# reported VIOLATION; ADVISORY never blocks; any linter anomaly fails open.
# ---------------------------------------------------------------------------

REAL_LINTER = REPO_ROOT / "tools" / "auto_commit_contract_lint.py"

# A cron script that BOTH authors (git add) and pushes a shared ref — i.e. in
# scope for the contract. Trips _ADD_RE + _PUSH_RE in the linter.
_VIOLATING_CRON_PY = textwrap.dedent(
    """\
    # synthetic in-scope auto-commit cron for the contract-gate test
    def run():
        _git("add", "report.txt")
        push_with_retry()
    """
)


def _seed_linter_repo(clone: Path, *, execstart_tail: str,
                      include_linter: bool = True) -> None:
    """Populate a clone with the real linter + a synthetic in-scope cron wired
    to a systemd unit. `execstart_tail` is appended after the script path
    (e.g. '' for a bare unit -> VIOLATION, ' --commit-prod' -> flagged).
    """
    tools = clone / "tools"
    systemd = tools / "systemd"
    systemd.mkdir(parents=True, exist_ok=True)
    if include_linter:
        shutil.copy2(REAL_LINTER, tools / "auto_commit_contract_lint.py")
    (tools / "foo_cron.py").write_text(_VIOLATING_CRON_PY, encoding="utf-8")
    (systemd / "foo_cron.service").write_text(
        "[Unit]\nDescription=synthetic cron\n\n"
        "[Service]\n"
        f"ExecStart=/usr/bin/python3 {tools}/foo_cron.py{execstart_tail}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# T-1937: seat CODE-path-to-main guard.
# ---------------------------------------------------------------------------


def _invoke_hook_with_refs(repo: Path, refs: str, user: str = "suborch1",
                           remote: str = "origin", url: str = "git@example") -> subprocess.CompletedProcess:
    """Invoke pre-push with ref lines on stdin and a stubbed `id -un`.

    The guard is seat-gated on the unix user, which a test cannot change — so a
    stub `id` earlier on PATH supplies the identity. Everything else (the range
    diff) runs against the real synthetic repo.
    """
    stub_dir = repo / ".idstub"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "id"
    stub.write_text("#!/bin/sh\necho " + user + "\n")
    stub.chmod(0o755)

    run_env = os.environ.copy()
    run_env["GIT_DIR"] = str(repo / ".git")
    run_env["PATH"] = f"{stub_dir}:{run_env['PATH']}"
    for k, v in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@e.com"),
                 ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "t@e.com")):
        run_env[k] = v
    return subprocess.run(
        ["bash", str(PRE_PUSH_HOOK), remote, url],
        cwd=repo, capture_output=True, text=True, check=False,
        input=refs, env=run_env,
    )


def _commit_path(repo: Path, relpath: str) -> str:
    """Create/modify a file at relpath, commit it, return the new HEAD sha."""
    f = repo / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((f.read_text() if f.exists() else "") + "x\n")
    ident = os.environ.copy()
    ident.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@e.com",
    })
    subprocess.run(["git", "add", relpath], cwd=repo, check=True,
                   capture_output=True, env=ident)
    subprocess.run(["git", "-c", "core.hooksPath=", "commit", "-q", "-m", f"touch {relpath}"],
                   cwd=repo, check=True, capture_output=True, env=ident)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True, env=ident).stdout.strip()


class TestSeatCodePathGuard(unittest.TestCase):
    """The guard must block CODE to main from a seat, and nothing else."""

    def _repo_with(self, relpath: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, check=True,
                              capture_output=True, text=True).stdout.strip()
        head = _commit_path(clone, relpath)
        return clone, origin, base, head

    def test_blocks_code_path_push_to_main_from_seat(self) -> None:
        clone, origin, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("BLOCKED", r.stderr)
        self.assertIn("tools/whatever.py", r.stderr)

    def test_allows_ledger_push_to_main_from_seat(self) -> None:
        """The deploy-key bypass is load-bearing for parallel ticket/ledger
        writers — this guard must not touch it."""
        clone, origin, base, head = self._repo_with("ledger/2026-W30/x.jsonl")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_allows_ticket_push_to_main_from_seat(self) -> None:
        clone, origin, base, head = self._repo_with("tickets/active/T-9999.md")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_allows_code_push_to_agent_branch(self) -> None:
        """Branch pushes are the sanctioned code path — never blocked."""
        clone, origin, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/agent/T-1 {head} refs/heads/agent/T-1 {base}\n")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_allows_code_push_to_main_from_non_seat(self) -> None:
        """Seat-gated: the HO's own push flow is unchanged."""
        clone, origin, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="claude")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_fail_open_on_empty_stdin(self) -> None:
        """A blocking read against absent stdin would brick every push on the
        account — the failure the contract gate's comment warns about."""
        clone, origin, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(clone, "")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_warns_but_allows_unclassified_path(self) -> None:
        """A path in neither set must surface loudly, not silently pick a side."""
        clone, origin, base, head = self._repo_with("brandnewdir/thing.md")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertIn("brandnewdir/thing.md", r.stderr)
        self.assertIn("neither code nor ticket/ledger", r.stderr)


class TestContractGate(unittest.TestCase):

    def test_blocks_push_on_contract_violation(self) -> None:
        """A bare-unit in-scope auto-commit cron in the tree -> hook exits 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)
            _seed_linter_repo(clone, execstart_tail="")  # bare unit -> VIOLATION

            result = _invoke_hook(clone, "origin", str(origin))

        self.assertEqual(
            result.returncode, 1,
            f"hook must block on a contract VIOLATION; stderr={result.stderr!r}",
        )
        self.assertIn("VIOLATION", result.stderr,
                      "blocked-push message should name the VIOLATION")

    def test_allows_push_when_advisory_only(self) -> None:
        """A flagged unit (no bare ExecStart) is at most ADVISORY -> hook allows."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)
            _seed_linter_repo(clone, execstart_tail=" --commit-prod")  # flagged

            result = _invoke_hook(clone, "origin", str(origin))

        self.assertEqual(
            result.returncode, 0,
            f"ADVISORY findings must not block a push; stderr={result.stderr!r}",
        )

    def test_allows_push_when_contract_clean(self) -> None:
        """Linter present, no in-scope cron at all -> 0 findings -> hook allows."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)
            tools = clone / "tools"
            (tools / "systemd").mkdir(parents=True, exist_ok=True)
            shutil.copy2(REAL_LINTER, tools / "auto_commit_contract_lint.py")

            result = _invoke_hook(clone, "origin", str(origin))

        self.assertEqual(
            result.returncode, 0,
            f"a clean tree must allow the push; stderr={result.stderr!r}",
        )

    def test_fail_open_when_linter_absent(self) -> None:
        """No linter in the tree -> gate is skipped, push allowed (fail-open).

        Guards the invariant that a missing/broken linter never bricks pushes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = _make_origin_and_clone(tmpdir)
            # Seed a bare-unit cron BUT do NOT include the linter.
            _seed_linter_repo(clone, execstart_tail="", include_linter=False)

            result = _invoke_hook(clone, "origin", str(origin))

        self.assertEqual(
            result.returncode, 0,
            f"missing linter must fail open (allow push); stderr={result.stderr!r}",
        )


_ZERO = "0" * 40


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return _git("rev-parse", ref, cwd=repo, check=True).stdout.strip()


def _pre_fix_hook_path(tmpdir: Path) -> Path | None:
    """Write origin/main's pre-push to a temp file for a non-vacuity comparison.

    Returns None once origin/main ALREADY contains the T-1961 fix (i.e. after
    this arc merged): at that point origin/main IS the fixed hook, so comparing
    it against the live hook proves nothing and would false-fail. The caller
    skips-with-reason in that case — the non-vacuity was genuinely established
    pre-merge (and is in the ticket/PR record); a test that can only pass before
    its own change merges must degrade to a skip, not a red on main.
    """
    raw = _git("show", "origin/main:.githooks/pre-push",
               cwd=REPO_ROOT, check=True).stdout
    if "_skip_freshness" in raw or "T-1961" in raw:
        return None  # origin/main already has the fix — non-vacuity no longer testable here
    dst = tmpdir / "pre-push.prefix"
    dst.write_text(raw)
    dst.chmod(0o755)
    return dst


def _invoke_hook_file(hook: Path, repo: Path, refs: str,
                      remote: str = "origin", url: str = "git@example") -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    run_env["GIT_DIR"] = str(repo / ".git")
    for k, v in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@e.com"),
                 ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "t@e.com")):
        run_env[k] = v
    return subprocess.run(
        ["bash", str(hook), remote, url],
        cwd=repo, capture_output=True, text=True, check=False,
        input=refs, env=run_env,
    )


class TestFreshnessStdinCases(unittest.TestCase):
    """T-1961: the freshness auto-rebase must skip deletes and deliberate
    rewrites (read from stdin), while preserving the parallel-writer case.

    Every case drives the REAL hook script against a synthetic repo, and proves
    non-vacuity by running the pre-fix hook (origin/main's version) on the same
    scenario — the discipline of lessons/2026-07-22-false-red-verifier-must-call-
    prod-parser.md: test the surface, not a reimplementation.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.origin, self.clone = _make_origin_and_clone(self.tmp)
        self.base = _rev(self.clone, "origin/main")

    # -- (a) delete -------------------------------------------------------

    def test_delete_push_skips_freshness(self) -> None:
        """A ref deletion (local oid all-zeros) must skip the freshness rebase
        entirely — even while local main is behind origin."""
        # Make local main behind: advance origin, keep local at base.
        _commit_file(self.clone, "b.txt", "x\n", "advance")
        _git("push", "origin", "main", cwd=self.clone, check=True)
        _git("reset", "--hard", self.base, cwd=self.clone, check=True)
        refs = f"(delete) {_ZERO} refs/heads/stale-branch {self.base}\n"

        r = _invoke_hook_with_refs(self.clone, refs)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ref deletion", r.stderr)
        self.assertNotIn("rebasing", r.stderr)

    def test_delete_is_nonvacuous_prefix_hook_does_not_skip(self) -> None:
        prefix = _pre_fix_hook_path(self.tmp)
        if prefix is None:
            self.skipTest("origin/main already contains the T-1961 fix; "
                          "non-vacuity was proven pre-merge (PR/ticket record)")
        refs = f"(delete) {_ZERO} refs/heads/stale-branch {self.base}\n"
        r = _invoke_hook_file(prefix, self.clone, refs)
        self.assertNotIn("ref deletion", r.stderr,
                         "pre-fix hook must NOT have the delete carve-out (proves the fix is live)")

    # -- (b) deliberate rewrite -------------------------------------------

    def _make_rewrite(self) -> tuple[str, str]:
        """Return (rewritten_local_oid, old_remote_oid) on a feature branch where
        the old remote tip is NOT an ancestor of the local tip."""
        _git("checkout", "-b", "feature", cwd=self.clone, check=True)
        _commit_file(self.clone, "f.txt", "v1\n", "A")
        _git("push", "-u", "origin", "feature", cwd=self.clone, check=True)
        old = _rev(self.clone, "HEAD")
        _git("reset", "--hard", self.base, cwd=self.clone, check=True)
        _commit_file(self.clone, "f.txt", "v2\n", "A-prime")
        new = _rev(self.clone, "HEAD")
        return new, old

    def test_rewrite_push_warns_and_skips(self) -> None:
        new, old = self._make_rewrite()
        self.assertFalse(
            _git("merge-base", "--is-ancestor", old, new, cwd=self.clone).returncode == 0,
            "fixture invalid: old must NOT be an ancestor of new",
        )
        refs = f"refs/heads/feature {new} refs/heads/feature {old}\n"
        r = _invoke_hook_with_refs(self.clone, refs)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("deliberate history rewrite", r.stderr)
        self.assertNotIn("rebase FAILED", r.stderr)

    def test_rewrite_is_nonvacuous_prefix_hook_rebases(self) -> None:
        new, old = self._make_rewrite()
        prefix = _pre_fix_hook_path(self.tmp)
        if prefix is None:
            self.skipTest("origin/main already contains the T-1961 fix; "
                          "non-vacuity was proven pre-merge (PR/ticket record)")
        refs = f"refs/heads/feature {new} refs/heads/feature {old}\n"
        r = _invoke_hook_file(prefix, self.clone, refs)
        # pre-fix hook fetches + sees behind + rebases onto stale origin/feature
        self.assertNotIn("deliberate history rewrite", r.stderr,
                         "pre-fix hook lacks the rewrite carve-out")

    # -- parallel-writer (must be preserved) ------------------------------

    def test_parallel_writer_still_rebases(self) -> None:
        """Local extends its known remote (remote oid IS an ancestor); a
        divergent remote must still trigger the auto-rebase, unchanged."""
        # origin/main advances (other writer), local adds its own commit off base.
        staging = self.tmp / "staging2"
        _git("clone", str(self.origin), str(staging), cwd=self.tmp, check=True)
        _commit_file(staging, "other.txt", "remote\n", "remote-side")
        _git("push", "origin", "main", cwd=staging, check=True)
        _commit_file(self.clone, "mine.txt", "local\n", "local-side")
        loid = _rev(self.clone, "HEAD")
        # stdin remote oid = base (local's KNOWN remote, an ancestor of loid)
        refs = f"refs/heads/main {loid} refs/heads/main {self.base}\n"

        r = _invoke_hook_with_refs(self.clone, refs)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rebasing", r.stderr)  # freshness ran, as before
        # and the local now contains the remote-side commit
        log = _git("log", "--oneline", cwd=self.clone, check=True).stdout
        self.assertIn("remote-side", log)

    # -- new branch -------------------------------------------------------

    def test_new_branch_push_skips(self) -> None:
        _git("checkout", "-b", "brand-new", cwd=self.clone, check=True)
        _commit_file(self.clone, "n.txt", "new\n", "new")
        loid = _rev(self.clone, "HEAD")
        refs = f"refs/heads/brand-new {loid} refs/heads/brand-new {_ZERO}\n"
        r = _invoke_hook_with_refs(self.clone, refs)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new remote branch", r.stderr)
        self.assertNotIn("rebasing", r.stderr)

    # -- empty stdin falls through (fail-safe) ----------------------------

    def test_empty_stdin_falls_through_to_freshness(self) -> None:
        """No stdin → behave exactly as the pre-fix hook (run the freshness
        check). Proven by an up-to-date repo: exit 0, no skip message."""
        r = _invoke_hook_with_refs(self.clone, "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ref deletion", r.stderr)
        self.assertNotIn("deliberate history rewrite", r.stderr)
        self.assertNotIn("new remote branch", r.stderr)


# ---------------------------------------------------------------------------
# T-2077: ledger shard-placement WARN gate.
# The gate runs the placement linter over the push range and WARNS on any
# newly-added entry in the wrong ISO-week shard — but NEVER blocks (misplacement
# is cosmetic: readers union-glob + key off entry_ts). Fleet-wide, main-only.
# ---------------------------------------------------------------------------

import json  # noqa: E402

# 2026-07-26 (Sun) is ISO W30's last day; 2026-07-27 (Mon) is W31's first —
# the rollover where boundary-spillover happens.
_TS_W30 = "2026-07-26T12:00:00Z"
_TS_W31 = "2026-07-27T12:00:00Z"
_LEDGER_LINT_SRC = REPO_ROOT / "tools" / "ledger_placement_lint.py"
_LEDGER_SHARD_SRC = REPO_ROOT / "tools" / "ledger_shard.py"


def _seed_ledger_lint(clone: Path) -> None:
    """Copy the real linter + its ledger_shard dependency into the clone so the
    hook's `${REPO_ROOT}/tools/ledger_placement_lint.py` resolves."""
    tools = clone / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_LEDGER_LINT_SRC, tools / "ledger_placement_lint.py")
    shutil.copy2(_LEDGER_SHARD_SRC, tools / "ledger_shard.py")


def _commit_ledger_line(repo: Path, seg: str, entry: dict) -> str:
    """Append one JSON entry to ledger/<seg>/<seg>.jsonl, commit, return HEAD."""
    rel = f"ledger/{seg}/{seg}.jsonl"
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((f.read_text() if f.exists() else "") + json.dumps(entry) + "\n")
    ident = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
    _git("add", rel, cwd=repo, check=True, env=ident)
    _git("-c", "core.hooksPath=", "commit", "-q", "-m", f"ledger {seg}",
         cwd=repo, check=True, env=ident)
    return _git("rev-parse", "HEAD", cwd=repo, check=True, env=ident).stdout.strip()


class TestLedgerPlacementWarn(unittest.TestCase):
    """WARN (never block) on a newly-added misplaced ledger entry."""

    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        base = _git("rev-parse", "HEAD", cwd=clone, check=True).stdout.strip()
        return clone, base

    def test_warns_but_does_not_block_on_misplaced_added_entry(self) -> None:
        clone, base = self._setup()
        _seed_ledger_lint(clone)
        # a W31-ts entry written into the W30 shard = boundary spillover
        head = _commit_ledger_line(
            clone, "2026-W30", {"id": "spill-1", "ts": _TS_W31, "agent": "metis"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, f"must WARN not block; stderr={r.stderr!r}")
        self.assertIn("wrong ISO-week shard", r.stderr)
        self.assertIn("spill-1", r.stderr)
        self.assertIn("NOT blocked", r.stderr)

    def test_silent_on_correctly_placed_entry(self) -> None:
        clone, base = self._setup()
        _seed_ledger_lint(clone)
        head = _commit_ledger_line(
            clone, "2026-W30", {"id": "ok-1", "ts": _TS_W30, "agent": "metis"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("wrong ISO-week shard", r.stderr)

    def test_fail_open_when_linter_absent(self) -> None:
        # linter NOT seeded -> gate no-ops, push proceeds, no warn text
        clone, base = self._setup()
        head = _commit_ledger_line(
            clone, "2026-W30", {"id": "spill-2", "ts": _TS_W31, "agent": "metis"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("wrong ISO-week shard", r.stderr)

    def test_does_not_fire_on_non_main_ref(self) -> None:
        # a branch push must not trigger the main-only ledger warn
        clone, base = self._setup()
        _seed_ledger_lint(clone)
        head = _commit_ledger_line(
            clone, "2026-W30", {"id": "spill-3", "ts": _TS_W31, "agent": "metis"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/agent/T-1 {head} refs/heads/agent/T-1 {base}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("wrong ISO-week shard", r.stderr)


if __name__ == "__main__":
    unittest.main()
