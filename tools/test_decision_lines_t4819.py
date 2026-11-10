"""T-4819: ORG-102 decision-line grammar widening (via qualifiers, rulings, multi-ref, marker).

CORPUS holds exact lines copied from tickets T-2917, T-2955, T-5740, T-5041, T-6240 and T-5140
as (line_no_in_ticket, text). Tests never read live ticket files.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools import decision_lines as dl
from tools import quant_calls_collect as qc

CORPUS = {'T-2917': [(37,
             'DECISION: approve the 5a-5d plan with rulings i1-i5 | serves: a per-seat executor '
             'that reaches the user manager without the private control socket | via: P1, P4, '
             'ORG-052 §2-§8, E19/E29/E39 | ref: T-2917'),
            (45,
             'DECISION: approve the 5b plan with B1-B12 | serves: target-uid executor + Verify end '
             'to end within §8 | via: P1, P4, ORG-052 §3/§5/§6/§8, E19, E30, T-4861 | ref: T-2917'),
            (50,
             'DECISION: send back for M1-M4 + S1-S5 | serves: complete receipts and B11 '
             'never-re-dispatch | via: P1, P4, ORG-052 §5/§6, E29 | ref: T-2917'),
            (53,
             'DECISION: send back for R1/R2 with the scope held tight for the last round | serves: '
             'complete receipts for every committed host write | via: ORG-052 §6, B11 | ref: '
             'T-2917'),
            (62,
             'DECISION: approve the 5c plan with D1-D10 | serves: E39: the executor reaches the '
             'user manager without the private systemctl path | via: P1, P4, ORG-052 §8 / '
             'pre-mortem / E30 / E39, T-4861 | ref: T-2917'),
            (93,
             'DECISION: approve the 5d plan with D11-D24 as recommended; bubble the ORG-068 roster '
             'addition | serves: a real persona install plan runs end to end through the broker '
             'with rollback-or-loud-refusal | via: P1, P4, ORG-052 §5/§6/§8, E26/E29/E30, '
             'T-2917:D5/D6 | ref: T-2917')],
 'T-2955': [(56,
             'DECISION: approve coverage.py 7.17.0 only as a hash-locked isolated adapter-venv '
             'dependency and file T-5971 | serves: implement truthful pantheon-config reach '
             'measurement without ambient runtime or suite contamination | via: P1 P3 P4 P5 D3 D4 '
             'D5 D7 D10 D11 ORG-083-sec-3 ORG-083-sec-10 | ref: T-2955,T-5825-B3,T-5971'),
            (72,
             '- 2026-11-03T23:51:44Z [codex] note @f04b2b1d16; STRUCTURED DECISION CORRECTION for '
             'the immediately prior ready-surface reconciliation; no scope, routing, or state '
             'change. DECISION: preserve T-2955 as the suppressed carrier and advance only the '
             'already-assigned T-6020 to T-6089 child chain | serves: keep the reviewed ORG-083 '
             'staged evidence graph and role ownership intact | via: T-2955 T-6020 P1 P3 P4 D6 D9 '
             'D10 | ref: T-6089')],
 'T-5041': [(66,
             '- 2026-10-15T17:18:02Z [archivist] note @e7d91c1bb; DECISION: keep the two-pass OCR '
             'design (olmOCR transcribes, Qwen2.5-VL classifies/describes/verifies) | serves: '
             'complete, accurate SuperNote transcription without degrading classification | via: '
             'P3, D11, operator:tg:794, operator:tg:798 | ref: T-5041 replacement test 2026-10-15')],
 'T-5140': [(55,
             '- 2026-10-23T04:29:55Z [suborch2] note @76a8a01ac; DECISION: take over mason r4 '
             '(origin/agent/T-5140 @812e4a5d3, 4 commits, never APPROVEd) by first probing which '
             'gaps current main 70d8735a1 still has, then rebasing r4 onto main (resolving the '
             'overlap with T-5360/T-5363/T-5379 in worker_guard_impl), corpus re-diff vs main, '
             'fresh capped review to the HO bar (a: split push word in shell-read body/code string '
             'denied where bash forms push; b: D->A 0; c: suites green; d: pathological nesting '
             'fails closed fast) | serves: worker-guard push containment | via: ORG-105, HO bar '
             '2026-10-22, P1 | ref: T-5140')],
 'T-5740': [(64,
             '- 2026-10-29T18:48:16Z [claude-fallback] note @46187494ba; Fresh evidence-chain '
             'independent APPROVE on unchanged 42351559680be2b376819cc5c792b58c4164f86b. Reviewer '
             'independently enumerated all unit/same-process/real-subprocess gates and audited '
             'M1-M7 causality; full harness 311 OK skip1, focused 157 OK, supervisor 44 OK, route '
             'OFF, clean SHA, no survivors. Approved branch pushed as '
             'origin/agent/T-5740-tui-process-group. This seat lacks gh authentication, so '
             'PR/merge is handed to Orchestrator; no live deployment/retry. DECISION: approve and push '
             'the unchanged frozen candidate after fresh verifier+reviewer certification | serves: '
             'close the ownership correction while preserving its audited bytes | via: T-5740 P1 '
             'P2 P3 P4 P5 Amendment-G Amendment-H Amendment-J | ref: T-5740'),
            (69,
             '- 2026-10-29T19:04:01Z [claude] note @3528926262; DECISION: Treat the post-merge '
             'wrapper exit as a new startup failure class and require first-fault evidence before '
             'any code correction | serves: preserve accepted TUI ownership safety while '
             'diagnosing the real failure | via: T-5740 D1 D4 P3 P4 Amendment-G Amendment-H | ref: '
             'T-5740'),
            (71,
             'DECISION: Require a disposable Watchman-shaped cross-process RED/restored reproducer '
             'plus the named unit, same-process, negative, mutation, preserved-regression, and '
             'combined-tree gates | serves: prove the fix reaches the actual wrapper/controller '
             'readiness boundary rather than process construction | via: T-5740 Amendment-G '
             'Amendment-H Amendment-J | ref: T-5740')],
 'T-6240': [(42,
             '- 2026-11-06T08:10:18Z [codex] note @12f6a25d1c; DECISION: APPROVE exact '
             '5daabb4a084b0617d3ef067b3a46218e6e28b298 for PR #1704 integration | serves: '
             'goal-neutral maintenance by making coordinated reboot preflight structurally fail '
             'closed for every current seat | via: P1 P4 D7 | ref: T-6240,pantheon-config#1704.')]}


def _only(line):
    scan = dl.scan_decisions(line)
    return scan


def _line(key, n):
    for num, text in CORPUS[key]:
        if num == n:
            return text
    raise KeyError((key, n))


class CorpusRegressionTests(unittest.TestCase):
    """Exact lines copied from tickets; never read live ticket files at test time."""

    def assert_valid(self, text, n_expected=1):
        scan = dl.scan_decisions(text)
        self.assertEqual(scan.problems, ())
        self.assertEqual(len(scan.decisions), n_expected)
        return scan

    def test_t5740_amendment_qualifier_lines_parse(self):
        for n in (64, 69, 71):
            scan = self.assert_valid(_line("T-5740", n))
            self.assertIn("ticket", scan.decisions[0].via_classes)
            self.assertNotIn("ruling", scan.decisions[0].via_classes)
        got = self.assert_valid(_line("T-5740", 71)).decisions[0]
        self.assertEqual(got.via_ids, ("T-5740",))
        self.assertEqual(
            got.via_qualifiers,
            (("T-5740", "Amendment-G"), ("T-5740", "Amendment-H"), ("T-5740", "Amendment-J")),
        )

    def test_t5041_ruling_via_and_trailing_ref_note_parse(self):
        got = self.assert_valid(_line("T-5041", 66)).decisions[0]
        self.assertEqual(got.via_ids, ("P3", "D11", "operator:tg:794", "operator:tg:798"))
        self.assertEqual(got.via_classes, {"principle": 1, "directive": 1, "ruling": 2})
        self.assertEqual(got.refs, ("T-5041",))
        self.assertEqual(got.ref_note, "replacement test 2026-10-15")
        self.assertFalse(got.principle_only)

    def test_t6240_multi_ref_with_trailing_period_parses(self):
        got = self.assert_valid(_line("T-6240", 42)).decisions[0]
        self.assertEqual(got.refs, ("T-6240", "pantheon-config#1704"))
        self.assertEqual(got.ref, "T-6240,pantheon-config#1704")

    def test_t2955_inline_structured_decision_after_prose_parses(self):
        got = self.assert_valid(_line("T-2955", 72)).decisions[0]
        self.assertEqual(got.refs, ("T-6089",))
        self.assertIn("T-2955", got.via_ids)

    def test_t2955_section_suffix_via_fixed_but_ref_stays_malformed(self):
        scan = dl.scan_decisions(_line("T-2955", 56))
        self.assertEqual(len(scan.problems), 1)
        self.assertEqual(scan.problems[0].message, "ref must be T-NNNN, repo#PR or commit sha (comma-separated)")

    def test_t2917_partially_fixed_by_design(self):
        # Section qualifiers now parse; E/B/local tokens stay malformed (unratified).
        for n in (37, 45, 50, 53, 62, 93):
            scan = dl.scan_decisions(_line("T-2917", n))
            self.assertEqual(scan.decisions, (), n)
            msg = scan.problems[0].message
            self.assertNotIn("§", msg, n)
            self.assertNotIn("'ORG-052", msg, n)
        self.assertEqual(dl.scan_decisions(_line("T-2917", 53)).problems[0].message, "invalid via id 'B11'")
        self.assertIn("'E19/E29/E39'", dl.scan_decisions(_line("T-2917", 37)).problems[0].message)
        self.assertIn("'T-2917:D5/D6'", dl.scan_decisions(_line("T-2917", 93)).problems[0].message)

    def test_t5140_bare_ho_ruling_stays_malformed(self):
        scan = dl.scan_decisions(_line("T-5140", 55))
        self.assertEqual(scan.decisions, ())
        self.assertIn("invalid via id 'HO'", scan.problems[0].message)


class ViaGrammarTests(unittest.TestCase):
    def via(self, raw):
        text = f"DECISION: c | serves: s | via: {raw} | ref: T-4780"
        scan = dl.scan_decisions(text)
        return scan

    def test_section_qualifier_forms_count_base(self):
        for raw in ("ORG-052§3", "ORG-052 §3/§5", "ORG-083-sec-3", "ORG-052 §2-§8", "ORG-052, §6"):
            scan = self.via(raw)
            self.assertEqual(scan.problems, (), raw)
            self.assertEqual(scan.decisions[0].via_ids[0], raw[:7], raw)
            self.assertEqual(scan.decisions[0].via_classes, {"org": 1})
            self.assertEqual(len(scan.decisions[0].via_qualifiers), 1)

    def test_qualifier_without_base_is_malformed(self):
        for raw in ("§3", "Amendment-G P1", "/"):
            self.assertNotEqual(self.via(raw).problems, (), raw)

    def test_amendment_attaches_to_preceding_base(self):
        got = self.via("P4 Amendment-G").decisions[0]
        self.assertEqual(got.via_ids, ("P4",))
        self.assertEqual(got.via_qualifiers, (("P4", "Amendment-G"),))

    def test_ruling_ids(self):
        got = self.via("operator:tg:12345, ho:mail:01794186657658141959.4330.000018.orchestrator").decisions[0]
        self.assertEqual(got.via_classes, {"ruling": 2})
        self.assertEqual(got.via_ids[1], "ho:mail:01794186657658141959.4330.000018.orchestrator")

    def test_bare_ruling_words_stay_malformed(self):
        for raw in ("HO ruling", "operator:tg:", "operator:tg:abc", "ho:mail:"):
            self.assertNotEqual(self.via(raw).problems, (), raw)

    def test_normalize_via_id_accepts_qualified_and_ruling(self):
        self.assertEqual(dl.normalize_via_id("ORG-052§3"), ("ORG-052", "org"))
        self.assertEqual(dl.normalize_via_id("operator:tg:794"), ("operator:tg:794", "ruling"))
        self.assertIsNone(dl.normalize_via_id("HO"))


class RefGrammarTests(unittest.TestCase):
    def ref(self, raw):
        return dl.scan_decisions(f"DECISION: c | serves: s | via: P1 | ref: {raw}")

    def test_multi_ref_forms(self):
        got = self.ref("T-2955, T-5825,repo#12,ABCDEF1234").decisions[0]
        self.assertEqual(got.refs, ("T-2955", "T-5825", "repo#12", "abcdef1234"))

    def test_bad_refs_stay_malformed(self):
        for raw in ("T-5825-B3", "ORG-083", "T-2955,ORG-083", "abcdef", "deadbeefz", "words only"):
            self.assertNotEqual(self.ref(raw).problems, (), raw)


class MarkerTests(unittest.TestCase):
    def test_headings_and_labels_are_markers(self):
        for text in ("## DECISIONS\nx", "DECISIONS:\nx", "**DECISIONS**\nx", "- DECISION: x", "DECISIONS (a): x"):
            self.assertTrue(dl.has_decision_marker(text), text)

    def test_bare_word_in_prose_is_not_marker(self):
        for text in (
            "the DECISIONS were made earlier",
            "see DECISIONS in the report",
            "Fixed. Prior DECISION recorded elsewhere.",
            "note @abc; DECISIONS (TUI): D1 route OFF",
        ):
            self.assertFalse(dl.has_decision_marker(text), text)
            scan = dl.scan_decisions(text)
            self.assertEqual(scan.problems, (), text)

    def test_semicolon_transition_log_marker_kept(self):
        self.assertTrue(dl.has_decision_marker("- 2026-10-01T00:00:00Z [m] note @a; DECISION: x"))

    def test_inline_unstructured_decision_is_ignored(self):
        scan = dl.scan_decisions("Done with evidence. DECISION: x without fields, see above.")
        self.assertEqual((scan.decisions, scan.problems, scan.marker_present), ((), (), False))

    def test_inline_structured_decision_parses(self):
        scan = dl.scan_decisions("Done with evidence. DECISION: x | serves: y | via: P1 | ref: T-4780")
        self.assertEqual(scan.problems, ())
        self.assertEqual(scan.decisions[0].call, "x")

    def test_two_inline_decisions_on_one_line(self):
        scan = dl.scan_decisions(
            "Ok. DECISION: a | serves: y | via: P1 | ref: T-4780. DECISION: b | serves: y | via: D1 | ref: T-4781"
        )
        self.assertEqual([d.call for d in scan.decisions], ["a", "b"])

    def test_line_start_malformed_still_warns(self):
        scan = dl.scan_decisions("DECISION: missing fields")
        self.assertIn("malformed", scan.problems[0].message)


class SchemaTests(unittest.TestCase):
    def test_ruling_class_in_schema_and_collector(self):
        schema = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "quant-delivery.schema.json").read_text())
        found = []

        def walk(node):
            if isinstance(node, dict):
                vc = node.get("via_classes")
                if isinstance(vc, dict) and "properties" in vc:
                    found.append(vc["properties"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(schema)
        self.assertTrue(found)
        self.assertIn("ruling", found[0])
        rows = qc.records_from_text(
            "DECISION: c | serves: s | via: P1, operator:tg:794 | ref: T-4780",
            run_id="r",
            generated_at="2026-11-09T00:00:00Z",
            source_type="ticket",
            source_ref="t",
        )
        self.assertEqual(rows[0]["via_classes"], {"principle": 1, "ruling": 1})
        self.assertEqual(qc.via_classes(rows[0]["via_ids"]), {"principle": 1, "ruling": 1})


if __name__ == "__main__":
    unittest.main()
