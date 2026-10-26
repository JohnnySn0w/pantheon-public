"""Prompt construction for GEPA reviewer rollouts (T-2809 Phase 2).

Two prompt surfaces, deliberately kept separate:

- `build_agents_persona_payload(candidate_text)` — the CANDIDATE side. It is
  fine, expected, and required for this to embed the candidate persona text
  verbatim; that text is what GEPA is optimizing, not a gold answer.
- `build_case_task_prompt(case, ...)` — the TASK side, shown to the executor
  as the user turn. Hard contract: this NEVER contains
  `case.expected.findings[].description` or
  `case.expected.must_not_flag[].description` text. Handing the executor the
  answer key would make every rollout trivially "correct" and the eval
  meaningless. Only `ask_summary`, the resolved diff (or a narrative
  fallback), and the fixed skills context go in.

No `gepa` import (see package docstring).
"""

from __future__ import annotations

from tools.gepa.case_loader import Case
from tools.ticket import REPO_ROOT

OUTPUT_CONTRACT = (
    "Respond as the reviewer would: open with exactly one of APPROVE, REBOUND, "
    "or ESCALATE, then your reasoning. End with one line: 'Smoke loop: green | red' "
    "(if no smoke loop applies to this material, write 'Smoke loop: n/a')."
)


def build_agents_persona_payload(candidate_text: str, *, agent_name: str = "reviewer") -> str:
    """Build the `--agents` JSON payload string embedding the candidate persona verbatim."""
    from tools.gepa.claude_executor import build_agents_json

    return build_agents_json(candidate_text, agent_name=agent_name)


def _load_narrative(case: Case) -> str | None:
    """Read the ticket source_path's transition-log narrative for null-diff cases.

    T-3837 v2: null-diff cases (escalate-class, negative_invalid_rebound, some
    clean_approve) previously had only `ask_summary` — insufficient signal for
    the reviewer to distinguish escalate from rebound. Reading source_path
    (relative to REPO_ROOT) gives the executor the same material a human
    reviewer would have. Returns None on any read failure (path outside repo,
    missing file, decode error) — narrative is best-effort, never fatal.
    """
    if not case.source_path:
        return None
    resolved = (REPO_ROOT / case.source_path).resolve()
    try:
        repo_root_resolved = REPO_ROOT.resolve()
        resolved.relative_to(repo_root_resolved)
    except ValueError:
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError:
        return None


def build_case_task_prompt(case: Case, *, diff_text: str | None, fixed_skills_context: str) -> str:
    """Build the executor's task prompt for one rollout of `case`.

    `diff_text` is the resolved diff (from `case_loader.resolve_diff`), or
    None for a narrative-only case (no resolvable sha) — matching the
    eval-set README's documented shape ("cases with diff_ref: null are
    narrative-only: the ask_summary plus the ticket's own transition-log
    text... is the artifact under review"). When `diff_text is None`, the
    ticket's `source_path` is read and included as `## Narrative` so the
    executor sees the transition log a human reviewer would have.
    """
    sections = [
        "You are reviewing the following change/decision as the project's reviewer persona.",
        "",
        f"## What is under review\n\n{case.review_material.ask_summary}",
    ]
    if diff_text is not None:
        sections.append(f"## Diff\n\n```\n{diff_text}\n```")
    else:
        narrative = _load_narrative(case)
        if narrative is not None:
            sections.append(f"## Narrative\n\n{narrative}")
        else:
            sections.append(
                "## Diff\n\nNo resolvable diff for this case — review the described change on its "
                "narrative merits (ask_summary above)."
            )
    if fixed_skills_context:
        sections.append(f"## Project discipline context (fixed, not part of what you're evaluating)\n\n{fixed_skills_context}")
    sections.append(f"## Output contract\n\n{OUTPUT_CONTRACT}")
    return "\n\n".join(sections)
