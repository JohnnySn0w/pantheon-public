#!/usr/bin/env python3
"""GEPA Phase-2 run entrypoint (T-2809, ORG-074 build-step 1).

THE ONE MODULE IN THIS PACKAGE THAT IMPORTS `gepa` AT TOP LEVEL. It is a
script entrypoint (not `test_*.py`), so `python3 -m pytest tools/ -q` under
system python never collects/imports it directly; `tools/test_gepa_run_phase2.py`
imports it behind a skip-guard for the same reason every other test module
in this package is skip-guarded against the real package's absence.

Must be run under the dedicated venv: `/home/claude/.venv-gepa/bin/python -m
tools.gepa.run_phase2` (or `tools/gepa/run_phase2.sh`), never system python3.

Builds and wires everything; runs `gepa.optimize(...)` on the TRAIN split
only (holdout never enters this call — see the anti-leakage note below),
then measures the seed and best candidates on the HOLDOUT split via two
direct, explicit `adapter.evaluate()` calls with a measurement-mode adapter
instance. NOTHING auto-applies: the terminal step is writing a report that
says what a ratify ticket should contain, never editing
`claude-config/agents/reviewer.md` or filing anything.

Anti-leakage design point (load-bearing, HO plan-approval feedback,
2026-08-12): `gepa.optimize()`'s `valset` parameter feeds Pareto-front
tracking DURING the run — i.e. it is in-loop even though it is scoring-only,
not training. Passing the holdout split there would violate "holdout never
enters the loop" despite not being used for reflection. So `valset=trainset`
(the identical id list), and holdout scores are computed only by the two
explicit post-optimize `evaluate()` calls below.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Sequence

import gepa
from gepa.utils import TimeoutStopCondition

from tools.gepa import case_loader, claude_executor, prompts, report
from tools.gepa.adapter import ExecutorCall, ReviewerGEPAAdapter
from tools.gepa.scorer import ScoredRollout
from tools.ticket import REPO_ROOT

DEFAULT_SEED_CANDIDATE_PATH = Path("/home/claude/claude-config/agents/reviewer.md")
DEFAULT_RUN_DIR = Path("/tmp/gepa_phase2_run")
DEFAULT_OUT_DIR = Path("/tmp/gepa_phase2_report")

# Fixed skills context (PoC plan §7.3 decision): the SAME two files for every
# rollout regardless of the case's source repo. Honest, flagged limitation
# for cross-repo cases (echo-prismatic/pantheon-pwa/pantheon-public) — see
# tools/gepa/prompts.py and the T-2809 Phase-2 plan approval message.
ORG_DISCIPLINE_SKILL_PATH = Path("/home/claude/.claude/skills/pantheon-org-discipline/SKILL.md")
DISPATCH_SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "pantheon-dispatch" / "SKILL.md"


class IsolationFlagCheckError(Exception):
    """Raised when a built argv is missing a mandatory isolation flag, or carries --bare."""


class VenvCheckError(Exception):
    """Raised when `gepa` is not being imported from the dedicated /home/claude/.venv-gepa venv."""


class EvalSetDigestMismatchError(Exception):
    """Raised when --expect-digest is given and does not match the live eval-set digest."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the GEPA reviewer-persona optimization (T-2809 Phase 2). "
        "Must run under /home/claude/.venv-gepa."
    )
    parser.add_argument("--max-rollouts", type=int, default=150, help="max_metric_calls budget (PoC-plan placeholder)")
    parser.add_argument("--wall-clock-seconds", type=int, default=21600, help="hard wall-clock stop, default 6h (placeholder)")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR, help="GEPA checkpoint/resume state dir (never the repo)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="report JSON+markdown output dir (never the repo)")
    parser.add_argument("--executor-model", default="claude-sonnet-5")
    parser.add_argument("--reflector-model", default="claude-opus-5")
    parser.add_argument("--judge-model", default=None, help="defaults to --executor-model if omitted")
    parser.add_argument("--seed-candidate-path", type=Path, default=DEFAULT_SEED_CANDIDATE_PATH)
    parser.add_argument(
        "--keep-sessions",
        action="store_true",
        default=False,
        help="omit --no-session-persistence (trades storage hygiene for --resume debuggability)",
    )
    parser.add_argument("--expect-digest", default=None, help="pin the eval-set digest; refuse to run on mismatch")
    parser.add_argument("--seed", type=int, default=2809, help="RNG seed (T-2809-numbered, matches the curator's split seed)")
    return parser.parse_args(argv)


def compute_eval_set_digest(eval_set_dir: Path = case_loader.EVAL_SET_DIR) -> str:
    """sha256 over manifest.json bytes + every sorted case file's (name, bytes).

    Deterministic; changes iff the frozen eval set's committed content
    changes. Printed on every run and optionally checked against
    `--expect-digest` so a run doesn't silently optimize/measure against an
    eval set that changed since the ticket log last cited it.
    """
    hasher = hashlib.sha256()
    hasher.update((eval_set_dir / "manifest.json").read_bytes())
    for path in sorted((eval_set_dir / "cases").glob("*.json")):
        hasher.update(path.name.encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def check_isolation_flags_present(argv: Sequence[str]) -> None:
    """Self-check that a built claude argv carries both mandatory isolation flags and never --bare.

    Defense in depth: `claude_executor.build_claude_argv` already makes this
    structurally unconditional, so this should never actually fire — it is a
    cheap runtime assertion at the one preflight call site and at every
    wiring factory in this module.
    """
    argv = list(argv)
    if "--bare" in argv:
        raise IsolationFlagCheckError(
            "argv contains --bare, which disables OAuth/keychain and breaks keyless-seat sub-billing"
        )
    if "--strict-mcp-config" not in argv:
        raise IsolationFlagCheckError("argv missing --strict-mcp-config")
    try:
        idx = argv.index("--setting-sources")
    except ValueError:
        raise IsolationFlagCheckError("argv missing --setting-sources") from None
    if idx + 1 >= len(argv) or argv[idx + 1] != "":
        raise IsolationFlagCheckError("argv's --setting-sources value is not the empty string")


def check_venv_or_raise() -> None:
    """Raise VenvCheckError if `gepa` was not imported from /home/claude/.venv-gepa."""
    if ".venv-gepa" not in gepa.__file__:
        raise VenvCheckError(
            f"gepa imported from {gepa.__file__!r}, not /home/claude/.venv-gepa — "
            "run via tools/gepa/run_phase2.sh or that venv's python directly."
        )


def build_seed_candidate(persona_path: Path) -> dict[str, str]:
    """Read the persona file into the single-component candidate dict. Raises FileNotFoundError as-is."""
    text = persona_path.read_text(encoding="utf-8")
    return {ReviewerGEPAAdapter.PERSONA_KEY: text}


def build_fixed_skills_context() -> str:
    parts = []
    for path in (ORG_DISCIPLINE_SKILL_PATH, DISPATCH_SKILL_PATH):
        parts.append(f"### {path.name} ({path})\n\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def make_claude_rollout_executor(*, model: str, keep_sessions: bool, timeout: float = 300.0):
    """Build the RolloutExecutorFn: (persona_text, task_prompt) -> ExecutorCall.

    Injects the candidate persona via `--agents`/`--agent` (live-verified
    2026-08-12 to survive the isolation flags; see
    tools/test_gepa_claude_executor.py::TestLiveAgentsPersonaMechanism).
    """

    def _executor(persona_text: str, task_prompt: str) -> ExecutorCall:
        agents_json = prompts.build_agents_persona_payload(persona_text)
        argv = claude_executor.build_claude_argv(
            model=model,
            prompt=task_prompt,
            agents_json=agents_json,
            agent_name="reviewer",
            keep_sessions=keep_sessions,
        )
        check_isolation_flags_present(argv)
        raw_response = claude_executor.run_argv(argv, timeout=timeout)
        return ExecutorCall(argv=tuple(argv), raw_response=raw_response)

    return _executor


def make_claude_plain_lm(*, model: str, keep_sessions: bool, timeout: float = 600.0):
    """Build a plain `(str) -> str` callable — the LanguageModel-protocol swap seam.

    Used for both `reflection_lm` (GEPA's own protocol) and `judge_fn`
    (this adapter's own protocol; identical shape). Swapping the reflector to
    a codex-backed callable later is exactly supplying a different function
    matching this same signature — no additional indirection needed.
    """

    def _lm(prompt: str) -> str:
        argv = claude_executor.build_claude_argv(model=model, prompt=prompt, keep_sessions=keep_sessions)
        check_isolation_flags_present(argv)
        return claude_executor.run_argv(argv, timeout=timeout)

    return _lm


def _scored_by_case_id(eval_batch, case_ids: Sequence[str]) -> dict[str, ScoredRollout]:
    """Zip an EvaluationBatch's trajectories back to case ids, substituting a
    0.0-scored placeholder for any case whose rollout failed (traj.scored is None)."""
    trajectories = eval_batch.trajectories or []
    out: dict[str, ScoredRollout] = {}
    for case_id, traj in zip(case_ids, trajectories):
        if traj.scored is not None:
            out[case_id] = traj.scored
        else:
            out[case_id] = ScoredRollout(
                score=0.0,
                verdict_found=None,
                verdict_correct=False,
                caught_categories=(),
                missed_categories=(),
                wrongly_flagged_categories=(),
                feedback=f"rollout failed and scored 0.0: {traj.error}",
            )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    check_venv_or_raise()

    digest = compute_eval_set_digest()
    print(f"[run_phase2] eval-set digest: {digest}")
    if args.expect_digest is not None and digest != args.expect_digest:
        raise EvalSetDigestMismatchError(
            f"eval-set digest mismatch: expected {args.expect_digest}, got {digest} — "
            "the frozen eval set changed since this run was pinned; re-verify before proceeding"
        )

    seed_candidate = build_seed_candidate(args.seed_candidate_path)
    persona_text = seed_candidate[ReviewerGEPAAdapter.PERSONA_KEY]
    print(f"[run_phase2] seed candidate: {args.seed_candidate_path} ({len(persona_text)} chars)")

    check_isolation_flags_present(claude_executor.build_claude_argv(model=args.executor_model))
    print("[run_phase2] isolation-flags self-check: OK")

    fixed_skills_context = build_fixed_skills_context()
    judge_model = args.judge_model or args.executor_model

    rollout_executor = make_claude_rollout_executor(model=args.executor_model, keep_sessions=args.keep_sessions)
    reflection_lm = make_claude_plain_lm(model=args.reflector_model, keep_sessions=args.keep_sessions)
    judge_fn = make_claude_plain_lm(model=judge_model, keep_sessions=args.keep_sessions)

    all_cases = case_loader.load_all_cases()
    train_ids = list(case_loader.load_cases("train"))
    holdout_ids = list(case_loader.load_cases("holdout"))
    print(f"[run_phase2] {len(train_ids)} train cases, {len(holdout_ids)} holdout cases")

    optimize_adapter = ReviewerGEPAAdapter(
        cases_by_id=all_cases,
        fixed_skills_context=fixed_skills_context,
        rollout_executor=rollout_executor,
        judge_fn=judge_fn,
        optimization_mode=True,
        baseline_length=len(persona_text),
    )

    result = gepa.optimize(
        seed_candidate=seed_candidate,
        trainset=train_ids,
        valset=train_ids,  # anti-leakage: see module docstring — never the holdout split
        adapter=optimize_adapter,
        reflection_lm=reflection_lm,
        max_metric_calls=args.max_rollouts,
        stop_callbacks=TimeoutStopCondition(args.wall_clock_seconds),
        run_dir=str(args.run_dir),
        seed=args.seed,
        raise_on_exception=True,
    )

    best_candidate = result.best_candidate
    best_text = best_candidate[ReviewerGEPAAdapter.PERSONA_KEY]

    measure_adapter = ReviewerGEPAAdapter(
        cases_by_id=all_cases,
        fixed_skills_context=fixed_skills_context,
        rollout_executor=rollout_executor,
        judge_fn=judge_fn,
        optimization_mode=False,
        baseline_length=None,
    )
    before_batch = measure_adapter.evaluate(holdout_ids, seed_candidate, capture_traces=True)
    after_batch = measure_adapter.evaluate(holdout_ids, best_candidate, capture_traces=True)

    report_dict = report.build_report(
        seed_persona_text=persona_text,
        best_persona_text=best_text,
        before_scored=_scored_by_case_id(before_batch, holdout_ids),
        after_scored=_scored_by_case_id(after_batch, holdout_ids),
        total_metric_calls=result.total_metric_calls,
        run_dir=str(args.run_dir),
    )
    markdown = report.render_markdown(report_dict)
    json_path, md_path = report.write_report(report_dict, markdown, args.out_dir)

    print(f"[run_phase2] DONE. Nothing auto-applied. Report written: {json_path} / {md_path}")
    print(report_dict["recommended_next_step"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
