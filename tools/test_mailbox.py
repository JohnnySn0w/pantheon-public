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
import subprocess
import sys
import tempfile
import json
import pathlib
import shutil
import unittest
import tools.mailbox as mb
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # bare-import the SUT
import mailbox  # noqa: E402
from _lease import Lease  # noqa: E402


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        # T-2216: send()/consume() no longer auto-provision. self.inbox is the
        # "already an established mailbox" fixture every pre-existing test in
        # this file assumes — mirroring every real production caller, which
        # only ever targets an already-provisioned inbox. Tests exercising the
        # NEW missing/partial-existence behavior use their OWN never-touched
        # path (see MailboxExistenceTest below), never self.inbox.
        mailbox.provision(self.inbox)

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
            self.assertEqual(
                res, {"skipped": True, "processed": [], "errors": [], "quarantined": []},
                "lease-skip return must carry the full four-key shape, same as a drain",
            )
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

    # ------------------------------------------------------------- severity (T-1640 Ph1)
    def test_valid_severities_accepted(self):
        for sev in ("normal", "urgent"):
            mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", severity=sev)
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(sorted(m["severity"] for m in got), ["normal", "urgent"])

    def test_invalid_severity_rejected(self):
        for bad in ["loud", "URGENT", "", "critical"]:
            with self.assertRaises(ValueError, msg=f"severity {bad!r} should be rejected"):
                mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", severity=bad)

    def test_severity_default_is_normal(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")  # no severity kwarg
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(got[0]["severity"], "normal")

    # ----------------------------------------------------------------- DLQ (T-1876)
    def test_dlq_counter_does_not_rename_below_threshold(self):
        # Same-shape assertion as test_handler_failure_leaves_message_for_retry:
        # attempts below the threshold must NOT touch the message's filename or
        # move it out of new/ — the sidecar counter is the only thing that changes.
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")
        calls = {"n": 0}

        def always_fails(_m):
            calls["n"] += 1
            raise RuntimeError("poison")

        for _ in range(2):  # 2 failures, threshold (default 3) not yet reached
            res = mailbox.consume(self.inbox, always_fails)
            self.assertEqual(res["processed"], [])
            self.assertEqual(res["quarantined"], [])
            self.assertTrue((self.inbox / "new" / mid).exists(),
                             "below-threshold failure must not rename/move the message")
        self.assertFalse((self.inbox / "failed").exists(),
                          "failed/ must not be created before any quarantine")

    def test_dlq_quarantines_at_threshold_not_before(self):
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")

        def always_fails(_m):
            raise RuntimeError("poison")

        res1 = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)
        self.assertEqual(res1["quarantined"], [])
        res2 = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)
        self.assertEqual(res2["quarantined"], [])
        self.assertTrue((self.inbox / "new" / mid).exists(), "still in new/ after 2 failures")

        res3 = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)
        self.assertEqual(len(res3["quarantined"]), 1)
        self.assertEqual(res3["quarantined"][0]["id"], mid)
        self.assertEqual(res3["quarantined"][0]["attempts"], 3)
        self.assertFalse((self.inbox / "new" / mid).exists(), "quarantined msg leaves new/")
        self.assertTrue((self.inbox / "failed" / mid).exists(), "quarantined msg lands in failed/")

    def test_dlq_max_attempts_configurable(self):
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")

        def always_fails(_m):
            raise RuntimeError("poison")

        res = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=1)
        self.assertEqual(len(res["quarantined"]), 1, "N=1 must quarantine on first failure")
        self.assertTrue((self.inbox / "failed" / mid).exists())

    def test_dlq_alerts_owner_with_id_sender_subject_attempts(self):
        mailbox.send(self.inbox, {"n": 1}, sender="recovery", subject="do-the-thing")

        def always_fails(_m):
            raise RuntimeError("boom")

        for _ in range(3):
            mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)

        # The alert lands as an ordinary new message in the SAME inbox, from the
        # reserved mailbox-dlq sender; a plain drain (no failures this time) picks
        # it up and it must carry id/sender/subject/attempts of the quarantined msg.
        got = []
        res = mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(res["quarantined"], [])
        alerts = [m for m in got if m["sender"] == "mailbox-dlq"]
        self.assertEqual(len(alerts), 1)
        payload = alerts[0]["payload"]
        self.assertEqual(payload["sender"], "recovery")
        self.assertEqual(payload["subject"], "do-the-thing")
        self.assertEqual(payload["attempts"], 3)
        self.assertIn("reason", payload)

    def test_dlq_alert_messages_are_exempt_from_quarantine(self):
        # A hand-placed alert-shaped message (sender=mailbox-dlq) must retry forever
        # like any pre-DLQ message, never counted, never quarantined — otherwise an
        # alert-about-an-alert could loop.
        mid = mailbox.send(self.inbox, {"id": "x", "attempts": 3}, sender="mailbox-dlq")

        def always_fails(_m):
            raise RuntimeError("handler always rejects this")

        for _ in range(10):  # far beyond any sane threshold
            res = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)
            self.assertEqual(res["quarantined"], [])
        self.assertTrue((self.inbox / "new" / mid).exists(), "alert must never be quarantined")
        self.assertFalse((self.inbox / "failed").exists())

    def test_dlq_alert_retry_is_bounded_by_backoff(self):
        """T-2386: the exempt mailbox-dlq alert must back off, not spin.

        Its .attempts/ dir stays EMPTY — that is the exemption working, and it is
        also exactly why it could never bound itself before. specialist measured ~60
        retries in 74 minutes off an empty counter dir; each one was a full agent
        turn burned into the seat's context.
        """
        mid = mailbox.send(self.inbox, {"any": "payload"}, sender=mailbox._DLQ_ALERT_SENDER)

        def always_fails(_m):
            raise RuntimeError("handler down")

        res = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)
        self.assertTrue(res["errors"], "the first attempt still runs and is recorded")

        for _ in range(20):  # far more than the ~60/74min specialist observed, in-window
            res = mailbox.consume(self.inbox, always_fails, dlq_max_attempts=3)
            self.assertEqual(res["errors"], [], "an exempt alert must not spin")
            self.assertEqual(res["quarantined"], [])

        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "still never quarantined — exemption from COUNTING is unchanged")
        self.assertFalse((self.inbox / ".attempts" / mid).exists(),
                         "and still never counted")

    def test_entitlement_error_is_bounded_even_though_unclassified(self):
        """T-2386's real point: the safety property must not depend on the
        signature list being complete.

        This is the VERBATIM wire string that matched none of T-2038's four cap
        signatures and therefore took the poison path. The test deliberately does
        NOT mark the exception transient — it stands for ANY environmental error
        nobody has classified yet, including wordings not invented at the time
        this was written. Backoff has to bound it regardless.
        """
        mid = mailbox.send(self.inbox, {"n": 1}, sender="inbox_consumer")
        wire = ("ACP error -32603: Internal error: Your organization does not have "
                "access to Claude. Please login again or contact your administrator.")

        drains = 0

        def unclassified(_m):
            raise RuntimeError(wire)

        for _ in range(12):
            mailbox.consume(self.inbox, unclassified, dlq_max_attempts=3)
            drains += 1

        # Unclassified, so it IS poison-path and DOES quarantine after 3 strikes —
        # that half is unchanged and correct. What must not happen is the
        # resulting alert spinning: assert the alert is present and backed off.
        alerts = [p.name for p in (self.inbox / "new").iterdir()
                  if p.name.endswith(mailbox._DLQ_ALERT_SENDER)]
        self.assertTrue(alerts, "the quarantine still alerts the owner")
        self.assertTrue(
            any(mailbox._backoff_active(self.inbox, a) for a in alerts),
            "the quarantine alert must be inside a backoff window, not free-spinning",
        )
        self.assertFalse((self.inbox / "new" / mid).exists(),
                         "the unclassified failure still quarantines the message itself")

    def test_transient_handler_failure_is_not_counted_toward_dlq(self):
        # T-2038: a handler exception carrying a truthy `mailbox_transient`
        # attribute is an ENVIRONMENTAL failure (upstream cap/outage), not
        # message poison — it must be recorded but NEVER quarantined, so an
        # outage cannot dead-letter innocent messages.
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")

        class _Transient(RuntimeError):
            mailbox_transient = True

        def always_transient(_m):
            raise _Transient("upstream capped")

        # T-2386 kept this loop UNCHANGED on purpose. I tried backing this path
        # off too and reverted it: a transient failure is already bounded by the
        # consumer's circuit breaker, and a backoff here makes the message sit out
        # its own recovery when the upstream clears.
        for _ in range(10):  # far beyond dlq_max_attempts
            res = mailbox.consume(self.inbox, always_transient, dlq_max_attempts=3)
            self.assertEqual(res["quarantined"], [])
            self.assertTrue(res["errors"], "the transient failure is still recorded")
        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "a transient failure keeps the message in new/ for retry")
        self.assertFalse((self.inbox / "failed").exists(),
                         "a transient failure must never create failed/")
        self.assertFalse((self.inbox / ".attempts" / mid).exists(),
                         "a transient failure must not create a DLQ attempt counter")

    def test_dlq_malformed_message_is_eligible_and_quarantines(self):
        mailbox.provision(self.inbox)
        bad_name = "00000000000000000004.1.0.badmsg"
        bad = self.inbox / "new" / bad_name
        bad.write_text("this is not json\n")

        for _ in range(3):
            res = mailbox.consume(self.inbox, lambda m: None, dlq_max_attempts=3)
        self.assertEqual(len(res["quarantined"]), 1)
        self.assertEqual(res["quarantined"][0]["id"], bad_name)
        self.assertTrue((self.inbox / "failed" / bad_name).exists())
        self.assertFalse(bad.exists())

    def test_dlq_success_clears_attempt_state(self):
        # Fail once (below threshold), then succeed — the sidecar counter must not
        # leak into a later independent failure run for a DIFFERENT message with the
        # same handler; verified indirectly via a fresh quarantine still needing the
        # full N attempts (i.e. no stale count carried over).
        mid1 = mailbox.send(self.inbox, {"n": 1}, sender="recovery")
        calls = {"n": 0}

        def flaky_once(_m):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")

        mailbox.consume(self.inbox, flaky_once, dlq_max_attempts=3)  # 1 failure, then...
        res = mailbox.consume(self.inbox, flaky_once, dlq_max_attempts=3)  # ...succeeds
        self.assertEqual(res["processed"], [mid1])
        self.assertFalse((self.inbox / ".attempts" / mid1).exists(),
                          "sidecar counter must be cleared on successful ack")

    def test_missing_severity_field_back_compat(self):
        # A record written before this field existed (or by any future non-updated
        # sender) has no "severity" key at all. The read-side contract is
        # record.get("severity", "normal") — this test exercises the CORE
        # consume(inbox, handler) path (raw record dict), same hand-placed pattern
        # as test_readdir_is_truth, not the MCP wrapper (which normalizes shape).
        mailbox.provision(self.inbox)
        import json
        name = "00000000000000000003.999.000000.presevfield"
        (self.inbox / "new" / name).write_text(
            json.dumps({"id": name, "sender": "x", "payload": {"pre": True}}) + "\n"
        )
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(len(got), 1)
        self.assertNotIn("severity", got[0], "pre-field record has no severity key")
        self.assertEqual(got[0].get("severity", "normal"), "normal",
                          "read-side default must be 'normal' for pre-field records")


class DedupePendingTest(unittest.TestCase):
    """T-1935: send() is idempotent-while-pending — a content-identical send
    from the same sender collapses onto the pending copy in new/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)  # T-2216: send() no longer auto-provisions

    def tearDown(self):
        self._tmp.cleanup()

    def _new_count(self):
        return len(list((self.inbox / "new").iterdir()))

    def test_identical_pending_send_collapses(self):
        first = mailbox.send(self.inbox, {"ruling": "X"}, sender="orchestrator", subject="s")
        second = mailbox.send(self.inbox, {"ruling": "X"}, sender="orchestrator", subject="s")
        self.assertEqual(first, second, "duplicate send must return the pending id")
        self.assertEqual(self._new_count(), 1, "no second file for identical pending content")

    def test_different_payload_not_deduped(self):
        a = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        b = mailbox.send(self.inbox, {"n": 2}, sender="orchestrator")
        self.assertNotEqual(a, b)
        self.assertEqual(self._new_count(), 2)

    def test_same_payload_different_sender_not_deduped(self):
        mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        mailbox.send(self.inbox, {"n": 1}, sender="specialist")
        self.assertEqual(self._new_count(), 2, "dedupe is per-sender")

    def test_consumed_message_does_not_dedupe(self):
        first = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        mailbox.consume(self.inbox, lambda m: None)
        second = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        self.assertNotEqual(first, second,
                            "re-send after consumption is a fresh delivery (new/ only)")
        self.assertEqual(self._new_count(), 1)

    def test_dedupe_opt_out(self):
        a = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        b = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator", dedupe_pending=False)
        self.assertNotEqual(a, b)
        self.assertEqual(self._new_count(), 2)

    def test_legacy_record_without_digest_still_dedupes(self):
        first = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator", subject="s")
        # Strip the digest field, simulating a pre-T-1935 pending record.
        path = self.inbox / "new" / first
        record = json.loads(path.read_text())
        del record["digest"]
        path.write_text(json.dumps(record) + "\n")
        second = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator", subject="s")
        self.assertEqual(first, second, "legacy pending record must dedupe via recompute")

    def test_unparseable_pending_file_is_skipped_not_fatal(self):
        first = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        (self.inbox / "new" / f"00000000000000000000.1.000000.orchestrator").write_text("not json")
        second = mailbox.send(self.inbox, {"n": 2}, sender="orchestrator")
        self.assertNotEqual(first, second)

    def test_digest_field_present_on_new_records(self):
        mid = mailbox.send(self.inbox, {"n": 1}, sender="orchestrator")
        record = json.loads((self.inbox / "new" / mid).read_text())
        self.assertIn("digest", record)


class OrphanSidecarPruneTest(unittest.TestCase):
    """T-1992: a process death between an ack/quarantine rename and its sidecar
    clear orphans a .attempts counter; the drain-start janitor prunes it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "ibx"
        mailbox.provision(self.inbox)
        self.addCleanup(self._tmp.cleanup)

    def test_orphan_sidecar_pruned_on_next_drain(self):
        # A message that failed once (sidecar) then was acked to cur/ WITHOUT
        # its sidecar cleared (the crash-between-rename-and-clear window).
        mid = mailbox.send(self.inbox, "hi", sender="keep_going", subject="n")
        mailbox.consume(self.inbox, lambda m: (_ for _ in ()).throw(RuntimeError("x")),
                        use_lease=False)  # fail → sidecar
        self.assertTrue((self.inbox / ".attempts" / mid).exists())
        # simulate the crash: move to cur/ but leave the sidecar
        os.rename(self.inbox / "new" / mid, self.inbox / "cur" / mid)
        self.assertTrue((self.inbox / ".attempts" / mid).exists(), "orphan set up")
        # next drain prunes it at start
        mailbox.consume(self.inbox, lambda m: None, use_lease=False)
        self.assertFalse((self.inbox / ".attempts" / mid).exists(),
                         "orphaned sidecar (no new/ message) must be pruned")

    def test_valid_sidecar_for_pending_message_is_kept(self):
        # A sidecar whose message is STILL in new/ (mid-retry) must NOT be pruned.
        mid = mailbox.send(self.inbox, "hi", sender="keep_going", subject="n")
        mailbox.consume(self.inbox, lambda m: (_ for _ in ()).throw(RuntimeError("x")),
                        use_lease=False)  # fail → sidecar, stays in new/
        self.assertTrue((self.inbox / ".attempts" / mid).exists())
        self.assertTrue((self.inbox / "new" / mid).exists())
        # a drain where the handler defers (raises again) keeps both
        mailbox.consume(self.inbox, lambda m: (_ for _ in ()).throw(RuntimeError("x")),
                        use_lease=False)
        self.assertTrue((self.inbox / ".attempts" / mid).exists(),
                        "a valid sidecar for a still-pending message must survive the janitor")

    def test_prune_helper_returns_count_and_tolerates_no_dir(self):
        # T-2287 changed the second argument from the new/ DIRECTORY to the set of
        # LIVE message names (new/ ∪ every in-flight claim), because a claimed
        # message is absent from new/ and a dir-based check would prune its counter.
        # no .attempts dir yet → 0, no error
        empty = Path(self._tmp.name) / "empty"
        mailbox.provision(empty)
        self.assertEqual(mailbox._prune_orphan_dlq_sidecars(empty, set()), 0)
        # two orphans → both pruned, count == 2
        att = self.inbox / ".attempts"
        att.mkdir(exist_ok=True)
        (att / "01.a").write_text("1")
        (att / "02.b").write_text("2")
        self.assertEqual(mailbox._prune_orphan_dlq_sidecars(self.inbox, set()), 2)
        # and a name in the live set is KEPT — the half that actually protects DLQ
        (att / "03.c").write_text("3")
        self.assertEqual(mailbox._prune_orphan_dlq_sidecars(self.inbox, {"03.c"}), 0)
        self.assertTrue((att / "03.c").exists())


# --------------------------------------------------------------------------
# T-2216: existence vs provisioning. send()/consume() no longer treat a
# well-formed-but-never-provisioned recipient path as fair game to silently
# create — that is exactly how a wrong/vanished hub path resurrects itself
# and accumulates undrained mail. Every fixture below uses its OWN
# never-touched path (never self.inbox from MailboxTest, which setUp()
# deliberately provisions).
# --------------------------------------------------------------------------
class MailboxExistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.missing = Path(self._tmp.name) / "never-provisioned"

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------- send()
    def test_send_to_missing_raises_mailbox_not_found(self):
        with self.assertRaises(mailbox.MailboxNotFound):
            mailbox.send(self.missing, {"x": 1}, sender="recovery")

    def test_send_to_missing_does_not_create_dirs(self):
        with self.assertRaises(mailbox.MailboxNotFound):
            mailbox.send(self.missing, {"x": 1}, sender="recovery")
        self.assertFalse(self.missing.exists(),
                          "a rejected send must create nothing on disk")

    def test_alias_names_the_canonical_box(self):
        """T-4311: specialist sent to <hub>/consultant and reported Consultant unreachable. The
        error now names codex's box. It does not deliver there."""
        hub = Path(self._tmp.name)
        mailbox.provision(hub / "codex")
        with self.assertRaises(mailbox.MailboxNotFound) as cm:
            mailbox.send(hub / "consultant", {"x": 1}, sender="specialist")
        self.assertIn(f"consultant is an alias of codex, whose mailbox is {hub / 'codex'}",
                      str(cm.exception))
        self.assertEqual(list((hub / "codex" / "new").iterdir()), [])  # no redirect

    def test_no_hint_for_an_unknown_name_or_a_missing_canonical_box(self):
        hub = Path(self._tmp.name)
        for name in ("nobody", "consultant"):  # consultant: codex's box is NOT provisioned here
            with self.subTest(name=name), self.assertRaises(mailbox.MailboxNotFound) as cm:
                mailbox.send(hub / name, {"x": 1}, sender="specialist")
            self.assertNotIn("alias", str(cm.exception))

    def test_a_failing_alias_lookup_leaves_the_plain_error(self):
        """T-4320 (T-4311 nit): _alias_hint's fail-soft path. If agent_role_map
        can't be imported, or raises while being read, the caller still gets the
        plain MailboxNotFound: no hint, and no other exception in its place."""
        hub = Path(self._tmp.name)
        mailbox.provision(hub / "codex")  # a working lookup WOULD add the hint

        class _Unreadable:
            def __getattr__(self, name):
                raise RuntimeError("agent_role_map is broken")

        plain = f"recipient inbox {hub / 'consultant'} does not exist (pass provision_if_missing=True"
        for label, modules in (
            ("import fails", {"tools.agent_role_map": None, "agent_role_map": None}),
            ("module raises", {"tools.agent_role_map": _Unreadable(), "agent_role_map": _Unreadable()}),
        ):
            with self.subTest(label), mock.patch.dict(sys.modules, modules):
                with self.assertRaises(mailbox.MailboxNotFound) as cm:
                    mailbox.send(hub / "consultant", {"x": 1}, sender="specialist")
                self.assertTrue(str(cm.exception).startswith(plain), str(cm.exception))

    def test_mailbox_not_found_is_a_file_not_found_error(self):
        # Locks in OSError-catchability: every existing fail-soft caller
        # already catches OSError (or the (OSError, ValueError, TypeError)
        # tuple) and must keep catching this for free.
        self.assertTrue(issubclass(mailbox.MailboxNotFound, FileNotFoundError))

    def test_send_provision_if_missing_creates_and_delivers(self):
        mid = mailbox.send(self.missing, {"x": 1}, sender="recovery",
                            provision_if_missing=True)
        for sub in ("tmp", "new", "cur"):
            self.assertTrue((self.missing / sub).is_dir())
        self.assertTrue((self.missing / "new" / mid).exists())

    def test_send_provision_if_missing_true_on_existing_box_is_noop_deliver(self):
        # Opt-in on an ALREADY-provisioned box must not surprise-recreate
        # anything — just deliver, same as the default path would if it existed.
        mailbox.provision(self.missing)
        mid = mailbox.send(self.missing, {"x": 1}, sender="recovery",
                            provision_if_missing=True)
        self.assertTrue((self.missing / "new" / mid).exists())

    # ---------------------------------------------------------- consume()
    def test_consume_on_missing_is_flagged_not_raised(self):
        called = []
        res = mailbox.consume(self.missing, lambda m: called.append(m))
        self.assertEqual(res, {"skipped": True, "reason": "inbox_missing",
                                "processed": [], "errors": [], "quarantined": []})
        self.assertEqual(called, [], "handler must never be invoked")

    def test_consume_on_missing_does_not_create_dirs(self):
        mailbox.consume(self.missing, lambda m: None)
        self.assertFalse(self.missing.exists(),
                          "a missing-inbox consume must create nothing on disk")

    def test_consume_missing_distinguishable_from_lease_busy(self):
        # Regression guard for callers (e.g. mailbox_mcp.py) that branch on
        # `skipped` — the two shapes must be tellable apart WITHOUT
        # string-matching the reason text, so a later reword can't silently
        # collapse them back together.
        missing_res = mailbox.consume(self.missing, lambda m: None)

        existing = Path(self._tmp.name) / "provisioned"
        mailbox.provision(existing)
        from _lease import Lease
        holder = Lease(existing / ".consumer.lock")
        self.assertTrue(holder.acquire())
        try:
            lease_busy_res = mailbox.consume(existing, lambda m: None)
        finally:
            holder.release()

        self.assertTrue(missing_res["skipped"])
        self.assertTrue(lease_busy_res["skipped"])
        self.assertIn("reason", missing_res)
        self.assertNotIn("reason", lease_busy_res,
                          "lease-held skip must keep its original 4-key shape")

    # ------------------------------------------------------------- exists()
    def test_exists_true_for_fully_provisioned(self):
        mailbox.provision(self.missing)
        self.assertTrue(mailbox.exists(self.missing))

    def test_exists_false_for_nonexistent(self):
        self.assertFalse(mailbox.exists(self.missing))

    def test_exists_false_for_partial_subdirs(self):
        # The "third state": a directory exists but is not a complete
        # mailbox. Must NOT be treated as healthy — that would silently
        # repair the exact class of lie this ticket closes.
        (self.missing / "new").mkdir(parents=True)
        self.assertFalse(mailbox.exists(self.missing))

    def test_consume_on_partial_subdirs_flagged_missing_not_repaired(self):
        (self.missing / "new").mkdir(parents=True)
        res = mailbox.consume(self.missing, lambda m: None)
        self.assertEqual(res["reason"], "inbox_missing")
        self.assertFalse((self.missing / "tmp").exists(),
                          "must never silently repair a partial mailbox")
        self.assertFalse((self.missing / "cur").exists())
class TestSupersession(unittest.TestCase):
    """T-2165: let a corrected message retract the one it replaces.

    The second case is the one that matters. A plain follow-up is
    indistinguishable from ordinary mail and carries no signal that work already
    done off the retracted claim needs revisiting — which is exactly what happened
    on 2026-07-28, when a "last live instance" claim of mine was acted on by two
    agents before I corrected it.
    """

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="t2165-"))
        for sub in ("new", "cur", "tmp"):
            (self.d / sub).mkdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _pending(self):
        return sorted(p.name for p in (self.d / "new").iterdir())

    def _rec(self, name):
        return json.loads((self.d / "new" / name).read_text(encoding="utf-8"))

    def test_unread_original_is_retracted_and_never_seen(self):
        a = mb.send(str(self.d), {"claim": "wrong"}, sender="recovery", subject="v1")
        b = mb.send(str(self.d), {"claim": "right"}, sender="recovery", subject="v2", supersedes=a)
        self.assertEqual(self._pending(), [b], "the retracted message must be gone")
        self.assertTrue(self._rec(b)["superseded_was_unread"])

    def test_already_read_original_delivers_WITH_the_marker(self):
        """The case that matters: downstream work may need revisiting."""
        a = mb.send(str(self.d), {"claim": "wrong"}, sender="recovery", subject="v1")
        mb.consume(str(self.d), lambda m: None)
        b = mb.send(str(self.d), {"claim": "right"}, sender="recovery", subject="v2", supersedes=a)
        rec = self._rec(b)
        self.assertEqual(rec["supersedes"], a)
        self.assertFalse(rec["superseded_was_unread"],
                         "already-read must report False so a handler can flag rework")

    def test_a_sender_cannot_retract_another_senders_message(self):
        """Safety property: supersession must not become a delete primitive."""
        theirs = mb.send(str(self.d), {"p": 1}, sender="specialist", subject="theirs")
        mine = mb.send(str(self.d), {"p": 2}, sender="recovery", subject="mine", supersedes=theirs)
        self.assertIn(theirs, self._pending(), "specialist's message must be untouched")
        self.assertIn(mine, self._pending())
        self.assertFalse(self._rec(mine)["superseded_was_unread"])

    def test_unknown_id_still_delivers_marked(self):
        b = mb.send(str(self.d), {"p": 1}, sender="recovery", subject="x",
                    supersedes="no-such-message-id")
        self.assertIn(b, self._pending())
        self.assertFalse(self._rec(b)["superseded_was_unread"])

    def test_ordinary_send_carries_null_supersession_fields(self):
        b = mb.send(str(self.d), {"p": 1}, sender="recovery", subject="plain")
        rec = self._rec(b)
        self.assertIsNone(rec["supersedes"])
        self.assertIsNone(rec["superseded_was_unread"],
                          "None, not False — 'no supersession' differs from 'was already read'")

    def test_consumer_sees_the_supersession_fields(self):
        a = mb.send(str(self.d), {"claim": "wrong"}, sender="recovery", subject="v1")
        mb.consume(str(self.d), lambda m: None)
        mb.send(str(self.d), {"claim": "right"}, sender="recovery", subject="v2", supersedes=a)
        got = []
        mb.consume(str(self.d), lambda m: got.append(m))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["supersedes"], a)
        self.assertIs(got[0]["superseded_was_unread"], False)

    def test_retraction_survives_a_chain(self):
        a = mb.send(str(self.d), {"v": 1}, sender="recovery", subject="v1")
        b = mb.send(str(self.d), {"v": 2}, sender="recovery", subject="v2", supersedes=a)
        c = mb.send(str(self.d), {"v": 3}, sender="recovery", subject="v3", supersedes=b)
        self.assertEqual(self._pending(), [c], "only the newest survives an unread chain")



# --------------------------------------------------------------------------
# T-2287: the consumer lease is narrowed to a CLAIM step.
#
# The pre-existing 60 tests all pass against this change, which is exactly why
# they are weak evidence for it: they encode the OLD design's properties (drain
# under one long lease) and none of them can observe the thing that changed.
# Everything below asserts a NEW property, and the two that could pass for a
# boring reason carry a CONTROL that shows the harness discriminates.
# --------------------------------------------------------------------------


def _claim_and_die(inbox_str, mode):
    """Run in a SUBPROCESS: claim whatever is in new/, then either exit (so the
    kernel drops the owner flock) or sleep holding it. Used to build a real
    abandoned claim and a real live one — not a simulated pair."""
    src = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {os.path.dirname(os.path.abspath(mailbox.__file__))!r})\n"
        "import mailbox\n"
        "from pathlib import Path\n"
        f"inbox = Path({inbox_str!r})\n"
        "claim_dir, owner = mailbox._open_claim(inbox)\n"
        "names = mailbox._claim(inbox / 'new', claim_dir)\n"
        "print(claim_dir, flush=True)\n"
        f"mode = {mode!r}\n"
        "if mode == 'die':\n"
        "    os._exit(0)\n"
        "time.sleep(120)\n"
    )
    return src


class ClaimStepReleasesTheLeaseTest(unittest.TestCase):
    """The point of the whole change: an agent in a long handler must not be
    holding its own mailbox shut. specialist measured 47 minutes of that."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "mb"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_consumer_lease_is_free_while_the_handler_runs(self):
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        seen = {}

        def handler(msg):
            # The lease must be acquirable by SOMEONE ELSE right now. This is the
            # liveness property; under the pre-T-2287 design it was held here.
            probe = Lease(self.inbox / ".consumer.lock", label="probe")
            seen["lease_free"] = probe.acquire(reap_stale=False)
            if seen["lease_free"]:
                probe.release()

        mailbox.consume(self.inbox, handler)
        self.assertTrue(seen.get("lease_free"),
                        "the consumer lease must be RELEASED before the handler runs")

    def test_a_concurrent_consumer_sees_no_messages_and_is_not_skipped(self):
        """Both halves matter and they pull opposite ways. Not-skipped is the
        liveness win; processed==[] is the safety half — the second consumer must
        not be able to deliver a message the first has already claimed. A design
        that merely released the lease early would pass the first and FAIL this."""
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        inner = {}

        def handler(msg):
            inner["res"] = mailbox.consume(self.inbox, lambda m: inner.setdefault(
                "DOUBLE_DELIVERED", []).append(m["id"]))

        outer = mailbox.consume(self.inbox, handler)
        self.assertEqual(len(outer["processed"]), 1)
        self.assertFalse(inner["res"]["skipped"],
                         "concurrent consumer must acquire the lease, not skip")
        self.assertEqual(inner["res"]["processed"], [],
                         "a claimed message must be invisible to a second consumer")
        self.assertNotIn("DOUBLE_DELIVERED", inner)


class AbandonedClaimReaperTest(unittest.TestCase):
    """at-least-once has to survive the new intermediate state, and the reaper is
    the new thing that can fail silently — the exact class the mail-staleness work
    exists to catch. So: a real crashed process, and a real live one as control."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "mb"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def _spawn(self, mode):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", _claim_and_die(str(self.inbox), mode)],
                             stdout=subprocess.PIPE, text=True)
        claim_dir = p.stdout.readline().strip()
        return p, Path(claim_dir)

    def test_crashed_consumer_message_is_requeued_and_redelivered(self):
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        p, claim_dir = self._spawn("die")
        p.wait(timeout=10)
        # precondition: the message really is in-flight, not in new/
        self.assertEqual(list((self.inbox / "new").iterdir()), [])
        self.assertTrue(any(f.name != mailbox._CLAIM_OWNER_LOCK
                            for f in claim_dir.iterdir()))
        got = []
        res = mailbox.consume(self.inbox, lambda m: got.append(m["payload"]))
        self.assertEqual(got, [{"n": 1}], "an abandoned claim must be redelivered")
        self.assertEqual(len(res["reaped"]["requeued"]), 1)
        self.assertFalse(claim_dir.exists(), "the reaped claim dir must be removed")

    def test_control_a_LIVE_claim_is_never_requeued(self):
        """The control, and the one that matters most: reaping a live consumer's
        claim is a SILENT duplicate delivery — strictly worse than the liveness
        bug this ticket set out to fix. Same harness as the test above, one
        variable changed (the owner stays alive)."""
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        p, claim_dir = self._spawn("live")
        try:
            got = []
            res = mailbox.consume(self.inbox, lambda m: got.append(m["payload"]))
            self.assertEqual(got, [], "a LIVE claim must not be redelivered")
            self.assertEqual(res["reaped"]["requeued"], [])
            self.assertEqual(res["reaped"]["skipped_live"], 1)
            self.assertTrue(claim_dir.exists())
        finally:
            p.kill()
            p.wait(timeout=10)

    def test_unknown_lock_state_fails_CLOSED(self):
        """/proc/locks unreadable => flock_holder_pid() raises/UNKNOWN => the claim
        must be treated as LIVE. 'Cannot tell' standing in for 'not held' is the
        defect family that produced four bugs on 2026-07-28 (T-2307); here the
        cost of getting it wrong is a duplicate delivery."""
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        p, claim_dir = self._spawn("die")
        p.wait(timeout=10)
        real = pathlib.Path.read_text

        def blind(self_, *a, **k):
            if str(self_) == "/proc/locks":
                raise OSError(12, "simulated procfs failure")
            return real(self_, *a, **k)

        pathlib.Path.read_text = blind
        try:
            self.assertFalse(mailbox._claim_is_abandoned(claim_dir),
                             "blind lock state must NOT be read as abandoned")
        finally:
            pathlib.Path.read_text = real
        # control: with procfs readable the SAME claim reaps, so the assertion
        # above is about blindness and not about the claim being unreapable.
        self.assertTrue(mailbox._claim_is_abandoned(claim_dir))


class ClaimStepDoesNotBreakDLQTest(unittest.TestCase):
    """The interaction T-2287 called the actual risk. A claimed message is absent
    from new/, so a sidecar pruner keyed on new/ alone would wipe its attempt
    counter every drain and the DLQ would never fire — present, wired, tested,
    and silently inert."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "mb"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_attempt_counter_survives_the_claim_and_still_quarantines(self):
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        boom = lambda m: (_ for _ in ()).throw(RuntimeError("poison"))
        for i in range(2):
            res = mailbox.consume(self.inbox, boom, dlq_max_attempts=3)
            self.assertEqual(res["quarantined"], [], f"too early at attempt {i+1}")
            # the failure must have gone BACK to new/, not stranded in a claim dir
            self.assertEqual(len(list((self.inbox / "new").iterdir())), 1,
                             "a failed message must be requeued to new/")
        res = mailbox.consume(self.inbox, boom, dlq_max_attempts=3)
        self.assertEqual(len(res["quarantined"]), 1,
                         "3rd recorded failure must quarantine — if this is green "
                         "only because the counter reset, it never would")
        self.assertEqual(len(list((self.inbox / "failed").iterdir())), 1)
        # new/ is NOT empty here and must not be asserted so: quarantining sends a
        # DLQ alert back into this same inbox (_alert_owner), which lands in new/.
        # The property under test is narrower — the QUARANTINED message itself must
        # not have been requeued as well, which would resurrect a poison message
        # the DLQ had just retired.
        quarantined_id = res["quarantined"][0]["id"]
        self.assertTrue((self.inbox / "failed" / quarantined_id).exists())
        self.assertFalse((self.inbox / "new" / quarantined_id).exists(),
                         "a quarantined message must NOT also be requeued")
        leftover = [p.name for p in (self.inbox / "new").iterdir()]
        self.assertEqual(len(leftover), 1)
        self.assertTrue(leftover[0].endswith(mailbox._DLQ_ALERT_SENDER),
                        f"only the DLQ alert may remain in new/, got {leftover}")

    def test_counter_survives_while_ANOTHER_consumer_holds_the_claim(self):
        """⚠️ THIS is the test that actually covers the flagged risk; the one above
        does NOT, and mutation testing is the only reason I know that.

        The sequential test passes even with a new/-only pruner, because a failed
        message is requeued to new/ before the next drain starts — so by the time
        the pruner runs, the message is back in new/ and the in-flight case is
        never exercised. The bug needs a CONCURRENT claim: consumer A holds the
        message in-flight while consumer B prunes, and B deletes a counter for a
        message it simply cannot see. That premise has to be built, not assumed —
        a test on an impossible premise asserts a bug as expected behaviour."""
        import subprocess
        mid = mailbox.send(self.inbox, {"n": 1}, sender="peer")
        boom = lambda m: (_ for _ in ()).throw(RuntimeError("poison"))
        for _ in range(2):
            mailbox.consume(self.inbox, boom, dlq_max_attempts=3)
        self.assertEqual((self.inbox / ".attempts" / mid).read_text(), "2",
                         "precondition: two recorded failures")

        # consumer A takes the message in-flight and STAYS ALIVE holding it
        p = subprocess.Popen(
            [sys.executable, "-c", _claim_and_die(str(self.inbox), "live")],
            stdout=subprocess.PIPE, text=True)
        claim_dir = Path(p.stdout.readline().strip())
        try:
            self.assertFalse((self.inbox / "new" / mid).exists(),
                             "precondition: the message is in-flight, not in new/")
            self.assertTrue((claim_dir / mid).exists())
            # consumer B drains concurrently — its janitor must NOT wipe A's counter
            mailbox.consume(self.inbox, lambda m: None, dlq_max_attempts=3)
            self.assertTrue(
                (self.inbox / ".attempts" / mid).exists(),
                "a concurrent drain must not prune the DLQ counter of a message "
                "that is merely in-flight — that silently disarms the DLQ")
            self.assertEqual((self.inbox / ".attempts" / mid).read_text(), "2")
        finally:
            p.kill()
            p.wait(timeout=10)
        # A is gone: the claim is reaped, the message redelivered, and the THIRD
        # recorded failure quarantines — which only happens if the count survived.
        res = mailbox.consume(self.inbox, boom, dlq_max_attempts=3)
        self.assertEqual(len(res["quarantined"]), 1,
                         "3rd failure must quarantine; a reset counter would not")
        self.assertEqual(res["quarantined"][0]["attempts"], 3)

    def test_live_names_refuses_to_guess_when_it_cannot_read_a_claim(self):
        """_live_names returns None rather than a partial set: handing the pruner
        an incomplete live set is indistinguishable from real absence, and the
        pruner deletes what it cannot see."""
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        claim_dir, owner = mailbox._open_claim(self.inbox)
        try:
            mailbox._claim(self.inbox / "new", claim_dir)
            self.assertEqual(mailbox._live_names(self.inbox / "new", self.inbox),
                             set(n for n in os.listdir(claim_dir)
                                 if n != mailbox._CLAIM_OWNER_LOCK))
            os.chmod(claim_dir, 0o000)
            try:
                self.assertIsNone(
                    mailbox._live_names(self.inbox / "new", self.inbox),
                    "an unreadable claim must yield None, never a partial set")
            finally:
                os.chmod(claim_dir, 0o770)
        finally:
            mailbox._close_claim(claim_dir, owner)


# --------------------------------------------------------------------------
# T-2275 (specialist): send() returned the message id and kept nothing, so
# `supersedes=` — which needs the id of your OWN earlier message — was unusable
# by construction. Measured 0 uses across 1552 messages and 9 senders.
# --------------------------------------------------------------------------
class SenderLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.inbox = root / "peer"
        mailbox.provision(self.inbox)
        # T-2275: point the ledger at the sandbox via the documented override.
        # An explicit PANTHEON_SENT_LEDGER wins over the skip-under-test guard —
        # that is deliberate: being in a test is a reason not to touch the REAL
        # ledger, never a reason to ignore where the caller pointed it.
        self._saved_env = os.environ.get("PANTHEON_SENT_LEDGER")
        self.ledger = root / "state" / "sent.jsonl"
        os.environ["PANTHEON_SENT_LEDGER"] = str(self.ledger)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("PANTHEON_SENT_LEDGER", None)
        else:
            os.environ["PANTHEON_SENT_LEDGER"] = self._saved_env
        self._tmp.cleanup()

    def _lines(self):
        p = self.ledger
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def test_send_records_the_id_the_caller_would_need_to_supersede(self):
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery", subject="s")
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], mid,
                         "the ledger must carry the id send() returned — that id "
                         "IS the input supersedes= requires")
        self.assertEqual(rows[0]["recipient"], "peer")
        self.assertEqual(rows[0]["sender"], "recovery")
        self.assertEqual(rows[0]["subject"], "s")

    def test_the_recorded_id_actually_works_as_supersedes(self):
        """THE POINT OF THE TICKET, end to end: read your own id back out of the
        ledger and retract with it. Asserting the row exists is not enough — the
        row has to be USABLE, which is the property that was missing."""
        first = mailbox.send(self.inbox, {"n": 1}, sender="recovery", subject="claim")
        recalled = self._lines()[0]["id"]
        self.assertEqual(recalled, first)
        mailbox.send(self.inbox, {"n": 2}, sender="recovery", subject="correction",
                     supersedes=recalled)
        self.assertFalse((self.inbox / "new" / first).exists(),
                         "the superseded message must have been retracted")
        rows = self._lines()
        self.assertEqual(rows[1]["supersedes"], first,
                         "the ledger must record that this send retracted one")

    def test_ledger_is_created_lazily_and_appends(self):
        self.assertFalse(self.ledger.exists(), "must not exist before a send")
        for i in range(3):
            mailbox.send(self.inbox, {"i": i}, sender="recovery")
        self.assertEqual(len(self._lines()), 3, "append-only, one line per send")

    def test_A_LEDGER_FAILURE_CANNOT_FAIL_A_DELIVERY(self):
        """The contract, and the one that would be actively harmful to get wrong:
        the message is already durably in new/ when the ledger is written, so a
        bookkeeping error must never turn a completed send into an exception —
        that would make a caller believe it had not sent a message it HAD sent."""
        os.environ["PANTHEON_SENT_LEDGER"] = "/proc/definitely-not-writable/sent.jsonl"
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")
        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "delivery must succeed even when the ledger cannot be written")

    def test_A_SUITE_RUN_MUST_NOT_WRITE_THE_OPERATORS_REAL_LEDGER(self):
        """The leak this shipped with, caught only by looking at the real file:
        one full-suite run put 775 FIXTURE rows into ~/.local/state/pantheon/
        sent.jsonl — senders like "test-harness", recipients like
        "fixture-draining". The ledger's only job is letting a sender find the id
        of a message it really sent, and burying real ids under hundreds of
        fixtures destroys exactly that."""
        os.environ.pop("PANTHEON_SENT_LEDGER", None)
        os.environ.pop("PANTHEON_DISABLE_LIVE_TELEGRAM", None)   # env markers are
        os.environ.pop("PYTEST_CURRENT_TEST", None)              # DELETABLE mid-run
        self.assertIsNone(mailbox._ledger_target(self.inbox),
                          "a delivery to a tmpdir is not a hub delivery and must "
                          "never touch the real ledger — and the guard must hold "
                          "with every env marker stripped, because five test "
                          "modules use patch.dict(clear=True)")
        mid = mailbox.send(self.inbox, {"n": 1}, sender="recovery")
        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "skipping the ledger must not affect delivery")

    def test_an_explicit_override_beats_the_test_guard(self):
        """Otherwise the guard would make the ledger untestable — the mirror
        failure, where a safety net removes the thing it protects."""
        os.environ["PANTHEON_SENT_LEDGER"] = str(self.ledger)
        self.assertEqual(mailbox._ledger_target(self.inbox), self.ledger)
        mailbox.send(self.inbox, {"n": 1}, sender="recovery")
        self.assertEqual(len(self._lines()), 1)

    def test_a_deduped_no_op_send_does_not_add_a_second_row(self):
        """send() returns the EXISTING id when an identical message is still
        pending. Nothing new was delivered, so nothing new should be recorded —
        a ledger that counts no-ops would misreport what this account sent."""
        a = mailbox.send(self.inbox, {"n": 1}, sender="recovery", subject="dup")
        b = mailbox.send(self.inbox, {"n": 1}, sender="recovery", subject="dup")
        self.assertEqual(a, b, "fixture: the second send must be the dedupe no-op")
        self.assertEqual(len(self._lines()), 1)

    def test_concurrent_senders_on_one_account_interleave_whole_lines(self):
        """specialist and archivist each run TWO executors against one home, so this is
        the normal case rather than an edge. O_APPEND must keep lines intact."""
        import subprocess
        src = (
            "import sys, pathlib\n"
            f"sys.path.insert(0, {os.path.dirname(os.path.abspath(mailbox.__file__))!r})\n"
            "import mailbox\n"
            "from pathlib import Path\n"
            f"import os; os.environ['PANTHEON_SENT_LEDGER'] = {str(self.ledger)!r}\n"
            f"for i in range(15): mailbox.send(Path({str(self.inbox)!r}), {{'i': i}}, sender=sys.argv[1])\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", src, f"w{n}"]) for n in range(3)]
        for p in procs:
            p.wait(timeout=60)
        rows = self._lines()           # json.loads on every line — a torn write raises
        self.assertEqual(len(rows), 45, f"expected 45 intact lines, got {len(rows)}")
        self.assertEqual({r["sender"] for r in rows}, {"w0", "w1", "w2"})


# --------------------------------------------------------------------------
# T-2266 (archivist): two structural gaps in the ORG-026 transport.
# --------------------------------------------------------------------------
class AckRecordsTheActorTest(unittest.TestCase):
    """A maildir ack records WHEN, never WHO. `_process` renames inside the
    per-message loop where `handler` is whatever the caller passed — a full agent
    turn for an ACP bridge, an append-to-a-list collector for a hand-run drain. So
    12-14 renames in one second is routine for a manual drain and says nothing
    about a bridge's per-message cost. Without the actor, NO ack-density statistic
    from these maildirs is attributable to any consumer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "mb"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def _acks(self):
        p = self.inbox / ".acks.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def test_ack_records_who_not_only_when(self):
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        mailbox.consume(self.inbox, lambda m: None, label="acp-bridge:specialist")
        rows = self._acks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "acp-bridge:specialist",
                         "the ack must name the consumer, or density stats are "
                         "unattributable to anyone")
        self.assertEqual(rows[0]["pid"], os.getpid())
        self.assertIn("ts_ns", rows[0])

    def test_two_consumers_are_distinguishable_in_the_ack_record(self):
        """THE POINT: 'which consumer acked these' must be answerable. Two drains
        with different labels must not be conflatable after the fact."""
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        mailbox.consume(self.inbox, lambda m: None, label="bridge")
        mailbox.send(self.inbox, {"n": 2}, sender="peer")
        mailbox.consume(self.inbox, lambda m: None, label="hand-drain")
        self.assertEqual([r["label"] for r in self._acks()], ["bridge", "hand-drain"])

    def test_a_failed_ack_log_cannot_fail_the_ack(self):
        """The message is already in cur/ when this runs; bookkeeping must never
        turn a completed ack into an exception."""
        (self.inbox / ".acks.jsonl").mkdir()          # a dir where a file must go
        mid = mailbox.send(self.inbox, {"n": 1}, sender="peer")
        res = mailbox.consume(self.inbox, lambda m: None, label="x")
        self.assertEqual(res["processed"], [mid])
        self.assertTrue((self.inbox / "cur" / mid).exists())


class SupersedesAlreadyCancelsPendingTest(unittest.TestCase):
    """T-2266 asked for EITHER a queued-subject list OR 'a message can carry a
    supersedes/retracts pointer that CANCELS its target while the target is still
    in new/'. The second already existed — what was missing was the ability to
    OBTAIN your own message id, which T-2275 fixed. Pinning it so the capability
    cannot regress unnoticed a second time."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "mb"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_supersedes_removes_the_target_while_it_is_still_unread(self):
        first = mailbox.send(self.inbox, {"claim": "x"}, sender="recovery", subject="claim")
        mailbox.send(self.inbox, {"fix": "y"}, sender="recovery", subject="correction",
                     supersedes=first)
        self.assertFalse((self.inbox / "new" / first).exists(),
                         "an unread target must be CANCELLED, not merely followed")
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(len(got), 1, "only the correction should be delivered")
        self.assertTrue(got[0]["superseded_was_unread"],
                        "the receiver must be told the original was never seen")

    def test_an_already_read_target_is_reported_not_silently_dropped(self):
        """The case that matters: work may have been done on the claim, so the
        follow-up must carry the signal rather than look like ordinary mail."""
        first = mailbox.send(self.inbox, {"claim": "x"}, sender="recovery")
        mailbox.consume(self.inbox, lambda m: None)          # recipient READ it
        mailbox.send(self.inbox, {"fix": "y"}, sender="recovery", supersedes=first)
        got = []
        mailbox.consume(self.inbox, lambda m: got.append(m))
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["superseded_was_unread"],
                         "already-read must be distinguishable from cancelled — "
                         "that distinction is the whole value")


class ConcurrentDrainIsNotAnError(unittest.TestCase):
    """T-2279 (archivist): T-2259 made concurrent drain a FEATURE — an idle main
    seat takes the queue instead of waiting on a long ACP turn. The loser of that
    race then finds the message already gone at ack time, and the ENOENT was being
    recorded as `ack-failed`. Her bridge log carried 22 real-looking
    "No such file or directory" lines in ONE iteration, which makes a healthy race
    read as a broken transport."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "mb"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_message_the_peer_acked_is_not_an_error(self):
        mid = mailbox.send(self.inbox, {"n": 1}, sender="peer")

        def steal(msg):
            # the peer drain path acks it while we are inside the handler
            for d in (self.inbox / ".inflight").glob("*/" + msg["id"]):
                d.rename(self.inbox / "cur" / msg["id"])

        res = mailbox.consume(self.inbox, steal)
        self.assertEqual(res["errors"], [],
                         "a message the peer already acked is the EXPECTED outcome "
                         "of concurrent drain, not a transport failure")
        self.assertEqual(res["already_acked"], [mid])
        self.assertEqual(res["processed"], [])
        self.assertTrue((self.inbox / "cur" / mid).exists())

    def test_a_REAL_ack_failure_still_surfaces_loudly(self):
        """The half that must not regress. Catching OSError broadly here would
        swallow a permissions/IO failure to silence the benign ENOENT — trading a
        noisy true signal for a quiet false one."""
        mailbox.send(self.inbox, {"n": 1}, sender="peer")
        real_rename = os.rename

        def boom(src, dst, *a, **k):
            if "/cur/" in str(dst):
                raise PermissionError(13, "denied")
            return real_rename(src, dst, *a, **k)

        os.rename = boom
        try:
            res = mailbox.consume(self.inbox, lambda m: None)
        finally:
            os.rename = real_rename
        self.assertEqual(len(res["errors"]), 1, "a real ack failure must be reported")
        self.assertIn("ack-failed", res["errors"][0]["error"])
        self.assertEqual(res["already_acked"], [])

    def test_the_peer_acked_message_is_NOT_requeued(self):
        """Requeueing would resurrect a message somebody already consumed."""
        mid = mailbox.send(self.inbox, {"n": 1}, sender="peer")

        def steal(msg):
            for d in (self.inbox / ".inflight").glob("*/" + msg["id"]):
                d.rename(self.inbox / "cur" / msg["id"])

        res = mailbox.consume(self.inbox, steal)
        self.assertFalse((self.inbox / "new" / mid).exists(),
                         "must not reappear in new/ — the peer has it")
        # ⚠️ AND NO ERROR EITHER. Attempting the requeue cannot actually resurrect
        # the message (the file is gone), so the only trace of the bug is a
        # spurious "requeue-failed" in errors[] — which is the very noise this
        # ticket exists to remove. Without this assertion the mutation that
        # re-adds the requeue passes, because reality catches what the test did not.
        self.assertEqual(res["errors"], [],
                         "the peer-acked path must add NO error, including no "
                         "requeue-failed")


class TestSenderSessionIsVisibleToTheRecipient(unittest.TestCase):
    """T-2230: two concurrent sessions of one persona were one name to a
    recipient, who acted on a review from a seat that could not do what it
    claimed. `sender` comes from $USER and cannot tell them apart."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="t2230-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox = Path(self.tmp) / "specialist-peer"
        mb.provision(self.inbox)
        self._env = os.environ.get(mb._SESSION_ENV_VAR)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._env is None:
            os.environ.pop(mb._SESSION_ENV_VAR, None)
        else:
            os.environ[mb._SESSION_ENV_VAR] = self._env

    def _as_session(self, value):
        if value is None:
            os.environ.pop(mb._SESSION_ENV_VAR, None)
        else:
            os.environ[mb._SESSION_ENV_VAR] = value

    def _records(self):
        out = []
        mb.consume(self.inbox, out.append)
        return out

    def test_session_reaches_the_recipient(self) -> None:
        self._as_session("sess-aaaa")
        mb.send(str(self.inbox), {"k": 1}, sender="specialist", subject="s")
        self.assertEqual(self._records()[0]["sender_session"], "sess-aaaa")

    def test_two_sessions_of_one_persona_are_distinguishable(self) -> None:
        """The reported failure, in its minimal form: same sender, two seats."""
        self._as_session("sess-with-images")
        mb.send(str(self.inbox), {"review": "ok"}, sender="specialist", subject="s1")
        self._as_session("sess-no-images")
        mb.send(str(self.inbox), {"review": "ok"}, sender="specialist", subject="s2")
        seen = self._records()
        self.assertEqual(len(seen), 2)
        self.assertEqual(
            {r["sender_session"] for r in seen}, {"sess-with-images", "sess-no-images"}
        )
        self.assertEqual({r["sender"] for r in seen}, {"specialist"})

    def test_absent_session_is_none_never_fabricated(self) -> None:
        """codex exposes no session id. None must mean 'no session identity
        available', and a reader must never receive an invented one — a field
        that overstates its reach recreates this bug somewhere new."""
        self._as_session(None)
        mb.send(str(self.inbox), {"k": 1}, sender="codex", subject="s")
        self.assertIsNone(self._records()[0]["sender_session"])

    def test_empty_session_var_is_treated_as_absent(self) -> None:
        self._as_session("")
        mb.send(str(self.inbox), {"k": 1}, sender="specialist", subject="s")
        self.assertIsNone(self._records()[0]["sender_session"])


class TestDedupeIsPerSessionNotPerPersona(unittest.TestCase):
    """T-2230, the half that is easy to miss: dedupe_pending keyed on
    (sender, subject, payload, severity), so two CONCURRENT sessions of one
    persona sending identical content collapsed into ONE message — destroying
    exactly the fact the recipient most needed. Two agreeing reports from one
    persona are either corroboration or one seat's work counted twice."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="t2230-dedupe-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox = Path(self.tmp) / "peer"
        mb.provision(self.inbox)
        self._env = os.environ.get(mb._SESSION_ENV_VAR)
        self.addCleanup(
            lambda: os.environ.__setitem__(mb._SESSION_ENV_VAR, self._env)
            if self._env is not None else os.environ.pop(mb._SESSION_ENV_VAR, None)
        )

    def test_same_session_identical_content_still_dedupes(self) -> None:
        """A retry is the SAME session — the case this key exists for is
        unchanged. Asserted first, because a precision gain that breaks retry
        dedupe would be a regression wearing a fix's clothes."""
        os.environ[mb._SESSION_ENV_VAR] = "sess-a"
        first = mb.send(str(self.inbox), {"k": 1}, sender="specialist", subject="s")
        again = mb.send(str(self.inbox), {"k": 1}, sender="specialist", subject="s")
        self.assertEqual(first, again)
        self.assertEqual(len(list((self.inbox / "new").iterdir())), 1)

    def test_different_sessions_identical_content_both_land(self) -> None:
        os.environ[mb._SESSION_ENV_VAR] = "sess-a"
        first = mb.send(str(self.inbox), {"k": 1}, sender="specialist", subject="s")
        os.environ[mb._SESSION_ENV_VAR] = "sess-b"
        second = mb.send(str(self.inbox), {"k": 1}, sender="specialist", subject="s")
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((self.inbox / "new").iterdir())), 2)

    def test_sessionless_senders_keep_the_old_behaviour_exactly(self) -> None:
        """codex-shaped senders must not lose retry dedupe as a side effect."""
        os.environ.pop(mb._SESSION_ENV_VAR, None)
        first = mb.send(str(self.inbox), {"k": 1}, sender="codex", subject="s")
        again = mb.send(str(self.inbox), {"k": 1}, sender="codex", subject="s")
        self.assertEqual(first, again)
        self.assertEqual(len(list((self.inbox / "new").iterdir())), 1)


class TestThePidIsNotASessionDiscriminator(unittest.TestCase):
    """T-2230 pins the REFUTATION, so the cheap-looking fix is not re-proposed.

    The ticket's own first candidate was to surface the maildir filename's pid.
    Measured against the live corpus it fails: 76 specialist-signed messages in
    archivist's cur/ carry 76 DISTINCT pids, because each send is a fresh
    short-lived process. This reproduces that property in miniature rather than
    citing the measurement, so it stays true rather than becoming a claim in a
    docstring."""

    def test_consecutive_sends_from_one_session_carry_different_pids(self) -> None:
        tmp = tempfile.mkdtemp(prefix="t2230-pid-")
        self.addCleanup(shutil.rmtree, tmp, True)
        inbox = Path(tmp) / "peer"
        mb.provision(inbox)
        code = (
            "import sys, mailbox as mb;"
            f"print(mb.send({str(inbox)!r}, {{'n': sys.argv[1]}}, sender='specialist'))"
        )
        names = []
        for n in ("1", "2"):
            res = subprocess.run(
                [sys.executable, "-c", code, n],
                cwd=str(Path(__file__).resolve().parent),
                capture_output=True, text=True, check=True,
            )
            names.append(res.stdout.strip())
        pids = {name.split(".")[1] for name in names}
        self.assertEqual(
            len(pids), 2,
            "two separate send PROCESSES produced the same pid — if this ever "
            "holds, re-read T-2230 before concluding the pid identifies a session",
        )


class ProcessBatchTest(unittest.TestCase):
    """T-2352: consume(batch_handler=...) delivers a whole snapshot in one call,
    all-or-nothing ack/requeue, and NEVER attributes a batch failure to the DLQ
    (attribution is the per-message fallback's job). Unreadable files are the one
    exception — corrupt to a single id — and are still quarantined individually."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_batch_acks_all_in_one_call(self):
        mids = [mailbox.send(self.inbox, {"i": i}, sender="orchestrator") for i in range(3)]
        seen = []

        def batch_handler(msgs):
            seen.append([m["id"] for m in msgs])  # exactly one call, all N msgs

        result = mailbox.consume(self.inbox, batch_handler=batch_handler)

        self.assertEqual(len(seen), 1, "one batch call for the whole snapshot")
        self.assertEqual(sorted(seen[0]), sorted(mids))
        self.assertEqual(sorted(result["processed"]), sorted(mids))
        for mid in mids:
            self.assertTrue((self.inbox / "cur" / mid).exists())

    def test_failed_batch_requeues_all_and_never_quarantines(self):
        mids = [mailbox.send(self.inbox, {"i": i}, sender="orchestrator") for i in range(3)]

        def batch_handler(msgs):
            raise RuntimeError("whole-batch failure")

        # dlq_max_attempts=1 would quarantine a per-message failure on the first
        # try; the batch path must STILL not quarantine, however many attempts.
        for _ in range(4):
            result = mailbox.consume(self.inbox, batch_handler=batch_handler,
                                     dlq_max_attempts=1)
            self.assertEqual(result["processed"], [])
            self.assertEqual(result["quarantined"], [],
                             "a batch failure is unattributable — never quarantine")
        for mid in mids:  # all still pending for the per-message fallback
            self.assertTrue((self.inbox / "new" / mid).exists())

    def test_unreadable_file_is_quarantined_individually_readable_batch_delivered(self):
        good = [mailbox.send(self.inbox, {"i": i}, sender="orchestrator") for i in range(2)]
        # Hand-plant a corrupt file directly in new/ (a torn/garbage record).
        bad_name = "9999999999999999999.000001.000000.orchestrator"
        (self.inbox / "new" / bad_name).write_text("{not valid json", encoding="utf-8")
        delivered = []

        def batch_handler(msgs):
            delivered.append([m["id"] for m in msgs])

        result = mailbox.consume(self.inbox, batch_handler=batch_handler,
                                 dlq_max_attempts=1)

        # The corrupt file is attributable to its own id -> quarantined; the two
        # readable messages still go through the one batch turn and ack.
        self.assertEqual(len(delivered), 1)
        self.assertEqual(sorted(delivered[0]), sorted(good))
        self.assertEqual(sorted(result["processed"]), sorted(good))
        self.assertTrue(any(q["id"] == bad_name for q in result["quarantined"]))
        self.assertTrue((self.inbox / "failed" / bad_name).exists())

    def test_consume_requires_exactly_one_handler(self):
        with self.assertRaises(ValueError):
            mailbox.consume(self.inbox)  # neither
        with self.assertRaises(ValueError):
            mailbox.consume(self.inbox, lambda m: None, batch_handler=lambda ms: None)

    def test_batch_works_on_the_unleased_drain_path(self):
        mids = [mailbox.send(self.inbox, {"i": i}, sender="orchestrator") for i in range(2)]

        def batch_handler(msgs):
            pass  # clean

        result = mailbox.consume(self.inbox, batch_handler=batch_handler,
                                 use_lease=False)
        self.assertEqual(sorted(result["processed"]), sorted(mids))


if __name__ == "__main__":
    unittest.main()
