"""ORG-076 BT2(a)+(c): quota-probe READABILITY detector, hook and dark switch (T-4708).

Hermetic: tmp state dirs, tmp git repo + local bare origin (tools/_episode_fixtures.py),
injected Ports/probe runners/adapters. No real ~/.local/state, mailbox, Telegram, tmux or push.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import episode_store as es
from tools import org076_store_runner as run
from tools import persona_bringup, quota_probe_detector as qpd, runtime_controller as rc
from tools._episode_fixtures import RepoFixture, FakeClock, make_ports
from tools.claude_quota_probe import ClaudeQuotaProbeError
from tools.test_runtime_controller import (
    REPO_ROOT, _Adapter, _binding, _quota_probe_result, _quota_snapshot,
)

NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
RUN = "20260803T000000Z"


def _evidence(probe):
    return {"authoritative_limit": "false", "provider": "claude", "path": probe.statusline_path,
            "sha256": str(probe.statusline_sha256), "session_id": probe.session_id}


class Rig:
    """A claude primary 'recovery' binding + helpers to run quota_check against fakes."""

    def __init__(self, tc: unittest.TestCase):
        self.tmp = tempfile.TemporaryDirectory(prefix="qpd-")
        tc.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.binding = _binding(self.root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
        self.fallback = _binding(self.root, harness="codex", profile="fallback")

    def probe(self, **kw):
        return _quota_probe_result(self.binding, self.state, **kw)

    def snapshot(self, probe, **kw):
        return _quota_snapshot(self.binding, self.root, used=50.0, remaining=50.0,
                               evidence=kw.pop("evidence", _evidence(probe)))

    def check(self, runner, probe=None, sink=None, now=NOW, snapshot=None, **kw):
        probe = probe or self.probe()
        snap = snapshot or self.snapshot(probe)
        return persona_bringup.quota_check(
            self.binding, adapter=_Adapter(self.binding, snap), fallback_binding=self.fallback,
            primary_binding=self.binding, state_dir=self.state, cwd=self.root, now=now,
            claude_probe_runner=runner, manifest_path=REPO_ROOT / "instance.yaml",
            outcome_sink=sink, **kw)


class MappingTests(unittest.TestCase):
    def test_outcome_mapping_table(self):
        P = "recovery"
        cases = [
            (qpd.ProbeOutcome(qpd.READ, RUN), es.Healthy),
            (qpd.ProbeOutcome(qpd.CAPPED, RUN, "cap sentinel"), es.Healthy),   # a cap is a successful READ
            (qpd.ProbeOutcome(qpd.PROBE_ERROR, RUN, "tmux died"), es.Unknown),
            (qpd.ProbeOutcome(qpd.CACHED_FAILURE, RUN), es.Unknown),
            (qpd.ProbeOutcome(qpd.PROBE_INVALID, RUN, "session_id"), es.Unknown),
            (qpd.ProbeOutcome(qpd.EVIDENCE_MISMATCH, RUN, "sha256"), es.Unknown),
        ]
        for outcome, cls in cases:
            v = qpd.outcome_to_verdict(outcome, P)
            self.assertIsInstance(v, cls, outcome.kind)
            if cls is es.Unknown:
                self.assertEqual(v.fault_id, "quota-probe-unreadable:recovery")
                self.assertIn(outcome.kind, v.evidence)
                self.assertIn(RUN, v.evidence)

    def test_spec_names_the_readability_domain_and_routes_to_watchman(self):
        b = SimpleNamespace(persona="recovery", account="claude-fallback")
        spec = qpd.spec_for(b)
        self.assertEqual(spec.detector, "claude-quota-probe-readability")
        self.assertEqual(spec.domain_key, "claude-quota-probe-readability:recovery")
        self.assertEqual(spec.subsystem, "claude-quota-probe-readability")
        self.assertEqual((spec.harness, spec.account, spec.requester), ("claude", "claude-fallback", "recovery"))
        self.assertEqual((spec.n, spec.m), (3, 2))
        (cand,) = spec.candidates
        self.assertEqual((cand.name, cand.harness, cand.account), ("watchman", "codex", "codex-fallback"))

    def test_domain_key_is_per_persona(self):
        a = qpd.spec_for(SimpleNamespace(persona="recovery", account="x"))
        b = qpd.spec_for(SimpleNamespace(persona="specialist", account="x"))
        self.assertNotEqual(a.domain_key, b.domain_key)

    def test_evidence_redaction_is_account_aware(self):
        spec = qpd.spec_for(SimpleNamespace(persona="recovery", account="claude-fallback"))
        out = spec.redactor("see /home/other-acct/.claude/x and /home/claude-fallback/y")
        self.assertNotIn("other-acct", out)
        self.assertIn("/home/claude-fallback/y", out)


class OutcomeSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self)
        self.sink: list = []

    def test_read_ok(self):
        r = self.rig.check(lambda **_: self.rig.probe(), sink=self.sink.append)
        self.assertEqual(r.decision, "stay")
        self.assertEqual([o.kind for o in self.sink], [qpd.READ])
        self.assertEqual(self.sink[0].run_id, RUN)

    def test_probe_exception(self):
        def boom(**_):
            raise ClaudeQuotaProbeError("tui never accepted")
        r = self.rig.check(boom, sink=self.sink.append)
        self.assertEqual((r.decision, r.reason), ("unknown", "probe-unavailable"))
        self.assertEqual([o.kind for o in self.sink], [qpd.PROBE_ERROR])
        self.assertIn("tui never accepted", self.sink[0].detail)

    def test_cached_failure_in_same_bucket(self):
        def boom(**_):
            raise ClaudeQuotaProbeError("x")
        self.rig.check(boom)
        r = self.rig.check(boom, sink=self.sink.append)
        self.assertEqual(r.reason, "probe-unavailable")
        self.assertEqual([o.kind for o in self.sink], [qpd.CACHED_FAILURE])

    def test_capped_probe(self):
        capped = self.rig.probe(status="capped", cap="You've hit your limit", pong=False)
        r = self.rig.check(lambda **_: capped, probe=capped, sink=self.sink.append)
        self.assertEqual(r.reason, "probe-unavailable")  # quota_check behaviour unchanged
        self.assertEqual([o.kind for o in self.sink], [qpd.CAPPED])

    def test_identity_mismatch(self):
        bad = self.rig.probe(session_id="not-the-session")
        r = self.rig.check(lambda **_: bad, probe=bad, sink=self.sink.append)
        self.assertEqual(r.reason, "probe-unavailable")
        self.assertEqual([o.kind for o in self.sink], [qpd.PROBE_INVALID])
        self.assertIn("session_id", self.sink[0].detail)

    def test_statusline_evidence_mismatch(self):
        probe = self.rig.probe()
        snap = self.rig.snapshot(probe, evidence={**_evidence(probe), "sha256": "b" * 64})
        r = self.rig.check(lambda **_: probe, probe=probe, snapshot=snap, sink=self.sink.append)
        self.assertEqual(r.reason, "probe-unavailable")
        self.assertEqual([o.kind for o in self.sink], [qpd.EVIDENCE_MISMATCH])

    def test_non_measurements_take_no_sample(self):
        # unsupported
        # T-5446: no persona allowlist; "unsupported" now means no manifest fallback.
        saved = self.rig.binding.controller
        self.rig.binding.controller = dataclasses.replace(
            saved, policy=dataclasses.replace(saved.policy, fallbacks=()))
        r = self.rig.check(lambda **_: self.rig.probe(), sink=self.sink.append)
        self.assertEqual(r.decision, "unsupported")
        self.rig.binding.controller = saved
        # operation-busy
        with mock.patch.object(rc, "persona_operation_lock", side_effect=rc.OperationLockBusy("busy")):
            r = self.rig.check(lambda **_: self.rig.probe(), sink=self.sink.append)
        self.assertEqual(r.reason, "operation-busy")
        # transition active
        with mock.patch.object(rc, "transition_state", return_value={"phase": "preparing"}):
            r = self.rig.check(lambda **_: self.rig.probe(), sink=self.sink.append)
        self.assertEqual(r.decision, "transition-active-noop")
        # fallback active
        with mock.patch.object(rc, "transition_state",
                               return_value={"phase": "completed", "fallback_active": True}):
            r = self.rig.check(lambda **_: self.rig.probe(), sink=self.sink.append)
        self.assertEqual(r.decision, "fallback-active-noop")
        self.assertEqual(self.sink, [])

    def test_quota_check_result_identical_with_and_without_sink(self):
        a = self.rig.check(lambda **_: self.rig.probe())
        b = self.rig.check(lambda **_: self.rig.probe(), sink=[].append)
        self.assertEqual(a, b)

    def test_refresh_primary_quota_wrapper_returns_unchanged(self):
        probe = self.rig.probe()
        snap = self.rig.snapshot(probe)
        got = persona_bringup.refresh_primary_quota(
            self.rig.binding, state_dir=self.rig.state, now=NOW,
            claude_probe_runner=lambda **_: probe, adapter=_Adapter(self.rig.binding, snap))
        self.assertIs(got, snap)
        rig2 = Rig(self)

        def boom(**_):
            raise ClaudeQuotaProbeError("x")
        self.assertIsNone(persona_bringup.refresh_primary_quota(
            rig2.binding, state_dir=rig2.state, now=NOW, claude_probe_runner=boom,
            adapter=_Adapter(rig2.binding)))

    def test_non_claude_primary_takes_no_sample(self):
        rig = Rig(self)
        codex = _binding(rig.root, harness="codex", profile="fallback")
        _, outcome = persona_bringup.refresh_primary_quota_detailed(
            codex, state_dir=rig.state, now=NOW, adapter=_Adapter(codex))
        self.assertIsNone(outcome)


class DarkSwitchTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self)
        self.sd = tempfile.TemporaryDirectory(prefix="qpd-sd-")
        self.addCleanup(self.sd.cleanup)
        p = mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": self.sd.name})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(qpd.ENV_SWITCH, None)

    def run_cli(self, runner, switch=None, extra_patches=()):
        env = {qpd.ENV_SWITCH: switch} if switch is not None else {}
        out = io.StringIO()
        probe = self.rig.probe()
        snap = self.rig.snapshot(probe)
        argv = ["persona_bringup.py", "quota-check", "--persona", "recovery", "--state-dir", str(self.rig.state),
                "--mailbox-hub", str(self.rig.root / "mail")]
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(os.environ, env))
            st.enter_context(mock.patch.object(__import__("sys"), "argv", argv))
            st.enter_context(mock.patch.object(persona_bringup, "load_binding", return_value=self.rig.binding))
            st.enter_context(mock.patch.object(persona_bringup, "quota_check_supported", return_value=(True, "ok")))
            st.enter_context(mock.patch.object(persona_bringup, "first_fallback_binding", return_value=self.rig.fallback))
            st.enter_context(mock.patch.object(persona_bringup, "build_adapter",
                                               return_value=_Adapter(self.rig.binding, snap)))
            st.enter_context(mock.patch.object(persona_bringup, "run_claude_quota_probe", runner))
            st.enter_context(mock.patch.object(persona_bringup, "quota_check", wraps=persona_bringup.quota_check))
            for p in extra_patches:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(out))
            code = persona_bringup.main()
        return code, out.getvalue()

    def _boom(self, **_):
        raise ClaudeQuotaProbeError("down")

    def test_deep_sleeping_seat_takes_no_probe_no_transition_and_no_feeder_record(self):
        # ORG-106 L3a: quota-check would start an ephemeral Claude TUI under the sleeping
        # seat's account (and could begin a planned transition). It must skip, with the
        # detector switched ON, before quota_check_supported / the probe / the feeder.
        runner = mock.Mock()
        feed = mock.Mock()
        with mock.patch.object(qpd, "feed_safely", feed), \
                mock.patch.object(persona_bringup, "_seat_deep_sleeping", return_value=True), \
                mock.patch.object(persona_bringup, "quota_check_supported") as supported:
            code, out = self.run_cli(runner, switch="1")
        self.assertEqual(code, 0)
        self.assertIn('"seat-sleep"', out)
        self.assertIn('"acted": false', out)
        runner.assert_not_called()
        supported.assert_not_called()
        feed.assert_not_called()
        self.assertEqual(os.listdir(self.sd.name), [])

    def test_default_off_no_observe_no_state_output_identical(self):
        feed = mock.Mock()
        with mock.patch.object(qpd, "feed", feed):
            code_unset, out_unset = self.run_cli(self._boom)
            code_zero, out_zero = self.run_cli(self._boom, switch="0")
            code_true, out_true = self.run_cli(self._boom, switch="true")
        feed.assert_not_called()
        self.assertEqual((code_unset, code_zero, code_true), (0, 0, 0))
        self.assertEqual(out_unset, out_zero)
        self.assertEqual(out_unset, out_true)
        self.assertEqual(os.listdir(self.sd.name), [])  # no store, heartbeat or dedupe state

    def test_off_path_does_not_even_import_episode_store(self):
        import subprocess
        import sys
        code = ("import sys; from tools import persona_bringup, quota_probe_detector;"
                "print('tools.episode_store' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_on_output_and_exit_identical_to_off(self):
        with mock.patch.object(qpd, "feed", mock.Mock()):
            _, off = self.run_cli(self._boom)
            code, on = self.run_cli(self._boom, switch="1")
        self.assertEqual((code, on), (0, off))

    def test_on_feeds_after_quota_check_returned_and_lock_released(self):
        seen = {}

        def fake_feed(binding, outcome, **kw):
            seen["kind"] = outcome.kind
            # the persona operation lock must be free here: never held across the store drive
            with rc.persona_operation_lock(binding.persona, state_root=self.rig.state, blocking=False,
                                           timeout_seconds=0.0):
                seen["lock_free"] = True
            seen["after_check"] = persona_bringup.quota_check.call_count == 1

        with mock.patch.object(qpd, "feed", fake_feed):
            code, _ = self.run_cli(self._boom, switch="1")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"kind": qpd.PROBE_ERROR, "lock_free": True, "after_check": True})

    def test_detector_exception_never_changes_exit_or_output(self):
        def boom(*a, **k):
            raise RuntimeError("store exploded")
        with mock.patch.object(qpd, "feed", boom):
            _, off = self.run_cli(self._boom)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, on = self.run_cli(self._boom, switch="1")
        self.assertEqual((code, on), (0, off))
        self.assertIn("store exploded", err.getvalue())

    def test_missing_drop_dir_never_changes_exit_or_output(self):
        gone = Path(self.sd.name) / "no-such-drop-dir"
        with mock.patch.dict(os.environ, {qpd.ENV_DROP_DIR: str(gone)}):
            _, off = self.run_cli(self._boom)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, on = self.run_cli(self._boom, switch="1")
        self.assertEqual((code, on), (0, off))
        self.assertIn("detector failed", err.getvalue())
        self.assertFalse(gone.exists())

    def test_on_writes_one_drop_file_and_no_store_state(self):
        drop = Path(self.sd.name) / "drop"
        drop.mkdir()
        with mock.patch.dict(os.environ, {qpd.ENV_DROP_DIR: str(drop)}):
            code, _ = self.run_cli(self._boom, switch="1")
        self.assertEqual(code, 0)
        (f,) = list(drop.iterdir())
        self.assertEqual(json.loads(f.read_text())["outcome_kind"], qpd.PROBE_ERROR)
        self.assertEqual([n for n in os.listdir(self.sd.name) if n != "drop"], [])  # no seat-side store

    def _busy(self):
        return mock.patch.object(rc, "persona_operation_lock", side_effect=rc.OperationLockBusy("b"))

    def test_no_measurement_writes_a_not_measured_record_when_on_and_nothing_when_off(self):
        feed = mock.Mock()
        with mock.patch.object(qpd, "feed", feed):
            self.run_cli(self._boom, extra_patches=[self._busy()])          # switch OFF
            feed.assert_not_called()
            self.run_cli(self._boom, switch="1", extra_patches=[self._busy()])
        feed.assert_called_once()
        outcome = feed.call_args.args[1]
        self.assertEqual((outcome.kind, outcome.run_id), (qpd.NOT_MEASURED, None))
        self.assertIn("operation-busy", outcome.detail)

    def test_unsupported_run_also_writes_not_measured_when_on(self):
        feed = mock.Mock()
        no = mock.patch.object(persona_bringup, "quota_check_supported", return_value=(False, "not a claude seat"))
        with mock.patch.object(qpd, "feed", feed):
            _, off = self.run_cli(self._boom, extra_patches=[no])
            feed.assert_not_called()
            code, on = self.run_cli(self._boom, switch="1", extra_patches=[no])
        self.assertEqual((code, on), (0, off))  # output + exit byte-identical, switch on or off
        outcome = feed.call_args.args[1]
        self.assertEqual(outcome.kind, qpd.NOT_MEASURED)
        self.assertIn("unsupported", outcome.detail)

    def test_not_measured_failure_is_swallowed_like_any_other_feed(self):
        def boom(*a, **k):
            raise RuntimeError("drop dir gone")
        with mock.patch.object(qpd, "feed", boom):
            _, off = self.run_cli(self._boom, extra_patches=[self._busy()])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, on = self.run_cli(self._boom, switch="1", extra_patches=[self._busy()])
        self.assertEqual((code, on), (0, off))
        self.assertIn("drop dir gone", err.getvalue())

    def test_not_measured_drop_file_is_a_valid_minimal_record(self):
        drop = Path(self.sd.name) / "drop"
        drop.mkdir()
        with mock.patch.dict(os.environ, {qpd.ENV_DROP_DIR: str(drop)}):
            self.run_cli(self._boom, switch="1", extra_patches=[self._busy()])
        (f,) = list(drop.iterdir())
        body = json.loads(f.read_text())
        self.assertEqual((body["outcome_kind"], body["run_id"]), (qpd.NOT_MEASURED, None))
        self.assertEqual(run.parse_sample(f.read_bytes())["persona"], "recovery")  # the runner accepts it

    def test_page_path_untouched(self):
        # quota-check never pages (always rc 0); consume's soft-persistent code is unchanged
        self.assertEqual(persona_bringup.EXIT_SOFT_PERSISTENT, 6)
        self.assertEqual(persona_bringup.SOFT_FAILURE_ALERT_STREAK, 3)
        code, _ = self.run_cli(self._boom, switch="1", extra_patches=[mock.patch.object(qpd, "feed", mock.Mock())])
        self.assertEqual(code, 0)


class EpisodeFlowTests(unittest.TestCase):
    """drop -> recovery runner ingest -> store, end to end against a real temp repo + bare origin and the
    REAL ticket.py file (the 'clone' is the fixture's working repo)."""

    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.sd = tempfile.TemporaryDirectory(prefix="qpd-sd-")
        self.addCleanup(self.sd.cleanup)
        self.drop = Path(self.sd.name) / "drop"
        self.drop.mkdir()
        self.state = Path(self.sd.name) / "state"
        env = {k: self.fx.env[k] for k in ("PANTHEON_TICKET_ALLOC_LOCKFILE", "PANTHEON_GIT_MUTATION_LOCKFILE",
                                           "PANTHEON_ACTOR")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.pages: list = []
        self.clock = FakeClock()
        self.binding = SimpleNamespace(persona="recovery", account="claude-fallback")
        self.spec = qpd.spec_for(self.binding)
        self.cfg = run.Config(clone=self.fx.repo, drop=self.drop, state_dir=self.state,
                              heartbeat=Path(self.sd.name) / "hb" / "x.json")
        self.n = 0

    def ports(self):
        e = es.make_rebase_edge(repo=self.fx.repo, state_dir=self.state,
                                page=lambda s, b: self.pages.append((s, b)))
        return make_ports(self.fx, clock=self.clock, pages=self.pages, pusher=None, mirror_url=None,
                          on_nonff=e.on_nonff, on_recover=e.on_recover, heartbeat_path=self.cfg.heartbeat)

    def feed(self, kind, run_id=None, binding=None):
        """Seat drops one sample, recovery ingests it; returns the store's cycle status."""
        self.n += 1
        run_id = run_id or f"20260803T{self.n:06d}Z"
        qpd.feed(binding or self.binding, qpd.ProbeOutcome(kind, run_id, "detail"), drop=self.drop,
                 now=datetime(2026, 8, 3, tzinfo=timezone.utc) + timedelta(seconds=self.n))
        real = es.observe
        statuses = []

        def spy(*a, **k):
            r = real(*a, **k)
            if a[0].detector == qpd.DETECTOR:  # BT3b: the runner also feeds the meta-detector domains
                statuses.append(r.status)
            return r

        with mock.patch.object(es, "observe", spy):
            run.run_cycle(self.cfg, self.ports())
        return statuses[-1] if statuses else None

    def episodes(self):
        return list(es.load_domain(self.spec.domain_key, self.state)["episodes"].values())

    def test_three_unreadable_buckets_open_one_episode_and_one_ticket(self):
        statuses = [self.feed(qpd.PROBE_ERROR) for _ in range(3)]
        self.assertEqual(statuses, ["counted", "counted", "opened"])
        (ep,) = self.episodes()
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(ep["verdict_kind"], "unknown")
        self.assertEqual(ep["fault_id"], "quota-probe-unreadable:recovery")
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)
        (name,) = self.fx.origin_ticket_files()
        text = es.subprocess.run(["git", "show", f"main:{name}"], cwd=self.fx.origin,
                                 capture_output=True, text=True).stdout
        self.assertIn("requester: recovery", text)       # real ticket.py file argv validation accepted it
        # routed via the store's resolve_route (no bespoke routing); ticket.py normalises the
        # T-5058: the recovery duty is watchman's (ORG-048 A1). T-5239: targets are roles, so the
        # ticket pools to role:system_orchestrator (recovery + watchman); repair_route still names watchman.
        self.assertIn("target: role:system_orchestrator", text)
        self.assertIn("repair_route: watchman@codex", text)
        self.assertEqual(self.pages, [])

    def test_suppression_while_open_no_second_ticket(self):
        for _ in range(3):
            self.feed(qpd.PROBE_ERROR)
        for _ in range(4):
            self.assertEqual(self.feed(qpd.PROBE_ERROR), "suppressed")
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_same_bucket_not_double_counted(self):
        self.assertEqual(self.feed(qpd.PROBE_ERROR, "20260803T000000Z"), "counted")
        self.assertEqual(self.feed(qpd.PROBE_ERROR, "20260803T000000Z"), "deduped")
        self.assertEqual(self.feed(qpd.PROBE_ERROR, "20260803T000000Z"), "deduped")
        self.assertEqual(self.episodes(), [])
        self.assertEqual(self.feed(qpd.PROBE_ERROR, "20260803T003000Z"), "counted")

    def test_one_healthy_blip_resets_counting(self):
        self.feed(qpd.PROBE_ERROR)
        self.feed(qpd.PROBE_ERROR)
        self.feed(qpd.READ)
        self.assertEqual(self.feed(qpd.PROBE_ERROR), "counted")
        self.assertEqual(self.episodes(), [])

    def test_two_healthy_close_then_new_fault_gets_fresh_episode_and_ticket(self):
        for _ in range(3):
            self.feed(qpd.PROBE_ERROR)
        self.assertEqual(self.feed(qpd.READ), "healthy")
        self.assertEqual(self.feed(qpd.CAPPED), "resolved")  # capped is a successful read -> Healthy
        for _ in range(3):
            self.feed(qpd.PROBE_ERROR)
        eps = self.episodes()
        self.assertEqual(sorted(e["state"] for e in eps), ["EMITTED", "RESOLVED"])
        self.assertEqual(len({e["episode_id"] for e in eps}), 2)
        self.assertEqual(len(self.fx.origin_ticket_files()), 2)

    def test_domains_are_isolated_per_persona(self):
        other = SimpleNamespace(persona="specialist", account="claude")
        for _ in range(2):
            self.feed(qpd.PROBE_ERROR)
        self.feed(qpd.PROBE_ERROR, "20260803T999999Z", binding=other)
        self.assertEqual(self.episodes(), [])

    def test_state_lives_under_recovery_state_dir_and_drop_is_consumed(self):
        self.feed(qpd.READ)
        names = set(os.listdir(self.state))
        self.assertTrue({"episode-store", "org076-runner"} <= names)
        self.assertEqual([p for p in self.drop.iterdir()], [])


if __name__ == "__main__":
    unittest.main()
