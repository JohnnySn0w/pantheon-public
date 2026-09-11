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

    def test_up_via_own_argv_when_cwd_and_parent_lack_the_path(self):
        # T-2697 regression: recovery's LIVE topology — `claude --channels plugin:telegram`
        # spawns `bun run --cwd <plugin-cache> … server.ts` DIRECTLY, so the
        # identifying plugin-cache path is in the bridge's OWN argv (NOT /proc cwd,
        # NOT the parent). Must read UP off the own argv, without the cwd/parent
        # probes ever running (only 2 _run results supplied here).
        status, detail = self._probe([
            (0, "12345"),
            (0, "/home/claude-fallback/.bun/bin/bun run --cwd "
                "/home/claude-fallback/.claude/plugins/cache/claude-plugins-official/"
                "telegram/0.0.6 --silent server.ts"),
        ])
        self.assertEqual(status, "up")
        self.assertIn("own argv", detail)

    def test_up_via_parent_claude_channels_when_cwd_unreadable(self):
        # T-2697: the parent-args fallback must also accept a `claude --channels
        # plugin:telegram …` parent (the bun bridge's direct parent when there is
        # no intermediate `bun run --cwd` wrapper), not only the wrapper form.
        status, detail = self._probe([
            (0, "12345"),
            (0, "/home/claude-fallback/.bun/bin/bun server.ts"),
            (0, "UNKNOWN"),
            (0, "12300"),
            (0, "/home/claude-fallback/.npm-global/bin/claude --channels "
                "plugin:telegram@claude-plugins-official --model claude-opus-4-8"),
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
        status, detail = self._probe([(0, "12345"), (0, "NOSUCHPROC")])
        self.assertEqual(status, "down")
        self.assertIn("DEAD", detail)

    def test_down_when_pid_is_not_the_bun_bridge(self):
        # PID recycled to some other process (not bun server.ts).
        status, _ = self._probe([(0, "12345"), (0, "/usr/bin/python3 something.py")])
        self.assertEqual(status, "down")

    def test_no_bridge_when_bot_pid_absent_and_unconfigured(self):
        # T-1794: MISSING bot.pid + .env ABSENT (NO) → genuinely no bridge → skip.
        status, _ = self._probe([(0, "MISSING"), (0, "ABSENT"), (0, "NO")])
        self.assertEqual(status, "no_bridge")

    def test_down_when_bot_pid_absent_but_bridge_configured(self):
        # T-1794 (the 2026-07-16 orchestrator comms-dark): MISSING bot.pid but .env
        # PRESENT (bridge configured) → the bridge DIED → DOWN (alertable).
        status, detail = self._probe([(0, "MISSING"), (0, "ABSENT"), (0, "YES")])
        self.assertEqual(status, "down")
        self.assertIn("configured", detail)

    def test_inconclusive_when_bot_pid_absent_and_env_probe_fails(self):
        status, _ = self._probe([(0, "MISSING"), (0, "ABSENT"), (-1, "")])
        self.assertEqual(status, "inconclusive")

    def test_inconclusive_when_sudo_read_fails(self):
        # rc != 0 on the bot.pid read (sudo denied / error) → fail-open, no alert.
        status, _ = self._probe([(-1, "")])
        self.assertEqual(status, "inconclusive")

    def test_inconclusive_when_bot_pid_not_numeric(self):
        status, _ = self._probe([(0, "garbage")])
        self.assertEqual(status, "inconclusive")


class CheckAgentTest(unittest.TestCase):
    def _check(self, status, *, dry_run=False, cooldown=False, restart_window=False,
               unit_disabled=None, maintenance=False, muted=False):
        # unit_disabled defaults to None = "could not tell", the fail-open value —
        # so every pre-existing case below still exercises the alerting path and
        # none of them silently starts shelling out to a real `systemctl`.
        # maintenance/muted default False + are patched (not read from the real
        # registries) so a live-armed/live-muted seat can never make these non-hermetic.
        with mock.patch.object(clm, "probe_bridge", return_value=(status, "detail")), \
             mock.patch.object(clm, "_seat_in_maintenance", return_value=maintenance), \
             mock.patch.object(clm, "_seat_muted", return_value=muted), \
             mock.patch.object(clm, "_session_unit_is_disabled", return_value=unit_disabled), \
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

    def test_down_muted_suppresses_page_but_still_remediates(self):
        # T-2987 / ORG-076 §7: a muted seat withholds the comms-dark PAGE, but a mute is
        # ALERT-ONLY — unlike 'paused'/'maintenance', remediation is UNAFFECTED. (Contrast
        # test_down_on_a_deliberately_paused_seat which suppresses BOTH.)
        with mock.patch.object(clm, "_maybe_remediate") as rem:
            ret, alert, rec = self._check("down", muted=True)
        self.assertEqual(ret, "down")
        alert.assert_not_called()
        rec.assert_not_called()
        rem.assert_called_once()  # remediation still runs — mute never vetoes an action

    def test_down_dry_run_does_not_alert(self):
        _, alert, rec = self._check("down", dry_run=True)
        alert.assert_not_called()
        rec.assert_not_called()

    def test_skip_set_short_circuits_before_probe(self):
        # A seat in CHANNEL_LIVENESS_SKIP (e.g. mid-harness-migration orchestrator) is not
        # probed/alerted/remediated at all — even when the bridge would read down.
        with mock.patch.object(clm, "CHANNEL_LIVENESS_SKIP", {AGENT}), \
             mock.patch.object(clm, "probe_bridge", return_value=("down", "detail")) as probe, \
             mock.patch.object(clm, "_maybe_remediate") as rem, \
             mock.patch("tools.inbox_consumer.route_failure_alert") as alert:
            ret = clm.check_agent(AGENT, now=1000)
        self.assertEqual(ret, "skipped")
        probe.assert_not_called()
        alert.assert_not_called()
        rem.assert_not_called()

    def test_empty_skip_set_is_inert(self):
        # Default (empty) skip set must NOT suppress: the agent is probed normally
        # and a fresh down still alerts. Guards against the skip accidentally firing.
        with mock.patch.object(clm, "CHANNEL_LIVENESS_SKIP", set()):
            ret, alert, rec = self._check("down")
        self.assertEqual(ret, "down")
        alert.assert_called_once()

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

    def test_down_on_a_deliberately_paused_seat_does_not_alert_or_remediate(self):
        # T-2393, the incident this was written from: after the operator-directed
        # fleet daemon pause, specialist and mnemosyne read down every poll and specialist
        # took FOUR graceful-restart remediations, each escalating to Recovery. A
        # disabled session unit means the seat was taken out of service on
        # purpose — the down-read IS the pause.
        with mock.patch.object(clm, "_maybe_remediate") as rem:
            ret, alert, rec = self._check("down", unit_disabled=True)
        self.assertEqual(ret, "paused")
        alert.assert_not_called()
        rec.assert_not_called()
        rem.assert_not_called()

    def test_paused_check_precedes_the_restart_window(self):
        # A paused seat is not mid-restart. Reaching the restart-window check at
        # all would make the verdict depend on stale restart state, and would
        # return 'down' rather than naming the pause.
        with mock.patch.object(clm, "_maybe_remediate"), \
             mock.patch.object(clm, "_in_restart_window") as win:
            ret, _, _ = self._check("down", unit_disabled=True)
        self.assertEqual(ret, "paused")

    def test_down_on_a_seat_in_maintenance_does_not_alert_or_remediate(self):
        # T-2984: during a runtime-controller cutover the bridge may read down while
        # the harness is swapped. A seat in an active maintenance window is left alone
        # — no alert, no remediation — so the monitor does not fight the cutover.
        with mock.patch.object(clm, "_maybe_remediate") as rem:
            ret, alert, rec = self._check("down", maintenance=True)
        self.assertEqual(ret, "maintenance")
        alert.assert_not_called()
        rec.assert_not_called()
        rem.assert_not_called()

    def test_maintenance_check_precedes_pause_and_restart_window(self):
        # Maintenance is the strongest "leave this seat alone" signal: it must win even
        # when the seat also looks paused and mid-restart, and must never consult those.
        with mock.patch.object(clm, "_maybe_remediate"), \
             mock.patch.object(clm, "_session_unit_is_disabled") as dis, \
             mock.patch.object(clm, "_in_restart_window") as win:
            ret, _, _ = self._check("down", maintenance=True, unit_disabled=True, restart_window=True)
        self.assertEqual(ret, "maintenance")
        dis.assert_not_called()
        win.assert_not_called()
        win.assert_not_called()

    def test_enabled_unit_still_alerts(self):
        # The guard must not swallow a REAL bridge death. An enabled unit whose
        # bridge is down is the 409 case this monitor exists for.
        ret, alert, rec = self._check("down", unit_disabled=False)
        self.assertEqual(ret, "down")
        alert.assert_called_once()
        rec.assert_called_once()

    def test_unknown_pause_state_still_alerts(self):
        # Fail-open: 'cannot tell' must never stand in for 'paused'. Silencing a
        # genuinely comms-dark agent is the expensive direction.
        ret, alert, _ = self._check("down", unit_disabled=None)
        self.assertEqual(ret, "down")
        alert.assert_called_once()

    def test_alert_text_does_not_claim_the_session_is_healthy(self):
        # T-2393: the alert asserted "while session appears healthy" about two
        # seats with no claude process and no tmux server. This probe does not
        # look at the session at all, so it must not characterise it.
        _, alert, _ = self._check("down", unit_disabled=False)
        msg = alert.call_args.args[1]
        self.assertNotIn("appears healthy", msg)


class SessionUnitDisabledTest(unittest.TestCase):
    """T-2393: the disabled-vs-inactive discriminator."""

    def _probe(self, rc, out, active=None, cfg=None):
        # `active`: None → no is-active probe expected (is-enabled short-circuits
        # for enabled/unknown/probe-fail); a (rc, out) tuple → the SECOND _run
        # (is-active) result, used only on the disabled/masked path (T-2671).
        cfg = cfg if cfg is not None else {"sudo_user": "claude-research",
                                           "restart_service": "claude-telegram-research.service",
                                           "restart_mode": "user"}
        results = [(rc, out)]
        if active is not None:
            results.append(active)
        with mock.patch.object(clm, "_run", side_effect=results) as run:
            return clm._session_unit_is_disabled("specialist", cfg), run

    def test_disabled_and_inactive_is_paused(self):
        # `is-enabled` exits NON-ZERO for disabled — the verdict must come from
        # stdout. Reading rc would make every disabled unit look like a failure.
        # T-2671: genuine pause is disabled AND stopped (specialist/mnemosyne).
        verdict, _ = self._probe(1, "disabled", active=(3, "inactive"))
        self.assertIs(verdict, True)

    def test_disabled_but_active_is_not_paused(self):
        # T-2671 regression: a seat can be disabled (not enabled to auto-start)
        # yet ACTIVE — launched via a scope/manually (recovery's old disabled+ACTIVE
        # state). It is LIVE, not paused, so it must judge normally — else its
        # own alerts are falsely suppressed.
        verdict, _ = self._probe(1, "disabled", active=(0, "active"))
        self.assertIs(verdict, False)

    def test_disabled_but_active_probe_unknown_is_unknown(self):
        # T-2671 fail-open: if we cannot confirm the disabled unit is stopped,
        # do NOT suppress (None → keep alerting) — silencing a possibly-live
        # comms-dark agent is the expensive direction.
        verdict, _ = self._probe(1, "disabled", active=(-1, ""))
        self.assertIsNone(verdict)

    def test_enabled_is_not_paused(self):
        verdict, _ = self._probe(0, "enabled")
        self.assertIs(verdict, False)

    def test_crashed_unit_is_not_paused(self):
        # The whole point of keying on DISABLED: a unit that crashed stays
        # `enabled` and merely reads inactive/failed. That must remain alertable.
        verdict, _ = self._probe(0, "enabled")
        self.assertIs(verdict, False)

    def test_probe_failure_is_unknown(self):
        verdict, _ = self._probe(-1, "")
        self.assertIsNone(verdict)

    def test_no_restart_service_is_unknown(self):
        # metis/atlas have no session-owning unit (T-2210) — unknown, not paused.
        verdict, run = self._probe(0, "enabled", cfg={"sudo_user": "suborch1"})
        self.assertIsNone(verdict)
        run.assert_not_called()

    def test_unrecognised_state_is_unknown(self):
        verdict, _ = self._probe(0, "some-future-systemd-state")
        self.assertIsNone(verdict)

    def test_user_mode_probe_carries_xdg_runtime_dir(self):
        # A --user systemctl call across the sudo boundary needs XDG_RUNTIME_DIR
        # or it cannot reach the peer's manager at all (and would read as a probe
        # failure → unknown → the alert storm continues). Assert the FIRST probe
        # (is-enabled) carries it; T-2671 added a second (is-active) call.
        _, run = self._probe(1, "disabled", active=(0, "active"))
        cmd = " ".join(run.call_args_list[0].args[0])
        self.assertIn("XDG_RUNTIME_DIR", cmd)
        self.assertIn("--user", cmd)
        self.assertIn("is-enabled", cmd)
        self.assertIn("claude-telegram-research.service", cmd)

    def test_disabled_active_probe_is_is_active_and_carries_xdg(self):
        # T-2671: the SECOND probe on the disabled path must be `is-active`
        # (not another is-enabled) and carry XDG_RUNTIME_DIR in --user mode.
        _, run = self._probe(1, "disabled", active=(0, "active"))
        cmd = " ".join(run.call_args_list[1].args[0])
        self.assertIn("is-active", cmd)
        self.assertIn("XDG_RUNTIME_DIR", cmd)
        self.assertIn("claude-telegram-research.service", cmd)


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

    def test_remediate_dispatch_stamp_counts(self):
        # T-1795: a detached remediation dispatch stamps channel_liveness_
        # <agent>_remediate.json. The restart now runs OUTSIDE the monitor's
        # poll, so the next poll can land mid-restart — that down-read must be
        # suppressed, and must not feed a false consecutive=2 "restart did NOT
        # restore" escalation on an independent later death.
        import json as _json
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as td:
            with mock.patch.dict(clm.os.environ, {"PANTHEON_STATE_DIR": td}):
                Path(td, f"channel_liveness_{AGENT}_remediate.json").write_text(
                    _json.dumps({"ts": 950, "unit": "pantheon-channel-remediate-x-950"}))
                self.assertTrue(clm._in_restart_window(AGENT, now=1000))
                self.assertFalse(
                    clm._in_restart_window(AGENT, now=950 + clm.RESTART_GRACE_SECONDS + 1))


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
             mock.patch.object(
                 clm, "fire_restart_detached",
                 return_value=(fire_ok, "", "pantheon-channel-remediate-x-123"),
             ) as fr, \
             mock.patch("tools.inbox_consumer.route_failure_alert"):
            ret = clm._maybe_remediate(agent, clm.AGENT_CONFIG[agent], "detail", now=now)
        return ret, wt, fr

    def test_disabled_does_not_fire(self):
        # SHIP DARK: with the flag off, never restart anything.
        ret, wt, fr = self._rem("recovery", enabled=False)
        self.assertFalse(ret)
        wt.assert_not_called()
        fr.assert_not_called()

    def test_per_agent_remediate_false_stays_alert_only(self):
        # Observation-only agents (mnemosyne, 2026-07-20 profile agreement)
        # opt out via cfg remediate=False: even with the global flag ON,
        # bridge-down must never fire a restart for them.
        ret, wt, fr = self._rem("mnemosyne", enabled=True)
        self.assertFalse(ret)
        wt.assert_not_called()
        fr.assert_not_called()

    def test_recovery_uses_tombstone(self):
        ret, wt, fr = self._rem("recovery")
        self.assertTrue(ret)
        wt.assert_called_once()
        fr.assert_not_called()  # recovery never direct-fires (would race its own restart)

    def test_direct_agent_dispatches_detached_restart(self):
        ret, wt, fr = self._rem("orchestrator")
        self.assertTrue(ret)
        fr.assert_called_once()
        wt.assert_not_called()
        # T-1795: the dispatched transient unit's name is persisted so the
        # consec>=2 escalation can point at its journal.
        state = json.loads(self.statefile.read_text())
        self.assertEqual(state["unit"], "pantheon-channel-remediate-x-123")

    def test_no_blocking_fire_restart_binding(self):
        # CONTRACT (T-1795): this module must never hold a binding to the
        # BLOCKING fire_restart — the monitor unit is a short-budget oneshot
        # and a blocking restart gets cgroup-SIGKILLed mid-handoff-ack
        # (2026-07-17 incident). Reintroducing the sync call requires a new
        # import, which this pin turns into a test failure.
        self.assertFalse(hasattr(clm, "fire_restart"))

    def test_dispatch_failure_not_recorded(self):
        # A failed DISPATCH (systemd-run error) must not write the cooldown
        # state — the next 5-min poll should retry, not wait out 30 min.
        ret, wt, fr = self._rem("orchestrator", fire_ok=False)
        self.assertFalse(ret)
        fr.assert_called_once()
        self.assertFalse(self.statefile.exists())

    def test_cooldown_blocks_refire(self):
        self.statefile.write_text(json.dumps({"ts": 10000 - 60, "consecutive": 1}))  # 60s ago
        ret, wt, fr = self._rem("recovery", now=10000)
        self.assertFalse(ret)
        wt.assert_not_called()


class BlindSentinelMisdirectionT2359(unittest.TestCase):
    """T-2359 (T-2309 part 2, MISDIRECTION class): a blind read must not be
    laundered into a named cause.

    `cat P 2>/dev/null || echo MISSING` makes the pipeline succeed no matter WHY
    cat failed, so rc is always 0 and probe_bridge's `rc != 0 -> inconclusive`
    guard is UNREACHABLE. Absent, mode-000, and present-under-a-non-traversable-
    parent are byte-identical (rc=0, "MISSING") — measured, see
    bugs/shell-sentinel-masks-exit-code-blind-predicate.md.

    The consequence was misdirection, not a destructive act: the alert names a
    CAUSE ("bridge died/never-started", "Likely the single-poller 409") and a
    flag-gated remediation cycles a possibly-healthy session. Note the asymmetry
    this defends: the cwd/ppid probes in the same function already use a
    DON'T-KNOW sentinel (UNKNOWN) and fall through — only these two branches used
    a CONCLUSION word.
    """

    def setUp(self):
        self.cfg = clm.AGENT_CONFIG[AGENT]

    def _probe(self, run_results):
        with mock.patch.object(clm, "_run", side_effect=run_results):
            return clm.probe_bridge(AGENT, self.cfg)

    def test_unreadable_pidfile_is_NOT_reported_as_a_dead_bridge(self):
        """THE REGRESSION. Pidfile EXISTS but cat could not read it."""
        status, detail = self._probe([(0, "MISSING"), (0, "PRESENT")])
        self.assertEqual(status, "inconclusive")
        self.assertNotIn("died", detail)
        self.assertNotIn("never-started", detail)

    def test_unprovable_absence_is_NOT_reported_as_a_dead_bridge(self):
        """Parent dir itself unreadable — absence cannot be established."""
        status, detail = self._probe([(0, "MISSING"), (0, "UNKNOWN")])
        self.assertEqual(status, "inconclusive")
        self.assertNotIn("died", detail)

    def test_absence_probe_subprocess_failure_is_inconclusive(self):
        status, _ = self._probe([(0, "MISSING"), (-1, "")])
        self.assertEqual(status, "inconclusive")

    def test_ps_failure_is_NOT_the_409_death_signature(self):
        """ps could not answer; that is not a statement about the bridge."""
        status, detail = self._probe([(0, "12345"), (0, "PSFAIL")])
        self.assertEqual(status, "inconclusive")
        self.assertNotIn("409", detail)
        self.assertNotIn("DEAD", detail)

    def test_CONTROL_the_real_signals_still_fire(self):
        """Without this, the guards above could pass by making everything
        inconclusive — which would be its own blindness."""
        # genuinely absent + configured -> still the alertable DOWN (T-1794)
        status, detail = self._probe([(0, "MISSING"), (0, "ABSENT"), (0, "YES")])
        self.assertEqual(status, "down")
        self.assertIn("configured", detail)
        # genuinely absent + unconfigured -> still skip
        status, _ = self._probe([(0, "MISSING"), (0, "ABSENT"), (0, "NO")])
        self.assertEqual(status, "no_bridge")
        # ps says no-such-process -> still the 409-death DOWN
        status, detail = self._probe([(0, "12345"), (0, "NOSUCHPROC")])
        self.assertEqual(status, "down")
        self.assertIn("409-death", detail)


if __name__ == "__main__":
    unittest.main()
