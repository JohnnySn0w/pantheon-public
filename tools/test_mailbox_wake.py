"""test_mailbox_wake.py — the wake half of the ORG-026 mailbox (T-0772).

Covers the decision logic (peek / debounce / wake), the marker advance-only-on-success
rule, and the wake-only invariant (the waker NEVER moves/acks a message). The live
tmux inject is stubbed in every case but one — these are pure-logic tests, no pane
required. The exception is T-2245's
test_real_inject_into_a_dead_seat_fails_and_leaves_the_mail_unread, which drives the
REAL inject against a uuid-named target that cannot exist (and skips where tmux does
not) — nothing here ever addresses a live agent pane.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mailbox  # noqa: E402
import pathlib  # noqa: E402
import mailbox_wake as mw  # noqa: E402


class WakeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.new = self.inbox / "new"
        self.marker = self.inbox / mw._MARKER_NAME
        self.injected = []  # records (pane, prompt, harness) the stub was asked to send

    def tearDown(self):
        self._tmp.cleanup()

    def _ok_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return True

    def _fail_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return False

    def _run(self, inject, session="S1", now=1000.0, pane_idle=None):
        # Deterministic session identity + clock: these are pure-logic tests, no
        # live tmux query (T-1901 added both knobs to run(); T-2158 added the third).
        # pane_idle DEFAULTS TO None so every pre-existing case keeps legacy
        # behaviour AND so no test ever captures a real pane — the default
        # pane_is_idle would shell out to tmux against a live agent session.
        return mw.run("recovery", inbox=self.inbox, do_inject=inject,
                      session_key_fn=lambda pane: session, now=now,
                      pane_idle_fn=lambda pane: pane_idle)

    # -------------------------------------------------------------- empty/peek
    def test_empty_inbox_no_wake(self):
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "empty inbox must not kick")

    # ------------------------------------------------- backoff-aware count (T-2384)
    # The defect these cover: T-2386 stopped a poison message from SPINNING but not
    # from WAKING. A message inside its retry-backoff window is one the drain will
    # refuse to claim, yet a raw readdir still counts it as unread — so the waker
    # re-kicks it once per REKICK_TTL forever, which is mnemosyne's 242 consecutive
    # true-but-useless "1 unread" wakes across three days. peek() stays raw; the
    # WAKE decision must ask the same question the drain asks.
    def test_backed_off_message_does_not_wake(self):
        name = mailbox.send(self.inbox, {"i": 1}, sender="inbox_consumer")
        mailbox._record_backoff(self.inbox, name)     # drain refuses to claim it now
        self.assertEqual(mw.peek(self.new), [name], "peek stays the raw readdir")
        self.assertEqual(mw.deliverable(self.new), [],
                         "a backed-off message is not deliverable")
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [],
                         "waking for a message the drain will not hand over burns a "
                         "turn and delivers nothing")
        self.assertEqual(sorted(p.name for p in self.new.iterdir()), [name],
                         "and the message must still be sitting unread in new/")

    def test_backoff_expiry_releases_the_wake(self):
        name = mailbox.send(self.inbox, {"i": 1}, sender="inbox_consumer")
        not_before = mailbox._record_backoff(self.inbox, name)
        self.assertEqual(mw.deliverable(self.new), [])
        # Same file, same readdir — only the clock moved past the window.
        self.assertEqual(mailbox.backoff_active(self.inbox, name, now=not_before + 1),
                         False)
        self.assertEqual(mw.deliverable(self.new, now=not_before + 1), [name])
        rc = self._run(self._ok_inject, now=not_before + 1)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "a lapsed backoff must wake normally")

    def test_only_the_deliverable_messages_are_counted(self):
        stuck = mailbox.send(self.inbox, {"i": 1}, sender="inbox_consumer")
        fresh = mailbox.send(self.inbox, {"i": 2}, sender="orchestrator")
        mailbox._record_backoff(self.inbox, stuck)
        self.assertEqual(mw.deliverable(self.new), [fresh])
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)
        text = self.injected[0][1]
        self.assertIn("You have 1 unread inter-agent message", text)
        self.assertIn("orchestrator", text)
        self.assertNotIn("(from inbox_consumer", text,
                         "the undeliverable message must not be named as the reason "
                         "for the kick")

    def test_unreadable_backoff_sidecar_still_wakes(self):
        # Fail-soft direction: a broken backoff store degrades to a spurious wake,
        # never to silence. Silence here would be lost mail.
        name = mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        sidecar = self.inbox / ".backoff" / name
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("{not json at all")
        self.assertEqual(mw.deliverable(self.new), [name])
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)

    def test_deliverable_degrades_to_peek_without_the_transport_module(self):
        # The mis-import this guards is real: a bare `import mailbox` off-path binds
        # Python's STDLIB mailbox, which has none of our helpers. Counting nothing
        # would silence every wake on the box.
        name = mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        mailbox._record_backoff(self.inbox, name)
        original = mw._mailbox_module
        mw._mailbox_module = lambda: None
        try:
            self.assertEqual(mw.deliverable(self.new), [name],
                             "no transport module → count it (pre-T-2384 behaviour)")
        finally:
            mw._mailbox_module = original

    def test_mailbox_module_rejects_the_stdlib_impostor(self):
        mod = mw._mailbox_module()
        self.assertIsNotNone(mod)
        self.assertTrue(hasattr(mod, "backoff_active"))
        # Identity is NOT the assertion: `tools.mailbox` and `mailbox` are two
        # distinct module objects for one file (the double-import trap mailbox.py
        # documents). What must hold is that we resolved OUR file and not the
        # stdlib's — same-file is the property, sameness of object is not.
        self.assertEqual(Path(mod.__file__).resolve(),
                         (Path(__file__).parent / "mailbox.py").resolve())

    def test_peek_skips_dotfiles_and_is_sorted(self):
        mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        mailbox.send(self.inbox, {"i": 2}, sender="specialist")
        (self.new / ".hidden").write_text("x")  # lock/marker-style noise
        names = mw.peek(self.new)
        self.assertEqual(len(names), 2)
        self.assertEqual(names, sorted(names))
        self.assertFalse(any(n.startswith(".") for n in names))

    # --------------------------------------------------------------- wake path
    def test_wake_injects_and_advances_marker(self):
        mailbox.send(self.inbox, {"task": "T-0767"}, sender="orchestrator")
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "a fresh message must kick once")
        self.assertIn("orchestrator", self.injected[0][1])
        self.assertEqual(self.injected[0][0], "claude-telegram-bot-fallback:0")
        self.assertTrue(self.marker.exists(), "marker advances on a landed kick")

    def test_wake_atomically_replaces_unwritable_stale_marker(self):
        self.marker.write_text("stale\n", encoding="utf-8")
        self.marker.chmod(0)

        mw._write_marker(self.marker, "newest", "S1", 1000.0)

        self.assertEqual(
            mw._read_marker(self.marker),
            {
                "newest": "newest",
                "session": "S1",
                "ts": 1000.0,
                "idle_kicked": False,
            },
        )
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o660)
        self.assertEqual(list(self.inbox.glob(f"{self.marker.name}.*")), [])

    def test_failed_atomic_marker_replace_preserves_original_and_cleans_temp(self):
        self.marker.write_text("original\n", encoding="utf-8")

        with mock.patch.object(
            mw.os,
            "replace",
            side_effect=PermissionError("synthetic replace denial"),
        ):
            with self.assertRaisesRegex(PermissionError, "synthetic replace denial"):
                mw._write_marker(self.marker, "newest", "S1", 1000.0)

        self.assertEqual(self.marker.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.inbox.glob(f"{self.marker.name}.*")), [])

    def test_codex_pane_passes_codex_harness(self):
        # T-1106: codex's pane must select the codex submit-verify profile, else
        # send_keys_submit false-passes on the always-absent claude-code sentinel.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        # Stub pane state so the harness assertion is isolated from the real pane
        # (T-2681 added a confirmed-busy skip to run(); without a stub this shells
        # out to the live pane and skips the inject whenever that pane is busy).
        mw.run("codex", inbox=self.inbox, do_inject=self._ok_inject,
               pane_idle_fn=lambda pane: None)
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][2], "codex", "codex pane must pass --harness codex")
        self.assertEqual(self.injected[0][0], "codex-session")

    def test_claude_persona_passes_default_harness(self):
        # A Claude-Code persona (recovery) keeps the default claude-code profile.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        # Stub pane state — see test_codex_pane_passes_codex_harness (T-2681).
        mw.run("recovery", inbox=self.inbox, do_inject=self._ok_inject,
               pane_idle_fn=lambda pane: None)
        self.assertEqual(self.injected[0][2], "claude-code")

    def test_wake_only_never_acks(self):
        # The load-bearing invariant: the waker leaves every message in new/ (no move).
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)
        self.assertTrue((self.new / mid).exists(), "waker must NOT move the message out of new/")
        self.assertFalse((self.inbox / "cur" / mid).exists(), "waker must NOT ack to cur/")

    # ----------------------------------------------------------------- debounce
    def test_debounce_no_rekick_while_unchanged(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)            # first → wake
        self._run(self._ok_inject)            # second, nothing newer → debounced
        self.assertEqual(len(self.injected), 1, "must not re-kick while newest is unchanged")

    def test_newer_message_rekicks(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)            # wake (1)
        mailbox.send(self.inbox, {"x": 2}, sender="specialist")  # genuinely newer
        self._run(self._ok_inject)            # wake again (2)
        self.assertEqual(len(self.injected), 2, "a newer message must re-nudge")

    # ----------------------------------------------------------- failed inject
    def test_failed_kick_does_not_advance_marker(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        rc = self._run(self._fail_inject)
        self.assertEqual(rc, 8, "a failed kick returns non-zero")
        self.assertFalse(self.marker.exists(), "marker must NOT advance on a dropped kick")
        # Next fire retries (marker never advanced) — message still in new/.
        rc2 = self._run(self._ok_inject)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self.injected), 2, "dropped kick is retried on the next fire")

    # --------------------------------------------------------------- malformed
    def test_malformed_message_does_not_break_waker(self):
        (self.new / "00000000000000000001.1.0.x").write_text("not json\n")
        rc = self._run(self._ok_inject)  # _senders must swallow the parse error
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "still wakes despite an unparseable message")

    def test_unknown_agent_rejected(self):
        self.assertEqual(mw.run("nobody", inbox=self.inbox, do_inject=self._ok_inject), 64)

    # ------------------------------------------------- T-1901: session identity
    def test_session_restart_rekicks_same_newest(self):
        # The Metis first-retask hole: kick landed, session restarted, newest-id
        # unchanged — the fresh session must be re-nudged.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject, session="S1")   # wake into S1
        self._run(self._ok_inject, session="S1")   # same session → debounced
        self._run(self._ok_inject, session="S2")   # restarted pane → re-kick
        self.assertEqual(len(self.injected), 2, "a session change must re-nudge")

    def test_session_query_failure_falls_back_to_legacy_debounce(self):
        # A flaky/absent tmux (session None) must NOT cause kick spam: newest-only
        # debounce governs, exactly the pre-T-1901 behavior.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject, session="S1")
        self._run(self._ok_inject, session=None)   # can't read identity → debounce
        self.assertEqual(len(self.injected), 1)

    def test_legacy_plaintext_marker_wakes_once_then_upgrades(self):
        # Pre-upgrade marker can't prove the kick reached the CURRENT session:
        # wake once (un-strands a stuck seat), rewrite marker in JSON form.
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self.marker.write_text(mid + "\n")          # legacy format, newest matches
        self._run(self._ok_inject, session="S1")    # → wake (marker predates tracking)
        self.assertEqual(len(self.injected), 1)
        self.assertIn('"session": "S1"', self.marker.read_text(), "marker upgraded to JSON")
        self._run(self._ok_inject, session="S1")    # now tracked → debounced
        self.assertEqual(len(self.injected), 1)

    def test_legacy_marker_without_session_key_stays_debounced(self):
        # Legacy marker AND no readable session identity → exact legacy behavior.
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self.marker.write_text(mid + "\n")
        self._run(self._ok_inject, session=None)
        self.assertEqual(self.injected, [], "no regression when tmux is unqueryable")

    # ---------------------------------------------------- T-1901: re-kick TTL
    def test_ttl_expiry_rekicks_unconsumed_mail(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject, now=1000.0)                          # wake
        self._run(self._ok_inject, now=1000.0 + mw.REKICK_TTL_SECONDS - 1)  # inside → debounced
        self._run(self._ok_inject, now=1000.0 + mw.REKICK_TTL_SECONDS + 1)  # past → re-kick
        self.assertEqual(len(self.injected), 2, "unconsumed mail re-nudges once per TTL")

    def test_ttl_rekick_is_bounded_one_per_window(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        t0 = 1000.0
        self._run(self._ok_inject, now=t0)                                   # wake 1
        t1 = t0 + mw.REKICK_TTL_SECONDS + 1
        self._run(self._ok_inject, now=t1)                                   # wake 2 (TTL)
        self._run(self._ok_inject, now=t1 + 10)                              # fresh marker → debounced
        self.assertEqual(len(self.injected), 2, "TTL re-kick must not spam every fire")

    # ------------------------------------------------- decide() reason surface
    def test_decide_reports_reasons(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        action, names, reason = mw.decide(self.new, self.marker, "S1", 1000.0)
        self.assertEqual(action, "wake")
        self.assertIn("newer", reason)

    # ---------------------------------------------------------- session_key()
    def test_session_key_swallows_bad_target(self):
        # Nonexistent pane (or no tmux at all) → None, never an exception.
        self.assertIsNone(mw.session_key("definitely-not-a-real-session-xyz:0"))

    # ------------------------------------------- T-2245: the REAL inject, for once
    def test_real_inject_into_a_dead_seat_fails_and_leaves_the_mail_unread(self):
        """T-2245 acceptance 6: send-side liveness at the wake rung, exercised for real.

        THE GAP THIS CLOSES. A sender cannot tell that a dispatch went into a seat
        with no live session — silence back is indistinguishable from a busy agent.
        This arc's own dispatch was lost exactly that way on 2026-07-28: sent to a
        dormant seat, the wake unit failed, and it surfaced only because an
        OnFailure alert routed to a third party. `run()`'s rc 8 IS that alert's
        trigger, so it is the load-bearing rung.

        WHAT IS ACTUALLY NEW HERE, stated so this does not imply more than it is:
        rc == 8 is ALREADY covered by test_failed_kick_does_not_advance_marker,
        which stubs inject. The lift is that this drives the REAL `mw.inject` —
        real subprocess, real send_keys_submit.sh, real tmux — against a target
        that does not exist, plus the effect assertions (marker not advanced, mail
        still in new/). Nothing else in this file touches the real inject path.

        The stderr assertion is deliberate: `inject() is False` on its own is also
        what a missing helper, an absent tmux, or any unrelated subprocess failure
        returns, so a bare False would not prove the DEAD-TARGET path was the one
        exercised. tmux's own `can't find session` for our uuid target does.

        Skip-not-fail where the substrate is absent, per
        test_restart_drain.test_validate_on_live_fleet_topology: a red here on a
        bare CI container measures the box, not the code; a skip says plainly that
        the check did not run.
        """
        if shutil.which("tmux") is None:
            self.skipTest("tmux absent — this exercises the real inject subprocess; "
                          "a failure here would measure the box, not the code")
        if not mw._SEND_KEYS_HELPER.exists():
            self.skipTest(f"{mw._SEND_KEYS_HELPER} absent — same reason")

        # uuid4 so this provably cannot name any live agent pane. The waker is
        # never allowed to address cfg["pane_target"] in this test.
        dead_seat = f"nosuch-t2245-{uuid.uuid4().hex}:0"

        def real_inject_at_dead_seat(pane, prompt, harness=mw._DEFAULT_HARNESS):
            return mw.inject(dead_seat, prompt, harness)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(mw.inject(dead_seat, "wake check"),
                             "injecting into a nonexistent tmux target must report failure")
        diag = err.getvalue()
        # T-2732: tmux's dead-target error depends on whether a server is running. Server up but
        # the SESSION is gone -> "can't find session: <name>" (names our target). No server at
        # all -> "no server running on <socket>" (names the socket, not the session). ORG-066
        # Rung-1 provisioned tmux on CT202, un-skipping this test onto exactly the no-server
        # variant; before that the box had no tmux and this skipped. BOTH prove inject() failed
        # for the dead-target reason this test exists to exercise (not an ambient subprocess
        # failure), so accept either.
        session_gone = "can't find session" in diag
        no_server = "no server running on" in diag
        self.assertTrue(
            session_gone or no_server,
            f"inject returned False, but not for the dead-target reason this test exists to "
            f"exercise (expected \"can't find session\" or \"no server running on\"). "
            f"stderr was: {diag!r}")
        # When a server IS up, the error names OUR uuid target — assert that, so a session-gone
        # error for some AMBIENT pane can't satisfy the test. With no server at all there is no
        # ambient pane to confuse us with, and tmux never parses the target name, so this
        # target-naming guard structurally does not apply.
        if session_gone:
            self.assertIn(dead_seat.split(":")[0], diag,
                          "the session-gone failure must name OUR uuid target, not some ambient pane")

        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = self._run(real_inject_at_dead_seat)
        self.assertEqual(rc, 8, "a wake into a dead seat must exit non-zero (8) so the "
                               "unit's OnFailure fires — silence is not an outcome")
        self.assertFalse(self.marker.exists(),
                         "a failed kick must NOT advance the marker, or the next fire "
                         "debounces and the message is stranded")
        self.assertTrue((self.new / mid).exists(),
                        "the message must still be unread in new/ — the waker never acks")


class IdleRekickCondition5Test(unittest.TestCase):
    """T-2158: a kick injected MID-TURN submit-verifies (the input line clears) but is
    never acted on, and the debounce then correctly counts it delivered. Specialist sat 32
    minutes on a 12-message backlog with every log line green. Condition 5 re-kicks on
    the busy->idle TRANSITION so the failure self-heals within one timer period instead
    of waiting out the 1800s TTL backstop.

    The load-bearing test in here is test_busy_with_unread_still_debounces: condition 5
    must NEVER interrupt an agent that is legitimately working, which was the whole
    reason declining to kick specialist was the right call at 04:19 that night.
    """

    # Own fixture rather than subclassing WakeTest: inheriting it would silently
    # RE-RUN all 19 base cases under this class's name, inflating the count with
    # duplicates — the count-stability lesson from T-2170, one layer up.
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.new = self.inbox / "new"
        self.marker = self.inbox / mw._MARKER_NAME
        self.injected = []
        self.addCleanup(self._tmp.cleanup)

    def _ok_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return True

    def _fail_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return False

    def _run(self, inject, session="S1", now=1000.0, pane_idle=None):
        return mw.run("recovery", inbox=self.inbox, do_inject=inject,
                      session_key_fn=lambda pane: session, now=now,
                      pane_idle_fn=lambda pane: pane_idle)

    def _kicked_state(self, now=1000.0, idle_kicked=False):
        """Deliver one message and record a kick for it, as a prior sweep would."""
        mailbox.send(str(self.inbox), payload="p", sender="orchestrator", subject="s")
        names = mw.peek(self.new)
        mw._write_marker(self.marker, names[-1], "S1", now, idle_kicked=idle_kicked)
        return names

    def test_idle_with_unread_after_kick_wakes(self):
        self._kicked_state()
        action, _, reason = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual(action, "wake")
        self.assertEqual(reason, mw.IDLE_REKICK_REASON)

    def test_busy_with_unread_still_debounces(self):
        """THE REGRESSION GUARD: a generating agent must not be interrupted."""
        self._kicked_state()
        action, _, _ = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=False)
        self.assertEqual(action, "debounced")

    def test_unknown_idle_state_is_legacy_behaviour(self):
        """None = UNKNOWN must never wake — same contract as session_key()."""
        self._kicked_state()
        action, _, _ = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=None)
        self.assertEqual(action, "debounced")

    def test_idle_rekick_fires_only_once_per_newest_message(self):
        """Firing on the TRANSITION is what removes the need for a spam floor."""
        self._kicked_state(idle_kicked=True)
        action, _, _ = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual(action, "debounced")

    def test_ttl_backstop_still_fires_independently_when_idle_kick_is_spent(self):
        """Condition 4 must remain the backstop for a swallowed idle kick."""
        self._kicked_state(idle_kicked=True)
        late = 1000.0 + mw.REKICK_TTL_SECONDS + 1
        action, _, reason = mw.decide(self.new, self.marker, "S1", late, pane_idle=True)
        self.assertEqual(action, "wake")
        self.assertIn("older than", reason)

    def test_ttl_backstop_still_fires_when_pane_is_busy(self):
        self._kicked_state()
        late = 1000.0 + mw.REKICK_TTL_SECONDS + 1
        action, _, _ = mw.decide(self.new, self.marker, "S1", late, pane_idle=False)
        self.assertEqual(action, "wake")

    def test_newer_message_still_wins_over_idle_state(self):
        self._kicked_state()
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        action, _, reason = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual((action, reason), ("wake", "newer message"))

    def test_no_marker_means_no_idle_rekick_path(self):
        """Condition 5 requires a RECORDED kick; a fresh inbox wakes as 'newer message'."""
        mailbox.send(str(self.inbox), payload="p", sender="orchestrator", subject="s")
        action, _, reason = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual((action, reason), ("wake", "newer message"))

    # ------------------------------------------------------------- marker state
    def test_idle_triggered_kick_is_recorded_on_the_marker(self):
        self._kicked_state()
        rc = self._run(self._ok_inject, now=1100.0, pane_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)
        self.assertTrue(mw._read_marker(self.marker)["idle_kicked"])

    def test_other_wake_reasons_reset_idle_kicked(self):
        """A fresh cause deserves a fresh idle re-kick if that one is swallowed too."""
        self._kicked_state(idle_kicked=True)
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        self._run(self._ok_inject, now=1100.0, pane_idle=True)
        self.assertFalse(mw._read_marker(self.marker)["idle_kicked"])

    def test_failed_idle_kick_does_not_advance_the_marker(self):
        """Advance-only-on-success holds for condition 5 too, or the retry is lost."""
        self._kicked_state()
        before = mw._read_marker(self.marker)
        self._run(self._fail_inject, now=1100.0, pane_idle=True)
        self.assertEqual(mw._read_marker(self.marker), before)

    def test_busy_pane_defers_wake_without_failing_or_injecting(self):
        """T-2681: a CONFIRMED-busy pane must DEFER (exit 0), not attempt a doomed
        inject that fails 'submit unconfirmed' and records a FALSE unit failure (8).
        The marker stays put so the next .path fire retries once the pane is idle."""
        self._kicked_state()
        # A newer message makes decide() return 'wake' regardless of pane state.
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        before = mw._read_marker(self.marker)
        rc = self._run(self._ok_inject, now=1100.0, pane_idle=False)
        self.assertEqual(rc, 0, "a busy pane defers with exit 0, not a unit failure")
        self.assertEqual(self.injected, [], "must NOT inject into a confirmed-busy pane")
        self.assertEqual(mw._read_marker(self.marker), before,
                         "marker not advanced → the next fire retries when idle")

    def test_idle_pane_inject_failure_still_returns_8(self):
        """T-2681: the busy-skip must NOT swallow a GENUINE failure. An idle pane
        that rejects the submit (or a dead seat, pane_idle unknown) still exits 8
        so the unit's OnFailure fires — only the confirmed-busy collision is benign."""
        self._kicked_state()
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        rc = self._run(self._fail_inject, now=1100.0, pane_idle=True)
        self.assertEqual(rc, 8, "an idle pane rejecting the submit is a real anomaly")
        self.assertEqual(len(self.injected), 1, "an idle pane IS injected (then fails)")

    def test_legacy_plaintext_marker_reads_idle_kicked_false(self):
        self.marker.write_text("some-old-name\n", encoding="utf-8")
        self.assertFalse(mw._read_marker(self.marker)["idle_kicked"])

    def test_marker_roundtrips_idle_kicked(self):
        mw._write_marker(self.marker, "n", "S1", 1.0, idle_kicked=True)
        self.assertTrue(mw._read_marker(self.marker)["idle_kicked"])

    # --------------------------------------------------------- the impure probe
    def test_pane_is_idle_returns_none_when_tmux_fails(self):
        """Fail-safe contract: ANY failure is UNKNOWN, never a wake."""
        self.assertIsNone(mw.pane_is_idle("no-such-session-t2158:0"))

    def test_pane_is_idle_returns_none_on_empty_target(self):
        self.assertIsNone(mw.pane_is_idle(""))


class QueuedSubjectsInTheKickTest(unittest.TestCase):
    """T-2266 (mnemosyne): the COUNT is the defect. Delivery is FIFO at one message
    per turn, so a correction is authored after the claim it corrects and therefore
    consumed after it — every time. A faster drain narrows that window; it cannot
    close it. "You have 4 unread messages" gives the receiving turn no way to know
    that message 4 retracts message 1."""

    def test_kick_names_what_is_queued_in_order(self):
        text = mw.build_kick("recovery", ["a", "b"], ["orchestrator"],
                             ["orchestrator: CLAIM: the runner is wedged",
                              "orchestrator: RETRACTION: it was queued, not wedged"])
        self.assertIn("RETRACTION: it was queued, not wedged", text,
                      "a correction sitting behind the claim it corrects must be "
                      "visible BEFORE the claim is acted on")
        self.assertIn("CLAIM: the runner is wedged", text)
        self.assertLess(text.index("CLAIM: the runner"),
                        text.index("RETRACTION: it was"),
                        "oldest first — the reader needs the real delivery order")
        self.assertIn("FIFO", text, "say WHY the order matters, or it reads as decoration")

    def test_truncation_is_declared_not_silent(self):
        """A silently clipped list reintroduces exactly the invisibility this fixes."""
        subjects = [f"peer: subject {i}" for i in range(8)]
        text = mw.build_kick("recovery", [str(i) for i in range(20)], ["peer"], subjects)
        self.assertIn("and 12 more", text)

    def test_no_subjects_leaves_the_kick_unchanged(self):
        """Back-compat: the waker must still work when subjects cannot be read."""
        text = mw.build_kick("recovery", ["a"], ["orchestrator"], None)
        self.assertIn("You have 1 unread inter-agent message", text)
        self.assertNotIn("QUEUED", text)

    def test_queued_subjects_reads_sender_and_subject_from_real_files(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            nd = pathlib.Path(d)
            (nd / "m1").write_text(json.dumps({"sender": "specialist", "subject": "hello"}))
            (nd / "m2").write_text("not json")            # must not break the waker
            got = mw._queued_subjects(nd, ["m1", "m2"])
        self.assertEqual(got, ["specialist: hello"])


if __name__ == "__main__":
    unittest.main()
