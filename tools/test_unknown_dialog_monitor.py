#!/usr/bin/env python3
"""Hermetic tests for unknown_dialog_monitor.py (T-2553): the pure bottom-dialog classifier,
the persistence/cooldown step, the detect-only fleet pass, and the ship-dark main(). No
live tmux, no sudo, no keystrokes. Claude-pane fixtures below are SYNTHESIZED (labelled
per fixture); the cap-modal and Bash/Read permission-modal specimens are the REAL captures
in tools/testdata/."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from tools import unknown_dialog_monitor as m
    from tools import cap_modal_reaper as reaper
except ImportError:  # bare run with cwd=tools/
    import unknown_dialog_monitor as m  # type: ignore
    import cap_modal_reaper as reaper  # type: ignore

_TESTDATA = Path(__file__).resolve().parent / "testdata"


def _specimen(name: str) -> str:
    return (_TESTDATA / name).read_text(encoding="utf-8")


# SYNTHESIZED: a claude seat parked on a dialog nothing in the fleet handles.
UNKNOWN_DIALOG_PANE = (
    "● Done. Pushed agent/T-9999.\n"
    "\n"
    "────────────────────────────────────────────────────────────────\n"
    " A new version of Claude Code is ready\n"
    "\n"
    " Restart now to finish updating?\n"
    " ❯ 1. Yes, restart now\n"
    "   2. Later\n"
    "\n"
    " Enter to confirm · Esc to cancel\n"
)
# SYNTHESIZED: normal idle claude seat — input box + status line at the bottom.
IDLE_PROMPT_PANE = (
    "● Done.\n"
    "────────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
)
# SYNTHESIZED: the same dialog text only in scrollback, live prompt drawn below it.
DIALOG_IN_SCROLLBACK_PANE = UNKNOWN_DIALOG_PANE + "\n● Continuing work…\n" + IDLE_PROMPT_PANE
# SYNTHESIZED: Edit/Write permission prompt — not the "Do you want to proceed?" shape that
# activity_monitor.classify_permission_modal keys on, but the same "Tab to amend" footer.
EDIT_PERMISSION_PANE = (
    "● Update(tools/foo.py)\n"
    "────────────────────────────────────────────────────────────────\n"
    " Edit file\n"
    " tools/foo.py\n"
    "   12 -    return None\n"
    "   12 +    return 0\n"
    " Do you want to make this edit to foo.py?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, allow all edits during this session (shift+tab)\n"
    "   3. No, and tell Claude what to do differently (esc)\n"
    "\n"
    " Esc to cancel · Tab to amend\n"
)
# SYNTHESIZED: WebFetch permission prompt.
WEBFETCH_PERMISSION_PANE = (
    "● Fetch(https://example.com/docs)\n"
    "────────────────────────────────────────────────────────────────\n"
    " Fetch\n"
    "   https://example.com/docs\n"
    "   Claude wants to fetch content from example.com\n"
    " Do you want to allow Claude to fetch this content?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and don't ask again for example.com\n"
    "   3. No, and tell Claude what to do differently (esc)\n"
    "\n"
    " Esc to cancel · Tab to amend\n"
)
# The reaper's own spend-limit modal fixture (live-modal shape from test_cap_modal_reaper).
SPEND_LIMIT_MODAL = (
    "  Ran 1 shell command\n"
    "  ⎿  You've hit your monthly spend limit · your weekly limit resets Sep 27, 5pm\n"
    "     (UTC)\n"
    "   What do you want to do?                        Usage credit balance: $8.08\n"
    "   ❯ Adjust monthly spend limit:        ← or → to adjust · Del to remove lim…\n"
    "     Wait for limit to reset                         Resets Sep 27, 5pm (UTC)\n"
    "   Enter to confirm · Esc to cancel"
)


class TestClassify(unittest.TestCase):
    def test_unknown_confirm_dialog(self):
        self.assertEqual(m.classify_claude_bottom_dialog(UNKNOWN_DIALOG_PANE), m.DIALOG_UNKNOWN)

    def test_real_cap_modal_specimen_is_known(self):
        self.assertEqual(m.classify_claude_bottom_dialog(_specimen("orchestrator_cap_modal_pane.txt")),
                         m.DIALOG_CAP_MODAL)

    def test_spend_limit_modal_is_known(self):
        self.assertEqual(m.classify_claude_bottom_dialog(SPEND_LIMIT_MODAL), m.DIALOG_CAP_MODAL)

    def test_real_permission_specimen_is_known(self):
        self.assertEqual(
            m.classify_claude_bottom_dialog(_specimen("artisan_permission_modal_pane.txt")),
            m.DIALOG_PERMISSION_MODAL)

    def test_edit_permission_prompt_is_permission_family(self):
        self.assertEqual(m.classify_claude_bottom_dialog(EDIT_PERMISSION_PANE),
                         m.DIALOG_PERMISSION_MODAL)

    def test_webfetch_permission_prompt_is_permission_family(self):
        self.assertEqual(m.classify_claude_bottom_dialog(WEBFETCH_PERMISSION_PANE),
                         m.DIALOG_PERMISSION_MODAL)

    def test_idle_prompt_is_none(self):
        self.assertIsNone(m.classify_claude_bottom_dialog(IDLE_PROMPT_PANE))

    def test_dialog_only_in_scrollback_is_none(self):
        self.assertIsNone(m.classify_claude_bottom_dialog(DIALOG_IN_SCROLLBACK_PANE))

    def test_empty_is_none(self):
        self.assertIsNone(m.classify_claude_bottom_dialog(None))
        self.assertIsNone(m.classify_claude_bottom_dialog("\n \n"))

    def test_old_known_modal_in_scrollback_does_not_mask_new_unknown(self):
        pane = (_specimen("artisan_permission_modal_pane.txt") + "\n"
                + "\n".join(f"output {i}" for i in range(30)) + "\n" + UNKNOWN_DIALOG_PANE)
        self.assertEqual(m.classify_claude_bottom_dialog(pane), m.DIALOG_UNKNOWN)

    def test_message_gives_no_blind_answer_advice(self):
        msg = m.unknown_dialog_message("specialist", UNKNOWN_DIALOG_PANE, 2)
        self.assertNotIn("if it is safe", msg)
        self.assertIn("do not answer blindly", msg)
        self.assertIn("Restart now", msg)


class TestStep(unittest.TestCase):
    def test_first_sample_does_not_alert(self):
        entry, alert = m.unknown_dialog_step(None, m.DIALOG_UNKNOWN, 1000, cooldown=3600)
        self.assertEqual(entry["streak"], 1)
        self.assertFalse(alert)

    def test_second_consecutive_sample_alerts(self):
        e1, _ = m.unknown_dialog_step(None, m.DIALOG_UNKNOWN, 1000, cooldown=3600)
        e2, alert = m.unknown_dialog_step(e1, m.DIALOG_UNKNOWN, 1300, cooldown=3600)
        self.assertTrue(alert)
        self.assertEqual(e2["last_alert"], 1300)

    def test_non_unknown_resets_streak(self):
        e1, _ = m.unknown_dialog_step(None, m.DIALOG_UNKNOWN, 1000)
        for kind in (None, m.DIALOG_CAP_MODAL, m.DIALOG_PERMISSION_MODAL):
            e2, alert = m.unknown_dialog_step(e1, kind, 1300)
            self.assertEqual(e2["streak"], 0)
            self.assertFalse(alert)

    def test_cooldown_holds_across_flicker(self):
        e, _ = m.unknown_dialog_step(None, m.DIALOG_UNKNOWN, 1000, cooldown=3600)
        e, a1 = m.unknown_dialog_step(e, m.DIALOG_UNKNOWN, 1300, cooldown=3600)
        e, _ = m.unknown_dialog_step(e, None, 1600, cooldown=3600)
        e, _ = m.unknown_dialog_step(e, m.DIALOG_UNKNOWN, 1900, cooldown=3600)
        e, a2 = m.unknown_dialog_step(e, m.DIALOG_UNKNOWN, 2200, cooldown=3600)
        self.assertTrue(a1)
        self.assertFalse(a2, "per-seat cooldown must hold")
        e, a3 = m.unknown_dialog_step(e, m.DIALOG_UNKNOWN, 1300 + 3600, cooldown=3600)
        self.assertTrue(a3, "re-alerts once the cooldown elapses")


class TestRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sp = Path(self._tmp.name) / "unk.json"
        self.alerts = []
        self.perm_alerts = []
        self.captured = []

    def _run(self, panes: dict, now: int, **kw):
        def cap(p, a, t):
            self.captured.append(p)
            return panes[p]
        kw.setdefault("resolve_target", lambda p: ("acct-" + p, f"{p}:0"))
        return m.run(tuple(panes), now=now, capture_pane=cap,
                     do_alert=lambda p, msg: self.alerts.append((p, msg)),
                     do_permission_alert=lambda p, msg: self.perm_alerts.append((p, msg)),
                     state_path=self.sp, log=lambda *a, **k: None, **kw)

    def test_persistent_unknown_alerts_once_on_second_sample(self):
        panes = {"specialist": UNKNOWN_DIALOG_PANE, "orchestrator": IDLE_PROMPT_PANE}
        self._run(panes, 1000)
        self.assertEqual(self.alerts, [])
        res = self._run(panes, 1300)
        self.assertEqual([p for p, _ in self.alerts], ["specialist"])
        self.assertTrue(res["specialist"].endswith(":alerted"))
        self.assertEqual(res["orchestrator"], "none")
        self._run(panes, 1600)
        self.assertEqual(len(self.alerts), 1, "cooldown suppresses repeat")

    def test_transient_dialog_never_alerts(self):
        self._run({"specialist": UNKNOWN_DIALOG_PANE}, 1000)
        self._run({"specialist": IDLE_PROMPT_PANE}, 1300)
        self._run({"specialist": UNKNOWN_DIALOG_PANE}, 1600)
        self.assertEqual(self.alerts, [])

    def test_known_dialogs_never_alert(self):
        # T-4183: a permission prompt alerts only after DEFAULT_PERMISSION_MIN_SAMPLES (3)
        # consecutive samples (TestPermissionPrompt); below that no known kind alerts.
        panes = {"artisan": _specimen("artisan_permission_modal_pane.txt"),
                 "orchestrator": _specimen("orchestrator_cap_modal_pane.txt"),
                 "mason": EDIT_PERMISSION_PANE, "specialist": WEBFETCH_PERMISSION_PANE}
        for now in (1000, 1300):
            self._run(panes, now)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.perm_alerts, [])

    def test_cap_modal_never_alerts_however_long(self):
        for now in (1000, 1300, 1600, 1900):
            self._run({"orchestrator": _specimen("orchestrator_cap_modal_pane.txt")}, now)
        self.assertEqual((self.alerts, self.perm_alerts), ([], []))

    def test_recoveries_own_pane_is_never_captured(self):
        for now in (1000, 1300):
            res = self._run({"recovery": UNKNOWN_DIALOG_PANE, "specialist": UNKNOWN_DIALOG_PANE}, now)
        self.assertEqual(res["recovery"], "skip:self-excluded")
        self.assertNotIn("recovery", self.captured)
        self.assertEqual([p for p, _ in self.alerts], ["specialist"])

    def test_runner_account_is_never_captured(self):
        me = m.getpass.getuser()
        res = self._run({"specialist": UNKNOWN_DIALOG_PANE}, 1000,
                        resolve_target=lambda p: (me, "x:0"))
        self.assertEqual(res["specialist"], "skip:self-account")
        self.assertEqual(self.captured, [])

    def test_default_fleet_excludes_recovery(self):
        with mock.patch.object(m, "_fleet_personas", return_value=("recovery", "specialist")):
            res = m.run(None, now=1000, capture_pane=lambda *a: IDLE_PROMPT_PANE,
                        resolve_target=lambda p: ("acct", "p:0"), do_alert=lambda p, x: None,
                        state_path=self.sp, log=lambda *a, **k: None)
        self.assertEqual(res, {"recovery": "skip:self-excluded", "specialist": "none"})

    def test_unreadable_pane_and_alert_failure_fail_open(self):
        def boom(p, msg):
            raise RuntimeError("recovery pane down")
        panes = {"specialist": UNKNOWN_DIALOG_PANE, "mason": None}
        for now in (1000, 1300):
            res = m.run(tuple(panes), now=now, capture_pane=lambda p, a, t: panes[p],
                        resolve_target=lambda p: ("acct-" + p, f"{p}:0"), do_alert=boom,
                        state_path=self.sp, log=lambda *a, **k: None)
        self.assertIn("alert-failed", res["specialist"])
        self.assertEqual(res["mason"], "unreadable")

    def test_no_target_skips(self):
        res = self._run({"ghost": None}, 1000, resolve_target=lambda p: (None, None))
        self.assertEqual(res["ghost"], "skip:no-pane-target")
        self.assertEqual(self.captured, [])

    def test_never_sends_keys(self):
        with mock.patch.object(reaper, "_send_escape",
                               side_effect=AssertionError("must never send keys")), \
             mock.patch.object(reaper.subprocess, "run",
                               side_effect=AssertionError("no shell-out when injected")):
            for now in (1000, 1300):
                self._run({"specialist": UNKNOWN_DIALOG_PANE}, now)
        self.assertEqual(len(self.alerts), 1)

    def test_module_has_no_send_keys_code_path(self):
        src = Path(m.__file__).read_text(encoding="utf-8")
        for needle in ("send-keys", "send_escape", "send_keys", "import subprocess"):
            self.assertNotIn(needle, src, f"forbidden token {needle!r} in module source")

    def test_default_capture_is_reaper_read_only_capture(self):
        with mock.patch.object(reaper, "_capture_pane", return_value="x") as cap:
            self.assertEqual(m._capture_pane("specialist", "claude-research", "p:0"), "x")
        cap.assert_called_once_with("specialist", "claude-research", "p:0")

    def test_state_write_is_atomic(self):
        with mock.patch.object(m.os, "replace", wraps=m.os.replace) as rep:
            m._save_json(self.sp, {"specialist": {"streak": 1, "last_alert": 0}})
        rep.assert_called_once()
        self.assertEqual(m._load_json(self.sp)["specialist"]["streak"], 1)
        self.assertEqual([p.name for p in self.sp.parent.iterdir()], ["unk.json"])

    def test_seat_target_falls_back_to_activity_monitor_pane(self):
        with mock.patch.object(m, "_instance_binding", return_value={"account": "suborch1"}):
            self.assertEqual(m.seat_pane_target("mason"), ("suborch1", "suborch1-main:0"))
        with mock.patch.object(m, "_instance_binding",
                               return_value={"account": "claude", "restart_pane": "x:0"}):
            self.assertEqual(m.seat_pane_target("orchestrator"), ("claude", "x:0"))


class TestMain(unittest.TestCase):
    def _env(self, **kv):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PANTHEON_UNKNOWN_DIALOG")}
        env.update(kv)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_default_off_is_noop(self):
        with self._env(), mock.patch.object(m, "run") as run_:
            self.assertEqual(m.main([]), 0)
        run_.assert_not_called()

    def test_enabled_runs(self):
        with self._env(PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED="1"), \
             mock.patch.object(m, "run") as run_:
            self.assertEqual(m.main([]), 0)
        run_.assert_called_once()
        self.assertEqual(run_.call_args.kwargs["min_samples"], 2)

    def test_bad_env_knob_fails_open(self):
        with self._env(PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED="1",
                       PANTHEON_UNKNOWN_DIALOG_MIN_SAMPLES="two"), \
             mock.patch.object(m, "run") as run_:
            self.assertEqual(m.main([]), 0)
        run_.assert_not_called()

    def test_run_exception_fails_open(self):
        with self._env(PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED="1"), \
             mock.patch.object(m, "run", side_effect=RuntimeError("x")):
            self.assertEqual(m.main([]), 0)

    def test_sibling_import_failure_fails_open(self):
        with self._env(PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED="1"), \
             mock.patch.object(m, "_reaper", side_effect=ImportError("broken")):
            self.assertEqual(m.main([]), 0)


class TestUnitFiles(unittest.TestCase):
    UNIT_DIR = Path(__file__).resolve().parent / "systemd"

    def test_service_shape(self):
        svc = (self.UNIT_DIR / "pantheon-unknown-dialog-monitor.service").read_text()
        self.assertIn("Type=oneshot", svc)
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", svc)
        self.assertIn("tools/unknown_dialog_monitor.py", svc)
        self.assertNotIn("PANTHEON_TELEGRAM_ENV", svc)
        # Ship-dark: the template itself must not arm the monitor.
        self.assertNotIn("PANTHEON_UNKNOWN_DIALOG_MONITOR_ENABLED=1", svc.split("[Service]")[1])

    def test_timer_shape(self):
        tmr = (self.UNIT_DIR / "pantheon-unknown-dialog-monitor.timer").read_text()
        self.assertIn("OnUnitActiveSec=5min", tmr)
        self.assertIn("Unit=pantheon-unknown-dialog-monitor.service", tmr)


class TestPermissionPrompt(TestRun):
    """T-4183: a seat parked on the SAME tool-permission prompt is reported after 3 runs."""

    def test_same_prompt_alerts_on_third_consecutive_run_then_cools_down(self):
        pane = _specimen("artisan_permission_modal_pane.txt")
        res = [self._run({"artisan": pane}, now)["artisan"] for now in (1000, 1300, 1600)]
        self.assertEqual(res[:2], ["permission_modal:streak=1", "permission_modal:streak=2"])
        self.assertEqual(res[2], "permission_modal:streak=3:alerted")
        [(persona, msg)] = self.perm_alerts
        self.assertEqual(persona, "artisan")
        self.assertIn("Do you want to proceed?", msg)
        self.assertIn("one-time", msg)
        self.assertEqual(self.alerts, [], "not reported as an UNKNOWN dialog")
        self._run({"artisan": pane}, 1900)
        self.assertEqual(len(self.perm_alerts), 1, "same-prompt cooldown")

    def test_a_different_prompt_restarts_the_count(self):
        a, b = EDIT_PERMISSION_PANE, WEBFETCH_PERMISSION_PANE
        for now, pane in ((1000, a), (1300, a), (1600, b), (1900, b)):
            self._run({"mason": pane}, now)
        self.assertEqual(self.perm_alerts, [])
        self._run({"mason": b}, 2200)
        self.assertEqual(len(self.perm_alerts), 1)

    def test_a_different_prompt_alerts_inside_the_cooldown(self):
        """T-4829: the cooldown is per (seat, prompt). Orchestrator 10-09: an earlier prompt's
        alert held a per-seat cooldown, so a new prompt parked 22:57-23:20Z unseen."""
        a, b = EDIT_PERMISSION_PANE, WEBFETCH_PERMISSION_PANE
        for now in (1000, 1300, 1600):
            self._run({"mason": a}, now)
        self.assertEqual(len(self.perm_alerts), 1)
        for now in (1900, 2200, 2500):              # well inside the 3h cooldown
            self._run({"mason": b}, now)
        self.assertEqual(len(self.perm_alerts), 2, "a new prompt alerts at its own threshold")
        for now in (2800, 3100, 3400):              # prompt A again: still cooling down
            self._run({"mason": a}, now)
        self.assertEqual(len(self.perm_alerts), 2, "the SAME prompt keeps its cooldown")

    def test_legacy_per_seat_state_still_cools_its_prompt(self):
        # State written before T-4829 has only perm_fp + perm_last_alert.
        pane = _specimen("artisan_permission_modal_pane.txt")
        fp = m._permission_fingerprint(pane)
        _, alert = m.permission_prompt_step(
            {"perm_streak": 2, "perm_fp": fp, "perm_last_alert": 1000},
            m.DIALOG_PERMISSION_MODAL, fp, 1300)
        self.assertFalse(alert)

    def test_an_answered_prompt_resets(self):
        pane = _specimen("artisan_permission_modal_pane.txt")
        for now, p in ((1000, pane), (1300, pane), (1600, IDLE_PROMPT_PANE), (1900, pane)):
            self._run({"artisan": p}, now)
        self.assertEqual(self.perm_alerts, [])

    def test_unknown_dialog_state_survives_alongside(self):
        self._run({"specialist": UNKNOWN_DIALOG_PANE}, 1000)
        self._run({"specialist": UNKNOWN_DIALOG_PANE}, 1300)
        self.assertEqual(len(self.alerts), 1)
        state = m._load_json(self.sp)["specialist"]
        self.assertEqual(state["streak"], 2)
        self.assertEqual(state["perm_streak"], 0)


if __name__ == "__main__":
    unittest.main()
