"""Validation smoke for the T-3837 GEPA reviewer eval set v2.

Run from repo root:
    python3 -m unittest tools.test_gepa_eval_set_v2 -v

Reads the eval set at knowledge/eval/reviewer-v2/ directly and asserts the
reviewer-v2 shape: four classes (positive, negative_invalid_rebound,
clean_approve_control, escalate), splits train/holdout (plus `deferred`, now
unused), and the stratified split described in manifest.json is what actually
landed.

T-4573: the escalate class was parked as `deferred` until scorer.parse_verdict
learned ESCALATE. #451 taught it, but the split was never regenerated and the
test below pinned the deferral, so the v2 run scored zero ESCALATE cases and its
accepted mutation dropped ESCALATE again. TestEveryClassIsScoreable is the
mechanism that closes that gap: every class must reach both train and holdout,
and every expected verdict must be one parse_verdict actually returns.

Sibling of `test_gepa_eval_set.py` (which pins reviewer-v1 shape: two classes,
two splits, seed 2809, holdout=1-train_fraction). Kept as a parallel module so
either eval-set version can be verified independently; the two do not share
constants.

TestSplitIsReproducibleV2 re-derives the split from scratch (per-class sorted
ids, a fresh `random.Random(seed)` per class, cut at `round(n * holdout_frac)`
into holdout-first, remainder train) and asserts case-by-case equality against
the committed split.json/case-file assignment.
"""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_SET_DIR = REPO_ROOT / "knowledge" / "eval" / "reviewer-v2"
CASES_DIR = EVAL_SET_DIR / "cases"
MANIFEST_PATH = EVAL_SET_DIR / "manifest.json"

VALID_CLASSES = {"positive", "negative_invalid_rebound", "clean_approve_control", "escalate"}
VALID_SPLITS = {"train", "holdout", "deferred"}
VALID_VERDICTS = {"REBOUND", "APPROVE", "ESCALATE"}
# Classes whose cases are parked outside train/holdout. Empty since T-4573;
# adding one should be rare and must come with a scorer reason.
DEFERRED_CLASSES: set[str] = set()

REQUIRED_TOP_FIELDS = {
    "case_id",
    "class",
    "split",
    "source_ticket",
    "source_path",
    "provenance",
    "review_material",
    "expected",
    "curation",
}
REQUIRED_EXPECTED_FIELDS = {"verdict", "findings", "must_not_flag"}


def _load_cases() -> dict[str, dict]:
    cases = {}
    for path in sorted(CASES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            cases[path.name] = json.load(fh)
    return cases


def _load_manifest() -> dict:
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


class TestEvalSetV2Layout(unittest.TestCase):
    def test_directory_exists(self):
        self.assertTrue(EVAL_SET_DIR.is_dir(), f"missing {EVAL_SET_DIR}")
        self.assertTrue(CASES_DIR.is_dir(), f"missing {CASES_DIR}")

    def test_manifest_exists(self):
        self.assertTrue(MANIFEST_PATH.is_file(), f"missing {MANIFEST_PATH}")

    def test_at_least_one_case_file(self):
        self.assertGreater(len(list(CASES_DIR.glob("*.json"))), 0)


class TestEveryCaseFileParsesV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()

    def test_every_case_has_required_top_fields(self):
        for filename, case in self.cases.items():
            missing = REQUIRED_TOP_FIELDS - set(case.keys())
            self.assertFalse(missing, f"{filename} missing top-level fields: {missing}")

    def test_every_case_class_is_valid(self):
        for filename, case in self.cases.items():
            self.assertIn(case["class"], VALID_CLASSES, f"{filename} class {case['class']!r}")

    def test_every_case_split_is_valid(self):
        for filename, case in self.cases.items():
            self.assertIn(case["split"], VALID_SPLITS, f"{filename} split {case['split']!r}")

    def test_every_case_expected_verdict_is_valid(self):
        for filename, case in self.cases.items():
            v = case["expected"]["verdict"]
            self.assertIn(v, VALID_VERDICTS, f"{filename} verdict {v!r}")

    def test_positive_cases_carry_at_least_one_finding(self):
        for filename, case in self.cases.items():
            if case["class"] == "positive":
                self.assertGreater(len(case["expected"]["findings"]), 0, f"{filename} positive but no findings")

    def test_deferred_split_matches_deferred_classes(self):
        for filename, case in self.cases.items():
            if case["class"] in DEFERRED_CLASSES:
                self.assertEqual(case["split"], "deferred", f"{filename} in a deferred class but split")
            else:
                self.assertIn(case["split"], {"train", "holdout"}, f"{filename} deferred but class is scoreable")


class TestEveryClassIsScoreable(unittest.TestCase):
    """T-4573: a class the optimizer never scores is a verdict it can drop for free."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()

    def test_every_expected_verdict_round_trips_through_parse_verdict(self):
        from tools.gepa.scorer import parse_verdict

        for verdict in sorted({c["expected"]["verdict"] for c in self.cases.values()}):
            self.assertEqual(
                parse_verdict(f"Verdict: {verdict}"), verdict,
                f"scorer.parse_verdict cannot return {verdict!r}; cases expecting it can never score",
            )

    def test_every_class_reaches_train_and_holdout(self):
        by_class: dict[str, set[str]] = {}
        for c in self.cases.values():
            by_class.setdefault(c["class"], set()).add(c["split"])
        for klass, splits in sorted(by_class.items()):
            if klass in DEFERRED_CLASSES:
                continue
            self.assertIn("train", splits, f"class {klass} has no train case")
            self.assertIn("holdout", splits, f"class {klass} has no holdout case")

    def test_every_expected_verdict_reaches_holdout(self):
        holdout_verdicts = {c["expected"]["verdict"] for c in self.cases.values() if c["split"] == "holdout"}
        all_verdicts = {c["expected"]["verdict"] for c in self.cases.values()}
        self.assertEqual(holdout_verdicts, all_verdicts, "some expected verdict is never measured on holdout")


class TestCaseIdsUniqueV2(unittest.TestCase):
    def test_case_ids_are_unique(self):
        cases = _load_cases()
        ids = [c["case_id"] for c in cases.values()]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate case_id(s): {ids}")


def _rederive_split_v2(cases: dict[str, dict], seed: int, holdout_frac: float) -> dict[str, str]:
    """Re-derive the v2 stratified split per manifest.split_method.

    For each class: sort ids ascending; a FRESH `random.Random(seed)` per class
    (empirically verified against the committed set — a single shared RNG
    consumed across classes does not reproduce); shuffle in-place; cut at
    `round(n * holdout_frac)` into holdout, clamped to [1, n-1] when n >= 2
    (the generator's stratification guarantee), remainder train. A class in
    DEFERRED_CLASSES routes wholesale to `deferred`.
    """
    by_class: dict[str, list[str]] = {}
    for c in cases.values():
        by_class.setdefault(c["class"], []).append(c["case_id"])

    rederived: dict[str, str] = {}
    for klass, ids in by_class.items():
        ids_sorted = sorted(ids)
        if klass in DEFERRED_CLASSES:
            for cid in ids_sorted:
                rederived[cid] = "deferred"
            continue
        rng = random.Random(seed)
        rng.shuffle(ids_sorted)
        h_count = round(len(ids_sorted) * holdout_frac)
        if len(ids_sorted) >= 2:
            h_count = max(1, min(h_count, len(ids_sorted) - 1))
        for cid in ids_sorted[:h_count]:
            rederived[cid] = "holdout"
        for cid in ids_sorted[h_count:]:
            rederived[cid] = "train"
    return rederived


class TestSplitIsReproducibleV2(unittest.TestCase):
    """The committed v2 split must equal what the manifest's stated method produces."""

    def test_rederived_split_matches_committed_assignment(self):
        cases = _load_cases()
        manifest = _load_manifest()
        seed = manifest["split_method"]["seed"]
        holdout_frac = manifest["split_method"]["holdout_frac"]

        rederived = _rederive_split_v2(cases, seed=seed, holdout_frac=holdout_frac)
        committed = {c["case_id"]: c["split"] for c in cases.values()}
        self.assertEqual(
            rederived, committed,
            "re-derived v2 split (per manifest.split_method: per-class sorted ids, "
            f"random.Random({seed}) per class, holdout_frac={holdout_frac}, holdout-first cut) "
            "does not match the committed split -- the split-method description would be an overclaim",
        )

    def test_scoreable_class_holdout_count_meets_min_floor(self):
        cases = _load_cases()
        manifest = _load_manifest()
        min_holdout = manifest["split_method"]["min_holdout"]
        actual_holdout = sum(1 for c in cases.values() if c["split"] == "holdout")
        self.assertGreaterEqual(
            actual_holdout, min_holdout,
            f"committed holdout {actual_holdout} below manifest min_holdout {min_holdout}",
        )

    def test_each_multi_case_scoreable_class_keeps_at_least_one_holdout(self):
        cases = _load_cases()
        by_class: dict[str, list[dict]] = {}
        for c in cases.values():
            by_class.setdefault(c["class"], []).append(c)
        for klass, cs in by_class.items():
            if klass in DEFERRED_CLASSES or len(cs) < 2:
                continue
            holdout_count = sum(1 for c in cs if c["split"] == "holdout")
            self.assertGreaterEqual(
                holdout_count, 1,
                f"class {klass} has {len(cs)} cases but 0 holdout",
            )


class TestSourceTicketsExistV2(unittest.TestCase):
    def test_every_source_path_resolves(self):
        cases = _load_cases()
        for filename, case in cases.items():
            path = REPO_ROOT / case["source_path"]
            self.assertTrue(path.is_file(), f"{filename}: source_path {case['source_path']} missing")


class TestManifestCountsMatchCaseFilesV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()
        cls.manifest = _load_manifest()

    def test_total_count_matches(self):
        self.assertEqual(self.manifest["counts"]["total"], len(self.cases))

    def test_by_class_counts_match(self):
        actual: dict[str, int] = {}
        for c in self.cases.values():
            actual[c["class"]] = actual.get(c["class"], 0) + 1
        self.assertEqual(self.manifest["counts"]["by_class"], actual)

    def test_by_split_counts_match(self):
        actual = {"train": 0, "holdout": 0, "deferred": 0}
        for c in self.cases.values():
            actual[c["split"]] += 1
        self.assertEqual(self.manifest["counts"]["by_split"], actual)

    def test_by_class_and_split_counts_match(self):
        actual: dict[str, dict[str, int]] = {}
        for c in self.cases.values():
            actual.setdefault(c["class"], {"train": 0, "holdout": 0, "deferred": 0})
            actual[c["class"]][c["split"]] += 1
        self.assertEqual(self.manifest["counts"]["by_class_and_split"], actual)


if __name__ == "__main__":
    unittest.main()
