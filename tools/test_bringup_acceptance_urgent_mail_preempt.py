"""Tests for tools.bringup_acceptance.urgent_mail_preempt (T-2585, parent T-2556).

Disposable fixtures only: every test runs against a fresh tempfile.
TemporaryDirectory() tree via real tools.mailbox / tools.urgent_escalation /
the real hook subprocess -- no live real mailbox or a real Telegram/tmux
alert is ever touched (the alert leaf is mocked; everything else is real).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import mailbox
from tools.bringup_acceptance import urgent_mail_preempt
from tools.bringup_acceptance.urgent_mail_preempt import UrgentMailPreemptModule
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    RunContext,
    TargetInventory,
)


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


# --------------------------------------------------------------------- declare


class DeclareTest(unittest.TestCase):
    def test_applies_when_inventory_marks_the_capability_present(self) -> None:
        module = UrgentMailPreemptModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("/hooks/urgent-mail-inject.py"), tier="core", status="applies",
                    module_id="urgent-mail-preempt",
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertTrue(decl.applies)
        self.assertEqual(decl.tier, "core")
        self.assertIn("urgent-mail-preempt-fully-functional", decl.contributes_to)

    def test_does_not_apply_when_inventory_marks_the_capability_absent(self) -> None:
        module = UrgentMailPreemptModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("/hooks/urgent-mail-inject.py"), tier="core", status="absent",
                    module_id="urgent-mail-preempt",
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)

    def test_does_not_apply_with_no_matching_inventory_entry(self) -> None:
        module = UrgentMailPreemptModule()
        inventory = TargetInventory(persona="recovery", entries=())
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)
        self.assertIn("no urgent-mail-preempt inventory entry", decl.reason)


# ---------------------------------------------------------- real-hub guard


class RealHubGuardTest(unittest.TestCase):
    def test_prove_refuses_when_hub_path_is_the_real_hub_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            with mock.patch.object(urgent_mail_preempt, "_REAL_HUB_DEFAULT", ctx.hub_path):
                module = UrgentMailPreemptModule()
                with self.assertRaises(HardStop):
                    module.prove(ctx)
            self.assertFalse(ctx.hub_path.exists())


# ------------------------------------------------------------------ prove() legs


class MarkerWriteLegTest(unittest.TestCase):
    def test_urgent_send_writes_a_marker_matching_the_real_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            mailbox.provision(inbox)
            with mock.patch.dict("os.environ", {"PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED": "1"}):
                detail = urgent_mail_preempt._marker_write_leg(inbox=inbox, nonce="abc123")
            marker = json.loads((inbox / ".urgent_pending").read_text())
            self.assertEqual(marker["msgid"], detail["message_id"])
            self.assertEqual(marker["sender"], urgent_mail_preempt._SENDER_PERSONA)


class HookInjectsAndClearsLegTest(unittest.TestCase):
    def test_raises_when_no_marker_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            mailbox.provision(inbox)
            with self.assertRaises(HardStop):
                urgent_mail_preempt._hook_injects_and_clears_leg(inbox=inbox)

    def test_real_subprocess_injects_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            mailbox.provision(inbox)
            with mock.patch.dict("os.environ", {"PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED": "1"}):
                urgent_mail_preempt._marker_write_leg(inbox=inbox, nonce="hook-test")
                detail = urgent_mail_preempt._hook_injects_and_clears_leg(inbox=inbox)
            self.assertIn(urgent_mail_preempt._SENDER_PERSONA, detail["additional_context"])
            self.assertFalse((inbox / ".urgent_pending").exists())


class RateLimitDowngradeLegTest(unittest.TestCase):
    def test_overflow_send_is_downgraded_never_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            mailbox.provision(inbox)
            with mock.patch.dict("os.environ", {"PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED": "1"}):
                detail = urgent_mail_preempt._rate_limit_downgrade_leg(inbox=inbox, nonce="rate-test")
            self.assertEqual(detail["overflow_severity"], "normal")
            overflow_record = json.loads((inbox / "new" / detail["overflow_id"]).read_text())
            self.assertEqual(overflow_record["severity"], "normal")
            marker = json.loads((inbox / ".urgent_pending").read_text())
            self.assertEqual(marker["msgid"], detail["in_limit_ids"][-1])
            downgrades = (inbox / ".urgent_rate" / f"{urgent_mail_preempt._SENDER_PERSONA}.downgrades.jsonl")
            self.assertEqual(len(downgrades.read_text().splitlines()), 1)


class EscalationLadderLegTest(unittest.TestCase):
    def test_stale_urgent_mail_fires_the_alert_exactly_once(self) -> None:
        # _escalation_ladder_leg mocks urgent_escalation._alert INTERNALLY
        # (the real Telegram/tmux leaf) -- wrapping it in a second, outer
        # mock.patch.object here would just shadow the leg's own inner patch
        # and never observe a call, so this test asserts on the leg's own
        # return value + the durable .urgent_escalated marker instead (the
        # leg itself already HardStops if the internal mock wasn't called
        # exactly once, so reaching `detail` at all proves that happened).
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            inbox = hub / "urgent-preempt-escalation-recipient"
            mailbox.provision(inbox)
            with mock.patch.dict("os.environ", {"PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED": "1"}):
                detail = urgent_mail_preempt._escalation_ladder_leg(hub=hub, inbox=inbox, nonce="esc-test")
            self.assertEqual(detail["alert_agent"], inbox.name)
            self.assertIn(urgent_mail_preempt._SENDER_PERSONA, detail["alert_message"])
            self.assertTrue((inbox / ".urgent_escalated" / detail["message_id"]).exists())


# ------------------------------------------------------------ prove() end-to-end


class ProveEndToEndTest(unittest.TestCase):
    def test_prove_proves_all_four_legs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = UrgentMailPreemptModule()
            evidence = module.prove(ctx)

            self.assertEqual(evidence.status, "proven")
            self.assertTrue(evidence.trace_path.exists())
            trace_events = [json.loads(line) for line in evidence.trace_path.read_text().splitlines()]
            self.assertEqual(
                [event["phase"] for event in trace_events],
                [
                    "baseline", "marker_write", "hook_injects_and_clears", "rate_limit_downgrade",
                    "escalation_ladder", "live_leg_deferred", "verdict",
                ],
            )
            self.assertEqual(set(evidence.detail["legs"]), {
                "marker_write", "hook_injects_and_clears", "rate_limit_downgrade",
                "escalation_ladder", "live_leg_deferred",
            })

            row = module.report(evidence)
            self.assertEqual(row.status, "proven")
            self.assertIn(urgent_mail_preempt.LIVE_LEG_LEDGER_ID, row.summary)
            self.assertIn(urgent_mail_preempt.LIVE_LEG_TICKET, row.summary)
            self.assertIn("NOT independently re-verified", row.summary)

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)
            self.assertFalse((ctx.hub_path / urgent_mail_preempt._MARKER_RECIPIENT_PERSONA).exists())
            self.assertFalse((ctx.hub_path / urgent_mail_preempt._RATE_RECIPIENT_PERSONA).exists())
            self.assertFalse((ctx.hub_path / urgent_mail_preempt._ESCALATION_RECIPIENT_PERSONA).exists())


# --------------------------------------------------------------------- restore


class RestoreTest(unittest.TestCase):
    def test_restore_before_prove_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _run_context(Path(tmp))
            module = UrgentMailPreemptModule()
            with self.assertRaises(HardStop):
                module.restore(ctx)

    def test_restore_after_prove_leaves_zero_residue_on_a_second_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = UrgentMailPreemptModule()
            module.prove(ctx)
            module.restore(ctx)

            from tools.bringup_acceptance import restore as restore_mod

            post_again = restore_mod.record_baseline(module._baseline.target)
            restore_mod.verify_zero_residue(module._baseline, post_again)  # no raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
