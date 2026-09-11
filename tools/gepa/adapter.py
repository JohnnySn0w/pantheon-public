"""ReviewerGEPAAdapter — the GEPAAdapter implementation for T-2809 Phase 2.

`gepa.core.adapter.GEPAAdapter` is a structural `Protocol` (no
`@runtime_checkable`, no ABC — verified from the installed gepa 0.1.4
source). GEPA's engine and proposer only ever do attribute access
(`.outputs`, `.scores`, `.trajectories`, `.objective_scores`,
`.num_metric_calls`) on whatever `evaluate()` returns; no `isinstance(...,
EvaluationBatch)` check exists anywhere outside each bundled adapter's own
construction of the real dataclass. So `EvaluationBatch`/`Trajectory` below
are LOCAL, duck-typed lookalikes — this module has zero runtime dependency
on the `gepa` package, keeping it importable and unit-testable under system
python3 (mirroring the `tools/a2a` interpreter-carve-out convention; see
package docstring). `TYPE_CHECKING`-only imports below are for editor/typing
support and cost nothing at runtime.

Two structural (not caller-discipline) guardrails, both from the T-2809
Phase-2 plan:

- Holdout-leak refusal: `evaluate()` raises `HoldoutLeakError` immediately —
  before firing a single rollout — if `optimization_mode=True` (the default,
  used for the train/reflect loop) and ANY case in the batch has
  `split == "holdout"`. The one call site that measures holdout scores
  explicitly constructs a SECOND adapter instance with
  `optimization_mode=False` (see `run_phase2.py`), making the flip auditable
  and singular rather than a parameter callers might forget.
- Prompt-length cap: if the candidate persona text exceeds
  `length_cap_ratio * baseline_length`, the WHOLE batch is rejected with
  score 0.0 for every case and ZERO rollouts fired (no wasted executor
  calls on an already-doomed candidate) — GEPA's own acceptance criterion
  then naturally discards a zero-scoring candidate. `baseline_length=None`
  disables the cap entirely (used for the measurement-mode adapter, where
  there is no "rejecting" — we already have the two exact candidates we
  want scored).

Debuggability contract (HO plan-approval addition B, 2026-08-12): every
captured `Trajectory` carries the FULL executor argv, the COMPLETE task
prompt, and the COMPLETE raw response — a bad rollout must be replayable
byte-identically without needing `--resume` on a persisted session.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from tools.gepa import case_loader, prompts, scorer
from tools.gepa.case_loader import REPO_ROOT, Case

if TYPE_CHECKING:  # pragma: no cover — typing only, zero runtime cost/dependency
    from gepa.core.adapter import EvaluationBatch as _RealEvaluationBatch  # noqa: F401


class HoldoutLeakError(Exception):
    """Raised when a holdout-split case is handed to an optimization-mode evaluate() call."""


@dataclass(frozen=True)
class ExecutorCall:
    """What a `rollout_executor` callable returns: the exact argv used plus the raw response."""

    argv: tuple[str, ...]
    raw_response: str


@dataclass(frozen=True)
class Trajectory:
    """Per-case rollout record. Debuggability contract: argv/task_prompt/raw_response are
    always the FULL, complete values — never truncated or summarized."""

    case_id: str
    argv: tuple[str, ...] | None
    task_prompt: str | None
    raw_response: str | None
    scored: "scorer.ScoredRollout | None"
    error: str | None


@dataclass
class EvaluationBatch:
    """Local, gepa-import-free lookalike of `gepa.core.adapter.EvaluationBatch`.

    Field names and semantics match the real dataclass exactly (see module
    docstring for why this is safe) so a caller that DOES have `gepa`
    installed can freely construct the real one from these fields, as
    `tools.test_gepa_adapter.TestConformanceAgainstRealGepa` verifies.
    """

    outputs: list[Any]
    scores: list[float]
    trajectories: list[Trajectory] | None = None
    objective_scores: list[dict[str, float]] | None = None
    num_metric_calls: int | None = None


RolloutExecutorFn = Callable[[str, str], ExecutorCall]
JudgeFn = Callable[[str], str]


class ReviewerGEPAAdapter:
    """GEPAAdapter for optimizing the reviewer persona (`claude-config/agents/reviewer.md`).

    Candidate shape: `{PERSONA_KEY: "<full persona text>"}` — a single
    component, per the PoC plan's per-component constraint (ORG-074).
    """

    PERSONA_KEY = "reviewer_persona"

    def __init__(
        self,
        *,
        cases_by_id: Mapping[str, Case],
        fixed_skills_context: str,
        rollout_executor: RolloutExecutorFn,
        judge_fn: JudgeFn | None = None,
        repo_allowlist: Mapping[str, Path | None] | None = None,
        self_repo_root: Path = REPO_ROOT,
        optimization_mode: bool = True,
        baseline_length: int | None = None,
        length_cap_ratio: float = 1.3,
    ) -> None:
        self.cases_by_id = cases_by_id
        self.fixed_skills_context = fixed_skills_context
        self.rollout_executor = rollout_executor
        self.judge_fn = judge_fn
        self.repo_allowlist = repo_allowlist
        self.self_repo_root = self_repo_root
        self.optimization_mode = optimization_mode
        self.baseline_length = baseline_length
        self.length_cap_ratio = length_cap_ratio

    def _resolve_case(self, case_id: str) -> Case:
        try:
            return self.cases_by_id[case_id]
        except KeyError as exc:
            raise KeyError(
                f"unknown case_id {case_id!r} not present in this adapter's cases_by_id "
                "(systemic misconfiguration, not a per-example failure)"
            ) from exc

    def evaluate(
        self,
        batch: Sequence[str],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> EvaluationBatch:
        cases = [self._resolve_case(case_id) for case_id in batch]

        if self.optimization_mode:
            leaked = [c.case_id for c in cases if c.split == "holdout"]
            if leaked:
                raise HoldoutLeakError(
                    f"optimization_mode=True but holdout case(s) present in batch: {leaked} "
                    "— holdout must never enter the GEPA optimization loop"
                )

        persona_text = candidate[self.PERSONA_KEY]

        if self.baseline_length is not None and len(persona_text) > self.length_cap_ratio * self.baseline_length:
            reason = (
                f"candidate rejected: persona length {len(persona_text)} exceeds cap "
                f"{self.length_cap_ratio}x baseline {self.baseline_length} — no rollouts fired"
            )
            trajectories = (
                [
                    Trajectory(case_id=c.case_id, argv=None, task_prompt=None, raw_response=None, scored=None, error=reason)
                    for c in cases
                ]
                if capture_traces
                else None
            )
            return EvaluationBatch(outputs=["" for _ in cases], scores=[0.0 for _ in cases], trajectories=trajectories)

        outputs: list[str] = []
        scores: list[float] = []
        trajectories: list[Trajectory] | None = [] if capture_traces else None

        for case in cases:
            diff_text = case_loader.resolve_diff(
                case, repo_allowlist=self.repo_allowlist, self_repo_root=self.self_repo_root
            )
            task_prompt = prompts.build_case_task_prompt(
                case, diff_text=diff_text, fixed_skills_context=self.fixed_skills_context
            )
            try:
                call = self.rollout_executor(persona_text, task_prompt)
            except Exception as exc:  # noqa: BLE001 — per-example tolerated failure, never a batch-wide raise
                outputs.append("")
                scores.append(0.0)
                if trajectories is not None:
                    trajectories.append(
                        Trajectory(
                            case_id=case.case_id,
                            argv=None,
                            task_prompt=task_prompt,
                            raw_response=None,
                            scored=None,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                continue

            scored = scorer.score_rollout(case, call.raw_response, judge_fn=self.judge_fn)
            outputs.append(call.raw_response)
            scores.append(scored.score)
            if trajectories is not None:
                trajectories.append(
                    Trajectory(
                        case_id=case.case_id,
                        argv=call.argv,
                        task_prompt=task_prompt,
                        raw_response=call.raw_response,
                        scored=scored,
                        error=None,
                    )
                )

        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories)

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch,
        components_to_update: list[str],
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        if self.PERSONA_KEY not in components_to_update:
            return {}

        records: list[dict[str, Any]] = []
        for traj in eval_batch.trajectories or []:
            case = self.cases_by_id.get(traj.case_id)
            ask_summary = case.review_material.ask_summary if case is not None else ""
            if traj.error is not None:
                feedback = f"Rollout failed and scored 0.0: {traj.error}"
                generated = ""
            else:
                feedback = traj.scored.feedback if traj.scored is not None else ""
                generated = traj.raw_response or ""
            records.append(
                {
                    "Inputs": {"case_id": traj.case_id, "ask_summary": ask_summary},
                    "Generated Outputs": generated,
                    "Feedback": feedback,
                }
            )
        return {self.PERSONA_KEY: records}
