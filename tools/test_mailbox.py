"""test_mailbox.py — ORG-026 file-mailbox core (tools/mailbox.py, T-0772).

Each test maps to an ORG-026 acceptance property:
  - atomic delivery (lands in new/, never tmp/)            §Decision 1
  - unique + lexically-sortable names                      §Decision 1
  - readdir(new/) is the truth (no event needed)           §Decision 3 / REQ2
  - payload round-trips through consume                    transport correctness
  - idempotent at-least-once ack (handler retry)           §Decision 3
  - per-inbox single-consumer lease serializes consumers   §Decision (lease reuse)
  - OFFLINE-SURVIVAL: send with no consumer, later consume  T-0772 PoC acceptance
  - handler error leaves message in new/ (no silent drop)
  - malformed file recorded, not dropped
  - sender validation (name can't inject a path/delimiter)
  - recipient sink validation (path can't traverse/inject)  T-0906 defense-in-depth
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # bare-import the SUT
import mailbox  # noqa: E402
from _lease import Lease  # noqa: E402


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"

    def tearDown(self):
        self._tmp.cleanup()

    # --------------------------------------------------------------- delivery
    def test_send_lands_in_new_not_tmp(self):
        mid = mailbox.send(self.inbox, {"hello": "world"}, sender="orchestrator")
        self.assertTrue((self.inbox / "new" / mid).exists(), "message must be in new/")
        self.assertEqual([], list((self.inbox / "tmp").iterdir()), "tmp/ must be empty")

    def test_names_unique(self):
        ids = {mailbox.send(self.inbox, {"i": i}, sender="recovery") for i in range(200)}
        self.assertEqual(len(ids), 200, "every message name must be unique")

    def test_names_sort_chronologically(self):
        ids = [mailbox.send(self.inbox, {"i": i}, sender="specialist") for i in range(20)]
        self.assertEqual(ids, sorted(ids), "lexical sort must equal send order")

    # --------------------------------------------------------------- consume
    def test_payload_round_trips(self):
        payload = {"task": "T-0767", "nums": [1, 2, 3], "nested": {"k": "v"}}
        mailbox.send(self.inbox, payload, sender="orchestrator", subject="cutover")
        got = []
        res = mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertFalse(res["skipped"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["payload"], payload)
        self.assertEqual(got[0]["sender"], "orchestrator")
        self.assertEqual(got[0]["subject"], "cutover")

    def test_consume_acks_by_move_to_cur(self):
        mid = mailbox.send(self.inbox, {"x": 1}, sender="recovery")
        mailbox.consume(self.inbox, lambda m: None)
        self.assertFalse((self.inbox / "new" / mid).exists(), "acked msg leaves new/")
        self.assertTrue((self.inbox / "cur" / mid).exists(), "acked msg lands in cur/")

    def test_readdir_is_truth(self):
        # Drop a well-formed message file straight into new/ with NO send() event —
        # consume must still find it (REQ2: readdir(new/) is authoritative).
        mailbox.provision(self.inbox)
        import json
        name = "00000000000000000001.999.000000.handplaced"
        (self.inbox / "new" / name).write_text(
            json.dumps({"id": name, "sender": "x", "payload": {"manual": True}}) + "\n"
        )
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["payload"]["manual"])

    def test_offline_survival(self):
        # The headline T-0772 acceptance: send while NOTHING is consuming (target
        # killed/capped), then consume later — the message must survive on disk.
        mid = mailbox.send(self.inbox, {"urgent": "resume T-0772"}, sender="orchestrator")
        self.assertTrue((self.inbox / "new" / mid).exists())
        got = []
        res = mailbox.consume(self.inbox, lambda m: got.append(m["payload"]))
        self.assertEqual(got, [{"urgent": "resume T-0772"}])
        self.assertFalse(res["skipped"])

    # --------------------------------------------------- at-least-once / retry
    def test_handler_failure_leaves_message_for_retry(self):
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")
        calls = {"n": 0}

        def flaky(_m):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")

        res1 = mailbox.consume(self.inbox, flaky)
        self.assertEqual(res1["processed"], [])
        self.assertEqual(len(res1["errors"]), 1)
        self.assertTrue((self.inbox / "new" / mid).exists(), "failed msg stays in new/")

        res2 = mailbox.consume(self.inbox, flaky)  # idempotent retry
        self.assertEqual(res2["processed"], [mid])
        self.assertTrue((self.inbox / "cur" / mid).exists(), "retry acks to cur/")

    def test_malformed_file_recorded_not_dropped(self):
        mailbox.provision(self.inbox)
        bad = self.inbox / "new" / "00000000000000000002.1.0.x"
        bad.write_text("this is not json\n")
        res = mailbox.consume(self.inbox, lambda m: None)
        self.assertEqual(res["processed"], [])
        self.assertEqual(len(res["errors"]), 1)
        self.assertTrue(bad.exists(), "unparseable msg left in new/, not silently dropped")

    # ------------------------------------------------------------------- lease
    def test_lease_serializes_concurrent_consumers(self):
        mailbox.send(self.inbox, {"x": 1}, sender="recovery")
        # Hold the per-inbox consumer lease, then try to consume — it must back off.
        holder = Lease(self.inbox / ".consumer.lock")
        self.assertTrue(holder.acquire())
        try:
            res = mailbox.consume(self.inbox, lambda m: self.fail("must not drain"))
            self.assertTrue(res["skipped"], "a second consumer must skip while lease held")
            self.assertEqual(res["processed"], [])
        finally:
            holder.release()
        # Once the lease is free, consume drains normally.
        got = []
        res2 = mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertFalse(res2["skipped"])
        self.assertEqual(len(got), 1)

    def test_use_lease_false_skips_lease(self):
        mailbox.send(self.inbox, {"x": 1}, sender="recovery")
        holder = Lease(self.inbox / ".consumer.lock")
        self.assertTrue(holder.acquire())
        try:
            got = []
            res = mailbox.consume(self.inbox, lambda m: got.append(m), use_lease=False)
            self.assertFalse(res["skipped"], "use_lease=False bypasses the lease")
            self.assertEqual(len(got), 1)
        finally:
            holder.release()

    # ------------------------------------------------------------- validation
    def test_invalid_sender_rejected(self):
        for bad in ["", "a/b", "a b", ".hidden", "a.b", "x;y"]:
            with self.assertRaises(ValueError, msg=f"sender {bad!r} should be rejected"):
                mailbox.send(self.inbox, {"x": 1}, sender=bad)

    def test_valid_senders_accepted(self):
        for ok in ["orchestrator", "claude-research", "recovery_2", "Specialist-1"]:
            mid = mailbox.send(self.inbox, {"x": 1}, sender=ok)
            self.assertTrue(mid.endswith(ok))

    def test_non_json_payload_raises(self):
        with self.assertRaises(TypeError):
            mailbox.send(self.inbox, {"bad": object()}, sender="recovery")

    # ----------------------------------------------- recipient sink guard (T-0906)
    def test_is_safe_inbox_predicate(self):
        base = str(self.inbox)  # an absolute, safe-charset tmp path
        for ok in [base, "/home/the-ancestor/mailbox/recovery", "/tmp/a_b-c.d/new"]:
            self.assertTrue(mailbox.is_safe_inbox(ok), f"{ok!r} should be safe")
        for bad in ["", None, "relative/inbox", "./inbox",
                    "/home/x/../../etc/inbox", "/tmp/in box", "/tmp/in$box",
                    "/tmp/in;box", base + "\n/etc"]:
            self.assertFalse(mailbox.is_safe_inbox(bad), f"{bad!r} should be unsafe")

    def test_send_rejects_unsafe_recipient(self):
        for bad in ["relative/inbox", "/home/x/../../etc/inbox", "/tmp/in box", ""]:
            with self.assertRaises(ValueError,
                                   msg=f"recipient {bad!r} should be rejected"):
                mailbox.send(bad, {"x": 1}, sender="recovery")

    def test_send_does_not_create_dirs_at_unsafe_recipient(self):
        # The sink rejects BEFORE _ensure_subdirs — a traversal path must create nothing.
        evil = str(self.inbox) + " evil"  # absolute but unsafe charset (space)
        with self.assertRaises(ValueError):
            mailbox.send(evil, {"x": 1}, sender="recovery")
        self.assertFalse(Path(evil).exists(), "rejected recipient must not be created")

    # ------------------------------------------------------------- provision
    def test_provision_creates_subdirs(self):
        mailbox.provision(self.inbox)
        for sub in ("tmp", "new", "cur"):
            self.assertTrue((self.inbox / sub).is_dir())


if __name__ == "__main__":
    unittest.main()
