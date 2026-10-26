"""Tests for tools/activity_monitor_drift_check.py (T-3072 activity_monitor
meta-drift alarm).

Isolation pattern mirrors tools/test_priority_list_health_watchdog.py:
FIRES_PATH / BOOTSTRAP_PATH / ALERT_STATE_PATH patched onto per-test tmp
paths; the alert-transmit boundaries (route_failure_alert, tools.mailbox.send)
are patched directly rather than exercising real delivery.

Run from repo root:
    python3 -m unittest tools.test_activity_monitor_drift_check -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.activity_monitor_drift_check as dc

NOW = time.time()
DAY = 86400.0


def _fire(agent: str, predicate: str, ts: float) -> dict:
    return {"ts": int(ts), "agent": agent, "predicate": predicate}


class _DriftCheckBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        for attr, name in (
            ("FIRES_PATH", "fires.jsonl"),
            ("BOOTSTRAP_PATH", "bootstrap.json"),
            ("FIRST_SEEN_PATH", "first_seen.json"),
            ("ALERT_STATE_PATH", "alert_state.json"),
        ):
            patcher = mock.patch.object(dc, attr, self.tmp_path / name)
            patcher.start()
            self.addCleanup(patcher.stop)

        # T-5439: default the quiescence seams to "not in maintenance" and
        # "holds unknown" so no test reads the live registry or sudo-cats a
        # real holds.json; the T-5439 tests override them per case.
        for attr, kwargs in (
            ("_seat_in_maintenance", {"return_value": False}),
            ("_read_persona_holds", {"return_value": None}),
        ):
            patcher = mock.patch.object(dc, attr, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_fires(self, entries: list[dict]) -> None:
        dc.FIRES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with dc.FIRES_PATH.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# read_fires / last_fire_by_key
# ---------------------------------------------------------------------------


class TestReadFires(_DriftCheckBase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(dc.read_fires(dc.FIRES_PATH), [])

    def test_reads_all_wellformed_lines(self) -> None:
        self._write_fires([_fire("orchestrator", "p1a", NOW), _fire("orchestrator", "p1b", NOW - 100)])
        self.assertEqual(len(dc.read_fires(dc.FIRES_PATH)), 2)

    def test_malformed_lines_skipped_not_raised(self) -> None:
        dc.FIRES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with dc.FIRES_PATH.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_fire("orchestrator", "p1a", NOW)) + "\n")
            f.write("{not json\n")
            f.write(json.dumps(_fire("orchestrator", "p1b", NOW)) + "\n")
        self.assertEqual(len(dc.read_fires(dc.FIRES_PATH)), 2)


class TestLastFireByKey(unittest.TestCase):
    def test_empty_list_returns_empty_dict(self) -> None:
        self.assertEqual(dc.last_fire_by_key([]), {})

    def test_single_fire(self) -> None:
        fires = [_fire("orchestrator", "p1a", 1000)]
        self.assertEqual(dc.last_fire_by_key(fires), {("orchestrator", "p1a"): 1000})

    def test_multiple_fires_takes_max_ts(self) -> None:
        fires = [
            _fire("orchestrator", "p1a", 1000),
            _fire("orchestrator", "p1a", 3000),
            _fire("orchestrator", "p1a", 2000),
        ]
        self.assertEqual(dc.last_fire_by_key(fires), {("orchestrator", "p1a"): 3000})

    def test_distinct_keys_tracked_independently(self) -> None:
        fires = [_fire("orchestrator", "p1a", 1000), _fire("orchestrator", "p1b", 2000)]
        self.assertEqual(
            dc.last_fire_by_key(fires),
            {("orchestrator", "p1a"): 1000, ("orchestrator", "p1b"): 2000},
        )

    def test_malformed_records_skipped(self) -> None:
        fires = [
            {"ts": 1000, "predicate": "p1a"},          # missing agent
            {"agent": "orchestrator", "predicate": "p1a"},    # missing ts
            {"agent": "orchestrator", "ts": "not-a-number", "predicate": "p1a"},
            _fire("orchestrator", "p1a", 5000),
        ]
        self.assertEqual(dc.last_fire_by_key(fires), {("orchestrator", "p1a"): 5000})


# ---------------------------------------------------------------------------
# threshold routing
# ---------------------------------------------------------------------------


class TestThresholdDaysFor(unittest.TestCase):
    def test_p1b_default_7_days(self) -> None:
        self.assertEqual(dc._threshold_days_for("p1b"), 7)

    def test_p1a_unarmed(self) -> None:
        # T-3299: p1a is mtime-based (not the language-regex class the drift-check
        # targets) and never fires on a busy fleet, so it is unarmed — read+reported,
        # never alerted — to stop indefinite false staleness alarms.
        self.assertIsNone(dc._threshold_days_for("p1a"))

    def test_unconfigured_predicate_is_unarmed(self) -> None:
        for predicate in ("p1a", "p1c", "p1d", "tui-wedge", "cap-modal", "spend-cap",
                          "cyber-refusal", "stuck-poke", "cap-modal-unsafe"):
            self.assertIsNone(dc._threshold_days_for(predicate), predicate)

    def test_env_override_arms_an_unarmed_predicate(self) -> None:
        with mock.patch.dict(
            "os.environ", {"ACTIVITY_MONITOR_DRIFT_THRESHOLD_TUI_WEDGE_DAYS": "5"}
        ):
            self.assertEqual(dc._threshold_days_for("tui-wedge"), 5)

    def test_env_override_replaces_default(self) -> None:
        with mock.patch.dict(
            "os.environ", {"ACTIVITY_MONITOR_DRIFT_THRESHOLD_P1B_DAYS": "14"}
        ):
            self.assertEqual(dc._threshold_days_for("p1b"), 14)

    def test_env_override_malformed_falls_back_to_default(self) -> None:
        with mock.patch.dict(
            "os.environ", {"ACTIVITY_MONITOR_DRIFT_THRESHOLD_P1B_DAYS": "not-a-number"}
        ):
            self.assertIsNone(dc._threshold_days_for("p1b"))


# ---------------------------------------------------------------------------
# compute_drift — gap compute + threshold routing + first-install grace
# ---------------------------------------------------------------------------


class TestComputeDrift(unittest.TestCase):
    def test_no_fires_within_grace_does_not_drift(self) -> None:
        """Never-fired pair; bootstrap was only 1 day ago — well inside p1a's
        3d default threshold. No drift yet (first-install grace)."""
        bootstrap_ts = int(NOW - 1 * DAY)
        drifted = dc.compute_drift([], bootstrap_ts, int(NOW), agents=["orchestrator"])
        self.assertEqual(drifted, [])

    def test_no_fires_past_grace_drifts(self) -> None:
        """Never-fired pair; bootstrap was 10 days ago — past the p1b (7d)
        default. p1b drifts; last_fire_ts is None (never fired). p1a is unarmed
        (T-3299) so it never drifts regardless of gap."""
        bootstrap_ts = int(NOW - 10 * DAY)
        drifted = dc.compute_drift([], bootstrap_ts, int(NOW), agents=["orchestrator"])
        keys = {(d["agent"], d["predicate"]) for d in drifted}
        self.assertIn(("orchestrator", "p1b"), keys)
        self.assertNotIn(("orchestrator", "p1a"), keys)
        for d in drifted:
            self.assertIsNone(d["last_fire_ts"])

    def test_recent_fire_does_not_drift(self) -> None:
        fires = [_fire("orchestrator", "p1b", NOW - 1 * DAY)]
        bootstrap_ts = int(NOW - 30 * DAY)
        drifted = dc.compute_drift(fires, bootstrap_ts, int(NOW), agents=["orchestrator"])
        self.assertFalse(any(d["predicate"] == "p1b" for d in drifted))

    def test_stale_fire_drifts_single_predicate(self) -> None:
        """p1b fired 8 days ago (> 7d threshold); p1a fired 1 day ago (< 3d) —
        only p1b should drift."""
        fires = [
            _fire("orchestrator", "p1a", NOW - 1 * DAY),
            _fire("orchestrator", "p1b", NOW - 8 * DAY),
        ]
        bootstrap_ts = int(NOW - 30 * DAY)
        drifted = dc.compute_drift(fires, bootstrap_ts, int(NOW), agents=["orchestrator"])
        predicates = {d["predicate"] for d in drifted}
        self.assertEqual(predicates, {"p1b"})
        (entry,) = [d for d in drifted if d["predicate"] == "p1b"]
        self.assertAlmostEqual(entry["gap_days"], 8, delta=0.01)
        self.assertEqual(entry["threshold_days"], 7)
        self.assertEqual(entry["last_fire_ts"], int(NOW - 8 * DAY))

    def test_multiple_fires_uses_most_recent(self) -> None:
        fires = [
            _fire("orchestrator", "p1b", NOW - 20 * DAY),
            _fire("orchestrator", "p1b", NOW - 1 * DAY),
        ]
        bootstrap_ts = int(NOW - 30 * DAY)
        drifted = dc.compute_drift(fires, bootstrap_ts, int(NOW), agents=["orchestrator"])
        self.assertFalse(any(d["predicate"] == "p1b" for d in drifted))

    def test_unarmed_predicate_never_drifts_regardless_of_gap(self) -> None:
        bootstrap_ts = int(NOW - 365 * DAY)
        drifted = dc.compute_drift([], bootstrap_ts, int(NOW), agents=["orchestrator"])
        predicates = {d["predicate"] for d in drifted}
        # Only the remaining armed default can ever appear (p1a unarmed, T-3299).
        self.assertEqual(predicates, {"p1b"})

    def test_armed_via_env_can_drift(self) -> None:
        bootstrap_ts = int(NOW - 30 * DAY)
        with mock.patch.dict(
            "os.environ", {"ACTIVITY_MONITOR_DRIFT_THRESHOLD_STUCK_POKE_DAYS": "5"}
        ):
            drifted = dc.compute_drift([], bootstrap_ts, int(NOW), agents=["orchestrator"])
        self.assertTrue(any(d["predicate"] == "stuck-poke" for d in drifted))


# ---------------------------------------------------------------------------
# alert dedup
# ---------------------------------------------------------------------------


class TestShouldAlert(unittest.TestCase):
    def test_no_prior_alert_fires(self) -> None:
        self.assertTrue(dc._should_alert("orchestrator", "p1b", NOW, {}))

    def test_within_cooldown_suppressed(self) -> None:
        state = {"orchestrator:p1b": {"ts": NOW - 60}}
        self.assertFalse(dc._should_alert("orchestrator", "p1b", NOW, state))

    def test_past_cooldown_fires_again(self) -> None:
        state = {"orchestrator:p1b": {"ts": NOW - dc.ALERT_COOLDOWN_SECONDS - 1}}
        self.assertTrue(dc._should_alert("orchestrator", "p1b", NOW, state))

    def test_distinct_predicate_not_suppressed_by_other_predicates_cooldown(self) -> None:
        state = {"orchestrator:p1b": {"ts": NOW - 60}}
        self.assertTrue(dc._should_alert("orchestrator", "p1a", NOW, state))


# ---------------------------------------------------------------------------
# alert routing
# ---------------------------------------------------------------------------


class TestAlert(unittest.TestCase):
    def test_routes_via_recovery_triage_not_self_triage(self) -> None:
        """agent param passed to route_failure_alert MUST be the drifted
        persona (e.g. "orchestrator"), never the literal "recovery" — that would trip
        the self-triage carve-out and bypass Recovery's pane entirely."""
        with mock.patch.object(dc, "_route_failure_alert") as mroute, \
             mock.patch("tools.mailbox.send") as msend:
            dc._alert("orchestrator", "p1b", 8.0, 7, int(NOW - 8 * DAY))
        mroute.assert_called_once()
        agent_arg = mroute.call_args[0][0]
        self.assertEqual(agent_arg, "orchestrator")
        self.assertNotEqual(agent_arg, "recovery")
        vocab_arg = mroute.call_args[0][2]
        self.assertEqual(vocab_arg, "detector-drift-orchestrator-p1b")
        msend.assert_called_once()

    def test_mailbox_send_targets_orchestrator_inbox(self) -> None:
        with mock.patch.object(dc, "_route_failure_alert"), \
             mock.patch("tools.mailbox.send") as msend:
            dc._alert("orchestrator", "p1a", 4.5, 3, None)
        args, kwargs = msend.call_args
        self.assertEqual(args[0], dc.ORCHESTRATOR_INBOX)
        self.assertIn("p1a", kwargs.get("subject", ""))

    def test_never_fired_message_says_never(self) -> None:
        with mock.patch.object(dc, "_route_failure_alert") as mroute, \
             mock.patch("tools.mailbox.send"):
            dc._alert("orchestrator", "p1a", 4.5, 3, None)
        msg = mroute.call_args[0][1]
        self.assertIn("never", msg)

    def test_route_failure_alert_failure_does_not_suppress_mailbox(self) -> None:
        with mock.patch.object(dc, "_route_failure_alert", side_effect=RuntimeError("recovery down")), \
             mock.patch("tools.mailbox.send") as msend:
            dc._alert("orchestrator", "p1b", 8.0, 7, int(NOW - 8 * DAY))
        msend.assert_called_once()

    def test_mailbox_failure_does_not_raise(self) -> None:
        with mock.patch.object(dc, "_route_failure_alert") as mroute, \
             mock.patch("tools.mailbox.send", side_effect=RuntimeError("mailbox down")):
            dc._alert("orchestrator", "p1b", 8.0, 7, int(NOW - 8 * DAY))
        mroute.assert_called_once()


# ---------------------------------------------------------------------------
# check() — end-to-end (fire-log -> drift -> alert, with dedup + dry-run)
# ---------------------------------------------------------------------------


class TestCheck(_DriftCheckBase):
    def test_no_drift_returns_0_no_alert(self) -> None:
        self._write_fires([_fire("orchestrator", "p1a", NOW - 1 * DAY),
                            _fire("orchestrator", "p1b", NOW - 1 * DAY)])
        with mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=False, now=NOW)
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_drift_detected_alerts(self) -> None:
        self._write_fires([_fire("orchestrator", "p1b", NOW - 8 * DAY)])
        with mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=False, now=NOW)
        self.assertGreaterEqual(rc, 1)
        malert.assert_called()

    def test_dry_run_never_alerts(self) -> None:
        self._write_fires([_fire("orchestrator", "p1b", NOW - 8 * DAY)])
        with mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=True, now=NOW)
        self.assertGreaterEqual(rc, 1)
        malert.assert_not_called()

    def test_cooldown_suppresses_repeat_alert(self) -> None:
        self._write_fires([_fire("orchestrator", "p1b", NOW - 8 * DAY)])
        with mock.patch.object(dc, "_alert") as malert:
            dc.check(dry_run=False, now=NOW)
            dc.check(dry_run=False, now=NOW + 60)  # well within 24h cooldown
        self.assertEqual(malert.call_count, 1)

    def test_cooldown_expiry_realerts(self) -> None:
        self._write_fires([_fire("orchestrator", "p1b", NOW - 8 * DAY)])
        with mock.patch.object(dc, "_alert") as malert:
            dc.check(dry_run=False, now=NOW)
            dc.check(dry_run=False, now=NOW + dc.ALERT_COOLDOWN_SECONDS + 1)
        self.assertEqual(malert.call_count, 2)

    def test_bootstrap_ts_persisted_across_runs(self) -> None:
        dc.check(dry_run=True, now=NOW)
        data = json.loads(dc.BOOTSTRAP_PATH.read_text())
        first_ts = data["bootstrap_ts"]
        dc.check(dry_run=True, now=NOW + 1000)
        data2 = json.loads(dc.BOOTSTRAP_PATH.read_text())
        self.assertEqual(data2["bootstrap_ts"], first_ts)


# ---------------------------------------------------------------------------
# T-3570 RC1 — per-pair first-seen floor (compute_drift)
# ---------------------------------------------------------------------------


class TestComputeDriftFirstSeen(unittest.TestCase):
    def test_first_seen_floor_graces_newly_added_pair(self) -> None:
        """FALSIFIER 1 (new-pair-added-today -> no alarm): a never-fired pair
        whose first_seen ts is recent must NOT drift, even though the global
        bootstrap is ancient — the pair inherits its own grace, not the whole
        since-bootstrap gap (T-3570 RC1)."""
        bootstrap_ts = int(NOW - 30 * DAY)  # ancient global floor (the bug's source)
        first_seen = {"orchestrator:p1b": int(NOW - 1 * DAY)}  # observed ~today
        drifted = dc.compute_drift(
            [], bootstrap_ts, int(NOW), agents=["orchestrator"], first_seen=first_seen
        )
        self.assertEqual(drifted, [])

    def test_first_seen_past_grace_still_drifts(self) -> None:
        """A never-fired pair whose OWN first-seen ts is past threshold still
        drifts — the floor moved, the detection did not weaken."""
        bootstrap_ts = int(NOW - 30 * DAY)
        first_seen = {"orchestrator:p1b": int(NOW - 10 * DAY)}
        drifted = dc.compute_drift(
            [], bootstrap_ts, int(NOW), agents=["orchestrator"], first_seen=first_seen
        )
        self.assertEqual({(d["agent"], d["predicate"]) for d in drifted}, {("orchestrator", "p1b")})

    def test_first_seen_missing_key_falls_back_to_bootstrap(self) -> None:
        """A pair not yet in the store falls back to the global bootstrap floor
        (robustness; the seed step normally populates it first)."""
        bootstrap_ts = int(NOW - 10 * DAY)
        drifted = dc.compute_drift(
            [], bootstrap_ts, int(NOW), agents=["orchestrator"], first_seen={}
        )
        self.assertIn(("orchestrator", "p1b"), {(d["agent"], d["predicate"]) for d in drifted})

    def test_first_seen_none_preserves_legacy_bootstrap_floor(self) -> None:
        """first_seen=None (legacy callers/tests) -> single bootstrap floor,
        unchanged behavior."""
        bootstrap_ts = int(NOW - 10 * DAY)
        drifted = dc.compute_drift([], bootstrap_ts, int(NOW), agents=["orchestrator"])
        self.assertIn(("orchestrator", "p1b"), {(d["agent"], d["predicate"]) for d in drifted})

    def test_has_fired_pair_ignores_first_seen(self) -> None:
        """first_seen only floors NEVER-fired pairs; a pair with a real last
        fire computes its gap from that fire regardless of first_seen."""
        fires = [_fire("orchestrator", "p1b", NOW - 8 * DAY)]
        first_seen = {"orchestrator:p1b": int(NOW)}  # 'seen now' must NOT reset a real gap
        drifted = dc.compute_drift(
            fires, int(NOW - 30 * DAY), int(NOW), agents=["orchestrator"], first_seen=first_seen
        )
        (entry,) = [d for d in drifted if d["predicate"] == "p1b"]
        self.assertAlmostEqual(entry["gap_days"], 8, delta=0.01)
        self.assertEqual(entry["last_fire_ts"], int(NOW - 8 * DAY))


class TestSeedFirstSeen(unittest.TestCase):
    def test_seeds_unseen_armed_pairs_to_now(self) -> None:
        store: dict[str, int] = {}
        changed = dc._seed_first_seen(store, NOW, ["orchestrator", "mason"])
        self.assertTrue(changed)
        self.assertEqual(store["orchestrator:p1b"], int(NOW))
        self.assertEqual(store["mason:p1b"], int(NOW))

    def test_does_not_overwrite_existing(self) -> None:
        store = {"orchestrator:p1b": 111}
        dc._seed_first_seen(store, NOW, ["orchestrator"])
        self.assertEqual(store["orchestrator:p1b"], 111)

    def test_skips_unarmed_predicates(self) -> None:
        store: dict[str, int] = {}
        dc._seed_first_seen(store, NOW, ["orchestrator"])
        # p1a and the other unarmed predicates never get a floor.
        self.assertNotIn("orchestrator:p1a", store)
        self.assertEqual(set(store), {"orchestrator:p1b"})

    def test_returns_false_when_nothing_added(self) -> None:
        store = {"orchestrator:p1b": 111}
        self.assertFalse(dc._seed_first_seen(store, NOW, ["orchestrator"]))


# ---------------------------------------------------------------------------
# T-3570 RC2 — can-it-currently-fire precondition
# ---------------------------------------------------------------------------


class TestPredicateCanCurrentlyFire(unittest.TestCase):
    def test_p1b_queue_loaded_can_fire(self) -> None:
        with mock.patch.object(dc, "_scan_actionable_tickets", return_value=3):
            self.assertIs(dc._predicate_can_currently_fire("orchestrator", "p1b"), True)

    def test_p1b_queue_confirmed_empty_cannot_fire(self) -> None:
        with mock.patch.object(dc, "_scan_actionable_tickets", return_value=0):
            self.assertIs(dc._predicate_can_currently_fire("mason", "p1b"), False)

    def test_p1b_probe_failure_is_unknown_not_no(self) -> None:
        """A failed cross-account probe (None) must NOT read as confirmed-empty
        — unknown must never stand in for 'no' (would mask a genuine stale)."""
        with mock.patch.object(dc, "_scan_actionable_tickets", return_value=None):
            self.assertIsNone(dc._predicate_can_currently_fire("orchestrator", "p1b"))

    def test_non_p1b_predicate_has_no_modeled_precondition(self) -> None:
        with mock.patch.object(dc, "_scan_actionable_tickets") as mscan:
            self.assertIsNone(dc._predicate_can_currently_fire("orchestrator", "p1a"))
        mscan.assert_not_called()

    def test_unknown_agent_is_unknown(self) -> None:
        self.assertIsNone(dc._predicate_can_currently_fire("nobody", "p1b"))

    def test_passes_agents_own_queue_states(self) -> None:
        """A sub-orch must be probed with its active-only queue_states, not the
        default inbox+active — else an unclaimed inbox ticket would read as
        'can fire' and wrongly un-suppress (T-3557e)."""
        with mock.patch.object(dc, "_scan_actionable_tickets", return_value=0) as mscan:
            dc._predicate_can_currently_fire("mason", "p1b")
        self.assertEqual(mscan.call_args.kwargs["queue_states"], ("active",))


class TestCheckSuppression(_DriftCheckBase):
    def _write_first_seen(self, mapping: dict[str, int]) -> None:
        dc.FIRST_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        dc.FIRST_SEEN_PATH.write_text(json.dumps(mapping), encoding="utf-8")

    def test_suborch_quiet_never_fired_suppressed(self) -> None:
        """FALSIFIER 3 (sub-orch-p1b-quiet -> no alarm): a never-fired mason p1b
        that has drifted is suppressed when it provably cannot currently fire
        (confirmed-empty queue)."""
        self._write_first_seen({"mason:p1b": int(NOW - 30 * DAY)})  # drifted
        with mock.patch.object(dc, "_predicate_can_currently_fire", return_value=False), \
             mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=False, now=NOW)
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_suborch_would_alarm_if_queue_loaded(self) -> None:
        """Same drifted never-fired mason p1b, but the queue is loaded (can
        fire) -> NOT suppressed -> alarms. The suppression is queue-gated, not
        a blanket sub-orch mute."""
        self._write_first_seen({"mason:p1b": int(NOW - 30 * DAY)})
        with mock.patch.object(dc, "_predicate_can_currently_fire", return_value=True), \
             mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=False, now=NOW)
        self.assertGreaterEqual(rc, 1)
        malert.assert_called()

    def test_probe_failure_does_not_suppress(self) -> None:
        """An unknown probe (None) never suppresses -> the drift still alarms."""
        self._write_first_seen({"mason:p1b": int(NOW - 30 * DAY)})
        with mock.patch.object(dc, "_predicate_can_currently_fire", return_value=None), \
             mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=False, now=NOW)
        self.assertGreaterEqual(rc, 1)
        malert.assert_called()

    def test_has_fired_then_stale_alarms_regardless_of_queue(self) -> None:
        """FALSIFIER 2 (genuine stale -> still alarms): a pair that FIRED then
        went silent past threshold always alarms — RC2 suppression is scoped to
        never-fired pairs, so the current queue snapshot never mutes a real
        T-3070 signal. The precondition probe must not even be consulted."""
        self._write_fires([_fire("orchestrator", "p1b", NOW - 8 * DAY)])
        with mock.patch.object(dc, "_predicate_can_currently_fire", return_value=False) as mcan, \
             mock.patch.object(dc, "_alert") as malert:
            rc = dc.check(dry_run=False, now=NOW)
        self.assertGreaterEqual(rc, 1)
        malert.assert_called()
        mcan.assert_not_called()

    def test_first_seen_seeded_and_persisted(self) -> None:
        """A fresh run seeds every currently-known armed pair to now and
        persists it, so a brand-new pair is graced from this run forward."""
        dc.check(dry_run=True, now=NOW)
        store = json.loads(dc.FIRST_SEEN_PATH.read_text())
        for agent in dc.AGENT_CONFIG:
            self.assertEqual(store[f"{agent}:p1b"], int(NOW))


# ---------------------------------------------------------------------------
# T-5439 — skip a persona in seat-maintenance or with every runtime held
# ---------------------------------------------------------------------------

_BOTH = ("claude/interactive-subscription", "codex/interactive-chatgpt")


class TestQuiescedPersonaSkipped(_DriftCheckBase):
    """A held seat has no pane, so p1b cannot match: its silence is an
    authorised no, not a stale detector (mason p1b paged daily 10-24)."""

    def setUp(self) -> None:
        super().setUp()
        p = mock.patch.object(dc, "_configured_runtimes", return_value=_BOTH)
        p.start()
        self.addCleanup(p.stop)
        # mason FIRED p1b 9d ago: the has-fired path, which RC2 never suppresses.
        self._write_fires([_fire("mason", "p1b", NOW - 9 * DAY),
                           _fire("orchestrator", "p1b", NOW - 1 * DAY),
                           _fire("artisan", "p1b", NOW - 1 * DAY)])
        p = mock.patch.object(dc, "_predicate_can_currently_fire", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def _alerted_agents(self) -> list[str]:
        with mock.patch.object(dc, "_alert") as malert:
            dc.check(dry_run=False, now=NOW)
        return [c.args[0] for c in malert.call_args_list]

    def test_control_unquiesced_mason_still_alerts(self) -> None:
        self.assertEqual(self._alerted_agents(), ["mason"])

    def test_seat_in_maintenance_is_skipped(self) -> None:
        with mock.patch.object(dc, "_seat_in_maintenance", side_effect=lambda a: a == "mason"):
            self.assertEqual(self._alerted_agents(), [])

    def _seat(self, **kw):
        from tools.seat_state_testkit import eff
        return mock.patch("tools.seat_state.safe_effective_for", return_value=eff(**kw))

    def test_deep_sleep_seat_is_skipped_T5527(self) -> None:
        for asleep in (True, False):
            with self._seat(state="deep-sleep", asleep=asleep):
                self.assertEqual(self._alerted_agents(), [])

    def test_paused_intake_seat_is_skipped_T5527(self) -> None:
        # L2 stops the activity monitor's pokes for a paused seat, so no fires is expected.
        with self._seat(state="paused-intake"):
            self.assertEqual(self._alerted_agents(), [])

    def test_quiesce_reasons_name_the_seat_state_T5527(self) -> None:
        with self._seat(state="deep-sleep", asleep=True):
            self.assertEqual(dc._persona_quiesced("mason"), "seat-sleep")
        with self._seat(state="paused-intake"):
            self.assertEqual(dc._persona_quiesced("mason"), "seat-paused")
        with self._seat():
            self.assertIsNone(dc._persona_quiesced("mason"))
        with self._seat(oom=True):          # an OOM hold alone is not a quiet declaration
            self.assertIsNone(dc._persona_quiesced("mason"))
        with mock.patch("tools.seat_state.safe_effective_for", return_value=None):
            self.assertIsNone(dc._persona_quiesced("mason"))

    def test_live_oom_shape_oom_hold_plus_maintenance_rearm_is_skipped_T5527(self) -> None:
        with self._seat(oom=True, maintenance=True), \
                mock.patch.object(dc, "_seat_in_maintenance", side_effect=lambda a: a == "mason"):
            self.assertEqual(self._alerted_agents(), [])
            self.assertEqual(dc._persona_quiesced("mason"), "seat-maintenance")

    def test_maintenance_fault_still_logs_its_own_line_T5527(self) -> None:
        with mock.patch("tools.notify_policy.in_maintenance", side_effect=RuntimeError("boom")), \
                mock.patch.object(dc, "_log") as log:
            self.assertFalse(_real_seat_in_maintenance("mason"))
        self.assertTrue(any("seat-maintenance check failed open for mason" in str(c)
                            for c in log.call_args_list))

    def test_every_runtime_held_is_skipped(self) -> None:
        holds = {r: {"set_by": "operator:orchestrator"} for r in _BOTH}
        with mock.patch.object(dc, "_read_persona_holds",
                               side_effect=lambda a: holds if a == "mason" else {}):
            self.assertEqual(self._alerted_agents(), [])

    def test_partial_hold_still_alerts(self) -> None:
        holds = {_BOTH[0]: {"set_by": "operator:orchestrator"}}
        with mock.patch.object(dc, "_read_persona_holds", return_value=holds):
            self.assertEqual(self._alerted_agents(), ["mason"])

    def test_unknown_holds_still_alerts(self) -> None:
        """An unreadable holds probe must not stand in for a confirmed hold."""
        with mock.patch.object(dc, "_read_persona_holds", return_value=None):
            self.assertEqual(self._alerted_agents(), ["mason"])

    def test_no_configured_runtimes_still_alerts(self) -> None:
        """all() over an empty runtime list is vacuously true; it must not quiet a seat."""
        with mock.patch.object(dc, "_configured_runtimes", return_value=()), \
             mock.patch.object(dc, "_read_persona_holds", return_value={}):
            self.assertEqual(self._alerted_agents(), ["mason"])



# the base class patches dc._seat_in_maintenance; keep a handle on the real function
_real_seat_in_maintenance = dc._seat_in_maintenance


class TestSeatInMaintenance(unittest.TestCase):
    def test_probe_fault_fails_open(self) -> None:
        with mock.patch("tools.notify_policy.in_maintenance", side_effect=RuntimeError("boom")):
            self.assertFalse(dc._seat_in_maintenance("mason"))

    def test_delegates_to_notify_policy(self) -> None:
        with mock.patch("tools.notify_policy.in_maintenance", return_value=True) as m:
            self.assertTrue(dc._seat_in_maintenance("mason"))
        m.assert_called_once_with("mason")


class TestReadPersonaHolds(unittest.TestCase):
    def _run(self, rc: int, out: str):
        res = mock.Mock(returncode=rc, stdout=out, stderr="")
        with mock.patch.object(dc.subprocess, "run", return_value=res) as mrun:
            got = dc._read_persona_holds("mason")
        return got, mrun

    def test_reads_holds_mapping_as_the_seat_user(self) -> None:
        doc = {"holds": {_BOTH[0]: {}}, "schema": "pantheon-persona-runtime-holds/v1"}
        got, mrun = self._run(0, json.dumps(doc))
        self.assertEqual(got, {_BOTH[0]: {}})
        argv = mrun.call_args.args[0]
        self.assertEqual(argv[:4], ["sudo", "-n", "-u", "suborch1"])
        self.assertIn("/home/suborch1/.local/state/pantheon/personas/mason/overrides/holds.json",
                      " ".join(argv))

    def test_absent_file_is_no_holds(self) -> None:
        got, _ = self._run(3, "")
        self.assertEqual(got, {})

    def test_malformed_is_unknown(self) -> None:
        got, _ = self._run(0, "{not json")
        self.assertIsNone(got)

    def test_sudo_failure_is_unknown(self) -> None:
        got, _ = self._run(1, "")
        self.assertIsNone(got)

    def test_unknown_agent_is_unknown(self) -> None:
        self.assertIsNone(dc._read_persona_holds("nobody"))


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def test_refuses_bare_invocation(self) -> None:
        rc = dc.main([])
        self.assertEqual(rc, 64)

    def test_dry_run_flag_accepted(self) -> None:
        with mock.patch.object(dc, "check", return_value=0) as mcheck:
            rc = dc.main(["--dry-run"])
        self.assertEqual(rc, 0)
        mcheck.assert_called_once()

    def test_check_flag_accepted_always_exits_0(self) -> None:
        """A monitor must not fail its own timer — even a nonzero (drift
        detected) return from check() must not propagate, else the systemd
        OnFailure= guard double-pages on top of this tool's own alert."""
        with mock.patch.object(dc, "check", return_value=3) as mcheck:
            rc = dc.main(["--check"])
        self.assertEqual(rc, 0)
        mcheck.assert_called_once()


if __name__ == "__main__":
    unittest.main()
