"""ORG-076 BT5a (T-6001): the mail-delivery staleness detector, monitor writer -> drop file -> runner
``run_cycle`` -> episode store, hermetic (tmp drop dir, tmp clone + bare tmp origin, fake ports; the real
production ``REGISTRY`` entry, no injected registry). Nothing touches a real state dir, hub or tmux."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tools import episode_store as es
from tools import org076_mail_delivery as omd
from tools import org076_store_runner as run
from tools import repair_evidence as rev
from tools._episode_fixtures import git
from tools.test_org076_store_runner import T0, Rig

import tools.mail_staleness_monitor as msm  # also puts tools/ on sys.path for its bare imports

ms = msm.ms
SWITCH = omd.ENV_SWITCH
DOMAIN = "mail-delivery"


def _obs(unread=0, pane_idle=None, ever=True, age=None, in_flight=0, readable=True, lease=False):
    return ms.InboxObservation(unread=unread, in_flight=in_flight, oldest_unread_age_s=age, pane_idle=pane_idle,
                               ever_received=ever, readable=readable, consumer_lease_held=lease)


COARSE_S = msm.COARSE_SAMPLES * msm.SAMPLE_INTERVAL_SECONDS


class RawVerdicts(unittest.TestCase):
    """The per-sample classification, taken from the monitor's own detect()/coarse_view (Q2)."""

    def v(self, **kw):
        return msm.raw_sample_verdicts({"mason": _obs(**kw)})["mason"]

    def test_table(self):
        self.assertEqual(self.v(), ("healthy", "ok"))
        self.assertEqual(self.v(unread=0, pane_idle=True), ("healthy", "ok"))
        self.assertEqual(self.v(unread=3, pane_idle=False, age=99999), ("healthy", "ok"))  # busy: the backstop's job
        self.assertEqual(self.v(unread=3, pane_idle=True, age=60), ("broken", ms.KIND_IDLE_WITH_UNREAD))
        self.assertEqual(self.v(unread=3, pane_idle=True, lease=True, age=60), ("healthy", "ok"))
        self.assertEqual(self.v(unread=0, ever=False, pane_idle=True), ("broken", ms.KIND_NEVER_RECEIVED))
        self.assertEqual(self.v(readable=False, pane_idle=True), ("unknown", ms.KIND_UNREADABLE))

    def test_pane_none_gate_tracks_the_monitors_coarse_budget(self):
        self.assertEqual(self.v(unread=2, pane_idle=None, age=COARSE_S - 1), ("healthy", "ok"))
        self.assertEqual(self.v(unread=2, pane_idle=None, age=COARSE_S)[0], "unknown")
        self.assertEqual(self.v(unread=2, pane_idle=None, age=None), ("healthy", "ok"))
        self.assertEqual(self.v(unread=0, pane_idle=None, age=COARSE_S * 9), ("healthy", "ok"))
        with mock.patch.object(msm, "COARSE_SAMPLES", msm.COARSE_SAMPLES + 1):  # cited, not copied
            self.assertEqual(self.v(unread=2, pane_idle=None, age=COARSE_S)[0], "healthy")

    def test_not_a_cooldown_alert(self):
        # an inbox idle-with-unread is broken on EVERY sample, whatever the monitor's alert cooldown did
        for _ in range(5):
            self.assertEqual(self.v(unread=1, pane_idle=True, age=10)[0], "broken")


class Switch(unittest.TestCase):
    def setUp(self):
        self.drop = Path(tempfile.mkdtemp(prefix="mdd-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.drop, True))
        p = mock.patch.object(msm, "_recipient_is_muted", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def feed(self, value):
        env = {} if value is None else {SWITCH: value}
        return msm.feed_mail_delivery({"mason": _obs(unread=1, pane_idle=True, age=5)}, drop=self.drop, env=env)

    def test_off_unless_exactly_1(self):
        for value in (None, "", "0", "true", "1 ", " 1", "yes", "True", "01"):
            with self.subTest(value=value):
                self.assertEqual(self.feed(value), [])
                self.assertEqual(list(self.drop.iterdir()), [])

    def test_exactly_1_writes_and_reads_the_env_at_call_time(self):
        self.assertEqual(self.feed("1").__len__(), 1)
        self.assertEqual(len(list(self.drop.iterdir())), 1)
        with mock.patch.dict(os.environ, {SWITCH: "1"}):
            self.assertTrue(omd.enabled())
        with mock.patch.dict(os.environ, {SWITCH: "0"}):
            self.assertFalse(omd.enabled())

    def test_main_alerting_and_state_unchanged_in_every_switch_state(self):
        hub = Path(tempfile.mkdtemp(prefix="hub-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(hub, True))
        import mailbox as _mb
        _mb.provision(hub / "mason")
        obs = {"mason": _obs(unread=2, pane_idle=False, age=msm.BUSY_STALE_AGE_SECONDS + 300)}

        def main_once(env):
            delivered, saved = [], {}
            blank = {"warn": {}, "coarse": {}, "urgent": {}, "busy_stale": {}}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(msm, "observe", return_value=obs), \
                    mock.patch.object(msm, "load_state", return_value=blank), \
                    mock.patch.object(msm, "save_state", side_effect=lambda st, *a, **k: saved.update(st)), \
                    mock.patch.object(msm, "delivery_recency", return_value={}), \
                    mock.patch.object(msm, "load_drain_clock", return_value={}), \
                    mock.patch.object(msm, "drain_progress", return_value=({}, set())), \
                    mock.patch.object(msm, "save_drain_clock"), \
                    mock.patch.dict(os.environ, {"PANTHEON_ORG076_SAMPLES_DIR": str(self.drop)}), \
                    mock.patch.object(msm, "deliver", side_effect=lambda alerts, **k: delivered.extend(alerts)):
                self.assertEqual(msm.main(["--hub", str(hub)]), 0)
            saved = {**saved, "busy_stale": sorted(saved.get("busy_stale", {}))}  # drop wall-clock stamps
            return [(a.inbox, a.kind) for a in delivered], saved

        os.environ.pop(SWITCH, None)
        base = main_once({})
        self.assertEqual(list(self.drop.iterdir()), [])
        for value in ("", "0", "true", "1 ", "yes"):
            with self.subTest(value=value):
                self.assertEqual(main_once({SWITCH: value}), base)
                self.assertEqual(list(self.drop.iterdir()), [], "no drop file for a non-'1' switch")
        self.assertEqual(main_once({SWITCH: "1"}), base)  # on: alerting + state identical, one drop written
        self.assertEqual(len(list(self.drop.iterdir())), 1)

    def test_dry_run_writes_nothing(self):
        hub = Path(tempfile.mkdtemp(prefix="hub-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(hub, True))
        with mock.patch.dict(os.environ, {SWITCH: "1", "PANTHEON_ORG076_SAMPLES_DIR": str(self.drop)}), \
                mock.patch.object(msm, "observe", return_value={"mason": _obs(unread=1, pane_idle=True, age=5)}), \
                mock.patch.object(msm, "deliver"):
            self.assertEqual(msm.main(["--hub", str(hub), "--dry-run"]), 0)
        self.assertEqual(list(self.drop.iterdir()), [])

    def test_write_failure_is_swallowed_and_logged(self):
        missing = self.drop / "no-such-dir"
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = msm.feed_mail_delivery({"mason": _obs(unread=1, pane_idle=True, age=5)},
                                         drop=missing, env={SWITCH: "1"})
        self.assertEqual(out, [])
        self.assertIn("drop failed (ignored; monitor alerting unaffected)", err.getvalue())

    def test_main_survives_a_writer_that_raises(self):
        hub = Path(tempfile.mkdtemp(prefix="hub-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(hub, True))
        delivered = []
        with mock.patch.dict(os.environ, {SWITCH: "1"}), \
                mock.patch.object(omd, "feed_safely", side_effect=RuntimeError("boom")), \
                mock.patch.object(msm, "observe", return_value={"mason": _obs(unread=1, pane_idle=True, age=5)}), \
                mock.patch.object(msm, "save_state") as save, mock.patch.object(msm, "save_drain_clock"), \
                mock.patch.object(msm, "load_state", return_value={}), \
                mock.patch.object(msm, "load_drain_clock", return_value={}), \
                mock.patch.object(msm, "delivery_recency", return_value={}), \
                mock.patch.object(msm, "drain_progress", return_value=({}, set())), \
                mock.patch.object(msm, "deliver", side_effect=lambda alerts, **k: delivered.extend(alerts)):
            self.assertEqual(msm.main(["--hub", str(hub)]), 0)
        save.assert_called_once()  # state still persisted


class SuppressedAndSeats(unittest.TestCase):
    def setUp(self):
        self.drop = Path(tempfile.mkdtemp(prefix="mdd-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.drop, True))

    def test_held_muted_and_non_seat_inboxes_get_no_verdict_and_are_logged(self):
        import io
        import contextlib
        obs = {n: _obs(unread=1, pane_idle=True, age=5) for n in ("mason", "artisan", "orchestrator", "specialist-a2a")}
        err = io.StringIO()
        with mock.patch.object(msm, "_recipient_is_muted", side_effect=lambda n: n == "artisan"), \
                contextlib.redirect_stderr(err):
            names = msm.feed_mail_delivery({k: v for k, v in obs.items() if k != "orchestrator"}, held={"orchestrator"},
                                           drop=self.drop, env={SWITCH: "1"})
        self.assertEqual(len(names), 1)  # mason only
        (f,) = list(self.drop.iterdir())
        self.assertEqual(json.loads(f.read_text())["persona"], "mason")
        log = err.getvalue()
        self.assertIn("orchestrator: suppressed (held", log)
        self.assertIn("artisan: suppressed (muted)", log)
        self.assertIn("specialist-a2a: suppressed (not a seat inbox)", log)

    def test_seat_for_is_the_static_map(self):
        import tools.mailbox_wake as mw
        for persona, cfg in mw.AGENT_PANE.items():
            self.assertEqual(omd.seat_for(persona),
                             ("codex" if cfg.get("harness") == "codex" else "claude", Path(cfg["home"]).name))
        self.assertEqual(omd.seat_for("watchman"), ("codex", "codex-fallback"))
        self.assertEqual(omd.seat_for("recovery"), ("claude", "claude-fallback"))
        self.assertIsNone(omd.seat_for("specialist-a2a"))

    def test_registry_entry(self):
        e = run.REGISTRY[omd.DETECTOR]
        self.assertEqual((e.n, e.m, e.fault_kinds, e.freshness_unit), (3, 2, omd.FAULT_KINDS, None))
        self.assertEqual(e.domain_key("mason"), "mail-delivery:mason")
        self.assertEqual(e.spec_for("watchman", "codex-fallback").harness, "codex")
        self.assertEqual(e.spec_for("mason", "suborch1").harness, "claude")
        self.assertEqual(e.spec_for("mason", "suborch1").candidates, rev.REPAIR_CANDIDATES)


class EndToEnd(Rig):
    """monitor writer -> drop file -> run_cycle -> episode store, real production registry."""

    def setUp(self):
        super().setUp()
        self.k = 0
        for p in (mock.patch.object(msm, "_recipient_is_muted", return_value=False),
                  mock.patch.dict(os.environ, {SWITCH: "1"})):
            p.start()
            self.addCleanup(p.stop)

    def sample(self, obs, held=(), gap=300):
        self.k += 1
        return msm.feed_mail_delivery(obs, set(held), now=T0 + timedelta(seconds=gap * self.k), drop=self.drop)

    def stuck(self, **kw):
        return _obs(unread=1, pane_idle=True, age=400, **kw)

    def idle_busy(self):
        return {"mason": _obs(unread=1, pane_idle=False, age=400)}

    def dom(self, inbox="mason"):
        return es.load_domain(f"{DOMAIN}:{inbox}", self.state)

    def ticket_text(self):
        (rel,) = self.fx.origin_ticket_files()
        return git("show", f"main:{rel}", cwd=self.fx.origin).stdout

    def test_stuck_composer_opens_exactly_one_episode_and_one_ticket(self):  # T-5380 shape + s6.1
        for _ in range(5):  # N=3 opens; samples 4 and 5 must not open a second
            self.sample({"mason": self.stuck()})
            self.cycle()
        (ep,) = self.dom()["episodes"].values()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"], ep["delivery"]["phase"]),
                         ("mail-stale:mason", "broken", es.VERIFIED))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual(self.dropped(), [])

    def test_two_samples_open_nothing_then_third_does(self):
        for _ in range(2):
            self.sample({"mason": self.stuck()})
        self.cycle()
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.sample({"mason": self.stuck()})
        self.cycle()
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_flap_idle_busy_alternation_under_n_opens_nothing(self):  # s6.4
        for i in range(12):
            self.sample({"mason": self.stuck()} if i % 2 == 0 else self.idle_busy())
            self.cycle()
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.assertEqual(self.dom()["episodes"], {})

    def test_healed_under_ticket_sets_recovered_before_repair(self):  # s6.5
        for _ in range(3):
            self.sample({"mason": self.stuck()})
        self.cycle()
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        for _ in range(2):  # M=2 healthy samples
            self.sample({"mason": _obs()})
        self.cycle()
        (ep,) = self.dom()["episodes"].values()
        self.assertEqual((ep["state"], ep["recovered_before_repair"]), (es.RESOLVED, True))
        self.assertIn("recovered_before_repair: true", self.ticket_text())
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_always_none_pane_seat_with_routine_mail_opens_no_episode(self):
        for _ in range(8):
            self.sample({"watchman": _obs(unread=2, pane_idle=None, age=COARSE_S - 60)})
            self.cycle()
        self.assertEqual(self.fx.origin_ticket_files(), [])

    def test_pane_none_past_the_coarse_budget_opens_an_unknown_episode(self):
        for _ in range(3):
            self.sample({"watchman": _obs(unread=2, pane_idle=None, age=COARSE_S + 60)})
        self.cycle()
        (ep,) = self.dom("watchman")["episodes"].values()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("mail-stale-unmeasurable:watchman", "unknown"))

    def test_harness_comes_from_the_seat_not_the_record(self):
        for _ in range(3):
            self.sample({"watchman": self.stuck(), "mason": self.stuck()})
        self.cycle()
        (h,) = self.dom("watchman")["episodes"].values()
        (m,) = self.dom("mason")["episodes"].values()
        # codex-faulted watchman routes other_harness -> recovery; claude-faulted mason -> watchman
        self.assertEqual((h["delivery"]["route"]["kind"], h["delivery"]["route"]["target"]), ("other_harness", "recovery"))
        self.assertEqual((m["delivery"]["route"]["kind"], m["delivery"]["route"]["target"]),
                         ("other_harness", "watchman"))

    def test_two_invocations_a_second_apart_have_distinct_run_ids_and_both_ingest(self):
        self.sample({"mason": self.stuck()}, gap=0)
        first = json.loads(next(self.drop.iterdir()).read_text())
        msm.feed_mail_delivery({"mason": self.stuck()}, set(), now=T0 + timedelta(seconds=1), drop=self.drop)
        runs = {json.loads(p.read_text())["run_id"] for p in self.drop.iterdir()}
        self.assertEqual(len(runs), 2)
        self.assertIn(first["run_id"], runs)
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped), (2, 0))
        self.assertEqual(self.dom()["runs"][DOMAIN]["n"], 2)

    def test_replay_of_the_same_file_dedupes(self):
        self.sample({"mason": self.stuck()})
        (f,) = list(self.drop.iterdir())
        raw = f.read_bytes()
        (self.drop / ("20261102T000000000000__mason__replay__0001.json")).write_bytes(raw)
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped), (1, 1))

    # -- evidence (s6.15-6.17)
    def test_evidence_snapshot_has_counts_ages_and_inbox_name_only(self):  # s6.15
        hub = Path(tempfile.mkdtemp(prefix="hub-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(hub, True))
        canary = "CANARY-SUBJECT-Quarterly-payroll-7f3a"
        body_canary = "CANARY-BODY-hunter2-secret"
        for sub in ("new", "cur", "tmp"):
            (hub / "mason" / sub).mkdir(parents=True)
        (hub / "mason" / "cur" / "old").write_text("x")
        msgs = []
        for i in range(2):
            m = hub / "mason" / "new" / f"169000000{i}.msg"
            m.write_text(json.dumps({"from": "sender-persona-zed", "subject": canary, "body": body_canary}))
            os.utime(m, (time.time() - 7200, time.time() - 7200))
            msgs.append(m)
        for _ in range(3):
            obs = msm.observe(hub, pane_fn=lambda name, panes: True)
            self.sample({"mason": obs["mason"]})
        drops = "".join(p.read_text() for p in self.drop.iterdir())
        self.cycle()
        text = self.ticket_text() + drops
        for needle in (canary, body_canary, "sender-persona-zed", "Quarterly", "hunter2"):
            self.assertNotIn(needle, text)
        fm_snapshot = text[text.index("evidence_snapshot"):]
        self.assertIn("inbox=mason", fm_snapshot)
        self.assertIn("unread=2", fm_snapshot)
        self.assertRegex(fm_snapshot, r"oldest_age_s=7\d{3}")
        self.assertIn("pane=idle", fm_snapshot)

    def test_over_cap_evidence_defers_and_omits_the_snapshot(self):  # s6.16
        with mock.patch.object(rev, "make_default_redactor", return_value=lambda s: s + ("y" * 99 + "\n") * 400):
            for _ in range(3):
                self.sample({"mason": self.stuck()})
            self.cycle()
        text = self.ticket_text()
        self.assertIn("evidence_deferred: true", text)
        self.assertNotIn("evidence_snapshot", text)

    def test_detail_is_capped_at_the_envelope_limit(self):  # s6.16 (writer side)
        s = omd.Sample("mason", "broken", "k" * 5000, 1, 0, 1.0, "idle")
        self.assertLessEqual(len(omd.detail_for(s)), omd.qpd.DETAIL_LIMIT)

    def test_redactor_failure_fails_closed(self):  # s6.17
        def boom(_):
            raise RuntimeError("mid-emit")
        with mock.patch.object(rev, "make_default_redactor", return_value=boom):
            for _ in range(3):
                self.sample({"mason": self.stuck()})
            self.cycle()
        text = self.ticket_text()
        self.assertIn("evidence_deferred: true", text)
        self.assertNotIn("evidence_snapshot", text)

    # -- runner-side trust
    def test_runner_and_monitor_clones_that_disagree_on_a_seat_reject_and_page(self):
        self.sample({"mason": self.stuck()})
        import tools.mailbox_wake as mw
        changed = dict(mw.AGENT_PANE)
        changed["mason"] = {**changed["mason"], "home": "/home/some-other-account"}
        with mock.patch.object(mw, "AGENT_PANE", changed):
            rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (1, 0))
        self.assertTrue(any("account" in str(p) for p in self.pages), self.pages)
        self.assertTrue((self.drop / run.REJECTED_DIR).is_dir())
        self.assertEqual(self.dom()["episodes"], {})

    def test_unknown_seat_on_the_runner_is_rejected(self):
        self.sample({"mason": self.stuck()})
        import tools.mailbox_wake as mw
        without = {k: v for k, v in mw.AGENT_PANE.items() if k != "mason"}
        with mock.patch.object(mw, "AGENT_PANE", without):
            rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (1, 0))
        self.assertTrue(self.pages)


if __name__ == "__main__":
    unittest.main()
