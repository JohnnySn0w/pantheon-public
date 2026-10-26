"""test_keep_going.py — anti-idle nudge (tools/keep_going.py, T-0772 sibling).

Covers the idle-gate tri-state, the run() wiring (nudge only when parked at the
idle prompt; never into active work or a modal; fail-safe on uncapturable/inconclusive),
the adaptive backoff (T-1364): a nudge that produces no work doubles the wait;
a nudge that produces work resets to the base cadence, and the per-agent delivery
flag (T-1827 R1: specialist rides mailbox.send(), everyone else stays on send-keys).
Pane/ledger I/O is stubbed — no live tmux, no real ledger, isolated state dir. The
real mailbox.send() unit tests use a tmp-dir inbox only — NEVER the real hub.
"""
import contextlib
import fcntl
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keep_going as kg  # noqa: E402
import mailbox  # noqa: E402


_ORIG_SEAT_HOLD = kg._seat_hold


def setUpModule():
    # ORG-106 L2: hermetic against the host's real seat-state / maintenance / OOM hold
    # (and the cross-account read for other-account seats). Seat-hold tests pass
    # do_seat_hold or patch the seat_state readers themselves.
    global _SEAT_HOLD_PATCH
    _SEAT_HOLD_PATCH = mock.patch.object(kg, "_seat_hold", lambda agent: None)
    _SEAT_HOLD_PATCH.start()
    # T-4510: hermetic against a host whose real fleet-usage dir holds a >=95% reading:
    # a nonexistent dir makes the usage gate fail open, so every pre-T-4510 test is
    # unchanged. The T-4510 class points the dir at its own temp readings.
    global _FLEET_ENV_PATCH
    _FLEET_ENV_PATCH = mock.patch.dict(
        os.environ, {"PANTHEON_FLEET_USAGE_DIR": "/nonexistent/pantheon-fleet-usage-hermetic"})
    _FLEET_ENV_PATCH.start()


def tearDownModule():
    _SEAT_HOLD_PATCH.stop()
    _FLEET_ENV_PATCH.stop()

IDLE_PANE = "\n".join([
    "● some earlier output", "─" * 80, "❯ ", "─" * 80, "  ? for shortcuts",
])
BUSY_PANE = "\n".join([
    "│ working on the thing…", "✻ Thinking…", "  ⎿  running tool",
    "  esc to interrupt",
])
MODAL_PANE = "\n".join([
    "Do you want to proceed?", "❯ 1. Yes", "  2. No",
    "  Enter to confirm · Esc to cancel",
])


class IdleGateTest(unittest.TestCase):
    def test_idle_prompt_is_nudgeable(self):
        self.assertIs(kg.is_idle_at_prompt(IDLE_PANE), True)

    def test_busy_is_not_nudgeable(self):
        self.assertIs(kg.is_idle_at_prompt(BUSY_PANE), False)

    def test_modal_is_not_nudgeable(self):
        # T-5435: the canonical classifier reads a modal as UNKNOWN (None), not False.
        # Either way it is not True, and run() skips both.
        self.assertIsNot(kg.is_idle_at_prompt(MODAL_PANE), True)

    def test_empty_capture_inconclusive(self):
        self.assertIsNone(kg.is_idle_at_prompt(""))
        self.assertIsNone(kg.is_idle_at_prompt("\n  \n   \n"))

    def test_unknown_text_inconclusive(self):
        self.assertIsNone(kg.is_idle_at_prompt("just some\nrandom lines\nno sentinels"))

    def test_idle_with_hidden_task_list_footer(self):
        # Regression (2026-06-28): orchestrator keeps a hidden task list, so its idle footer
        # carries "ctrl+t to show tasks". T-5435: now judged by the canonical classifier,
        # which needs the mode indicator (every seat has one) plus known hints.
        R = "─" * 80
        pane = "\n".join([
            "● earlier output", R, "❯ ", R,
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ctrl+t to show tasks",
        ])
        self.assertIs(kg.is_idle_at_prompt(pane), True)

    def test_busy_wins_over_hidden_task_footer(self):
        # The "show tasks" footer coexists with "esc to interrupt" while busy —
        # busy must still win (never nudge active generation).
        pane = "✻ Building…\n  esc to interrupt · ctrl+t to show tasks · ← for agents"
        self.assertIs(kg.is_idle_at_prompt(pane), False)

    def test_busy_wins_over_idle_hint_in_region(self):
        # If both an idle hint AND "esc to interrupt" are in the bottom region,
        # busy must win (never interrupt active generation).
        pane = "? for shortcuts\nworking…\nesc to interrupt"
        self.assertIs(kg.is_idle_at_prompt(pane), False)


RULE = "─" * 80
BYPASS_FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def _bypass_pane(footer=BYPASS_FOOTER, above=()):
    return "\n".join(["● earlier output", *above, RULE, "❯ ", RULE, footer])


class BypassModeIdleTest(unittest.TestCase):
    """T-5435: bypass-permissions mode shows no '? for shortcuts' / 'ctrl+t' hint, so the
    old private hint list read every idle seat as inconclusive and keep_going never nudged.
    Classification is delegated to inbox_consumer.pane_idle_state (nudge caller)."""

    def test_idle_bypass_footer_is_nudgeable(self):
        self.assertIs(kg.is_idle_at_prompt(_bypass_pane()), True)

    def test_bypass_footer_while_generating_is_not_nudgeable(self):
        pane = _bypass_pane(
            footer=BYPASS_FOOTER + " · esc to interrupt", above=("✻ Thinking…",))
        self.assertIs(kg.is_idle_at_prompt(pane), False)

    def test_modal_footer_is_not_nudgeable(self):
        pane = _bypass_pane(footer="  Enter to confirm · Esc to cancel")
        self.assertIsNot(kg.is_idle_at_prompt(pane), True)

    def test_turn_ended_with_running_background_agent_is_not_nudgeable(self):
        # state B: main turn ended, a background agent still runs -> nudge must wait.
        fx = Path(__file__).resolve().parent / "testdata" / "orchestrator_turn_ended_bg_agents_pane.txt"
        self.assertIsNot(kg.is_idle_at_prompt(fx.read_text()), True)

    def test_dim_prompt_suggestion_ansi_capture_is_still_idle(self):
        # capture-pane -e: a DIM suggestion in the composer is ghost text, not input.
        pane = "\n".join(["● out", RULE, "❯ \x1b[2mrun the tests\x1b[0m", RULE, BYPASS_FOOTER])
        self.assertIs(kg.is_idle_at_prompt(pane), True)

    def test_is_generating_ignores_escape_only_lines(self):
        pane = "\n".join(["✻ Working…", "  esc to interrupt", "\x1b[0m", "\x1b[0m", "\x1b[0m",
                          "\x1b[0m", "\x1b[0m", "\x1b[0m"])
        self.assertTrue(kg.is_generating(pane))

    def test_capture_pane_requests_ansi_attributes(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="x")

        with mock.patch.object(kg.subprocess, "run", fake_run):
            kg.capture_pane("%1", kg.getpass.getuser())
        self.assertIn("capture-pane -t %1 -p -e", calls[0][-1])


class RunTest(unittest.TestCase):
    """Gate + wiring. Each test starts from a FRESH (isolated) backoff state, so the
    first idle nudge always fires (threshold 2^0 = 1 cycle) — these assertions are
    unchanged by the adaptive backoff. Ledger/state I/O is stubbed/isolated."""

    def setUp(self):
        self.injected = []
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        # No work has happened (stub) — irrelevant on a first nudge, fixed for the rest.
        # Session probe stubbed to None (unknown) — no reset path, no real find/sudo.
        self.kw = dict(do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None,
                       do_cap_check=lambda *a, **k: False)

    def _cap(self, pane):
        return lambda pt, uu: pane

    def _inject_ok(self, pt, prompt):
        self.injected.append((pt, prompt)); return True

    def _inject_fail(self, pt, prompt):
        self.injected.append((pt, prompt)); return False

    def test_nudges_when_parked(self):
        rc = kg.run("recovery", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][0], "claude-telegram-bot-fallback:0")
        self.assertIn("keep going", self.injected[0][1].lower())

    def test_skips_when_busy(self):
        rc = kg.run("recovery", do_capture=self._cap(BUSY_PANE), do_inject=self._inject_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "must not nudge an actively-working agent")

    def test_skips_when_modal(self):
        rc = kg.run("recovery", do_capture=self._cap(MODAL_PANE), do_inject=self._inject_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])

    def test_skips_when_uncapturable(self):
        rc = kg.run("recovery", do_capture=lambda pt, uu: None, do_inject=self._inject_ok, **self.kw)
        self.assertEqual(rc, 0, "uncapturable pane is a harmless skip, not an error")
        self.assertEqual(self.injected, [])

    def test_skips_when_inconclusive(self):
        rc = kg.run("recovery", do_capture=self._cap("noise\nnoise"), do_inject=self._inject_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])

    def test_failed_inject_returns_nonzero(self):
        # An idle pane on every T-4432 re-check is still a failure (sleep stubbed).
        rc = kg.run("recovery", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_fail,
                    do_sleep=lambda s: None, **self.kw)
        self.assertEqual(rc, 8)

    def test_unknown_agent_rejected(self):
        rc = kg.run("nobody", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok, **self.kw)
        self.assertEqual(rc, 64)

    def test_custom_prompt_used(self):
        kg.run("recovery", prompt="CUSTOM NUDGE", do_capture=self._cap(IDLE_PANE),
               do_inject=self._inject_ok, **self.kw)
        self.assertEqual(self.injected[0][1], "CUSTOM NUDGE")

    def test_default_prompt_carries_blocked_protocol(self):
        # the operator's directive: blocked -> update the ticket, send to the right person, move on.
        p = kg.DEFAULT_PROMPT.lower()
        self.assertIn("keep going", p)
        self.assertIn("blocked", p)
        self.assertIn("ticket", p)
        self.assertIn("move on", p)

    def test_parity_all_agents_nudge_with_correct_pane(self):
        # Parity: every standing agent gets identical idle-gated treatment, each
        # routed to its own canonical pane (matches inbox_consumer's config). This
        # isolates AGENT_PANE routing from DELIVERY_MODE (T-1827) — specialist's default
        # is "mailbox" (see DeliveryModeTest), so force send-keys for all three here
        # to test the pane map on its own axis; never touches the real mailbox hub.
        expected = {
            "orchestrator": "claude-telegram-bot:0",
            "specialist": "claude-research-bot:0",
            "recovery": "claude-telegram-bot-fallback:0",
        }
        original_modes = dict(kg.DELIVERY_MODE)
        kg.DELIVERY_MODE.clear()
        kg.DELIVERY_MODE.update({a: "send-keys" for a in expected})
        self.addCleanup(lambda: (kg.DELIVERY_MODE.clear(), kg.DELIVERY_MODE.update(original_modes)))
        for agent, pane in expected.items():
            sent = []
            rc = kg.run(agent, do_capture=self._cap(IDLE_PANE),
                        do_inject=lambda pt, pr: sent.append((pt, pr)) or True, **self.kw)
            self.assertEqual(rc, 0)
            self.assertEqual(len(sent), 1, f"{agent} must nudge when parked")
            self.assertEqual(sent[0][0], pane, f"{agent} routed to wrong pane")


class SeatHoldTest(unittest.TestCase):
    """ORG-106 L2 (T-5525): a seat with intake blocked is not nudged, and costs no pane
    capture. Q1: maintenance and the OOM hold are honoured too (keep_going had no hold)."""

    def _state(self, **kw):
        from tools import seat_state as ss
        return ss.Contribution(**kw)

    def _run(self, **readers):
        """run() with the REAL _seat_hold over patched seat_state source readers."""
        from tools import seat_state as ss
        captured = []
        injected = []
        none = ss.Contribution()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(kg, "_seat_hold", _ORIG_SEAT_HOLD), \
                mock.patch("tools.seat_state._read_seat_state", return_value=readers.get("record", none)), \
                mock.patch("tools.seat_state._read_seat_state_cross", return_value=readers.get("record", none)), \
                mock.patch("tools.seat_state._read_maintenance", return_value=readers.get("maint", none)), \
                mock.patch("tools.seat_state._read_oom", return_value=readers.get("oom", none)), \
                mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": tmp.name}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = kg.run("recovery", do_capture=lambda pt, uu: captured.append(1) or IDLE_PANE,
                            do_inject=lambda pt, p: injected.append(p) or True,
                            do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None,
                            do_cap_check=lambda *a, **k: False)
        return rc, captured, injected, buf.getvalue()

    def test_active_seat_nudged(self):
        rc, cap, inj, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(inj), 1)

    def test_paused_record_no_capture_no_nudge(self):
        c = self._state(source="seat-state", state="paused-intake", intake_blocked=True)
        rc, cap, inj, out = self._run(record=c)
        self.assertEqual((rc, cap, inj), (0, [], []))
        self.assertIn("seat-state", out)

    def test_maintenance_now_honoured(self):
        c = self._state(source="maintenance", intake_blocked=True, remediation_vetoed=True)
        rc, cap, inj, out = self._run(maint=c)
        self.assertEqual((rc, cap, inj), (0, [], []))
        self.assertIn("maintenance", out)

    def test_oom_hold_now_honoured(self):
        c = self._state(source="oom-hold", intake_blocked=True, launch_blocked=True)
        rc, cap, inj, out = self._run(oom=c)
        self.assertEqual((rc, cap, inj), (0, [], []))
        self.assertIn("oom-hold", out)

    def test_reader_fault_fails_open(self):
        rc, cap, inj, _ = self._run(record=self._state(fault="seat-state: boom"))
        self.assertEqual(len(inj), 1)
        with mock.patch("tools.seat_state.effective_for", side_effect=RuntimeError("x")):
            self.assertIsNone(_ORIG_SEAT_HOLD("recovery"))


class DecideNudgeTest(unittest.TestCase):
    """Pure backoff decision (T-1364). NOW is a fixed epoch — no wall clock."""
    NOW = 1_800_000_000

    def test_first_nudge_always_fires(self):
        should, st, _ = kg.decide_nudge({}, None, self.NOW)
        self.assertTrue(should)
        self.assertEqual(st["consecutive_ineffective"], 0)
        self.assertEqual(st["cycles_since_nudge"], 0)
        self.assertEqual(st["last_nudge_ts"], self.NOW)

    def test_ineffective_nudge_doubles_interval(self):
        # Prior nudge fired with no work; work signal hasn't advanced → ineffective.
        st = {"last_nudge_ts": self.NOW - 900, "work_ts_at_last_nudge": None,
              "consecutive_ineffective": 0, "cycles_since_nudge": 0}
        # 1 cycle later: threshold for consec=0 is 2^0=1 → due, judged ineffective → consec=1.
        should, st2, _ = kg.decide_nudge(st, None, self.NOW)
        self.assertTrue(should)
        self.assertEqual(st2["consecutive_ineffective"], 1)

    def test_backed_off_until_threshold(self):
        # consec=1 → threshold 2^1=2 cycles. One idle cycle is NOT enough.
        st = {"last_nudge_ts": self.NOW, "work_ts_at_last_nudge": None,
              "consecutive_ineffective": 1, "cycles_since_nudge": 0}
        should, st2, _ = kg.decide_nudge(st, None, self.NOW)
        self.assertFalse(should, "1/2 cycles → backed off")
        self.assertEqual(st2["cycles_since_nudge"], 1)
        self.assertEqual(st2["consecutive_ineffective"], 1, "backoff state untouched on skip")
        # Second idle cycle reaches the threshold → due.
        should2, _, _ = kg.decide_nudge(st2, None, self.NOW)
        self.assertTrue(should2, "2/2 cycles → due")

    def test_work_advance_resets_to_base(self):
        # Prior nudge fired at work_ts=100; the agent then produced work (ts now 500).
        st = {"last_nudge_ts": self.NOW - 7200, "work_ts_at_last_nudge": 100,
              "consecutive_ineffective": 4, "cycles_since_nudge": 16}  # deeply backed off
        should, st2, _ = kg.decide_nudge(st, 500, self.NOW)
        self.assertTrue(should, "16 cycles >= 2^4=16 → due")
        self.assertEqual(st2["consecutive_ineffective"], 0, "work advanced → reset to base cadence")

    def test_backoff_caps(self):
        st = {"last_nudge_ts": self.NOW, "work_ts_at_last_nudge": None,
              "consecutive_ineffective": 99, "cycles_since_nudge": 0}
        # threshold capped at 2^cap; never fires before that many cycles, never overflows.
        cap = kg.BACKOFF_CAP_EXP
        should, _, _ = kg.decide_nudge(st, None, self.NOW, cap=cap)
        self.assertFalse(should)
        st["cycles_since_nudge"] = 2 ** cap - 1
        should2, _, _ = kg.decide_nudge(st, None, self.NOW, cap=cap)
        self.assertTrue(should2, "reaching 2^cap cycles fires even for a huge consec")


class SessionResetTest(unittest.TestCase):
    """Pure new-session reset (T-1500 B)."""

    def test_unknown_current_session_no_reset(self):
        st = {"consecutive_ineffective": 3, "session_id": "S1"}
        new, reset = kg.maybe_reset_for_new_session(st, None)
        self.assertFalse(reset)
        self.assertEqual(new, st)

    def test_first_record_no_reset(self):
        # Pre-feature state (no session_id) → record, don't reset.
        st = {"consecutive_ineffective": 3, "cycles_since_nudge": 2}
        new, reset = kg.maybe_reset_for_new_session(st, "S1")
        self.assertFalse(reset)
        self.assertEqual(new["session_id"], "S1")
        self.assertEqual(new["consecutive_ineffective"], 3, "backoff kept")

    def test_same_session_no_reset(self):
        st = {"consecutive_ineffective": 3, "session_id": "S1"}
        new, reset = kg.maybe_reset_for_new_session(st, "S1")
        self.assertFalse(reset)
        self.assertEqual(new["consecutive_ineffective"], 3)

    def test_changed_session_resets(self):
        st = {"consecutive_ineffective": 5, "cycles_since_nudge": 7,
              "last_nudge_ts": 123, "session_id": "S1"}
        new, reset = kg.maybe_reset_for_new_session(st, "S2")
        self.assertTrue(reset)
        self.assertEqual(new, {"session_id": "S2"}, "fresh state — old escalation voided")


class AdaptiveRunTest(unittest.TestCase):
    """End-to-end run() under adaptive backoff across simulated timer fires."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self.sent = []

    def _fire(self, work_ts, now, session_id="S1"):
        # specialist defaults to mailbox delivery (T-1827) — this class tests the
        # backoff DECISION logic, agent-agnostic, so stub do_mailbox_send the same
        # way do_inject is stubbed (both just record into self.sent) rather than
        # letting it fall through to the real mailbox.send() against the live hub.
        return kg.run("specialist",
                      do_capture=lambda pt, uu: IDLE_PANE,
                      do_inject=lambda pt, pr: self.sent.append(now) or True,
                      do_mailbox_send=lambda agent, pr, **kw: self.sent.append(now) or True,
                      do_work_ts=lambda cfg: work_ts,
                      do_cap_check=lambda *a, **k: False,
                      do_session_id=lambda cfg: session_id, now_ts=now)

    def test_idle_agent_backs_off_exponentially(self):
        # A perpetually-idle agent (work_ts never advances) should nudge on idle
        # cycles 1, 2, 4, 8, … — far fewer than every cycle.
        t = 1_800_000_000
        fired_on = []
        for cycle in range(1, 17):  # 16 timer fires
            n = t + cycle * 900
            rc = self._fire(work_ts=None, now=n)
            self.assertEqual(rc, 0)
            if self.sent and self.sent[-1] == n:
                fired_on.append(cycle)
        # Nudges land on cycles 1,2,4,8,16 (intervals 1,2,4,8) — NOT all 16 cycles.
        self.assertEqual(fired_on, [1, 2, 4, 8, 16])
        self.assertLess(len(fired_on), 16, "must be far fewer nudges than timer fires")

    def test_resumes_base_cadence_after_real_work(self):
        t = 1_800_000_000
        # Two ineffective nudges → backed off (consec=2, threshold 4).
        self._fire(work_ts=None, now=t + 900)    # cycle1: nudge (first)
        self._fire(work_ts=None, now=t + 1800)   # cycle2: nudge (ineffective→consec1)
        n_sent = len(self.sent)
        # Agent does real work (ledger advances). Next DUE nudge resets to base.
        # consec after cycle2 = 1 → threshold 2; need 2 idle cycles to be due again.
        self._fire(work_ts=t + 2000, now=t + 2700)  # cycle1 of backoff: skipped
        self.assertEqual(len(self.sent), n_sent, "still within backoff window")
        rc = self._fire(work_ts=t + 2000, now=t + 3600)  # cycle2: due, work advanced→reset
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), n_sent + 1, "nudged once backoff window elapsed")
        st = kg.read_state(kg._state_path("specialist"))
        self.assertEqual(st["consecutive_ineffective"], 0, "real work reset the backoff")

    def test_new_session_resets_deep_backoff(self):
        # T-1500 (B) — the 2026-07-02 gap: escalate a deep backoff in session S1,
        # then restart (S2). The FIRST idle cycle of the new session must nudge,
        # not sit out the inherited 2^consec window.
        t = 1_800_000_000
        for cycle in range(1, 9):  # S1: nudges on 1,2,4,8 → consec climbs
            self._fire(work_ts=None, now=t + cycle * 900, session_id="S1")
        st = kg.read_state(kg._state_path("specialist"))
        self.assertGreaterEqual(st["consecutive_ineffective"], 2, "deeply backed off in S1")
        n_sent = len(self.sent)
        rc = self._fire(work_ts=None, now=t + 9 * 900, session_id="S2")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), n_sent + 1, "new session → immediate nudge")
        st2 = kg.read_state(kg._state_path("specialist"))
        self.assertEqual(st2["session_id"], "S2")
        self.assertEqual(st2["consecutive_ineffective"], 0, "backoff reset with the session")

    def test_unknown_session_keeps_backoff(self):
        # Probe failure (None) must NOT reset — fail-safe keeps the existing window.
        t = 1_800_000_000
        self._fire(work_ts=None, now=t + 900, session_id="S1")   # nudge 1
        self._fire(work_ts=None, now=t + 1800, session_id="S1")  # nudge 2 → consec=1
        n_sent = len(self.sent)
        rc = self._fire(work_ts=None, now=t + 2700, session_id=None)  # 1/2 cycles
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), n_sent, "unknown session id → backoff untouched")

    def test_legacy_mode_nudges_every_cycle(self):
        t = 1_800_000_000
        for cycle in range(1, 6):
            kg.run("specialist", do_capture=lambda pt, uu: IDLE_PANE,
                   do_inject=lambda pt, pr: self.sent.append(cycle) or True,
                   do_mailbox_send=lambda agent, pr, **kw: self.sent.append(cycle) or True,
                   do_work_ts=lambda cfg: None, do_cap_check=lambda *a, **k: False,
                   now_ts=t + cycle * 900, adaptive=False)
        self.assertEqual(len(self.sent), 5, "legacy mode nudges on every idle cycle")


class DeliveryModeTest(unittest.TestCase):
    """Per-agent delivery flag. A "mailbox" agent rides mailbox.send(); every other
    agent (and any agent absent from the map) stays on the byte-identical send-keys
    path. Both do_inject and do_mailbox_send are stubbed — no live tmux, no real
    mailbox I/O here (see MailboxDeliveryUnitTest for the real mailbox.send() path).
    T-3832 (2026-09-20): specialist consolidated to send-keys, so keep_going no longer
    has a real mailbox agent; setUp forces specialist into mailbox mode as a fixture so
    these tests keep exercising the mailbox-delivery mechanism."""

    def setUp(self):
        self._orig_dm = dict(kg.DELIVERY_MODE)
        kg.DELIVERY_MODE["specialist"] = "mailbox"
        self.addCleanup(lambda: (kg.DELIVERY_MODE.clear(), kg.DELIVERY_MODE.update(self._orig_dm)))
        self.injected = []
        self.mailbox_sent = []
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self.kw = dict(do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None,
                       do_cap_check=lambda *a, **k: False)

    def _cap(self, pane):
        return lambda pt, uu: pane

    def _inject_ok(self, pt, prompt):
        self.injected.append((pt, prompt)); return True

    def _mailbox_send_ok(self, agent, prompt, *, inbox=None):
        self.mailbox_sent.append((agent, prompt, inbox)); return True

    def test_recovery_default_mode_uses_send_keys_not_mailbox(self):
        rc = kg.run("recovery", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=self._mailbox_send_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "default (send-keys) must still nudge via pane inject")
        self.assertEqual(self.mailbox_sent, [], "default mode must never call mailbox.send")

    def test_orchestrator_default_mode_uses_send_keys_not_mailbox(self):
        rc = kg.run("orchestrator", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=self._mailbox_send_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.mailbox_sent, [])

    def test_specialist_mode_uses_mailbox_not_send_keys(self):
        rc = kg.run("specialist", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=self._mailbox_send_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "specialist mode must never pane-inject")
        self.assertEqual(len(self.mailbox_sent), 1)
        agent, prompt, inbox = self.mailbox_sent[0]
        self.assertEqual(agent, "specialist")
        self.assertEqual(prompt, kg.DEFAULT_PROMPT, "payload must be the nudge text verbatim")

    def test_specialist_mailbox_inbox_override_passed_through(self):
        tmp_inbox = Path(self._tmp.name) / "specialist-inbox"
        rc = kg.run("specialist", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=self._mailbox_send_ok, mailbox_inbox=tmp_inbox, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.mailbox_sent[0][2], tmp_inbox)

    def test_unknown_delivery_mode_entry_falls_back_to_send_keys(self):
        # Simulate an agent absent from DELIVERY_MODE (fail-open contract) without
        # touching AGENT_PANE — pop+restore the map entry around the call.
        original = dict(kg.DELIVERY_MODE)
        kg.DELIVERY_MODE.pop("recovery", None)
        self.addCleanup(lambda: (kg.DELIVERY_MODE.clear(), kg.DELIVERY_MODE.update(original)))
        rc = kg.run("recovery", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=self._mailbox_send_ok, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "missing map entry must fail open to send-keys")
        self.assertEqual(self.mailbox_sent, [])

    def test_specialist_legacy_mode_also_routes_to_mailbox(self):
        # Legacy (KEEP_GOING_ADAPTIVE=0 shape) path must respect the delivery flag too.
        rc = kg.run("specialist", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=self._mailbox_send_ok, adaptive=False, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])
        self.assertEqual(len(self.mailbox_sent), 1)

    def test_specialist_failed_mailbox_send_returns_nonzero(self):
        rc = kg.run("specialist", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_ok,
                    do_mailbox_send=lambda agent, prompt, **kw: False, **self.kw)
        self.assertEqual(rc, 8)


class RecheckAfterFailedNudgeTest(unittest.TestCase):
    """T-4432: a send-keys nudge that send_keys_submit reports unconfirmed, on a pane that
    is GENERATING by a re-check, landed (the idle->busy race, T-3554). It must not page
    via exit 8. Idle, unknown or MODAL on every re-check is still a failure, and mailbox
    delivery has no pane race, so it keeps exit 8 without re-reading."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self.captures, self.sleeps, self.injected = [], [], []
        self.kw = dict(do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None,
                       do_cap_check=lambda *a, **k: False, do_sleep=self.sleeps.append)

    def _seq(self, *panes):
        """The first capture is the decide-time read; the rest are the re-checks. The
        last pane repeats if the code reads more often than the test scripted."""
        it = iter(panes)
        def cap(pt, uu):
            p = next(it, panes[-1])
            self.captures.append(p)
            return p
        return cap

    def _inject_fail(self, pt, prompt):
        self.injected.append(prompt)
        return False

    def test_generating_on_a_later_recheck_means_landed(self):
        rc = kg.run("recovery", do_capture=self._seq(IDLE_PANE, IDLE_PANE, BUSY_PANE),
                    do_inject=self._inject_fail, **self.kw)
        self.assertEqual(rc, 0, "a nudge that started a turn is not a unit failure")
        self.assertEqual(len(self.injected), 1, "never re-inject on the soft path")
        self.assertEqual(len(self.captures), 3, "stop re-reading at the first generating sample")
        self.assertEqual(self.sleeps, [kg.RECHECK_INTERVAL_SECS])
        self.assertFalse(kg._state_path("recovery").exists(),
                         "the soft path must not persist backoff state (failed-send rule)")

    def test_legacy_mode_gets_the_same_recheck(self):
        rc = kg.run("recovery", do_capture=self._seq(IDLE_PANE, BUSY_PANE),
                    do_inject=self._inject_fail, adaptive=False, **self.kw)
        self.assertEqual(rc, 0)

    def test_idle_on_every_recheck_is_still_a_failure(self):
        rc = kg.run("recovery", do_capture=self._seq(IDLE_PANE), do_inject=self._inject_fail, **self.kw)
        self.assertEqual(rc, 8)
        self.assertEqual(len(self.captures), 1 + kg.RECHECK_SAMPLES)
        self.assertEqual(len(self.sleeps), kg.RECHECK_SAMPLES - 1)

    def test_modal_after_a_failed_nudge_is_still_a_failure(self):
        # is_idle_at_prompt() reads a modal as False (not idle). The re-check must not
        # mistake that for "busy": a modal right after a nudge is a real wedge.
        rc = kg.run("recovery", do_capture=self._seq(IDLE_PANE, MODAL_PANE),
                    do_inject=self._inject_fail, **self.kw)
        self.assertEqual(rc, 8)

    def test_uncapturable_or_unknown_rechecks_are_still_a_failure(self):
        for later in (None, "noise\nnoise"):
            self.captures.clear()
            rc = kg.run("recovery", do_capture=self._seq(IDLE_PANE, later),
                        do_inject=self._inject_fail, **self.kw)
            self.assertEqual(rc, 8, repr(later))

    def test_mailbox_delivery_failure_does_not_reread_the_pane(self):
        # No seat is in mailbox mode today (specialist moved to send-keys, T-3832), so pin the
        # mode here. do_inject is stubbed as well: an unstubbed send-keys path would type
        # into the real pane of whichever seat runs this suite.
        with mock.patch.dict(kg.DELIVERY_MODE, {"specialist": "mailbox"}):
            rc = kg.run("specialist", do_capture=self._seq(IDLE_PANE, BUSY_PANE),
                        do_inject=self._inject_fail,
                        do_mailbox_send=lambda agent, prompt, **kw: False, **self.kw)
        self.assertEqual(rc, 8)
        self.assertEqual(self.injected, [], "mailbox mode never uses send-keys")
        self.assertEqual(len(self.captures), 1, "mailbox mode has no pane race to re-check")

    def test_is_generating_is_narrower_than_not_idle(self):
        self.assertTrue(kg.is_generating(BUSY_PANE))
        self.assertFalse(kg.is_generating(MODAL_PANE))
        self.assertFalse(kg.is_generating(IDLE_PANE))
        self.assertFalse(kg.is_generating(""))
        # Only the live bottom region counts: an old "esc to interrupt" scrolled far
        # above an idle prompt is not generation.
        self.assertFalse(kg.is_generating("esc to interrupt\n" + "\n".join(["x"] * 10) + "\n" + IDLE_PANE))


class CapSkipTest(unittest.TestCase):
    """T-2024: keep-going must SKIP nudging a usage/capability-capped seat — a
    "keep going" prompt into a hit cap only burns budget and can push deeper in.
    run() consults do_cap_check (defaults to is_usage_capped) after confirming the
    seat is parked-at-idle, before the backoff/deliver decision."""

    def setUp(self):
        self.injected = []
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self.kw = dict(do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None)

    def _inject_ok(self, pt, prompt):
        self.injected.append((pt, prompt)); return True

    def test_run_skips_when_capped(self):
        rc = kg.run("recovery", do_capture=lambda pt, uu: IDLE_PANE,
                    do_inject=self._inject_ok,
                    do_cap_check=lambda *a, **k: True, **self.kw)
        self.assertEqual(rc, 0, "a capped skip is harmless, not an error")
        self.assertEqual(self.injected, [], "must not nudge a capped seat")

    def test_run_nudges_when_not_capped(self):
        rc = kg.run("recovery", do_capture=lambda pt, uu: IDLE_PANE,
                    do_inject=self._inject_ok,
                    do_cap_check=lambda *a, **k: False, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "an uncapped parked seat still nudges")

    def test_cap_skip_does_not_consume_backoff(self):
        # A capped skip must not burn the adaptive-backoff budget: the next
        # uncapped idle cycle must still fire the first nudge immediately.
        kg.run("recovery", do_capture=lambda pt, uu: IDLE_PANE, do_inject=self._inject_ok,
               do_cap_check=lambda *a, **k: True, **self.kw)
        self.assertEqual(self.injected, [])
        kg.run("recovery", do_capture=lambda pt, uu: IDLE_PANE, do_inject=self._inject_ok,
               do_cap_check=lambda *a, **k: False, **self.kw)
        self.assertEqual(len(self.injected), 1,
                         "capped skip must not have consumed the first-nudge budget")

    def test_cap_check_not_reached_when_busy(self):
        # The cap check is downstream of the idle gate — a busy seat is skipped
        # before we ever spend a jsonl read. A cap-check that would explode proves
        # it is never called on a busy pane.
        def boom(*a, **k):
            raise AssertionError("cap check must not run on a busy seat")
        rc = kg.run("recovery", do_capture=lambda pt, uu: BUSY_PANE,
                    do_inject=self._inject_ok, do_cap_check=boom, **self.kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])


class IsUsageCappedTest(unittest.TestCase):
    """T-2024: is_usage_capped() correctly reuses the T-1980 detectors and fails
    OPEN (returns False → nudge as before) on any read/resolution error."""

    def _cfg(self):
        # unix_user/projects_dir are unused here because both I/O seams are stubbed.
        return {"unix_user": "nobody", "projects_dir": "/nonexistent/projects"}

    def _iso(self, epoch):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")

    def test_capability_cap_detected(self):
        now = 1_800_000_000
        recs = [{"type": "assistant", "isApiErrorMessage": True,
                 "message": {"content": "You've hit your usage limit · resets 5pm"},
                 "timestamp": self._iso(now - 10)} for _ in range(2)]
        self.assertTrue(kg.is_usage_capped(
            self._cfg(), "recovery", now,
            do_find_jsonl=lambda *a: Path("/x.jsonl"), do_read_records=lambda *a: recs))

    def test_consent_fallback_detected(self):
        recs = [{"type": "system", "subtype": "model_consent_fallback",
                 "originalModel": "claude-fable-5", "fallbackModel": "claude-opus-4-8"}]
        self.assertTrue(kg.is_usage_capped(
            self._cfg(), "recovery", 1_800_000_000,
            do_find_jsonl=lambda *a: Path("/x.jsonl"), do_read_records=lambda *a: recs))

    def test_not_capped_when_clean(self):
        recs = [{"type": "assistant", "message": {"content": "normal turn"},
                 "timestamp": self._iso(1_800_000_000)}]
        self.assertFalse(kg.is_usage_capped(
            self._cfg(), "recovery", 1_800_000_000,
            do_find_jsonl=lambda *a: Path("/x.jsonl"), do_read_records=lambda *a: recs))

    def test_fail_open_when_no_jsonl(self):
        self.assertFalse(kg.is_usage_capped(
            self._cfg(), "recovery", 1_800_000_000,
            do_find_jsonl=lambda *a: None, do_read_records=lambda *a: [{"x": 1}]))

    def test_fail_open_when_no_records(self):
        self.assertFalse(kg.is_usage_capped(
            self._cfg(), "recovery", 1_800_000_000,
            do_find_jsonl=lambda *a: Path("/x.jsonl"), do_read_records=lambda *a: []))

    def test_fail_open_on_read_error(self):
        def boom(*a):
            raise RuntimeError("sudo denied")
        self.assertFalse(kg.is_usage_capped(
            self._cfg(), "recovery", 1_800_000_000,
            do_find_jsonl=lambda *a: Path("/x.jsonl"), do_read_records=boom))


class MailboxDeliveryUnitTest(unittest.TestCase):
    """send_mailbox()/mailbox_inbox_for() in isolation, against the REAL
    tools/mailbox.py send()/consume() — always a tmp-dir inbox, NEVER the real hub
    at /home/the-ancestor/mailbox."""

    def test_mailbox_inbox_for_matches_hub_convention(self):
        self.assertEqual(kg.mailbox_inbox_for("specialist"), kg.MAILBOX_HUB / "specialist")

    def test_send_mailbox_delivers_verbatim_payload(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d) / "specialist"
            mailbox.provision(inbox)  # T-2216: send() no longer auto-provisions
            ok = kg.send_mailbox("specialist", kg.DEFAULT_PROMPT, inbox=inbox)
            self.assertTrue(ok)
            got = []
            res = mailbox.consume(inbox, lambda m: got.append(m))
            self.assertFalse(res["skipped"])
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["payload"], kg.DEFAULT_PROMPT, "payload must be verbatim")
            # T-1935 (Specialist forensics): the producer stamps ITSELF, not the
            # account persona — nudge provenance must be legible in the id.
            self.assertEqual(got[0]["sender"], "keep_going")
            self.assertIn("keep-going", (got[0]["subject"] or "").lower())


class QueueAwareFloorTest(unittest.TestCase):
    """T-1926: the floor clamps the WAIT while a queue exists — and nothing else."""

    NOW = 1_700_000_000

    def test_floor_clamps_wait_that_would_otherwise_reach_the_ceiling(self):
        # consec=5 → normal threshold 2**5 = 32 cycles (~8 h at the 15-min cadence).
        st = {"last_nudge_ts": self.NOW - 900, "work_ts_at_last_nudge": 100,
              "consecutive_ineffective": 5, "cycles_since_nudge": 1}
        # Without the floor: 2 cycles elapsed of 32 → not due.
        should, _, _ = kg.decide_nudge(st, None, self.NOW)
        self.assertFalse(should, "precondition: deeply backed off agent is not due")
        # With floor_exp=1: threshold 2**1 = 2 → due now (30 min, not 8 h).
        should, _, reason = kg.decide_nudge(st, None, self.NOW, floor_exp=1)
        self.assertTrue(should, f"floor should make the nudge due; reason={reason!r}")

    def test_floor_does_not_reset_the_ineffectiveness_counter(self):
        """The floor shortens the wait; it must not pretend the nudges are working."""
        st = {"last_nudge_ts": self.NOW - 900, "work_ts_at_last_nudge": 100,
              "consecutive_ineffective": 5, "cycles_since_nudge": 1}
        _, new, _ = kg.decide_nudge(st, None, self.NOW, floor_exp=1)
        self.assertEqual(new["consecutive_ineffective"], 6,
                         "counter must keep climbing so an emptied queue resumes the "
                         "real backoff rather than getting a free reset")

    def test_floor_never_lengthens_a_short_wait(self):
        """floor_exp caps the exponent; it must not raise a lower one."""
        st = {"last_nudge_ts": self.NOW - 900, "work_ts_at_last_nudge": None,
              "consecutive_ineffective": 0, "cycles_since_nudge": 0}
        a_should, _, _ = kg.decide_nudge(st, None, self.NOW)
        b_should, _, _ = kg.decide_nudge(st, None, self.NOW, floor_exp=4)
        self.assertEqual(a_should, b_should)

    def test_default_is_bit_identical_to_pre_t1926_behaviour(self):
        """THE load-bearing regression guard: this function runs for all five agents.

        Asserts the no-floor path against the documented formula
        (due iff cycles_since_nudge + 1 >= 2**min(consec, cap)) across a wide matrix,
        so an opt-in agent's change cannot silently alter orchestrator/specialist/recovery.
        """
        for consec in range(0, 8):
            for cycles in range(0, 40):
                st = {"last_nudge_ts": self.NOW - 900, "work_ts_at_last_nudge": 100,
                      "consecutive_ineffective": consec, "cycles_since_nudge": cycles}
                expected_due = (cycles + 1) >= 2 ** min(consec, kg.BACKOFF_CAP_EXP)
                should, _, _ = kg.decide_nudge(st, None, self.NOW)
                with self.subTest(consec=consec, cycles=cycles):
                    self.assertEqual(should, expected_due)


class HasQueuedWorkTest(unittest.TestCase):
    """T-1926 detector: filesystem-only, fail-safe to False."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for d in ("inbox", "active", "done"):
            (self.root / "tickets" / d).mkdir(parents=True)
        self.cfg = {"pantheon_config_path": str(self.root),
                    "ticket_targets": frozenset({"artisan"})}

    def _write(self, state_dir, tid, target):
        (self.root / "tickets" / state_dir / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntarget: {target}\nstate: {state_dir}\n---\n\n# {tid} title\n",
            encoding="utf-8")

    def test_empty_queue_is_false(self):
        self.assertFalse(kg.has_queued_work(self.cfg))

    def test_inbox_ticket_for_agent_is_true(self):
        self._write("inbox", "T-0001", "artisan")
        self.assertTrue(kg.has_queued_work(self.cfg))

    def test_active_ticket_counts_this_is_the_parked_mid_arc_case(self):
        self._write("active", "T-0002", "artisan")
        self.assertTrue(kg.has_queued_work(self.cfg),
                        "a claimed-but-unfinished ticket IS the T-1926 failure state")

    def test_other_agents_tickets_do_not_count(self):
        self._write("inbox", "T-0003", "orchestrator")
        self._write("active", "T-0004", "recovery")
        self.assertFalse(kg.has_queued_work(self.cfg))

    def test_done_tickets_do_not_count(self):
        self._write("done", "T-0005", "artisan")
        self.assertFalse(kg.has_queued_work(self.cfg))

    def test_agent_without_ticket_targets_is_false(self):
        """Non-opted agents short-circuit before touching the filesystem."""
        self.assertFalse(kg.has_queued_work({"pantheon_config_path": str(self.root)}))

    def test_missing_tree_is_false_not_an_exception(self):
        """Fail-safe: a detector fault must never invent a queue and pin the floor on."""
        cfg = {"pantheon_config_path": "/nonexistent/path/xyz",
               "ticket_targets": frozenset({"artisan"})}
        self.assertFalse(kg.has_queued_work(cfg))


class QueueAwareOptInConfigTest(unittest.TestCase):
    """T-1926 gate: seats opt in, the other three deliberately do not."""

    def test_only_seats_are_opted_in(self):
        opted = {a for a, c in kg.AGENT_PANE.items() if c.get("queue_aware_backoff")}
        self.assertEqual(opted, {"mason", "artisan"},
                         "widening the floor to orchestrator/specialist/recovery is a separate decision; "
                         "it must not arrive as a side effect of an edit here")

    def test_every_opted_in_agent_has_ticket_targets(self):
        for agent, cfg in kg.AGENT_PANE.items():
            if cfg.get("queue_aware_backoff"):
                with self.subTest(agent=agent):
                    self.assertTrue(cfg.get("ticket_targets"),
                                    "queue_aware_backoff without ticket_targets is a "
                                    "silent no-op — the detector returns False forever")

    def test_floor_exponent_is_below_the_ceiling(self):
        self.assertLess(kg.QUEUED_BACKOFF_FLOOR_EXP, kg.BACKOFF_CAP_EXP,
                        "a floor at or above the cap would be a no-op")


class GateAwareQueuedWorkTest(unittest.TestCase):
    """T-1951: gated tickets are not actionable and must not floor the backoff."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for d in ("inbox", "active", "done"):
            (self.root / "tickets" / d).mkdir(parents=True)
        self.cfg = {"pantheon_config_path": str(self.root),
                    "ticket_targets": frozenset({"artisan"})}

    def _write(self, state_dir, tid, target, extra_fm=""):
        (self.root / "tickets" / state_dir / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntarget: {target}\nstate: {state_dir}\n{extra_fm}---\n\n# {tid} title\n",
            encoding="utf-8")

    def test_future_not_before_does_not_count(self):
        self._write("inbox", "T-0001", "artisan",
                    "not_before: 2099-01-01T00:00:00Z\n")
        self.assertFalse(kg.has_queued_work(self.cfg))

    def test_past_not_before_counts(self):
        self._write("inbox", "T-0001", "artisan",
                    "not_before: 2020-01-01T00:00:00Z\n")
        self.assertTrue(kg.has_queued_work(self.cfg))

    def test_malformed_not_before_fails_open_and_counts(self):
        # Mirrors inbox_consumer: malformed treated as missing (fail-open).
        self._write("inbox", "T-0001", "artisan", "not_before: not-a-date\n")
        self.assertTrue(kg.has_queued_work(self.cfg))

    def test_unmet_dep_does_not_count(self):
        self._write("inbox", "T-0002", "artisan", "depends_on: [T-0001]\n")
        self.assertFalse(kg.has_queued_work(self.cfg))

    def test_met_dep_counts(self):
        self._write("done", "T-0001", "artisan")
        self._write("inbox", "T-0002", "artisan", "depends_on: [T-0001]\n")
        self.assertTrue(kg.has_queued_work(self.cfg))

    def test_one_gated_one_clear_counts(self):
        self._write("inbox", "T-0003", "artisan", "depends_on: [T-9999]\n")
        self._write("inbox", "T-0004", "artisan")
        self.assertTrue(kg.has_queued_work(self.cfg))

    def test_gated_active_ticket_does_not_count(self):
        # Gate-filtering applies to active/ too: a deliberately paused arc
        # (calendar/dep-gated while claimed) is work the agent cannot start.
        self._write("active", "T-0005", "artisan",
                    "not_before: 2099-01-01T00:00:00Z\n")
        self.assertFalse(kg.has_queued_work(self.cfg))


class GateParityWithInboxConsumer(unittest.TestCase):
    """T-1951 drift-guard: keep_going's local gate parse must agree with
    inbox_consumer's classification — THE SEMANTICS OF RECORD — on every
    fixture. keep_going stays import-light in production (no consumer import);
    this TEST imports both sides so any drift fires here, loudly, instead of
    diverging silently (the shared-leaf shape is ruled out by the import-light
    constraint; the parity test is the drift mechanism instead)."""

    FIXTURES = (
        ("clear", ""),
        ("nb_future", "not_before: 2099-01-01T00:00:00Z\n"),
        ("nb_past", "not_before: 2020-01-01T00:00:00Z\n"),
        ("nb_date_only_future", "not_before: 2099-01-01\n"),
        ("nb_malformed", "not_before: not-a-date\n"),
        ("dep_unmet", "depends_on: [T-9990]\n"),
        ("dep_met", "depends_on: [T-9991]\n"),
        ("dep_mixed", "depends_on: [T-9991, T-9990]\n"),
        # T-2171: the bucket the guard could not see. keep_going had no labels arm
        # at all, so this fixture is the one that would have caught it.
        ("interactive", "labels: [interactive-required]\n"),
        ("interactive_with_other_labels", "labels: [tooling, interactive-required, org-012]\n"),
        ("label_prefix_not_the_label", "labels: [interactive-required-followup]\n"),
        ("labels_empty", "labels: []\n"),
    )

    def test_gate_verdicts_agree(self):
        from tools import inbox_consumer as ic
        for name, fm in self.FIXTURES:
            with self.subTest(fixture=name):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                root = Path(tmp.name)
                for d in ("inbox", "done"):
                    (root / "tickets" / d).mkdir(parents=True)
                # dep_met fixtures need T-9991 resolved on disk
                fm_base = ("created: 2026-07-23T00:00:00Z\nrequester: orchestrator\n"
                           "target: artisan\n")
                (root / "tickets" / "done" / "T-9991.md").write_text(
                    f"---\nid: T-9991\n{fm_base}state: done\n---\n\n# t\n")
                tpath = root / "tickets" / "inbox" / "T-0001.md"
                tpath.write_text(
                    f"---\nid: T-0001\n{fm_base}state: inbox\n{fm}---\n\n# t\n")

                kg_verdict = kg._ticket_is_actionable(
                    tpath, frozenset({"artisan"}), root / "tickets")

                # Consumer side: the REAL classifier, not a copy of it (T-2171).
                # The previous version re-implemented the classification inline, so
                # there were three implementations and this test pinned keep_going
                # against the copy — it could not detect drift in the thing its own
                # docstring names as the semantics of record, and demonstrably did
                # not (keep_going had no labels arm at all). list_eligible_tickets
                # reads the module global TICKETS_ROOT, so point that at the fixture
                # tree; that coupling is the price of testing the real gate.
                with mock.patch.object(ic, "TICKETS_ROOT", root / "tickets"):
                    try:
                        ic_verdict = bool(ic.list_eligible_tickets("artisan"))
                    except ic.SkipCycle:
                        ic_verdict = False

                self.assertEqual(
                    kg_verdict, ic_verdict,
                    f"gate drift on {name}: keep_going={kg_verdict} consumer={ic_verdict}")


class MappedOrExemptCoverageTest(unittest.TestCase):
    """T-2370: every registry persona is nudged (AGENT_PANE) or exempt with a stated
    reason (_NO_KEEP_GOING) — never silently absent.

    The defect this closes is SILENCE. archivist was missing from AGENT_PANE with no
    comment, and nothing in the code could distinguish that from the stale omission
    that had already left mason's and artisan's heartbeats dead from bringup. Same
    mapped-or-exempt shape as mailbox_mcp._NO_MCP_RECIPIENT (T-2285).
    """

    def _registry(self) -> set:
        try:
            from tools.agent_role_map import account_persona_map
        except ImportError:
            from agent_role_map import account_persona_map  # type: ignore[no-redef]
        return set(account_persona_map().values())

    def test_every_registry_persona_is_nudged_or_exempt(self) -> None:
        missing = sorted(self._registry() - set(kg.AGENT_PANE) - set(kg._NO_KEEP_GOING))
        self.assertEqual(
            missing, [],
            f"persona(s) {missing} are in NEITHER AGENT_PANE (nudged) nor "
            "_NO_KEEP_GOING (why not). A registry persona in neither gets no "
            "heartbeat for no stated reason — the T-2370 defect.",
        )

    def test_no_persona_is_both_nudged_and_exempt(self) -> None:
        both = sorted(set(kg.AGENT_PANE) & set(kg._NO_KEEP_GOING))
        self.assertEqual(both, [], f"persona(s) {both} are both nudged and exempt")

    def test_every_exemption_states_a_reason(self) -> None:
        for persona, reason in kg._NO_KEEP_GOING.items():
            with self.subTest(persona=persona):
                self.assertGreater(
                    len(reason.strip()), 40,
                    f"{persona}'s exemption reason is too thin to distinguish a "
                    "decision from an omission — which is the whole point",
                )

    def test_exemptions_are_registry_personas(self) -> None:
        # An exemption for a non-existent persona is dead text that outlives what it
        # described; it would quietly stop covering anything.
        stray = sorted(set(kg._NO_KEEP_GOING) - self._registry())
        self.assertEqual(stray, [], f"exemption(s) for unknown persona(s): {stray}")


class StateLockTest(unittest.TestCase):
    """T-2583: the read-decide-deliver-write critical section in run()'s adaptive
    branch must be serialized per-agent, so two concurrent invocations on one seat
    cannot both observe the same pre-nudge state and both decide to deliver (a
    double-nudge). Fail-open: any lock-acquisition problem degrades to the
    pre-T-2583 lockless behavior rather than blocking or dropping the nudge."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))

    def test_lock_serializes_concurrent_invocations(self):
        # Pre-seed a state sitting exactly AT the nudge threshold (consec=3 ->
        # threshold=8, cycles_since_nudge=7). Without a lock, two invocations that
        # both read this identical stale state would BOTH compute should_nudge=True
        # and BOTH deliver. With the lock, the second invocation must wait, then
        # re-read the first's freshly-written (bumped) state and correctly sit out.
        agent = "recovery"
        sp = kg._state_path(agent)
        kg.write_state(sp, {
            "last_nudge_ts": 1000, "work_ts_at_last_nudge": None,
            "consecutive_ineffective": 3, "cycles_since_nudge": 7,
            "session_id": "S1",
        })
        delivered = []

        def slow_work_ts(cfg):
            # Widens whichever thread's critical section wins the lock first, so
            # the second thread's acquisition attempt genuinely overlaps the first
            # thread holding the lock — proving serialization, not a lucky race.
            time.sleep(0.2)
            return None  # never advances -> the prior nudge is judged ineffective

        def inject_ok(pt, prompt):
            delivered.append(threading.get_ident())
            return True

        def run_once():
            return kg.run(agent, do_capture=lambda pt, uu: IDLE_PANE,
                          do_inject=inject_ok, do_work_ts=slow_work_ts,
                          do_session_id=lambda cfg: "S1",
                          do_cap_check=lambda *a, **k: False, now_ts=2000)

        t1 = threading.Thread(target=run_once)
        t2 = threading.Thread(target=run_once)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "a thread failed to join — lock leaked")
        self.assertEqual(len(delivered), 1,
                          "concurrent invocations on identical stale state must not both deliver")
        st = kg.read_state(sp)
        self.assertEqual(st["consecutive_ineffective"], 4,
                          "exactly one sequential decision applied, not a lost update")

    def test_lock_always_releases(self):
        agent = "recovery"
        for i in range(5):
            kg.run(agent, do_capture=lambda pt, uu: IDLE_PANE, do_inject=lambda pt, pr: True,
                  do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None,
                  do_cap_check=lambda *a, **k: False, now_ts=1000 + i * 900)
        # Direct probe: the lockfile must be free (non-blocking acquire succeeds).
        lp = kg._lock_path(agent)
        with open(lp, "w") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_lock_fail_open_on_oserror(self):
        with mock.patch.object(kg.fcntl, "flock", side_effect=OSError("no locks here")):
            rc = kg.run("recovery", do_capture=lambda pt, uu: IDLE_PANE,
                        do_inject=lambda pt, pr: True, do_work_ts=lambda cfg: None,
                        do_session_id=lambda cfg: None, do_cap_check=lambda *a, **k: False,
                        now_ts=1000)
        self.assertEqual(rc, 0, "lock unavailable must degrade to unlocked, not fail the nudge")


class LedgerTacCmdTest(unittest.TestCase):
    """T-4517: only <seg>/<seg>.jsonl is a shard; ledger/appetite/ must not crowd out the newest lines."""

    def test_non_shard_subdir_cannot_crowd_out_the_newest_shard(self) -> None:
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger"
            (ledger / "2026-W40").mkdir(parents=True)
            (ledger / "appetite").mkdir()
            (ledger / "2026-W40" / "2026-W40.jsonl").write_text(json.dumps({"action": "newest"}) + "\n")
            (ledger / "appetite" / "2026-W40.jsonl").write_text(
                "".join(json.dumps({"appetite_row": i}) + "\n" for i in range(600)))
            out = subprocess.run(["bash", "-c", kg._ledger_tac_cmd(td, 500)],
                                 capture_output=True, text=True, check=False).stdout
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(rows, [{"action": "newest"}])


FLEET_NOW = 1791140000            # 2026-10-04T18:53:20Z
FLEET_SEVEN_RESET = 1791738000    # 2026-10-11T17:00:00Z (observed live on specialist, Orchestrator)


class FleetUsageGateT4510(unittest.TestCase):
    """T-4510: at >=95% of the fleet's shared weekly Max the periodic keep-going nudge
    pauses and resumes by itself at the reset. The gate rides the EXISTING do_cap_check
    seam (default cap_or_usage_gate) — no parallel gate in run(). Fails open on every
    unknown. A hit cap (is_usage_capped) keeps its original behaviour and message."""

    def setUp(self):
        self.injected = []
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fleet = Path(self._tmp.name) / "fleet-usage"
        self.fleet.mkdir()
        patcher = mock.patch.dict(os.environ, {
            "PANTHEON_STATE_DIR": str(Path(self._tmp.name) / "state"),
            "PANTHEON_FLEET_USAGE_DIR": str(self.fleet),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # The fall-through (hit-cap) check reads real session jsonl via sudo: stub it.
        hit = mock.patch.object(kg, "is_usage_capped", return_value=False)
        self.hit_cap = hit.start()
        self.addCleanup(hit.stop)
        self.kw = dict(do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None,
                       do_capture=lambda pt, uu: IDLE_PANE, do_inject=self._inject_ok)

    def _inject_ok(self, pt, prompt):
        self.injected.append((pt, prompt))
        return True

    def _reading(self, pct, *, resets_at=FLEET_SEVEN_RESET, seat="orchestrator", ts=FLEET_NOW - 5):
        (self.fleet / f"{seat}.json").write_text(json.dumps({
            "schema": "pantheon-fleet-usage/v1", "ts": ts, "seat": seat,
            "seven_day": {"used_percentage": pct, "resets_at": resets_at},
            "five_hour": None}), encoding="utf-8")

    def _run(self, now_ts=FLEET_NOW, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = kg.run("recovery", now_ts=now_ts, **{**self.kw, **kw})
        return rc, buf.getvalue()

    def test_at_99_the_nudge_is_skipped_with_a_reason(self):
        self._reading(99)
        rc, out = self._run()
        self.assertEqual(rc, 0, "a gated skip is harmless, not an error")
        self.assertEqual(self.injected, [])
        self.assertIn("fleet usage gate", out)
        self.assertIn("99", out)

    def test_at_exactly_95_the_nudge_is_skipped(self):
        self._reading(95)
        self._run()
        self.assertEqual(self.injected, [])

    def test_below_95_still_nudges(self):
        self._reading(94)
        rc, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)

    def test_integer_percentage_as_observed_live(self):
        self._reading(97)   # int, as the live payload carries
        self._run()
        self.assertEqual(self.injected, [])

    def test_expired_reading_fails_open(self):
        self._reading(99, resets_at=FLEET_NOW - 1)
        self._run()
        self.assertEqual(len(self.injected), 1)

    def test_missing_dir_and_garbage_files_fail_open(self):
        self._run()
        self.assertEqual(len(self.injected), 1)
        (self.fleet / "junk.json").write_text("{{{ not json", encoding="utf-8")
        with mock.patch.object(kg, "ADAPTIVE", False):
            self._run()
        self.assertEqual(len(self.injected), 2)

    def test_logs_once_per_window_not_once_per_tick(self):
        self._reading(99)
        outs = [self._run(now_ts=FLEET_NOW + i * 900)[1] for i in range(3)]
        self.assertIn("fleet usage gate", outs[0])
        self.assertEqual(outs[1], "")
        self.assertEqual(outs[2], "")
        self.assertEqual(self.injected, [])

    def test_a_new_window_logs_again(self):
        self._reading(99)
        self.assertIn("fleet usage gate", self._run()[1])
        self._reading(99, resets_at=FLEET_SEVEN_RESET + 3600)   # a different reset => new window
        self.assertIn("fleet usage gate", self._run(now_ts=FLEET_NOW + 900)[1])

    def test_gated_skip_does_not_consume_the_backoff_budget(self):
        self._reading(99)
        self._run()
        self.assertEqual(self.injected, [])
        self._reading(10)     # usage fell (or the window rolled): first nudge is immediate
        self._run(now_ts=FLEET_NOW + 900)
        self.assertEqual(len(self.injected), 1)

    def test_nudges_resume_automatically_at_the_reset(self):
        self._reading(99)
        self._run(now_ts=FLEET_SEVEN_RESET - 900)
        self.assertEqual(self.injected, [])
        self._run(now_ts=FLEET_SEVEN_RESET + 1)     # reading now expired -> fail open
        self.assertEqual(len(self.injected), 1)

    def test_freshest_reading_across_seats_wins(self):
        self._reading(99, seat="orchestrator", ts=FLEET_NOW - 1000)
        self._reading(40, seat="artisan", ts=FLEET_NOW - 5)
        self._run()
        self.assertEqual(len(self.injected), 1)

    def test_gate_is_checked_before_the_jsonl_hit_cap_read(self):
        self._reading(99)
        self._run()
        self.hit_cap.assert_not_called()   # cheap file read first; no sudo jsonl scan

    def test_open_gate_falls_through_to_the_hit_cap_check(self):
        self._reading(10)
        self.hit_cap.return_value = True
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])
        self.hit_cap.assert_called_once()
        self.assertIn("usage/capability cap detected", out)   # original message intact

    def test_gate_exception_fails_open_to_the_hit_cap_check(self):
        def boom(now):
            raise RuntimeError("gate bug")
        got = kg.cap_or_usage_gate(kg.AGENT_PANE["recovery"], "recovery", FLEET_NOW, do_fleet_gate=boom)
        self.assertFalse(got)
        self.hit_cap.assert_called_once()

    def test_wrapper_return_shapes(self):
        cfg = kg.AGENT_PANE["recovery"]
        self._reading(99)
        got = kg.cap_or_usage_gate(cfg, "recovery", FLEET_NOW)
        self.assertTrue(got)
        self.assertIsNot(got, True)
        self.assertIn("99", got.reason)
        self._reading(10)
        self.assertIs(kg.cap_or_usage_gate(cfg, "recovery", FLEET_NOW), False)
        self.hit_cap.return_value = True
        self.assertIs(kg.cap_or_usage_gate(cfg, "recovery", FLEET_NOW), True)

    def test_a_failing_log_marker_does_not_fail_the_unit(self):
        # should_log_once can raise (Path.home() -> RuntimeError); an unguarded call in run()
        # would turn a harmless suppression into a unit failure alert. Noisy beats failing.
        self._reading(99)
        with mock.patch.object(kg._fug, "should_log_once", side_effect=RuntimeError("no home")):
            rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])
        self.assertIn("fleet usage gate", out)

    def test_default_seam_is_the_wrapper_not_a_parallel_gate(self):
        import inspect
        default = inspect.signature(kg.run).parameters["do_cap_check"].default
        self.assertIs(default, kg.cap_or_usage_gate)

    def test_kill_switch_env_restores_old_behaviour(self):
        self._reading(99)
        with mock.patch.dict(os.environ, {"PANTHEON_USAGE_GATE_ENABLED": "0"}):
            self._run()
        self.assertEqual(len(self.injected), 1)


if __name__ == "__main__":
    unittest.main()
