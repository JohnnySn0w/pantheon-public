"""ORG-076 BT5b (T-6000): the Telegram inbound liveness detector, check_agent writer -> drop file -> runner
``run_cycle`` -> episode store, hermetic (tmp drop dir, tmp clone + bare tmp origin, fake probe seam; the real
production ``REGISTRY`` entry). Nothing touches a real state dir, .env, bot.pid, sudo or tmux."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import channel_liveness_monitor as clm
from tools import episode_store as es
from tools import mailbox_wake as mw
from tools import org076_store_runner as run
from tools import org076_telegram_inbound as tgi
from tools import repair_evidence as rev
from tools._episode_fixtures import git
from tools.test_org076_store_runner import T0, Rig

SWITCH = tgi.ENV_SWITCH
DOMAIN = "telegram-inbound"
CANARY = "CANARY-9f3a1c7e-bot-token-123456:AAEhBP0av"
NOW = 1_800_000_000


def _tmpdir(case) -> Path:
    d = Path(tempfile.mkdtemp(prefix="tgi-"))
    case.addCleanup(lambda: __import__("shutil").rmtree(d, True))
    return d


class Harness:
    """Runs clm.check_agent with every external gate faked, and records alerts/remediation/record calls."""

    def check(self, agent, status, detail="detail", *, env=None, drop=None, restart_window=False, deaths=0,
              muted=False, maintenance=False, unit_disabled=None, seat=None, dry_run=False, now=NOW,
              cooldown=False):
        env = dict(env or {})
        if drop is not None:
            env["PANTHEON_ORG076_SAMPLES_DIR"] = str(drop)
        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(clm, "probe_bridge", return_value=(status, detail)), \
                mock.patch.object(clm.seat_state, "safe_effective_for", return_value=seat), \
                mock.patch.object(clm, "_seat_in_maintenance", return_value=maintenance), \
                mock.patch.object(clm, "_seat_muted", return_value=muted), \
                mock.patch.object(clm, "_session_unit_is_disabled", return_value=unit_disabled), \
                mock.patch.object(clm, "_in_alert_cooldown", return_value=cooldown), \
                mock.patch.object(clm, "_in_restart_window", return_value=restart_window), \
                mock.patch.object(clm, "_session_deaths_in_window", return_value=deaths), \
                mock.patch.object(clm, "_record_alert") as rec, \
                mock.patch.object(clm, "_maybe_remediate") as rem, \
                mock.patch("tools.inbox_consumer.route_failure_alert") as alert:
            ret = clm.check_agent(agent, dry_run=dry_run, now=now)
        return ret, alert.call_args_list, rem.call_args_list, rec.call_args_list


class Mapping(unittest.TestCase, Harness):
    def setUp(self):
        self.drop = _tmpdir(self)
        p = mock.patch.dict("os.environ", {})
        p.start()
        self.addCleanup(p.stop)
        __import__("os").environ.pop(SWITCH, None)

    def drops(self):
        return [json.loads(p.read_text()) for p in sorted(self.drop.iterdir())]

    def run_on(self, status, detail="d", **kw):
        self.tick = getattr(self, "tick", 0) + 1  # distinct drop-name stamps, so name order == call order
        kw.setdefault("now", NOW + self.tick)
        return self.check("orchestrator", status, detail, env={SWITCH: "1"}, drop=self.drop, **kw)

    def test_up_is_healthy_and_codex_poller_is_healthy(self):
        self.run_on("up", "bun live")
        self.run_on("up", f"{clm.CODEX_POLLER_UNIT} active on the same bot token (codex harness); idle ('x')")
        a, b = self.drops()
        self.assertEqual((a["verdict"], a["fault_id"], b["verdict"], b["fault_id"]), ("healthy", None, "healthy", None))
        self.assertIn("reason=codex-poller-serves", b["detail"])
        self.assertEqual(a["domain_key"], "telegram-inbound:orchestrator")

    def test_down_is_broken_and_inconclusive_is_unknown(self):
        self.run_on("down", "bot.pid ABSENT but bridge configured (.env present) -- bridge died/never-started")
        self.run_on("inconclusive", "bot.pid read rc=-1")
        d, u = self.drops()
        self.assertEqual((d["verdict"], d["fault_id"]), ("broken", "telegram-inbound-down:orchestrator"))
        self.assertEqual((u["verdict"], u["fault_id"]), ("unknown", "telegram-inbound-unmeasurable:orchestrator"))
        self.assertIn("reason=pidfile-absent-configured", d["detail"])
        self.assertIn(tgi.PLAYBOOK, d["detail"])
        self.assertEqual(d["account"], "claude")

    def test_no_bridge_writes_no_verdict(self):
        self.run_on("no_bridge", "no bot.pid and no .env")
        self.assertEqual(self.drops(), [])

    def test_down_inside_the_restart_grace_writes_nothing_not_healthy(self):
        ret, *_ = self.run_on("down", restart_window=True, deaths=0)
        self.assertEqual(ret, "down")
        self.assertEqual(self.drops(), [])

    def test_up_inside_the_restart_grace_is_healthy(self):
        self.run_on("up", restart_window=True)
        self.assertEqual([d["verdict"] for d in self.drops()], ["healthy"])

    def test_crash_loop_inside_the_grace_writes_broken_like_the_alert(self):
        _, alert, rem, _ = self.run_on("down", restart_window=True, deaths=clm.RESPAWN_LOOP_DEATHS)
        self.assertEqual([d["verdict"] for d in self.drops()], ["broken"])
        self.assertEqual(len(alert), 1)
        self.assertEqual(rem, [])

    def test_deaths_below_the_bound_keep_the_grace_silent(self):
        self.run_on("down", restart_window=True, deaths=clm.RESPAWN_LOOP_DEATHS - 1)
        self.assertEqual(self.drops(), [])

    def test_muted_seat_gets_no_verdict_of_any_kind(self):
        for status in ("up", "down", "inconclusive"):
            with self.subTest(status=status):
                self.run_on(status, muted=True)
        self.assertEqual(self.drops(), [])

    def test_maintenance_paused_asleep_and_dry_run_write_nothing(self):
        self.run_on("down", maintenance=True)
        self.run_on("down", unit_disabled=True)
        asleep = mock.Mock(expected_down=True, state="deep-sleep", owner="o", reason="r", expires=None)
        self.run_on("down", seat=asleep)
        self.run_on("up", seat=asleep)  # sleep breach: alerted by its own path, no verdict
        self.run_on("down", dry_run=True)
        self.assertEqual(self.drops(), [])

    def test_down_in_alert_cooldown_still_writes_the_raw_verdict(self):
        self.run_on("down", cooldown=True)
        self.assertEqual([d["verdict"] for d in self.drops()], ["broken"])

    def test_persona_without_a_seat_is_suppressed_and_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(tgi.feed_safely("not-a-seat", "down", "d", drop=self.drop, env={SWITCH: "1"}))
        self.assertEqual(self.drops(), [])
        self.assertIn("not-a-seat: suppressed (no seat", err.getvalue())

    def test_every_agent_config_persona_has_a_seat_or_is_logged_suppressed(self):  # A3: no silent drop
        self.assertTrue(clm.AGENT_CONFIG)
        for persona in sorted(clm.AGENT_CONFIG):
            with self.subTest(persona=persona):
                err = io.StringIO()
                before = len(list(self.drop.iterdir()))
                with contextlib.redirect_stderr(err):
                    name = tgi.feed_safely(persona, "down", "d", drop=self.drop, env={SWITCH: "1"})
                if tgi.seat_for(persona) is not None:
                    self.assertIsNotNone(name)
                    self.assertEqual(len(list(self.drop.iterdir())), before + 1)
                else:
                    self.assertIsNone(name)
                    self.assertIn(f"{persona}: suppressed", err.getvalue())
        for persona in clm.AGENT_CONFIG:  # today they all have one; if that changes the loop above still holds
            self.assertIn(persona, mw.AGENT_PANE)

    def test_seat_for_is_the_mail_delivery_function(self):
        from tools import org076_mail_delivery as omd
        self.assertIs(tgi.seat_for, omd.seat_for)

    def test_registry_entry(self):
        e = run.REGISTRY[DOMAIN]
        self.assertEqual((e.n, e.m, e.fault_kinds, e.freshness_unit), (3, 2, tgi.FAULT_KINDS, None))
        self.assertEqual(e.domain_key("specialist"), "telegram-inbound:specialist")
        self.assertEqual(e.spec_for("watchman", "codex-fallback").harness, "codex")
        self.assertEqual(e.spec_for("specialist", "claude-research").harness, "claude")
        self.assertEqual(e.spec_for("specialist", "claude-research").candidates, rev.REPAIR_CANDIDATES)


class DarkSwitch(unittest.TestCase, Harness):
    """A non-"1" switch writes no drop and leaves the monitor's alerts/state identical to a baseline run."""

    def setUp(self):
        self.drop = _tmpdir(self)
        p = mock.patch.dict("os.environ", {})
        p.start()
        self.addCleanup(p.stop)
        __import__("os").environ.pop(SWITCH, None)

    def test_off_unless_exactly_1_and_nothing_else_changes(self):
        base = self.check("orchestrator", "down", drop=self.drop)
        self.assertEqual(list(self.drop.iterdir()), [])
        for value in ("", "0", "true", "1 ", " 1", "yes", "True", "01"):
            with self.subTest(value=value):
                self.assertEqual(self.check("orchestrator", "down", env={SWITCH: value}, drop=self.drop), base)
                self.assertEqual(list(self.drop.iterdir()), [])
                self.assertFalse(tgi.enabled({SWITCH: value}))
        self.assertEqual(self.check("orchestrator", "down", env={SWITCH: "1"}, drop=self.drop), base)
        self.assertEqual(len(list(self.drop.iterdir())), 1)

    def test_switch_is_read_at_call_time(self):
        with mock.patch.dict("os.environ", {SWITCH: "1"}):
            self.assertTrue(tgi.enabled())
        with mock.patch.dict("os.environ", {SWITCH: "0"}):
            self.assertFalse(tgi.enabled())

    def test_check_agent_is_byte_identical_switch_on_vs_off_on_every_status_path(self):  # A8
        paths = {
            "up": dict(status="up"),
            "down": dict(status="down"),
            "down-in-grace": dict(status="down", restart_window=True),
            "crash-loop": dict(status="down", restart_window=True, deaths=clm.RESPAWN_LOOP_DEATHS),
            "muted-down": dict(status="down", muted=True),
            "no_bridge": dict(status="no_bridge"),
            "inconclusive": dict(status="inconclusive"),
        }
        for name, kw in paths.items():
            with self.subTest(path=name):
                off = self.check("orchestrator", detail="d", drop=self.drop, **kw)
                on = self.check("orchestrator", detail="d", env={SWITCH: "1"}, drop=self.drop, **kw)
                self.assertEqual(on, off)

    def test_a_writer_that_raises_changes_nothing(self):
        base = self.check("orchestrator", "down", drop=self.drop)
        with mock.patch.object(tgi, "write_drop", side_effect=OSError("disk full")):
            self.assertEqual(self.check("orchestrator", "down", env={SWITCH: "1"}, drop=self.drop), base)
        with mock.patch.object(tgi, "feed_safely", side_effect=RuntimeError("boom")):
            self.assertEqual(self.check("orchestrator", "down", env={SWITCH: "1"}, drop=self.drop), base)
        with mock.patch.object(tgi, "enabled", side_effect=RuntimeError("boom")):
            self.assertEqual(self.check("orchestrator", "down", env={SWITCH: "1"}, drop=self.drop), base)

    def test_unwritable_drop_dir_is_logged_and_ignored(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            base = self.check("orchestrator", "down", drop=self.drop / "no-such-dir")
            got = self.check("orchestrator", "down", env={SWITCH: "1"}, drop=self.drop / "no-such-dir")
        self.assertEqual(got, base)
        self.assertIn("drop failed (ignored; monitor alerting unaffected)", err.getvalue())


class Evidence(unittest.TestCase):
    def test_reason_allowlist_and_unrecognised_prefix_maps_to_other(self):  # A5
        self.assertEqual(tgi.reason_for("down", "totally new probe wording " + CANARY), "other")
        self.assertEqual(tgi.reason_for("up", "mystery"), "bridge-live")
        self.assertEqual(tgi.reason_for("down", "bot.pid=12 but process DEAD (409-death signature)"),
                         "pid-not-live-bridge")
        self.assertEqual(tgi.reason_for("down", None), "other")
        self.assertIn(tgi.reason_for("down", "x"), tgi.REASONS)

    def test_probe_detail_never_reaches_the_envelope(self):  # A5
        probe = f"bot.pid=77 is not the bun bridge: 'bun server.ts --token {CANARY}'"
        body = tgi.envelope("orchestrator", "down", probe, "claude", "r1", datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertNotIn(CANARY, json.dumps(body))
        self.assertNotIn("77", body["detail"])
        self.assertLessEqual(len(body["detail"]), tgi.qpd.DETAIL_LIMIT)
        self.assertEqual(body["detail"], "persona=orchestrator status=down reason=pid-not-live-bridge playbook=" + tgi.PLAYBOOK)


class EndToEnd(Rig, Harness):
    """check_agent writer -> drop file -> run_cycle -> episode store, real production registry."""

    def setUp(self):
        super().setUp()
        self.k = 0
        p = mock.patch.dict("os.environ", {SWITCH: "1", "PANTHEON_ORG076_SAMPLES_DIR": str(self.drop)})
        p.start()
        self.addCleanup(p.stop)

    def sample(self, agent, status, detail="d", **kw):
        self.k += 1
        return self.check(agent, status, detail, now=int(T0.timestamp()) + 300 * self.k, **kw)

    def dom(self, persona="orchestrator"):
        return es.load_domain(f"{DOMAIN}:{persona}", self.state)

    def ticket_text(self):
        (rel,) = self.fx.origin_ticket_files()
        return git("show", f"main:{rel}", cwd=self.fx.origin).stdout

    def test_down_opens_exactly_one_episode_and_one_ticket(self):  # s6.1
        for _ in range(5):
            self.sample("orchestrator", "down")
            self.cycle()
        (ep,) = self.dom().get("episodes", {}).values()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("telegram-inbound-down:orchestrator", "broken"))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual(self.dropped(), [])
        self.assertIn(tgi.PLAYBOOK, self.ticket_text())

    def test_two_samples_open_nothing(self):
        for _ in range(2):
            self.sample("orchestrator", "down")
        self.cycle()
        self.assertEqual(self.fx.origin_ticket_files(), [])

    def test_flap_alternation_under_n_opens_nothing(self):  # s6.4
        for i in range(12):
            self.sample("orchestrator", "down" if i % 2 == 0 else "up")
            self.cycle()
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.assertEqual(self.dom().get("episodes", {}), {})

    def test_down_during_the_restart_grace_opens_nothing(self):
        for _ in range(10):
            self.sample("orchestrator", "down", restart_window=True)
            self.cycle()
        self.assertEqual(self.dropped(), [])
        self.assertEqual(self.fx.origin_ticket_files(), [])

    def test_muted_down_opens_nothing(self):
        for _ in range(5):
            self.sample("orchestrator", "down", muted=True)
            self.cycle()
        self.assertEqual(self.fx.origin_ticket_files(), [])

    def test_t5256_shape_configured_bridge_no_bot_pid_opens_exactly_one_episode(self):
        """Through the REAL probe_bridge with only the subprocess seam faked: bot.pid MISSING, parent dir
        present (provably absent), bridge .env PRESENT -> 'down' -> one broken episode."""
        seen = []

        def fake_run(argv, *a, **k):
            script = argv[-1] if isinstance(argv[-1], str) else ""
            seen.append(script)
            if "cat " in script and "bot.pid" in script:
                return 0, "MISSING"
            if "echo ABSENT" in script:
                return 0, "ABSENT"
            if "test -f" in script and ".env" in script:
                return 0, "YES"
            return -1, ""

        env_dir = _tmpdir(self)
        (env_dir / ".env").write_text(f"TELEGRAM_BOT_TOKEN={CANARY}\n")
        with mock.patch.object(clm, "_run", side_effect=fake_run), \
                mock.patch.object(clm, "_codex_poller_serves_bridge", return_value=False):
            for _ in range(3):
                self.k += 1
                with mock.patch.dict("os.environ", {}), \
                        mock.patch.object(clm.seat_state, "safe_effective_for", return_value=None), \
                        mock.patch.object(clm, "_seat_in_maintenance", return_value=False), \
                        mock.patch.object(clm, "_seat_muted", return_value=False), \
                        mock.patch.object(clm, "_session_unit_is_disabled", return_value=None), \
                        mock.patch.object(clm, "_in_restart_window", return_value=False), \
                        mock.patch.object(clm, "_in_alert_cooldown", return_value=True), \
                        mock.patch.object(clm, "_maybe_remediate"):
                    self.assertEqual(clm.check_agent("specialist", now=int(T0.timestamp()) + 300 * self.k), "down")
        self.cycle()
        (ep,) = self.dom("specialist")["episodes"].values()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("telegram-inbound-down:specialist", "broken"))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertNotIn(CANARY, self.ticket_text())

    def test_unconfigured_persona_no_bot_pid_no_env_opens_nothing(self):
        def fake_run(argv, *a, **k):
            script = argv[-1]
            if "echo ABSENT" in script:
                return 0, "ABSENT"
            if "bot.pid" in script and "cat " in script:
                return 0, "MISSING"
            return 0, "NO"
        with mock.patch.object(clm, "_run", side_effect=fake_run), \
                mock.patch.object(clm.seat_state, "safe_effective_for", return_value=None):
            for _ in range(4):
                self.assertEqual(clm.check_agent("specialist", now=NOW), "no_bridge")
        self.assertEqual(self.dropped(), [])

    def test_canary_in_probe_detail_env_and_bot_config_never_reaches_drop_store_or_ticket(self):  # s6.15
        cfg_dir = _tmpdir(self)
        (cfg_dir / ".env").write_text(f"TELEGRAM_BOT_TOKEN={CANARY}\n")
        (cfg_dir / "bot.json").write_text(json.dumps({"token": CANARY}))
        leaky = f"bot.pid=77 is not the bun bridge: 'bun server.ts --token {CANARY}' env={CANARY}"
        for _ in range(3):
            self.sample("orchestrator", "down", leaky)
        drops = "".join(p.read_text() for p in self.drop.iterdir())
        self.cycle()
        store = "".join(p.read_text() for p in Path(self.state).rglob("*") if p.is_file())
        self.assertNotIn(CANARY, drops)
        self.assertNotIn(CANARY, store)
        self.assertNotIn(CANARY, self.ticket_text())
        self.assertNotIn("TELEGRAM_BOT_TOKEN", drops + store + self.ticket_text())
        self.assertIn("reason=pid-not-live-bridge", self.ticket_text())

    def test_deferred_evidence_never_routes_cross_account_or_to_the_subject(self):  # s6.16 / s6.18
        with mock.patch.object(rev, "make_default_redactor", return_value=lambda s: s + ("y" * 99 + "\n") * 400):
            for _ in range(3):
                self.sample("orchestrator", "down")
            self.cycle()
        text = self.ticket_text()
        self.assertIn("evidence_deferred: true", text)
        self.assertNotIn("evidence_snapshot", text)
        (ep,) = self.dom().get("episodes", self.dom().get("episodes")).values()
        route = ep["delivery"]["route"]
        # s6.18: deferred evidence is never routed cross-account. With no same-account live candidate the
        # store pages the detector owner (kind "page"), it does not hand the evidence to another account.
        self.assertTrue(route["deferred"])
        self.assertEqual(route["kind"], "page")
        self.assertTrue(route["refused_cross_account"], route)

    def test_unknown_episode_for_persistent_inconclusive(self):
        for _ in range(3):
            self.sample("orchestrator", "inconclusive", "bot.pid read rc=-1")
        self.cycle()
        (ep,) = self.dom().get("episodes", {}).values()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("telegram-inbound-unmeasurable:orchestrator", "unknown"))

    def test_fleet_wide_inconclusive_opens_one_unknown_episode_per_persona(self):  # A4: documents CURRENT behaviour
        personas = sorted(clm.AGENT_CONFIG)
        for _ in range(3):
            for p in personas:
                self.sample(p, "inconclusive", "bot.pid read rc=-1")
        self.cycle()
        for p in personas:
            with self.subTest(persona=p):
                (ep,) = self.dom(p)["episodes"].values()
                self.assertEqual((ep["verdict_kind"], ep["fault_id"]),
                                 ("unknown", f"telegram-inbound-unmeasurable:{p}"))
                self.assertNotEqual(ep["delivery"]["route"].get("target"), p)
        self.assertEqual(len(self.fx.origin_ticket_files()), len(personas))

    def test_runner_rejects_a_persona_with_no_seat(self):
        self.sample("orchestrator", "down")
        without = {k: v for k, v in mw.AGENT_PANE.items() if k != "orchestrator"}
        with mock.patch.object(mw, "AGENT_PANE", without):
            rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (1, 0))

    def test_long_persona_domain_fits_the_store_slug(self):
        self.sample("archivist", "down")
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (0, 1))


if __name__ == "__main__":
    unittest.main()
