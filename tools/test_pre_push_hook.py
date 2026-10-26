"""Tests for .githooks/pre-push.

Run from repo root:
    python3 -m unittest tools.test_pre_push_hook -v

Covers:
  - Hook skips rebase when local is up-to-date with origin
  - Hook rebases cleanly when local is behind with no conflict
  - Hook blocks push (exit 1) when rebase hits a conflict
  - Hook warns + exits 0 on fetch failure (fail-open)
  - Hook skips the freshness check on detached HEAD (the seat CODE-path guard
    still runs there; see TestSeatGuardOnDetachedHead)
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


class TestPrePushFetchHeadFree(unittest.TestCase):
    """T-5575: the freshness auto-rebase must not lean on FETCH_HEAD. A concurrent fetch in
    the same clone (the seat's session, the 5-min inbox-consumer pull) can leave a
    multi-entry FETCH_HEAD, and `git pull --rebase` then refuses with 'Cannot rebase onto
    multiple branches' (live: pantheon-inbox-consumer@specialist, 2026-10-26 08:22Z). Same class
    as T-0218/T-5434. The hook already fetches, so it rebases onto the explicit
    remote-tracking ref."""

    def _wrapper(self, tmpdir: Path) -> tuple[dict, Path]:
        real = subprocess.run(["bash", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
        bindir = tmpdir / "bin"
        bindir.mkdir()
        log = tmpdir / "git-calls.log"
        (bindir / "git").write_text(
            "#!/bin/bash\n"
            f"echo \"$1 $2\" >> '{log}'\n"
            f"'{real}' \"$@\"; rc=$?\n"
            # Simulate the race: after ANY fetch, a second for-merge entry lands in FETCH_HEAD.
            "for a in \"$@\"; do if [ \"$a\" = fetch ]; then\n"
            f"  gd=$('{real}' rev-parse --git-dir 2>/dev/null)\n"
            "  if [ -s \"$gd/FETCH_HEAD\" ]; then l=$(head -1 \"$gd/FETCH_HEAD\"); printf '%s\\n' \"${l/main/other}\" >> \"$gd/FETCH_HEAD\"; fi\n"
            "  break; fi; done\n"
            "exit $rc\n")
        (bindir / "git").chmod(0o755)
        return {"PATH": f"{bindir}:{os.environ['PATH']}"}, log

    def _behind_clone(self, tmpdir: Path) -> tuple[Path, Path]:
        origin, clone = _make_origin_and_clone(tmpdir)
        staging2 = tmpdir / "staging2"
        subprocess.run(["git", "clone", str(origin), str(staging2)], capture_output=True, text=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})
        _commit_file(staging2, "file_b.txt", "origin-only\n", "origin-side commit")
        _git("push", "origin", "main", cwd=staging2, check=True)
        _commit_file(clone, "file_c.txt", "local-only\n", "local-side commit")
        return origin, clone

    def test_hook_never_invokes_git_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = self._behind_clone(tmpdir)
            env, log = self._wrapper(tmpdir)
            _invoke_hook(clone, "origin", str(origin), env=env)
            calls = log.read_text().splitlines() if log.exists() else []
        self.assertTrue(any(c.startswith("fetch") for c in calls), calls)  # the wrapper saw the hook
        self.assertFalse([c for c in calls if c.startswith("pull")],
                         f"pre-push must not run `git pull` (FETCH_HEAD-reliant): {calls}")

    def test_behind_rebase_survives_a_multi_entry_fetch_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone = self._behind_clone(tmpdir)
            env, _ = self._wrapper(tmpdir)
            result = _invoke_hook(clone, "origin", str(origin), env=env)
            log = _git("log", "--oneline", cwd=clone).stdout
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        self.assertNotIn("multiple branches", result.stderr)
        self.assertIn("origin-side commit", log)
        self.assertIn("local-side commit", log)


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
                           remote: str = "origin", url: str = "git@example",
                           extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke pre-push with ref lines on stdin and a stubbed `id -un`.

    The guard is seat-gated on the unix user, which a test cannot change — so a
    stub `id` earlier on PATH supplies the identity. Everything else (the range
    diff) runs against the real synthetic repo. `extra_env` overrides the
    environment (e.g. LC_ALL). Output is decoded with errors="replace": the hook
    may print a path holding an invalid-UTF-8 byte and the harness must not choke
    on the very thing a test is trying to provoke.
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
    if extra_env:
        run_env.update(extra_env)
    return subprocess.run(
        ["bash", str(PRE_PUSH_HOOK), remote, url],
        cwd=repo, capture_output=True, text=True, errors="replace", check=False,
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

    def test_blocks_harness_code_path_push_to_main_from_seat(self) -> None:
        """T-4761: harness/ (ORG-091's imported harness code) is PR-only like tools/."""
        clone, origin, base, head = self._repo_with("harness/codex/codex_telegram/x.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("BLOCKED", r.stderr)
        self.assertIn("harness/codex/codex_telegram/x.py", r.stderr)

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

    def _diverged(self, own_relpath: str):
        """Another writer lands a CODE commit on origin after the seat's base;
        the seat commits `own_relpath` on the stale base. Returns (clone, R, L)
        where R is the remote tip and L the seat's (pre-rebase) local sha —
        exactly the pair git hands the hook when the seat is behind."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        staging = Path(tmp.name) / "staging"
        (staging / "tools").mkdir()
        # Distinct content: identical blobs on both sides would let rename
        # detection pair them up and hide the foreign path from --name-only.
        _commit_file(staging, "tools/other_writer.py", "foreign code\n",
                     "other writer lands code")
        remote_tip = _git("rev-parse", "HEAD", cwd=staging, check=True).stdout.strip()
        _git("push", "origin", "main", cwd=staging, check=True)
        # No fetch here: like the artisan incident, the remote tip is unknown
        # locally, so the hook takes the parallel-writer path (fetch + rebase)
        # before the guard runs.
        local = _commit_path(clone, own_relpath)
        return clone, remote_tip, local

    def test_allows_ticket_push_when_behind_a_foreign_code_commit(self) -> None:
        """T-4104: a seat's ticket-only push that races someone else's code
        merge must not be blocked for the OTHER writer's paths (the two-dot
        R..L tree diff reported them as if the seat were pushing them)."""
        clone, remote_tip, local = self._diverged("tickets/active/T-9999.md")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertNotIn("BLOCKED", r.stderr)

    def test_still_blocks_own_code_when_behind(self) -> None:
        """Non-vacuous twin: on the same diverged shape, the seat's OWN code
        path still blocks — and only its own path is named."""
        clone, remote_tip, local = self._diverged("tools/mine.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("tools/mine.py", r.stderr)
        self.assertNotIn("tools/other_writer.py", r.stderr)


# T-4226: the seat test is INVERTED — every unix user except HO's `claude` is a
# seat, so a new account is fail-closed by construction rather than by edit.
_SEATS = (
    "claude-fallback",   # Recovery
    "codex",             # Consultant
    "claude-research",   # Specialist
    "exocortex",         # Archivist
    "suborch1",          # mason
    "suborch2",          # artisan
    "pantheon-test",
    "the-ancestor",
    "some-future-seat",  # any account not yet created
)


class TestSeatSetInverted(unittest.TestCase):
    """T-4226: HO (`claude`) is the only non-seat; everyone else is fail-closed."""

    def _repo_with(self, relpath: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        base = _rev(clone)
        head = _commit_path(clone, relpath)
        return clone, base, head

    def test_every_non_ho_user_is_blocked_on_code_to_main(self) -> None:
        clone, base, head = self._repo_with("tools/whatever.py")
        for user in _SEATS:
            with self.subTest(user=user):
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {head} refs/heads/main {base}\n", user=user)
                self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
                self.assertIn("BLOCKED", r.stderr)
                self.assertIn(user, r.stderr)

    def test_blocks_every_code_root_for_a_new_seat(self) -> None:
        for path in ("tools/x.py", ".github/workflows/x.yml", ".githooks/x"):
            with self.subTest(path=path):
                clone, base, head = self._repo_with(path)
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {head} refs/heads/main {base}\n",
                    user="some-future-seat")
                self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
                self.assertIn(path, r.stderr)

    def test_unusual_filenames_cannot_dodge_the_code_path_match(self) -> None:
        """`git diff --name-only` C-quotes a path with a non-ASCII byte, a double
        quote or a backslash (`"tools/caf\\303\\251.py"`), and the quoted form starts
        with `"`, not `tools/` -- so a seat could name a file to walk past the guard."""
        for path in ("tools/café.py", 'tools/a"b.py', "tools/a\\b.py"):
            with self.subTest(path=path):
                clone, base, head = self._repo_with(path)
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {head} refs/heads/main {base}\n",
                    user="some-future-seat")
                self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
                self.assertIn("BLOCKED", r.stderr)

    def test_invalid_utf8_code_name_blocks_under_a_utf8_locale(self) -> None:
        """N1a: under a UTF-8 locale GNU grep treats a name holding an invalid byte
        (`tools/caf\\xe9.py`) as a BINARY file — "binary file matches", no line
        printed — so a line-oriented match came out empty and the hook exited 0."""
        clone, base, head = self._repo_with("tools/caf\udce9.py")  # surrogateescape = raw \xe9
        for locale in ("C.UTF-8", "C"):
            with self.subTest(locale=locale):
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {head} refs/heads/main {base}\n",
                    user="some-future-seat", extra_env={"LC_ALL": locale})
                self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
                self.assertIn("BLOCKED", r.stderr)

    def test_a_newline_in_a_name_cannot_take_the_readme_exemption(self) -> None:
        """N1b: `tools/README.md<LF>x` is ONE path under tools/. Split on newlines it
        becomes `tools/README.md` (exempt) plus `x` (not code) and the exemption
        leaks: the whole name must be matched as a single string."""
        clone, base, head = self._repo_with("tools/README.md\nx")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("BLOCKED", r.stderr)

    def test_block_message_says_how_to_unstick_a_local_main_that_carries_the_code(self) -> None:
        """N7: a seat whose LOCAL main already holds the blocked commit stays blocked
        for its ticket/ledger pushes too until it moves the commit off main."""
        clone, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("git branch agent/T-NNNN", r.stderr)
        self.assertIn("git reset --hard origin/main", r.stderr)
        self.assertIn("ticket/ledger pushes", r.stderr)
        # N-c: origin/main must be current BEFORE the reset, and the reset discards
        # uncommitted work and moves every unpushed main commit onto the branch.
        self.assertIn("git fetch origin", r.stderr)
        self.assertLess(r.stderr.index("git fetch origin"), r.stderr.index("git reset --hard"))
        # the message hard-wraps lines; compare the flattened prose
        flat = " ".join(line.removeprefix("pre-push:").strip()
                        for line in r.stderr.splitlines()).lower()
        self.assertIn("commit or stash first", flat)
        self.assertIn("ticket commits", flat)

    def test_ho_is_allowed_on_code_to_main(self) -> None:
        clone, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="claude")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertNotIn("BLOCKED", r.stderr)

    def test_every_seat_still_allowed_on_ticket_and_ledger_only(self) -> None:
        """Non-vacuous twin: the same seats the block test stops are NOT stopped
        for the parallel-writer ticket/ledger traffic (T-1922 accept-and-monitor)."""
        for path in ("tickets/active/T-9999.md", "ledger/2026-W40/x.jsonl"):
            clone, base, head = self._repo_with(path)
            for user in _SEATS:
                with self.subTest(user=user, path=path):
                    r = _invoke_hook_with_refs(
                        clone, f"refs/heads/main {head} refs/heads/main {base}\n", user=user)
                    self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
                    self.assertNotIn("BLOCKED", r.stderr)

    def test_every_seat_allowed_to_push_code_to_a_branch(self) -> None:
        clone, base, head = self._repo_with("tools/whatever.py")
        for user in _SEATS:
            with self.subTest(user=user):
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/agent/T-1 {head} refs/heads/agent/T-1 {base}\n",
                    user=user)
                self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_unresolvable_identity_fails_open(self) -> None:
        """`id -un` printing nothing is an anomaly, not an unknown seat: fail
        OPEN like every other anomaly in this hook (a broken guard must not
        brick the fleet's pushes)."""
        clone, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertNotIn("BLOCKED", r.stderr)

    def test_fail_open_cases_unchanged_for_a_new_seat(self) -> None:
        clone, base, head = self._repo_with("tools/whatever.py")
        cases = {
            "empty stdin": "",
            "zero local sha (deletion)": f"(delete) {_ZERO} refs/heads/main {base}\n",
            "zero remote sha (new main)": f"refs/heads/main {head} refs/heads/main {_ZERO}\n",
            "unreadable range": f"refs/heads/main {head} refs/heads/main {'1' * 40}\n",
        }
        for label, refs in cases.items():
            with self.subTest(case=label):
                r = _invoke_hook_with_refs(clone, refs, user="some-future-seat")
                self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
                self.assertNotIn("BLOCKED", r.stderr)


class TestSeatGuardRunsBeforeFreshness(unittest.TestCase):
    """N2: the guard works from the stdin shas and the object store, so it must not
    sit behind the freshness fetch. On a NAMED branch a failed fetch exits 0
    (fail-open) — with the guard after it, an unreachable origin (or any fetch
    failure) silently switched the seat guard off.

    The catch, found by the existing "behind a foreign code commit" tests when the
    guard was first hoisted: git hands the hook the REMOTE's real tip, which a
    clone that is behind has not fetched, so the range diff was unreadable
    (fail-open) — the guard now fetches for itself when, and only when, that tip is
    not local."""

    def _diverged(self, own_relpath: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        staging = Path(tmp.name) / "staging"
        (staging / "tools").mkdir()
        _commit_file(staging, "tools/other_writer.py", "foreign code\n", "other writer lands code")
        remote_tip = _git("rev-parse", "HEAD", cwd=staging, check=True).stdout.strip()
        _git("push", "origin", "main", cwd=staging, check=True)
        local = _commit_path(clone, own_relpath)  # remote tip NOT fetched into clone
        return clone, remote_tip, local

    def test_behind_clone_with_a_reachable_remote_still_blocks_its_own_code(self) -> None:
        clone, remote_tip, local = self._diverged("tools/mine.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n",
            user="claude-fallback")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("tools/mine.py", r.stderr)
        self.assertNotIn("tools/other_writer.py", r.stderr)

    def test_the_guards_own_fetch_is_time_capped(self) -> None:
        """N-b: a wedged remote must not hang every seat push behind the guard. A stub
        `timeout` on PATH records its argv and passes through; the guard's fetch of a
        missing remote tip must be wrapped in `timeout 30`."""
        clone, remote_tip, local = self._diverged("tools/mine.py")
        stub_dir = clone / ".idstub"
        stub_dir.mkdir(exist_ok=True)
        log = clone / "timeout.log"
        real = shutil.which("timeout")
        stub = stub_dir / "timeout"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec {real} "$@"\n')
        stub.chmod(0o755)
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n",
            user="claude-fallback")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")  # the fetch still worked
        self.assertIn("30 git fetch", log.read_text())

    def test_unknown_remote_tip_and_a_failed_fetch_is_an_unreadable_range_so_fail_open(self) -> None:
        """The honest residual: with the remote tip absent AND unfetchable there is
        nothing to diff against, so the guard fails open (as for every anomaly) and
        the server-side audit is the backstop."""
        clone, remote_tip, local = self._diverged("tools/mine.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n",
            user="claude-fallback", remote="no-such-remote")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertNotIn("BLOCKED", r.stderr)

    def _repo_with(self, relpath: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        base = _rev(clone)
        head = _commit_path(clone, relpath)
        return clone, base, head

    def test_code_push_to_main_is_blocked_even_when_the_fetch_fails(self) -> None:
        clone, base, head = self._repo_with("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n",
            user="claude-fallback", remote="no-such-remote")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("BLOCKED", r.stderr)

    def test_twin_ticket_push_still_fails_open_on_the_same_failed_fetch(self) -> None:
        """Non-vacuous twin: the fetch really does fail here (WARNING), and a
        non-code push still goes through — the freshness fail-open is unchanged."""
        clone, base, head = self._repo_with("tickets/active/T-9999.md")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n",
            user="claude-fallback", remote="no-such-remote")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertIn("WARNING", r.stderr)


class TestFreshnessSurvivesTheGuardFetch(unittest.TestCase):
    """B1 (review round 2): the T-1961 freshness gate decides "behind -> rebase" versus
    "deliberate rewrite -> skip" by whether the remote tip was ALREADY local
    (`git cat-file -e`). The seat guard's own fetch (added so it can read the range of
    a behind clone) makes the tip local, so every behind SEAT was reclassified as a
    rewrite and never auto-rebased — on retry too, since the tip stayed local. Every
    other freshness test ran with empty stdin or the HO identity, so nothing saw it.
    The classification is now computed from the stdin shas BEFORE the guard fetches."""

    def _behind(self, *, fetched: bool):
        """Seat clone: local main = 1 ticket commit on the base; origin main = the base
        + a newer FOREIGN ticket commit (unfetched unless `fetched`)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        staging = Path(tmp.name) / "staging"
        (staging / "tickets" / "active").mkdir(parents=True)
        _commit_file(staging, "tickets/active/T-8888.md", "foreign\n", "foreign ticket commit")
        remote_tip = _git("rev-parse", "HEAD", cwd=staging, check=True).stdout.strip()
        _git("push", "origin", "main", cwd=staging, check=True)
        local = _commit_path(clone, "tickets/active/T-9999.md")
        if fetched:
            _git("fetch", "origin", cwd=clone, check=True)
        return clone, remote_tip, local

    def _behind_count(self, clone: Path) -> int:
        return int(_git("rev-list", "--count", "HEAD..origin/main", cwd=clone,
                        check=True).stdout.strip())

    def test_a_behind_seat_is_still_auto_rebased_when_the_tip_is_unfetched(self) -> None:
        for user in ("codex", "claude-fallback", "suborch1"):
            with self.subTest(user=user):
                clone, remote_tip, local = self._behind(fetched=False)
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n", user=user)
                self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
                self.assertIn("rebasing local onto", r.stderr)
                self.assertNotIn("NOT auto-rebasing", r.stderr)
                self.assertEqual(self._behind_count(clone), 0, "seat is still behind origin")
                self.assertIn("foreign ticket commit",
                              _git("log", "--oneline", cwd=clone, check=True).stdout)

    def test_the_ho_identity_is_unchanged_control(self) -> None:
        clone, remote_tip, local = self._behind(fetched=False)
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n", user="claude")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertIn("rebasing local onto", r.stderr)
        self.assertEqual(self._behind_count(clone), 0)

    def test_tip_already_fetched_keeps_todays_behaviour_pinned(self) -> None:
        """PINNED, not endorsed: a clone that has ALREADY fetched the newer tip has it
        local and not an ancestor, which T-1961 classifies as a deliberate rewrite —
        NOT auto-rebased. That is the hook's behaviour before T-4226 and must not
        change as a side effect of this ticket (identical for a seat and for HO)."""
        for user in ("claude-fallback", "claude"):
            with self.subTest(user=user):
                clone, remote_tip, local = self._behind(fetched=True)
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n", user=user)
                self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
                self.assertIn("deliberate history rewrite", r.stderr)
                self.assertNotIn("rebasing local onto", r.stderr)
                self.assertEqual(self._behind_count(clone), 1)

    def test_a_seat_code_push_from_a_behind_clone_is_still_blocked(self) -> None:
        """The guard's fetch is what lets it read a behind clone's range; moving the
        classification earlier must not cost the guard that."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        staging = Path(tmp.name) / "staging"
        (staging / "tickets" / "active").mkdir(parents=True)
        _commit_file(staging, "tickets/active/T-8888.md", "foreign\n", "foreign ticket commit")
        remote_tip = _git("rev-parse", "HEAD", cwd=staging, check=True).stdout.strip()
        _git("push", "origin", "main", cwd=staging, check=True)
        local = _commit_path(clone, "tools/mine.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {local} refs/heads/main {remote_tip}\n", user="codex")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("tools/mine.py", r.stderr)


class TestSeatGuardOnDetachedHead(unittest.TestCase):
    """T-4226: the seat guard keys on the REMOTE ref, not on the local branch, so
    a detached HEAD must not switch it off (`git checkout --detach; git push
    origin HEAD:main` used to walk straight past it). Freshness, the contract
    gate and the warnings stay skipped on detached HEAD."""

    def _detached(self, relpath: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        base = _rev(clone)
        head = _commit_path(clone, relpath)
        _git("checkout", "--detach", head, cwd=clone, check=True)
        return clone, base, head

    def test_seat_code_push_to_main_blocked_when_detached(self) -> None:
        clone, base, head = self._detached("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"HEAD {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("BLOCKED", r.stderr)
        self.assertIn("tools/whatever.py", r.stderr)

    def test_seat_ticket_push_to_main_allowed_when_detached(self) -> None:
        clone, base, head = self._detached("tickets/active/T-9999.md")
        r = _invoke_hook_with_refs(
            clone, f"HEAD {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_ho_code_push_to_main_allowed_when_detached(self) -> None:
        clone, base, head = self._detached("tools/whatever.py")
        r = _invoke_hook_with_refs(
            clone, f"HEAD {head} refs/heads/main {base}\n", user="claude")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")

    def test_detached_push_still_skips_freshness_and_contract_gate(self) -> None:
        """Only the seat guard runs on detached HEAD. A bare-unit cron in the tree
        (a contract VIOLATION) must NOT block a detached ticket push, and no
        fetch may be attempted (unreachable remote -> no WARNING)."""
        clone, base, head = self._detached("tickets/active/T-9999.md")
        _seed_linter_repo(clone, execstart_tail="")  # would be a VIOLATION on a branch
        r = _invoke_hook_with_refs(
            clone, f"HEAD {head} refs/heads/main {base}\n", user="claude-fallback",
            url="file:///nonexistent_path_detached_gate_test")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertNotIn("VIOLATION", r.stderr)
        self.assertNotIn("WARNING", r.stderr)


class TestSpineGroomExemption(unittest.TestCase):
    """The spine groom (Recovery's timer) rewrites the generated sentinel region of
    the tools spine README and pushes it to main from a detached worktree. That
    ONE path is exempt from the seat guard; nothing else under tools/ is."""

    def _repo_with(self, *relpaths: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        origin, clone = _make_origin_and_clone(Path(tmp.name))
        base = _rev(clone)
        head = base
        for rel in relpaths:
            head = _commit_path(clone, rel)
        return clone, base, head

    def test_seat_may_push_the_tools_spine_readme_alone(self) -> None:
        clone, base, head = self._repo_with("tools/README.md")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertNotIn("BLOCKED", r.stderr)

    def test_readme_alongside_code_still_blocks_and_names_only_the_code(self) -> None:
        clone, base, head = self._repo_with("tools/README.md", "tools/x.py")
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")
        self.assertIn("tools/x.py", r.stderr)
        self.assertNotIn("tools/README.md", r.stderr)

    def test_exemption_is_exact_path_not_a_prefix_or_a_suffix(self) -> None:
        for path in ("tools/systemd/README.md", "tools/sub/README.md",
                     "tools/README.md.py", "tools/README.mdx"):
            with self.subTest(path=path):
                clone, base, head = self._repo_with(path)
                r = _invoke_hook_with_refs(
                    clone, f"refs/heads/main {head} refs/heads/main {base}\n",
                    user="claude-fallback")
                self.assertEqual(r.returncode, 1, f"stderr={r.stderr!r}")

    def test_spine_groom_shape_detached_readme_push_passes(self) -> None:
        """The real shape: detached worktree, HEAD:main, the README only."""
        clone, base, head = self._repo_with("tools/README.md")
        _git("checkout", "--detach", head, cwd=clone, check=True)
        r = _invoke_hook_with_refs(
            clone, f"HEAD {head} refs/heads/main {base}\n", user="claude-fallback")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")


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

    def test_mixed_push_including_current_branch_still_rebases(self) -> None:
        """T-4374: a mixed push where one line DOES target the current branch
        keeps per-line classification — not "otherref"; freshness runs."""
        staging = self.tmp / "staging2"
        _git("clone", str(self.origin), str(staging), cwd=self.tmp, check=True)
        _commit_file(staging, "other.txt", "remote\n", "remote-side")
        _git("push", "origin", "main", cwd=staging, check=True)
        _commit_file(self.clone, "mine.txt", "local\n", "local-side")
        loid = _rev(self.clone, "HEAD")
        refs = (f"refs/heads/feature {loid} refs/heads/feature {_ZERO}\n"
                f"refs/heads/main {loid} refs/heads/main {self.base}\n")
        r = _invoke_hook_with_refs(self.clone, refs)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("no pushed ref targets the current branch", r.stderr)
        self.assertIn("rebasing", r.stderr)

    def _order_fixture(self, case: str) -> tuple[Path, str, str]:
        """Fresh repo per call (freshness mutates it). Returns (clone, line,
        base) where `line` is the non-delete ref line for `case`."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tdir = Path(tmp.name)
        origin, clone = _make_origin_and_clone(tdir)
        base = _rev(clone, "origin/main")
        if case == "extends":
            # origin advances (other writer); local extends its known remote.
            w = tdir / "writer"
            _git("clone", str(origin), str(w), cwd=tdir, check=True)
            _commit_file(w, "other.txt", "remote\n", "remote-side")
            _git("push", "origin", "main", cwd=w, check=True)
            _commit_file(clone, "mine.txt", "local\n", "local-side")
            return clone, f"refs/heads/main {_rev(clone)} refs/heads/main {base}", base
        if case == "rewrite":
            _commit_file(clone, "f.txt", "v1\n", "A")
            _git("push", "origin", "main", cwd=clone, check=True)
            old = _rev(clone)
            _git("reset", "--hard", base, cwd=clone, check=True)
            _commit_file(clone, "f.txt", "v2\n", "A-prime")
            return clone, f"refs/heads/main {_rev(clone)} refs/heads/main {old}", base
        if case == "newbranch":
            _git("checkout", "-b", "brand-new", cwd=clone, check=True)
            _commit_file(clone, "n.txt", "new\n", "new")
            return clone, f"refs/heads/brand-new {_rev(clone)} refs/heads/brand-new {_ZERO}", base
        if case == "other":
            _git("checkout", "-b", "feature", cwd=clone, check=True)
            _commit_file(clone, "feat.md", "v1\n", "feat-1")
            loid = _rev(clone)
            _git("checkout", "main", cwd=clone, check=True)
            return clone, f"refs/heads/feature {loid} refs/heads/feature {_ZERO}", base
        raise AssertionError(case)

    def test_classification_is_independent_of_ref_line_order(self) -> None:
        """T-4374: a delete line alongside a non-delete line must not change the
        outcome, whichever order git lists them in."""
        markers = {
            "delete": "ref deletion",
            "newbranch": "new remote branch",
            "rewrite": "deliberate history rewrite",
            "otherref": "no pushed ref targets the current branch",
        }
        expected = {"extends": "freshness", "rewrite": "rewrite",
                    "newbranch": "newbranch", "other": "otherref"}
        for case, want in expected.items():
            for order in ("line-first", "delete-first"):
                with self.subTest(case=case, order=order):
                    clone, line, base = self._order_fixture(case)
                    dele = f"(delete) {_ZERO} refs/heads/old {base}"
                    pair = [line, dele] if order == "line-first" else [dele, line]
                    r = _invoke_hook_with_refs(clone, "\n".join(pair) + "\n")
                    self.assertEqual(r.returncode, 0, r.stderr)
                    for reason, msg in markers.items():
                        if reason == want:
                            self.assertIn(msg, r.stderr)
                        else:
                            self.assertNotIn(msg, r.stderr)
                    if want == "freshness":
                        self.assertIn("rebasing local onto", r.stderr)
                    else:
                        self.assertNotIn("rebasing local onto", r.stderr)

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


class TestFreshnessRealPush(unittest.TestCase):
    """T-4290: drive the hook through REAL `git push` to a temp bare remote,
    so git itself builds the hook's stdin (the ref-fed tests above inject it).

    Why this matters: on a NON-force push git drops a rejected non-fast-forward
    ref from the pre-push stdin, so a behind clone — even one that already
    fetched the newer tip — reaches the hook with EMPTY input and self-heals
    via the freshness rebase. The stdin-driven "rewrite" classification is
    therefore reachable only by force pushes, where it must hold even for a
    clone that never held the commits it rewrites away.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.origin, self.clone = _make_origin_and_clone(self.tmp)
        # Install the hook under test (read at call time, so a driver can
        # swap PRE_PUSH_HOOK for a non-vacuity comparison).
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        shutil.copy(PRE_PUSH_HOOK, hooks / "pre-push")
        (hooks / "pre-push").chmod(0o755)
        _git("config", "core.hooksPath", str(hooks), cwd=self.clone, check=True)

    def _writer(self, name: str, branch: str = "main") -> Path:
        w = self.tmp / f"writer-{name}"
        _git("clone", "-b", branch, str(self.origin), str(w), cwd=self.tmp, check=True)
        return w

    def _subjects(self, ref: str, repo: Path) -> list[str]:
        return _git("log", "--format=%s", ref, cwd=repo, check=True).stdout.split()

    def test_behind_already_fetched_non_force_push_self_heals(self) -> None:
        w = self._writer("other")
        _commit_file(w, "remote.md", "remote\n", "remote-newer")
        _git("push", "origin", "main", cwd=w, check=True)
        roid = _rev(w, "HEAD")

        _commit_file(self.clone, "ticket.md", "mine\n", "local-ticket")
        _git("fetch", "origin", cwd=self.clone, check=True)
        self.assertEqual(_git("cat-file", "-e", roid, cwd=self.clone).returncode, 0,
                         "fixture invalid: newer tip must already be fetched")
        self.assertNotEqual(
            _git("merge-base", "--is-ancestor", roid, "HEAD", cwd=self.clone).returncode, 0,
            "fixture invalid: clone must be behind the fetched tip")

        first = _git("push", "origin", "main", cwd=self.clone)
        self.assertNotEqual(first.returncode, 0,
                            "git fixes refs pre-hook, so the first push is rejected")
        self.assertIn("rebasing local onto", first.stderr)
        self.assertNotIn("deliberate history rewrite", first.stderr)
        # The clone self-healed: rebased onto the remote tip, local commit on top.
        self.assertEqual(_rev(self.clone, "HEAD~1"), roid)
        self.assertEqual(self._subjects("-1", self.clone), ["local-ticket"])

        second = _git("push", "origin", "main", cwd=self.clone)
        self.assertEqual(second.returncode, 0, second.stderr)
        remote_log = _git("log", "--format=%s", "main", cwd=self.origin, check=True).stdout
        self.assertIn("remote-newer", remote_log)
        self.assertIn("local-ticket", remote_log)

    def test_force_push_rewrite_of_never_held_branch_is_not_rebased_back(self) -> None:
        """Cross-clone rewrite: a writer pushed feature@A; this clone fetches
        it, rebuilds the branch from main with a rewritten A' (never holding A
        on its local branch) and force-pushes. Must stay a rewrite: no rebase
        back onto A, force lands, local tip untouched."""
        w = self._writer("feat")
        _git("checkout", "-b", "feature", cwd=w, check=True)
        _commit_file(w, "f.txt", "v1\n", "A")
        _git("push", "-u", "origin", "feature", cwd=w, check=True)
        old = _rev(w, "HEAD")

        _git("fetch", "origin", cwd=self.clone, check=True)
        _git("checkout", "-b", "feature", "origin/main", cwd=self.clone, check=True)
        _commit_file(self.clone, "f.txt", "v2\n", "A-prime")
        new = _rev(self.clone, "HEAD")
        reflog = _git("reflog", "show", "--format=%H", "refs/heads/feature",
                      cwd=self.clone, check=True).stdout.split()
        self.assertNotIn(old, reflog, "fixture invalid: clone must never have held A")

        r = _git("push", "--force", "origin", "feature", cwd=self.clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("deliberate history rewrite", r.stderr)
        self.assertNotIn("rebasing local onto", r.stderr)
        self.assertEqual(_rev(self.clone, "HEAD"), new)
        self.assertEqual(_rev(self.origin, "feature"), new)

    # -- T-4374: pushing a ref other than the current branch -------------

    def _behind_main_with_feature(self) -> tuple[str, str, str]:
        """Clone on main: an existing remote `feature` with a new local commit,
        main behind origin (another writer landed) AND carrying a local commit.
        Returns (main_tip, feature_tip, remote_main_tip)."""
        _git("checkout", "-b", "feature", cwd=self.clone, check=True)
        _commit_file(self.clone, "feat.md", "v1\n", "feat-1")
        _git("push", "-u", "origin", "feature", cwd=self.clone, check=True)
        _commit_file(self.clone, "feat.md", "v2\n", "feat-2")
        feature_tip = _rev(self.clone, "HEAD")
        _git("checkout", "main", cwd=self.clone, check=True)

        w = self._writer("other")
        _commit_file(w, "remote.md", "remote\n", "remote-newer")
        _git("push", "origin", "main", cwd=w, check=True)
        roid = _rev(w, "HEAD")

        _commit_file(self.clone, "ticket.md", "mine\n", "local-ticket")
        _git("fetch", "origin", cwd=self.clone, check=True)
        self.assertNotEqual(
            _git("merge-base", "--is-ancestor", roid, "HEAD", cwd=self.clone).returncode, 0,
            "fixture invalid: main must be behind the remote tip")
        return _rev(self.clone, "HEAD"), feature_tip, roid

    def test_pushing_other_branch_does_not_rebase_behind_current(self) -> None:
        """(1) On a behind main with a local commit, `git push origin feature`
        lands feature and leaves main's tip untouched (no unrequested rebase)."""
        main_tip, feature_tip, _ = self._behind_main_with_feature()

        r = _git("push", "origin", "feature", cwd=self.clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no pushed ref targets the current branch", r.stderr)
        self.assertNotIn("rebasing local onto", r.stderr)
        self.assertEqual(_rev(self.clone, "main"), main_tip, "main must not be rebased")
        self.assertEqual(_rev(self.origin, "feature"), feature_tip)

    def test_multi_ref_behind_push_needs_retry_to_heal_current(self) -> None:
        """(3) Accepted trade-off pin (T-4366 shape): `git push origin main
        feature` from a behind main — git drops main's rejected line, the hook
        sees only feature, so feature lands and main is rejected and NOT
        rebased. A plain `git push origin main` retry then self-heals."""
        main_tip, feature_tip, roid = self._behind_main_with_feature()

        multi = _git("push", "origin", "main", "feature", cwd=self.clone)
        self.assertNotEqual(multi.returncode, 0, "main must be rejected (behind)")
        self.assertIn("no pushed ref targets the current branch", multi.stderr)
        self.assertNotIn("rebasing local onto", multi.stderr)
        self.assertEqual(_rev(self.origin, "feature"), feature_tip)
        self.assertEqual(_rev(self.origin, "main"), roid)
        self.assertEqual(_rev(self.clone, "main"), main_tip, "main must not be rebased")

        # Retry of the current branch alone: empty stdin → freshness rebases.
        # As in the T-4290 single-branch test: git fixes the ref status before
        # the hook runs, so this push is still rejected, but the hook heals
        # the clone and the next push lands.
        retry = _git("push", "origin", "main", cwd=self.clone)
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn("rebasing local onto", retry.stderr)
        self.assertEqual(_rev(self.clone, "HEAD~1"), roid)
        self.assertEqual(self._subjects("-1", self.clone), ["local-ticket"])
        again = _git("push", "origin", "main", cwd=self.clone)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(_rev(self.origin, "main"), _rev(self.clone, "main"))
        remote_log = _git("log", "--format=%s", "main", cwd=self.origin, check=True).stdout
        self.assertIn("remote-newer", remote_log)
        self.assertIn("local-ticket", remote_log)

    def test_pushing_other_branch_when_current_up_to_date_is_noop(self) -> None:
        """(4) Main NOT behind: pushing another branch leaves main untouched."""
        _commit_file(self.clone, "ticket.md", "mine\n", "local-ticket")
        main_tip = _rev(self.clone, "HEAD")
        _git("checkout", "-b", "feature", cwd=self.clone, check=True)
        _commit_file(self.clone, "feat.md", "v1\n", "feat-1")
        _git("push", "-u", "origin", "feature", cwd=self.clone, check=True)
        _commit_file(self.clone, "feat.md", "v2\n", "feat-2")
        feature_tip = _rev(self.clone, "HEAD")
        _git("checkout", "main", cwd=self.clone, check=True)

        r = _git("push", "origin", "feature", cwd=self.clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("rebasing local onto", r.stderr)
        self.assertEqual(_rev(self.clone, "main"), main_tip)
        self.assertEqual(_rev(self.origin, "feature"), feature_tip)


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
    """Copy the real linter + its dependencies into the clone so the hook's
    `${REPO_ROOT}/tools/ledger_placement_lint.py` resolves: ledger_shard, and
    (T-4202) the shared diff framing hermes_diff_scan with its _git_utils."""
    tools = clone / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_LEDGER_LINT_SRC, tools / "ledger_placement_lint.py")
    shutil.copy2(_LEDGER_SHARD_SRC, tools / "ledger_shard.py")
    for dep in ("__init__.py", "hermes_diff_scan.py", "_git_utils.py"):
        shutil.copy2(REPO_ROOT / "tools" / dep, tools / dep)


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
            clone, "2026-W30", {"id": "spill-1", "ts": _TS_W31, "agent": "mason"})
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
            clone, "2026-W30", {"id": "ok-1", "ts": _TS_W30, "agent": "mason"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("wrong ISO-week shard", r.stderr)

    def test_fail_open_when_linter_absent(self) -> None:
        # linter NOT seeded -> gate no-ops, push proceeds, no warn text
        clone, base = self._setup()
        head = _commit_ledger_line(
            clone, "2026-W30", {"id": "spill-2", "ts": _TS_W31, "agent": "mason"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/main {head} refs/heads/main {base}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("wrong ISO-week shard", r.stderr)

    def test_does_not_fire_on_non_main_ref(self) -> None:
        # a branch push must not trigger the main-only ledger warn
        clone, base = self._setup()
        _seed_ledger_lint(clone)
        head = _commit_ledger_line(
            clone, "2026-W30", {"id": "spill-3", "ts": _TS_W31, "agent": "mason"})
        r = _invoke_hook_with_refs(
            clone, f"refs/heads/agent/T-1 {head} refs/heads/agent/T-1 {base}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("wrong ISO-week shard", r.stderr)


# ---------------------------------------------------------------------------
# T-5160: a stale same-named remote agent branch must not be rebased onto.
# ---------------------------------------------------------------------------


class TestStaleSameNamedAgentBranch(unittest.TestCase):
    """origin already holds an older, unrelated agent/<ticket> tip. The freshness
    step used to `pull --rebase` the fresh local branch onto it, replaying every
    main commit since the stale tip (158 picks on 2026-10-18) and stopping
    mid-rebase. It must refuse instead and leave the worktree untouched."""

    BRANCH = "agent/T-9999"

    def _setup(self, tmpdir: Path) -> tuple[Path, Path, Path]:
        origin, clone = _make_origin_and_clone(tmpdir)
        other = tmpdir / "other"
        _git("clone", str(origin), str(other), cwd=tmpdir, check=True)
        return origin, clone, other

    def test_refuses_when_the_remote_agent_branch_is_not_an_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone, other = self._setup(tmpdir)
            # The stale attempt: an old agent branch with its own commit.
            _git("checkout", "-b", self.BRANCH, cwd=other, check=True)
            _commit_file(other, "stale.txt", "stale attempt\n", "stale attempt")
            _git("push", "origin", self.BRANCH, cwd=other, check=True)
            # main moves on.
            _git("checkout", "main", cwd=other, check=True)
            for i in range(3):
                _commit_file(other, f"main_{i}.txt", f"{i}\n", f"main commit {i}")
            _git("push", "origin", "main", cwd=other, check=True)
            # The fresh attempt: same branch name, cut from current main.
            _git("fetch", "origin", cwd=clone, check=True)
            _git("checkout", "-b", self.BRANCH, "origin/main", cwd=clone, check=True)
            _commit_file(clone, "fresh.txt", "fresh attempt\n", "fresh attempt")
            head_before = _git("rev-parse", "HEAD", cwd=clone, check=True).stdout.strip()

            result = _invoke_hook(clone, "origin", str(origin))

            head_after = _git("rev-parse", "HEAD", cwd=clone, check=True).stdout.strip()
            rebase_dirs = [(clone / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply")]
            branch_after = _git("symbolic-ref", "--short", "HEAD", cwd=clone).stdout.strip()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not an ancestor", result.stderr)
        self.assertNotIn("rebasing local onto", result.stderr)
        self.assertEqual(head_after, head_before)
        self.assertEqual(branch_after, self.BRANCH)
        self.assertEqual(rebase_dirs, [False, False])

    def test_still_fast_forwards_an_agent_branch_that_is_only_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            origin, clone, other = self._setup(tmpdir)
            _git("checkout", "-b", self.BRANCH, cwd=clone, check=True)
            _commit_file(clone, "one.txt", "1\n", "own commit one")
            _git("push", "origin", self.BRANCH, cwd=clone, check=True)
            # The same branch advanced elsewhere (e.g. pushed from another worktree).
            _git("fetch", "origin", cwd=other, check=True)
            _git("checkout", "-b", self.BRANCH, f"origin/{self.BRANCH}", cwd=other, check=True)
            _commit_file(other, "two.txt", "2\n", "own commit two")
            _git("push", "origin", self.BRANCH, cwd=other, check=True)
            _git("branch", "--set-upstream-to", f"origin/{self.BRANCH}", cwd=clone, check=True)

            result = _invoke_hook(clone, "origin", str(origin))
            log = _git("log", "--oneline", cwd=clone).stdout

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("own commit two", log)


if __name__ == "__main__":
    unittest.main()
