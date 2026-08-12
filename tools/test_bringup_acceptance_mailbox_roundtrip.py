"""Tests for tools.bringup_acceptance.mailbox_roundtrip (T-2556 phase 4 / T-2563).

Disposable fixtures only: every test runs against a fresh tempfile.
TemporaryDirectory() tree via real tools.mailbox calls -- no live real
mailbox is ever touched (mirrors test_bringup_acceptance_restore.py's own
"real temp dirs, no live infra" convention; the D5 real-path bar is met by
exercising the real transport against a disposable tree, not a fake).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import mailbox
from tools.bringup_acceptance import mailbox_roundtrip
from tools.bringup_acceptance.mailbox_roundtrip import MailboxRoundtripModule
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    RunContext,
    TargetInventory,
)
from tools.persona_bringup import _reply_inbox as persona_bringup_reply_inbox


def _run_context(root: Path) -> RunContext:
    return RunContext(
        run_id="20260805T000000Z",
        persona="recovery",
        account="claude-fallback",
        harness="claude",
        profile="interactive",
        hub_path=root / "hub",
        state_path=root / "state",
        evidence_root=root / "evidence",
        baseline_ref="deadbeef" * 5,
    )


def _provisioned(path: Path) -> Path:
    mailbox.provision(path)
    return path


# --------------------------------------------------------------------- declare


class DeclareTest(unittest.TestCase):
    def test_applies_when_inventory_marks_the_mailbox_present(self) -> None:
        module = MailboxRoundtripModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("/hub/recovery"), tier="core", status="applies",
                    module_id="mailbox-roundtrip",
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertTrue(decl.applies)
        self.assertEqual(decl.tier, "core")
        self.assertIn("mailbox-fully-functional", decl.contributes_to)

    def test_does_not_apply_when_inventory_marks_the_mailbox_absent(self) -> None:
        module = MailboxRoundtripModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("/hub/recovery"), tier="core", status="absent",
                    module_id="mailbox-roundtrip",
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)

    def test_does_not_apply_with_no_matching_inventory_entry(self) -> None:
        module = MailboxRoundtripModule()
        inventory = TargetInventory(persona="recovery", entries=())
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)
        self.assertIn("no mailbox-roundtrip inventory entry", decl.reason)


# ---------------------------------------------------------- real-hub guard


class RealHubGuardTest(unittest.TestCase):
    def test_prove_refuses_when_hub_path_is_the_real_hub_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            with mock.patch.object(mailbox_roundtrip, "_REAL_HUB_DEFAULT", ctx.hub_path):
                module = MailboxRoundtripModule()
                with self.assertRaises(HardStop):
                    module.prove(ctx)
            # The guard must fire before anything is created.
            self.assertFalse(ctx.hub_path.exists())

    def test_prove_and_restore_never_touch_a_path_outside_the_disposable_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "stand-in-real-hub"
            sentinel.mkdir()
            ctx = _run_context(root)
            with mock.patch.object(mailbox_roundtrip, "_REAL_HUB_DEFAULT", sentinel):
                module = MailboxRoundtripModule()
                evidence = module.prove(ctx)
                self.assertEqual(evidence.status, "proven")
                self.assertEqual(list(sentinel.iterdir()), [])
                module.restore(ctx)
                self.assertEqual(list(sentinel.iterdir()), [])


# --------------------------------------------------------- _verify_pending_dedupe


class VerifyPendingDedupeTest(unittest.TestCase):
    def test_raises_on_id_mismatch(self) -> None:
        with self.assertRaises(HardStop):
            mailbox_roundtrip._verify_pending_dedupe(
                first_id="a", dedupe_id="b", pending_names=("a",)
            )

    def test_raises_on_extra_pending_file(self) -> None:
        with self.assertRaises(HardStop):
            mailbox_roundtrip._verify_pending_dedupe(
                first_id="a", dedupe_id="a", pending_names=("a", "stray")
            )

    def test_passes_on_a_clean_match(self) -> None:
        mailbox_roundtrip._verify_pending_dedupe(
            first_id="a", dedupe_id="a", pending_names=("a",)
        )  # no raise


# --------------------------------------------------------------- _verify_chain


class VerifyChainTest(unittest.TestCase):
    def test_raises_when_message_is_missing_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._verify_chain(
                    recipient_inbox=inbox, message_id="missing-id",
                    consume_result={"skipped": False, "processed": ["missing-id"], "quarantined": []},
                    received=[{"id": "missing-id"}],
                )

    def test_raises_when_result_reports_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._verify_chain(
                    recipient_inbox=inbox, message_id="m1",
                    consume_result={"skipped": False, "processed": [], "quarantined": [{"id": "m1"}]},
                    received=[],
                )

    def test_raises_when_message_still_in_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / "new" / "m1").write_text("{}")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._verify_chain(
                    recipient_inbox=inbox, message_id="m1",
                    consume_result={"skipped": False, "processed": ["m1"], "quarantined": []},
                    received=[{"id": "m1"}],
                )

    def test_raises_on_orphan_inflight_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / "cur" / "m1").write_text("{}")
            claim_dir = inbox / ".inflight" / "stale-claim"
            claim_dir.mkdir(parents=True)
            (claim_dir / "leftover").write_text("{}")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._verify_chain(
                    recipient_inbox=inbox, message_id="m1",
                    consume_result={"skipped": False, "processed": ["m1"], "quarantined": []},
                    received=[{"id": "m1"}],
                )

    def test_raises_when_handler_observed_wrong_message_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / "cur" / "m1").write_text("{}")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._verify_chain(
                    recipient_inbox=inbox, message_id="m1",
                    consume_result={"skipped": False, "processed": ["m1"], "quarantined": []},
                    received=[],
                )

    def test_passes_on_a_clean_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / "cur" / "m1").write_text("{}")
            mailbox_roundtrip._verify_chain(
                recipient_inbox=inbox, message_id="m1",
                consume_result={"skipped": False, "processed": ["m1"], "quarantined": []},
                received=[{"id": "m1"}],
            )  # no raise


# ------------------------------------------------------------------ _read_ack


class ReadAckTest(unittest.TestCase):
    def test_raises_when_acks_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._read_ack(inbox, "m1")

    def test_raises_when_no_record_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / ".acks.jsonl").write_text(
                json.dumps({"id": "other", "ts_ns": 1, "label": "x", "pid": os.getpid(), "uid": os.getuid()}) + "\n"
            )
            with self.assertRaises(HardStop):
                mailbox_roundtrip._read_ack(inbox, "m1")

    def test_raises_on_pid_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / ".acks.jsonl").write_text(
                json.dumps({
                    "id": "m1", "ts_ns": 1, "label": mailbox_roundtrip._CONSUME_LABEL,
                    "pid": os.getpid() + 1, "uid": os.getuid(),
                }) + "\n"
            )
            with self.assertRaises(HardStop):
                mailbox_roundtrip._read_ack(inbox, "m1")

    def test_returns_the_matching_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            record = {
                "id": "m1", "ts_ns": 1, "label": mailbox_roundtrip._CONSUME_LABEL,
                "pid": os.getpid(), "uid": os.getuid(),
            }
            (inbox / ".acks.jsonl").write_text(json.dumps(record) + "\n")
            self.assertEqual(mailbox_roundtrip._read_ack(inbox, "m1"), record)


# ------------------------------------------------------------ _read_sole_reply


class ReadSoleReplyTest(unittest.TestCase):
    def test_raises_when_no_reply_landed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            with self.assertRaises(HardStop):
                mailbox_roundtrip._read_sole_reply(inbox)

    def test_raises_when_more_than_one_reply_landed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / "new" / "r1").write_text(json.dumps({"payload": {"correlation_id": "a"}}))
            (inbox / "new" / "r2").write_text(json.dumps({"payload": {"correlation_id": "b"}}))
            with self.assertRaises(HardStop):
                mailbox_roundtrip._read_sole_reply(inbox)

    def test_reads_the_sole_reply_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            (inbox / "new" / "r1").write_text(json.dumps({"payload": {"correlation_id": "a"}}))
            name, payload = mailbox_roundtrip._read_sole_reply(inbox)
            self.assertEqual(name, "r1")
            self.assertEqual(payload["correlation_id"], "a")


# ------------------------------------------------ reply-route fidelity (D11)


class ReplyRouteFidelityTest(unittest.TestCase):
    """The reply_route shape this module emits MUST match what the real
    persona_bringup._reply_inbox reads -- reimplementing it locally (see
    DECISIONS) is only safe if it stays byte-for-byte compatible with the
    real consumer. Read from source, not hand-invented (D11)."""

    def test_emitted_reply_route_shape_resolves_via_the_real_reply_inbox(self) -> None:
        sender_inbox = Path("/disposable/hub/roundtrip-sender")
        record = {
            "sender": "roundtrip-recipient",
            "payload": {
                "nonce": "abc123",
                "correlation_id": "abc123",
                "reply_route": {"recipient_inbox": str(sender_inbox)},
            },
        }
        resolved = persona_bringup_reply_inbox(record, reply_hub=Path("/irrelevant/reply-hub"))
        self.assertEqual(resolved, sender_inbox)

    def test_a_relative_recipient_inbox_is_rejected_by_the_real_reply_inbox(self) -> None:
        # Pin the OTHER half of the real contract: _reply_inbox only accepts
        # an ABSOLUTE recipient_inbox string. This module always emits
        # str(<absolute Path>), so this documents why that matters.
        record = {
            "sender": "roundtrip-recipient",
            "payload": {"reply_route": {"recipient_inbox": "relative/path"}},
        }
        self.assertIsNone(persona_bringup_reply_inbox(record, reply_hub=Path("/irrelevant")))


# --------------------------------- mailbox substrate dedupe contract (scope pin)


class MailboxSubstrateDedupeContractTest(unittest.TestCase):
    """Pins BOTH real redelivery shapes this module's IDEMPOTENCY SCOPE note
    relies on, empirically, against the REAL tools.mailbox (not a fake):

    (a) a byte-identical mailbox.send() retry while the original is still
        PENDING (unconsumed, in new/) collapses to the same id -- T-1935's
        `_find_pending_duplicate`, new/-only.
    (b) NEITHER a byte-identical mailbox.send() retry AFTER the original was
        acked into cur/, NOR a message with the SAME on-disk id physically
        reappearing in new/ post-ack (a crash/restore/replay shape, not a
        mailbox.send() call) is recognized or skipped by the transport --
        tools/mailbox.py:1027-1139 (`_process`) calls the handler
        unconditionally for every claimed name; nothing checks cur/ or
        .acks.jsonl first. `already_acked` (T-2279) is a DIFFERENT thing --
        a concurrent-drain RACE on mail nobody has acked yet, not a
        persistent already-acked recognition. If either of these ever
        changes, this test breaks and flags that mailbox_roundtrip's scope
        claim is stale."""

    def test_pending_resend_dedupes_but_post_ack_resend_is_a_fresh_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            payload = {"marker": "pin-test"}

            first = mailbox.send(str(inbox), payload, "sender-a", subject="s")
            again = mailbox.send(str(inbox), payload, "sender-a", subject="s")
            self.assertEqual(again, first)
            self.assertEqual([p.name for p in (inbox / "new").iterdir()], [first])

            result = mailbox.consume(str(inbox), lambda msg: None, label="pin-test")
            self.assertEqual(result["processed"], [first])

            resent = mailbox.send(str(inbox), payload, "sender-a", subject="s")
            self.assertNotEqual(resent, first)
            self.assertEqual([p.name for p in (inbox / "new").iterdir()], [resent])

    def test_a_same_id_reappearing_in_new_post_ack_is_reprocessed_not_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = _provisioned(Path(tmp) / "inbox")
            calls: list[str] = []

            first = mailbox.send(str(inbox), {"marker": "pin-test"}, "sender-a", subject="s")
            mailbox.consume(str(inbox), lambda msg: calls.append(msg["id"]), label="pin-test")
            self.assertEqual(calls, [first])
            self.assertEqual((inbox / ".acks.jsonl").read_text().count("\n"), 1)

            # Simulate a same-id reappearance in new/ (e.g. a restore/replay),
            # NOT a fresh mailbox.send() call -- the transport never produces
            # this on its own, but nothing in _process() distinguishes it from
            # a first delivery either.
            shutil.copy(inbox / "cur" / first, inbox / "new" / first)

            result = mailbox.consume(str(inbox), lambda msg: calls.append(msg["id"]), label="pin-test")
            self.assertEqual(result["processed"], [first])
            self.assertEqual(calls, [first, first])  # handler ran a SECOND time
            self.assertEqual((inbox / ".acks.jsonl").read_text().count("\n"), 2)  # second ack line


# ------------------------------------------------------------ prove() end-to-end


class ProveEndToEndTest(unittest.TestCase):
    def test_prove_proves_the_full_correlated_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = MailboxRoundtripModule()
            evidence = module.prove(ctx)

            self.assertEqual(evidence.status, "proven")
            self.assertTrue(evidence.trace_path.exists())
            trace_events = [json.loads(line) for line in evidence.trace_path.read_text().splitlines()]
            self.assertEqual(
                [event["phase"] for event in trace_events],
                [
                    "baseline", "send", "idempotency-pending-dedupe", "consume",
                    "chain-verify", "reply-route-verify",
                ],
            )

            chain = json.loads(evidence.evidence_paths[0].read_text())
            self.assertEqual(chain["nonce"], evidence.detail["nonce"])
            self.assertEqual(chain["dedupe_id"], chain["message_id"])
            self.assertEqual(chain["reply_payload"]["correlation_id"], chain["nonce"])
            self.assertEqual(chain["ack_record"]["id"], chain["message_id"])

            recipient_inbox = ctx.hub_path / mailbox_roundtrip._RECIPIENT_PERSONA
            sender_inbox = ctx.hub_path / mailbox_roundtrip._SENDER_PERSONA
            self.assertFalse((recipient_inbox / "new" / chain["message_id"]).exists())
            self.assertTrue((recipient_inbox / "cur" / chain["message_id"]).exists())
            self.assertEqual(
                [p.name for p in (sender_inbox / "new").iterdir()], [chain["reply_id"]]
            )

            row = module.report(evidence)
            self.assertEqual(row.status, "proven")
            self.assertIn("substrate-proven", row.summary)
            self.assertNotIn("mailbox delivery proven", row.summary)
            self.assertIn("T-2570", row.summary)

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            self.assertFalse(sender_inbox.exists())
            self.assertFalse(recipient_inbox.exists())


# ----------------------------------------- adversarial: never trust the dict


class AdversarialGroundTruthTest(unittest.TestCase):
    def test_prove_rejects_a_consume_result_that_disk_state_contradicts(self) -> None:
        """mailbox.consume() is monkeypatched to CLAIM success over whatever
        is actually sitting in new/ -- without doing any real work (no move,
        no ack, no handler call). prove() must still HardStop, because
        _verify_chain re-reads disk rather than trusting the returned dict."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = MailboxRoundtripModule()

            def _fake_consume(inbox, handler, **kwargs):
                names = [p.name for p in (Path(inbox) / "new").iterdir() if p.is_file()]
                return {"skipped": False, "processed": names, "errors": [], "quarantined": [], "reaped": {}}

            with mock.patch.object(mailbox_roundtrip.mailbox, "consume", side_effect=_fake_consume):
                with self.assertRaises(HardStop) as caught:
                    module.prove(ctx)
            self.assertIn("still present in new/", str(caught.exception))


# --------------------------------------------------------------------- restore


class RestoreTest(unittest.TestCase):
    def test_restore_before_prove_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            module = MailboxRoundtripModule()
            with self.assertRaises(HardStop):
                module.restore(ctx)

    def test_restore_after_prove_leaves_zero_residue_on_a_second_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = MailboxRoundtripModule()
            module.prove(ctx)
            module.restore(ctx)

            # Independently re-poll the same baseline target: verify_zero_residue
            # must not raise a SECOND time either -- proves the clean state is
            # durable, not just self-reported by restore()'s own single check.
            from tools.bringup_acceptance import restore as restore_mod

            post_again = restore_mod.record_baseline(module._baseline.target)
            restore_mod.verify_zero_residue(module._baseline, post_again)  # no raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
