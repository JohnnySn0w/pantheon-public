"""Tests for tools/decision_recall_poc.py — T-2816 decision-recall PoC.

The PoC asks one falsifiable question: does surfacing the RELEVANT past ADR
at point-of-need (without the agent knowing to look) clear the bar the Dei
recall-hook design (notes/dei-foundational-wip/recall-hook.md) set when it
DECLINED broad fuzzy recall as bloat-positive? These tests cover the
retrieval mechanics in isolation, on synthetic fixtures — the real 74-ADR
corpus + hand-labeled eval set is only exercised by the eval CLI (see
decision_recall_poc_RESULTS.md for those numbers, not asserted here).

Run:
    python3 -m pytest tools/test_decision_recall_poc.py -q
    python3 -m unittest tools.test_decision_recall_poc -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tools.decision_recall_poc as recall_poc


def _write_adr(d: Path, filename: str, org_id: str, title: str, date: str) -> None:
    (d / filename).write_text(
        f"---\naliases: [{org_id}]\ntitle: {title}\nstatus: Ratified {date}\n"
        f"date: {date}\nbuild_disposition: built\n---\n# {org_id} — {title}\n\n"
        f"## Context\n\nSome context prose about {title.lower()}.\n",
        encoding="utf-8",
    )


class TestTokenize(unittest.TestCase):
    def test_lowercase_and_split_on_punctuation(self) -> None:
        toks = recall_poc.tokenize("Editing `knowledge/watch/topics.md`!")
        self.assertIn("knowledge", toks)
        self.assertIn("watch", toks)
        self.assertIn("topics", toks)
        self.assertNotIn("Editing", toks)  # must be lowercased, not verbatim

    def test_org_id_kept_as_single_token(self) -> None:
        toks = recall_poc.tokenize("see ORG-017 for the knowledge tree decision")
        self.assertIn("org-017", toks)
        # must not also fragment into bare "org" + "017"
        self.assertNotIn("017", toks)

    def test_stopwords_removed(self) -> None:
        toks = recall_poc.tokenize("this is the plan for the migration")
        self.assertNotIn("this", toks)
        self.assertNotIn("is", toks)
        self.assertNotIn("the", toks)
        self.assertIn("plan", toks)
        self.assertIn("migration", toks)

    def test_empty_string_yields_no_tokens(self) -> None:
        self.assertEqual(recall_poc.tokenize(""), [])


class TestBM25(unittest.TestCase):
    def setUp(self) -> None:
        self.docs = {
            "A": recall_poc.tokenize("resolving a ticket cascades unblock dependants"),
            "B": recall_poc.tokenize("git remote manifest resolution self hosted"),
            "C": recall_poc.tokenize("secret credential rotation lifecycle host"),
        }
        self.bm25 = recall_poc.BM25(self.docs)

    def test_more_relevant_doc_scores_higher(self) -> None:
        scores = self.bm25.scores(recall_poc.tokenize("resolving a dependant ticket cascade"))
        self.assertGreater(scores["A"], scores["B"])
        self.assertGreater(scores["A"], scores["C"])

    def test_doc_with_no_overlap_scores_zero(self) -> None:
        scores = self.bm25.scores(recall_poc.tokenize("credential rotation secret"))
        self.assertEqual(scores["A"], 0.0)
        self.assertGreater(scores["C"], 0.0)

    def test_empty_corpus_all_zero(self) -> None:
        bm25 = recall_poc.BM25({})
        self.assertEqual(bm25.scores(["anything"]), {})

    def test_empty_query_all_zero(self) -> None:
        scores = self.bm25.scores([])
        self.assertTrue(all(s == 0.0 for s in scores.values()))


class TestLoadAdrs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parses_aliases_title_date(self) -> None:
        _write_adr(self.d, "ORG-099-example.md", "ORG-099", "Example decision", "2026-08-01")
        adrs = recall_poc.load_adrs(self.d)
        self.assertIn("ORG-099", adrs)
        self.assertEqual(adrs["ORG-099"]["title"], "Example decision")
        self.assertEqual(adrs["ORG-099"]["date"], "2026-08-01")

    def test_falls_back_to_filename_when_aliases_missing(self) -> None:
        (self.d / "ORG-098-no-aliases.md").write_text(
            "---\ntitle: No aliases here\ndate: 2026-08-02\n---\n# body\n", encoding="utf-8"
        )
        adrs = recall_poc.load_adrs(self.d)
        self.assertIn("ORG-098", adrs)
        self.assertEqual(adrs["ORG-098"]["title"], "No aliases here")

    def test_malformed_frontmatter_skipped_not_crashed(self) -> None:
        (self.d / "ORG-097-broken.md").write_text("no frontmatter at all, just text\n", encoding="utf-8")
        _write_adr(self.d, "ORG-096-fine.md", "ORG-096", "Fine one", "2026-08-03")
        adrs = recall_poc.load_adrs(self.d)
        self.assertNotIn("ORG-097", adrs)
        self.assertIn("ORG-096", adrs)

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(recall_poc.load_adrs(self.d / "nope"), {})


class TestLoadTriggers(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loads_valid_json(self) -> None:
        p = self.d / "triggers.json"
        p.write_text(json.dumps({"ORG-013": "resolving a ticket with dependants"}), encoding="utf-8")
        self.assertEqual(recall_poc.load_triggers(p), {"ORG-013": "resolving a ticket with dependants"})

    def test_missing_file_fails_open_empty(self) -> None:
        self.assertEqual(recall_poc.load_triggers(self.d / "absent.json"), {})

    def test_malformed_json_fails_open_empty(self) -> None:
        p = self.d / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(recall_poc.load_triggers(p), {})

    def test_non_string_values_dropped(self) -> None:
        p = self.d / "mixed.json"
        p.write_text(json.dumps({"ORG-001": "fine", "ORG-002": 42, "ORG-003": None}), encoding="utf-8")
        loaded = recall_poc.load_triggers(p)
        self.assertEqual(loaded, {"ORG-001": "fine"})

    def test_non_dict_json_fails_open_empty(self) -> None:
        p = self.d / "list.json"
        p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        self.assertEqual(recall_poc.load_triggers(p), {})


class TestRecencyWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.adrs = {
            "ORG-001": {"title": "old", "date": "2026-01-01"},
            "ORG-002": {"title": "mid", "date": "2026-03-01"},
            "ORG-003": {"title": "newer", "date": "2026-05-01"},
            "ORG-004": {"title": "newest", "date": "2026-06-01"},
            "ORG-005": {"title": "no-date", "date": ""},
        }

    def test_returns_last_k_by_date(self) -> None:
        window = recall_poc.recency_window(self.adrs, k=2)
        self.assertEqual(window, {"ORG-004", "ORG-003"})

    def test_missing_date_excluded(self) -> None:
        window = recall_poc.recency_window(self.adrs, k=10)
        self.assertNotIn("ORG-005", window)

    def test_k_larger_than_corpus_returns_all_dated(self) -> None:
        window = recall_poc.recency_window(self.adrs, k=100)
        self.assertEqual(window, {"ORG-001", "ORG-002", "ORG-003", "ORG-004"})

    def test_k_zero_returns_empty(self) -> None:
        self.assertEqual(recall_poc.recency_window(self.adrs, k=0), set())


class TestBuildDocuments(unittest.TestCase):
    def test_trigger_plus_title_used_when_triggered(self) -> None:
        adrs = {"ORG-001": {"title": "Blocked state retirement", "date": "2026-05-19"}}
        triggers = {"ORG-001": "marking a ticket blocked"}
        docs = recall_poc.build_documents(adrs, triggers, {"ORG-001"})
        joined = " ".join(docs["ORG-001"])
        self.assertIn("marking", joined)
        self.assertIn("blocked", joined)
        self.assertIn("retirement", joined)

    def test_title_only_fallback_when_no_trigger(self) -> None:
        adrs = {"ORG-002": {"title": "Manifest resolved git remotes", "date": "2026-08-10"}}
        docs = recall_poc.build_documents(adrs, {}, {"ORG-002"})
        joined = " ".join(docs["ORG-002"])
        self.assertIn("manifest", joined)
        self.assertIn("remotes", joined)

    def test_pool_ids_absent_from_adrs_are_skipped(self) -> None:
        docs = recall_poc.build_documents({}, {"ORG-999": "ghost"}, {"ORG-999"})
        self.assertEqual(docs, {})


class TestRetrieve(unittest.TestCase):
    def setUp(self) -> None:
        self.adrs = {
            "ORG-013": {"title": "Relational triggers on ticket resolve", "date": "2026-05-14"},
            "ORG-046": {"title": "Manifest resolved git remotes", "date": "2026-08-10"},
            "ORG-999": {"title": "Unrelated topic about lunch scheduling", "date": "2026-01-01"},
        }
        self.triggers = {
            "ORG-013": "resolving a ticket that other tickets depend on",
        }

    def test_candidate_pool_is_sidecar_union_recency(self) -> None:
        # ORG-046 has no trigger entry but IS the most-recent ADR (k=1 recency
        # window) -> still retrievable via title-only text.
        results = recall_poc.retrieve(
            "hardcoding a git remote instead of resolving it via the manifest",
            self.adrs, self.triggers, k=2, recency_k=1,
        )
        self.assertIn("ORG-046", [r[0] for r in results])

    def test_outside_pool_never_retrieved(self) -> None:
        # ORG-999 is neither in the sidecar nor in a k=1 recency window.
        results = recall_poc.retrieve(
            "unrelated topic about lunch scheduling", self.adrs, self.triggers,
            k=2, recency_k=1,
        )
        self.assertNotIn("ORG-999", [r[0] for r in results])

    def test_precision_cap_respected(self) -> None:
        adrs = {f"ORG-{i:03d}": {"title": "ticket depend cascade resolve", "date": "2026-01-01"}
                for i in range(5)}
        triggers = {k: "resolving a ticket that other tickets depend on" for k in adrs}
        results = recall_poc.retrieve("resolving a ticket that other tickets depend on",
                                       adrs, triggers, k=2)
        self.assertLessEqual(len(results), 2)

    def test_score_gate_excludes_zero_overlap(self) -> None:
        # NB: avoid "unrelated" — it's a real token in ORG-999's fixture title
        # (`test_candidate_pool_is_sidecar_union_recency`'s decoy doc) and
        # would create accidental overlap, defeating the point of this test.
        results = recall_poc.retrieve("completely irrelevant query about weather forecasts",
                                       self.adrs, self.triggers, k=5, recency_k=5)
        self.assertEqual(results, [])

    def test_ranking_order_descending(self) -> None:
        results = recall_poc.retrieve("resolving a ticket that other tickets depend on cascade",
                                       self.adrs, self.triggers, k=5, recency_k=5)
        scores = [s for _, s in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_pool_returns_empty(self) -> None:
        self.assertEqual(recall_poc.retrieve("anything", {}, {}, k=2, recency_k=5), [])

    def test_min_score_raises_the_gate(self) -> None:
        # A weak single-token overlap clears score>0 but not a raised min_score.
        weak = recall_poc.retrieve("ticket", self.adrs, self.triggers, k=5, recency_k=5)
        self.assertTrue(weak)  # sanity: the default (loosest) gate does surface it
        raised = recall_poc.retrieve("ticket", self.adrs, self.triggers,
                                      k=5, recency_k=5, min_score=100.0)
        self.assertEqual(raised, [])


class TestEvaluate(unittest.TestCase):
    def setUp(self) -> None:
        self.adrs = {
            "ORG-013": {"title": "Relational triggers on ticket resolve", "date": "2026-05-14"},
            "ORG-051": {"title": "Secret and authentication lifecycle", "date": "2026-07-30"},
        }
        self.triggers = {
            "ORG-013": "resolving a ticket that other tickets depend on",
            "ORG-051": "storing a password or api key as a home directory pass file",
        }

    def test_known_hit_miss_and_false_trigger(self) -> None:
        scenarios = [
            # positive, should hit
            {"scenario": "resolving a ticket that other tickets depend on", "expected": ["ORG-013"]},
            # positive, should miss (no lexical overlap at all with either doc)
            {"scenario": "completely unrelated weather forecast discussion", "expected": ["ORG-051"]},
            # negative, should stay silent
            {"scenario": "checking free disk space on the vps", "expected": []},
            # negative but WILL false-trigger (shares vocab with ORG-051's doc text)
            {"scenario": "storing a password api key home directory pass file", "expected": []},
        ]
        result = recall_poc.evaluate(scenarios, self.adrs, self.triggers, k=2)
        self.assertEqual(result["n_positive"], 2)
        self.assertEqual(result["n_negative"], 2)
        self.assertAlmostEqual(result["hit_rate_at_k"], 0.5)  # 1 of 2 positives hit
        self.assertAlmostEqual(result["false_trigger_rate"], 0.5)  # 1 of 2 negatives fired

    def test_min_score_threads_through_to_retrieval(self) -> None:
        scenarios = [{"scenario": "resolving a ticket that other tickets depend on", "expected": ["ORG-013"]}]
        loose = recall_poc.evaluate(scenarios, self.adrs, self.triggers, k=2, min_score=0.0)
        strict = recall_poc.evaluate(scenarios, self.adrs, self.triggers, k=2, min_score=100.0)
        self.assertEqual(loose["hit_rate_at_k"], 1.0)
        self.assertEqual(strict["hit_rate_at_k"], 0.0)  # n_pos=1 still counted, 0 hits
        self.assertEqual(strict["details"][0]["got"], [])

    def test_no_scenarios_yields_none_metrics(self) -> None:
        result = recall_poc.evaluate([], self.adrs, self.triggers, k=2)
        self.assertIsNone(result["hit_rate_at_k"])
        self.assertIsNone(result["false_trigger_rate"])
        self.assertIsNone(result["precision_at_k"])


class TestLoadEvalSet(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_wrapped_object_form(self) -> None:
        p = self.d / "eval.json"
        p.write_text(json.dumps({"_note": "authored before tuning", "scenarios": [
            {"scenario": "x", "expected": []}
        ]}), encoding="utf-8")
        loaded = recall_poc.load_eval_set(p)
        self.assertEqual(loaded, [{"scenario": "x", "expected": []}])

    def test_bare_list_form(self) -> None:
        p = self.d / "eval2.json"
        p.write_text(json.dumps([{"scenario": "y", "expected": []}]), encoding="utf-8")
        self.assertEqual(recall_poc.load_eval_set(p), [{"scenario": "y", "expected": []}])


class TestCLI(unittest.TestCase):
    """Drive main() as a subprocess — the actual argv/stdout contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.adr_dir = self.d / "adr"
        self.adr_dir.mkdir()
        _write_adr(self.adr_dir, "ORG-013-relational.md", "ORG-013",
                   "Relational triggers on ticket resolve", "2026-05-14")
        self.triggers_path = self.d / "triggers.json"
        self.triggers_path.write_text(
            json.dumps({"ORG-013": "resolving a ticket that other tickets depend on"}),
            encoding="utf-8",
        )
        self.eval_path = self.d / "eval.json"
        self.eval_path.write_text(json.dumps({"_note": "n/a", "scenarios": [
            {"scenario": "resolving a ticket that other tickets depend on", "expected": ["ORG-013"]},
            {"scenario": "checking free disk space", "expected": []},
        ]}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tools.decision_recall_poc",
             "--adr-dir", str(self.adr_dir),
             "--triggers", str(self.triggers_path),
             "--eval-set", str(self.eval_path),
             *extra_args],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=60,
        )

    def test_eval_mode_prints_metrics(self) -> None:
        p = self._run()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("hit_rate@2", p.stdout)
        self.assertIn("false_trigger_rate", p.stdout)

    def test_query_mode_prints_matches(self) -> None:
        p = self._run("--query", "resolving a ticket that other tickets depend on")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ORG-013", p.stdout)


if __name__ == "__main__":
    unittest.main()
