"""T-4862: live subscription-usability probing + fallback selection.

Hermetic: tmp state roots, injected fakes. No tmux, codex, claude or network.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import runtime_controller as rc
from tools import subscription_usability as su
from tools.harness_adapter.quota import QuotaSnapshot, QuotaWindow
from tools.test_runtime_controller import _binding

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def snap(binding, *, remaining=50.0, freshness="fresh", authoritative=False, windows=True, resets=None):
    ws = (
        (QuotaWindow("weekly", 100.0 - remaining, remaining, resets or NOW + timedelta(days=2)),)
        if windows
        else ()
    )
    return QuotaSnapshot(
        profile=su.runtime_ref(binding),
        windows=ws,
        observed_at=NOW,
        freshness=freshness,
        evidence={"authoritative_limit": "true" if authoritative else "false"},
    )


class Rig:
    def __init__(self, tc: unittest.TestCase, *, claude_primary=True):
        tmp = tempfile.TemporaryDirectory(prefix="su-")
        tc.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.primary = _binding(self.root, harness="claude", profile="primary", fallbacks=("codex/a", "codex/b"))
        self.a = _binding(self.root, harness="codex", profile="a")
        self.b = _binding(self.root, harness="codex", profile="b")
        self.calls: list[str] = []
        self.results: dict[str, object] = {}

    def deps(self) -> su.ProbeDeps:
        def codex(binding):
            self.calls.append(su.runtime_ref(binding))
            value = self.results[su.runtime_ref(binding)]
            if isinstance(value, BaseException):
                raise value
            return value

        return su.ProbeDeps(codex_snapshot=codex)

    def probe(self, binding, *, now=NOW, **kw):
        return su.probe_usability(
            binding, state_root=self.state, now=now, allow_spend=False, deps=self.deps(), **kw
        )

    def select(self, *, confirmed, now=NOW, candidates=None):
        return su.select_usable_fallback(
            "recovery", candidates or [self.a, self.b], state_root=self.state, now=now,
            primary_confirmed_capped=confirmed, allow_spend=False, deps=self.deps(),
        )


class VerdictTests(unittest.TestCase):
    def test_codex_verdicts(self):
        r = Rig(self)
        cases = [
            (snap(r.a, remaining=50.0), "usable"),
            (snap(r.a, remaining=3.0), "near-cap"),  # below the 5% reserve floor, still positive
            (snap(r.a, remaining=0.0), "capped"),
            (snap(r.a, remaining=-1.0), "capped"),
            (snap(r.a, remaining=50.0, authoritative=True), "capped"),
            (snap(r.a, freshness="stale"), "unknown"),
            (snap(r.a, freshness="unavailable", windows=False), "unknown"),
            (snap(r.a, windows=False), "unknown"),
        ]
        for i, (snapshot, want) in enumerate(cases):
            r.results["codex/a"] = snapshot
            p = r.probe(r.a, force=True)
            self.assertEqual(p.verdict, want, i)

    def test_probe_errors_are_unknown(self):
        r = Rig(self)
        for exc in (TimeoutError("t"), RuntimeError("boom"), OSError("x")):
            r.results["codex/a"] = exc
            self.assertEqual(r.probe(r.a, force=True).verdict, "unknown")
        r.results["codex/a"] = None
        self.assertEqual(r.probe(r.a, force=True).verdict, "unknown")

    def test_usability_persisted_roundtrip(self):
        r = Rig(self)
        r.results["codex/a"] = snap(r.a, remaining=50.0)
        r.probe(r.a, force=True)
        loaded = su.load_usability("recovery", r.a, state_root=r.state)
        self.assertEqual(loaded.verdict, "usable")
        self.assertEqual(loaded.observed_at, NOW)
        path = su.usability_path("recovery", r.a, state_root=r.state)
        self.assertTrue(str(path).endswith("runtimes/codex--a/usability.json"))


class CostBoundTests(unittest.TestCase):
    def test_fresh_usable_result_reused_without_runner(self):
        r = Rig(self)
        r.results["codex/a"] = snap(r.a)
        r.probe(r.a)
        r.calls.clear()
        again = r.probe(r.a, now=NOW + timedelta(minutes=9))
        self.assertEqual(again.verdict, "usable")
        self.assertEqual(r.calls, [])

    def test_usable_expires_at_ttl_and_reprobes(self):
        r = Rig(self)
        r.results["codex/a"] = snap(r.a)
        r.probe(r.a)
        r.calls.clear()
        r.probe(r.a, now=NOW + timedelta(minutes=11))
        self.assertEqual(r.calls, ["codex/a"])

    def test_unknown_not_due_makes_no_call_and_stays_unknown(self):
        r = Rig(self)
        r.results["codex/a"] = RuntimeError("down")
        r.probe(r.a)
        r.calls.clear()
        again = r.probe(r.a, now=NOW + timedelta(minutes=2))
        self.assertEqual(again.verdict, "unknown")
        self.assertEqual(r.calls, [])
        r.probe(r.a, now=NOW + timedelta(minutes=6))
        self.assertEqual(r.calls, ["codex/a"])

    def test_capped_asymmetry_stays_authoritative_until_reprobe_at(self):
        """A live capped observation outlives the 10m TTL until reprobe_at (ceiling-bounded)."""
        r = Rig(self)
        r.results["codex/a"] = snap(r.a, remaining=0.0, resets=NOW + timedelta(days=3))
        first = r.probe(r.a)
        self.assertEqual(first.verdict, "capped")
        self.assertEqual(first.reprobe_at, NOW + timedelta(minutes=30))  # ceiling, not the 3-day reset
        r.calls.clear()
        later = r.probe(r.a, now=NOW + timedelta(minutes=11))
        self.assertEqual(later.verdict, "capped")
        self.assertEqual(r.calls, [])
        # R1 forced selection still excludes it at 11m
        decision, _ = r.select(confirmed=True, now=NOW + timedelta(minutes=11), candidates=[r.a])
        self.assertIsNone(decision.chosen)
        # once reprobe_at passes it is re-probed (and found usable after an early reset)
        r.results["codex/a"] = snap(r.a, remaining=80.0)
        r.calls.clear()
        after = r.probe(r.a, now=NOW + timedelta(minutes=31))
        self.assertEqual(after.verdict, "usable")
        self.assertEqual(r.calls, ["codex/a"])

    def test_hint_pulls_reprobe_earlier_never_later(self):
        r = Rig(self)
        r.results["codex/a"] = snap(r.a, remaining=0.0)
        r.probe(r.a)
        r.results["codex/a"] = snap(r.a, remaining=70.0)
        r.calls.clear()
        r.probe(r.a, now=NOW + timedelta(minutes=12), hint=NOW + timedelta(minutes=10))
        self.assertEqual(r.calls, ["codex/a"])  # hint passed -> look again
        r.calls.clear()
        r.results["codex/a"] = snap(r.a, remaining=0.0)
        r.probe(r.a, now=NOW + timedelta(minutes=13), force=True)
        r.calls.clear()
        r.probe(r.a, now=NOW + timedelta(minutes=14), hint=NOW + timedelta(days=9))
        self.assertEqual(r.calls, [])  # a far-future hint never delays past the ceiling record


class SelectionTests(unittest.TestCase):
    def test_all_capped_no_viable_target(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b, authoritative=True)})
        decision, _ = r.select(confirmed=False)
        self.assertIsNone(decision.chosen)
        self.assertEqual(decision.decision, "no-viable-target")
        self.assertEqual(dict(decision.per_candidate), {"codex/a": "capped", "codex/b": "capped"})

    def test_first_capped_second_usable(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b)})
        decision, _ = r.select(confirmed=False)
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/b")

    def test_probe_capped_records_subscription_cap_once(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b)})
        r.select(confirmed=False)
        att = rc.load_last_attempt("recovery", "codex--a", state_root=r.state)
        self.assertEqual(att["failure_class"], "subscription_cap")
        self.assertEqual(att["consecutive_failures"], 1)
        self.assertIsNotNone(att["cooldown_until"])
        # re-probe of the same episode (forced) does not bump the counter
        su.probe_usability(r.a, state_root=r.state, now=NOW + timedelta(minutes=40),
                           allow_spend=False, deps=r.deps(), force=True)
        r.select(confirmed=False, now=NOW + timedelta(minutes=40))
        att = rc.load_last_attempt("recovery", "codex--a", state_root=r.state)
        self.assertEqual(att["consecutive_failures"], 1)

    def test_unknown_not_selectable_when_primary_not_confirmed(self):
        r = Rig(self)
        for i, bad in enumerate((RuntimeError("x"), TimeoutError("t"), None, snap(r.a, freshness="stale"))):
            r.results.update({"codex/a": bad, "codex/b": bad})
            decision, _ = r.select(confirmed=False, now=NOW + timedelta(hours=2 * i))
            self.assertEqual(decision.decision, "no-viable-target")

    def test_confirmed_capped_unknown_is_attempted_capped_is_not(self):
        r = Rig(self)
        r.results.update({"codex/a": RuntimeError("x"), "codex/b": RuntimeError("y")})
        decision, _ = r.select(confirmed=True)
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/a")
        r2 = Rig(self)
        r2.results.update({"codex/a": snap(r2.a, remaining=0.0), "codex/b": snap(r2.b, remaining=0.0)})
        decision, _ = r2.select(confirmed=True)
        self.assertEqual(decision.decision, "no-viable-target")

    def test_rank_usable_beats_unknown_beats_nothing_when_confirmed(self):
        r = Rig(self)
        r.results.update({"codex/a": RuntimeError("x"), "codex/b": snap(r.b)})
        decision, _ = r.select(confirmed=True)
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/b")

    def test_near_cap_ranks_below_usable(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a, remaining=3.0), "codex/b": snap(r.b, remaining=60.0)})
        decision, _ = r.select(confirmed=False)
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/b")

    def test_near_cap_selected_when_only_non_capped(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b, remaining=3.0)})
        decision, _ = r.select(confirmed=False)
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/b")

    def test_held_candidates_skipped(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a), "codex/b": snap(r.b)})
        decision, _ = su.select_usable_fallback(
            "recovery", [r.a, r.b], state_root=r.state, now=NOW, primary_confirmed_capped=False,
            allow_spend=False, holds={"codex/a": {"reason": "x"}}, deps=r.deps(),
        )
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/b")
        self.assertEqual(decision.per_candidate["codex/a"], "held")
        self.assertNotIn("codex/a", r.calls)

    def test_early_reset_clears_cooldown_with_distinguishable_record(self):
        r = Rig(self)
        rc.record_last_attempt("recovery", r.a, outcome="failed", failure_class="subscription_cap",
                               state_root=r.state, now=NOW - timedelta(hours=1),
                               cooldown_until_override=NOW + timedelta(days=2))
        r.results.update({"codex/a": snap(r.a, remaining=90.0), "codex/b": snap(r.b, remaining=0.0)})
        decision, _ = r.select(confirmed=False)
        self.assertEqual(su.runtime_ref(decision.chosen), "codex/a")
        att = rc.load_last_attempt("recovery", "codex--a", state_root=r.state)
        self.assertIsNone(att["cooldown_until"])
        self.assertEqual(att["provenance"]["trigger"], "early-reset-probe")
        events = (rc._evidence_log_path("recovery", r.state)).read_text()
        self.assertIn("early_reset_cooldown_cleared", events)

    def test_no_viable_report_emits_evidence_and_one_alert(self):
        r = Rig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b, remaining=0.0)})
        decision, _ = r.select(confirmed=False)
        with mock.patch.object(rc, "_route_failure_alert") as alert:
            for _ in range(2):
                su.report_no_viable_target("recovery", "planned_quota_no_viable_target", decision,
                                           stamp_name=su.NO_VIABLE_ALERT_STAMP, state_root=r.state, now=NOW)
        self.assertEqual(alert.call_count, 1)
        text = rc._evidence_log_path("recovery", r.state).read_text()
        self.assertIn("planned_quota_no_viable_target", text)
        self.assertIn("codex/a", text)

    def test_abandon_alert_stamp_independent_of_no_viable(self):
        r = Rig(self)
        decision = su.SelectionDecision(None, "no-viable-target", {"codex/a": "capped"})
        with mock.patch.object(rc, "_route_failure_alert") as alert:
            su.report_no_viable_target("recovery", "planned_quota_no_viable_target", decision,
                                       stamp_name=su.NO_VIABLE_ALERT_STAMP, state_root=r.state, now=NOW)
            su.report_no_viable_target("recovery", "planned_swap_abandoned", decision,
                                       stamp_name=su.ABANDON_ALERT_STAMP, state_root=r.state, now=NOW)
        self.assertEqual(alert.call_count, 2)


if __name__ == "__main__":
    unittest.main()
