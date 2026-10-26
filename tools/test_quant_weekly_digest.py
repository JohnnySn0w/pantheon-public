from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import quant_h2_sampler as h2
from tools import quant_weekly_digest as digest


def write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def interval(mid: float) -> dict[str, float]:
    return {"low": max(0.0, mid - 0.05), "mid": mid, "high": min(1.0, mid + 0.05)}


def h1_row(group: str, calls: int, adverse: int = 0, verdict: str = "pass") -> dict:
    return {
        "schema": "pantheon.quant.call_h1/v1",
        "run_id": "h1",
        "generated_at": "2026-10-09T00:00:00Z",
        "group": group,
        "calls": calls,
        "held": calls - adverse,
        "reversed": adverse,
        "overridden": 0,
        "adverse": adverse,
        "adverse_rate_wilson90": interval(adverse / calls) if calls else None,
        "reversal_rate_wilson90": interval(adverse / calls) if calls else None,
        "override_rate_wilson90": interval(0) if calls else None,
        "verdict": verdict,
        "verdict_reasons": [],
    }


def outcome(call_id: str) -> dict:
    return {
        "schema": "pantheon.quant.call_outcome/v1",
        "run_id": "outcomes",
        "generated_at": "2026-10-09T00:00:00Z",
        "call_id": call_id,
        "call_source_type": "ticket",
        "call_source_ref": "tickets/done/T-1000.md",
        "call_ref": "T-1000",
        "ticket_ids": ["T-1000"],
        "via_ids": ["ORG-102"],
        "via_classes": {"org": 1},
        "agent": "mason",
        "call": "approve the rollout after review evidence was collected",
        "source_title": "Rollout review gate with service impact",
        "outcome": "held",
        "outcome_window_days": 14,
        "call_occurred_at": "2026-09-01T00:00:00Z",
        "eligible_for_h1": True,
        "evidence": [],
    }


def delivery_weekly() -> list[dict]:
    rows = []
    weeks = ["2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05"]
    for idx, week in enumerate(weeks):
        first_half = idx < 4
        rows.append(
            {
                "schema": "pantheon.quant.delivery_weekly/v1",
                "run_id": "delivery",
                "generated_at": "2026-10-09T00:00:00Z",
                "week_start": week,
                "repo": "ALL",
                "merged_prs": 4,
                "delivery_prs": 4,
                "excluded_prs": 0,
                "change_failures": 1 if first_half else 0,
                "failure_rate_wilson90": interval(0.25 if first_half else 0.05),
                "arc_count": 2,
                "operator_touch_ids": ["TG-1"] if first_half else [],
                "operator_touches": 2 if first_half else 1,
                "operator_touches_per_arc": 2.0 if first_half else 0.5,
                "quota_source_status": "insufficient-source",
            }
        )
    return rows


def delivery_arcs() -> list[dict]:
    rows = []
    for idx in range(5):
        rows.append(
            {
                "schema": "pantheon.quant.delivery_arc/v1",
                "run_id": "delivery",
                "generated_at": "2026-10-09T00:00:00Z",
                "repo": "pantheon-config",
                "arc_id": f"S-{idx}",
                "pr_count": 1,
                "merged_prs": 1,
                "change_failures": 1 if idx == 0 else 0,
                "change_failure_breakdown": {"revert_prs": 1 if idx == 0 else 0, "hotfix_prs": 0, "rollback_prs": 0},
                "failed_pr_refs": [f"pantheon-config#{idx}"] if idx == 0 else [],
                "operator_touches": 0,
                "lead_time_arc_hours": 20,
            }
        )
        rows.append(
            {
                "schema": "pantheon.quant.delivery_arc/v1",
                "run_id": "delivery",
                "generated_at": "2026-10-09T00:00:00Z",
                "repo": "pantheon-config",
                "arc_id": f"M-{idx}",
                "pr_count": 3,
                "merged_prs": 3,
                "change_failures": 1 if idx == 0 else 0,
                "change_failure_breakdown": {"revert_prs": 0, "hotfix_prs": 1 if idx == 0 else 0, "rollback_prs": 0},
                "failed_pr_refs": [f"pantheon-config#{idx + 10}"] if idx == 0 else [],
                "operator_touches": 0,
                "lead_time_arc_hours": 30,
            }
        )
    return rows


class FakeRenderer:
    def render(self, *, row, source, attempt, prior_reject_reasons):
        return {
            "situation": "A rollout had enough review evidence to proceed.",
            "decision": "The call was to approve the rollout.",
            "renderer_model": "fake-renderer",
            "renderer_prompt_version": h2.RENDERER_PROMPT_VERSION,
            "renderer_prompt_sha256": "fake-sha",
            "renderer_command": "fake",
        }


# Shaped like a live rate-limited `codex exec`: rc 1, stderr starts with the
# codex banner / prompt echo and the usage-limit line comes last.
CODEX_BANNER = "OpenAI Codex v0.0.0 (research preview)"
OUTAGE_REASON = (
    "renderer_unavailable: codex exec rc=1: "
    f"{CODEX_BANNER}\n--------\nworkdir: /home/x\nmodel: m\n--------\n"
    "user\nINPUT_JSON: {...}\n"
    "ERROR: You've hit your usage limit. Try again at Oct 14th, 2026 4:13 AM."
)
RAW_STDERR_FRAGMENTS = (CODEX_BANNER, "INPUT_JSON", "workdir:", "renderer_unavailable:", "hit your usage limit")


class UnavailableRenderer:
    """Fake renderer behaving like codex while rate-limited."""

    def render(self, *, row, source, attempt, prior_reject_reasons):
        raise h2.RendererUnavailable(OUTAGE_REASON)


class FailsAfterFirstItemRenderer:
    """Renders item 1, then codex goes unavailable on item 2."""

    def __init__(self):
        self.calls = 0

    def render(self, *, row, source, attempt, prior_reject_reasons):
        self.calls += 1
        if self.calls > 1:
            raise h2.RendererUnavailable(OUTAGE_REASON)
        return FakeRenderer().render(
            row=row, source=source, attempt=attempt, prior_reject_reasons=prior_reject_reasons
        )


class BrokenRenderer:
    """A non-outage bug: must still fail the build."""

    def render(self, *, row, source, attempt, prior_reject_reasons):
        raise ValueError("renderer bug")


class UnavailableCodexRenderer:
    """Stand-in for CodexParaphraseRenderer whose `codex --version` fails."""

    def __init__(self, *args, **kwargs):
        raise h2.RendererUnavailable("renderer_unavailable: codex --version rc=1: rate limited")


def low_week_samples_and_replies(weeks: list[str]) -> tuple[list[dict], list[dict]]:
    samples, replies = [], []
    for week in weeks:
        sample_id = f"h2-{week}-prior"
        for item_no in (1, 2):
            samples.append(
                {
                    "schema": "pantheon.quant.call_h2_sample/v1",
                    "sample_id": sample_id,
                    "week_start": week,
                    "item_no": item_no,
                    "call_id": f"prior-{week}-{item_no}",
                }
            )
        replies.append(
            {
                "schema": "pantheon.quant.call_h2_reply/v1",
                "sample_id": sample_id,
                "parse_status": "parsed",
                "answers": [{"item_no": 1, "answer": "disagree"}, {"item_no": 2, "answer": "disagree"}],
            }
        )
    return samples, replies


def falsifier_view(rows: list[dict]) -> list[tuple]:
    return [
        (
            row["week_start"],
            row["answered"],
            row["lower_bound_below_0_70"],
            row["consecutive_low_weeks"],
            row["falsifier_triggered"],
            row["verdict"],
        )
        for row in rows
    ]


class QuantWeeklyDigestTests(unittest.TestCase):
    def seed(self, root: Path) -> None:
        write_jsonl(root / "call_h1.jsonl", [
            h1_row("ALL", 100, 2),
            h1_row("cited_pd", 60, 1),
            h1_row("uncited_pd", 60, 2),
            h1_row("D999", 0, 0, "dead-guidance"),
        ])
        write_jsonl(root / "call_outcomes.jsonl", [outcome("call-1"), outcome("call-2")])
        write_jsonl(root / "delivery_weekly.jsonl", delivery_weekly())
        write_jsonl(root / "delivery_arcs.jsonl", delivery_arcs())

    def args(self, root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            out=root,
            outcomes=root / "call_outcomes.jsonl",
            week_start="2026-10-05",
            days=60,
            seed="digest-seed",
            sample_id="digest-sample",
            limit=1,
            renderer_timeout=120,
            renderer_max_attempts=3,
        )

    def test_build_writes_markdown_pointer_and_h2_outbox(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            run = digest.build_digest(self.args(root), renderer=FakeRenderer())
            digest_path = Path(run["digest_file"])
            pointer_path = Path(run["telegram_pointer_file"])
            h2_outbox = Path(run["h2_outbox"])
            self.assertTrue(digest_path.exists())
            self.assertTrue(pointer_path.exists())
            self.assertTrue(h2_outbox.exists())

            md = digest_path.read_text(encoding="utf-8")
            self.assertIn("H1, H3 and H4 are observational correlations, not causation", md)
            self.assertIn("## Dead Guidance", md)
            self.assertIn("D999", md)
            self.assertIn("## P/D Cite Counts", md)
            self.assertIn("## Via Class Breakdown", md)
            self.assertIn("## H4 Failed Arc Spot Check", md)
            self.assertIn("REBOUND/review churn is excluded", md)
            self.assertIn("## Top Uncited Calls", md)

            pointer = pointer_path.read_text(encoding="utf-8")
            self.assertLessEqual(len(pointer.strip().splitlines()), 6)
            self.assertIn("Calls holding: 2% reversed (pass)", pointer)
            self.assertIn("You agree: awaiting your grade", pointer)
            self.assertNotIn("H1", pointer)
            self.assertNotIn("H2", pointer)
            self.assertNotIn("H3", pointer)
            self.assertNotIn("H4", pointer)

            rows = digest.load_jsonl(root / "digest_weekly.jsonl")
            self.assertEqual(rows[-1]["verdicts"], {"h1": "pass", "h2": "insufficient-n", "h3": "pass", "h4": "pass"})

    def test_reuses_existing_h2_sample_for_week(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            args = self.args(root)
            digest.build_digest(args, renderer=FakeRenderer())
            samples_before = digest.load_jsonl(root / "call_h2_samples.jsonl")
            digest.build_digest(args, renderer=FakeRenderer())
            samples_after = digest.load_jsonl(root / "call_h2_samples.jsonl")
            self.assertEqual(samples_before, samples_after)

    def test_renderer_unavailable_degrades_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run = digest.build_digest(self.args(root), renderer=UnavailableRenderer())

            self.assertIn("WARNING", stderr.getvalue())
            self.assertIn("paraphrase renderer unavailable (codex usage limit)", stderr.getvalue())
            self.assertIn("hit your usage limit", stderr.getvalue())

            digest_path = Path(run["digest_file"])
            pointer_path = Path(run["telegram_pointer_file"])
            self.assertTrue(digest_path.exists())
            self.assertTrue(pointer_path.exists())
            self.assertIsNone(run["h2_outbox"])
            self.assertEqual(run["degraded"], ["h2_renderer_unavailable"])
            self.assertFalse(run["h2_info"]["created"])
            # Full raw reason is kept in the machine-readable fields.
            self.assertEqual(run["h2_info"]["renderer_unavailable"], OUTAGE_REASON)

            # No H2 sample and no outbox were written for the outage week.
            self.assertFalse((root / "h2" / "outbox").exists())
            self.assertEqual(digest.load_jsonl(root / "call_h2_samples.jsonl"), [])

            md = digest_path.read_text(encoding="utf-8")
            self.assertIn("H2 sample not drawn this run: paraphrase renderer unavailable (codex usage limit).", md)
            self.assertIn("will be drawn late", md)
            self.assertIn("Until that late sample is drawn and graded", md)
            for fragment in RAW_STDERR_FRAGMENTS:
                self.assertNotIn(fragment, md)
            self.assertNotIn("- outbox:", md)
            # The rest of the digest still renders.
            self.assertIn("D999", md)
            self.assertIn("## H4 Failed Arc Spot Check", md)
            self.assertIn("## Top Uncited Calls", md)

            pointer = pointer_path.read_text(encoding="utf-8")
            self.assertLessEqual(len(pointer.strip().splitlines()), 6)
            self.assertIn("Calls holding: 2% reversed (pass)", pointer)
            self.assertIn("You agree: no sample this week, paraphrase renderer unavailable (codex usage limit)", pointer)
            self.assertIn("will be drawn late", pointer)
            for fragment in RAW_STDERR_FRAGMENTS:
                self.assertNotIn(fragment, pointer)
            self.assertIn("weekly calls: not drawn yet (renderer unavailable)", pointer)
            self.assertNotIn("None", pointer)
            self.assertNotIn("H2", pointer)

            rows = digest.load_jsonl(root / "digest_weekly.jsonl")
            self.assertEqual(rows[-1]["verdicts"], {"h1": "pass", "h2": "insufficient-n", "h3": "pass", "h4": "pass"})
            self.assertIsNone(rows[-1]["h2_outbox"])
            self.assertIsNone(rows[-1]["h2_sample_id"])
            self.assertFalse(rows[-1]["h2_sample_created"])
            self.assertEqual(rows[-1]["h2_renderer_unavailable"], OUTAGE_REASON)

            run_files = list((root / "runs").glob("quant-weekly-digest-*.jsonl"))
            self.assertEqual(len(run_files), 1)
            run_row = digest.load_jsonl(run_files[0])[-1]
            self.assertEqual(run_row["h2_info"]["renderer_unavailable"], OUTAGE_REASON)

    def test_unrecognised_outage_uses_plain_phrase(self):
        self.assertEqual(
            digest.h2_outage_phrase("renderer_unavailable: codex --version failed: [Errno 2] No such file"),
            "paraphrase renderer unavailable",
        )

    def test_non_outage_renderer_error_still_fails_build(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            with self.assertRaises(ValueError):
                digest.build_digest(self.args(root), renderer=BrokenRenderer())
            self.assertFalse((root / "digest").exists())
            self.assertEqual(digest.load_jsonl(root / "digest_weekly.jsonl"), [])
            self.assertFalse((root / "runs").exists() and list((root / "runs").glob("quant-weekly-digest-*")))
            self.assertEqual(digest.load_jsonl(root / "call_h2_samples.jsonl"), [])

    def test_outage_mid_sample_writes_no_partial_sample(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            args = self.args(root)
            args.limit = 2
            renderer = FailsAfterFirstItemRenderer()
            with contextlib.redirect_stderr(io.StringIO()):
                run = digest.build_digest(args, renderer=renderer)
            self.assertEqual(renderer.calls, 2)  # item 1 rendered, item 2 hit the outage
            self.assertEqual(digest.load_jsonl(root / "call_h2_samples.jsonl"), [])
            self.assertFalse((root / "h2" / "outbox").exists())
            self.assertIsNone(run["h2_outbox"])
            self.assertEqual(run["degraded"], ["h2_renderer_unavailable"])
            self.assertTrue(Path(run["digest_file"]).exists())
            self.assertNotIn("2026-10-05", [row.get("week_start") for row in digest.load_jsonl(root / "call_h2_weekly.jsonl")])
            pointer = Path(run["telegram_pointer_file"]).read_text(encoding="utf-8")
            md = Path(run["digest_file"]).read_text(encoding="utf-8")
            for fragment in RAW_STDERR_FRAGMENTS:
                self.assertNotIn(fragment, pointer)
                self.assertNotIn(fragment, md)

    def test_renderer_unavailable_does_not_feed_h2_falsifier(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            samples, replies = low_week_samples_and_replies(["2026-09-14", "2026-09-21", "2026-09-28"])
            write_jsonl(root / "call_h2_samples.jsonl", samples)
            write_jsonl(root / "call_h2_replies.jsonl", replies)
            before = falsifier_view(h2.recompute_weekly(root, "before", "2026-10-09T00:00:00Z"))
            self.assertEqual(before[-1][3], 3)

            with contextlib.redirect_stderr(io.StringIO()):
                digest.build_digest(self.args(root), renderer=UnavailableRenderer())

            after = falsifier_view(digest.load_jsonl(root / "call_h2_weekly.jsonl"))
            self.assertEqual(before, after)
            self.assertNotIn("2026-10-05", [row[0] for row in after])
            self.assertFalse(any(row[4] for row in after))
            self.assertEqual(digest.load_jsonl(root / "call_h2_samples.jsonl"), samples)

    def test_main_exits_zero_when_default_renderer_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(h2, "CodexParaphraseRenderer", UnavailableCodexRenderer), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = digest.main(["build", "--out", str(root), "--week-start", "2026-10-05"])
            self.assertEqual(rc, 0)
            self.assertIn("WARNING", stderr.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertIsNone(result["h2_outbox"])
            self.assertIn("codex --version rc=1", result["h2_info"]["renderer_unavailable"])
            self.assertTrue(Path(result["digest_file"]).exists())

    def test_build_after_outage_draws_sample_when_renderer_returns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            args = self.args(root)
            with contextlib.redirect_stderr(io.StringIO()):
                digest.build_digest(args, renderer=UnavailableRenderer())
            run = digest.build_digest(args, renderer=FakeRenderer())
            self.assertTrue(run["h2_info"]["created"])
            self.assertNotIn("renderer_unavailable", run["h2_info"])
            self.assertEqual(run["degraded"], [])
            self.assertTrue(Path(run["h2_outbox"]).exists())
            pointer = Path(run["telegram_pointer_file"]).read_text(encoding="utf-8")
            self.assertIn("You agree: awaiting your grade", pointer)
            self.assertNotIn("renderer unavailable", pointer)
            rows = digest.load_jsonl(root / "digest_weekly.jsonl")
            self.assertTrue(rows[-1]["h2_sample_created"])
            self.assertIsNone(rows[-1]["h2_renderer_unavailable"])

    def test_late_manual_sample_is_picked_up_by_next_build(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.seed(root)
            args = self.args(root)
            with contextlib.redirect_stderr(io.StringIO()):
                digest.build_digest(args, renderer=UnavailableRenderer())
            manual = h2.sample(
                argparse.Namespace(
                    out=root,
                    outcomes=root / "call_outcomes.jsonl",
                    week_start="2026-10-05",
                    seed="manual-late",
                    sample_id=None,
                    limit=1,
                    days=60,
                    renderer_timeout=120,
                    renderer_max_attempts=3,
                ),
                renderer=FakeRenderer(),
            )
            # The next digest build must reuse the manual sample (existing path),
            # even if the renderer is still down.
            run = digest.build_digest(args, renderer=UnavailableRenderer())
            self.assertFalse(run["h2_info"]["created"])
            self.assertEqual(run["h2_info"]["sample_id"], manual["sample_id"])
            self.assertEqual(run["h2_outbox"], manual["outbox"])
            self.assertEqual(run["degraded"], [])
            md = Path(run["digest_file"]).read_text(encoding="utf-8")
            self.assertIn(f"- outbox: {manual['outbox']}", md)
            self.assertNotIn("not drawn", md)

    def test_h3_is_insufficient_before_eight_weeks(self):
        got = digest.summarize_h3(delivery_weekly()[:7], "2026-10-05")
        self.assertEqual(got["verdict"], "insufficient-n")

    def test_h1_marks_structural_citation_comparison_untestable(self):
        rows = [
            h1_row("ALL", 100, 2),
            h1_row("cited_pd", 100, 2),
            h1_row("uncited_pd", 0, 0, "insufficient-n"),
            h1_row("D1", 50, 10),
        ]
        got = digest.summarize_h1(rows)
        self.assertEqual(got["verdict"], "pass")
        self.assertEqual(got["citation_comparison"], "untestable by design")
        self.assertIn("untestable by design", got["reason"])
        self.assertEqual([row["group"] for row in got["pd_adverse_above_overall"]], ["D1"])

    def test_h4_splits_failure_rate_from_lead_time_scaling(self):
        rows = []
        for idx in range(255):
            rows.append(
                {
                    "repo": "pantheon-config",
                    "arc_id": f"S-{idx}",
                    "merged_prs": 1,
                    "change_failures": 1 if idx == 0 else 0,
                    "change_failure_breakdown": {"revert_prs": 0, "hotfix_prs": 1 if idx == 0 else 0, "rollback_prs": 0},
                    "lead_time_arc_hours": 4,
                }
            )
        for idx in range(59):
            rows.append(
                {
                    "repo": "pantheon-config",
                    "arc_id": f"M-{idx}",
                    "merged_prs": 3,
                    "change_failures": 1 if idx < 4 else 0,
                    "change_failure_breakdown": {"revert_prs": 0, "hotfix_prs": 0, "rollback_prs": 1 if idx < 4 else 0},
                    "lead_time_arc_hours": 78,
                }
            )
        got = digest.summarize_h4(rows)
        self.assertEqual(got["verdict"], "fail")
        self.assertEqual(got["failure_rate_verdict"], "no-clear-difference")
        self.assertEqual(got["lead_time_verdict"], "fail")
        self.assertIn("failure rate had no clear difference", got["reason"])
        self.assertIn("lead-time scaling failed", got["reason"])


if __name__ == "__main__":
    unittest.main()
