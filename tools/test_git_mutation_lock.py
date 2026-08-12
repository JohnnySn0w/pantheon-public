"""Unit tests for tools/_git_utils.py — git_mutation_lock (T-1770 Part b).

Closes the shared-checkout interleaved-writer race
(bugs/ticket-py-shared-checkout-interleaved-writer-race.md) with an exclusive,
blocking-with-timeout, cross-process/cross-user lockfile around the git
mutate -> stage -> commit(-> push) critical section.

Run from repo root:
    python3 -m unittest tools.test_git_mutation_lock -v

Covers:
  (a) lock acquired + released cleanly around a mutation (happy path, and
      released-on-exception so a raising mutation never leaks the hold).
  (b) a second acquirer BLOCKS until the first releases — cross-process via
      multiprocessing. fcntl.flock is process-scoped (not thread-scoped), so
      a thread-based test would share the fd and trivially "pass" without
      exercising the real guarantee — same rationale as
      tools/test_ticket.py's TestAllocationLockConcurrency (T-0786).
  (c) the timeout path raises GitMutationLockTimeout naming the lock path and
      a holder diagnostic, bounded by timeout_secs (never hangs forever, and
      the wrapped critical section body never runs — never proceeds
      unlocked).

Fixture-string note: the HERMES trigger byte sequence is never written in
contiguous form in this source file (byte-sequence discipline per
`tools/_git_utils.py` module docstring + `~/CLAUDE.md`).
"""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure repo root is importable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import GitMutationLockTimeout, git_mutation_lock

_ENV_VAR = "PANTHEON_GIT_MUTATION_LOCKFILE"


# ---------------------------------------------------------------------------
# (a) Happy path: acquired + released around a mutation.
# ---------------------------------------------------------------------------


class TestGitMutationLockHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # Nested path: also exercises the parent-dir auto-mkdir.
        self._lockfile = os.path.join(self._tmp.name, "nested", "ops.lock")
        os.environ[_ENV_VAR] = self._lockfile

    def tearDown(self) -> None:
        os.environ.pop(_ENV_VAR, None)
        self._tmp.cleanup()

    def test_lock_creates_lockfile_and_runs_body(self) -> None:
        self.assertFalse(os.path.exists(self._lockfile))
        entered = False
        with git_mutation_lock(label="unit-test mutation", timeout_secs=5):
            entered = True
            self.assertTrue(os.path.exists(self._lockfile))
        self.assertTrue(entered, "critical-section body must run once acquired")

    def test_lock_released_on_normal_exit_allows_immediate_reacquire(self) -> None:
        with git_mutation_lock(label="first", timeout_secs=5):
            pass
        start = time.monotonic()
        with git_mutation_lock(label="second", timeout_secs=5):
            pass
        elapsed = time.monotonic() - start
        self.assertLess(
            elapsed, 2.0, "a released lock must be immediately reacquirable"
        )

    def test_lock_released_on_exception_in_body(self) -> None:
        class _MutationFailed(Exception):
            pass

        with self.assertRaises(_MutationFailed):
            with git_mutation_lock(label="raises mid-mutation", timeout_secs=5):
                raise _MutationFailed("simulated git-mv/commit failure")

        # The lock must be free afterward: an independent flock (LOCK_NB) on
        # the SAME lockfile from this process must succeed immediately —
        # proves __exit__ released the flock even though the body raised.
        fd = os.open(self._lockfile, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_holder_record_written_while_held(self) -> None:
        with git_mutation_lock(label="claim T-9999", timeout_secs=5):
            content = Path(self._lockfile).read_text(encoding="utf-8")
        self.assertIn("claim T-9999", content)
        self.assertIn(f"pid={os.getpid()}", content)


# ---------------------------------------------------------------------------
# (b) Cross-process: second acquirer blocks until the first releases.
# ---------------------------------------------------------------------------

# Module-level workers: must be at module scope (not closures) so
# multiprocessing can pickle them for subprocess dispatch (fork context).


def _holder_worker(lockfile_path: str, hold_secs: float, ready_event, release_ts_queue) -> None:
    """Acquire the lock, signal readiness, hold for `hold_secs`, release, and
    report the release wall-clock time (CLOCK_MONOTONIC — shared across
    processes on Linux) onto `release_ts_queue`."""
    import os as _os
    import sys as _sys
    import time as _time
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))
    _os.environ[_ENV_VAR] = lockfile_path

    from tools._git_utils import git_mutation_lock as _lock

    with _lock(label="holder", timeout_secs=30):
        ready_event.set()
        _time.sleep(hold_secs)
    release_ts_queue.put(_time.monotonic())


def _waiter_worker(lockfile_path: str, ready_event, acquire_ts_queue) -> None:
    """Wait for the holder's readiness signal, then attempt to acquire — this
    call blocks until the holder releases — and report the acquire wall-clock
    time."""
    import os as _os
    import sys as _sys
    import time as _time
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))
    _os.environ[_ENV_VAR] = lockfile_path

    from tools._git_utils import git_mutation_lock as _lock

    ready_event.wait(timeout=10)
    with _lock(label="waiter", timeout_secs=30):
        acquire_ts_queue.put(_time.monotonic())


class TestGitMutationLockCrossProcessBlocking(unittest.TestCase):
    """Race-safety acceptance test (T-1770 Part b), same shape as
    tools/test_ticket.py's TestAllocationLockConcurrency (T-0786): launch two
    real processes so the fcntl.flock semantics under test are the actual
    cross-process guarantee, not an in-process approximation.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._lockfile = os.path.join(self._tmp.name, "ops.lock")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_second_acquirer_blocks_until_first_releases(self) -> None:
        import multiprocessing

        ctx = multiprocessing.get_context("fork")
        ready_event = ctx.Event()
        release_ts_queue: "multiprocessing.Queue" = ctx.Queue()
        acquire_ts_queue: "multiprocessing.Queue" = ctx.Queue()

        hold_secs = 1.0
        holder = ctx.Process(
            target=_holder_worker,
            args=(self._lockfile, hold_secs, ready_event, release_ts_queue),
        )
        waiter = ctx.Process(
            target=_waiter_worker,
            args=(self._lockfile, ready_event, acquire_ts_queue),
        )

        holder.start()
        ready_event.wait(timeout=10)  # don't start the waiter racing setup
        waiter.start()

        holder.join(timeout=15)
        waiter.join(timeout=15)

        self.assertEqual(holder.exitcode, 0, "holder worker crashed")
        self.assertEqual(waiter.exitcode, 0, "waiter worker crashed")

        release_ts = release_ts_queue.get_nowait()
        acquire_ts = acquire_ts_queue.get_nowait()

        self.assertGreaterEqual(
            acquire_ts,
            release_ts,
            "waiter acquired the lock BEFORE the holder released it — "
            "the lock did not serialize the two critical sections",
        )


# ---------------------------------------------------------------------------
# (c) Timeout path: bounded, clear error, never proceeds unlocked.
# ---------------------------------------------------------------------------


class TestGitMutationLockTimeout(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._lockfile = os.path.join(self._tmp.name, "ops.lock")
        os.environ[_ENV_VAR] = self._lockfile
        # Simulate another process holding the lock: acquire it directly via
        # a raw flock, bypassing git_mutation_lock (same-process re-entrancy
        # is not supported by design — a second open()+flock() from THIS
        # process genuinely self-contends, which is exactly the contention
        # this test needs, per the docstring on git_mutation_lock).
        os.makedirs(os.path.dirname(self._lockfile), exist_ok=True)
        self._holder_fd = os.open(self._lockfile, os.O_CREAT | os.O_WRONLY, 0o666)
        fcntl.flock(self._holder_fd, fcntl.LOCK_EX)
        os.write(self._holder_fd, b"pid=999999 label=simulated-stuck-holder started=fixture\n")

    def tearDown(self) -> None:
        fcntl.flock(self._holder_fd, fcntl.LOCK_UN)
        os.close(self._holder_fd)
        os.environ.pop(_ENV_VAR, None)
        self._tmp.cleanup()

    def test_timeout_raises_clear_error_naming_path_and_holder(self) -> None:
        entered = False
        start = time.monotonic()
        with self.assertRaises(GitMutationLockTimeout) as ctx:
            with git_mutation_lock(label="waiting-caller", timeout_secs=0.5):
                entered = True  # must never happen — proves "never proceed unlocked"
        elapsed = time.monotonic() - start

        self.assertFalse(entered, "critical section ran despite lock contention")
        self.assertGreaterEqual(elapsed, 0.5, "must actually wait out the timeout")
        self.assertLess(elapsed, 5.0, "must never hang past a bounded timeout")

        msg = str(ctx.exception)
        self.assertIn(self._lockfile, msg, "error must name the lock path")
        self.assertIn(
            "simulated-stuck-holder", msg, "error must surface a holder diagnostic hint"
        )

    def test_lock_freed_by_other_holder_is_acquirable_after_timeout(self) -> None:
        """A timeout on one attempt must not corrupt the lock for the next
        caller once the real holder releases."""
        with self.assertRaises(GitMutationLockTimeout):
            with git_mutation_lock(label="first waiter", timeout_secs=0.3):
                pass

        fcntl.flock(self._holder_fd, fcntl.LOCK_UN)
        os.close(self._holder_fd)

        entered = False
        with git_mutation_lock(label="second waiter", timeout_secs=5):
            entered = True
        self.assertTrue(entered)

        # Prevent tearDown from double-closing the already-closed holder fd.
        self._holder_fd = os.open(self._lockfile, os.O_WRONLY)


if __name__ == "__main__":
    unittest.main()
