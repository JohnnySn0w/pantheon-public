"""Validation smoke for the T-2809 GEPA reviewer eval set.

Run from repo root:
    python3 -m unittest tools.test_gepa_eval_set -v

Reads the COMMITTED eval set at knowledge/eval/reviewer-v1/ directly (this
suite validates the frozen artifact itself, not a fixture reconstruction of
it) and asserts, per the T-2809 dispatch brief's smoke spec:

  1. every case file under cases/ parses as JSON with the required fields;
  2. case_ids are unique;
  3. the train/holdout split partitions cleanly (no overlap, no case left
     unassigned);
  4. every case's source_ticket resolves to an existing tickets/done/*.md
     file;
  5. manifest.json's declared counts (by class, by split, by class-and-split)
     match the actual case files on disk.

This module does not touch the live tickets/ tree beyond a read-only
existence check on tickets/done/<id>.md — no lifecycle CLI, no fixtures
mutate anything.

TestSplitIsReproducible actually re-derives the split (per-class sorted ids,
random.Random(2809).shuffle, cut at round(n * 0.6) -- round(), not int(); the
two diverge for this exact case set, e.g. the positive class has n=31 so
n*0.6=18.6, and round(18.6)=19 while int(18.6)=18 -- and asserts the
re-derivation equals the committed split.json/case-file assignment. Without
this, "the suite re-derives and checks this" in the README would be an
overclaim: the other tests here check the split is a clean PARTITION, not
that it's the SPECIFIC partition the stated method produces. That gap is the
same false-green shape as one of this eval set's own T-0063 findings
(coverage that reads green without proving the mechanism it claims to
guard).
"""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_SET_DIR = REPO_ROOT / "knowledge" / "eval" / "reviewer-v1"
CASES_DIR = EVAL_SET_DIR / "cases"
MANIFEST_PATH = EVAL_SET_DIR / "manifest.json"
EXCLUSIONS_PATH = EVAL_SET_DIR / "exclusions.jsonl"

SPLIT_SEED = 2809
TRAIN_FRACTION = 0.6

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
VALID_CLASSES = {"positive", "negative_invalid_rebound"}
VALID_SPLITS = {"train", "holdout"}
VALID_VERDICTS = {"REBOUND", "APPROVE"}


def _load_cases() -> dict[str, dict]:
    cases = {}
    for path in sorted(CASES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            case = json.load(fh)
        cases[path.name] = case
    return cases


def _load_manifest() -> dict:
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


class TestEvalSetLayout(unittest.TestCase):
    """The directory exists and holds at least the expected file shapes."""

    def test_directory_exists(self):
        self.assertTrue(EVAL_SET_DIR.is_dir(), f"missing {EVAL_SET_DIR}")
        self.assertTrue(CASES_DIR.is_dir(), f"missing {CASES_DIR}")

    def test_manifest_and_exclusions_exist(self):
        self.assertTrue(MANIFEST_PATH.is_file(), f"missing {MANIFEST_PATH}")
        self.assertTrue(EXCLUSIONS_PATH.is_file(), f"missing {EXCLUSIONS_PATH}")

    def test_at_least_one_case_file(self):
        case_files = list(CASES_DIR.glob("*.json"))
        self.assertGreater(len(case_files), 0, "cases/ directory has no *.json files")


class TestEveryCaseFileParses(unittest.TestCase):
    """Every case file is valid JSON with the required field shape."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()

    def test_at_least_one_case_loaded(self):
        self.assertGreater(len(self.cases), 0)

    def test_every_case_has_required_top_fields(self):
        for filename, case in self.cases.items():
            missing = REQUIRED_TOP_FIELDS - set(case.keys())
            self.assertFalse(missing, f"{filename} missing top-level fields: {missing}")

    def test_every_case_class_is_valid(self):
        for filename, case in self.cases.items():
            self.assertIn(case["class"], VALID_CLASSES, f"{filename} has invalid class {case['class']!r}")

    def test_every_case_split_is_valid(self):
        for filename, case in self.cases.items():
            self.assertIn(case["split"], VALID_SPLITS, f"{filename} has invalid split {case['split']!r}")

    def test_every_case_expected_block_is_well_formed(self):
        for filename, case in self.cases.items():
            expected = case["expected"]
            missing = REQUIRED_EXPECTED_FIELDS - set(expected.keys())
            self.assertFalse(missing, f"{filename} expected block missing fields: {missing}")
            self.assertIn(
                expected["verdict"], VALID_VERDICTS,
                f"{filename} has invalid verdict {expected['verdict']!r}",
            )
            self.assertIsInstance(expected["findings"], list, f"{filename} findings must be a list")
            self.assertIsInstance(expected["must_not_flag"], list, f"{filename} must_not_flag must be a list")

    def test_positive_cases_carry_at_least_one_finding(self):
        for filename, case in self.cases.items():
            if case["class"] == "positive":
                self.assertGreater(
                    len(case["expected"]["findings"]), 0,
                    f"{filename} is class=positive but expected.findings is empty",
                )

    def test_negative_cases_carry_at_least_one_must_not_flag(self):
        for filename, case in self.cases.items():
            if case["class"] == "negative_invalid_rebound":
                self.assertGreater(
                    len(case["expected"]["must_not_flag"]), 0,
                    f"{filename} is class=negative_invalid_rebound but expected.must_not_flag is empty",
                )

    def test_filename_matches_case_id(self):
        for filename, case in self.cases.items():
            self.assertEqual(filename, f"{case['case_id']}.json", f"filename/case_id mismatch: {filename}")


class TestCaseIdsUnique(unittest.TestCase):
    def test_case_ids_are_unique(self):
        cases = _load_cases()
        ids = [case["case_id"] for case in cases.values()]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate case_id(s) found: {ids}")


class TestSplitPartitionsCleanly(unittest.TestCase):
    """train/holdout must be a clean partition of the full case set: no overlap, no gaps."""

    def test_train_and_holdout_are_disjoint_and_complete(self):
        cases = _load_cases()
        all_ids = {case["case_id"] for case in cases.values()}
        train_ids = {case["case_id"] for case in cases.values() if case["split"] == "train"}
        holdout_ids = {case["case_id"] for case in cases.values() if case["split"] == "holdout"}

        self.assertTrue(train_ids.isdisjoint(holdout_ids), "train and holdout overlap")
        self.assertEqual(train_ids | holdout_ids, all_ids, "train ∪ holdout does not equal the full case set")
        self.assertEqual(len(train_ids) + len(holdout_ids), len(all_ids), "split counts don't sum to total")


def _rederive_split(cases: dict[str, dict]) -> dict[str, str]:
    """Re-derive the train/holdout split per the README's stated method.

    Per class: sort case_ids, shuffle with random.Random(SPLIT_SEED), cut at
    round(n * TRAIN_FRACTION) -- round(), not int() (they diverge for this
    case set; see module docstring). Returns {case_id: "train"|"holdout"}.
    """
    by_class: dict[str, list[str]] = {}
    for case in cases.values():
        by_class.setdefault(case["class"], []).append(case["case_id"])

    rederived: dict[str, str] = {}
    for klass, ids in by_class.items():
        ids = sorted(ids)
        rng = random.Random(SPLIT_SEED)
        rng.shuffle(ids)
        cut = round(len(ids) * TRAIN_FRACTION)
        for case_id in ids[:cut]:
            rederived[case_id] = "train"
        for case_id in ids[cut:]:
            rederived[case_id] = "holdout"
    return rederived


class TestSplitIsReproducible(unittest.TestCase):
    """The committed split must equal what the stated method actually produces.

    TestSplitPartitionsCleanly only checks the committed split is A clean
    partition -- not that it's THE partition the README's split-method
    description claims to produce. This test closes that gap by re-deriving
    the split from scratch (per-class sorted ids, seeded shuffle, round()
    cut) and comparing case-by-case against what's on disk.
    """

    def test_rederived_split_matches_committed_assignment(self):
        cases = _load_cases()
        rederived = _rederive_split(cases)
        committed = {case["case_id"]: case["split"] for case in cases.values()}
        self.assertEqual(
            rederived, committed,
            "re-derived split (per README's stated method: per-class sorted ids, "
            "random.Random(2809).shuffle, cut at round(n*0.6)) does not match the "
            "committed split -- the split-method description in README.md and "
            "manifest.json would be an overclaim",
        )

    def test_rederived_split_uses_round_not_int_at_the_divergence_point(self):
        # Documents WHY round() matters for this exact case set: the positive
        # class (n=31) is the concrete case where round() and int() disagree.
        cases = _load_cases()
        n_positive = sum(1 for case in cases.values() if case["class"] == "positive")
        exact = n_positive * TRAIN_FRACTION
        self.assertNotEqual(
            round(exact), int(exact),
            f"positive class n={n_positive} no longer sits at a round()/int() "
            "divergence point -- if the case count changed, re-verify which "
            "rounding convention the committed split actually used before "
            "trusting this test's title",
        )


class TestSourceTicketsExist(unittest.TestCase):
    """Every case's source_ticket must resolve to a real tickets/done/*.md file."""

    def test_every_source_ticket_resolves_in_tickets_done(self):
        cases = _load_cases()
        for filename, case in cases.items():
            ticket_path = REPO_ROOT / case["source_path"]
            self.assertTrue(
                ticket_path.is_file(),
                f"{filename}: source_path {case['source_path']} does not exist",
            )
            expected_path = REPO_ROOT / "tickets" / "done" / f"{case['source_ticket']}.md"
            self.assertEqual(
                ticket_path.resolve(), expected_path.resolve(),
                f"{filename}: source_path does not point at tickets/done/{case['source_ticket']}.md",
            )


class TestManifestCountsMatchCaseFiles(unittest.TestCase):
    """manifest.json's declared counts must equal what's actually on disk."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _load_cases()
        cls.manifest = _load_manifest()

    def test_total_count_matches(self):
        self.assertEqual(self.manifest["counts"]["total"], len(self.cases))

    def test_by_class_counts_match(self):
        actual: dict[str, int] = {}
        for case in self.cases.values():
            actual[case["class"]] = actual.get(case["class"], 0) + 1
        self.assertEqual(self.manifest["counts"]["by_class"], actual)

    def test_by_split_counts_match(self):
        actual = {"train": 0, "holdout": 0}
        for case in self.cases.values():
            actual[case["split"]] += 1
        self.assertEqual(self.manifest["counts"]["by_split"], actual)

    def test_by_class_and_split_counts_match(self):
        actual: dict[str, dict[str, int]] = {}
        for case in self.cases.values():
            klass = case["class"]
            actual.setdefault(klass, {"train": 0, "holdout": 0})
            actual[klass][case["split"]] += 1
        self.assertEqual(self.manifest["counts"]["by_class_and_split"], actual)

    def test_manifest_case_list_matches_case_files(self):
        manifest_ids = {entry["case_id"] for entry in self.manifest["cases"]}
        actual_ids = {case["case_id"] for case in self.cases.values()}
        self.assertEqual(manifest_ids, actual_ids)

    def test_manifest_case_list_class_and_split_match(self):
        by_id = {case["case_id"]: case for case in self.cases.values()}
        for entry in self.manifest["cases"]:
            case = by_id[entry["case_id"]]
            self.assertEqual(entry["class"], case["class"], f"{entry['case_id']}: class mismatch vs manifest")
            self.assertEqual(entry["split"], case["split"], f"{entry['case_id']}: split mismatch vs manifest")
            self.assertEqual(
                entry["source_ticket"], case["source_ticket"],
                f"{entry['case_id']}: source_ticket mismatch vs manifest",
            )


class TestExclusionsLogWellFormed(unittest.TestCase):
    """exclusions.jsonl parses and every line names a resolvable ticket."""

    def test_every_line_is_valid_json_with_required_fields(self):
        with EXCLUSIONS_PATH.open(encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        self.assertGreater(len(lines), 0, "exclusions.jsonl has no entries")
        for line in lines:
            entry = json.loads(line)
            for field in ("ticket_id", "round", "classification", "reason"):
                self.assertIn(field, entry, f"exclusions.jsonl entry missing {field}: {entry}")

    def test_excluded_tickets_are_not_also_shipped_as_cases(self):
        cases = _load_cases()
        shipped_source_tickets = {case["source_ticket"] for case in cases.values()}
        with EXCLUSIONS_PATH.open(encoding="utf-8") as fh:
            excluded_ids = {json.loads(line)["ticket_id"] for line in fh if line.strip()}
        overlap = shipped_source_tickets & excluded_ids
        self.assertFalse(overlap, f"tickets both shipped and excluded: {overlap}")


if __name__ == "__main__":
    unittest.main()
