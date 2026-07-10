"""Tests for codex_modal_monitor.py (T-1366) — detect codex wedged on the rate-limit
model-switch modal + alert Recovery. Fixtures are the REAL 2026-06-28 codex pane captures."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codex_modal_monitor as cm  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
