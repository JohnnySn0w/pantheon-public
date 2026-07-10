"""Shared integration-test fixtures for Layer-3 testing of auto-commit cron
scripts. Per T-0259.

Layer-3 = end-to-end same-machine with isolated tmp resources. The contract
is: spawn the real script in a subprocess against a tmp git repo + tmp state
dir; assert the script's effects landed in tmp, the real pantheon-config
repo HEAD didn't move, and no `git push` reached the real `origin`.

Provides:
    IntegrationEnv         — dataclass holding tmp repo + bare origin + state dir paths
    setup_integration_env  — context-manager-friendly setup
    teardown_integration_env
    run_script             — invokes python3 tools/<name>.py as a subprocess
    snapshot_real_repo     — records real repo local-only commit set pre-test
    assert_real_repo_untouched — asserts no new local-only commits appeared
    local_only_commits     — returns the set of SHAs local but not yet at origin

Usage:

    class TestMyScript(unittest.TestCase):
        def setUp(self):
            self.snapshot = snapshot_real_repo()
            self.env = setup_integration_env()

        def tearDown(self):
            teardown_integration_env(self.env)
            assert_real_repo_untouched(self.snapshot)

        def test_safe_default_does_not_commit(self):
            rc, stdout, stderr = run_script("my_script", [], env=self.env.env_overrides)
            self.assertEqual(rc, 0)
            # The tmp repo HEAD also shouldn't move (script defaulted to safe).
            head_after = git_head(self.env.repo_path)
            self.assertEqual(head_after, self.env.baseline_head)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_BASELINE_FILENAME = "BASELINE.md"
_BASELINE_CONTENT = "# integration test baseline\n"


# ---------------------------------------------------------------------------
# Real-repo snapshot — local-only commit guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RepoSnapshot:
    """Opaque snapshot of the real repo's guard state at setUp time.

    Holds the frozenset of local-only commit SHAs (commits reachable from
    HEAD but not from any origin/* ref) at snapshot time.  Opaque to callers;
    passed through setUp → tearDown unchanged.

    The ``repo_path`` field is carried so that ``assert_real_repo_untouched``
    can default to the same path without requiring the caller to repeat it.
    """

    local_only: frozenset[str]
    repo_path: Path


def git_head(repo_path: Path) -> str:
    """Return the current HEAD SHA of `repo_path`, or '' on any failure."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def local_only_commits(repo_path: Path) -> frozenset[str]:
    """Return the set of SHAs reachable from HEAD but not from any origin/* ref.

    These are commits that exist locally but have not yet been pushed to
    origin.  An empty set means HEAD is fully reachable from origin (clean /
    no unpushed local work).

    When no ``origin/*`` remote-tracking refs exist (e.g. a fresh repo where
    no fetch has been run yet), git treats ``--not --remotes=origin`` as
    ``--not <nothing>``, so ALL commits reachable from HEAD are returned.
    This is correct: in a brand-new repo every commit is "local-only."  The
    guard still works because the snapshot captures that full set as the
    baseline; tearDown only fails if *new* SHAs appear after the snapshot,
    regardless of the baseline size.

    Returns ``frozenset()`` on any git failure (safe default: no local
    commits assumed, guard is lenient rather than crashing a test suite).
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "log",
             "HEAD", "--not", "--remotes=origin",
             "--format=%H"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()
    if r.returncode != 0:
        return frozenset()
    shas = {line.strip() for line in r.stdout.splitlines() if line.strip()}
    return frozenset(shas)


def snapshot_real_repo(repo_path: Path | None = None) -> _RepoSnapshot:
    """Record the real repo's local-only commit set before the test runs.

    Captures ``local_only_commits(repo_path)`` at setUp time.  The returned
    ``_RepoSnapshot`` is opaque to callers; pass it to
    ``assert_real_repo_untouched`` in tearDown.

    Args:
        repo_path: path to the real repo to guard.  Defaults to ``REPO_ROOT``
                   (``/home/claude/pantheon-config``).  Injectable so the
                   guard itself can be unit-tested without touching the live
                   repo.
    """
    resolved = repo_path if repo_path is not None else REPO_ROOT
    return _RepoSnapshot(
        local_only=local_only_commits(resolved),
        repo_path=resolved,
    )


def assert_real_repo_untouched(
    snapshot: _RepoSnapshot,
    repo_path: Path | None = None,
) -> None:
    """Raise AssertionError if a NEW local-only commit appeared in the real
    repo since the snapshot was taken.

    External advancement (``git pull``, ``origin/main`` advancing due to
    another actor's push) does **not** trigger this guard: such commits are
    reachable from ``origin/*`` refs and therefore not local-only.

    A script-under-test that erroneously commits to ``repo_path`` **will**
    trigger this guard: the erroneous commit is local-only until pushed, and
    its SHA will appear in the tearDown set but not in the setUp set.

    Residual-race acknowledgment: another actor (e.g. Specialist) can create a
    local commit in REPO_ROOT between setUp and tearDown before pushing it.
    That commit would be transiently local-only during the push window
    (~sub-second).  If that SHA was NOT already in the setUp snapshot, it
    would be a false positive.  This window is orders of magnitude narrower
    than the prior "any HEAD movement" false-positive rate and is the
    accepted tradeoff (ratified in T-1034 brief).

    Args:
        snapshot:  the ``_RepoSnapshot`` returned by ``snapshot_real_repo()``.
        repo_path: path to the real repo.  Defaults to ``snapshot.repo_path``
                   (which was set by ``snapshot_real_repo()``).  Override only
                   in tests that need to redirect the assertion to a different
                   repo than the one snapshotted.

    Raises:
        AssertionError: if new local-only commits appeared since the snapshot.
    """
    resolved = repo_path if repo_path is not None else snapshot.repo_path
    current = local_only_commits(resolved)
    new_local = current - snapshot.local_only
    if new_local:
        raise AssertionError(
            f"Real repo at {resolved} gained new local-only commits during "
            f"the test (not reachable from any origin/* ref). "
            f"Tests must not commit to the real repo. "
            f"New local-only SHAs: {sorted(new_local)}"
        )


# ---------------------------------------------------------------------------
# IntegrationEnv
# ---------------------------------------------------------------------------


@dataclass
class IntegrationEnv:
    """Tmp infrastructure for one integration test.

    Attributes:
      base_dir         — root tempdir; cleaned up on teardown.
      repo_path        — working tree where the script reads/writes/commits.
      bare_origin      — bare repo set as `origin` remote for `repo_path`.
      state_dir        — tmp ~/.local/state/pantheon equivalent (env-pointed).
      baseline_head    — repo_path HEAD after baseline seed; unchanged ←→ no commit.
      env_overrides    — dict of env vars to pass to subprocess.
    """
    base_dir: Path
    repo_path: Path
    bare_origin: Path
    state_dir: Path
    baseline_head: str
    env_overrides: dict[str, str] = field(default_factory=dict)


def setup_integration_env() -> IntegrationEnv:
    """Build tmp working tree + bare origin + state dir for one test.

    Returns an IntegrationEnv. Tests typically call teardown_integration_env
    in tearDown.
    """
    base_dir = Path(tempfile.mkdtemp(prefix="layer3_"))

    # Bare repo as origin.
    bare = base_dir / "origin.git"
    bare.mkdir()
    _run(["git", "init", "--bare"], cwd=bare)

    # Working tree.
    repo = base_dir / "pantheon-config"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], cwd=repo)
    # Identity inside this tmp repo (not global config).
    _run(["git", "config", "user.email", "layer3-test@example"], cwd=repo)
    _run(["git", "config", "user.name", "Layer3 Test"], cwd=repo)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=repo)

    # Baseline commit so the repo isn't empty (some scripts need this).
    (repo / _BASELINE_FILENAME).write_text(_BASELINE_CONTENT, encoding="utf-8")
    _run(["git", "add", _BASELINE_FILENAME], cwd=repo)
    _run(["git", "commit", "-m", "baseline"], cwd=repo)
    _run(["git", "push", "origin", "main"], cwd=repo)
    baseline_head = git_head(repo)

    # State dir (tmp ~/.local/state/pantheon equivalent).
    state = base_dir / "state"
    state.mkdir()

    env_overrides = {
        "PANTHEON_STATE_DIR": str(state),
        # Pin the caller to a KNOWN_ACCOUNTS value so the own-account read
        # branch honors PANTHEON_STATE_DIR. Without this, a CI runner's login
        # name (e.g. "runner") routes reads through the cross-account sudo path
        # and never sees the seeded tmp state dir (T-1386 hermeticity fix).
        "PANTHEON_ACCOUNT": "claude",
    }

    return IntegrationEnv(
        base_dir=base_dir,
        repo_path=repo,
        bare_origin=bare,
        state_dir=state,
        baseline_head=baseline_head,
        env_overrides=env_overrides,
    )


def teardown_integration_env(env: IntegrationEnv) -> None:
    """Remove the tmp base dir."""
    import shutil
    shutil.rmtree(env.base_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Script invocation
# ---------------------------------------------------------------------------


def run_script(
    script_name: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Invoke `python3 tools/<script_name>.py <args>` as a subprocess.

    Args:
        script_name   — bare name without .py (e.g., "session_reset_reconcile")
        args          — list of CLI args
        env           — env overrides; merged on top of os.environ
        cwd           — working dir for the subprocess; defaults to REPO_ROOT
                        (so the script's sys.path injection lands correctly)
        timeout       — subprocess timeout in seconds

    Returns:
        (returncode, stdout, stderr)
    """
    script_path = REPO_ROOT / "tools" / f"{script_name}.py"
    if not script_path.exists():
        raise FileNotFoundError(f"script not found: {script_path}")
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["python3", str(script_path), *args],
        capture_output=True, text=True, check=False,
        env=full_env,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path) -> None:
    """Run a setup command; fail loudly if it errors (setup must succeed)."""
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"setup command failed: {cmd!r}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
