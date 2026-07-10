"""test_keep_going.py — anti-idle nudge (tools/keep_going.py, T-0772 sibling).

Covers the idle-gate tri-state, the run() wiring (nudge only when parked at the
idle prompt; never into active work or a modal; fail-safe on uncapturable/inconclusive),
and the adaptive backoff (T-1364): a nudge that produces no work doubles the wait;
a nudge that produces work resets to the base cadence. Pane/ledger I/O is stubbed —
no live tmux, no real ledger, isolated state dir.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keep_going as kg  # noqa: E402

IDLE_PANE = "\n".join([
    "│ > some earlier output", "╭───────────────╮", "│ ❯             │",
    "╰───────────────╯", "  ? for shortcuts",
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
        self.assertIs(kg.is_idle_at_prompt(MODAL_PANE), False)

    def test_empty_capture_inconclusive(self):
        self.assertIsNone(kg.is_idle_at_prompt(""))
        self.assertIsNone(kg.is_idle_at_prompt("\n  \n   \n"))

    def test_unknown_text_inconclusive(self):
        self.assertIsNone(kg.is_idle_at_prompt("just some\nrandom lines\nno sentinels"))

    def test_idle_with_hidden_task_list_footer(self):
        # Regression (2026-06-28): orchestrator keeps a hidden task list, so its idle
        # footer reads "ctrl+t to show tasks" — NOT "? for shortcuts". This must
        # read as idle, or keep_going skips it forever (the weekly-cap-reset gap).
        pane = "\n".join([
            "│ > earlier output", "╭───────────────╮", "│ ❯             │",
            "╰───────────────╯", "  ctrl+t to show tasks · ← for agents · new task? /clear to save 120.3k tokens",
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
        self.kw = dict(do_work_ts=lambda cfg: None, do_session_id=lambda cfg: None)

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
        rc = kg.run("recovery", do_capture=self._cap(IDLE_PANE), do_inject=self._inject_fail, **self.kw)
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
        # routed to its own canonical pane (matches inbox_consumer's config).
        expected = {
            "orchestrator": "claude-telegram-bot:0",
            "specialist": "claude-research-bot:0",
            "recovery": "claude-telegram-bot-fallback:0",
        }
        for agent, pane in expected.items():
            sent = []
            rc = kg.run(agent, do_capture=self._cap(IDLE_PANE),
                        do_inject=lambda pt, pr: sent.append((pt, pr)) or True, **self.kw)
            self.assertEqual(rc, 0)
            self.assertEqual(len(sent), 1, f"{agent} must nudge when parked")
            self.assertEqual(sent[0][0], pane, f"{agent} routed to wrong pane")


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
        return kg.run("specialist",
                      do_capture=lambda pt, uu: IDLE_PANE,
                      do_inject=lambda pt, pr: self.sent.append(now) or True,
                      do_work_ts=lambda cfg: work_ts,
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
                   do_work_ts=lambda cfg: None, now_ts=t + cycle * 900, adaptive=False)
        self.assertEqual(len(self.sent), 5, "legacy mode nudges on every idle cycle")


if __name__ == "__main__":
    unittest.main()
