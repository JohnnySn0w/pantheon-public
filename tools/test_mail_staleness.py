"""Tests for tools.mail_staleness — the pure mail-staleness detector (T-2157)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as the other tools tests.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.mail_staleness import (
    KIND_IDLE_WITH_UNREAD,
    KIND_NEVER_RECEIVED,
    InboxObservation,
    InboxState,
    detect,
)

N = 3          # idle_samples_required, small for readability
RE = 5         # realert_after_samples


def obs(unread=1, pane_idle=True, age=600.0, in_flight=0, ever_received=True,
        consumer_lease_held=False, readable=True):
    return InboxObservation(
        unread=unread,
        in_flight=in_flight,
        oldest_unread_age_s=age if unread else None,
        pane_idle=pane_idle,
        ever_received=ever_received,
        consumer_lease_held=consumer_lease_held,
        readable=readable,
    )


def run(seq, *, n=N, re_=RE, inbox="specialist"):
    """Feed a sequence of observations through detect(), returning
    (alerts_per_sample, final_state)."""
    state = {}
    out = []
    for o in seq:
        alerts, state = detect(
            {inbox: o}, state, idle_samples_required=n, realert_after_samples=re_
        )
        out.append(alerts)
    return out, state[inbox]


class IdleWithUnread(unittest.TestCase):
    def test_alerts_only_once_N_idle_unread_samples_accumulate(self):
        per, final = run([obs()] * N)
        self.assertEqual([len(a) for a in per], [0, 0, 1])
        self.assertEqual(final.idle_unread_samples, N)
        self.assertEqual(per[-1][0].kind, KIND_IDLE_WITH_UNREAD)
        self.assertEqual(per[-1][0].samples, N)

    def test_does_not_alert_before_N(self):
        per, _ = run([obs()] * (N - 1))
        self.assertEqual(sum(len(a) for a in per), 0)

    def test_alert_text_states_the_observation_and_the_count(self):
        per, _ = run([obs(unread=12, age=3600.0)] * N)
        text = per[-1][0].text
        self.assertIn("specialist", text)
        self.assertIn("12 unread", text)
        self.assertIn(f"{N} consecutive samples", text)
        self.assertIn("OBSERVED idle", text)
        self.assertIn("60min", text)

    def test_age_formatting_across_the_ranges(self):
        for seconds, expected in ((45, "45s"), (600, "10min"), (7200, "2.0h")):
            per, _ = run([obs(age=float(seconds))] * N)
            self.assertIn(expected, per[-1][0].text)

    def test_unknown_age_is_labelled_not_blank(self):
        o = InboxObservation(
            unread=1, in_flight=0, oldest_unread_age_s=None,
            pane_idle=True, ever_received=True,
        )
        per, _ = run([o] * N)
        self.assertIn("oldest unknown", per[-1][0].text)

    def test_in_flight_is_mentioned_only_when_nonzero(self):
        per, _ = run([obs(in_flight=2)] * N)
        self.assertIn("2 in flight", per[-1][0].text)
        per2, _ = run([obs(in_flight=0)] * N)
        self.assertNotIn("in flight", per2[-1][0].text)


class UnreadableIsNotEmptyT2310(unittest.TestCase):
    """T-2310 (specialist): _count returned 0 on OSError, so an unreadable inbox
    looked EMPTY -> ever_received=False -> the never-RECEIVED wiring alert.

    Three costs, worst last: the real unread go invisible; the alert text
    actively misdirects ("a kick cannot fix it" about mail sitting right
    there); and it LATCHES, so that wrong alert is the last thing that inbox
    ever emits.
    """

    def test_CONTROL_a_readable_empty_inbox_still_reports_never_received(self):
        """The control — without it, 'unreadable does not alert never_received'
        could pass simply because nothing ever alerts."""
        per, _ = run([obs(unread=0, ever_received=False)])
        self.assertIn("never_received", [a.kind for s in per for a in s])

    def test_unreadable_never_reports_never_received(self):
        per, _ = run([obs(unread=0, ever_received=False, readable=False)])
        kinds = [a.kind for s in per for a in s]
        self.assertNotIn("never_received", kinds,
                         "an inbox we cannot READ is not an inbox that never received")
        self.assertIn("unreadable", kinds)

    def test_unreadable_alerts_once_then_stays_quiet(self):
        per, _ = run([obs(readable=False)] * 5)
        self.assertEqual(len([a for s in per for a in s]), 1)

    def test_unreadable_does_not_accumulate_the_staleness_ledger(self):
        per, final = run([obs(readable=False)] * (N * 3))
        self.assertEqual(final.idle_unread_samples, 0)
        self.assertEqual(len([a for s in per for a in s]), 1)

    def test_recovery_rearms_so_a_SECOND_outage_alerts_again(self):
        """Latching is what made the original bug terminal; recovery must clear it."""
        per, _ = run([obs(readable=False)] + [obs(unread=0)] + [obs(readable=False)])
        kinds = [a.kind for s in per for a in s]
        self.assertEqual(kinds.count("unreadable"), 2)


class ConsumerLeaseHeldHoldsT2283(unittest.TestCase):
    """T-2283: while the inbox's single-consumer lease is held by a live
    consumer, the agent structurally CANNOT ack its own mail.

    The drain API takes the lease for the whole drain and the ACP bridge's
    handler blocks on a full agent turn inside it (1800s on specialist, deliberate
    per T-1985). Measured live 2026-07-29: lease held 47 minutes, 12 unread,
    pane idle at a prompt -- and this detector fired twice, correctly by its own
    rules and wrongly about the world. Under a held lease "idle with unread" is
    evidence about the agent's TURN LENGTH, not about the transport, so the
    failure direction was crying wolf at the BUSIEST seat.
    """

    def test_held_lease_never_accumulates_however_long_it_is_held(self):
        per, final = run([obs(consumer_lease_held=True)] * (N * 3))
        self.assertEqual([a for s in per for a in s], [],
                         "a healthy long turn must never alert")
        self.assertEqual(final.idle_unread_samples, 0)

    def test_alerts_normally_once_the_lease_is_released(self):
        """Suppression must be exactly as wide as the lease -- no wider."""
        per, _ = run([obs(consumer_lease_held=True)] * N + [obs()] * N)
        self.assertEqual(len([a for s in per for a in s]), 1,
                         "a genuinely stuck inbox must still alert after release")

    def test_held_lease_HOLDS_rather_than_RESETS(self):
        """Same reasoning as busy/unknown: evidence that the agent previously
        reached idle WITHOUT draining is not invalidated by a lease taken now.
        RESET would discard it and let a lease-flickering agent escape forever."""
        pre = [obs()] * (N - 1)                      # accumulate to just below
        per, final = run(pre + [obs(consumer_lease_held=True)] * 3 + [obs()])
        self.assertEqual(len([a for s in per for a in s]), 1,
                         "the pre-lease evidence must survive the hold")

    def test_lease_does_not_suppress_never_received(self):
        """A wiring defect is a different fault and no lease excuses it."""
        per, _ = run([obs(unread=0, ever_received=False, consumer_lease_held=True)])
        kinds = [a.kind for s in per for a in s]
        self.assertIn("never_received", kinds)


class BusyAndUnknownHold(unittest.TestCase):
    """Busy is NOT-YET-EVIDENCE; unknown is not evidence in either direction.
    Neither may alert, and neither may DISCARD accumulated evidence."""

    def test_busy_never_alerts_however_long(self):
        per, final = run([obs(pane_idle=False)] * 50)
        self.assertEqual(sum(len(a) for a in per), 0)
        self.assertEqual(final.idle_unread_samples, 0)

    def test_unknown_never_alerts_however_long(self):
        per, final = run([obs(pane_idle=None)] * 50)
        self.assertEqual(sum(len(a) for a in per), 0)
        self.assertEqual(final.idle_unread_samples, 0)

    def test_busy_HOLDS_rather_than_resets_the_ledger(self):
        # The slow-drain / flicker case: an agent that alternates busy and idle
        # without ever draining must still accumulate. RESET-on-busy would let it
        # escape detection forever — the failure this choice exists to prevent.
        seq = [obs(), obs(pane_idle=False), obs(), obs(pane_idle=False), obs()]
        per, final = run(seq)
        self.assertEqual(final.idle_unread_samples, N)
        self.assertEqual(sum(len(a) for a in per), 1)
        self.assertEqual(per[-1][0].samples, N)

    def test_unknown_HOLDS_rather_than_resets(self):
        seq = [obs(), obs(pane_idle=None), obs(), obs(pane_idle=None), obs()]
        _, final = run(seq)
        self.assertEqual(final.idle_unread_samples, N)


class DrainResets(unittest.TestCase):
    def test_drained_inbox_resets_the_ledger(self):
        seq = [obs(), obs(), obs(unread=0, pane_idle=True), obs(), obs()]
        per, final = run(seq)
        self.assertEqual(sum(len(a) for a in per), 0)
        self.assertEqual(final.idle_unread_samples, 2)

    def test_drained_inbox_never_alerts_even_when_idle(self):
        per, _ = run([obs(unread=0)] * 50)
        self.assertEqual(sum(len(a) for a in per), 0)


class Cooldown(unittest.TestCase):
    def test_does_not_realert_every_sample(self):
        per, _ = run([obs()] * (N + 3))
        self.assertEqual(sum(len(a) for a in per), 1, "must alert once, not per-sample")

    def test_realerts_after_the_cooldown(self):
        # alert at sample N, then RE further samples -> second alert
        per, _ = run([obs()] * (N + RE))
        self.assertEqual(sum(len(a) for a in per), 2)

    def test_drain_clears_the_cooldown_so_a_fresh_stall_alerts_promptly(self):
        seq = [obs()] * N + [obs(unread=0)] + [obs()] * N
        per, _ = run(seq)
        self.assertEqual(sum(len(a) for a in per), 2)
        self.assertEqual(len(per[-1]), 1, "the post-drain stall must alert at N again")


class NeverReceived(unittest.TestCase):
    def test_fires_once_and_is_a_distinct_kind(self):
        per, final = run([obs(unread=0, ever_received=False)] * 10)
        flat = [a for s in per for a in s]
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0].kind, KIND_NEVER_RECEIVED)
        self.assertTrue(final.never_received_alerted)

    def test_text_names_it_as_wiring_not_staleness(self):
        per, _ = run([obs(unread=0, ever_received=False)])
        text = per[0][0].text
        self.assertIn("NEVER received", text)
        self.assertIn("wiring", text)
        self.assertIn("kick cannot fix it", text)

    def test_an_unwired_inbox_can_also_go_stale_independently(self):
        # ever_received False but mail present and the agent idle: both faults.
        seq = [obs(unread=1, ever_received=False)] * N
        per, _ = run(seq)
        flat = [a for s in per for a in s]
        kinds = [a.kind for a in flat]
        self.assertIn(KIND_NEVER_RECEIVED, kinds)
        self.assertIn(KIND_IDLE_WITH_UNREAD, kinds)


class Purity(unittest.TestCase):
    def test_same_inputs_give_same_outputs(self):
        o = {"specialist": obs()}
        s = {"specialist": InboxState(idle_unread_samples=N - 1)}
        a1, s1 = detect(o, s, idle_samples_required=N, realert_after_samples=RE)
        a2, s2 = detect(o, s, idle_samples_required=N, realert_after_samples=RE)
        self.assertEqual(a1, a2)
        self.assertEqual(s1, s2)

    def test_prior_state_is_not_mutated(self):
        s = {"specialist": InboxState(idle_unread_samples=1)}
        detect({"specialist": obs()}, s, idle_samples_required=N, realert_after_samples=RE)
        self.assertEqual(s["specialist"].idle_unread_samples, 1)

    def test_missing_prior_state_defaults_cleanly(self):
        alerts, state = detect(
            {"new-inbox": obs()}, {}, idle_samples_required=N, realert_after_samples=RE
        )
        self.assertEqual(alerts, [])
        self.assertEqual(state["new-inbox"].idle_unread_samples, 1)

    def test_all_inboxes_are_evaluated_independently(self):
        o = {
            "specialist": obs(),                    # idle+unread
            "recovery": obs(pane_idle=False),      # busy
            "codex": obs(unread=0, ever_received=False),
        }
        state = {k: InboxState(idle_unread_samples=N - 1) for k in ("specialist", "recovery")}
        alerts, nxt = detect(
            o, state, idle_samples_required=N, realert_after_samples=RE
        )
        by_inbox = {a.inbox: a.kind for a in alerts}
        self.assertEqual(by_inbox.get("specialist"), KIND_IDLE_WITH_UNREAD)
        self.assertNotIn("recovery", by_inbox)
        self.assertEqual(by_inbox.get("codex"), KIND_NEVER_RECEIVED)
        self.assertEqual(nxt["recovery"].idle_unread_samples, N - 1, "busy holds")


class Validation(unittest.TestCase):
    def test_rejects_nonsense_thresholds(self):
        for kwargs in ({"idle_samples_required": 0}, {"realert_after_samples": 0}):
            full = {"idle_samples_required": N, "realert_after_samples": RE, **kwargs}
            with self.assertRaises(ValueError):
                detect({}, {}, **full)

    def test_empty_observations_is_a_no_op(self):
        alerts, state = detect({}, {}, idle_samples_required=N, realert_after_samples=RE)
        self.assertEqual(alerts, [])
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
