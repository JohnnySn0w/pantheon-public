"""Deterministic + LM-judge-assisted scoring for GEPA reviewer rollouts (T-2809 Phase 2).

Scoring formula (documented plainly, no P/D cite — empirical PoC
calibration, explicitly tunable at the eventual ratify review):

    score = clip(0.4 * verdict_match + 0.6 * findings_recall
                 - 0.25 * count(must_not_flag hits), 0.0, 1.0)

    findings_recall = caught / len(findings) if findings else 1.0

Category matching is deterministic-first (kebab-case category tokens
present in the review text) with an OPTIONAL `judge_fn` assist consulted
only when the deterministic check misses — the fuzzy-match rescue path, not
the primary path, to keep judge-model calls cheap.

Anti-leakage hard contract: `ScoredRollout.feedback` is built ENTIRELY from
this module's own templates plus category SLUGS (e.g.
"unbounded-retry-loop") and verdict labels (APPROVE/REBOUND). It never
contains `Finding.description` text (gold reference) or raw `judge_fn`
output text. See tools/test_gepa_scorer.py::TestFeedbackNeverLeaksGoldText
for the full-corpus regression proof.

No `gepa` import (see package docstring).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from tools.gepa.case_loader import Case, Finding

VERDICT_MATCH_WEIGHT = 0.4
FINDINGS_RECALL_WEIGHT = 0.6
MUST_NOT_FLAG_PENALTY = 0.25

# Category tokens shorter than this are too generic to match on alone
# ("of", "in", "vs") — mirrors the org's own kebab-case category convention
# seen throughout knowledge/eval/reviewer-v1/cases/*.json.
_MIN_TOKEN_LEN = 3


@dataclass(frozen=True)
class ScoredRollout:
    score: float
    verdict_found: str | None
    verdict_correct: bool
    caught_categories: tuple[str, ...]
    missed_categories: tuple[str, ...]
    wrongly_flagged_categories: tuple[str, ...]
    feedback: str


def parse_verdict(review_text: str) -> str | None:
    """Uppercase-literal APPROVE/REBOUND search (org convention: case-sensitive).

    Returns None when neither or both are present — an ambiguous/malformed
    rollout output, not a guess.
    """
    has_approve = "APPROVE" in review_text
    has_rebound = "REBOUND" in review_text
    if has_approve and not has_rebound:
        return "APPROVE"
    if has_rebound and not has_approve:
        return "REBOUND"
    return None


def _category_tokens(category: str) -> set[str]:
    return {tok for tok in category.split("-") if len(tok) >= _MIN_TOKEN_LEN}


def deterministic_category_hit(category: str, review_text: str) -> bool:
    """True if a strict majority of the category's keyword tokens appear (case-insensitive)."""
    tokens = _category_tokens(category)
    if not tokens:
        return False
    text_lower = review_text.lower()
    hits = sum(1 for tok in tokens if re.search(re.escape(tok), text_lower))
    return hits > len(tokens) / 2


def _category_hit(category: str, review_text: str, judge_fn: Callable[[str], str] | None) -> bool:
    if deterministic_category_hit(category, review_text):
        return True
    if judge_fn is None:
        return False
    prompt = (
        f"Does the following review text substantively raise a concern in the category "
        f"'{category}' (even if phrased differently)? Answer with exactly YES or NO, "
        f"then a one-sentence reason.\n\nReview text:\n{review_text}"
    )
    try:
        answer = judge_fn(prompt)
    except Exception:
        return False
    return "YES" in answer.upper()


def _feedback_for(
    *,
    case: Case,
    verdict_found: str | None,
    verdict_correct: bool,
    missed: tuple[str, ...],
    wrongly_flagged: tuple[str, ...],
) -> str:
    """Build the feedback string from templates + category slugs only. No gold text, no judge text."""
    parts: list[str] = []
    if not verdict_correct:
        found_label = verdict_found if verdict_found is not None else "no clear APPROVE/REBOUND verdict"
        parts.append(f"Verdict mismatch: expected {case.expected.verdict}, review gave {found_label}.")
    if missed:
        parts.append(f"Missed expected defect categor{'y' if len(missed) == 1 else 'ies'}: {', '.join(missed)}.")
    if wrongly_flagged:
        parts.append(
            f"Incorrectly raised categor{'y' if len(wrongly_flagged) == 1 else 'ies'} that a "
            f"correctly-calibrated review must not flag: {', '.join(wrongly_flagged)}."
        )
    if not parts:
        parts.append("Verdict matched and all expected defect categories were caught with no false findings.")
    return " ".join(parts)


def score_rollout(
    case: Case,
    review_text: str,
    judge_fn: Callable[[str], str] | None = None,
) -> ScoredRollout:
    """Score one rollout's `review_text` against `case.expected`. Pure; never raises."""
    verdict_found = parse_verdict(review_text)
    verdict_correct = verdict_found == case.expected.verdict

    caught: list[str] = []
    missed: list[str] = []
    for finding in case.expected.findings:
        if _category_hit(finding.category, review_text, judge_fn):
            caught.append(finding.category)
        else:
            missed.append(finding.category)

    wrongly_flagged: list[str] = []
    for finding in case.expected.must_not_flag:
        if _category_hit(finding.category, review_text, judge_fn):
            wrongly_flagged.append(finding.category)

    total_findings = len(case.expected.findings)
    findings_recall = (len(caught) / total_findings) if total_findings else 1.0

    raw_score = (
        VERDICT_MATCH_WEIGHT * (1.0 if verdict_correct else 0.0)
        + FINDINGS_RECALL_WEIGHT * findings_recall
        - MUST_NOT_FLAG_PENALTY * len(wrongly_flagged)
    )
    score = max(0.0, min(1.0, raw_score))

    feedback = _feedback_for(
        case=case,
        verdict_found=verdict_found,
        verdict_correct=verdict_correct,
        missed=tuple(missed),
        wrongly_flagged=tuple(wrongly_flagged),
    )

    return ScoredRollout(
        score=score,
        verdict_found=verdict_found,
        verdict_correct=verdict_correct,
        caught_categories=tuple(caught),
        missed_categories=tuple(missed),
        wrongly_flagged_categories=tuple(wrongly_flagged),
        feedback=feedback,
    )
