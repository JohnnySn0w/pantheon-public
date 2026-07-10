#!/usr/bin/env python3
"""Tests for tools/channel_liveness_monitor.py (T-0894 detect+alert probe).

The probe's two subprocess calls (`cat bot.pid`, then `ps -o args= -p <pid>`) are
mocked at the `_run` boundary; the alert router is mocked at
`tools.inbox_consumer.route_failure_alert`.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools import channel_liveness_monitor as clm

AGENT = "orchestrator"  # a real AGENT_CONFIG key


class ProbeBridgeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = clm.AGENT_CONFIG[AGENT]

    def _probe(self, run_results):
        with mock.patch.object(clm, "_run", side_effect=run_results):
            return clm.probe_bridge(AGENT, self.cfg)

    # Real bridge fixture (ground truth 2026-07-07): argv is just `bun server.ts`;
    # the identifying signal is the process cwd under the plugin cache (T-1606).
    BRIDGE_CWD = "/home/claude/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6"

    def test_up_when_pid_live_bun_server(self):
        status, _ = self._probe([
            (0, "12345"),
            (0, "/home/claude/.bun/bin/bun server.ts"),
            (0, self.BRIDGE_CWD),
        ])
        self.assertEqual(status, "up")

    def test_down_when_unrelated_bun_server_recycled_pid(self):
        # T-1606 / S3STAR-2026-07-07-002 regression: an UNRELATED `bun server.ts`
        # (e.g. echo-recorder) on a recycled PID must read DOWN, not up — its cwd
        # is not the telegram plugin cache.
        status, detail = self._probe([
            (0, "12345"),
            (0, "/home/claude/.bun/bin/bun server.ts"),
            (0, "/home/claude/echo-prismatic/recorder"),
        ])
        self.assertEqual(status, "down")
        self.assertIn("stale-PID recycling", detail)

    def test_inconclusive_when_cwd_probe_fails(self):
        # cwd probe subprocess failure -> fail-open (probe failure != bridge failure).
        status, _ = self._probe([
            (0, "12345"),
            (0, "/home/claude/.bun/bin/bun server.ts"),
            (-1, ""),
        ])
        self.assertEqual(status, "inconclusive")

    def test_up_via_parent_cwd_when_proc_cwd_unreadable(self):
        # orchestrator's sg-spawned bridge is non-dumpable: /proc cwd denied even to its
        # owner. Fallback = PPID ancestry; the plugin's `bun run --cwd <cache>`
        # wrapper argv carries the identifying path (ground truth 2026-07-07).
        status, detail = self._probe([
            (0, "12345"),
            (0, "/home/claude/.bun/bin/bun server.ts"),
            (0, "UNKNOWN"),
            (0, "12300"),
            (0, "bun run --cwd /home/claude/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6 --shell=bun --silent start"),
        ])
        self.assertEqual(status, "up")
        self.assertIn("via parent", detail)

    def test_down_when_cwd_unreadable_and_parent_is_not_plugin(self):
        # Recycled-PID orphan: cwd unreadable AND parent (e.g. systemd) argv has
        # no plugin-cache path -> down.
        status, detail = self._probe([
            (0, "12345"),
            (0, "/home/claude/.bun/bin/bun server.ts"),
            (0, "UNKNOWN"),
            (0, "1"),
            (0, "/sbin/init splash"),
        ])
        self.assertEqual(status, "down")
        self.assertIn("stale-PID recycling", detail)

    def test_inconclusive_when_cwd_unreadable_and_ppid_probe_fails(self):
        status, _ = self._probe([
            (0, "12345"),
            (0, "/home/claude/.bun/bin/bun server.ts"),
            (0, "UNKNOWN"),
            (-1, ""),
        ])
        self.assertEqual(status, "inconclusive")

    def test_down_when_pid_dead(self):
        # bot.pid present, but ps says DEAD — the 409-death signature.
        status, detail = self._probe([(0, "12345"), (0, "DEAD")])
        self.assertEqual(status, "down")
        self.assertIn("DEAD", detail)

    def test_down_when_pid_is_not_the_bun_bridge(self):
        # PID recycled to some other process (not bun server.ts).
        status, _ = self._probe([(0, "12345"), (0, "/usr/bin/python3 something.py")])
        self.assertEqual(status, "down")

    def test_no_bridge_when_bot_pid_absent(self):
        status, _ = self._probe([(0, "MISSING")])
        self.assertEqual(status, "no_bridge")

    def test_inconclusive_when_sudo_read_fails(self):
        # rc != 0 on the bot.pid read (sudo denied / error) → fail-open, no alert.
        status, _ = self._probe([(-1, "")])
        self.assertEqual(status, "inconclusive")

    def test_inconclusive_when_bot_pid_not_numeric(self):
        status, _ = self._probe([(0, "garbage")])
        self.assertEqual(status, "inconclusive")


class CheckAgentTest(unittest.TestCase):
    def _check(self, status, *, dry_run=False, cooldown=False, restart_window=False):
        with mock.patch.object(clm, "probe_bridge", return_value=(status, "detail")), \
             mock.patch.object(clm, "_in_alert_cooldown", return_value=cooldown), \
             mock.patch.object(clm, "_in_restart_window", return_value=restart_window), \
             mock.patch.object(clm, "_record_alert") as rec, \
             mock.patch("tools.inbox_consumer.route_failure_alert") as alert:
            ret = clm.check_agent(AGENT, dry_run=dry_run, now=1000)
        return ret, alert, rec

    def test_down_in_restart_window_suppresses_alert_and_remediation(self):
        # T-1622: a down-read during a recent graceful restart is the restart,
        # not an incident — no alert, no remediation attempt.
        with mock.patch.object(clm, "_maybe_remediate") as rem:
            ret, alert, rec = self._check("down", restart_window=True)
        self.assertEqual(ret, "down")
        alert.assert_not_called()
        rec.assert_not_called()
        rem.assert_not_called()

    def test_down_fresh_alerts_and_records(self):
        ret, alert, rec = self._check("down")
        self.assertEqual(ret, "down")
        alert.assert_called_once()
        # routed for the right agent + vocab
        args = alert.call_args.args
        self.assertEqual(args[0], AGENT)
        self.assertEqual(args[2], clm.VOCAB)
        rec.assert_called_once()

    def test_down_in_cooldown_does_not_alert(self):
        _, alert, rec = self._check("down", cooldown=True)
        alert.assert_not_called()
        rec.assert_not_called()

    def test_down_dry_run_does_not_alert(self):
        _, alert, rec = self._check("down", dry_run=True)
        alert.assert_not_called()
        rec.assert_not_called()

    def test_up_does_not_alert(self):
        _, alert, _ = self._check("up")
        alert.assert_not_called()

    def test_no_bridge_does_not_alert(self):
        _, alert, _ = self._check("no_bridge")
        alert.assert_not_called()

    def test_inconclusive_does_not_alert(self):
        # A probe failure is not a bridge failure — must never alert.
        _, alert, _ = self._check("inconclusive")
        alert.assert_not_called()


class RestartWindowTest(unittest.TestCase):
    def test_reads_context_fill_state_files(self):
        import json as _json
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as td:
            with mock.patch.dict(clm.os.environ, {"PANTHEON_STATE_DIR": td}):
                # no state files -> not in window (fail-open)
                self.assertFalse(clm._in_restart_window(AGENT, now=1000))
                # fresh last_fire stamp -> in window
                Path(td, f"context_fill_{AGENT}_last_fire.json").write_text(
                    _json.dumps({"ts": 900}))
                self.assertTrue(clm._in_restart_window(AGENT, now=1000))
                # past the grace -> not in window
                self.assertFalse(clm._in_restart_window(AGENT, now=900 + clm.RESTART_GRACE_SECONDS + 1))
                # malformed file -> fail-open, not in window
                Path(td, f"context_fill_{AGENT}_last_fire.json").write_text("not json")
                self.assertFalse(clm._in_restart_window(AGENT, now=1000))
                # last_restart alone also counts (recovery tombstone path)
                Path(td, f"context_fill_{AGENT}_last_restart.json").write_text(
                    _json.dumps({"ts": 950}))
                self.assertTrue(clm._in_restart_window(AGENT, now=1000))


class CliTest(unittest.TestCase):
    def test_requires_agent_or_all(self):
        with self.assertRaises(SystemExit):
            clm.main([])

    def test_all_probes_every_agent(self):
        with mock.patch.object(clm, "check_agent") as ca:
            rc = clm.main(["--all"])
        self.assertEqual(rc, 0)
        self.assertEqual(ca.call_count, len(clm.AGENT_CONFIG))


class RemediateTest(unittest.TestCase):
    """Auto-remediation (T-0997, ship-dark)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.statefile = Path(self._tmp.name) / "rem.json"
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(clm, "_remediate_state_path_for", return_value=self.statefile)
        p.start()
        self.addCleanup(p.stop)

    def _rem(self, agent, *, enabled=True, fire_ok=True, now=10000):
        with mock.patch.object(clm, "REMEDIATE_ENABLED", enabled), \
             mock.patch.object(clm, "write_tombstone") as wt, \
             mock.patch.object(clm, "fire_restart", return_value=(fire_ok, "")) as fr, \
             mock.patch("tools.inbox_consumer.route_failure_alert"):
            ret = clm._maybe_remediate(agent, clm.AGENT_CONFIG[agent], "detail", now=now)
        return ret, wt, fr

    def test_disabled_does_not_fire(self):
        # SHIP DARK: with the flag off, never restart anything.
        ret, wt, fr = self._rem("recovery", enabled=False)
        self.assertFalse(ret)
        wt.assert_not_called()
        fr.assert_not_called()

    def test_recovery_uses_tombstone(self):
        ret, wt, fr = self._rem("recovery")
        self.assertTrue(ret)
        wt.assert_called_once()
        fr.assert_not_called()  # recovery never direct-fires (would race its own restart)

    def test_direct_agent_uses_fire_restart(self):
        ret, wt, fr = self._rem("orchestrator")
        self.assertTrue(ret)
        fr.assert_called_once()
        wt.assert_not_called()

    def test_cooldown_blocks_refire(self):
        self.statefile.write_text(json.dumps({"ts": 10000 - 60, "consecutive": 1}))  # 60s ago
        ret, wt, fr = self._rem("recovery", now=10000)
        self.assertFalse(ret)
        wt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
