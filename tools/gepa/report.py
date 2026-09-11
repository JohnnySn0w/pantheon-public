"""Phase-2 run report builder (T-2809 Phase 2).

Pure data-in/data-out — no `gepa` import, no LM calls, no subprocess. Takes
already-scored before/after holdout results and produces a JSON-serializable
report dict plus a markdown draft shaped like what a ratify ticket should
contain. Nothing here writes to the repo: `write_report` refuses any
`out_dir` that resolves inside the repo and is not under `/tmp`, mirroring
`tools/gepa_eval_extract.py`'s `is_refused_out_path` convention.

Per the brief: "NOTHING auto-applies — the run's terminal step is printing
what a ratify ticket should contain, not filing/editing anything." This
module is that terminal step's formatter; `run_phase2.py` calls it and
prints/writes the result, and does nothing else with it.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Mapping

from tools.gepa.scorer import ScoredRollout
from tools.ticket import REPO_ROOT


def unified_diff(before_text: str, after_text: str) -> str:
    """Unified diff of the persona text, before (seed) vs after (best candidate)."""
    diff_lines = difflib.unified_diff(
        before_text.splitlines(),
        after_text.splitlines(),
        fromfile="seed (before)",
        tofile="best (after)",
        lineterm="",
    )
    return "\n".join(diff_lines)


def build_report(
    *,
    seed_persona_text: str,
    best_persona_text: str,
    before_scored: Mapping[str, ScoredRollout],
    after_scored: Mapping[str, ScoredRollout],
    total_metric_calls: int | None,
    run_dir: str | None,
) -> dict:
    """Build the JSON-serializable report dict from before/after holdout scoring.

    `before_scored` and `after_scored` must cover exactly the same case-id
    set — a mismatch is a systemic wiring error (comparing two different
    holdout slices would silently produce a meaningless delta), so this
    raises rather than degrading.
    """
    if set(before_scored) != set(after_scored):
        raise ValueError(
            "before_scored and after_scored must cover the same case ids; "
            f"before-only={set(before_scored) - set(after_scored)}, "
            f"after-only={set(after_scored) - set(before_scored)}"
        )

    case_ids = sorted(before_scored)
    n = len(case_ids)
    mean_before = sum(before_scored[cid].score for cid in case_ids) / n if n else 0.0
    mean_after = sum(after_scored[cid].score for cid in case_ids) / n if n else 0.0

    per_case = {
        cid: {
            "before": before_scored[cid].score,
            "after": after_scored[cid].score,
            "delta": after_scored[cid].score - before_scored[cid].score,
            "before_missed": list(before_scored[cid].missed_categories),
            "after_missed": list(after_scored[cid].missed_categories),
            "before_wrongly_flagged": list(before_scored[cid].wrongly_flagged_categories),
            "after_wrongly_flagged": list(after_scored[cid].wrongly_flagged_categories),
        }
        for cid in case_ids
    }

    return {
        "holdout": {
            "n_cases": n,
            "mean_before": mean_before,
            "mean_after": mean_after,
            "mean_delta": mean_after - mean_before,
            "per_case": per_case,
        },
        "persona_diff": unified_diff(seed_persona_text, best_persona_text),
        "total_metric_calls": total_metric_calls,
        "run_dir": run_dir,
        "nothing_auto_applies": True,
        "recommended_next_step": (
            "File a ratify ticket to the operator with this report's holdout numbers and persona_diff "
            "(ORG-074 R1 go/no-go). No persona file has been changed by this run."
        ),
    }


def render_markdown(report: dict) -> str:
    """Render `report` (from build_report) as a markdown draft for a ratify-ticket body."""
    h = report["holdout"]
    lines = [
        "# GEPA Phase-2 run report — proposed reviewer-persona diff",
        "",
        "**Nothing has been applied automatically.** This is a draft for a ratify ticket, not a change.",
        "",
        f"- Holdout cases measured: {h['n_cases']}",
        f"- Mean holdout score before: {h['mean_before']:.3f}",
        f"- Mean holdout score after: {h['mean_after']:.3f}",
        f"- Mean delta: {h['mean_delta']:+.3f}",
        f"- Total GEPA metric calls: {report.get('total_metric_calls')}",
        f"- Run dir (checkpoint state): {report.get('run_dir')}",
        "",
        "## Per-case holdout scores",
        "",
        "| case_id | before | after | delta |",
        "|---|---|---|---|",
    ]
    for cid, row in h["per_case"].items():
        lines.append(f"| {cid} | {row['before']:.3f} | {row['after']:.3f} | {row['delta']:+.3f} |")
    lines += [
        "",
        "## Proposed persona diff (seed vs best candidate)",
        "",
        "```diff",
        report["persona_diff"] or "(no textual change — best candidate equals the seed candidate)",
        "```",
        "",
        "## Recommended next step",
        "",
        report["recommended_next_step"],
    ]
    return "\n".join(lines)


def _refuse_if_repo_internal(out_dir: Path, repo_root: Path) -> None:
    """Raise iff `out_dir` resolves inside `repo_root` — unconditionally, regardless of
    where `repo_root` itself happens to be checked out.

    This checks repo-membership FIRST and has no separate "/tmp carve-out" branch. An
    earlier version checked "is this under /tmp?" first and returned early (allowed) if
    so — which silently defeated the whole refusal for every worker dispatch, since this
    org's normal dispatch shape stages worktrees AT `/tmp/<repo>-<ticket>`: any path
    nested inside such a worktree (e.g. `knowledge/some_report_dir`) is *also* nested
    inside `/tmp`, so the old carve-out matched first and let the write through before
    the repo-internal check ever ran. That defect is exactly how
    `knowledge/some_report_dir/{report.json,report.md}` ended up committed-tree-adjacent
    during Phase-2 development (T-2809). No separate /tmp allowance is needed: a
    genuinely external target (e.g. the real default `/tmp/gepa_phase2_report`, a sibling
    of the worktree, not nested inside it) is simply never inside `repo_root` in the
    first place, so it is never refused by this check.
    """
    resolved = out_dir.resolve()
    resolved_root = repo_root.resolve()
    if resolved == resolved_root or resolved_root in resolved.parents:
        raise ValueError(
            f"refusing to write the Phase-2 report to {out_dir} — it resolves inside the "
            f"repo checkout ({resolved_root}). Nothing this run produces belongs in the "
            "tracked repo tree; point --out-dir somewhere outside the checkout (e.g. /tmp)."
        )


def write_report(report: dict, markdown: str, out_dir: Path, *, repo_root: Path = REPO_ROOT) -> tuple[Path, Path]:
    """Write report.json and report.md to out_dir. Raises ValueError for a repo-internal out_dir.

    `repo_root` defaults to this checkout's REPO_ROOT and is overridable for tests that
    want to exercise the refusal against a repo_root distinct from wherever this test
    process happens to be running.
    """
    _refuse_if_repo_internal(out_dir, repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path
