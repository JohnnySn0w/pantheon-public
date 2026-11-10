"""Tests for tools/chronicle_prune.py (T-6118 PR-5): the receipt-driven Chronicle prune.

Hermetic: every tree lives under a TemporaryDirectory, the ingest uid is injected through Config
(fixture files are created by the test process, so uid == ingest_uid == os.getuid() is the good
path), alerts are routed to an injected route_fn with PANTHEON_CHRONICLE_PRUNE_ALERT=off (no test
may route a real alert), and nothing reads /var/lib/pantheon, a seat home or the real Chronicle.
The receipt shape comes from the committed, scrubbed fixture of one real receipt.
"""
from __future__ import annotations

import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import chronicle_prune as cp

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tools" / "fixtures" / "chronicle" / "ingest_receipt.v1.json"
UID = os.getuid()
SEAT = "seatx"
DATE = "2026-01-02"
UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OLD_ID = "11111111-2222-3333-4444-555555555555"
CUR_ID = "99999999-8888-7777-6666-555555555555"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(*roots: Path) -> dict:
    """lstat (ino, mode, nlink, size, mtime_ns) of every path under the roots, roots included."""
    out = {}
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            for name in [""] + dirs + files:
                p = os.path.join(dirpath, name) if name else dirpath
                st = os.lstat(p)
                out[p] = (st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns)
    return out


class Env(unittest.TestCase):
    persona = SEAT
    seat = SEAT

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        patcher = mock.patch.dict(os.environ, {cp.ALERT_ENV: "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.export = self.tmp / "export"
        self.chron = self.tmp / "chron"
        self.state = self.tmp / "state"
        self.cstate = self.tmp / "codex-appserver"
        self.chome = self.tmp / ".codex"
        self.routed: list = []
        self.template = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.n = 0
        for d in (self.export, self.export / ".ingested", self.export / ".ingested" / self.seat,
                  self.export / self.seat, self.chron, self.chron / self.persona):
            d.mkdir(parents=True, exist_ok=True)
        for d in (self.export, self.export / ".ingested", self.export / ".ingested" / self.seat):
            d.chmod(0o755)
        (self.export / self.seat).chmod(0o750)
        self.chrond = self.chron / self.persona
        self.decoys()

    # ------------------------------------------------------------------ builders
    def route(self, persona, msg, vocab):
        self.routed.append((persona, msg, vocab))

    def decoys(self, n: int = 3) -> None:
        """Three never-exported newer .md files: they fill the Chronicle's newest-3 so that the
        test records are NOT protected unless a test wants them to be."""
        d = self.chrond / "2030-01-01"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f"2030-01-01T00000{i}Z-decoy{i}.md").write_text("decoy\n")

    def clear_decoys(self) -> None:
        for p in (self.chrond / "2030-01-01").glob("*"):
            p.unlink()

    def stem(self, date: str = DATE, minute: int = 0) -> str:
        self.n += 1
        return f"{date}T03{minute:02d}{self.n:02d}.123456Z-{UUID}"

    def receipt_dir(self, date: str) -> Path:
        d = self.export / ".ingested" / self.seat / date
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o755)
        return d

    def write_receipt(self, date: str, name: str, data: bytes, **over) -> Path:
        doc = copy.deepcopy(self.template)
        doc.update({"file": f"{self.seat}/{date}/{name}", "seat": self.seat, "size": len(data),
                    "sha256": sha(data),
                    "raw_dest": f"/srv/chronicle-raw/{self.seat}/{date}/{name}"})
        doc.update(over)
        path = self.receipt_dir(date) / (name + ".receipt.json")
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        path.chmod(0o644)
        return path

    def add_record(self, *, date: str = DATE, stem: "str | None" = None,
                   kinds=("gz", "md", "session"), receipts=True, persona: "str | None" = None,
                   data: "dict | None" = None) -> dict:
        """Chronicle file + export hardlink (same inode) for each kind, plus a receipt per file."""
        stem = stem or self.stem(date)
        cdir = self.chrond / date
        edir = self.export / self.seat / date
        cdir.mkdir(parents=True, exist_ok=True)
        edir.mkdir(parents=True, exist_ok=True)
        edir.chmod(0o750)
        names = {"gz": f"{stem}.transcript.jsonl.gz", "md": f"{stem}.md",
                 "summary": f"{stem}.summary.md", "session": f"{stem}.session.json"}
        rec = {"stem": stem, "date": date, "files": {}, "receipts": {}}
        for kind in kinds:
            name = names[kind]
            if data and kind in data:
                body = data[kind]
            elif kind == "gz":
                body = gzip.compress(b'{"type":"x"}\n' * 50, mtime=0)
            elif kind == "session":
                body = json.dumps({"persona": persona or self.persona, "agent": self.persona}).encode()
            else:
                body = f"# handoff {stem} {kind}\n".encode()
            cpath = cdir / name
            cpath.write_bytes(body)
            cpath.chmod(0o640)
            epath = edir / name
            os.link(cpath, epath)
            rec["files"][kind] = {"name": name, "chron": cpath, "export": epath, "data": body}
            if receipts:
                rec["receipts"][kind] = self.write_receipt(date, name, body)
        return rec

    def cfg(self, **over) -> cp.Config:
        base = dict(persona=self.persona, seat=self.seat, uid=UID, ingest_uid=UID,
                    export_root=self.export, chronicle_root=self.chron, state_dir=self.state,
                    codex_state_dir=None, codex_home=None, execute=True, ttl_days=35)
        base.update(over)
        return cp.Config(**base)

    def run_prune(self, *, now=None, unlink=None, **over) -> cp.Result:
        kw = {"route_fn": self.route}
        if now is not None:
            kw["now"] = now
        if unlink is not None:
            kw["unlink"] = unlink
        return cp.run(self.cfg(**over), **kw)

    def gone(self, rec, kind) -> bool:
        f = rec["files"][kind]
        return not os.path.lexists(f["chron"]) and not os.path.lexists(f["export"])

    def present(self, rec, kind) -> bool:
        f = rec["files"][kind]
        return os.path.lexists(f["chron"]) and os.path.lexists(f["export"])


# ---------------------------------------------------------------------- receipt-driven deletion
class DeletionTests(Env):
    def test_matching_receipt_deletes_export_and_chronicle(self):
        rec = self.add_record()
        res = self.run_prune()
        for kind in ("gz", "md", "session"):
            self.assertTrue(self.gone(rec, kind), kind)
        self.assertEqual(res.exit, cp.EXIT_OK)
        self.assertEqual(self.routed, [])
        self.assertEqual(res.counts["pruned-files"], 3)
        # receipts are the trust anchor and are never touched
        self.assertTrue(all(p.exists() for p in rec["receipts"].values()))

    def test_sha_mismatch_keeps_and_alerts_once(self):
        rec = self.add_record()
        f = rec["files"]["gz"]
        os.chmod(f["chron"], 0o640)
        with open(f["chron"], "r+b") as h:  # same inode: both names see it; same size, other bytes
            h.write(b"\x00")
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.exit, cp.EXIT_ALERT)
        self.assertEqual(len(self.routed), 1)
        self.assertEqual(res.counts["sha-mismatch"], 1)
        res2 = self.run_prune()
        self.assertEqual(len(self.routed), 1, "a persisting condition does not re-alert")
        self.assertEqual(res2.exit, cp.EXIT_OK)
        self.assertEqual(res2.counts["sha-mismatch"], 1, "but it is still counted")

    def test_extra_hardlink_keeps_whole_record_and_alerts(self):
        """Twin present, same inode, nlink 3 (a third link elsewhere): not the export<->Chronicle pair we
        proved, so nothing in the record is deleted and the third link survives."""
        rec = self.add_record()
        third = self.tmp / "elsewhere-third-link"
        os.link(rec["files"]["gz"]["chron"], third)
        self.assertEqual(os.stat(third).st_nlink, 3)
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertTrue(third.exists())
        self.assertEqual(third.read_bytes(), rec["files"]["gz"]["data"])
        self.assertEqual(res.counts["unexpected-nlink"], 1)
        self.assertEqual(res.exit, cp.EXIT_ALERT)
        self.assertEqual(len(self.routed), 1)

    def test_size_mismatch_keeps(self):
        rec = self.add_record()
        with open(rec["files"]["md"]["chron"], "ab") as h:
            h.write(b"more")
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["size-mismatch"], 1)
        self.assertEqual(len(self.routed), 1)

    def test_receipt_not_owned_by_ingest_refused(self):
        rec = self.add_record()
        res = self.run_prune(ingest_uid=UID + 1)
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertGreaterEqual(res.counts["receipt-dir-untrusted"], 1)
        self.assertEqual(len(self.routed), 1)

    def test_group_writable_receipt_refused(self):
        rec = self.add_record()
        rec["receipts"]["md"].chmod(0o664)
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["receipt-untrusted"], 1)
        self.assertEqual(len(self.routed), 1)

    def _dir_case(self, which: str) -> None:
        rec = self.add_record()
        target = {"ingested": self.export / ".ingested",
                  "seat": self.export / ".ingested" / self.seat,
                  "date": self.export / ".ingested" / self.seat / DATE}[which]
        target.chmod(0o775)
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")), which)
        self.assertGreaterEqual(res.counts["receipt-dir-untrusted"], 1, which)
        self.assertEqual(len(self.routed), 1, which)

    def test_group_writable_receipt_dir_refused_ingested(self):
        self._dir_case("ingested")

    def test_group_writable_receipt_dir_refused_seat(self):
        self._dir_case("seat")

    def test_group_writable_receipt_dir_refused_date(self):
        self._dir_case("date")

    def test_world_writable_receipt_refused(self):
        rec = self.add_record()
        rec["receipts"]["gz"].chmod(0o646)
        self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))

    def test_symlinked_receipt_refused(self):
        rec = self.add_record()
        real = self.tmp / "elsewhere.receipt.json"
        real.write_bytes(rec["receipts"]["md"].read_bytes())
        real.chmod(0o644)
        rec["receipts"]["md"].unlink()
        os.symlink(real, rec["receipts"]["md"])
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["receipt-symlink"], 1)
        self.assertEqual(len(self.routed), 1)

    def test_symlinked_receipt_dir_refused(self):
        rec = self.add_record()
        d = self.export / ".ingested" / self.seat / DATE
        moved = self.tmp / "moved-date"
        d.rename(moved)
        os.symlink(moved, d)
        self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(len(self.routed), 1)

    def test_receipt_schema_each_field_and_type(self):
        for key in ("file", "seat", "size", "sha256", "raw_sha256_verified", "raw_dest", "memex_commit",
                    "memex_record_ids", "ingested_utc", "schema", "supersedes"):
            with self.subTest(missing=key):
                self._schema_case(lambda d, k=key: d.pop(k))
        bad = {"raw_sha256_verified": ["true", 1, None, "yes"], "size": ["12", True, -1, 1.5],
               "sha256": ["ABC", "0" * 63, 7], "memex_commit": ["abc", 5], "memex_record_ids": [[], "x", [""]],
               "schema": ["chronicle-ingest-receipt/v2", None], "ingested_utc": ["yesterday", 3],
               "seat": ["otherseat"], "file": [f"{SEAT}/{DATE}/other.md", "elsewhere"],
               "supersedes": [{}, "x", [{"file": "a"}], [{"file": "a", "size": "1", "sha256": "0" * 64}]]}
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self._schema_case(lambda d, k=key, v=value: d.__setitem__(k, v))

    def _schema_case(self, mutate) -> None:
        self.setUp()  # fresh tree per sub-case
        rec = self.add_record(kinds=("md", "session"))
        path = rec["receipts"]["md"]
        doc = json.loads(path.read_text())
        mutate(doc)
        path.write_text(json.dumps(doc))
        path.chmod(0o644)
        self.run_prune()
        self.assertTrue(self.present(rec, "md") and self.present(rec, "session"))

    def test_unknown_extra_receipt_key_is_tolerated(self):
        rec = self.add_record(kinds=("md", "session"))
        path = rec["receipts"]["md"]
        doc = json.loads(path.read_text())
        doc["future_field"] = {"x": 1}
        path.write_text(json.dumps(doc))
        path.chmod(0o644)
        self.run_prune()
        self.assertTrue(self.gone(rec, "md") and self.gone(rec, "session"))

    def test_partial_record_prunes_nothing(self):
        rec = self.add_record()
        rec["receipts"]["gz"].unlink()
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["no-receipt"], 1)
        self.assertEqual(self.routed, [], "waiting for the ingest is not an alert")
        self.assertEqual(res.exit, cp.EXIT_OK)

    def test_no_session_json_keeps_the_record(self):
        rec = self.add_record(kinds=("gz", "md"))
        res = self.run_prune()
        self.assertTrue(self.present(rec, "gz") and self.present(rec, "md"))
        self.assertEqual(res.counts["no-session-json"], 1)

    def test_summary_is_pruned_with_its_record(self):
        rec = self.add_record(kinds=("gz", "md", "summary", "session"))
        self.run_prune()
        for kind in ("gz", "md", "summary", "session"):
            self.assertTrue(self.gone(rec, kind), kind)

    def test_session_json_unlinked_last_and_chronicle_before_export(self):
        rec = self.add_record(kinds=("gz", "md", "summary", "session"))
        calls = []
        real = cp.default_unlink

        def spy(name, dir_fd):
            calls.append(name)
            return real(name, dir_fd)

        self.run_prune(unlink=spy)
        self.assertEqual(len(calls), 8)
        self.assertTrue(calls[-1].endswith(".session.json") and calls[-2].endswith(".session.json"))
        for first, second in zip(calls[0::2], calls[1::2]):
            self.assertEqual(first, second)
        order = [c for c in calls[0::2]]
        self.assertEqual(order[0].endswith(".gz"), True)
        self.assertEqual(order[-1].endswith(".session.json"), True)
        # the chronicle name goes first: after the first call of a pair the export name is still there
        rec2 = self.add_record(kinds=("md", "session"))
        seen = []

        def spy2(name, dir_fd):
            exp = rec2["files"]["md"]["export"].exists() and rec2["files"]["md"]["chron"].exists()
            seen.append((name, rec2["files"]["md"]["chron"].exists(), rec2["files"]["md"]["export"].exists()))
            return real(name, dir_fd)

        self.run_prune(unlink=spy2)
        self.assertEqual(seen[0][1:], (True, True))      # about to unlink the chronicle name
        self.assertEqual(seen[1][1:], (False, True))     # chronicle gone, export link still there

    def test_resume_after_kill_between_chronicle_and_export_unlink(self):
        rec = self.add_record()
        real = cp.default_unlink
        count = {"n": 0}

        def killing(name, dir_fd):
            count["n"] += 1
            if count["n"] == 2:  # the export half of the first file
                raise OSError(5, "simulated kill")
            return real(name, dir_fd)

        res = self.run_prune(unlink=killing)
        self.assertEqual(res.exit, cp.EXIT_ALERT)
        self.assertTrue(os.path.lexists(rec["files"]["session"]["export"]), "the session.json is still the last")
        res2 = self.run_prune()  # a healthy rerun finishes the record
        for kind in ("gz", "md", "session"):
            self.assertTrue(self.gone(rec, kind), kind)
        self.assertEqual(res2.exit, cp.EXIT_OK)

    def test_second_execute_is_noop_and_does_not_alert(self):
        rec = self.add_record()
        self.run_prune()
        first = len(self.routed)
        res = self.run_prune()
        self.assertEqual(res.exit, cp.EXIT_OK)
        self.assertEqual(res.counts["pruned-files"], 0)
        self.assertEqual(len(self.routed), first)
        self.assertTrue(self.gone(rec, "md"))

    def test_chronicle_inode_mismatch_keeps_and_alerts(self):
        rec = self.add_record()
        f = rec["files"]["md"]
        f["chron"].unlink()
        f["chron"].write_bytes(f["data"])  # same bytes, a different inode
        f["chron"].chmod(0o640)
        res = self.run_prune()
        self.assertTrue(self.present(rec, "md") and self.present(rec, "session"))
        self.assertEqual(res.counts["inode-mismatch"], 1)
        self.assertEqual(len(self.routed), 1)

    def test_chronicle_twin_absent_with_nlink_two_keeps(self):
        rec = self.add_record(kinds=("md", "session"))
        extra = self.tmp / "third-link"
        os.link(rec["files"]["md"]["export"], extra)
        rec["files"]["md"]["chron"].unlink()
        res = self.run_prune()
        self.assertTrue(os.path.lexists(rec["files"]["md"]["export"]))
        self.assertEqual(res.counts["unexpected-nlink"], 1)

    def test_symlink_in_export_dir_kept(self):
        rec = self.add_record(kinds=("md", "session"))
        planted = self.export / self.seat / DATE / f"{rec['stem']}.transcript.jsonl.gz"
        os.symlink("/etc/passwd", planted)
        res = self.run_prune()
        self.assertTrue(os.path.islink(planted))
        self.assertTrue(self.present(rec, "md") and self.present(rec, "session"))
        self.assertEqual(res.counts["unsafe-file"], 1)

    def test_foreign_persona_session_json_kept(self):
        rec = self.add_record(persona="someoneelse")
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["foreign-persona"], 1)

    def test_claim_not_done_keeps_the_record(self):
        rec = self.add_record()
        claims = self.chrond / ".claims"
        claims.mkdir()
        ts = f"{DATE}T" + rec["stem"].split("T", 1)[1].split("Z-")[0]
        ts = ts[:13] + ":" + ts[13:15] + ":" + ts[15:] + "Z"
        (claims / "k.json").write_text(json.dumps(
            {"state": "claimed", "stem_ts": ts, "stem_uuid": UUID}))
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["claim-not-done"], 1)
        (claims / "k.json").write_text(json.dumps({"state": "done", "stem_ts": ts, "stem_uuid": UUID}))
        self.run_prune()
        self.assertTrue(self.gone(rec, "md"))

    def test_unreadable_claims_keep_everything_and_alert(self):
        rec = self.add_record()
        claims = self.chrond / ".claims"
        claims.mkdir()
        (claims / "bad.json").write_text("{not json")
        res = self.run_prune()
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        self.assertEqual(res.counts["claims-unreadable"], 1)
        self.assertEqual(len(self.routed), 1)

    def test_file_changed_during_prune_keeps_the_rest(self):
        rec = self.add_record(kinds=("gz", "md", "session"))
        real = cp.default_unlink
        state = {"n": 0}

        def mutate(name, dir_fd):
            state["n"] += 1
            if state["n"] == 2:  # after the gz pair, before the md pair: the md grows
                with open(rec["files"]["md"]["chron"], "ab") as h:
                    h.write(b"x")
            return real(name, dir_fd)

        res = self.run_prune(unlink=mutate)
        self.assertTrue(self.gone(rec, "gz"))
        self.assertTrue(self.present(rec, "md") and self.present(rec, "session"))
        self.assertEqual(res.counts["changed-during-prune"], 1)
        self.assertEqual(len(self.routed), 1)


# ---------------------------------------------------------------------- supersede
class SupersedeTests(Env):
    def _pair(self):
        old = self.add_record(date="2026-01-01", receipts=False, kinds=("gz", "md", "session"))
        new = self.add_record(date="2026-01-02", kinds=("gz", "md", "session"))
        return old, new

    def _supersede(self, new, old, **over):
        old_gz = old["files"]["gz"]
        entry = {"file": f"{SEAT}/{old['date']}/{old_gz['name']}", "size": len(old_gz["data"]),
                 "sha256": sha(old_gz["data"])}
        entry.update(over)
        path = new["receipts"]["gz"]
        doc = json.loads(path.read_text())
        doc["supersedes"] = [entry]
        path.write_text(json.dumps(doc))
        path.chmod(0o644)

    def _cover_rest_of_old(self, old):
        for kind in ("md", "session"):
            self.write_receipt(old["date"], old["files"][kind]["name"], old["files"][kind]["data"])

    def test_superseded_copy_pruned_via_exact_size_and_sha(self):
        old, new = self._pair()
        self._cover_rest_of_old(old)
        self._supersede(new, old)
        self.run_prune()
        self.assertTrue(self.gone(old, "gz"))
        self.assertTrue(self.gone(old, "md") and self.gone(old, "session"))

    def test_superseded_copy_with_other_bytes_kept(self):
        old, new = self._pair()
        self._cover_rest_of_old(old)
        self._supersede(new, old, sha256="0" * 64)
        res = self.run_prune()
        self.assertTrue(self.present(old, "gz"))
        self.assertEqual(res.counts["supersede-mismatch"], 1)

    def test_supersede_entry_from_invalid_receipt_ignored(self):
        old, new = self._pair()
        self._cover_rest_of_old(old)
        self._supersede(new, old)
        new["receipts"]["gz"].chmod(0o666)  # the receipt carrying the entry is untrusted
        self.run_prune()
        self.assertTrue(self.present(old, "gz"))

    def test_supersede_entry_naming_another_seat_ignored(self):
        old, new = self._pair()
        self._cover_rest_of_old(old)
        self._supersede(new, old, file=f"otherseat/{old['date']}/{old['files']['gz']['name']}")
        self.run_prune()
        self.assertTrue(self.present(old, "gz"))


# ---------------------------------------------------------------------- keep-3, ttl, tmp sweep
class KeepTtlTmpTests(Env):
    def test_newest_three_md_kept_export_link_removed(self):
        self.clear_decoys()
        recs = [self.add_record(date=f"2026-01-0{i}") for i in range(1, 6)]
        self.run_prune()
        for i, rec in enumerate(recs):
            md = rec["files"]["md"]
            self.assertFalse(os.path.lexists(md["export"]), "export links are removed regardless")
            self.assertTrue(self.gone(rec, "gz") and self.gone(rec, "session"))
            self.assertEqual(os.path.lexists(md["chron"]), i >= 2, f"record {i}")
        # the kept .md has one link left and its bytes are intact
        kept = recs[4]["files"]["md"]
        self.assertEqual(os.stat(kept["chron"]).st_nlink, 1)
        self.assertEqual(kept["chron"].read_bytes(), kept["data"])

    def test_keep3_listing_failure_protects_every_md_and_alerts(self):
        rec = self.add_record()
        with mock.patch.object(cp, "protected_md_set", return_value=None):
            res = self.run_prune()
        self.assertTrue(os.path.lexists(rec["files"]["md"]["chron"]))
        self.assertEqual(res.counts["keep3-unknown"], 1)
        self.assertEqual(len(self.routed), 1)

    def test_ttl_alerts_and_never_deletes(self):
        rec = self.add_record(kinds=("gz", "md", "session"), receipts=False)
        young = self.run_prune()
        self.assertEqual(self.routed, [])
        self.assertEqual(young.exit, cp.EXIT_OK)
        later = datetime.now(timezone.utc) + timedelta(days=36)
        old = self.run_prune(now=later)
        self.assertEqual(len(self.routed), 1)
        self.assertEqual(old.exit, cp.EXIT_ALERT)
        self.assertEqual(old.counts["ttl"], 3)
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))
        again = self.run_prune(now=later)
        self.assertEqual(len(self.routed), 1, "once, not daily")
        self.assertEqual(again.exit, cp.EXIT_OK)
        week = self.run_prune(now=later + timedelta(days=8))
        self.assertEqual(len(self.routed), 2, "re-alerts after the repeat window")
        self.assertTrue(week.exit == cp.EXIT_ALERT)

    def _tmp(self, name, *, age_s=7200, date=DATE, nlink_extra=False):
        d = self.chrond / date
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(b"partial")
        t = datetime.now().timestamp() - age_s
        os.utime(p, (t, t))
        return p

    def test_tmp_sweep_pattern_and_age(self):
        old = self._tmp(f".{DATE}T030405Z-{UUID}.transcript.jsonl.gz.tmp-12345")
        counter = self._tmp(f".{DATE}T030405Z-{UUID}.transcript.jsonl.2.gz.tmp-7")
        young = self._tmp(f".{DATE}T030406Z-{UUID}.transcript.jsonl.gz.tmp-1", age_s=60)
        res = self.run_prune()
        self.assertFalse(old.exists() or counter.exists())
        self.assertTrue(young.exists())
        self.assertEqual(res.counts["tmp-swept"], 2)
        self.assertEqual(res.counts["tmp-young"], 1)

    def test_tmp_lookalikes_not_swept(self):
        names = [f"{DATE}T030405Z-{UUID}.transcript.jsonl.gz.tmp-1",       # no leading dot
                 f".{DATE}T030405Z-{UUID}.transcript.jsonl.gz.tmp-",        # no pid
                 f".{DATE}T030405Z-{UUID}.transcript.jsonl.gz.tmp-1.bak",
                 f".{DATE}T030405Z-{UUID}.pane.log.gz.tmp-1",
                 f".{DATE}T030405Z-{UUID}.session.json.tmp-1",
                 f".{DATE}T030405Z-{UUID}.transcript.jsonl.gz"]
        paths = [self._tmp(n) for n in names]
        link_target = self.tmp / "victim"
        link_target.write_text("v")
        link = self.chrond / DATE / f".{DATE}T030499Z-{UUID}.transcript.jsonl.gz.tmp-9"
        os.symlink(link_target, link)
        directory = self.chrond / DATE / f".{DATE}T030498Z-{UUID}.transcript.jsonl.gz.tmp-8"
        directory.mkdir()
        self.run_prune()
        self.assertTrue(all(p.exists() for p in paths))
        self.assertTrue(os.path.islink(link) and link_target.exists() and directory.is_dir())

    def test_tmp_with_extra_hardlink_not_swept(self):
        p = self._tmp(f".{DATE}T030405Z-{UUID}.transcript.jsonl.gz.tmp-5")
        os.link(p, self.tmp / "other-name")
        self.run_prune()
        self.assertTrue(p.exists())


# ---------------------------------------------------------------------- codex thread history
class CodexTests(Env):
    persona = "codex"
    seat = "cairnx"

    def setUp(self):
        super().setUp()
        self.hist = self.cstate / "thread-history"
        self.hist.mkdir(parents=True)
        (self.cstate / "thread.json").write_text(json.dumps({"thread_id": CUR_ID}))
        self.sessions = self.chome / "sessions" / "2026" / "01" / "02"
        self.sessions.mkdir(parents=True)

    def cfg(self, **over):
        over.setdefault("codex_state_dir", self.cstate)
        over.setdefault("codex_home", self.chome)
        return super().cfg(**over)

    def entry(self, stamp, old_id, *, rollout_data=b'{"r":1}\n' * 20, archive_receipt=True, **over):
        """One thread-history entry: rollout file + the archive record (gz, md, session) it points at."""
        rollout = self.sessions / f"rollout-2026-01-02T00-00-00-{old_id}.jsonl"
        rollout.write_bytes(rollout_data)
        rec = self.add_record(date=DATE, receipts=False)
        gz = rec["files"]["gz"]
        if archive_receipt:
            self.write_receipt(DATE, gz["name"], gz["data"])
        doc = {"old_thread_id": old_id, "record": {"thread_id": old_id}, "rollout": str(rollout),
               "archive": {"path": str(gz["chron"]), "bytes": len(gz["data"]), "sha256": sha(gz["data"]),
                           "source_bytes": len(rollout_data), "source_sha256": sha(rollout_data)},
               "rotated_at": "2026-01-02T00:00:00+00:00"}
        doc.update(over)
        path = self.hist / f"{stamp}-{old_id}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path, rollout, rec

    def test_old_entry_pruned_newest_never(self):
        old_path, old_rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        new_path, new_rollout, _ = self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        res = self.run_prune()
        self.assertFalse(old_rollout.exists())
        self.assertTrue(new_rollout.exists(), "the newest entry is what --rollback restores")
        stamped = json.loads(old_path.read_text())
        self.assertIn("rollout_pruned_at", stamped)
        self.assertNotIn("rollout_pruned_at", json.loads(new_path.read_text()))
        self.assertEqual(res.counts["codex-rollout-pruned"], 1)

    def test_single_entry_is_newest_and_kept(self):
        _p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.run_prune()
        self.assertTrue(rollout.exists())

    def test_current_thread_rollout_protected(self):
        old_path, rollout, _ = self.entry("20260101T000000Z", CUR_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["rollout-protected"], 1)

    def test_renamed_entries_protect_their_rollout(self):
        p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        p.rename(p.with_name(p.name + ".rolledback"))
        # a live candidate that names the same rollout: only the renamed entry's claim protects it
        self.entry("20260102T000000Z", OLD_ID)
        self.entry("20260103T000000Z", "44444444-2222-3333-4444-555555555555")
        self.run_prune()
        self.assertTrue(rollout.exists())

    def test_archive_receipt_must_match_the_archive_pointer(self):
        _p, rollout, rec = self.entry("20260101T000000Z", OLD_ID, archive_receipt=False)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["archive-receipt-missing"], 1)
        # a receipt whose bytes differ from the pointer does not count either
        gz = rec["files"]["gz"]
        self.write_receipt(DATE, gz["name"], b"other bytes")
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["archive-receipt-mismatch"], 1)

    def test_pointer_without_source_sha256_keeps_count_only(self):
        """Same size but no recorded source_sha256: bytes cannot be proven equal, so KEEP (no alert)."""
        p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        doc = json.loads(p.read_text())
        del doc["archive"]["source_sha256"]
        p.write_text(json.dumps(doc), encoding="utf-8")
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["rollout-sha-not-recorded"], 1)
        self.assertEqual(self.routed, [])
        self.assertNotIn("rollout_pruned_at", json.loads(p.read_text()))

    def test_unverified_archive_receipt_keeps(self):
        _p, rollout, rec = self.entry("20260101T000000Z", OLD_ID)
        gz = rec["files"]["gz"]
        self.write_receipt(DATE, gz["name"], gz["data"], raw_sha256_verified=False)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        self.run_prune()
        self.assertTrue(rollout.exists())

    def test_rollout_size_and_sha_mismatch_keep(self):
        _p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        rollout.write_bytes(rollout.read_bytes() + b"x")
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["rollout-size-mismatch"], 1)
        rollout.write_bytes(b"y" * (len(rollout.read_bytes()) - 1))
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["rollout-sha-mismatch"], 1)

    def test_already_absent_rollout_is_stamped_with_a_note(self):
        old_path, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        rollout.unlink()
        self.run_prune()
        doc = json.loads(old_path.read_text())
        self.assertIn("rollout_pruned_at", doc)
        self.assertEqual(doc["rollout_pruned_note"], "rollout already absent")

    def test_stamp_is_atomic_temp_and_rename(self):
        old_path, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        before = old_path.read_bytes()
        with mock.patch.object(cp.os, "replace", side_effect=OSError(5, "boom")):
            res = self.run_prune()
        self.assertEqual(old_path.read_bytes(), before, "a failed stamp leaves the entry whole")
        self.assertEqual([p.name for p in self.hist.iterdir() if p.name.startswith(".")], [],
                         "and no temp file is left behind")
        self.assertEqual(res.exit, cp.EXIT_ALERT)
        res = self.run_prune()  # rerun: the rollout is gone and unstamped -> stamped with the note
        self.assertIn("rollout_pruned_at", json.loads(old_path.read_text()))
        self.assertEqual(res.exit, cp.EXIT_OK)

    def test_unreadable_history_file_disables_the_phase(self):
        _p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        (self.hist / "20260103T000000Z-bad.json").write_text("{nope")
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["history-unreadable"], 1)

    def test_missing_thread_json_skips_the_phase(self):
        _p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        (self.cstate / "thread.json").unlink()
        res = self.run_prune()
        self.assertTrue(rollout.exists())
        self.assertEqual(res.counts["codex-thread-unknown"], 1)

    def test_rollout_outside_codex_sessions_never_touched(self):
        outside = self.tmp / "precious.jsonl"
        outside.write_bytes(b"keep")
        _p, _r, _ = self.entry("20260101T000000Z", OLD_ID, rollout=str(outside))
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        res = self.run_prune()
        self.assertTrue(outside.exists())
        self.assertEqual(res.counts["rollout-path-unsafe"], 1)

    def test_symlinked_rollout_not_followed(self):
        p, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        target = self.tmp / "victim.jsonl"
        target.write_bytes(rollout.read_bytes())
        rollout.unlink()
        os.symlink(target, rollout)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        res = self.run_prune()
        self.assertTrue(target.exists())
        self.assertEqual(res.counts["rollout-path-unsafe"], 1)

    def test_dry_run_does_not_stamp_or_delete(self):
        old_path, rollout, _ = self.entry("20260101T000000Z", OLD_ID)
        self.entry("20260102T000000Z", "33333333-2222-3333-4444-555555555555")
        before = old_path.read_bytes()
        res = self.run_prune(execute=False)
        self.assertTrue(rollout.exists())
        self.assertEqual(old_path.read_bytes(), before)
        self.assertEqual(res.counts["codex-rollout-pruned"], 0)
        self.assertEqual(res.counts["codex-rollout-would-prune"], 1)

    def test_codex_home_symlink_refused(self):
        self.entry("20260101T000000Z", OLD_ID)
        real = self.tmp / "real-codex"
        self.chome.rename(real)
        os.symlink(real, self.chome)
        res = self.run_prune()
        self.assertEqual(res.exit, cp.EXIT_REFUSED)


# ---------------------------------------------------------------------- dry run, refusals, CLI
class AgedOutKeep3Tests(Env):
    """A .md kept locally by the newest-3 rule has lost its export link and Chronicle session.json;
    once it ages out of the top 3 only the receipt can release it."""

    def aged(self, date="2026-01-01", **kw):
        rec = self.add_record(date=date, kinds=("md",), **kw)
        os.unlink(rec["files"]["md"]["export"])  # what the main path left behind
        return rec

    def alive(self, rec):
        return os.path.lexists(rec["files"]["md"]["chron"])

    def test_aged_out_md_with_matching_receipt_deleted_on_execute(self):
        rec = self.aged()
        res = self.run_prune()
        self.assertFalse(self.alive(rec))
        self.assertEqual(res.counts["aged-out-keep3"], 1)
        self.assertEqual(res.exit, cp.EXIT_OK)
        self.assertEqual(self.routed, [])
        self.assertTrue(rec["receipts"]["md"].exists())
        manifest = res.manifest.read_text()
        self.assertIn("aged-out-keep3", manifest)
        self.assertIn('"side": "chronicle"', manifest)
        self.assertIn('"phase": "done"', manifest)
        again = self.run_prune()  # idempotent
        self.assertEqual(again.exit, cp.EXIT_OK)
        self.assertEqual(again.counts["aged-out-keep3"], 0)

    def test_dry_run_lists_and_deletes_nothing(self):
        rec = self.aged()
        res = self.run_prune(execute=False)
        self.assertTrue(self.alive(rec))
        self.assertEqual(res.counts["would-prune-files"], 1)
        manifest = res.manifest.read_text()
        self.assertIn("would-delete", manifest)
        self.assertIn("aged-out-keep3", manifest)

    def test_still_in_newest_three_kept(self):
        self.clear_decoys()
        recs = [self.aged(date=f"2026-01-0{i}") for i in range(1, 6)]
        self.run_prune()
        self.assertEqual([self.alive(r) for r in recs], [False, False, True, True, True])

    def test_unknown_newest_three_set_does_nothing(self):
        rec = self.aged()
        with mock.patch.object(cp, "protected_md_set", return_value=None):
            self.run_prune()
        self.assertTrue(self.alive(rec))

    def test_mismatch_kept_and_alerts_once(self):
        rec = self.aged()
        with open(rec["files"]["md"]["chron"], "ab") as h:
            h.write(b"tampered")
        res = self.run_prune()
        self.assertTrue(self.alive(rec))
        self.assertEqual(res.exit, cp.EXIT_ALERT)
        self.assertEqual(len(self.routed), 1)
        self.run_prune()
        self.assertEqual(len(self.routed), 1)

    def test_invalid_receipt_kept_and_alerts(self):
        rec = self.aged()
        rec["receipts"]["md"].chmod(0o666)
        self.run_prune()
        self.assertTrue(self.alive(rec))
        self.assertEqual(len(self.routed), 1)

    def test_unverified_raw_receipt_kept(self):
        rec = self.aged()
        data = rec["files"]["md"]["data"]
        self.write_receipt(rec["date"], rec["files"]["md"]["name"], data,
                           raw_sha256_verified=False)
        self.run_prune()
        self.assertTrue(self.alive(rec))
        self.assertEqual(len(self.routed), 1)

    def test_missing_receipt_kept_without_alert(self):
        rec = self.aged(receipts=False)
        res = self.run_prune()
        self.assertTrue(self.alive(rec))
        self.assertEqual(self.routed, [])
        self.assertEqual(res.exit, cp.EXIT_OK)

    def test_supersede_entry_does_not_cover_this_phase(self):
        rec = self.aged(receipts=False)
        f = rec["files"]["md"]
        other = self.add_record(date="2026-01-03", kinds=("md",))
        self.write_receipt("2026-01-03", other["files"]["md"]["name"], other["files"]["md"]["data"],
                           supersedes=[{"file": f"{self.seat}/{rec['date']}/{f['name']}",
                                        "size": len(f["data"]), "sha256": sha(f["data"])}])
        self.run_prune()
        self.assertTrue(self.alive(rec))

    def test_chronicle_session_json_present_kept(self):
        rec = self.aged()
        sj = self.chrond / rec["date"] / (rec["stem"] + ".session.json")
        sj.write_text("{}")
        self.run_prune()
        self.assertTrue(self.alive(rec))

    def test_export_name_present_is_left_to_the_export_path(self):
        rec = self.add_record(kinds=("md",))  # export link still there, nlink 2
        before = os.stat(rec["files"]["md"]["chron"])
        res = self.run_prune()
        self.assertEqual(res.counts["aged-out-keep3"], 0)
        self.assertEqual(os.stat(rec["files"]["md"]["chron"]).st_ino, before.st_ino)

    def test_claim_not_done_kept(self):
        rec = self.aged()
        claims = self.chrond / ".claims"
        claims.mkdir()
        t = rec["stem"].split("T", 1)[1].split("Z-")[0]
        ts = f"{rec['date']}T{t[:2]}:{t[2:4]}:{t[4:]}Z"
        (claims / "k.json").write_text(json.dumps({"state": "claimed", "stem_ts": ts, "stem_uuid": UUID}))
        self.run_prune()
        self.assertTrue(self.alive(rec))

    def test_summary_md_symlink_and_extra_link_are_not_candidates(self):
        rec = self.aged()
        summ = self.chrond / rec["date"] / (rec["stem"] + ".summary.md")
        summ.write_text("s")
        link = self.chrond / rec["date"] / "2026-01-01T000000Z-sym.md"
        link.symlink_to(rec["files"]["md"]["chron"])
        extra = self.chrond / rec["date"] / "extra-link.md"
        os.link(rec["files"]["md"]["chron"], extra)  # nlink 2 now
        self.run_prune()
        self.assertTrue(summ.exists() and os.path.lexists(link) and extra.exists())
        self.assertTrue(self.alive(rec))

    def test_changed_during_prune_kept(self):
        rec = self.aged()
        path = rec["files"]["md"]["chron"]

        def sneaky(name, dir_fd):
            raise AssertionError("must not unlink")
        real = cp.Pruner._unchanged

        def tamper(self_, m, dfd, nlink):
            with open(path, "ab") as h:
                h.write(b"!")
            return real(self_, m, dfd, nlink)
        with mock.patch.object(cp.Pruner, "_unchanged", tamper):
            self.run_prune(unlink=sneaky)
        self.assertTrue(self.alive(rec))
        self.assertEqual(len(self.routed), 1)


class DryRunAndRefusalTests(Env):
    def _populate(self):
        """Every keep/prune case present at once, so the dry-run snapshot is meaningful."""
        self.clear_decoys()
        self.prunable = self.add_record(date="2026-01-01")
        self.partial = self.add_record(date="2026-01-02")
        self.partial["receipts"]["gz"].unlink()
        self.mismatch = self.add_record(date="2026-01-03")
        with open(self.mismatch["files"]["md"]["chron"], "ab") as h:
            h.write(b"x")
        self.unreceipted = self.add_record(date="2026-01-04", receipts=False)
        for i in range(4):
            self.add_record(date=f"2026-01-1{i}")  # the newest three keep their .md
        self.aged = self.add_record(date="2025-12-30", kinds=("md",))
        os.unlink(self.aged["files"]["md"]["export"])
        self.tmp_old = self.chrond / "2026-01-04" / f".2026-01-04T000000Z-{UUID}.transcript.jsonl.gz.tmp-3"
        self.tmp_old.write_bytes(b"x")
        t = datetime.now().timestamp() - 7200
        os.utime(self.tmp_old, (t, t))
        # codex history
        self.cstate.mkdir(exist_ok=True)
        (self.cstate / "thread-history").mkdir(exist_ok=True)
        (self.cstate / "thread.json").write_text(json.dumps({"thread_id": CUR_ID}))
        sess = self.chome / "sessions" / "2026" / "01" / "02"
        sess.mkdir(parents=True, exist_ok=True)
        gz = self.prunable["files"]["gz"]
        for stamp, tid in (("20260101T000000Z", OLD_ID), ("20260102T000000Z", "33333333-2222-3333-4444-555555555555")):
            rollout = sess / f"rollout-2026-01-02T00-00-00-{tid}.jsonl"
            rollout.write_bytes(b"r" * 100)
            (self.cstate / "thread-history" / f"{stamp}-{tid}.json").write_text(json.dumps({
                "old_thread_id": tid, "record": {}, "rollout": str(rollout),
                "archive": {"path": str(gz["chron"]), "bytes": len(gz["data"]), "sha256": sha(gz["data"]),
                            "source_bytes": 100, "source_sha256": sha(b"r" * 100)},
                "rotated_at": "2026-01-02T00:00:00+00:00"}))

    def test_dry_run_performs_zero_writes_under_the_trees(self):
        self._populate()
        trees = (self.export, self.chron, self.cstate, self.chome)
        before = snapshot(*trees)
        for now in (None, datetime.now(timezone.utc) + timedelta(days=40)):  # also with the TTL leg live
            res = self.run_prune(execute=False, codex_state_dir=self.cstate, codex_home=self.chome,
                                 **({} if now is None else {"now": now}))
            self.assertEqual(snapshot(*trees), before, "a dry run must not touch the export/Chronicle/codex trees")
            self.assertEqual(res.counts["pruned-files"], 0)
            self.assertGreater(res.counts["would-prune-files"], 0)
        # its only writes: the manifest and the alert state under --state-dir
        written = sorted(p.name for p in (self.state / "chronicle-prune").iterdir())
        self.assertIn("alert-state.json", written)
        self.assertTrue(any(n.endswith(".jsonl") for n in written))
        manifest = next((self.state / "chronicle-prune").glob("*.jsonl")).read_text()
        self.assertIn("would-delete", manifest)
        self.assertNotIn('"phase": "done"', manifest)

    def test_execute_does_prune_the_same_tree(self):
        self._populate()
        self.run_prune(execute=True, codex_state_dir=self.cstate, codex_home=self.chome)
        self.assertTrue(self.gone(self.prunable, "gz"))
        self.assertFalse(self.tmp_old.exists())
        self.assertTrue(self.present(self.partial, "md"))
        self.assertFalse(self.aged["files"]["md"]["chron"].exists())

    def test_summary_line_carries_keep_counts_by_reason(self):
        self.add_record(receipts=False)
        part = self.add_record(date="2026-01-03")
        part["receipts"]["gz"].unlink()
        res = self.run_prune(execute=False)
        line = res.lines[0]
        self.assertIn("kept-no-receipt=", line)
        self.assertIn("kept-partial-record=2", line)
        self.assertEqual(res.exit, cp.EXIT_OK)

    def _cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cp, "load_ingest_uid", return_value=UID), \
                mock.patch.object(cp.chronicle_export, "resolve_seat", return_value=(self.seat, None)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cp.main(argv, route_fn=self.route)
        return code, out.getvalue(), err.getvalue()

    def _argv(self, *extra, export=None):
        return ["--persona", self.persona, "--export-root", str(export or self.export),
                "--chronicle-root", str(self.chron), "--state-dir", str(self.state),
                "--codex-state-dir", str(self.tmp / "no-codex-state"),
                "--codex-home", str(self.tmp / "no-codex-home"), *extra]

    @unittest.skipIf(UID == 0, "the tool refuses to run as root")
    def test_unflagged_run_is_dry_run_and_says_so(self):
        rec = self.add_record()
        code, out, _err = self._cli(self._argv())
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN (no --execute given): nothing will be deleted", out)
        self.assertTrue(all(self.present(rec, k) for k in ("gz", "md", "session")))

    @unittest.skipIf(UID == 0, "the tool refuses to run as root")
    def test_explicit_dry_run_flag_and_execute_flag(self):
        rec = self.add_record()
        code, out, _ = self._cli(self._argv("--dry-run"))
        self.assertEqual(code, 0)
        self.assertTrue(self.present(rec, "md"))
        code, out, _ = self._cli(self._argv("--execute"))
        self.assertEqual(code, 0)
        self.assertNotIn("DRY-RUN", out)
        self.assertTrue(self.gone(rec, "md"))

    def test_dry_run_and_execute_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cp.main(self._argv("--dry-run", "--execute"))
        self.assertEqual(ctx.exception.code, 2)

    @unittest.skipIf(UID == 0, "the tool refuses to run as root")
    def test_symlinked_export_root_refused(self):
        real = self.tmp / "real-export"
        self.export.rename(real)
        os.symlink(real, self.export)
        code, _out, err = self._cli(self._argv("--execute"))
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)

    @unittest.skipIf(UID == 0, "the tool refuses to run as root")
    def test_symlinked_export_root_ancestor_refused(self):
        real = self.tmp / "real-parent"
        real.mkdir()
        self.export.rename(real / "export")
        link = self.tmp / "link-parent"
        os.symlink(real, link)
        code, _o, _e = self._cli(self._argv("--execute", export=link / "export"))
        self.assertEqual(code, 2)

    @unittest.skipIf(UID == 0, "the tool refuses to run as root")
    def test_symlinked_chronicle_root_refused(self):
        real = self.tmp / "real-chron"
        self.chron.rename(real)
        os.symlink(real, self.chron)
        code, _o, _e = self._cli(self._argv("--execute"))
        self.assertEqual(code, 2)

    def test_symlinked_seat_dir_refused(self):
        seat = self.export / self.seat
        real = self.tmp / "real-seat"
        seat.rename(real)
        os.symlink(real, seat)
        res = self.run_prune()
        self.assertEqual(res.exit, cp.EXIT_REFUSED)

    def test_seat_dir_not_owned_by_running_uid_refused(self):
        rec = self.add_record()
        res = self.run_prune(uid=UID + 1)
        self.assertEqual(res.exit, cp.EXIT_REFUSED)
        self.assertTrue(self.present(rec, "md"))

    def test_missing_seat_dir_refused(self):
        (self.export / self.seat).rmdir()
        self.assertEqual(self.run_prune().exit, cp.EXIT_REFUSED)

    def test_root_is_refused(self):
        with mock.patch.object(cp.os, "geteuid", return_value=0):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cp.main(self._argv("--execute")), 2)

    def test_invalid_persona_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cp.main(["--persona", "../x", "--export-root", str(self.export)]), 2)

    def test_alert_env_off_without_route_fn_routes_nothing(self):
        self.add_record()
        rec = self.add_record(kinds=("md", "session"))
        rec["receipts"]["md"].chmod(0o666)
        with mock.patch("tools.inbox_consumer.route_failure_alert") as real_route:
            res = cp.run(self.cfg(), now=None)
        real_route.assert_not_called()
        self.assertEqual(res.exit, cp.EXIT_OK)

    def test_module_does_not_import_persona_installer(self):
        code = ("import sys; import tools.chronicle_prune; "
                "sys.exit(1 if 'tools.persona_installer' in sys.modules else 0)")
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_fixture_is_a_v1_receipt_with_the_full_key_set(self):
        doc = json.loads(FIXTURE.read_text())
        self.assertEqual(sorted(doc), sorted([
            "file", "ingested_utc", "memex_commit", "memex_record_ids", "raw_dest",
            "raw_sha256_verified", "schema", "seat", "sha256", "size", "supersedes"]))
        self.assertEqual(doc["schema"], cp.RECEIPT_SCHEMA)


class IngestAccountTests(unittest.TestCase):
    def test_ingest_uid_comes_from_instance_yaml_account(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "instance.yaml").write_text(
                "persona_installer:\n  chronicle_export:\n    root: /x\n    group: g\n    ingest_account: ingester\n")
            fake = mock.Mock(pw_uid=4242)
            self.assertEqual(cp.load_ingest_uid(root, getpwnam=lambda n: fake if n == "ingester" else 1 / 0), 4242)
            with self.assertRaises(cp.Refused):
                cp.load_ingest_uid(root, getpwnam=mock.Mock(side_effect=KeyError("nope")))
            (root / "instance.yaml").write_text("persona_installer: {}\n")
            with self.assertRaises(cp.Refused):
                cp.load_ingest_uid(root, getpwnam=lambda n: fake)
            (root / "instance.yaml").write_text(
                "persona_installer:\n  chronicle_export:\n    ingest_account: '../evil'\n")
            with self.assertRaises(cp.Refused):
                cp.load_ingest_uid(root, getpwnam=lambda n: fake)

    def test_the_live_manifest_names_an_ingest_account(self):
        doc = cp.load_instance_document(REPO / "instance.yaml")
        self.assertTrue(doc["persona_installer"]["chronicle_export"]["ingest_account"])


if __name__ == "__main__":
    unittest.main()
