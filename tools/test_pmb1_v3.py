#!/usr/bin/env python3
"""PMB-1 fixture v3 closes the tier-1 gate's governance blind spots (T-2518 step 2b').

On v2 a tier with authorization skipped, or one serving the superseded version,
PASSED the gate. These tests drive the REAL gate path (real substrates, real
runner, real records, tiny x 3 seeds x 2 stub families, offline guard on) and
prove that on v3 each sabotage FAILS the criterion that names it, that governed
itself passes, and that the controls (which have no authorization) leak without
failing a governed candidate. The sabotage substrates are self-contained test
doubles built on the real governed store.
"""
from __future__ import annotations

import itertools
import sys
import types
import unittest
from unittest import mock

from tools.pmb1 import fixtures as fx1
from tools.pmb1 import fixtures_v2 as fx2
from tools.pmb1 import fixtures_v3 as fx3
from tools.pmb1 import governed as govmod
from tools.pmb1 import offline, runner
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.gate import gate
from tools.pmb1.governed import GovernedStoreSubstrate
from tools.pmb1.records import validate_record
from tools.pmb1.fixtures import PURPOSE
from tools.pmb1.substrate import Evidence
from tools.pmb1.tracks_v3 import track_gov

gatem = sys.modules["tools.pmb1.gate"]

SEEDS = (1, 2, 3)
FAMILIES = ("strict", "lenient")
SIZE = "tiny"
_GUARD = None
_REAL: dict[str, list[dict]] = {}

# v1 / v2 are pinned: the v3 work must not move a byte of either.
V1_SHAS = {1: "962ef15d4f75baad", 2: "c9cdceb0b45c4fba", 3: "295df924333fba66"}
V2_SHAS = {1: "24cb378d9921c32f", 2: "292e9470ef9f4eba", 3: "16376fad118e3f2d"}


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install()


def tearDownModule() -> None:
    _GUARD.close()


class _PassThroughEv:
    """``evaluate_retrieval`` replaced by a pass-through: every candidate is 'authorized'."""

    @staticmethod
    def evaluate_retrieval(request, envelopes, policy):
        items = [types.SimpleNamespace(memory_id=e.memory_id, version_id=e.version_id,
                                       use_disposition=types.SimpleNamespace(value="allowed")) for e in envelopes]
        return types.SimpleNamespace(items=items)


class SkipsAuthorization(GovernedStoreSubstrate):
    """Governed, with authorization bypassed (INV-6 broken): ceiling and purpose are never checked."""
    name = "tier-skips-authorization"

    def _retrieve_memoised(self, query, purpose, budget_tokens):
        with mock.patch.object(govmod, "ev", _PassThroughEv):
            return super()._retrieve_memoised(query, purpose, budget_tokens)


class ServesSuperseded(GovernedStoreSubstrate):
    """Governed, but retrieval serves the SUPERSEDED version of a corrected memory."""
    name = "tier-serves-superseded"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._old: dict[str, Evidence] = {}

    def _supersede(self, ref, new_item):
        old = Evidence(self.store.read_payload(*self._split(ref)), ref)
        new_ref = super()._supersede(ref, new_item)
        self._old[new_ref] = old
        return new_ref

    def _retrieve(self, query, purpose, budget_tokens):
        return [self._old.get(e.ref, e) for e in super()._retrieve(query, purpose, budget_tokens)]


def _passthrough(sub, q, p, b):
    with mock.patch.object(govmod, "ev", _PassThroughEv):
        return GovernedStoreSubstrate._retrieve_memoised(sub, q, p, b)


class PurposeOnlyLeak(GovernedStoreSubstrate):
    """Ceiling enforced, purpose ignored."""
    name = "adv-purpose-only"

    def _retrieve_memoised(self, q, p, b):
        return super()._retrieve_memoised(q, PURPOSE, b)


class LeakWhenNotTop(GovernedStoreSubstrate):
    """Honest when the restricted item would be the top hit; leaks restricted items at rank >= 2."""
    name = "adv-leak-when-not-top"

    def _retrieve_memoised(self, q, p, b):
        ok = super()._retrieve_memoised(q, p, b)
        if not ok:
            return ok
        k, self.top_k = self.top_k, 10**6
        try:
            everything = _passthrough(self, q, p, 10**9)
            auth = {e.ref for e in super()._retrieve_memoised(q, p, 10**9)}
        finally:
            self.top_k = k
        if everything and everything[0].ref not in auth:
            return ok
        return ok + [e for e in everything if e.ref not in auth][:2]


class ExtractLeak(GovernedStoreSubstrate):
    """Leaks restricted content as a fresh-ref extract with the ceiling probes' values dropped."""
    name = "adv-extract-leak"
    drop: set = set()

    @classmethod
    def prepare(cls, fx) -> None:
        cls.drop = {p.value for p in fx.gov_probes if p.kind == "ceiling"}

    def _retrieve_memoised(self, q, p, b):
        ok = super()._retrieve_memoised(q, p, b)
        have = {e.ref for e in ok}
        return ok + [Evidence("extract: " + " ".join(w for w in e.text.split() if not any(v in w.lower() for v in self.drop)),
                              "derived-x") for e in _passthrough(self, q, p, b) if e.ref not in have and p == PURPOSE]


class ParaphraseLeak(GovernedStoreSubstrate):
    """Leaks restricted text hyphenated under a fresh ref."""
    name = "adv-paraphrase-leak"

    def _retrieve_memoised(self, q, p, b):
        ok = super()._retrieve_memoised(q, p, b)
        have = {e.ref for e in ok}
        return ok + [Evidence(" ".join(w[:2] + "-" + w[2:] for w in e.text.split()), "derived-p")
                     for e in _passthrough(self, q, p, b) if e.ref not in have and p == PURPOSE]


class SometimesStale(ServesSuperseded):
    """Serves the superseded version on every third retrieval."""
    name = "adv-sometimes-stale"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._c = itertools.count()

    def _retrieve(self, query, purpose, budget_tokens):
        out = GovernedStoreSubstrate._retrieve(self, query, purpose, budget_tokens)
        return [self._old.get(e.ref, e) if next(self._c) % 3 == 0 else e for e in out]


def adversary(cls) -> list[dict]:
    """Governed's v3 records with ONLY the GOV and A2 tracks re-run on the adversary (real run_one),
    so the gate path is real while the cost stays low: these doubles differ from governed nowhere else."""
    def build():
        out = []
        for gr in of("governed"):
            fx = fx3.generate_v3(gr["fixture"]["seed"], SIZE)
            if hasattr(cls, "prepare"):
                cls.prepare(fx)
            bb = StubBackbone(gr["backbone"]["family"].removeprefix("stub-"))
            part = runner.run_one(lambda: cls(privacy_ceiling=fx.privacy_ceiling), bb, fx, gate=True,
                                  tracks=("GOV", "A2"), embedder_is_lexical_stub=True)
            rec = {**gr, "substrate": {**gr["substrate"], "name": cls.name},
                   "tracks": {**gr["tracks"], "GOV": part["tracks"]["GOV"], "A2": part["tracks"]["A2"]},
                   "samples": {**gr["samples"], **{k: v for k, v in part["samples"].items() if k[:3] in ("GOV", "A2.")}}}
            out.append(rec)
        return out
    key = f"adv/{cls.__name__}"
    if key not in _REAL:
        _REAL[key] = build()
    return _REAL[key]


def real(key: str, make, version: int = 3) -> list[dict]:
    """Records for ``make(fx)`` over every seed x family (cached)."""
    k = f"{key}/v{version}"
    if k not in _REAL:
        out = []
        for seed in SEEDS:
            fx = fx3.generate_by_version(seed, SIZE, version)
            for fam in FAMILIES:
                out.append(runner.run_one(make(fx), StubBackbone(fam), fx, gate=True, embedder_is_lexical_stub=True))
        _REAL[k] = out
    return _REAL[k]


def of(name: str, version: int = 3) -> list[dict]:
    return real(name, lambda fx: (lambda: runner.SUBSTRATES[name](fx)), version)


def sabotage(cls, version: int = 3) -> list[dict]:
    return real(cls.__name__, lambda fx: (lambda: cls(privacy_ceiling=getattr(fx, "privacy_ceiling", "personal"))), version)


def controls(version: int = 3) -> list[dict]:
    return [r for n in ("rag", "random", "full", "amnesiac") for r in of(n, version)]


def crit(v, name):
    return next(c for c in v.criteria if c.name == name)


def mean(recs, name, key):
    xs = [x for r in recs if r["substrate"]["name"] == name for x in r["samples"][key]]
    return sum(xs) / len(xs)


class TestV1V2Untouched(unittest.TestCase):
    def test_shas_are_pinned(self) -> None:
        for s in SEEDS:
            self.assertEqual(fx1.generate(s, SIZE).sha256[:16], V1_SHAS[s])
            self.assertEqual(fx2.generate_v2(s, SIZE).sha256[:16], V2_SHAS[s])

    def test_v2_spec_and_records_keep_their_version(self) -> None:
        self.assertEqual(fx3.parse_fixture_spec("seed=1,size=tiny,version=2"), (1, "tiny", 2))
        self.assertEqual(fx3.generate_from_spec("seed=1,size=tiny,version=2").sha256, fx2.generate_v2(1, SIZE).sha256)
        rec = of("amnesiac", 2)[0]
        self.assertEqual(rec["fixture"]["version"], 2)
        self.assertEqual(rec["not_run"][-3:], ["A2", "A7", "E4"])
        self.assertNotIn("GOV", rec["tracks"])


class TestV3Fixture(unittest.TestCase):
    def test_deterministic_and_distinct_from_v2(self) -> None:
        a, b = fx3.generate_v3(1, SIZE), fx3.generate_v3(1, SIZE)
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual(a.sha256, fx3.generate_v3(2, SIZE).sha256)
        self.assertNotEqual(a.sha256, fx2.generate_v2(1, SIZE).sha256)
        self.assertEqual(fx3.generate_from_spec("seed=1,size=tiny,version=3").sha256, a.sha256)

    def test_v3_is_v2_plus_the_new_axes(self) -> None:
        v2, v3 = fx2.generate_v2(1, SIZE), fx3.generate_v3(1, SIZE)
        self.assertEqual((v3.episodes, v3.probes, v3.forgets), (v2.episodes, v2.probes, v2.forgets))
        self.assertEqual(v3.version, 3)

    def test_axes_are_nonzero_and_well_formed(self) -> None:
        for s in SEEDS:
            fx = fx3.generate_v3(s, SIZE)
            kinds = [p.kind for p in fx.gov_probes]
            self.assertGreater(len(fx.restricted), 0)
            self.assertGreater(kinds.count("ceiling"), 0)
            self.assertGreater(kinds.count("purpose"), 0)
            self.assertGreater(kinds.count("comention"), 0)
            self.assertEqual(len(fx.restricted_tokens), 3 * len(fx.restricted))
            self.assertGreater(len(fx.supersedes), 0)
            self.assertGreater(len(fx.restricted_class), 0)
            self.assertNotEqual(fx.restricted_class, fx.privacy_ceiling)
            # a ceiling probe's ONLY lexical match is its restricted episode
            ordinary = " ".join(t.lower() for _, _, t in fx.episodes)
            for p in (q for q in fx.gov_probes if q.kind == "ceiling"):
                self.assertNotIn(p.entity.lower(), ordinary)
                self.assertNotIn(p.value, ordinary)
                self.assertEqual(sum(p.value in t for _, _, t in fx.restricted), 1)
            # the denied purpose is not the default one, and the superseded value is new and gone from the new text
            self.assertTrue(all(p.purpose != fx1.PURPOSE for p in fx.gov_probes if p.kind == "purpose"))
            for u in fx.supersedes:
                self.assertNotIn(u.old_value, u.new_text.lower().split())
                self.assertIn(u.new_value, u.new_text)
                self.assertNotIn(u.new_value, ordinary)

    def test_records_validate_and_carry_the_new_samples(self) -> None:
        rec = of("amnesiac")[0]
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["fixture"]["version"], 3)
        self.assertEqual(rec["not_run"][-2:], ["A7", "E4"])
        for k in ("GOV.leak", "A2.stale_evidence", "A2.new_value"):
            self.assertTrue(rec["samples"][k])
        broken = {**rec, "samples": {k: v for k, v in rec["samples"].items() if k != "GOV.leak"}}
        self.assertTrue(any("GOV.leak" in e for e in validate_record(broken)))


class TestGovernedPassesV3(unittest.TestCase):
    def test_governed_has_zero_leak_and_zero_stale_and_passes_the_gate(self) -> None:
        recs = of("governed") + controls()
        for r in (x for x in recs if x["substrate"]["name"] == "governed-store"):
            self.assertEqual(sum(r["samples"]["GOV.leak"]), 0.0)
            self.assertEqual(sum(r["samples"]["A2.stale_evidence"]), 0.0)
            self.assertGreater(r["tracks"]["A2"]["metrics"]["new_value_accuracy"], 0.0)
        v = gate(1, recs, candidate="governed-store")
        self.assertTrue(crit(v, "governance-leak").passed, v.render())
        self.assertTrue(crit(v, "A2-stale-evidence").passed, v.render())
        self.assertEqual(v.failed, (), v.render())

    def test_controls_still_order_correctly_on_v3(self) -> None:
        recs = of("governed") + controls()
        rnd, rag, full = (mean(recs, n, "A1.accuracy") for n in ("random-control", "verbatim-rag", "full-context-ceiling"))
        self.assertLess(rnd, rag)
        self.assertLess(rag, full)
        self.assertGreater(mean(recs, "verbatim-rag", "E3.utility"), mean(recs, "random-control", "E3.utility"))
        self.assertEqual(mean(recs, "full-context-ceiling", "E3.utility"), 0.0)


class TestSabotageFailsV3(unittest.TestCase):
    def test_skipping_authorization_fails_governance_leak_and_nothing_else(self) -> None:
        recs = sabotage(SkipsAuthorization) + controls()
        v = gate(1, recs, candidate=SkipsAuthorization.name)
        self.assertIn("governance-leak", v.failed, v.render())
        self.assertEqual(v.failed, ("governance-leak",), v.render())
        self.assertTrue(crit(v, "A2-stale-evidence").passed)
        self.assertGreater(mean(recs, SkipsAuthorization.name, "GOV.leak"), 0.0)

    def test_the_same_sabotage_passes_v2_the_blind_spot_this_closes(self) -> None:
        v = gate(1, sabotage(SkipsAuthorization, 2) + controls(2), candidate=SkipsAuthorization.name)
        self.assertEqual(v.failed, (), v.render())

    def test_serving_the_superseded_version_fails_a2_stale_evidence_and_nothing_else(self) -> None:
        recs = sabotage(ServesSuperseded) + controls()
        v = gate(1, recs, candidate=ServesSuperseded.name)
        self.assertEqual(v.failed, ("A2-stale-evidence",), v.render())
        self.assertEqual(mean(recs, ServesSuperseded.name, "GOV.leak"), 0.0)
        self.assertEqual(mean(recs, ServesSuperseded.name, "A2.stale_evidence"), 1.0)

    def test_a_tier_over_governed_is_held_to_zero_stale_and_to_governed(self) -> None:
        name = "tier-over-governed-serves-superseded"
        recs = [{**r, "substrate": {**r["substrate"], "name": name, "over": "governed"}} for r in sabotage(ServesSuperseded)]
        v = gate(1, recs + of("governed") + controls(), candidate=name)
        self.assertIn("A2-stale-evidence", v.failed, v.render())

    def test_a_single_leak_fails(self) -> None:
        recs = [dict(r) for r in sabotage(SkipsAuthorization) + controls()]
        for r in recs:
            if r["substrate"]["name"] == SkipsAuthorization.name:
                r["samples"] = {**r["samples"], "GOV.leak": [0.0] * (len(r["samples"]["GOV.leak"]) - 1) + [1.0]}
        self.assertIn("governance-leak", gate(1, recs, candidate=SkipsAuthorization.name).failed)


class TestAdversariesFailV3(unittest.TestCase):
    """The reviewer's adversaries (T-2518 s2e REBOUND 1): each is RED through the real gate."""

    def failed(self, cls):
        return gate(1, adversary(cls) + controls(), candidate=cls.name)

    def test_leaking_a_restricted_item_that_is_not_the_top_hit_fails(self) -> None:
        v = self.failed(LeakWhenNotTop)
        self.assertEqual(v.failed, ("governance-leak",), v.render())
        recs = adversary(LeakWhenNotTop)
        fx = fx3.generate_v3(1, SIZE)
        kinds = [p.kind for p in fx.gov_probes]
        leaks = [x for r in recs if r["fixture"]["seed"] == 1 for x in r["samples"]["GOV.leak"]]
        # it is the co-mention probes that catch it: ceiling and purpose probes alone score 0
        self.assertEqual(sum(l for l, k in zip(leaks, kinds) if k != "comention"), 0.0)
        self.assertGreater(sum(l for l, k in zip(leaks, kinds) if k == "comention"), 0.0)

    def test_an_extract_under_a_fresh_ref_fails(self) -> None:
        self.assertEqual(self.failed(ExtractLeak).failed, ("governance-leak",))

    def test_a_paraphrase_under_a_fresh_ref_fails(self) -> None:
        self.assertEqual(self.failed(ParaphraseLeak).failed, ("governance-leak",))

    def test_ignoring_only_the_purpose_fails(self) -> None:
        self.assertEqual(self.failed(PurposeOnlyLeak).failed, ("governance-leak",))

    def test_serving_the_superseded_version_sometimes_fails(self) -> None:
        v = self.failed(SometimesStale)
        self.assertEqual(v.failed, ("A2-stale-evidence",), v.render())
        self.assertTrue(0.0 < mean(adversary(SometimesStale), SometimesStale.name, "A2.stale_evidence") < 1.0)

    def test_governed_stays_green_on_the_same_path(self) -> None:
        self.assertEqual(gate(1, of("governed") + controls(), candidate="governed-store").failed, ())

    def test_governed_records_for_only_some_seeds_do_not_crash_the_bound(self) -> None:
        partial = [r for r in of("governed") if r["fixture"]["seed"] == 1]
        v = gate(1, adversary(SometimesStale) + partial + controls(), candidate=SometimesStale.name)
        self.assertEqual(crit(v, "A2-stale-evidence").numbers["stub-strict"]["bound"], 0.0)
        self.assertIn("A2-stale-evidence", v.failed)


class TestProbesAreAnswerable(unittest.TestCase):
    def test_governed_at_a_ceiling_that_admits_the_restricted_class_leaks_them(self) -> None:
        """POSITIVE control: with the ceiling raised, the same probes DO return restricted content."""
        co = 0.0
        for seed in SEEDS:
            fx = fx3.generate_v3(seed, SIZE)
            sub = GovernedStoreSubstrate(privacy_ceiling=fx.restricted_class)
            try:
                m = track_gov(sub, StubBackbone("strict"), fx).metrics
            finally:
                sub.close()
            self.assertEqual(m["leak_rate_ceiling"], 1.0)
            co += m["leak_rate_comention"]  # a secondary match: reached in some seeds, not all
            self.assertEqual(m["leak_rate_purpose"], 0.0)  # a denied purpose still returns nothing
        self.assertGreater(co, 0.0)

    def test_the_leak_check_catches_an_extract_and_a_paraphrase_by_token(self) -> None:
        from tools.pmb1.tracks_v3 import squash
        fx = fx3.generate_v3(1, SIZE)
        tok = fx.restricted_tokens[2]
        self.assertIn(tok, squash(tok[:2] + "-" + tok[2:]))
        ordinary = squash(" ".join(t for _, _, t in fx.episodes))
        self.assertFalse(any(t in ordinary for t in fx.restricted_tokens))


class TestControlsLeakWithoutFailingTheCandidate(unittest.TestCase):
    def test_rag_random_and_full_context_leak_and_the_verdict_reports_it(self) -> None:
        recs = of("governed") + controls()
        for n in ("verbatim-rag", "full-context-ceiling"):
            self.assertGreater(mean(recs, n, "GOV.leak"), 0.0, n)
        self.assertEqual(mean(recs, "amnesiac-floor", "GOV.leak"), 0.0)
        v = gate(1, recs, candidate="governed-store")
        info = crit(v, "governance-controls")
        self.assertFalse(info.required)
        for n in ("verbatim-rag", "random-control", "full-context-ceiling"):
            self.assertIn(n, info.detail)
        self.assertGreater(info.numbers["stub-strict"]["verbatim-rag"]["governance_leak"], 0.0)
        self.assertTrue(any("leak by construction" in n for n in v.notes))
        self.assertTrue(v.passed, v.render())


class TestGateGuards(unittest.TestCase):
    def test_a_batch_mixing_v2_and_v3_is_refused(self) -> None:
        v = gate(1, of("amnesiac", 2) + of("amnesiac", 3))
        self.assertIn("fixture-versions-consistent", v.failed)

    def test_a_stale_v3_sample_vector_is_caught(self) -> None:
        recs = [dict(r) for r in of("governed") + controls()]
        recs[0]["samples"] = {**recs[0]["samples"], "GOV.leak": recs[0]["samples"]["GOV.leak"][:-1]}
        self.assertIn("fixture-matches-seed", gate(1, recs, candidate="governed-store").failed)

    def test_the_docstring_names_v3(self) -> None:
        self.assertIn("v3 is the version tier-1 gates should use", gatem.__doc__.replace("\n  ", " "))


if __name__ == "__main__":
    unittest.main()
