#!/usr/bin/env python3
"""Tests for tui_wedge_sampler (T-1369) — the measurement that has to be right
before a restart-capable threshold can be set from it."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.tui_wedge_sampler as tws  # noqa: E402


def _s(ts, pane, mtime, agent="orchestrator"):
    return {"agent": agent, "ts": ts, "pane": pane, "mtime": mtime}


class FrozenRunsTest(unittest.TestCase):
    def test_pane_moving_while_jsonl_frozen_is_one_run(self):
        runs = tws.frozen_runs([
            _s(0, "a", 100), _s(10, "b", 100), _s(20, "c", 100), _s(30, "d", 200),
        ])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["seconds"], 20)
        self.assertFalse(runs[0].get("open"))

    def test_jsonl_advancing_is_NOT_a_run(self):
        """The session is emitting — that is the healthy case this must ignore."""
        self.assertEqual(tws.frozen_runs([
            _s(0, "a", 100), _s(10, "b", 110), _s(20, "c", 120)]), [])

    def test_a_still_pane_is_NOT_a_run(self):
        """Idle: nothing moving, nothing emitting. The healthy-idle guard's
        territory, not this one's."""
        self.assertEqual(tws.frozen_runs([
            _s(0, "a", 100), _s(10, "a", 100), _s(20, "a", 100)]), [])

    def test_run_is_measured_from_the_LAST_UNAMBIGUOUS_sample(self):
        """The ambiguity starts at the sample before the first moved pair, not at
        the moved sample — otherwise every run under-reports by one interval and
        the ceiling comes out low, which is the dangerous direction."""
        runs = tws.frozen_runs([_s(100, "a", 5), _s(160, "b", 5), _s(220, "c", 9)])
        self.assertEqual(runs[0]["start_ts"], 100)
        self.assertEqual(runs[0]["seconds"], 60)

    def test_a_failed_capture_BREAKS_the_run_rather_than_bridging_it(self):
        """A None pane is not evidence of stillness. Bridging over it would
        inflate the very number this tool exists to bound."""
        runs = tws.frozen_runs([
            _s(0, "a", 100), _s(10, "b", 100),
            _s(20, None, 100),
            _s(30, "c", 100), _s(40, "d", 100), _s(50, "e", 100), _s(60, "f", 200),
        ])
        # Two SEPARATE runs of 10s and 20s — not one bridged 50s run. The
        # lengths are deliberately different so a bug that merged or split them
        # cannot pass by coincidence.
        self.assertEqual([r["seconds"] for r in runs], [10, 20])

    def test_an_unfinished_run_is_flagged_OPEN(self):
        runs = tws.frozen_runs([_s(0, "a", 1), _s(10, "b", 1), _s(20, "c", 1)])
        self.assertTrue(runs[0]["open"])

    def test_empty_and_single_sample_are_silent(self):
        self.assertEqual(tws.frozen_runs([]), [])
        self.assertEqual(tws.frozen_runs([_s(0, "a", 1)]), [])


class SummarizeTest(unittest.TestCase):
    def test_no_runs_reports_no_ceiling_rather_than_zero(self):
        """max_seconds=0 would read as 'measured, and it is zero'. It is not
        measured at all — the guard-vacuity trap in summary form."""
        s = tws.summarize([])
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["max_seconds"])

    def test_an_OPEN_longest_run_is_reported_as_a_lower_bound(self):
        """Reading an open run as a ceiling is how a threshold ends up under the
        real one — i.e. how this tool would cause the restart it exists to
        prevent."""
        s = tws.summarize([
            {"seconds": 30, "open": False}, {"seconds": 900, "open": True}])
        self.assertEqual(s["max_seconds"], 900)
        self.assertTrue(s["longest_was_open"])

    def test_closed_longest_run_is_a_real_ceiling(self):
        s = tws.summarize([{"seconds": 400, "open": False}])
        self.assertFalse(s["longest_was_open"])


class FingerprintTest(unittest.TestCase):
    def test_none_capture_stays_none(self):
        self.assertIsNone(tws.pane_fingerprint(None))

    def test_same_text_same_digest_different_text_differs(self):
        self.assertEqual(tws.pane_fingerprint("x"), tws.pane_fingerprint("x"))
        self.assertNotEqual(tws.pane_fingerprint("x"), tws.pane_fingerprint("y"))

    def test_empty_pane_is_a_digest_not_None(self):
        """An empty capture is a real observation ('the pane is blank'); only a
        FAILED capture is None. Collapsing them would make a broken probe look
        like a still screen."""
        self.assertIsNotNone(tws.pane_fingerprint(""))


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = self.d / "samples.jsonl"

    def test_append_then_load_preserves_order_and_filters_by_agent(self):
        tws.append_samples(self.p, [_s(2, "b", 1), _s(1, "a", 1, agent="specialist")])
        self.assertEqual([r["ts"] for r in tws.load_samples(self.p)], [1, 2])
        self.assertEqual([r["agent"] for r in tws.load_samples(self.p, "orchestrator")],
                         ["orchestrator"])

    def test_a_torn_line_does_not_lose_the_history(self):
        tws.append_samples(self.p, [_s(1, "a", 1)])
        with self.p.open("a") as fh:
            fh.write('{"agent": "orchestrator", "ts":\n')
        tws.append_samples(self.p, [_s(2, "b", 1)])
        self.assertEqual([r["ts"] for r in tws.load_samples(self.p)], [1, 2])

    def test_absent_file_is_empty_not_an_error(self):
        self.assertEqual(tws.load_samples(self.d / "nope.jsonl"), [])


class SamplerIsReadOnlyTest(unittest.TestCase):
    """The safety property: this tool must never be able to act on an agent."""

    def test_it_holds_no_send_or_alert_CALLABLE(self):
        """Checked against the module NAMESPACE, not the source text.

        The first version of this test grepped the file for words including
        'restart' and failed on the module's own docstring, which explains why a
        restart threshold has to be measured. Prose about an authority is not the
        authority; what matters is whether a callable is reachable from here.
        """
        reachable = {n for n in dir(tws) if callable(getattr(tws, n, None))}
        for forbidden in ("send_poke", "send_modal_confirm", "send_keys",
                          "route_failure_alert", "_route_failure_alert",
                          "restart_agent"):
            self.assertNotIn(forbidden, reachable,
                             f"sampler must not be able to call {forbidden}")

    def test_it_does_not_shell_out_to_send_keys(self):
        """The one string form worth checking, because it bypasses the namespace:
        a raw `tmux send-keys` in a subprocess argument."""
        src = (REPO_ROOT / "tools" / "tui_wedge_sampler.py").read_text()
        self.assertNotIn("send-keys", src)

    def test_sample_agent_with_no_pane_records_None_without_capturing(self):
        with patch("tools.tui_wedge_sampler.capture_pane") as cap, \
             patch("tools.tui_wedge_sampler.jsonl_turn_signal", return_value=7):
            rec = tws.sample_agent("specialist-a2a", {"sudo_user": "x"})
        cap.assert_not_called()
        self.assertIsNone(rec["pane"])
        self.assertEqual(rec["mtime"], 7)

    def test_sample_agent_records_a_fingerprint_not_the_pane_text(self):
        secret = "sibling agent working content that must not be copied"
        with patch("tools.tui_wedge_sampler.capture_pane", return_value=secret), \
             patch("tools.tui_wedge_sampler.jsonl_turn_signal", return_value=1):
            rec = tws.sample_agent("orchestrator", {"tmux_target_pane": "p", "sudo_user": "u"})
        self.assertNotIn(secret, json.dumps(rec))
        self.assertEqual(rec["pane"], tws.pane_fingerprint(secret))


class SamplerTargetsAreIndependentOfTheActuator(unittest.TestCase):
    """The sampler must NOT inherit activity_monitor's AGENT_CONFIG.

    That config lists the agents which get POKED. Inheriting it capped this
    measurement at one agent (3 usable runs in 8.5h — far too thin for a
    threshold whose action is a RESTART), and the obvious fix — adding recovery to
    AGENT_CONFIG — would have enrolled recovery in the POKE path, violating T-0559.
    A read-only measurement must never be able to widen an actuator's blast
    radius as a side effect of wanting more data.
    """

    def test_recovery_is_sampled_but_NOT_pokeable(self):
        from tools import activity_monitor as am
        self.assertIn("recovery", tws.SAMPLE_TARGETS)
        self.assertNotIn("recovery", am.AGENT_CONFIG,
                         "recovery entered the POKE path — T-0559 says recovery is never "
                         "self-injected; sampling must not have done this")

    def test_the_two_lists_are_separate_objects(self):
        from tools import activity_monitor as am
        self.assertIsNot(tws.SAMPLE_TARGETS, am.AGENT_CONFIG)

    def test_every_target_has_what_sampling_needs(self):
        for agent, cfg in tws.SAMPLE_TARGETS.items():
            with self.subTest(agent=agent):
                self.assertTrue(cfg.get("tmux_target_pane"))
                self.assertTrue(cfg.get("jsonl_dir"))

    def test_jsonl_dirs_follow_the_seat_launch_shape(self):
        # T-4159: derived from agent_role_map, not literals. The old recovery literal
        # pointed at -pantheon-config while recovery was bespoke (no jsonl there).
        from tools import agent_role_map
        for agent, account in (("orchestrator", "claude"), ("recovery", "claude-fallback")):
            with self.subTest(agent=agent):
                self.assertEqual(tws.SAMPLE_TARGETS[agent]["jsonl_dir"],
                                 agent_role_map.live_session_projects_dir(account))


class SelfCaptureAvoidsSudo(unittest.TestCase):
    """`sudo -nu <self>` is DENIED by policy — an account has no sudo to itself —
    so the self case cannot go through capture_pane(), which always sudos.

    ⚠️ EVERY TEST HERE PINS `getpass.getuser`. sample_agent decides self-vs-foreign
    with `user == getpass.getuser()`, so a config's sudo_user is only "foreign"
    relative to whoever is RUNNING the suite. Left unpinned, `sudo_user: "claude"`
    is foreign on recovery's seat and self on orchestrator's — and the foreign-path test below
    passed as recovery and failed as orchestrator at the same sha (T-2445), which is the worst
    shape a test can fail in: it looks like it works. Pinning the identity is what
    makes these tests about the ROUTING RULE rather than about the account that
    happened to invoke pytest."""

    def test_self_target_uses_the_direct_path_not_capture_pane(self):
        # sudo_user None short-circuits before the comparison, so this arm never
        # depended on the running account. Pinned anyway, so the whole class states
        # its identity assumption in one place rather than one test being subtly
        # different from its neighbours.
        cfg = {"tmux_target_pane": "p:0", "sudo_user": None, "jsonl_dir": "/tmp"}
        with patch.object(tws.getpass, "getuser", return_value="somebody-else"), \
             patch.object(tws, "capture_pane") as cp, \
             patch.object(tws, "_capture_self", return_value="X") as direct, \
             patch.object(tws, "_mtime_self", return_value=7):
            rec = tws.sample_agent("recovery", cfg)
        cp.assert_not_called()
        direct.assert_called_once()
        self.assertEqual(rec["pane"], tws.pane_fingerprint("X"))
        self.assertEqual(rec["mtime"], 7)

    def test_named_sudo_user_that_IS_the_running_account_takes_the_direct_path(self):
        """The arm the other two do not cover, and the one that actually bites.

        A config can name a sudo_user explicitly and still be self — that is the
        live case on orchestrator's seat, where SAMPLE_TARGETS["orchestrator"]["sudo_user"] is
        "claude" and the sampler runs as claude. `sudo -nu claude` from claude is
        denied, so taking the capture_pane path here would fail in production while
        every test stayed green."""
        cfg = {"tmux_target_pane": "p:0", "sudo_user": "claude", "jsonl_dir": "/x"}
        with patch.object(tws.getpass, "getuser", return_value="claude"), \
             patch.object(tws, "capture_pane") as cp, \
             patch.object(tws, "_capture_self", return_value="Z") as direct, \
             patch.object(tws, "_mtime_self", return_value=11):
            rec = tws.sample_agent("orchestrator", cfg)
        cp.assert_not_called()
        direct.assert_called_once()
        self.assertEqual(rec["pane"], tws.pane_fingerprint("Z"))
        self.assertEqual(rec["mtime"], 11)

    def test_foreign_target_still_uses_capture_pane(self):
        """The control: without this, routing EVERYTHING through the direct path
        would pass the test above and silently break cross-account sampling."""
        cfg = {"tmux_target_pane": "p:0", "sudo_user": "claude", "jsonl_dir": "/x"}
        with patch.object(tws.getpass, "getuser", return_value="recovery"), \
             patch.object(tws, "capture_pane", return_value="Y") as cp, \
             patch.object(tws, "_capture_self") as direct, \
             patch.object(tws, "jsonl_turn_signal", return_value=9):
            rec = tws.sample_agent("orchestrator", cfg)
        cp.assert_called_once()
        direct.assert_not_called()
        self.assertEqual(rec["pane"], tws.pane_fingerprint("Y"))


if __name__ == "__main__":
    unittest.main()
