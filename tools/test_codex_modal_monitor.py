"""Tests for codex_modal_monitor.py (T-1366) — detect codex wedged on the rate-limit
model-switch modal + alert Recovery. Fixtures are the REAL 2026-06-28 codex pane captures."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codex_modal_monitor as cm  # noqa: E402


class TimerContract(unittest.TestCase):
    def test_level_triggered_timer_rearms_after_manager_restart(self):
        timer = (Path(__file__).resolve().parent / "systemd" /
                 "pantheon-codex-modal-monitor.timer").read_text(encoding="utf-8")
        self.assertIn("OnActiveSec=1min", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertNotRegex(timer, r"(?m)^\s*Persistent\s*=")

# Real capture (2026-06-28) of codex wedged on the model-switch modal.
MODAL_PANE = "\n".join([
    "• STATUS: idle_ready",
    "────────────────────────────────────────────────────────────────────────────────",
    "  Approaching rate limits",
    "  Switch to gpt-5.4-mini for lower credit usage?",
    "› 1. Switch to gpt-5.4-mini                 Small, fast, and cost-efficient",
    "  2. Keep current model",
    "  3. Keep current model (never show again)  Hide future rate limit reminders",
    "  Press enter to confirm or esc to go back",
])

# codex back at a clean idle composer (modal dismissed) — only the warning lingers.
IDLE_WITH_WARNING = "\n".join([
    "• STATUS: idle_ready",
    "  MAILBOX: no unread items",
    "⚠ Heads up, you have less than 10% of your weekly limit left. Run /status for a",
    "  breakdown.",
    "────────────────────────────────────────────────────────────────────────────────",
    "› Implement {feature}",
    "  gpt-5.5 default · ~",
])


class Classify(unittest.TestCase):
    def test_modal_detected(self):
        self.assertEqual(cm.classify_codex_pane(MODAL_PANE), "model_switch_modal")

    def test_idle_with_warning_is_not_modal(self):
        # The <10% warning is NOT the wedging modal — codex isn't blocked by it.
        self.assertIsNone(cm.classify_codex_pane(IDLE_WITH_WARNING))

    def test_empty_is_none(self):
        self.assertIsNone(cm.classify_codex_pane(""))
        self.assertIsNone(cm.classify_codex_pane("\n  \n"))

    def test_title_alone_without_structure_is_none(self):
        # A passing mention of the title with no modal structure → not a wedge.
        self.assertIsNone(cm.classify_codex_pane("we were approaching rate limits earlier"))

    def test_title_in_old_scrollback_ignored(self):
        # Title far above the live bottom region (lots of newer output) → ignored.
        pane = "  Approaching rate limits\n  esc to go back\n" + "\n".join(
            f"line {i}" for i in range(40)) + "\n› Implement {feature}\n  gpt-5.5 default"
        self.assertIsNone(cm.classify_codex_pane(pane))


class Run(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self.alerts = []

    def _alert(self, msg):
        self.alerts.append(msg)

    def test_alerts_on_modal(self):
        rc = cm.run(do_capture=lambda: MODAL_PANE, do_alert=self._alert, now=1000)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Esc", self.alerts[0])  # the safe-dismiss instruction

    def test_no_alert_when_no_modal(self):
        rc = cm.run(do_capture=lambda: IDLE_WITH_WARNING, do_alert=self._alert, now=1000)
        self.assertEqual(rc, 0)
        self.assertEqual(self.alerts, [])

    def test_no_alert_when_uncapturable(self):
        rc = cm.run(do_capture=lambda: None, do_alert=self._alert, now=1000)
        self.assertEqual(rc, 0)
        self.assertEqual(self.alerts, [])

    def test_cooldown_suppresses_repeat(self):
        cm.run(do_capture=lambda: MODAL_PANE, do_alert=self._alert, now=1000, cooldown=3600)
        # Same modal 10 min later — within cooldown → no second alert.
        cm.run(do_capture=lambda: MODAL_PANE, do_alert=self._alert, now=1600, cooldown=3600)
        self.assertEqual(len(self.alerts), 1, "must not re-alert within cooldown")

    def test_realerts_after_cooldown(self):
        cm.run(do_capture=lambda: MODAL_PANE, do_alert=self._alert, now=1000, cooldown=3600)
        cm.run(do_capture=lambda: MODAL_PANE, do_alert=self._alert, now=1000 + 3601, cooldown=3600)
        self.assertEqual(len(self.alerts), 2, "re-alerts once the cooldown elapses")

    def test_alert_failure_does_not_raise(self):
        def boom(msg):
            raise RuntimeError("telegram down")
        rc = cm.run(do_capture=lambda: MODAL_PANE, do_alert=boom, now=1000)
        self.assertEqual(rc, 0, "alert routing failure must never wedge the monitor")


# --------------------------------------------------------------------------- #
# T-2553: update / model-migration modal detection + ship-dark recovery.      #
# Fixtures mirror tools/test_codex_modal.py (real codex-cli captures /         #
# codex-rs insta snapshots), with a codex status header above as a live pane. #
# --------------------------------------------------------------------------- #

UPDATE_MODAL_PANE = "\n".join([
    "• STATUS: idle_ready",
    "",
    "  ✨ Update available! 0.136.0 -> 0.146.1",
    "",
    "  Release notes: https://github.com/openai/codex/releases/latest",
    "",
    "› 1. Update now (runs `npm install -g @openai/codex`)",
    "  2. Skip",
    "  3. Skip until next version",
    "",
    "  Press enter to continue",
])

UPDATE_MODAL_ON_SKIP = UPDATE_MODAL_PANE.replace(
    "› 1. Update now", "  1. Update now").replace(
    "  3. Skip until next version", "› 3. Skip until next version")

# Live-captured (T-2599 wet-run) markdown migration shape.
MIGRATION_MODAL_PANE = "\n".join([
    "",
    "  GPT-5.4 will be deprecated soon",
    "",
    "  Codex now uses GPT-5.6 Terra in place of GPT-5.4. Switch to GPT-5.6 Terra to continue.",
    "",
    "  Choose how you'd like Codex to proceed.",
    "",
    "› 1. Try new model",
    "  2. Use existing model",
    "",
    "  Use ↑/↓ to move, press enter to confirm",
])

# Update modal text only far above the live bottom region (lots of newer output).
UPDATE_IN_OLD_SCREEN_TEXT = UPDATE_MODAL_PANE + "\n" + "\n".join(
    f"• output line {i}" for i in range(30)) + "\n› Implement {feature}\n  gpt-5.5 default · ~"


class ClassifyT2553(unittest.TestCase):
    def test_update_modal(self):
        self.assertEqual(cm.classify_codex_pane(UPDATE_MODAL_PANE), cm.UPDATE_MODAL)

    def test_migration_modal(self):
        self.assertEqual(cm.classify_codex_pane(MIGRATION_MODAL_PANE), cm.MIGRATION_MODAL)

    def test_rate_limit_modal_still_model_switch(self):
        self.assertEqual(cm.classify_codex_pane(MODAL_PANE), cm.MODEL_SWITCH_MODAL)

    def test_idle_codex_is_none(self):
        self.assertIsNone(cm.classify_codex_pane(IDLE_WITH_WARNING))

    def test_update_needles_quoted_above_prompt_box_is_none(self):
        # Review repro: codex printed codex_modal.py source (both update needles) right
        # above its live prompt box + status line — well inside the 14-line region.
        pane = "\n".join([
            "• Here is the matcher:",
            '  _UPDATE_MODAL_NEEDLES = ("Update available!", "Skip until next version")',
            "  3. Skip until next version",
            "  Press enter to continue",
            "",
            "› Implement {feature}",
            "",
            "  ? for shortcuts                                    84% context left",
        ])
        self.assertTrue(cm.codex_modal.is_codex_update_modal(pane))  # needles DO match
        self.assertIsNone(cm.classify_codex_pane(pane))

    def test_migration_needle_quoted_above_prompt_box_is_none(self):
        pane = "\n".join([
            "• The migration prompt says \"Choose how you'd like Codex to proceed.\"",
            "  and its footer is: Use ↑/↓ to move, press enter to confirm",
            "",
            "› Implement {feature}",
            "",
            "  gpt-5.5 default · ~",
        ])
        self.assertTrue(cm.codex_modal.is_codex_model_migration_modal(pane))
        self.assertIsNone(cm.classify_codex_pane(pane))

    def test_modal_with_chrome_below_footer_is_none(self):
        # Even with the real modal text, a status line under it = chat view owns bottom.
        self.assertIsNone(cm.classify_codex_pane(
            MIGRATION_MODAL_PANE + "\n  ? for shortcuts   84% context left"))

    def test_update_modal_only_in_old_screen_text_is_none(self):
        self.assertIsNone(cm.classify_codex_pane(UPDATE_IN_OLD_SCREEN_TEXT))

    def test_rate_limit_wins_over_recoverable_when_ambiguous(self):
        # Defence in depth: a pane carrying both must resolve to the ALERT-ONLY kind.
        both = UPDATE_MODAL_PANE + "\n" + MODAL_PANE
        self.assertEqual(cm.classify_codex_pane(both), cm.MODEL_SWITCH_MODAL)

    def test_reuses_codex_modal_matchers(self):
        # The needles live in codex_modal only — stubbing them flips the classification.
        from unittest import mock
        with mock.patch.object(cm.codex_modal, "is_codex_update_modal", return_value=False):
            self.assertIsNone(cm.classify_codex_pane(UPDATE_MODAL_PANE))


class _Recorder:
    """Scripted capture + key/sleep/alert recorder sharing one event log."""

    def __init__(self, panes):
        self.panes = list(panes)
        self.events = []
        self.alerts = []

    def capture(self):
        pane = self.panes.pop(0) if len(self.panes) > 1 else self.panes[0]
        self.events.append(("capture", pane))
        return pane

    def send_key(self, key):
        self.events.append(("key", key))

    def sleep(self, s):
        self.events.append(("sleep", s))

    def alert(self, msg):
        self.alerts.append(msg)


class RunT2553(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self._settle = cm.RECOVER_SETTLE_SECONDS
        cm.RECOVER_SETTLE_SECONDS = 0
        self.addCleanup(lambda: setattr(cm, "RECOVER_SETTLE_SECONDS", self._settle))

    def _run(self, rec, **kw):
        # min_samples=1 isolates the recover/alert logic; persistence has its own tests.
        kw.setdefault("min_samples", 1)
        return cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
                      do_sleep=rec.sleep, now=kw.pop("now", 1000), **kw)

    def _keys(self, rec):
        return [e[1] for e in rec.events if e[0] == "key"]

    # ---- unarmed (default) ----
    def test_unarmed_update_alerts_and_never_dismisses(self):
        from unittest import mock
        rec = _Recorder([UPDATE_MODAL_PANE])
        with mock.patch.object(cm.codex_modal, "dismiss_codex_update_modal",
                               side_effect=AssertionError("unarmed must not dismiss")), \
             mock.patch.object(cm.codex_modal, "dismiss_codex_model_migration_modal",
                               side_effect=AssertionError("unarmed must not dismiss")):
            self.assertEqual(self._run(rec), 0)
            rec2 = _Recorder([MIGRATION_MODAL_PANE])
            self.assertEqual(self._run(rec2), 0)
        self.assertEqual(self._keys(rec) + self._keys(rec2), [])
        self.assertEqual(len(rec.alerts), 1)
        self.assertIn("DISARMED", rec.alerts[0])
        self.assertIn("WOULD have dismissed", rec.alerts[0])
        self.assertEqual(rec2.alerts, [cm._MIGRATION_ALERT_MSG])

    def test_default_run_is_unarmed(self):
        rec = _Recorder([UPDATE_MODAL_PANE])
        for now in (1000, 1300):
            cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
                   do_sleep=rec.sleep, now=now)
        self.assertEqual(self._keys(rec), [])
        self.assertIn("DISARMED", rec.alerts[0])

    # ---- persistence (>= 2 consecutive samples, default) ----
    def test_single_sample_armed_never_dismisses_or_alerts(self):
        from unittest import mock
        rec = _Recorder([UPDATE_MODAL_PANE])
        with mock.patch.object(cm, "recover", side_effect=AssertionError("1 sample")):
            cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
                   do_sleep=rec.sleep, now=1000, arm=True)
        self.assertEqual(self._keys(rec), [])
        self.assertEqual(rec.alerts, [])

    def test_second_consecutive_sample_armed_dismisses(self):
        rec = _Recorder([UPDATE_MODAL_PANE])
        cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
               do_sleep=rec.sleep, now=1000, arm=True)
        self.assertEqual(self._keys(rec), [])
        rec2 = _Recorder([UPDATE_MODAL_PANE, UPDATE_MODAL_PANE, UPDATE_MODAL_PANE,
                          UPDATE_MODAL_ON_SKIP, IDLE_WITH_WARNING])
        cm.run(do_capture=rec2.capture, do_alert=rec2.alert, do_send_key=rec2.send_key,
               do_sleep=rec2.sleep, now=1300, arm=True)
        self.assertEqual(self._keys(rec2), ["Down", "Enter"])

    def test_migration_single_sample_does_not_alert(self):
        rec = _Recorder([MIGRATION_MODAL_PANE])
        cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
               do_sleep=rec.sleep, now=1000, arm=True)
        self.assertEqual(rec.alerts, [])

    def test_interrupted_streak_resets(self):
        for pane, now in ((MIGRATION_MODAL_PANE, 1000), (IDLE_WITH_WARNING, 1300),
                          (MIGRATION_MODAL_PANE, 1600)):
            rec = _Recorder([pane])
            cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
                   do_sleep=rec.sleep, now=now, arm=True)
            self.assertEqual(self._keys(rec), [])
            self.assertEqual(rec.alerts, [])

    def test_next_streak_pure(self):
        s = cm.next_streak(None, cm.UPDATE_MODAL)
        self.assertEqual(s, {"kind": cm.UPDATE_MODAL, "streak": 1})
        s = cm.next_streak(s, cm.UPDATE_MODAL)
        self.assertEqual(s["streak"], 2)
        self.assertEqual(cm.next_streak(s, cm.MIGRATION_MODAL)["streak"], 1)
        self.assertEqual(cm.next_streak(s, cm.MODEL_SWITCH_MODAL)["streak"], 0)
        self.assertEqual(cm.next_streak(s, None)["streak"], 0)

    def test_in_progress_stamp_written_before_recover(self):
        from unittest import mock
        import json as _json
        seen = []

        def spy(kind, **kw):
            seen.append(_json.loads(cm._state_path(kind).read_text())["reason"])
            return "recovered", ""
        rec = _Recorder([UPDATE_MODAL_PANE])
        with mock.patch.object(cm, "recover", side_effect=spy):
            self._run(rec, arm=True)
        self.assertEqual(seen, ["update_modal:in-progress"])
        self.assertEqual(_json.loads(cm._state_path(cm.UPDATE_MODAL).read_text())["reason"],
                         "update_modal:recovered")

    def test_killed_mid_recovery_is_not_retried(self):
        from unittest import mock
        rec = _Recorder([UPDATE_MODAL_PANE])
        with mock.patch.object(cm, "recover", side_effect=KeyboardInterrupt):  # ~SIGTERM
            with self.assertRaises(KeyboardInterrupt):
                self._run(rec, arm=True, now=1000, cooldown=3600)
        with mock.patch.object(cm, "recover", side_effect=AssertionError("retried")) as r:
            self._run(rec, arm=True, now=1300, cooldown=3600)
        r.assert_not_called()

    def test_state_writes_are_atomic(self):
        from unittest import mock
        with mock.patch.object(cm.os, "replace", wraps=cm.os.replace) as rep:
            cm._stamp(cm._state_path(cm.UPDATE_MODAL), 1000)
        rep.assert_called_once()

    def test_unarmed_cooldown_per_kind(self):
        rec = _Recorder([UPDATE_MODAL_PANE])
        self._run(rec, now=1000, cooldown=3600)
        self._run(rec, now=1600, cooldown=3600)
        self.assertEqual(len(rec.alerts), 1)
        # A different kind has its own cooldown — the rate-limit alert still fires.
        rl = _Recorder([MODAL_PANE])
        self._run(rl, now=1700, cooldown=3600)
        self.assertEqual(len(rl.alerts), 1)

    # ---- armed ----
    def test_armed_update_recaptures_before_dismiss_and_recovers(self):
        # detect capture, live re-capture, dismiss captures (cursor on 1, then on 3), confirm.
        rec = _Recorder([UPDATE_MODAL_PANE, UPDATE_MODAL_PANE, UPDATE_MODAL_PANE,
                         UPDATE_MODAL_ON_SKIP, IDLE_WITH_WARNING])
        self.assertEqual(self._run(rec, arm=True), 0)
        kinds = [e[0] for e in rec.events]
        # Two captures (detect + live re-capture) happen before the first keystroke.
        self.assertEqual(kinds[:2], ["capture", "capture"])
        self.assertEqual(self._keys(rec), ["Down", "Enter"])
        self.assertEqual(len(rec.alerts), 1)
        self.assertIn("AUTO-RECOVERED", rec.alerts[0])

    def test_armed_migration_is_alert_only(self):
        # Recovery, T-2553 rev 3: Enter = "Try new model" = mid-run model switch (the operator's call).
        from unittest import mock
        rec = _Recorder([MIGRATION_MODAL_PANE])
        with mock.patch.object(cm, "recover", side_effect=AssertionError("never recover")):
            self._run(rec, arm=True)
        self.assertEqual(self._keys(rec), [])
        self.assertEqual(rec.alerts, [cm._MIGRATION_ALERT_MSG])
        self.assertIn("ALERT-ONLY", rec.alerts[0])
        self.assertIn("Use existing model", rec.alerts[0])  # the manual decline option
        self.assertIn("the operator", rec.alerts[0])

    def test_migration_alert_respects_cooldown(self):
        rec = _Recorder([MIGRATION_MODAL_PANE])
        self._run(rec, arm=True, now=1000, cooldown=3600)
        self._run(rec, arm=True, now=1300, cooldown=3600)
        self.assertEqual(len(rec.alerts), 1)
        self.assertEqual(self._keys(rec), [])

    def test_armed_changed_pane_aborts_dismiss(self):
        from unittest import mock
        rec = _Recorder([UPDATE_MODAL_PANE, IDLE_WITH_WARNING])
        with mock.patch.object(cm.codex_modal, "dismiss_codex_update_modal",
                               side_effect=AssertionError("changed pane must abort")) as d:
            self._run(rec, arm=True)
        d.assert_not_called()
        self.assertEqual(self._keys(rec), [])
        self.assertIn("ABORTED", rec.alerts[0])

    def test_armed_kind_changed_to_rate_limit_aborts(self):
        rec = _Recorder([UPDATE_MODAL_PANE, MODAL_PANE])
        self._run(rec, arm=True)
        self.assertEqual(self._keys(rec), [])
        self.assertIn("ABORTED", rec.alerts[0])

    def test_armed_unreadable_recapture_aborts(self):
        panes = iter([UPDATE_MODAL_PANE, None])
        rec = _Recorder([UPDATE_MODAL_PANE])
        rec.capture = lambda: next(panes)
        self._run(rec, arm=True)
        self.assertEqual(self._keys(rec), [])
        self.assertIn("ABORTED", rec.alerts[0])

    def test_armed_rate_limit_stays_alert_only(self):
        from unittest import mock
        rec = _Recorder([MODAL_PANE])
        with mock.patch.object(cm, "recover", side_effect=AssertionError("never recover")):
            self._run(rec, arm=True)
        self.assertEqual(self._keys(rec), [])
        self.assertEqual(rec.alerts, [cm._ALERT_MSG])

    def test_recover_refuses_rate_limit_kind(self):
        rec = _Recorder([MODAL_PANE])
        outcome, _ = cm.recover(cm.MODEL_SWITCH_MODAL, do_capture=rec.capture,
                                do_send_key=rec.send_key, do_sleep=rec.sleep)
        self.assertEqual(outcome, "aborted")
        self.assertEqual(rec.events, [])

    def test_recover_refuses_migration_kind_exactly_like_rate_limit(self):
        rec = _Recorder([MIGRATION_MODAL_PANE])
        outcome, detail = cm.recover(cm.MIGRATION_MODAL, do_capture=rec.capture,
                                     do_send_key=rec.send_key, do_sleep=rec.sleep)
        self.assertEqual((outcome, detail),
                         ("aborted", "kind 'migration_modal' is not auto-recoverable"))
        self.assertEqual(rec.events, [])
        self.assertEqual(cm.RECOVERABLE_KINDS, frozenset({cm.UPDATE_MODAL}))

    def test_monitor_never_references_the_migration_dismisser(self):
        with open(cm.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("dismiss_codex_model_migration_modal", src)

    def test_armed_navigation_failure_alerts_failed(self):
        # Cursor never reaches option 3 -> codex_modal raises -> FAILED alert, bounded keys.
        rec = _Recorder([UPDATE_MODAL_PANE])
        self._run(rec, arm=True)
        self.assertNotIn("Enter", self._keys(rec))
        self.assertLessEqual(len(self._keys(rec)), cm.codex_modal.MAX_MODAL_NAV_ATTEMPTS)
        self.assertIn("FAILED", rec.alerts[0])

    def test_armed_modal_persists_after_dismiss_alerts_failed(self):
        rec = _Recorder([UPDATE_MODAL_ON_SKIP])  # stays up forever
        self._run(rec, arm=True)
        self.assertEqual(self._keys(rec), ["Enter"])
        self.assertIn("FAILED", rec.alerts[0])
        self.assertIn("still on screen", rec.alerts[0])

    def test_armed_failure_not_retried_within_cooldown(self):
        rec = _Recorder([UPDATE_MODAL_ON_SKIP])
        self._run(rec, arm=True, now=1000, cooldown=3600)
        self._run(rec, arm=True, now=1300, cooldown=3600)
        self.assertEqual(self._keys(rec), ["Enter"], "no keystroke loop inside cooldown")
        self.assertEqual(len(rec.alerts), 1)

    def test_armed_send_key_error_is_failed_not_raised(self):
        rec = _Recorder([UPDATE_MODAL_ON_SKIP])

        def boom(key):
            raise RuntimeError("tmux gone")
        rc = cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=boom,
                    do_sleep=rec.sleep, now=1000, arm=True, min_samples=1)
        self.assertEqual(rc, 0)
        self.assertIn("FAILED", rec.alerts[0])

    def test_capture_raising_is_fail_open(self):
        def boom():
            raise OSError("sudo broke")
        self.assertEqual(cm.run(do_capture=boom, do_alert=lambda m: None, now=1000, arm=True), 0)

    def test_send_codex_key_refuses_non_whitelisted_keys(self):
        from unittest import mock
        with mock.patch.object(cm.subprocess, "run",
                               side_effect=AssertionError("must not shell out")):
            for key in ("Escape", "y", "Up", "C-c"):
                with self.assertRaises(ValueError):
                    cm.send_codex_key(key)


# --------------------------------------------------------------------------- #
# REAL-PANE specimens (Recovery's arming condition, T-2553 rev 2). tools/testdata/ #
# codex_*_pane.txt are exported byte-for-byte from the live captures already   #
# in tools/test_bringup_acceptance_harness_codex_ready.py: the update picker    #
# (+ post-dismiss composer) captured live 2026-08-05 against codex-cli 0.136.0  #
# (T-2572), and the model-migration prompt captured live 2026-08-08 against the #
# pinned codex 0.146.0 (T-2599 wet-run). Trailing pane-height padding trimmed,  #
# as in the source fixtures. NOT synthesized.                                   #
# --------------------------------------------------------------------------- #

_TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata")


def _real(name):
    with open(os.path.join(_TESTDATA, name), encoding="utf-8", newline="") as fh:
        return fh.read()


REAL_UPDATE_OPT1 = _real("codex_update_modal_cursor_opt1_pane.txt")
REAL_UPDATE_OPT2 = _real("codex_update_modal_cursor_opt2_pane.txt")
REAL_UPDATE_OPT3 = _real("codex_update_modal_cursor_opt3_pane.txt")
REAL_READY_AFTER_DISMISS = _real("codex_ready_composer_after_update_dismiss_pane.txt")
# T-4181 arming precondition: fresh live captures against codex-cli 0.147.0 (the fleet
# pin), 2026-09-28, throwaway pane on recovery's account with check_for_update_on_startup
# forced on at runtime. The picker is structurally identical to the 0.136.0 specimen
# (only the version line differs); the unauthenticated onboarding screen carries the
# SAME "Press enter to continue" footer the recoverable-footer anchor matches.
REAL_UPDATE_OPT1_0147 = _real("codex_update_modal_cursor_opt1_pane_0147.txt")
REAL_LOGIN_ONBOARDING_0147 = _real("codex_login_onboarding_pane_0147.txt")
REAL_MIGRATION = _real("codex_migration_modal_live_markdown_pane.txt")


class RealPaneSpecimens(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PANTHEON_STATE_DIR"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PANTHEON_STATE_DIR", None))
        self._settle = cm.RECOVER_SETTLE_SECONDS
        cm.RECOVER_SETTLE_SECONDS = 0
        self.addCleanup(lambda: setattr(cm, "RECOVER_SETTLE_SECONDS", self._settle))

    def _run(self, rec, now, **kw):
        return cm.run(do_capture=rec.capture, do_alert=rec.alert, do_send_key=rec.send_key,
                      do_sleep=rec.sleep, now=now, arm=True, **kw)

    @staticmethod
    def _keys(rec):
        return [e[1] for e in rec.events if e[0] == "key"]

    def test_specimens_carry_their_needles(self):
        # Guard against a specimen silently losing the tokens the assertions rely on.
        for pane in (REAL_UPDATE_OPT1, REAL_UPDATE_OPT2, REAL_UPDATE_OPT3):
            self.assertTrue(cm.codex_modal.is_codex_update_modal(pane))
        self.assertTrue(cm.codex_modal.is_codex_model_migration_modal(REAL_MIGRATION))
        self.assertTrue(cm.codex_modal.cursor_on_skip_until_next_version(REAL_UPDATE_OPT3))

    def test_classify_real_update_picker_all_cursor_positions(self):
        for pane in (REAL_UPDATE_OPT1, REAL_UPDATE_OPT2, REAL_UPDATE_OPT3):
            self.assertEqual(cm.classify_codex_pane(pane), cm.UPDATE_MODAL)

    def test_fresh_0147_update_picker_classifies(self):
        self.assertIn("0.147.0 -> ", REAL_UPDATE_OPT1_0147)
        self.assertTrue(cm.codex_modal.is_codex_update_modal(REAL_UPDATE_OPT1_0147))
        self.assertFalse(cm.codex_modal.cursor_on_skip_until_next_version(REAL_UPDATE_OPT1_0147))
        self.assertEqual(cm.classify_codex_pane(REAL_UPDATE_OPT1_0147), cm.UPDATE_MODAL)

    def test_login_onboarding_with_the_recoverable_footer_is_none(self):
        # Same footer as the picker, but it is NOT the update picker: armed recovery must
        # never navigate a sign-in menu.
        self.assertIn("Press enter to continue", REAL_LOGIN_ONBOARDING_0147.splitlines()[-1])
        self.assertIsNone(cm.classify_codex_pane(REAL_LOGIN_ONBOARDING_0147))

    def test_classify_real_migration_prompt(self):
        self.assertEqual(cm.classify_codex_pane(REAL_MIGRATION), cm.MIGRATION_MODAL)

    def test_real_post_dismiss_composer_is_none(self):
        # The composer still shows an "Update available!" BANNER (non-blocking) — it must
        # never classify as the picker: that is exactly the quoted-needle hazard, live.
        self.assertIsNone(cm.classify_codex_pane(REAL_READY_AFTER_DISMISS))

    def test_armed_update_recovery_against_real_panes(self):
        # detect (sample 2) -> live re-capture -> dismiss: opt1 Down, opt2 Down, opt3 Enter
        # -> confirm = real post-dismiss composer.
        first = _Recorder([REAL_UPDATE_OPT1])
        self._run(first, 1000)  # sample 1: persistence gate, no keys
        self.assertEqual(self._keys(first), [])
        rec = _Recorder([REAL_UPDATE_OPT1, REAL_UPDATE_OPT1, REAL_UPDATE_OPT1,
                         REAL_UPDATE_OPT2, REAL_UPDATE_OPT3, REAL_READY_AFTER_DISMISS])
        self._run(rec, 1300)
        kinds = [e[0] for e in rec.events]
        self.assertEqual(kinds[:2], ["capture", "capture"], "re-capture precedes any key")
        # The live re-capture is re-verified bottom-anchored (footer is the last line).
        self.assertEqual(rec.events[1][1].strip().splitlines()[-1].strip(),
                         "Press enter to continue")
        self.assertEqual(self._keys(rec), ["Down", "Down", "Enter"])
        self.assertIn("AUTO-RECOVERED", rec.alerts[0])

    def test_armed_real_migration_is_alert_only_zero_keys(self):
        from unittest import mock
        with mock.patch.object(cm, "recover", side_effect=AssertionError("never recover")):
            self._run(_Recorder([REAL_MIGRATION]), 1000)  # sample 1
            rec = _Recorder([REAL_MIGRATION])
            self._run(rec, 1300)  # sample 2: alert-only
        self.assertEqual(self._keys(rec), [])
        self.assertEqual(rec.alerts, [cm._MIGRATION_ALERT_MSG])

    def test_real_pane_changed_before_act_aborts(self):
        from unittest import mock
        self._run(_Recorder([REAL_UPDATE_OPT1]), 1000)
        rec = _Recorder([REAL_UPDATE_OPT1, REAL_READY_AFTER_DISMISS])
        with mock.patch.object(cm.codex_modal, "dismiss_codex_update_modal",
                               side_effect=AssertionError("changed pane must abort")) as d:
            self._run(rec, 1300)
        d.assert_not_called()
        self.assertEqual(self._keys(rec), [])
        self.assertIn("ABORTED", rec.alerts[0])

    def test_recover_refuses_real_migration_pane(self):
        rec = _Recorder([REAL_MIGRATION])
        outcome, _ = cm.recover(cm.MIGRATION_MODAL, do_capture=rec.capture,
                                do_send_key=rec.send_key, do_sleep=rec.sleep)
        self.assertEqual(outcome, "aborted")
        self.assertEqual(rec.events, [], "no capture, no key")

class ArmFlag(unittest.TestCase):
    def test_env_arms(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {cm.ARM_ENV: "1"}), \
             mock.patch.object(cm, "run", return_value=0) as r:
            cm.main([])
        self.assertTrue(r.call_args.kwargs["arm"])

    def test_default_unarmed(self):
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != cm.ARM_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(cm, "run", return_value=0) as r:
            cm.main([])
        self.assertFalse(r.call_args.kwargs["arm"])

    def test_cli_arms(self):
        from unittest import mock
        with mock.patch.object(cm, "run", return_value=0) as r:
            cm.main(["--arm"])
        self.assertTrue(r.call_args.kwargs["arm"])


if __name__ == "__main__":
    unittest.main()
