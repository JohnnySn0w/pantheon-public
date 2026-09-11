"""Tests for tools/gepa/case_loader.py (T-2809 Phase 2).

Run: python3 -m unittest tools.test_gepa_case_loader -v

No `gepa` import anywhere in this module or in case_loader.py itself — this
suite runs identically under system python3 and the gepa venv. Diff
resolution is tested against REAL throwaway git repos built in tempdirs (git
init + commit), never a mocked subprocess — a real `git show` is cheap and
the D11-preferred surface.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.gepa import case_loader
from tools.gepa.case_loader import Case, CaseLoadError, DiffRef, Expected, Finding, ReviewMaterial


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo_with_commit(repo: Path) -> str:
    """Init a real git repo with one commit inside `repo`; return its sha.

    `repo` must already exist and be owned by the caller for cleanup — this helper
    creates no tempdir of its own. Callers use `with tempfile.TemporaryDirectory() as
    tmp:` (self-cleaning) rather than an internal `mkdtemp()` this helper would have to
    remember to remove itself; see `tools/tempfile_hygiene_lint.py`.
    """
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "fixture@example.invalid")
    _run(repo, "config", "user.name", "fixture")
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", "file.txt")
    _run(repo, "commit", "-q", "-m", "fixture commit")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return sha


SAMPLE_CASE_DICT = {
    "case_id": "T-9001",
    "class": "positive",
    "split": "train",
    "source_ticket": "T-9001",
    "source_path": "tickets/done/T-9001.md",
    "provenance": {"requester": "x", "target": "y", "labels": [], "round": None},
    "review_material": {
        "ask_summary": "a summary",
        "diff_ref": {"repo": "pantheon-config", "sha": "abc1234"},
        "review_kind": "diff",
    },
    "expected": {
        "verdict": "REBOUND",
        "findings": [{"category": "some-category", "description": "a defect description"}],
        "must_not_flag": [],
    },
    "curation": {"classification_rationale": "r", "spot_read": True},
}


class TestCaseFromJsonDict(unittest.TestCase):
    def test_parses_all_fields(self):
        case = Case.from_json_dict(SAMPLE_CASE_DICT)
        self.assertEqual(case.case_id, "T-9001")
        self.assertEqual(case.case_class, "positive")
        self.assertEqual(case.split, "train")
        self.assertEqual(case.source_ticket, "T-9001")
        self.assertEqual(case.review_material.ask_summary, "a summary")
        self.assertEqual(case.review_material.diff_ref, DiffRef(repo="pantheon-config", sha="abc1234"))
        self.assertEqual(case.review_material.review_kind, "diff")
        self.assertEqual(case.expected.verdict, "REBOUND")
        self.assertEqual(case.expected.findings, (Finding(category="some-category", description="a defect description"),))
        self.assertEqual(case.expected.must_not_flag, ())

    def test_null_diff_ref_object_parses_as_null_repo_and_sha(self):
        d = dict(SAMPLE_CASE_DICT)
        d["review_material"] = dict(d["review_material"])
        d["review_material"]["diff_ref"] = {"repo": None, "sha": None}
        case = Case.from_json_dict(d)
        self.assertIsNone(case.review_material.diff_ref.repo)
        self.assertIsNone(case.review_material.diff_ref.sha)

    def test_missing_required_field_raises_keyerror(self):
        d = dict(SAMPLE_CASE_DICT)
        del d["expected"]
        with self.assertRaises(KeyError):
            Case.from_json_dict(d)


class TestLoadAllCasesAgainstCommittedSet(unittest.TestCase):
    """These exercise the REAL, frozen, committed eval set."""

    def test_loads_all_35_committed_cases(self):
        cases = case_loader.load_all_cases()
        self.assertEqual(len(cases), 35)

    def test_every_loaded_case_id_matches_its_key(self):
        cases = case_loader.load_all_cases()
        for cid, case in cases.items():
            self.assertEqual(cid, case.case_id)

    def test_load_manifest_matches_load_all_cases_count(self):
        manifest = case_loader.load_manifest()
        cases = case_loader.load_all_cases()
        self.assertEqual(manifest["counts"]["total"], len(cases))


class TestLoadCasesSplitScoping(unittest.TestCase):
    def test_train_split_only_returns_train_cases(self):
        train_cases = case_loader.load_cases("train")
        self.assertGreater(len(train_cases), 0)
        for case in train_cases.values():
            self.assertEqual(case.split, "train")

    def test_holdout_split_only_returns_holdout_cases(self):
        holdout_cases = case_loader.load_cases("holdout")
        self.assertGreater(len(holdout_cases), 0)
        for case in holdout_cases.values():
            self.assertEqual(case.split, "holdout")

    def test_train_and_holdout_partition_the_full_set(self):
        train_cases = case_loader.load_cases("train")
        holdout_cases = case_loader.load_cases("holdout")
        all_cases = case_loader.load_all_cases()
        self.assertEqual(set(train_cases) | set(holdout_cases), set(all_cases))
        self.assertTrue(set(train_cases).isdisjoint(set(holdout_cases)))

    def test_invalid_split_raises_value_error(self):
        with self.assertRaises(ValueError):
            case_loader.load_cases("not-a-real-split")


class TestLoadAllCasesMalformedInput(unittest.TestCase):
    def test_missing_cases_dir_raises_case_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CaseLoadError):
                case_loader.load_all_cases(eval_set_dir=Path(tmp))

    def test_malformed_json_file_raises_case_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = Path(tmp)
            cases_dir = eval_dir / "cases"
            cases_dir.mkdir()
            (cases_dir / "T-BAD.json").write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(CaseLoadError):
                case_loader.load_all_cases(eval_set_dir=eval_dir)

    def test_manifest_missing_raises_case_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CaseLoadError):
                case_loader.load_manifest(eval_set_dir=Path(tmp))


class TestResolveDiff(unittest.TestCase):
    def test_returns_none_when_sha_is_null(self):
        case = Case.from_json_dict(
            {**SAMPLE_CASE_DICT, "review_material": {**SAMPLE_CASE_DICT["review_material"], "diff_ref": {"repo": "pantheon-config", "sha": None}}}
        )
        self.assertIsNone(case_loader.resolve_diff(case))

    def test_returns_none_when_repo_not_in_allowlist(self):
        case = Case.from_json_dict(
            {**SAMPLE_CASE_DICT, "review_material": {**SAMPLE_CASE_DICT["review_material"], "diff_ref": {"repo": "some-unlisted-repo", "sha": "deadbeef"}}}
        )
        self.assertIsNone(case_loader.resolve_diff(case))

    def test_returns_none_when_allowlisted_checkout_missing(self):
        case = Case.from_json_dict(
            {**SAMPLE_CASE_DICT, "review_material": {**SAMPLE_CASE_DICT["review_material"], "diff_ref": {"repo": "pantheon-config", "sha": "deadbeef"}}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            allowlist = {"pantheon-config": Path(tmp) / "does-not-exist"}
            self.assertIsNone(case_loader.resolve_diff(case, repo_allowlist=allowlist))

    def test_returns_none_when_sha_unresolvable_in_a_real_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_repo_with_commit(repo)
            case = Case.from_json_dict(
                {**SAMPLE_CASE_DICT, "review_material": {**SAMPLE_CASE_DICT["review_material"], "diff_ref": {"repo": "pantheon-config", "sha": "0000000dead"}}}
            )
            allowlist = {"pantheon-config": repo}
            self.assertIsNone(case_loader.resolve_diff(case, repo_allowlist=allowlist))

    def test_resolves_real_sha_in_a_real_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            sha = _make_repo_with_commit(repo)
            case = Case.from_json_dict(
                {**SAMPLE_CASE_DICT, "review_material": {**SAMPLE_CASE_DICT["review_material"], "diff_ref": {"repo": "pantheon-config", "sha": sha}}}
            )
            allowlist = {"pantheon-config": repo}
            diff_text = case_loader.resolve_diff(case, repo_allowlist=allowlist)
            self.assertIsNotNone(diff_text)
            self.assertIn("fixture commit", diff_text)
            self.assertIn("file.txt", diff_text)

    def test_none_sentinel_resolves_to_self_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            sha = _make_repo_with_commit(repo)
            case = Case.from_json_dict(
                {**SAMPLE_CASE_DICT, "review_material": {**SAMPLE_CASE_DICT["review_material"], "diff_ref": {"repo": "pantheon-config", "sha": sha}}}
            )
            allowlist = {"pantheon-config": None}
            diff_text = case_loader.resolve_diff(case, repo_allowlist=allowlist, self_repo_root=repo)
            self.assertIsNotNone(diff_text)
            self.assertIn("fixture commit", diff_text)


if __name__ == "__main__":
    unittest.main()
