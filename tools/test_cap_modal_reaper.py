#!/usr/bin/env python3
"""Hermetic tests for cap_modal_reaper.py increment 1 (T-4024): the decision core,
the state read, and the live-pane classifier. No live fleet, no sudo, no keystrokes."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

try:
    from tools import cap_modal_reaper as r
except ImportError:  # bare run with cwd=tools/
    import cap_modal_reaper as r  # type: ignore

NOW = datetime(2026, 9, 27, 18, 0, 0, tzinfo=timezone.utc)


def _state(**over):
    """A fully-passing state; override fields per case."""
    s = {
        "schema": "pantheon-persona-mid-session-cap/v1",
        "modal_present": True,
        "modal_kind": "weekly-limit",
        "detected_at": "2026-09-27T16:00:00Z",
        "observed_at": "2026-09-27T17:59:30Z",  # 30s before NOW -> fresh
        "reset_at": "2026-09-27T17:00:00Z",     # 1h before NOW -> window rolled
        "reset_source": "parsed",
    }
    s.update(over)
    return s


class TestReapDisposition(unittest.TestCase):
    def test_all_gates_pass_reaps(self):
        self.assertEqual(r.reap_disposition(_state(), NOW), "reap")

    def test_empty_state_noop(self):
        self.assertEqual(r.reap_disposition({}, NOW), "noop:no-state")

    def test_modal_absent_noop(self):
        self.assertEqual(r.reap_disposition(_state(modal_present=False), NOW), "noop:no-modal")
        self.assertEqual(r.reap_disposition(_state(modal_present=None), NOW), "noop:no-modal")

    def test_candidate_only_never_reaps(self):
        # A classifying-but-unconfirmed sample: modal_present False, candidate set.
        # The reaper must key on modal_present ONLY, never candidate_modal_kind.
        s = _state(modal_present=False, modal_kind=None, candidate_modal_kind="monthly-spend-limit")
        self.assertEqual(r.reap_disposition(s, NOW), "noop:no-modal")

    def test_no_reset_at_noop(self):
        self.assertEqual(r.reap_disposition(_state(reset_at=None), NOW), "noop:no-reset-at")

    def test_pre_reset_noop(self):
        # reset_at in the future -> window not rolled -> Esc would just re-pop.
        future = (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(r.reap_disposition(_state(reset_at=future), NOW), "noop:pre-reset")

    def test_stale_observed_at_noop(self):
        # observed_at far in the past -> detector not currently writing -> not authoritative.
        stale = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(r.reap_disposition(_state(observed_at=stale), NOW), "noop:stale")

    def test_missing_observed_at_noop(self):
        self.assertEqual(r.reap_disposition(_state(observed_at=None), NOW), "noop:no-observed-at")

    def test_exactly_at_reset_reaps(self):
        # now == reset_at is "rolled" (>=).
        self.assertEqual(r.reap_disposition(_state(reset_at="2026-09-27T18:00:00Z"), NOW), "reap")


class TestPaneClassifier(unittest.TestCase):
    LIVE_MODAL = (
        "  Ran 1 shell command\n"
        "  ⎿  You've hit your monthly spend limit · your weekly limit resets Sep 27, 5pm\n"
        "     (UTC)\n"
        "   What do you want to do?                        Usage credit balance: $8.08\n"
        "   ❯ Adjust monthly spend limit:        ← or → to adjust · Del to remove lim…\n"
        "     Wait for limit to reset                         Resets Sep 27, 5pm (UTC)\n"
        "   Enter to confirm · Esc to cancel"
    )
    # A pane merely QUOTING the modal (e.g. an agent reviewing the T-4037 PR): the modal
    # text is present but the live input box + status line sit BELOW it, so the footer is
    # NOT the last non-empty line. orchestrator T-4037 round-2 false-positive case.
    QUOTED_MODAL = (
        "  reviewing the modal text: 'You've hit your monthly spend limit ... Wait for\n"
        "  limit to reset ... Enter to confirm · Esc to cancel'\n"
        "────────────────────────────────────────\n"
        "❯ \n"
        "────────────────────────────────────────\n"
        "  ⏸ manual mode on · ? for shortcuts"
    )

    def test_live_modal_at_bottom_true(self):
        self.assertTrue(r.pane_shows_cap_modal_at_bottom(self.LIVE_MODAL))

    def test_quoted_modal_with_input_below_false(self):
        self.assertFalse(r.pane_shows_cap_modal_at_bottom(self.QUOTED_MODAL))

    def test_footer_last_but_no_sentinel_false(self):
        self.assertFalse(r.pane_shows_cap_modal_at_bottom("some dialog\n  Enter to confirm · Esc to cancel"))

    def test_empty_or_none_false(self):
        self.assertFalse(r.pane_shows_cap_modal_at_bottom(""))
        self.assertFalse(r.pane_shows_cap_modal_at_bottom(None))


class TestReadState(unittest.TestCase):
    def test_ok_json_parsed(self):
        raw = lambda p, a: ("ok", '{"modal_present": true, "modal_kind": "weekly-limit"}')
        self.assertEqual(r.read_cap_modal_state("specialist", "claude-research", read_raw=raw).get("modal_present"), True)

    def test_missing_is_empty(self):
        self.assertEqual(r.read_cap_modal_state("specialist", "claude-research", read_raw=lambda p, a: ("missing", "")), {})

    def test_fault_is_empty(self):
        self.assertEqual(r.read_cap_modal_state("specialist", "claude-research", read_raw=lambda p, a: ("fault", "")), {})

    def test_malformed_json_is_empty(self):
        self.assertEqual(r.read_cap_modal_state("specialist", "claude-research", read_raw=lambda p, a: ("ok", "{not json")), {})


class TestObserveDryRun(unittest.TestCase):
    def test_would_reap_and_recovery_self_excluded(self):
        # specialist fully-capped-rolled -> would-reap; recovery same state -> self-excluded (T-0559).
        def read_state(persona, account):
            return _state()
        res = r.observe(("specialist", "recovery"), read_state=read_state, now=NOW, log=lambda *a, **k: None)
        self.assertEqual(res["specialist"], "would-reap(dry-run)")
        self.assertEqual(res["recovery"], "would-reap-but-self-excluded")

    def test_uncapped_seat_noop(self):
        res = r.observe(("specialist",), read_state=lambda p, a: {}, now=NOW, log=lambda *a, **k: None)
        self.assertEqual(res["specialist"], "noop:no-state")


# ---------------------------------------------------------------- increment 2
from unittest import mock  # noqa: E402

_CLEARED_PANE = "────────\n❯ \n────────\n  ⏸ manual mode on · ? for shortcuts"


def _seq(*vals):
    """A callable ignoring its args and returning vals[0], vals[1], ... per call
    (last value sticks). For stubbing successive capture_pane / read_state calls."""
    box = list(vals)
    def f(*_a, **_k):
        return box.pop(0) if len(box) > 1 else box[0]
    return f


class TestReapSeatLive(unittest.TestCase):
    """Increment 2: reap_seat's live re-verify + arm gate. capture/send are stubbed —
    no real tmux, no sudo, no keystrokes. ESC_SETTLE_SECONDS patched to 0 (no sleep)."""

    def setUp(self):
        self._p = mock.patch.object(r, "ESC_SETTLE_SECONDS", 0)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.MODAL = TestPaneClassifier.LIVE_MODAL

    def _reap(self, **over):
        kw = dict(
            now=NOW, arm=False,
            read_state=lambda p, a: _state(),
            capture_pane=_seq(self.MODAL),
            send_escape=lambda a, t: True,
        )
        kw.update(over)
        return r.reap_seat("specialist", "claude-research", "claude-research-bot:0", **kw)

    def test_verified_but_unarmed_sends_nothing(self):
        sent = []
        d = self._reap(arm=False, send_escape=lambda a, t: sent.append(t) or True)
        self.assertEqual(d, "would-reap(verified)")
        self.assertEqual(sent, [])  # NOTHING sent when unarmed

    def test_armed_full_reap_confirms_cleared(self):
        d = self._reap(arm=True, capture_pane=_seq(self.MODAL, _CLEARED_PANE))
        self.assertEqual(d, "reaped")

    def test_pane_unreadable_fails_closed(self):
        d = self._reap(arm=True, capture_pane=_seq(None))
        self.assertEqual(d, "skip:pane-unreadable")

    def test_modal_not_at_bottom_fails_closed(self):
        # live pane no longer shows the modal at the bottom (dismissed / quoted) -> no Esc.
        d = self._reap(arm=True, capture_pane=_seq(_CLEARED_PANE))
        self.assertEqual(d, "skip:modal-not-at-bottom")

    def test_reverify_state_changed_fails_closed(self):
        # state said reap, but the fresh re-read no longer does (detector cleared modal_present).
        d = self._reap(arm=True, read_state=_seq(_state(), {}), capture_pane=_seq(self.MODAL))
        self.assertEqual(d, "skip:reverify-state-changed")

    def test_esc_send_failure_reported(self):
        d = self._reap(arm=True, send_escape=lambda a, t: False, capture_pane=_seq(self.MODAL))
        self.assertEqual(d, "esc-failed")

    def test_reaped_but_not_cleared_surfaced(self):
        # Esc sent ok, but the post-Esc capture still shows the modal.
        d = self._reap(arm=True, capture_pane=_seq(self.MODAL, self.MODAL))
        self.assertEqual(d, "reaped:not-cleared")

    def test_self_excluded_never_captures_or_sends(self):
        cap, snd = [], []
        d = r.reap_seat("recovery", "claude-fallback", "claude-telegram-bot-fallback:0",
                        now=NOW, arm=True, read_state=lambda p, a: _state(),
                        capture_pane=lambda *a: cap.append(1) or self.MODAL,
                        send_escape=lambda a, t: snd.append(1) or True)
        self.assertEqual(d, "skip:self-excluded")
        self.assertEqual((cap, snd), ([], []))  # never touched the pane

    def test_non_reap_disposition_passes_through(self):
        d = self._reap(arm=True, read_state=lambda p, a: {})  # empty -> noop:no-state
        self.assertEqual(d, "noop:no-state")

    def test_no_pane_target_skips(self):
        d = r.reap_seat("specialist", "claude-research", None, now=NOW, arm=True,
                        read_state=lambda p, a: _state(),
                        capture_pane=_seq(self.MODAL), send_escape=lambda a, t: True)
        self.assertEqual(d, "skip:no-pane-target")


class TestSendEscapeGuards(unittest.TestCase):
    def test_refuses_self_account(self):
        # never self-inject (T-0559): _send_escape must refuse the runner's own account
        # WITHOUT shelling out to sudo.
        with mock.patch.object(r.subprocess, "run",
                               side_effect=AssertionError("must not shell for self/empty")):
            self.assertFalse(r._send_escape(r.getpass.getuser(), "some:0"))
            self.assertFalse(r._send_escape("claude-research", ""))  # empty target


class TestRunArmGate(unittest.TestCase):
    def test_run_observe_verifies_without_sending(self):
        sent = []
        res = r.run(("specialist",), arm=False, now=NOW,
                    read_state=lambda p, a: _state(),
                    capture_pane=_seq(TestPaneClassifier.LIVE_MODAL),
                    send_escape=lambda a, t: sent.append(t) or True,
                    log=lambda *a, **k: None)
        self.assertEqual(res["specialist"], "would-reap(verified)")
        self.assertEqual(sent, [])

    def test_run_uncapped_all_noop(self):
        res = r.run(("specialist", "mason"), arm=True, now=NOW,
                    read_state=lambda p, a: {},
                    capture_pane=_seq(None), send_escape=lambda a, t: True,
                    log=lambda *a, **k: None)
        self.assertEqual(set(res.values()), {"noop:no-state"})


if __name__ == "__main__":
    unittest.main()
