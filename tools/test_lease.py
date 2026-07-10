"""Tests for tools/_lease.py — the single-holder stale-safe daemon-safe lease (T-0896)."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools._lease import Lease, LeaseHeld


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

    def test_reaps_stale_holder_then_acquires(self) -> None:
        # Simulate a wedged holder: hold a real flock on the lockfile with a record,
        # but make the holder look dead. _signal is mocked so the test process is safe.
        holder_fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
        self.addCleanup(lambda: os.close(holder_fd))
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder_fd, (json.dumps({"pid": 999999, "start": 1, "label": "wedged"}) + "\n").encode())
        os.fsync(holder_fd)

        reaped = {}
        def fake_signal(pid, sig):
            reaped["pid"] = pid
        with patch.object(Lease, "_pid_alive", return_value=False), \
             patch.object(Lease, "_signal", staticmethod(fake_signal)):
            lz = Lease(self.path, label="new")
            self.assertTrue(lz.acquire())  # reap stale (rm) → reopen new inode → flock ok
            self.addCleanup(lz.release)
        self.assertEqual(lz.read_record()["label"], "new")

    def test_no_reap_when_holder_fresh(self) -> None:
        holder = Lease(self.path, label="fresh")
        self.assertTrue(holder.acquire())  # live, just-started → not stale
        self.addCleanup(holder.release)
        with patch.object(Lease, "_reap") as reap:
            contender = Lease(self.path)
            self.assertFalse(contender.acquire())  # fresh holder → contended, no reap
        reap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
