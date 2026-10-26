#!/usr/bin/env python3
"""Tests for tools/memory_store_repair.py and the store reader's ``repair_void``
rule (T-4503, ORG-058). Synthetic stores only: built with the store's own API,
then corrupted at the byte level. The marker string planted in corrupt bytes
must never reach stdout, stderr or an exception message."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_lifecycle_ops as ops  # noqa: E402
import memory_store as ms  # noqa: E402
import memory_store_repair as rp  # noqa: E402
from test_memory_lifecycle_ops import _Case  # noqa: E402
from test_memory_store import NOW, SRC_OBJECT, _StoreCase, admit  # noqa: E402

MARKER = "ZZ-SECRET-MARKER-7f3a"
GARBAGE = ("{" + MARKER + " not json").encode()


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def run(root: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = rp.main(["--root", str(root), *args])
        except SystemExit as exc:  # argparse usage error
            code = int(exc.code)
    return code, out.getvalue(), err.getvalue()


def replace_line(path: Path, n: int, new: bytes) -> bytes:
    """Replace 1-based line ``n`` with ``new``; return the old bytes."""
    lines = path.read_bytes().split(b"\n")
    old = lines[n - 1]
    lines[n - 1] = new
    path.write_bytes(b"\n".join(lines))
    return old


def line_offset(path: Path, n: int) -> int:
    return sum(len(x) + 1 for x in path.read_bytes().split(b"\n")[: n - 1])


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): sha(p.read_bytes()) if p.is_file() else "dir"
        for p in sorted(root.rglob("*"))
        if p.name != ".lock"
    }


class _EventsCase(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        for i in (1, 2, 3):
            admit(self.store, f"m-{i}", "v-1")
        self.events = self.root / "events.jsonl"

    def corrupt_line2(self) -> bytes:
        return replace_line(self.events, 2, GARBAGE)


class TestEventsF8b(_EventsCase):
    def test_store_wedges_then_dry_run_reports_and_writes_nothing(self) -> None:
        self.corrupt_line2()
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()
        before = snapshot(self.root)
        code, out, err = run(self.root, "--log", "events")
        self.assertEqual(code, rp.EXIT_WORK, out + err)
        self.assertIn(f"line 2 offset {line_offset(self.events, 2)} length {len(GARBAGE)} digest {sha(GARBAGE)} class corrupt-unparseable", out)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "quarantine").exists())

    def test_apply_quarantines_exact_bytes_voids_verifies_and_is_idempotent(self) -> None:
        old_events = [json.loads(x) for x in self.events.read_bytes().split(b"\n")[:-1]]
        self.corrupt_line2()
        code, out, err = run(self.root, "--log", "events", "--apply", "--operation-id", "rep-1")
        self.assertEqual(code, rp.EXIT_OK, out + err)
        side = self.root / "quarantine" / "events.rep-1.jsonl"
        self.assertEqual(oct(side.stat().st_mode & 0o777), "0o600")
        rec = [json.loads(x) for x in side.read_bytes().splitlines()]
        self.assertEqual(len(rec), 1)
        self.assertEqual(base64.b64decode(rec[0]["bytes_b64"]), GARBAGE)
        self.assertEqual((rec[0]["line"], rec[0]["length"], rec[0]["digest"]), (2, len(GARBAGE), sha(GARBAGE)))
        # the reader loads; the surviving records are the originals minus line 2
        fresh = ms.MemoryStore(self.root)
        self.assertEqual(list(fresh.events()), [old_events[0], old_events[2]])
        self.assertEqual(set(fresh.folded()), {("m-1", "v-1"), ("m-3", "v-1")})
        fresh.rebuild_projections()
        # the log was APPENDED to, never rewritten: the corrupt bytes are still in place
        self.assertEqual(self.events.read_bytes().split(b"\n")[1], GARBAGE)
        after = snapshot(self.root)
        code2, out2, _ = run(self.root, "--log", "events", "--apply")
        self.assertEqual(code2, rp.EXIT_OK, out2)
        self.assertEqual(snapshot(self.root), after)
        self.assertIn("repair-voided=1", out2)

    def test_non_object_line_is_corrupt_and_blank_line_is_not(self) -> None:
        lines = self.events.read_bytes().split(b"\n")
        lines.insert(1, b"")  # a mid-file blank line: harmless
        self.events.write_bytes(b"\n".join(lines))
        self.assertEqual(len(self.store.events()), 3)
        code, out, _ = run(self.root, "--log", "events")
        self.assertEqual(code, rp.EXIT_OK, out)
        self.assertIn("blank=1", out)
        replace_line(self.events, 3, b'["a", "list"]')
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()
        code, out, _ = run(self.root, "--log", "events")
        self.assertEqual(code, rp.EXIT_WORK)
        self.assertIn("class corrupt-nonobject", out)
        code, out, err = run(self.root, "--log", "events", "--apply")
        self.assertEqual(code, rp.EXIT_OK, out + err)
        self.assertEqual(len(ms.MemoryStore(self.root).events()), 2)

    def test_torn_tail_is_reported_not_repaired(self) -> None:
        with open(self.events, "ab") as fh:
            fh.write(b'{"half')
        code, out, _ = run(self.root, "--log", "events", "--apply")
        self.assertEqual(code, rp.EXIT_OK, out)
        self.assertIn("class torn-tail", out)
        self.assertFalse((self.root / "quarantine").exists())

    def test_a_loss_that_breaks_the_fold_is_refused_and_writes_nothing(self) -> None:
        # corrupt the ADMISSION of m-1 after a second event chained on it
        lines = self.events.read_bytes().split(b"\n")
        chained = json.loads(lines[0])
        chained["seq"] = 99
        chained["transitions"] = [{"memory_id": "m-1", "version_id": "v-1", "from": "RETAINED_DERIVED", "to": "DECAYED"}]
        with open(self.events, "ab") as fh:
            fh.write((json.dumps(chained, sort_keys=True) + "\n").encode())
        replace_line(self.events, 1, GARBAGE)
        before = snapshot(self.root)
        code, out, _ = run(self.root, "--log", "events", "--apply")
        self.assertEqual(code, rp.EXIT_REFUSED, out)
        self.assertEqual(snapshot(self.root), before)
        self.assertNotIn(MARKER, out)


class TestL2DoubleTear(_StoreCase):
    def test_torn_heal_wedges_the_store_and_the_repair_unwedges_it(self) -> None:
        admit(self.store, "m-1", "v-1")
        events = self.root / "events.jsonl"
        first = events.read_bytes()
        torn1 = b'{"seq":2,"kind":"adm' + MARKER.encode()  # crash residue: torn tail
        with open(events, "ab") as fh:
            fh.write(torn1)
        real_write = os.write

        def tearing(fd, data):  # THE HEAL APPEND TEARS: only a prefix reaches the file
            if data.startswith(b"\n"):
                real_write(fd, data[: len(data) // 2])
                raise OSError(5, "I/O error")
            return real_write(fd, data)

        with mock.patch.object(ms.os, "write", tearing), self.assertRaises(OSError):
            admit(self.store, "m-2", "v-1")
        # the first fragment is now terminated mid-file with no voider: wedged
        # for reads, and for writes (append_event folds first)
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()
        with self.assertRaises(ms.StoreIntegrityError):
            admit(self.store, "m-3", "v-1")
        code, out, _ = run(self.root, "--log", "events")
        self.assertEqual(code, rp.EXIT_WORK, out)
        self.assertIn(f"offset {len(first)} length {len(torn1)} digest {sha(torn1)} class corrupt-unparseable", out)
        self.assertEqual(out.count("class corrupt-unparseable"), 1)
        code, out, err = run(self.root, "--log", "events", "--apply")
        self.assertEqual(code, rp.EXIT_OK, out + err)
        fresh = ms.MemoryStore(self.root)
        self.assertEqual({m for (m, _v) in fresh.folded()}, {"m-1"})
        admit(fresh, "m-3", "v-1")  # unwedged


class TestL2OnTombstones(_Case):
    def test_second_heal_voids_only_the_second_fragment_and_repair_needs_ack(self) -> None:
        # add_tombstone never reads the log, so a LATER heal does run: it voids
        # only the second fragment, leaving the first wedged forever.
        tomb = self.root / "tombstones.jsonl"
        torn1 = b'{"memory_id":"m-1","sour' + MARKER.encode()
        tomb.write_bytes(torn1)
        real_write = os.write

        def tearing(fd, data):
            if data.startswith(b"\n"):
                real_write(fd, data[: len(data) // 2])
                raise OSError(5, "I/O error")
            return real_write(fd, data)

        kw = dict(source_system="file-src", object_id="a.md", operation_id="op", now=NOW,
                  policy_revision="lc-rev-1", forgetting_policy="forget-tomb")
        with mock.patch.object(ms.os, "write", tearing), self.assertRaises(OSError):
            self.store.add_tombstone(memory_id="m-1", **kw)
        self.store.add_tombstone(memory_id="m-2", **kw)  # heals the 2nd fragment only
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.tombstones()
        code, out, _ = run(self.root, "--log", "tombstones", "--apply")
        self.assertEqual(code, rp.EXIT_REFUSED, out)  # no receipt evidence: unprovable
        self.assertNotIn(MARKER, out)
        code, out, _ = run(self.root, "--log", "tombstones", "--apply", "--ack-tombstone-loss", sha(torn1))
        self.assertEqual(code, rp.EXIT_OK, out)
        self.assertEqual([t["memory_id"] for t in ms.MemoryStore(self.root).tombstones()], ["m-2"])


class TestRepairVoidReader(_EventsCase):
    def _void(self, offset: int, length: int, digest: str) -> bytes:
        rec = {"repair_void": {"operation_id": "x", "at": NOW, "quarantine": "q",
                               "voids": [{"offset": offset, "length": length, "digest": digest}]}}
        return (json.dumps(rec, sort_keys=True) + "\n").encode()

    def test_forged_void_with_wrong_digest_is_corruption_not_a_licence(self) -> None:
        l2 = self.events.read_bytes().split(b"\n")[1]
        with open(self.events, "ab") as fh:
            fh.write(self._void(line_offset(self.events, 2), len(l2), sha(b"other")))
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()
        # and with a corrupt line present, the forged void does NOT heal it
        replace_line(self.events, 2, GARBAGE)
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()

    def test_a_void_never_hides_a_valid_event(self) -> None:
        lines = self.events.read_bytes().split(b"\n")
        with open(self.events, "ab") as fh:  # digest + length match, but the line is VALID
            fh.write(self._void(line_offset(self.events, 2), len(lines[1]), sha(lines[1])))
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()

    def test_a_void_never_hides_a_valid_tombstone(self) -> None:
        self.store.add_tombstone(memory_id="m-1", source_system="file-src", object_id="a.md",
                                 operation_id="op", now=NOW, policy_revision="r", forgetting_policy="f")
        tomb = self.root / "tombstones.jsonl"
        line = tomb.read_bytes().split(b"\n")[0]
        with open(tomb, "ab") as fh:
            fh.write(self._void(0, len(line), sha(line)))
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.tombstones()

    def test_void_of_a_corrupt_line_needs_the_exact_length_and_digest(self) -> None:
        self.corrupt_line2()
        at = line_offset(self.events, 2)
        with open(self.events, "ab") as fh:
            fh.write(self._void(at, len(GARBAGE) - 1, sha(GARBAGE)))
        with self.assertRaises(ms.StoreIntegrityError):
            self.store.events()
        # the exact bytes: the void takes effect, the void record is never returned
        replace_line(self.events, 4, self._void(at, len(GARBAGE), sha(GARBAGE)).rstrip(b"\n"))
        got = self.store.events()
        self.assertEqual(len(got), 2)
        self.assertTrue(all("repair_void" not in r for r in got))


class TestNoContentLeak(_EventsCase):
    def test_marker_never_in_output_or_exceptions(self) -> None:
        self.corrupt_line2()
        try:
            self.store.events()
        except ms.StoreIntegrityError as exc:
            self.assertNotIn(MARKER, str(exc))
        for args in ((), ("--apply",), ("--apply",)):
            code, out, err = run(self.root, "--log", "events", *args)
            self.assertNotIn(MARKER, out + err)
        self.assertNotIn(MARKER, run(self.root, "--log", "events", "--operation-id", "bad id!")[2])
        # tombstones too (a non-JSON tombstone line, unprovable, refused)
        (self.root / "tombstones.jsonl").write_bytes(GARBAGE + b"\n")
        for args in ((), ("--apply",)):
            code, out, err = run(self.root, "--log", "tombstones", *args)
            self.assertEqual(code, rp.EXIT_REFUSED)
            self.assertNotIn(MARKER, out + err)


class TestUsage(_EventsCase):
    def test_not_a_store_and_bad_args_exit_3(self) -> None:
        self.assertEqual(run(self.root / "nope")[0], rp.EXIT_USAGE)
        self.assertEqual(run(self.root, "--log", "bogus")[0], rp.EXIT_USAGE)
        self.assertEqual(run(self.root, "--operation-id", "no spaces")[0], rp.EXIT_USAGE)

    def test_clean_store_exits_0(self) -> None:
        self.assertEqual(run(self.root)[0], rp.EXIT_OK)

    def test_any_failure_taking_the_lock_exits_3_without_content(self) -> None:
        with mock.patch.object(ms.MemoryStore, "locked", side_effect=RuntimeError(MARKER)):
            code, out, err = run(self.root)
        self.assertEqual(code, rp.EXIT_USAGE)
        self.assertNotIn(MARKER, out + err)

    def test_holds_the_store_lock(self) -> None:
        seen = []
        real = rp._run

        def probe(store, *a, **k):
            seen.append(store._held_fd is not None)
            return real(store, *a, **k)

        with mock.patch.object(rp, "_run", probe):
            run(self.root)
        self.assertEqual(seen, [True])


class TestTombstones(_Case):
    def setUp(self) -> None:
        super().setUp()
        (self.src_root / "b.md").write_text("synthetic second source\n")
        self.seed("m-1", "v-1", forgetting_policy="forget-tomb")
        self.seed("m-2", "v-1", forgetting_policy="forget-tomb",
                  canonical_source_ref=self.live_ref(object_id="b.md"))
        r = self.forget(ops.LifecycleMode.REMOVE_DERIVED, targets=("m-1", "m-2"))
        self.assertEqual(r.outcome, ops.LifecycleOutcome.VERIFIED_WITH_RETENTION, r.unverifiable_surfaces)
        self.tomb = self.root / "tombstones.jsonl"
        self.assertEqual(len(self.store.tombstones()), 2)

    def test_corrupt_line_is_reappended_always_but_voided_only_with_ack(self) -> None:
        replace_line(self.tomb, 1, GARBAGE)
        code, out, _ = run(self.root, "--log", "tombstones")
        self.assertEqual(code, rp.EXIT_REFUSED, out)  # a dry run reports the refusal
        self.assertIn("memory m-1: expected 1 tombstone(s), 1 to re-append", out)
        self.assertIn("evidence complete for every receipt: yes", out)
        before = snapshot(self.root)
        run(self.root, "--log", "tombstones")
        self.assertEqual(snapshot(self.root), before)
        # --apply without the ack: exit 2; ONLY the re-append is written
        code, out, _ = run(self.root, "--log", "tombstones", "--apply", "--operation-id", "rep-t")
        self.assertEqual(code, rp.EXIT_REFUSED, out)
        self.assertFalse((self.root / "quarantine").exists())
        self.assertEqual(self.tomb.read_bytes().count(b'"memory_id":"m-1"'), 1)  # re-appended
        self.assertEqual(self.tomb.read_bytes().split(b"\n")[0], GARBAGE)  # not voided
        self.assertNotIn(b"repair_void", self.tomb.read_bytes())
        code, out, err = run(self.root, "--log", "tombstones", "--apply", "--operation-id", "rep-t",
                             "--ack-tombstone-loss", sha(GARBAGE))
        self.assertEqual(code, rp.EXIT_OK, out + err)
        fresh = ms.MemoryStore(self.root)
        self.assertTrue(fresh.is_tombstoned("file-src", SRC_OBJECT, memory_id="m-1"))
        self.assertTrue(fresh.is_tombstoned("file-src", "b.md", memory_id="m-2"))
        self.assertEqual(len(fresh.tombstones()), 2)
        self.assertEqual(
            base64.b64decode(json.loads((self.root / "quarantine" / "tombstones.rep-t.jsonl").read_bytes())["bytes_b64"]), GARBAGE
        )
        again = snapshot(self.root)
        self.assertEqual(run(self.root, "--log", "tombstones", "--apply")[0], rp.EXIT_OK)
        self.assertEqual(snapshot(self.root), again)

    def test_missing_evidenced_tombstone_is_reappended_without_any_corrupt_line(self) -> None:
        lines = self.tomb.read_bytes().split(b"\n")
        self.tomb.write_bytes(b"\n".join(lines[1:]))  # m-1's tombstone line vanished cleanly
        self.assertFalse(ms.MemoryStore(self.root).is_tombstoned("file-src", SRC_OBJECT, memory_id="m-1"))
        code, out, _ = run(self.root, "--log", "tombstones", "--apply")
        self.assertEqual(code, rp.EXIT_OK, out)
        self.assertTrue(ms.MemoryStore(self.root).is_tombstoned("file-src", SRC_OBJECT, memory_id="m-1"))

    def test_an_evidenced_missing_tombstone_does_not_license_voiding_an_unrelated_line(self) -> None:
        # the reviewer's m-9: an UNEVIDENCED tombstone whose line is corrupt, while an
        # evidenced one (m-1) happens to be missing. The count heuristic let the void through.
        self.store.add_tombstone(memory_id="m-9", source_system="file-src", object_id="c.md",
                                 operation_id="op-9", now=NOW, policy_revision="r", forgetting_policy="f")
        lines = self.tomb.read_bytes().split(b"\n")
        self.tomb.write_bytes(b"\n".join([lines[1], GARBAGE, b""]))  # drop m-1's, corrupt m-9's
        code, out, _ = run(self.root, "--log", "tombstones", "--apply")
        self.assertEqual(code, rp.EXIT_REFUSED, out)
        self.assertIn(GARBAGE, self.tomb.read_bytes())  # m-9's bytes are still there
        self.assertNotIn(b"repair_void", self.tomb.read_bytes())
        self.assertFalse((self.root / "quarantine").exists())

    def test_unprovable_corrupt_line_refuses_and_writes_nothing_then_ack_voids(self) -> None:
        for p in (self.root / "receipts").glob("*.json"):
            p.unlink()  # no evidence left
        gone = json.loads(replace_line(self.tomb, 1, GARBAGE))
        before = snapshot(self.root)
        code, out, _ = run(self.root, "--log", "tombstones", "--apply")
        self.assertEqual(code, rp.EXIT_REFUSED, out)
        self.assertIn(f"line 1 digest {sha(GARBAGE)}", out)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "quarantine").exists())
        code, out, _ = run(self.root, "--log", "tombstones", "--apply", "--ack-tombstone-loss", sha(GARBAGE))
        self.assertEqual(code, rp.EXIT_OK, out)
        self.assertIn(f"digest {sha(GARBAGE)}: tombstone loss ACKNOWLEDGED", out)
        fresh = ms.MemoryStore(self.root)
        self.assertEqual(len(fresh.tombstones()), 1)
        self.assertNotEqual(fresh.tombstones()[0]["memory_id"], gone["memory_id"])

    def test_a_wrong_ack_does_not_license_the_loss(self) -> None:
        for p in (self.root / "receipts").glob("*.json"):
            p.unlink()
        replace_line(self.tomb, 1, GARBAGE)
        code, _out, _ = run(self.root, "--log", "tombstones", "--apply", "--ack-tombstone-loss", sha(b"x"))
        self.assertEqual(code, rp.EXIT_REFUSED)

    def test_both_logs_refusal_writes_nothing_to_either(self) -> None:
        for p in (self.root / "receipts").glob("*.json"):
            p.unlink()
        admit(self.store, "m-9", "v-1", canonical_source_ref=self.live_ref(object_id="c.md"))
        replace_line(self.root / "events.jsonl", 3, GARBAGE)  # repairable alone
        replace_line(self.tomb, 1, GARBAGE)
        before = snapshot(self.root)
        code, _out, _ = run(self.root, "--apply")
        self.assertEqual(code, rp.EXIT_REFUSED)
        self.assertEqual(snapshot(self.root), before)


class TestForgottenMemoryMustNotReturn(_Case):
    def _forgotten_with_payload_left(self) -> tuple[Path, int]:
        self.seed("m-1", "v-1", forgetting_policy="forget-std")
        self.seed("m-2", "v-1", forgetting_policy="forget-std",
                  canonical_source_ref=self.live_ref(object_id="b.md"))
        pay = self.root / "payloads" / "m-1" / "v-1"
        kept = pay.read_bytes()
        r = self.forget(ops.LifecycleMode.REMOVE_DERIVED, targets=("m-1",))
        self.assertIn(r.outcome, (ops.LifecycleOutcome.VERIFIED, ops.LifecycleOutcome.VERIFIED_WITH_RETENTION))
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ())
        pay.parent.mkdir(exist_ok=True)
        pay.write_bytes(kept)  # the payload is (still / again) on disk
        events = self.root / "events.jsonl"
        for n, raw in enumerate(events.read_bytes().split(b"\n")[:-1], 1):
            if any(t.get("from") for t in json.loads(raw).get("transitions", [])):
                return events, n
        raise AssertionError("no forget transition found")

    def test_voiding_a_forget_transition_that_would_resurrect_a_memory_is_refused(self) -> None:
        events, n = self._forgotten_with_payload_left()
        replace_line(events, n, GARBAGE)
        before = snapshot(self.root)
        for args in ((), ("--apply",)):
            code, out, err = run(self.root, "--log", "events", *args)
            self.assertEqual(code, rp.EXIT_REFUSED, out)
            self.assertIn("m-1", out)
            self.assertNotIn(MARKER, out + err)
        self.assertEqual(snapshot(self.root), before)  # nothing written

    def _gap_case(self, how: str) -> tuple[Path, int]:
        events, n = self._forgotten_with_payload_left()
        for p in list((self.root / "receipts").glob("*.json")):
            if how == "garbage":
                p.write_bytes(b"{garbage")
            else:
                p.unlink()
        replace_line(events, n, GARBAGE)
        return events, n

    def test_unreadable_or_deleted_receipts_fail_closed_unless_event_loss_is_acked(self) -> None:
        for how in ("garbage", "deleted"):
            with self.subTest(how=how):
                self.setUp()
                self._gap_case(how)
                before = snapshot(self.root)
                for args in ((), ("--apply",)):
                    code, out, err = run(self.root, "--log", "events", *args)
                    self.assertEqual(code, rp.EXIT_REFUSED, out)
                    self.assertIn(f"digest {sha(GARBAGE)}: evidence incomplete; voiding needs --ack-event-loss", out)
                    self.assertNotIn(MARKER, out + err)
                self.assertEqual(snapshot(self.root), before)
                code, out, err = run(self.root, "--log", "events", "--apply", "--ack-event-loss", sha(GARBAGE))
                self.assertEqual(code, rp.EXIT_OK, out + err)
                self.assertIn(f"digest {sha(GARBAGE)}: event loss ACKNOWLEDGED (--ack-event-loss)", out)

    def test_a_memory_with_a_tombstone_is_protected_even_with_the_ack(self) -> None:
        # m-1 holds a tombstone (no receipt exists) yet is retrievable (the tombstone names
        # another object): a repair that leaves it retrievable is refused, ack or not.
        self.seed("m-1", "v-1")
        self.seed("m-2", "v-1", canonical_source_ref=self.live_ref(object_id="b.md"))
        self.store.add_tombstone(memory_id="m-1", source_system="file-src", object_id="other.md",
                                 operation_id="op-x", now=NOW, policy_revision="r", forgetting_policy="f")
        replace_line(self.root / "events.jsonl", 2, GARBAGE)
        before = snapshot(self.root)
        for extra in ((), ("--ack-event-loss", sha(GARBAGE))):
            code, out, err = run(self.root, "--log", "events", "--apply", *extra)
            self.assertEqual(code, rp.EXIT_REFUSED, out)
            self.assertIn("m-1", out)
            self.assertIn("retrievable again", out)  # the F4 check, which no ack overrides
            self.assertNotIn(MARKER, out + err)
        self.assertEqual(snapshot(self.root), before)

    def test_a_corrupt_line_that_resurrects_nothing_is_still_repairable(self) -> None:
        events, n = self._forgotten_with_payload_left()
        (self.root / "payloads" / "m-1" / "v-1").unlink()  # forgotten and gone: nothing to resurrect
        replace_line(events, n, GARBAGE)
        code, out, err = run(self.root, "--log", "events", "--apply")
        self.assertEqual(code, rp.EXIT_OK, out + err)


if __name__ == "__main__":
    unittest.main()
