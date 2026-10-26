from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from tools import quant_h2_sampler as h2


def outcome(call_id: str, *, agent: str = "mason", call: str | None = None, days_old: str = "2026-09-01T00:00:00Z"):
    return {
        "schema": "pantheon.quant.call_outcome/v1",
        "run_id": "run",
        "generated_at": "2026-10-09T00:00:00Z",
        "call_id": call_id,
        "call_source_type": "ticket",
        "call_source_ref": "tickets/done/T-1000.md",
        "call_ref": "T-1000",
        "ticket_ids": ["T-1000"],
        "via_ids": ["P1", "D6"],
        "via_classes": {"principle": 1, "directive": 1},
        "agent": agent,
        "call": call or "approve the rollout via P1 and D6 in ORG-102 using abcdef123456 at tools/example.py",
        "source_title": "Rollout review gate with service impact",
        "outcome": "held",
        "outcome_window_days": 14,
        "call_occurred_at": days_old,
        "eligible_for_h1": True,
        "evidence": [],
    }


def write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class H2SamplerTests(unittest.TestCase):
    class FakeRenderer:
        def __init__(self, outputs: list[dict] | None = None):
            self.outputs = list(outputs or [])
            self.calls = []

        def render(self, *, row, source, attempt, prior_reject_reasons):
            self.calls.append(
                {
                    "call_id": row["call_id"],
                    "source_excerpt": source["source_excerpt"],
                    "attempt": attempt,
                    "prior_reject_reasons": list(prior_reject_reasons),
                }
            )
            if self.outputs:
                return self.outputs.pop(0)
            return {
                "situation": f"Situation for {row['call_id']} is understandable.",
                "decision": f"Decision for {row['call_id']} is stated neutrally.",
                "renderer_model": "fake-renderer",
                "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                "renderer_prompt_sha256": "fake-sha",
                "renderer_command": "fake",
            }

    def test_blind_scrub_removes_guidance_hashes_prs_and_paths(self):
        scrubbed = h2.scrub_text("ship PR #823 and pantheon-config#824 from /home/mason/pantheon-config/tools/x.py")
        self.assertFalse(h2.PR_RE.search(scrubbed), scrubbed)
        self.assertFalse(h2.PATH_RE.search(scrubbed), scrubbed)
        combined = h2.scrub_text(
            "ship the rollout because via P1 D6 ORG-102 ADR-9 abcdef123456 after review completed"
        )
        self.assertLessEqual(len(combined), 200)
        self.assertFalse(h2.GUIDANCE_RE.search(combined), combined)
        self.assertFalse(h2.HEX_RE.search(combined), combined)
        self.assertFalse(h2.PR_RE.search(combined), combined)
        self.assertFalse(h2.TICKET_RE.search(combined), combined)
        self.assertFalse(h2.PATH_RE.search(combined), combined)

    def test_sample_is_stratified_deterministic_and_never_resamples(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcomes = root / "call_outcomes.jsonl"
            rows = [
                outcome("a", agent="mason", call="approve the small rollout after review"),
                outcome("b", agent="mason", call="pause the noisy rollout before deployment"),
                outcome("c", agent="recovery", call="restart the service after the gate"),
                outcome("d", agent="orchestrator", call="send the review to the operator after checks"),
            ]
            write_jsonl(outcomes, rows)
            args = argparse.Namespace(
                out=root,
                outcomes=outcomes,
                week_start="2026-10-05",
                seed="seed-1",
                sample_id="sample-1",
                limit=3,
                days=60,
                renderer_timeout=120,
                renderer_max_attempts=3,
            )
            run = h2.sample(args, renderer=self.FakeRenderer())
            samples = h2.load_jsonl(root / "call_h2_samples.jsonl")
            self.assertEqual(run["records"]["call_h2_samples"], 3)
            self.assertEqual(len({row["agent"] for row in samples}), 3)
            self.assertTrue(all(row["source_excerpt"] for row in samples))
            self.assertEqual({row["renderer_model"] for row in samples}, {"fake-renderer"})
            self.assertTrue((root / "h2" / "outbox" / "sample-1.md").exists())

            args.sample_id = "sample-2"
            args.seed = "seed-2"
            second = h2.sample(args, renderer=self.FakeRenderer())
            self.assertEqual(second["records"]["call_h2_samples"], 1)
            all_samples = h2.load_jsonl(root / "call_h2_samples.jsonl")
            self.assertEqual(len({row["call_id"] for row in all_samples}), 4)

    def test_ugly_source_stays_eligible_and_is_audited(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcomes = root / "call_outcomes.jsonl"
            row = outcome(
                "ugly",
                call="ship PR #823 via P1 from /tmp/noisy/path.py despite unreadable source prose",
            )
            write_jsonl(outcomes, [row])
            args = argparse.Namespace(
                out=root,
                outcomes=outcomes,
                week_start="2026-10-05",
                seed="seed-ugly",
                sample_id="sample-ugly",
                limit=1,
                days=60,
                renderer_timeout=120,
                renderer_max_attempts=3,
            )
            run = h2.sample(args, renderer=self.FakeRenderer())
            self.assertEqual(run["eligible"], 1)
            self.assertEqual(run["skipped"], {})
            sample = h2.load_jsonl(root / "call_h2_samples.jsonl")[0]
            self.assertIn("PR #823", sample["source_excerpt"])
            self.assertNotIn("PR #823", sample["blind_call"])

    def test_no_source_text_is_the_hard_text_exclusion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcomes = root / "call_outcomes.jsonl"
            row = outcome("blank", call="")
            row["call"] = ""
            row["source_title"] = ""
            row["serves"] = ""
            row["rationale"] = ""
            write_jsonl(outcomes, [row])
            args = argparse.Namespace(
                out=root,
                outcomes=outcomes,
                week_start="2026-10-05",
                seed="seed-blank",
                sample_id="sample-blank",
                limit=1,
                days=60,
                renderer_timeout=120,
                renderer_max_attempts=3,
            )
            run = h2.sample(args, renderer=self.FakeRenderer())
            self.assertEqual(run["eligible"], 0)
            self.assertEqual(run["skipped"], {"no_source_text": 1})

    def test_source_excerpt_has_ledger_context(self):
        source = h2.source_excerpt(
            {
                "call_id": "ledger-storage",
                "call_source_type": "ledger",
                "call_source_ref": "2026-07-18T20:33:29Z-ho-t1764-8770-orphan-killed-remediated",
                "call_ref": "msg_88",
                "ticket_ids": [],
                "agent": "human:operator",
                "call": "decide_ledger_storage",
                "outcome": "held",
                "call_occurred_at": "2026-07-18T20:33:29Z",
            }
        )
        self.assertIsNotNone(source)
        self.assertEqual(source["source_excerpt_kind"], "ledger")
        self.assertRegex(source["source_excerpt"].lower(), r"leak|service|broad bind")

    def test_postcheck_retries_leaks_then_accepts_clean_paraphrase(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcomes = root / "call_outcomes.jsonl"
            write_jsonl(outcomes, [outcome("retry", call="approve the noisy rollout after review")])
            fake = self.FakeRenderer(
                [
                    {
                        "situation": "The source mentions T-1234 and /055 in a way that leaks.",
                        "decision": "The decision correctly shipped PR #823.",
                        "renderer_model": "fake-renderer",
                        "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                        "renderer_prompt_sha256": "fake-sha",
                        "renderer_command": "fake",
                    },
                    {
                        "situation": "A rollout had enough review evidence to proceed.",
                        "decision": "The call was to ship the rollout.",
                        "renderer_model": "fake-renderer",
                        "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                        "renderer_prompt_sha256": "fake-sha",
                        "renderer_command": "fake",
                    },
                ]
            )
            args = argparse.Namespace(
                out=root,
                outcomes=outcomes,
                week_start="2026-10-05",
                seed="seed-retry",
                sample_id="sample-retry",
                limit=1,
                days=60,
                renderer_timeout=120,
                renderer_max_attempts=3,
            )
            run = h2.sample(args, renderer=fake)
            sample = h2.load_jsonl(root / "call_h2_samples.jsonl")[0]
            self.assertEqual(run["records"]["call_h2_samples"], 1)
            self.assertEqual(sample["renderer_attempts"], 2)
            self.assertEqual(fake.calls[1]["prior_reject_reasons"], [
                "leak:slash_number",
                "leak:source_ref",
                "neutrality:judgment_word",
                "quality:vague_pronoun",
            ])

    def test_postcheck_rejects_ask_shaped_calls(self):
        self.assertIn(
            "quality:ask_shaped_call",
            h2.paraphrase_reject_reasons(
                "A rollout had enough review evidence to proceed.",
                "Decide whether to approve the rollout.",
            ),
        )
        self.assertIn(
            "quality:ask_shaped_call",
            h2.paraphrase_reject_reasons(
                "A rollout had enough review evidence to proceed.",
                "The call was to approve the rollout?",
            ),
        )

    def test_postcheck_retries_ask_shaped_call_then_accepts_made_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcomes = root / "call_outcomes.jsonl"
            write_jsonl(outcomes, [outcome("ask", call="approve the noisy rollout after review")])
            fake = self.FakeRenderer(
                [
                    {
                        "situation": "A rollout had enough review evidence to proceed.",
                        "decision": "Decide whether to approve the rollout.",
                        "renderer_model": "fake-renderer",
                        "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                        "renderer_prompt_sha256": "fake-sha",
                        "renderer_command": "fake",
                    },
                    {
                        "situation": "A rollout had enough review evidence to proceed.",
                        "decision": "The call was to approve the rollout.",
                        "renderer_model": "fake-renderer",
                        "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                        "renderer_prompt_sha256": "fake-sha",
                        "renderer_command": "fake",
                    },
                ]
            )
            args = argparse.Namespace(
                out=root,
                outcomes=outcomes,
                week_start="2026-10-05",
                seed="seed-ask",
                sample_id="sample-ask",
                limit=1,
                days=60,
                renderer_timeout=120,
                renderer_max_attempts=3,
            )
            run = h2.sample(args, renderer=fake)
            sample = h2.load_jsonl(root / "call_h2_samples.jsonl")[0]
            self.assertEqual(run["records"]["call_h2_samples"], 1)
            self.assertEqual(sample["renderer_attempts"], 2)
            self.assertEqual(fake.calls[1]["prior_reject_reasons"], ["quality:ask_shaped_call"])
            self.assertEqual(sample["blind_call"], "The call was to approve the rollout.")

    def test_postcheck_failure_skips_and_replaces_from_candidate_stream(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcomes = root / "call_outcomes.jsonl"
            write_jsonl(
                outcomes,
                [
                    outcome("bad", call="approve the noisy rollout after review"),
                    outcome("good", call="pause the risky rollout after review"),
                ],
            )
            fake = self.FakeRenderer(
                [
                    {
                        "situation": "T-1234 leaks from the renderer output.",
                        "decision": "The decision correctly shipped PR #823.",
                        "renderer_model": "fake-renderer",
                        "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                        "renderer_prompt_sha256": "fake-sha",
                        "renderer_command": "fake",
                    },
                    {
                        "situation": "A rollout had unresolved risk after review.",
                        "decision": "The call was to pause the rollout.",
                        "renderer_model": "fake-renderer",
                        "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
                        "renderer_prompt_sha256": "fake-sha",
                        "renderer_command": "fake",
                    },
                ]
            )
            args = argparse.Namespace(
                out=root,
                outcomes=outcomes,
                week_start="2026-10-05",
                seed="seed-replace",
                sample_id="sample-replace",
                limit=1,
                days=60,
                renderer_timeout=120,
                renderer_max_attempts=1,
            )
            run = h2.sample(args, renderer=fake)
            samples = h2.load_jsonl(root / "call_h2_samples.jsonl")
            self.assertEqual(len(samples), 1)
            self.assertEqual(run["skipped"]["render_failed_postcheck"], 1)
            self.assertEqual(samples[0]["blind_call"], "The call was to pause the rollout.")

    def test_reply_parser_accepts_compact_all_and_numbered_forms(self):
        compact = h2.parse_reply("Y n y", 4)
        self.assertEqual(compact["parse_status"], "partial")
        self.assertEqual([a["answer"] for a in compact["answers"]], ["agree", "disagree", "agree"])

        all_yes = h2.parse_reply("all y", 3)
        self.assertEqual(all_yes["parse_status"], "parsed")
        self.assertEqual([a["answer"] for a in all_yes["answers"]], ["agree", "agree", "agree"])

        numbered = h2.parse_reply("1y 2n 3Y", 3)
        self.assertEqual(numbered["parse_status"], "parsed")
        self.assertEqual([a["answer"] for a in numbered["answers"]], ["agree", "disagree", "agree"])

        ambiguous = h2.parse_reply("1 yes 2 no", 3)
        self.assertEqual(ambiguous["parse_status"], "unparsed")
        self.assertEqual(ambiguous["answers"], [])

        duplicate = h2.parse_reply("1y 1n 2y", 2)
        self.assertEqual(duplicate["parse_status"], "unparsed")
        self.assertEqual(duplicate["answers"], [])

    def test_recompute_weekly_flags_four_consecutive_low_weeks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            samples = []
            replies = []
            for idx, week in enumerate(["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"], start=1):
                sample_id = f"s{idx}"
                samples.append(
                    {
                        "schema": "pantheon.quant.call_h2_sample/v1",
                        "run_id": "r",
                        "generated_at": "2026-10-09T00:00:00Z",
                        "sample_id": sample_id,
                        "week_start": week,
                        "seed": "seed",
                        "item_no": 1,
                        "call_id": f"c{idx}",
                        "agent": "mason",
                        "display_ref": "T-1000",
                        "outcome": "held",
                        "blind_situation": "During ticket or PR work, mason made this call.",
                        "blind_call": "ship the small fix",
                    }
                )
                replies.append(
                    {
                        "schema": "pantheon.quant.call_h2_reply/v1",
                        "run_id": "reply",
                        "generated_at": "2026-10-09T00:00:00Z",
                        "sample_id": sample_id,
                        "reply_received_at": "2026-10-09T00:00:00Z",
                        "reply_file": None,
                        "raw_reply": "n",
                        "answers": [{"item_no": 1, "answer": "disagree"}],
                        "answered_count": 1,
                        "invalid_chars_count": 0,
                        "surplus_count": 0,
                        "parse_status": "parsed",
                    }
                )
            h2.append_jsonl(root / "call_h2_samples.jsonl", samples)
            h2.append_jsonl(root / "call_h2_replies.jsonl", replies)
            weekly = h2.recompute_weekly(root, "run", "2026-10-09T00:00:00Z")
            self.assertEqual(weekly[-1]["consecutive_low_weeks"], 4)
            self.assertTrue(weekly[-1]["falsifier_triggered"])
            self.assertEqual(weekly[-1]["verdict"], "fail")

    def test_recompute_weekly_uses_rolling_bound_for_consecutive_lows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            samples = []
            replies = []
            for idx, week in enumerate(["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"], start=1):
                sample_id = f"s{idx}"
                for item_no in range(1, 11):
                    samples.append(
                        {
                            "schema": "pantheon.quant.call_h2_sample/v1",
                            "run_id": "r",
                            "generated_at": "2026-10-09T00:00:00Z",
                            "sample_id": sample_id,
                            "week_start": week,
                            "seed": "seed",
                            "item_no": item_no,
                            "call_id": f"c{idx}-{item_no}",
                            "agent": "mason",
                            "display_ref": None,
                            "outcome": "held",
                            "blind_situation": "During ticket or PR work, mason made this call.",
                            "blind_call": "ship the small fix",
                        }
                    )
                replies.append(
                    {
                        "schema": "pantheon.quant.call_h2_reply/v1",
                        "run_id": "reply",
                        "generated_at": "2026-10-09T00:00:00Z",
                        "sample_id": sample_id,
                        "reply_received_at": "2026-10-09T00:00:00Z",
                        "reply_file": None,
                        "raw_reply": "yyyyyyyyyn",
                        "answers": [
                            {"item_no": item_no, "answer": "agree" if item_no < 10 else "disagree"}
                            for item_no in range(1, 11)
                        ],
                        "answered_count": 10,
                        "invalid_chars_count": 0,
                        "surplus_count": 0,
                        "parse_status": "parsed",
                    }
                )
            h2.append_jsonl(root / "call_h2_samples.jsonl", samples)
            h2.append_jsonl(root / "call_h2_replies.jsonl", replies)
            weekly = h2.recompute_weekly(root, "run", "2026-10-09T00:00:00Z")
            self.assertGreaterEqual(weekly[-1]["rolling_4w_wilson90"]["low"], 0.70)
            self.assertEqual(weekly[-1]["consecutive_low_weeks"], 0)
            self.assertFalse(weekly[-1]["falsifier_triggered"])
            self.assertEqual(weekly[-1]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
