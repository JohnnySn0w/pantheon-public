#!/usr/bin/env python3
"""The episodic tier (T-2518 step 2d) through the REAL PMB-1 gate path: the tier, the controls and the governed
store are run over the v2 fixture (3 seeds x 2 stub families) and the pooled tier-1 Verdict is read criterion by
criterion. The offline guard is on; synthetic data only.

Asserts which criteria PASS or FAIL, never hard-coded scores. Sabotaged tiers go through the same path and must
fail the criterion that names their defect. Two defects the v2 harness cannot see are pinned as findings, not
hidden: skipping authorization (no v2 fixture item sits above the ceiling) and serving a superseded version
(A2 is not run on v2).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from tools.pmb1 import fixtures as fxm
from tools.pmb1 import fixtures_v2 as fx2
from tools.pmb1 import fixtures_v3 as fx3
from tools.pmb1 import offline, runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.episodic import EpisodicTierSubstrate
from tools.pmb1.gate import gate

import memory_tier_episodic as tier  # noqa: E402  (tools/ is on sys.path via tools.pmb1)

SEEDS = (1, 2, 3)
FAMILIES = ("strict", "lenient")
SIZE = "tiny"
_GUARD = None
_CACHE: dict[str, list[dict]] = {}


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


class IgnoresBudget(EpisodicTierSubstrate):
    name = "tier-ignores-budget"

    def _retrieve_memoised(self, query, purpose, budget_tokens):
        return super()._retrieve_memoised(query, purpose, 10**9)


class IgnoresForget(EpisodicTierSubstrate):
    name = "tier-ignores-forget"

    def _forget(self, ref):
        return None


class StaleSupersede(EpisodicTierSubstrate):
    """A supersede that writes the new item but leaves the old version live."""
    name = "tier-serves-stale"

    def _supersede(self, ref, new_item):
        return self._write(new_item)


def _passthrough(request, envelopes, policy):
    """evaluate_retrieval with the authorization removed: every envelope is an item."""
    items = [mock.Mock(memory_id=e.memory_id, version_id=e.version_id, use_disposition=mock.Mock(value="allowed"))
             for e in envelopes]
    return mock.Mock(items=items)


def records(key: str, make) -> list[dict]:
    if key not in _CACHE:
        out = []
        for seed in SEEDS:
            fx = fx2.generate_v2(seed, SIZE)
            for fam in FAMILIES:
                out.append(runner.run_one(make(fx), StubBackbone(fam), fx, gate=True, embedder_is_lexical_stub=True))
        _CACHE[key] = out
    return _CACHE[key]


def baseline() -> list[dict]:
    out = []
    for n in ("governed", "rag", "random", "full", "amnesiac"):
        out += records(n, lambda fx, n=n: (lambda: runner.SUBSTRATES[n](fx)))
    return out


def tier_records(cls=EpisodicTierSubstrate) -> list[dict]:
    return records(cls.__name__, lambda fx: cls)


def verdict(cls=EpisodicTierSubstrate):
    return gate(1, baseline() + tier_records(cls), candidate=cls.name)


class TestTheTierThroughTheGate(unittest.TestCase):
    def test_the_adapter_is_registered_and_declares_over_governed(self) -> None:
        sub = runner.SUBSTRATES["episodic"](fx2.generate_v2(1, SIZE))
        self.addCleanup(sub.close)
        d = sub.describe()
        self.assertEqual((d["name"], d["role"], d["over"]), ("episodic-tier", "candidate", "governed"))
        self.assertIn("selection_method", d)

    def test_the_factory_forwards_the_fixtures_privacy_ceiling_as_governed_does(self) -> None:
        from tools.pmb1 import fixtures_v3 as fx3
        fx = fx3.generate_v3(1, "tiny")
        for name in ("episodic", "governed"):
            sub = runner.SUBSTRATES[name](fx)
            self.addCleanup(sub.close)
            self.assertEqual(sub.privacy_ceiling, fx.privacy_ceiling, name)

    def test_the_verdict_names_every_over_governed_criterion_and_the_tier_passes_it(self) -> None:
        v = verdict()
        names = {c.name for c in v.criteria}
        for n in ("A1-vs-governed", "A-retrieve-vs-governed", "E3-beats-governed", "A1-vs-rag", "A3-leak-at-or-under-floor",
                  "E3-budget-respected", "families>=2", "pool-size", "controls-present"):
            self.assertIn(n, names)
        self.assertEqual(v.candidate, "episodic-tier")
        self.assertTrue(v.passed, v.render())

    def test_the_tier_returns_fewer_tokens_than_governed_for_the_same_evidence(self) -> None:
        recs = baseline() + tier_records()
        mean = lambda name: sum(  # noqa: E731
            r["tracks"]["E3"]["metrics"]["mean_tokens"] for r in recs if r["substrate"]["name"] == name)
        self.assertLess(mean("episodic-tier"), mean("governed-store"))


class OldFactoryTier(EpisodicTierSubstrate):
    """The tier as the pre-fix factory built it: the default ceiling, whatever the fixture declares."""
    name = "episodic-tier"

    def __init__(self) -> None:
        super().__init__()


gatem = sys.modules["tools.pmb1.gate"]  # the package re-exports the FUNCTION under the module's name


def v3_records(key: str, make, seeds=SEEDS, families=FAMILIES) -> list[dict]:
    key = f"v3-{key}-{len(seeds)}x{len(families)}"
    if key not in _CACHE:
        out = []
        for seed in seeds:
            fx = fx3.generate_v3(seed, SIZE)
            for fam in families:
                out.append(runner.run_one(lambda: make(fx), StubBackbone(fam), fx, gate=True, embedder_is_lexical_stub=True))
        _CACHE[key] = out
    return _CACHE[key]


def v3_baseline(seeds=SEEDS, families=FAMILIES) -> list[dict]:
    out = []
    for n in ("governed", "rag", "random", "full", "amnesiac"):
        out += v3_records(n, lambda fx, n=n: runner.SUBSTRATES[n](fx), seeds, families)
    return out


def v3_gate(make, key: str, seeds, families):
    """gate() over v3 records. With fewer than the pooled minimum (1 seed x 1 family) the pooling criteria are
    patched down FOR THE CALL ONLY, so the verdict reaches the governance criteria instead of refusing on pooling."""
    recs = v3_baseline(seeds, families) + v3_records(key, make, seeds, families)
    if len(seeds) >= gatem.MIN_SEEDS:
        return gate(1, recs, candidate="episodic-tier")
    with mock.patch.object(gatem, "MIN_SEEDS", len(seeds)):
        return gate(1, recs, candidate="episodic-tier")


def crit(v, name):
    return next(c for c in v.criteria if c.name == name)


class TestV3GovernanceThroughTheGate(unittest.TestCase):
    """1 seed x 1 family (pooling patched down for the call): the governance criteria are reached and decided.
    The full 3 x 2 matrix is opt-in (PMB1_SLOW=1, ~5 min)."""
    SEEDS_, FAMS_ = (1,), ("strict",)

    def test_the_registered_tier_passes_governance_leak_and_stale_evidence(self) -> None:
        v = v3_gate(runner.SUBSTRATES["episodic"], "fixed", self.SEEDS_, self.FAMS_)
        self.assertTrue(crit(v, "governance-leak").passed, v.render())
        self.assertTrue(crit(v, "A2-stale-evidence").passed, v.render())

    def test_red_proof_the_pre_fix_factory_fails_governance_leak_specifically(self) -> None:
        v = v3_gate(lambda fx: OldFactoryTier(), "old", self.SEEDS_, self.FAMS_)
        self.assertFalse(crit(v, "governance-leak").passed, v.render())
        self.assertTrue(crit(v, "A2-stale-evidence").passed, v.render())  # only the ceiling is broken
        self.assertNotIn("pool-size", v.failed)  # we reached the governance criteria, not a pooling refusal

    @unittest.skipUnless(os.environ.get("PMB1_SLOW") == "1", "full 3 seeds x 2 families matrix: set PMB1_SLOW=1")
    def test_full_matrix_registered_tier_passes_and_pre_fix_factory_fails(self) -> None:
        good = v3_gate(runner.SUBSTRATES["episodic"], "fixed", SEEDS, FAMILIES)
        self.assertTrue(good.passed, good.render())
        bad = v3_gate(lambda fx: OldFactoryTier(), "old", SEEDS, FAMILIES)
        self.assertIn("governance-leak", bad.failed, bad.render())


class TestTheGateCanFailTheTier(unittest.TestCase):
    def test_a_tier_that_ignores_the_budget_fails_the_budget_criterion(self) -> None:
        v = verdict(IgnoresBudget)
        self.assertIn("E3-budget-respected", v.failed, v.render())
        self.assertFalse(v.passed)

    def test_a_tier_that_ignores_forget_fails_the_leak_criterion(self) -> None:
        v = verdict(IgnoresForget)
        self.assertIn("A3-leak-at-or-under-floor", v.failed, v.render())
        self.assertFalse(v.passed)

    def test_skipping_authorization_is_invisible_to_the_v2_gate_but_caught_by_the_unit_tests(self) -> None:
        """FINDING: no v2 fixture item sits above the privacy ceiling or has an unknown purpose, so the v2 gate
        cannot fail a tier that skips INV-6. Pinned so nobody reads the gate's PASS as an authorization proof:
        that proof is ``test_memory_tier_episodic.TestAuthorizationBeforeReading`` (which this patch turns RED)."""
        with mock.patch.object(tier.ev, "evaluate_retrieval", _passthrough):
            recs = []
            for seed in SEEDS:
                fx = fx2.generate_v2(seed, SIZE)
                for fam in FAMILIES:
                    recs.append(runner.run_one(lambda: EpisodicTierSubstrate(), StubBackbone(fam), fx, gate=True,
                                               embedder_is_lexical_stub=True))
        v = gate(1, baseline() + recs, candidate="episodic-tier")
        self.assertTrue(v.passed, v.render())

    def test_serving_a_superseded_version_is_caught_by_the_a2_track_not_by_a_v2_gate_criterion(self) -> None:
        """FINDING: A2 is not run on v2, and no gate criterion reads A2. The defect shows in the v1 A2 track."""
        fx = fxm.generate(1, "tiny")
        good = runner.run_one(lambda: EpisodicTierSubstrate(), StubBackbone("strict"), fx, gate=True)
        bad = runner.run_one(lambda: StaleSupersede(), StubBackbone("strict"), fx, gate=True)
        self.assertEqual(good["tracks"]["A2"]["metrics"]["stale_evidence_rate"], 0.0)
        self.assertGreater(bad["tracks"]["A2"]["metrics"]["stale_evidence_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
