"""ORG-103 L7 (T-5307): the blocking-dialog policy for app-server codex seats.

Fixtures are the real codex 0.147.0 screens captured in the T-5307 probe (an
app-server plus a --remote TUI in a throwaway CODEX_HOME), not hand-written shapes.
The probe's governing result, that a TUI dialog does not block app-server turns,
is recorded on T-5307; these tests cover the two policy halves that follow from it:
the supervisor's config gate and the paste fallback's refusal to type into a dialog.

Run: python3 -m unittest tools.test_codex_dialog_policy
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "harness" / "codex"))
sys.path.insert(0, str(REPO / "tools"))

from codex_telegram import appserver_route as ar  # noqa: E402
import appserver_supervise as sup  # noqa: E402
import codex_modal  # noqa: E402

UPDATE_DIALOG = """  ✨ Update available! 0.147.0 -> 0.164.0
  Release notes: https://github.com/openai/codex/releases/latest

› 1. Update now (runs `npm install -g @openai/codex`)
  2. Skip
  3. Skip until next version

  Press enter to continue
"""
TRUST_DIALOG = """> You are in /run/user/1001/t5307/work2

  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection. Trusting the directory allows
  project-local config, hooks, and exec policies to load.

› 1. Yes, continue
  2. No, quit

  Press enter to continue
"""
IDLE_REMOTE_TUI = """╭─────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.147.0)                      │
│                                                 │
│ model:       gpt-5.6-sol low   /model to change │
│ directory:   /run/user/1001/t5307/work          │
│ permissions: YOLO mode                          │
╰─────────────────────────────────────────────────╯
  Tip: Type / to open the command popup; Tab autocompletes slash commands.
› Reply with exactly the word PONG and nothing else.
• PONG
• You have 2 usage limit resets available. Run /usage to use one.
› Write tests for @filename
  gpt-5.6-sol low · /run/user/1001/t5307/work
"""
WRAPPED = "[inbound Telegram DM from the operator | chat_id=1 | message_id=7] hello"
down = lambda *_a, **_k: (False, "writer down")   # noqa: E731
ok = lambda *_a, **_k: (True, "ok")               # noqa: E731


class PaneGuardTests(unittest.TestCase):
    def test_real_dialogs_block(self):
        self.assertEqual(ar.pane_blocks_paste(UPDATE_DIALOG), "update dialog")
        self.assertEqual(ar.pane_blocks_paste(TRUST_DIALOG), "selection dialog")

    def test_migration_and_confirm_shapes_block(self):
        for needle in codex_modal._MODEL_MIGRATION_MODAL_NEEDLES:
            self.assertEqual(ar.pane_blocks_paste(f"x\n{needle}\ny"), "model-migration dialog")
        self.assertEqual(ar.pane_blocks_paste("Enter to confirm · Esc to cancel"), "confirm dialog")

    def test_idle_remote_tui_and_unreadable_panes_do_not_block(self):
        self.assertIsNone(ar.pane_blocks_paste(IDLE_REMOTE_TUI))
        self.assertIsNone(ar.pane_blocks_paste(""))
        self.assertIsNone(ar.pane_blocks_paste(None))

    def test_needles_stay_in_sync_with_codex_modal(self):
        table = dict(ar.DIALOG_NEEDLES)
        self.assertEqual(tuple(table["update dialog"]), tuple(codex_modal._UPDATE_MODAL_NEEDLES))
        self.assertEqual(tuple(table["model-migration dialog"]),
                         tuple(codex_modal._MODEL_MIGRATION_MODAL_NEEDLES))

    def test_codex_modal_agrees_on_the_real_update_capture(self):
        self.assertTrue(codex_modal.is_codex_update_modal(UPDATE_DIALOG))


class FallbackRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        sw = self.home / ar.SWITCH_REL
        sw.parent.mkdir(parents=True)
        sw.write_text(json.dumps({"enabled": True, "seat": "consultant",
                                  "state_dir": "~/state", "socket": str(self.home / "s")}))
        self.pasted, self.alerts = [], []
        p = mock.patch.object(ar, "alert_recovery", lambda s, b: self.alerts.append((s, b)) or True)
        p.start()
        self.addCleanup(p.stop)

    def deliver(self, pane, writer=down):
        return ar.deliver(WRAPPED, "operator_dm", lambda t: self.pasted.append(t) or True,
                          injector="test", home=self.home, writer_check=writer, socket_check=ok,
                          pane_capture=lambda: pane)

    def outcomes(self):
        return [json.loads(x) for x in (self.home / "state" / "inject-outcomes.jsonl")
                .read_text().splitlines()]

    def test_fallback_never_types_into_the_update_dialog(self):
        self.assertFalse(self.deliver(UPDATE_DIALOG))
        self.assertEqual(self.pasted, [])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("paste REFUSED", self.alerts[0][1])
        self.assertIn("update dialog", self.alerts[0][1])
        self.assertEqual(self.outcomes()[-1]["pasted"], False)

    def test_fallback_never_types_into_the_trust_dialog(self):
        self.assertFalse(self.deliver(TRUST_DIALOG))
        self.assertEqual(self.pasted, [])

    def test_fallback_pastes_into_an_idle_pane(self):
        self.assertTrue(self.deliver(IDLE_REMOTE_TUI))
        self.assertEqual(self.pasted, [WRAPPED])
        self.assertNotIn("REFUSED", self.alerts[0][1])

    def test_route_up_never_reads_the_pane(self):
        capture = mock.Mock(return_value=UPDATE_DIALOG)
        self.assertTrue(ar.deliver(WRAPPED, "operator_dm", lambda t: False, injector="t",
                                   home=self.home, writer_check=ok, socket_check=ok,
                                   pane_capture=capture))
        capture.assert_not_called()

    def test_capture_raising_is_not_a_dialog(self):
        def boom():
            raise OSError("tmux gone")
        self.assertTrue(ar.deliver(WRAPPED, "operator_dm", lambda t: True, injector="t",
                                   home=self.home, writer_check=down, socket_check=ok,
                                   pane_capture=boom))

    def test_poller_passes_its_pane_capture(self):
        from codex_telegram import poller
        with mock.patch.object(poller, "_capture_pane", return_value=UPDATE_DIALOG), \
                mock.patch.object(poller, "inject") as inj, \
                mock.patch.object(ar, "_home", return_value=self.home), \
                mock.patch.object(ar, "writer_alive", down):
            self.assertFalse(poller.inject_routed("codex-session", WRAPPED))
        inj.assert_not_called()

    def test_keepgoing_passes_its_pane_capture(self):
        from codex_telegram import keepgoing
        with mock.patch.object(keepgoing, "_capture_pane", return_value=TRUST_DIALOG), \
                mock.patch.object(keepgoing, "inject") as inj, \
                mock.patch.object(ar, "_home", return_value=self.home), \
                mock.patch.object(ar, "writer_alive", down):
            self.assertFalse(keepgoing.routed_injector("nudge")("codex-session", "go"))
        inj.assert_not_called()


class SupervisorDialogConfigTests(unittest.TestCase):
    GOOD = ('check_for_update_on_startup = false\n[notice]\n'
            'hide_rate_limit_model_nudge = true\nhide_full_access_warning = true\n')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = Path(self._tmp.name)

    def check(self, text):
        if text is not None:
            (self.h / "config.toml").write_text(text)
        return sup.check_dialog_config(self.h)

    def test_compliant_config(self):
        self.assertEqual(self.check(self.GOOD), ([], []))

    def test_update_check_on_or_missing_is_an_error(self):
        for text in (self.GOOD.replace("false", "true", 1),
                     self.GOOD.replace("check_for_update_on_startup = false\n", "")):
            errors, _ = self.check(text)
            self.assertTrue(errors and "check_for_update_on_startup" in errors[0], text)

    def test_missing_notice_hides_are_warnings_only(self):
        errors, warnings = self.check("check_for_update_on_startup = false\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)

    def test_missing_or_broken_config_is_an_error(self):
        self.assertTrue(self.check(None)[0])
        self.assertTrue(self.check("not = [toml")[0])

    def test_consultant_live_shape_passes_the_required_check(self):
        # consultant's live config (2026-10-23) sets the update check off; its [notice] has
        # only model_migrations, so the two hides are warnings until L6 adds them.
        errors, warnings = self.check('check_for_update_on_startup = false\n'
                                      '[notice.model_migrations]\n"gpt-5.4" = "gpt-5.6-terra"\n')
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)

    def test_supervisor_refuses_a_bound_seat_with_the_update_check_on(self):
        (self.h / "config.toml").write_text(self.GOOD.replace("false", "true", 1))
        a = sup.parse_args(["--rundir", str(self.h / "rd"), "--state-dir", str(self.h / "st"),
                            "--codex-home", str(self.h), "--model", "m", "--poll", "0",
                            "--account", "codex"])
        pin = sys.modules["codex_schema_contract"].load_bindings()["consultant"]["codex_version"]
        run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, f"codex-cli {pin}", "")  # noqa
        s = sup.Supervisor(a, run=run, popen=lambda *x, **k: self.fail("started"))
        self.assertEqual(s.run(), sup.EXIT_REFUSED)
        self.assertIn("check_for_update_on_startup", s.check_pin())


if __name__ == "__main__":
    unittest.main()
