"""Tests for tools/_lease.py — the single-holder stale-safe daemon-safe lease (T-0896)."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import pathlib
import unittest
from pathlib import Path
from unittest.mock import patch

from tools._lease import Lease, LeaseHeld, UNKNOWN


class TestRecord(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "x.lock"
        self.addCleanup(self._tmp.cleanup)

    def test_absent_returns_none(self) -> None:
        self.assertIsNone(Lease(self.path).read_record())

    def test_empty_returns_none(self) -> None:
        self.path.write_text("")
        self.assertIsNone(Lease(self.path).read_record())

    def test_malformed_returns_none(self) -> None:
        self.path.write_text("{not json")
        self.assertIsNone(Lease(self.path).read_record())

    def test_roundtrip_after_acquire(self) -> None:
        lz = Lease(self.path, label="veneration:orchestrator")
        self.assertTrue(lz.acquire())
        self.addCleanup(lz.release)
        rec = lz.read_record()
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["label"], "veneration:orchestrator")
        self.assertIsInstance(rec["start"], int)


class TestStaleness(unittest.TestCase):
    def setUp(self) -> None:
        self.lz = Lease("/tmp/unused.lock", ttl_secs=900)

    def test_dead_pid_is_stale(self) -> None:
        with patch.object(Lease, "_pid_alive", return_value=False):
            self.assertTrue(self.lz._holder_is_stale({"pid": 12345, "start": int(time.time())}, int(time.time())))

    def test_live_within_ttl_not_stale(self) -> None:
        now = 1_000_000
        with patch.object(Lease, "_pid_alive", return_value=True):
            self.assertFalse(self.lz._holder_is_stale({"pid": 1, "start": now - 10}, now))

    def test_live_past_ttl_is_stale(self) -> None:
        now = 1_000_000
        with patch.object(Lease, "_pid_alive", return_value=True):
            self.assertTrue(self.lz._holder_is_stale({"pid": 1, "start": now - 901}, now))

    def test_malformed_pid_is_stale(self) -> None:
        self.assertTrue(self.lz._holder_is_stale({"pid": "nope", "start": 0}, 0))

    # ---- T-2236: staleness is the HOLDER's property, not the acquirer's ----

    def test_holder_ttl_is_honoured_over_the_acquirers(self) -> None:
        """THE LIVE BUG. A 900s acquirer (an MCP consume) must NOT declare a 7200s
        holder (an ACP bridge) stale at 901s. Before this, `_holder_is_stale` read
        the ACQUIRER's ttl_secs, so the bridge was reaped -- SIGTERM then SIGKILL,
        same uid so permitted. Measured live with both bridges past threshold."""
        now = 1_000_000
        rec = {"pid": 1, "start": now - 901, "ttl_secs": 7200, "label": "acp-bridge:specialist"}
        with patch.object(Lease, "_pid_alive", return_value=True):
            self.assertFalse(self.lz._holder_is_stale(rec, now))

    def test_holder_past_its_own_ttl_is_still_stale(self) -> None:
        """The fix must not make long-lived holders immortal -- a genuinely wedged
        7200s holder is still reapable once IT is past 7200s."""
        now = 1_000_000
        rec = {"pid": 1, "start": now - 7201, "ttl_secs": 7200}
        with patch.object(Lease, "_pid_alive", return_value=True):
            self.assertTrue(self.lz._holder_is_stale(rec, now))

    def test_record_without_ttl_falls_back_to_the_acquirers(self) -> None:
        """Backward compatibility: a record written before T-2236 has no ttl_secs,
        and must keep the old behaviour rather than becoming unreapable."""
        now = 1_000_000
        with patch.object(Lease, "_pid_alive", return_value=True):
            self.assertTrue(self.lz._holder_is_stale({"pid": 1, "start": now - 901}, now))

    def test_malformed_ttl_falls_back_rather_than_crashing(self) -> None:
        """A garbage ttl_secs must degrade to the acquirer's value, never raise and
        never be treated as a valid bound. `True` is int-ish in Python and would
        otherwise pass an isinstance check, making the holder stale after 1 second."""
        now = 1_000_000
        with patch.object(Lease, "_pid_alive", return_value=True):
            for bad in ("7200", None, -1, 0, True):
                self.assertTrue(
                    self.lz._holder_is_stale({"pid": 1, "start": now - 901, "ttl_secs": bad}, now),
                    f"ttl_secs={bad!r} should fall back to the acquirer's 900s",
                )

    def test_acquired_record_carries_its_ttl(self) -> None:
        """The read side is useless if the write side never records it."""
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as d:
            lz = Lease(_os.path.join(d, "x.lock"), ttl_secs=7200, label="acp-bridge:test")
            self.assertTrue(lz.acquire())
            try:
                self.assertEqual(lz.read_record().get("ttl_secs"), 7200)
            finally:
                lz.release()

    def test_pid_alive_dead(self) -> None:
        # A PID that almost certainly does not exist.
        self.assertFalse(Lease._pid_alive(4_000_000_000))

    def test_pid_alive_self(self) -> None:
        self.assertTrue(Lease._pid_alive(os.getpid()))


class TestAcquireRelease(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "lease.lock"
        self.addCleanup(self._tmp.cleanup)

    def test_acquire_free_then_release_idempotent(self) -> None:
        lz = Lease(self.path)
        self.assertTrue(lz.acquire())
        self.assertTrue(lz.held())
        lz.release()
        self.assertFalse(lz.held())
        lz.release()  # idempotent

    def test_contention_returns_false(self) -> None:
        holder = Lease(self.path, label="holder")
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        # Second acquirer, holder is fresh (not stale) → contended → False.
        contender = Lease(self.path, label="contender")
        self.assertFalse(contender.acquire(reap_stale=False))
        self.assertFalse(contender.held())

    def test_raising_on_contention(self) -> None:
        holder = Lease(self.path)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        with self.assertRaises(LeaseHeld):
            Lease(self.path).acquire(reap_stale=False, raising=True)

    def test_perm_error_not_misreported_as_contention(self) -> None:
        lz = Lease(self.path)
        with patch("tools._lease.os.open", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError) as ctx:
                lz.acquire()
        self.assertIn("permission", str(ctx.exception).lower())

    def test_context_manager(self) -> None:
        with Lease(self.path, label="ctx") as lz:
            self.assertTrue(lz.held())
            self.assertEqual(lz.read_record()["label"], "ctx")
        self.assertFalse(lz.held())

    def test_reacquire_after_release(self) -> None:
        a = Lease(self.path)
        self.assertTrue(a.acquire())
        a.release()
        b = Lease(self.path)
        self.assertTrue(b.acquire())  # freed → reacquirable
        self.addCleanup(b.release)


class TestStaleReap(unittest.TestCase):
    """A contended acquirer reaps a STALE holder and reacquires (T-0584 Layer 2)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "lease.lock"
        self.addCleanup(self._tmp.cleanup)

    def test_reaps_a_genuinely_DEAD_holder_then_acquires(self) -> None:
        """Orphan reapability — the property this test was always FOR.

        REWRITTEN (T-2311). The original held a REAL flock open while patching
        _pid_alive to False, i.e. it modelled a dead process that was still
        holding a kernel lock — something that cannot happen, since the kernel
        releases flocks on exit. It then asserted acquire() SUCCEEDED, and its
        own comment named the mechanism: "reap stale (rm) -> reopen new inode
        -> flock ok". That is the lock FORKING, pinned as expected behaviour.
        A test asserting the bug is why the bug survived three fixes to this
        function in one night.

        A dead holder leaves its record text and NO lock. That is modelled here
        by releasing the flock, which is what dying actually does.
        """
        holder_fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
        self.addCleanup(lambda: os.close(holder_fd))
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder_fd, (json.dumps({"pid": 999999, "start": 1, "label": "wedged"}) + "\n").encode())
        os.fsync(holder_fd)
        fcntl.flock(holder_fd, fcntl.LOCK_UN)   # the holder died -> kernel drops the lock

        with patch.object(Lease, "_signal") as sig:
            lz = Lease(self.path, label="new")
            self.assertTrue(lz.acquire(), "an orphaned record must not block acquisition")
            self.addCleanup(lz.release)
        sig.assert_not_called()  # nothing held the lock, so nothing to signal
        self.assertEqual(lz.read_record()["label"], "new")

    def test_stale_TEXT_over_a_LIVE_lock_does_not_evict_the_holder(self) -> None:
        """T-2311 (specialist): the fork the old test enshrined.

        flock_holder_pid POSITIVELY names a live holder that simply is not the
        pid in the record text. Reached in production with no error at all, via
        acquire()'s own ordering (flock succeeds, THEN _write_record): A crashes
        leaving text=A; B flocks; C contends, reads A, finds A dead so stale,
        reaps, sees holder=B != A, and unlinks B's lockfile. C's retry O_CREATs
        a new inode and flock is per-inode -> TWO LIVE LEASES.

        Stale text is not a reason to disturb a live lock.
        """
        holder_fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
        self.addCleanup(lambda: os.close(holder_fd))
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # LIVE lock, this process
        os.write(holder_fd, (json.dumps({"pid": 999999, "start": 1, "label": "stale-text"}) + "\n").encode())
        os.fsync(holder_fd)

        with patch.object(Lease, "_signal") as sig:
            lz = Lease(self.path, label="contender")
            self.assertFalse(
                lz.acquire(),
                "acquire must FAIL against a live holder — succeeding means the lock forked",
            )
        sig.assert_not_called()
        self.assertTrue(self.path.exists(), "a live holder's lockfile must survive")

    def test_no_reap_when_holder_fresh(self) -> None:
        holder = Lease(self.path, label="fresh")
        self.assertTrue(holder.acquire())  # live, just-started → not stale
        self.addCleanup(holder.release)
        with patch.object(Lease, "_reap") as reap:
            contender = Lease(self.path)
            self.assertFalse(contender.acquire())  # fresh holder → contended, no reap
        reap.assert_not_called()


class TestAcquirerTtlMustNotUndercutHolder(unittest.TestCase):
    """T-2236: a SHORT-TTL acquirer reaps a healthy LONG-TTL holder.

    `_holder_is_stale` measures against the ACQUIRER's ttl_secs, so an MCP
    mailbox_consume defaulting to 900 declares an ACP bridge holding at 7200
    "stale" at 901s and SIGTERM/SIGKILLs it — permitted, same uid.

    Measured live 2026-07-28 with both holders ALIVE and already past the window:
    specialist pid 323758 held 5111s, mnemosyne pid 385082 held 3375s. One MCP consume
    call would have killed either.

    These tests live here rather than in test_mailbox_mcp because all 15 tests in
    that module SKIP (the `mcp` package is not importable in this environment), so
    coverage placed there would have asserted nothing.
    """

    def _rec(self, held_s):
        import time as _t
        return {"pid": os.getpid(), "start": int(_t.time()) - held_s,
                "label": "acp-bridge:test"}

    def test_short_ttl_acquirer_would_reap_a_healthy_long_holder(self):
        import time as _t
        import tools._lease as L
        with tempfile.TemporaryDirectory() as d:
            short = L.Lease(pathlib.Path(d) / "a.lock", ttl_secs=900)
            self.assertTrue(short._holder_is_stale(self._rec(3000), int(_t.time())),
                            "this is the bug: a live 3000s holder looks stale to a 900s acquirer")

    def test_matching_ttl_acquirer_does_not_reap(self):
        import time as _t
        import tools._lease as L
        with tempfile.TemporaryDirectory() as d:
            longg = L.Lease(pathlib.Path(d) / "b.lock", ttl_secs=7200)
            self.assertFalse(longg._holder_is_stale(self._rec(3000), int(_t.time())),
                             "an acquirer must never declare staleness sooner than the holder's TTL")

    def test_dead_holder_is_still_reapable_regardless_of_ttl(self):
        """The mitigation must not disable genuine orphan cleanup."""
        import time as _t
        import tools._lease as L
        with tempfile.TemporaryDirectory() as d:
            lease = L.Lease(pathlib.Path(d) / "c.lock", ttl_secs=7200)
            dead = {"pid": 2 ** 22, "start": int(_t.time()), "label": "gone"}
            self.assertTrue(lease._holder_is_stale(dead, int(_t.time())))

    def test_mailbox_mcp_call_site_actually_passes_a_matching_ttl(self):
        """Source-level guard, and it is here for a reason.

        The four tests above prove the MECHANISM (lease TTL semantics + consume
        forwarding). None of them would notice if mailbox_mcp.py stopped passing
        ttl_secs — and that call site is the whole mitigation. Its own module's
        tests all SKIP (no `mcp` package), so without this a revert is invisible.
        A guard that cannot fail on the thing it guards is decoration.

        T-2243: the TTL comparison is a PARSED RELATION, not a hardcoded literal.
        The constant is one half of a cross-language pair — the HOLDER side is
        `BRIDGE_CONSUME_TTL` in acp-bridge-supervise.sh, the ACQUIRER side is
        `_BRIDGE_CONSUME_TTL_SECS` here — and until now only a source comment
        coupled them. Asserting the acquirer against a literal 7200 pinned one
        side: raising the shell default to 14400 restored the exact T-2236
        inequality (acquirer < holder, so an MCP consume can reap a live bridge
        mid-drain) with the whole suite still green. So parse the holder's
        default and assert acquirer >= holder.

        SCOPE, and it is NARROWER THAN THIS DOCSTRING USED TO CLAIM (corrected
        2026-08-02, T-2243). What this test closes is source-level drift of the
        two DEFAULTS. `ACP_BRIDGE_CONSUME_TTL` is a runtime environment
        override, and a unit drop-in setting it escapes this static check — that
        much is still true.

        What is NOT true, and used to be asserted here as open T-2243 residue:
        that such an override "silently restores the T-2236 inequality" and that
        closing it needs the acquirer to read the same env var. It does not.
        Staleness is the HOLDER's property — `_lease.Lease.acquire()` RECORDS
        `ttl_secs` into the lock file, and `_holder_is_stale` reads the holder's
        recorded value, falling back to the acquirer's only when the record has
        none (pre-T-2236 records) or it is malformed. So a bridge started with a
        raised `ACP_BRIDGE_CONSUME_TTL` writes that raised TTL into its own lock,
        and every acquirer honours it regardless of its own constant. The
        acquirer's constant is a FALLBACK, not the reaping threshold.
        Covered by `test_holder_ttl_is_honoured_over_the_acquirers` and
        `test_record_without_ttl_falls_back_to_the_acquirers` above.

        The correction matters because of what an overstated residue COSTS: it
        told a future reader there was an open production hole, in a file whose
        entire subject is guards that quietly stop being true. Left standing, it
        buys someone a needless production change to close a hole that the
        T-2236 fix had already closed.
        """
        import re
        tools_dir = pathlib.Path(__file__).resolve().parent
        text = (tools_dir / "mailbox_mcp.py").read_text(encoding="utf-8")
        m = re.search(r"_mailbox\.consume\((.*?)\)", text, re.S)
        self.assertIsNotNone(m, "could not locate the MCP consume call site")
        self.assertIn("ttl_secs", m.group(1),
                      "mailbox_mcp must pass an explicit ttl_secs — the 900s default "
                      "reaps a live ACP bridge holding at 7200 (T-2236)")
        # T-2287: the acquirer constant may now be a LITERAL or a reference to
        # mailbox._DEFAULT_CONSUME_TTL_SECS (one owner for the value, per T-2304).
        # Resolving the reference keeps this guard live; matching only \d+ would
        # have made it blind the moment the constant stopped being a literal —
        # which is exactly the failure this docstring warns about, so it is fixed
        # here rather than by relaxing the assertion.
        mconst = re.search(r"_BRIDGE_CONSUME_TTL_SECS\s*=\s*(\d+)", text)
        if mconst is None:
            self.assertRegex(
                text, r"_BRIDGE_CONSUME_TTL_SECS\s*=\s*_?mailbox\._DEFAULT_CONSUME_TTL_SECS",
                "the TTL constant must be named — a literal, or the shared "
                "mailbox._DEFAULT_CONSUME_TTL_SECS — not inline at the call site")
            mconst = re.search(
                r"^_DEFAULT_CONSUME_TTL_SECS\s*=\s*(\d+)",
                (tools_dir / "mailbox.py").read_text(encoding="utf-8"), re.M)
            self.assertIsNotNone(
                mconst,
                "mailbox_mcp defers to mailbox._DEFAULT_CONSUME_TTL_SECS but that "
                "constant could not be parsed out of mailbox.py — THIS GUARD IS "
                "NOW BLIND. Fix the parse here; do not delete the assertion.")

        sh = (tools_dir / "acp-bridge-supervise.sh").read_text(encoding="utf-8")
        mholder = re.search(
            r'^BRIDGE_CONSUME_TTL="\$\{ACP_BRIDGE_CONSUME_TTL:-(\d+)\}"\s*$', sh, re.M)
        self.assertIsNotNone(
            mholder,
            "could not parse the BRIDGE_CONSUME_TTL default out of "
            "acp-bridge-supervise.sh — the line shape changed, so THIS GUARD IS "
            "NOW BLIND to the drift it exists to catch (T-2243). Fix the parse "
            "here; do not delete the assertion.")
        holder, acquirer = int(mholder.group(1)), int(mconst.group(1))
        self.assertGreaterEqual(
            acquirer, holder,
            f"acquirer TTL ({acquirer}s, mailbox_mcp._BRIDGE_CONSUME_TTL_SECS) must not "
            f"undercut the bridges' --consume-ttl default ({holder}s, "
            f"acp-bridge-supervise.sh BRIDGE_CONSUME_TTL) — an acquirer TTL below a "
            f"holder's is exactly the T-2236 bug: a consume reaps a live bridge mid-drain")


    def test_consume_forwards_ttl_secs_into_the_lease(self):
        """The plumbing the mitigation relies on."""
        import tools._lease as L
        import tools.mailbox as mb
        seen = {}
        real = mb.Lease

        class Spy(real):
            def __init__(self, path, **kw):
                seen.update(kw)
                super().__init__(path, **kw)

        with tempfile.TemporaryDirectory() as d:
            box = pathlib.Path(d)
            for sub in ("new", "cur", "tmp"):
                (box / sub).mkdir()
            mb.Lease = Spy
            try:
                mb.consume(str(box), lambda m: None, ttl_secs=7200)
            finally:
                mb.Lease = real
        self.assertEqual(seen.get("ttl_secs"), 7200)





class TestReapTargetsTheKernelNotTheText(unittest.TestCase):
    """T-2292 (specialist): _reap used to signal the pid the LOCKFILE NAMES, guarded
    only by "is some process with that pid alive".

    Two ways that is wrong, and _signal escalates to os.killpg, so the blast
    radius is a whole PROCESS GROUP:
      - RECYCLED PID: the record outlives its writer, Linux reuses the number,
        and we SIGKILL an innocent group.
      - SURVIVING HOLDER: the unlink was unconditional, so a holder that
        ignored the signals lost its lockfile anyway; the next acquire O_CREATs
        a NEW INODE and flock is per-inode -> TWO LIVE LEASES.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".consumer.lock"
        self.lease = Lease(self.path, label="t2292", ttl_secs=1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_signal_when_the_named_pid_does_not_hold_the_lock(self):
        """The record names a LIVE pid (our own) with no lock behind it.

        os.getpid() is alive by construction, so the old `_pid_alive` guard
        passed and the reap signalled. Nothing holds the flock, so the correct
        behaviour is to clear the stale text and signal NOTHING.
        """
        self.path.write_text(json.dumps({"pid": os.getpid(), "label": "x"}), encoding="utf-8")
        with patch.object(Lease, "_signal") as sig:
            self.lease._reap({"pid": os.getpid(), "label": "x"})
        sig.assert_not_called()
        self.assertFalse(self.path.exists(), "stale record should still be cleared")

    def test_surviving_holder_keeps_its_lockfile_so_the_lock_cannot_FORK(self):
        """Holder ignores the signals (mocked no-op) and still holds the flock.

        Unlinking here is what forks the lock: the next acquire creates a new
        inode and both leases become live. A failed acquire is the correct,
        recoverable outcome.
        """
        self.path.write_text(json.dumps({"pid": os.getpid(), "label": "x"}), encoding="utf-8")
        fd = os.open(self.path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.lease.flock_holder_pid(), os.getpid())
        self.lease.grace_secs = 0.05
        with patch.object(Lease, "_signal"):        # holder survives
            self.lease._reap({"pid": os.getpid(), "label": "x"})
        self.assertTrue(
            self.path.exists(),
            "lockfile must survive a holder that did not die — unlinking forks the lock",
        )

    def test_holder_that_really_died_is_cleaned_up(self):
        """The normal path must still work: no lock held -> record removed."""
        self.path.write_text(json.dumps({"pid": 999999, "label": "x"}), encoding="utf-8")
        self.lease._reap({"pid": 999999, "label": "x"})
        self.assertFalse(self.path.exists())

    def test_flock_holder_pid_reports_the_real_holder(self):
        self.path.write_text("{}", encoding="utf-8")
        self.assertIsNone(self.lease.flock_holder_pid())
        fd = os.open(self.path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.lease.flock_holder_pid(), os.getpid())
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertIsNone(self.lease.flock_holder_pid())


class TestBlindLockStateNeverForksTheLock(unittest.TestCase):
    """T-2307 (specialist): flock_holder_pid returned None for BOTH 'not held' and
    'cannot tell', and _reap acts on those oppositely.

    `holder is None` took the clear-the-record branch, so an unreadable
    /proc/locks made a live holder lose its lockfile; the retry O_CREATs a
    second inode and the lock FORKS — the exact outcome T-2292 removed,
    reachable again through the error path.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".consumer.lock"
        self.lease = Lease(self.path, label="t2307", ttl_secs=1)
        self.path.write_text(json.dumps({"pid": os.getpid(), "label": "x"}), encoding="utf-8")
        self.fd = os.open(self.path, os.O_RDWR)
        self.addCleanup(os.close, self.fd)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tearDown(self):
        self._tmp.cleanup()

    def test_CONTROL_procfs_readable_behaves_as_designed(self):
        """The control: with procfs up, a real holder is detected."""
        self.assertEqual(self.lease.flock_holder_pid(), os.getpid())

    def test_unreadable_procfs_is_UNKNOWN_not_None(self):
        with patch.object(pathlib.Path, "read_text", side_effect=OSError("procfs down")):
            self.assertIs(self.lease.flock_holder_pid(), UNKNOWN)

    def test_blind_reap_does_NOT_unlink_a_live_holders_lockfile(self):
        """The fork. The lockfile must survive when we cannot see the kernel."""
        with patch.object(pathlib.Path, "read_text", side_effect=OSError("procfs down")):
            with patch.object(Lease, "_signal") as sig:
                self.lease._reap({"pid": os.getpid(), "label": "x"})
            sig.assert_not_called()
        self.assertTrue(
            self.path.exists(),
            "blind reap unlinked a lockfile whose flock the kernel still enforces -> lock forks",
        )
        # and the lock is genuinely still ours
        self.assertEqual(self.lease.flock_holder_pid(), os.getpid())

    def test_missing_lockfile_is_None_not_UNKNOWN(self):
        """FileNotFoundError is a definitive answer, not blindness — otherwise
        an ordinary tombstone would become un-reapable."""
        p = Path(self._tmp.name) / "absent.lock"
        self.assertIsNone(Lease(p, label="t").flock_holder_pid())

    def test_UNKNOWN_refuses_bool_coercion(self):
        """`if holder:` must not silently read as 'not held'."""
        with self.assertRaises(TypeError):
            bool(UNKNOWN)


if __name__ == "__main__":
    unittest.main()
