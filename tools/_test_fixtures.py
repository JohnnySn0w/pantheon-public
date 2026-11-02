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
import time
import warnings
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

    # None = the snapshot could not be taken (git failed or timed out). The
    # guard is then UNKNOWN, not empty; see assert_real_repo_untouched (T-4108).
    local_only: frozenset[str] | None
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


# T-4108: the guard's own snapshots need a failure signal that local_only_commits
# (lenient by contract: frozenset() on any failure) can't give. A timed-out
# setUp snapshot used to become an EMPTY baseline. Then a successful tearDown
# read reported every pre-existing local-only commit as "new". On a CI
# pull_request checkout, HEAD is GitHub's refs/pull/N/merge commit, which is
# local-only by construction, so under runner IO contention this produced
# false "leaked commit" failures (PRs #495/#497/#501, 2026-09-28).
_GUARD_GIT_TIMEOUT_S = 60

# T-5817: on a LIVE seat checkout another actor (the seat's own ticket.py, a
# timer) commits to REPO_ROOT and pushes it moments later. Between the two the
# commit is local-only, and a test tearing down in that window reported it as a
# leak (orchestrator's soak, 2026-11-01: `tickets: clear stale auto_delivered on
# T-4514` by orchestrator, failing TestRetargetDetachedHeadPush). A test's leaked
# commit is never pushed, so before failing the guard waits up to this long for
# the new SHAs to become reachable from origin/* and re-reads. It only costs
# time when new local-only commits were seen; a clean tearDown never waits.
REAL_REPO_SETTLE_S = 120.0
_REAL_REPO_SETTLE_POLL_S = 2.0


def _local_only_commits_or_none(repo_path: Path) -> frozenset[str] | None:
    """Like local_only_commits, but None (unknown) on any git failure or
    timeout instead of an empty set, so the guard can tell 'no local commits'
    from 'could not look'."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "log",
             "HEAD", "--not", "--remotes=origin",
             "--format=%H"],
            capture_output=True, text=True, check=False,
            timeout=_GUARD_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return frozenset(line.strip() for line in r.stdout.splitlines() if line.strip())


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
        local_only=_local_only_commits_or_none(resolved),
        repo_path=resolved,
    )


def assert_real_repo_untouched(
    snapshot: _RepoSnapshot,
    repo_path: Path | None = None,
    *,
    settle_s: float | None = None,
) -> None:
    """Raise AssertionError if a NEW local-only commit appeared in the real
    repo since the snapshot was taken.

    External advancement (``git pull``, ``origin/main`` advancing due to
    another actor's push) does **not** trigger this guard: such commits are
    reachable from ``origin/*`` refs and therefore not local-only.

    A script-under-test that erroneously commits to ``repo_path`` **will**
    trigger this guard: the erroneous commit is local-only until pushed, and
    its SHA will appear in the tearDown set but not in the setUp set.

    Concurrent actors (T-5817): another actor (e.g. the seat's own
    ticket.py) can create a local commit in REPO_ROOT between setUp and
    tearDown and push it moments later; until then it is local-only. So new
    local-only SHAs are re-read for up to ``settle_s`` seconds (default
    ``REAL_REPO_SETTLE_S``) and only the ones still local-only fail. A test's
    leaked commit is never pushed, so it still fails, after the wait.

    Args:
        snapshot:  the ``_RepoSnapshot`` returned by ``snapshot_real_repo()``.
        repo_path: path to the real repo.  Defaults to ``snapshot.repo_path``
                   (which was set by ``snapshot_real_repo()``).  Override only
                   in tests that need to redirect the assertion to a different
                   repo than the one snapshotted.
        settle_s:  how long new local-only SHAs may take to reach origin/*
                   before they count as leaked (T-5817). None means
                   ``REAL_REPO_SETTLE_S``.

    Raises:
        AssertionError: if new local-only commits appeared since the snapshot.
    """
    resolved = repo_path if repo_path is not None else snapshot.repo_path
    current = _local_only_commits_or_none(resolved)
    if snapshot.local_only is None or current is None:
        # UNKNOWN on either side (git failed or timed out): skip rather than
        # diff against a fake-empty set, which is the T-4108 false positive.
        # Lenient in the same direction local_only_commits already documents.
        warnings.warn(
            f"assert_real_repo_untouched: could not read local-only commits in "
            f"{resolved} ({'setUp' if snapshot.local_only is None else 'tearDown'} "
            f"snapshot failed); guard skipped for this test (T-4108).",
            RuntimeWarning, stacklevel=2,
        )
        return
    new_local = current - snapshot.local_only
    if new_local:
        new_local = _settle_new_local(resolved, snapshot.local_only, new_local,
                                      REAL_REPO_SETTLE_S if settle_s is None else settle_s)
    if new_local:
        raise AssertionError(
            f"Real repo at {resolved} gained new local-only commits during "
            f"the test (not reachable from any origin/* ref). "
            f"Tests must not commit to the real repo. "
            f"New local-only SHAs: {sorted(new_local)}"
            f"{_describe_commits(resolved, sorted(new_local))}"
        )


def _settle_new_local(repo_path: Path, baseline: frozenset[str], new_local: frozenset[str],
                      settle_s: float) -> frozenset[str]:
    """T-5817: re-read until none of `new_local` is still local-only, or `settle_s`
    runs out; return the SHAs that stayed local-only (from the last good read).
    A read that fails mid-wait keeps the previous answer (fail toward reporting)."""
    deadline = time.monotonic() + max(0.0, settle_s)
    while new_local and time.monotonic() < deadline:
        time.sleep(min(_REAL_REPO_SETTLE_POLL_S, max(0.0, deadline - time.monotonic())))
        current = _local_only_commits_or_none(repo_path)
        if current is not None:
            new_local = (current - baseline) & new_local
    return new_local


def _describe_commits(repo_path: Path, shas: list[str], max_files: int = 8) -> str:
    """Best-effort forensic detail for leaked SHAs: author, date, subject and the
    first changed paths of each (T-4108).

    Under pytest-xdist the tearDown guard that fires is often NOT the test that
    committed; a concurrent test in another worker is. And the leaked commit
    exists only in that checkout (often a CI runner workspace, unrecoverable
    afterwards). The commit's own subject and paths are the only reliable
    pointer to the culprit, so they go into the assertion message. Fail-safe:
    any error degrades to a note and never masks the guard's AssertionError.
    """
    lines: list[str] = []
    for sha in shas:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_path), "show", "--no-patch",
                 "--format=%h | %an <%ae> | %aI | %s", sha],
                capture_output=True, text=True, timeout=10,
            )
            files = subprocess.run(
                ["git", "-C", str(repo_path), "show", "--name-only",
                 "--format=", sha],
                capture_output=True, text=True, timeout=10,
            )
            head = out.stdout.strip() or f"{sha[:10]} | (no detail: {out.stderr.strip()[:120]})"
            paths = [f for f in files.stdout.splitlines() if f.strip()]
            shown = ", ".join(paths[:max_files])
            more = f" (+{len(paths) - max_files} more)" if len(paths) > max_files else ""
            lines.append(f"  - {head}\n      files: {shown or '(none)'}{more}")
        except Exception as exc:  # noqa: BLE001 - forensic helper must never raise
            lines.append(f"  - {sha[:10]} | (could not describe: {exc!r})")
    return "\nLeaked commit detail (T-4108):\n" + "\n".join(lines) if lines else ""


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
        # T-4167: the spawned script's sync helpers resolve the REAL checkout; never
        # let a Layer-3 run fetch + fast-forward it (see _git_utils._real_repo_sync_blocked).
        "PANTHEON_TEST_NO_REAL_REPO_SYNC": "1",
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
# Fake `git mv` containment (T-4167)
# ---------------------------------------------------------------------------


def contained_path(root: Path, arg: str) -> Path:
    """``root / arg`` for a fake-git stub, refusing any path that escapes ``root``.

    pathlib DISCARDS ``root`` when ``arg`` is absolute (and ``..`` climbs out of
    it), so a stub doing ``shutil.move(root / argv[2], root / argv[3])`` would move
    a REAL file in the shared checkout -- the CI flake behind T-4165 moved
    tickets/active/T-4137.md mid-run. Raising names the offending argv in the
    failing test instead of touching the live tree.
    """
    candidate = Path(root) / arg
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(candidate)]) != real_root:
        raise AssertionError(
            f"fake git stub refused a path outside the test root: {arg!r} "
            f"(root {root}) -- it would touch the real checkout (T-4167)"
        )
    return candidate


class TicketTreeWatch:
    """Cheap per-test drift check on a REAL ticket tree + checkout (T-4167 CI tripwire).

    ``stamp()`` is only the mtimes of the tree root and its immediate subdirs, which
    change whenever an entry is added, removed or renamed, plus (given ``git_dir``)
    the checkout's fetch/ref markers, so the common no-drift case costs a handful of
    stats. The full listing is rebuilt only when the stamp moves, and it is cached per
    stamp, so ``begin()`` stays cheap across tests.

    The git markers exist because the T-4165 flake was most likely a REAL ``git pull``
    in CI's checkout: T-4137 moved active->done exactly as origin's resolve did,
    mid-job. That only shows in the tree when origin happens to change tickets/
    during the run; FETCH_HEAD (any fetch/pull), ORIG_HEAD (merge/rebase/reset),
    HEAD, packed-refs and the loose branch ref catch it on every run.
    """

    GIT_MARKERS = ("FETCH_HEAD", "ORIG_HEAD", "HEAD", "packed-refs")

    def __init__(self, tickets_root: Path, git_dir: Path | None = None) -> None:
        self.root = Path(tickets_root)
        self.git_dir = Path(git_dir) if git_dir is not None else None
        self._cache: tuple[tuple, frozenset[str]] | None = None

    def _git_stamp(self) -> tuple:
        if self.git_dir is None or not self.git_dir.is_dir():
            return ()
        names = list(self.GIT_MARKERS)
        try:
            head = (self.git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            head = ""
        if head.startswith("ref: "):
            names.append(head[len("ref: "):])
        out = []
        for name in names:
            try:
                out.append((name, (self.git_dir / name).stat().st_mtime_ns))
            except OSError:
                out.append((name, None))
        return tuple(out)

    def stamp(self) -> tuple:
        dirs = [self.root, *sorted(p for p in self.root.iterdir() if p.is_dir())]
        return tuple((d.name, d.stat().st_mtime_ns) for d in dirs), self._git_stamp()

    def _listing(self, stamp: tuple) -> frozenset[str]:
        if self._cache is None or self._cache[0] != stamp[0]:
            entries = set()
            for child in self.root.iterdir():
                entries.add(child.name)
                if child.is_dir():
                    entries.update(f"{child.name}/{e.name}" for e in os.scandir(child))
            self._cache = (stamp[0], frozenset(entries))
        return self._cache[1]

    def begin(self) -> tuple[tuple, frozenset[str]]:
        stamp = self.stamp()
        return stamp, self._listing(stamp)

    def drift(
        self, token: tuple[tuple, frozenset[str]]
    ) -> tuple[list[str], list[str], list[str]] | None:
        """None when untouched, else (removed, added, git_markers_changed) since ``begin()``.

        removed/added can both be empty (an entry created then removed still bumps
        the mtime); that is still a write into the real tree.
        """
        stamp, before = token
        now = self.stamp()
        if now == stamp:
            return None
        after = self._listing(now)
        git_changed = sorted(
            {name for name, _ in set(stamp[1]) ^ set(now[1])}
        )
        return sorted(before - after), sorted(after - before), git_changed


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
