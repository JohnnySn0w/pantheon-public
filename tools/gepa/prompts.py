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

OUTPUT_CONTRACT = (
    "Respond as the reviewer would: open with exactly one of APPROVE, REBOUND, "
    "or ESCALATE, then your reasoning. End with one line: 'Smoke loop: green | red' "
    "(if no smoke loop applies to this material, write 'Smoke loop: n/a')."
)


def build_agents_persona_payload(candidate_text: str, *, agent_name: str = "reviewer") -> str:
    """Build the `--agents` JSON payload string embedding the candidate persona verbatim."""
    from tools.gepa.claude_executor import build_agents_json

    return build_agents_json(candidate_text, agent_name=agent_name)


def build_case_task_prompt(case: Case, *, diff_text: str | None, fixed_skills_context: str) -> str:
    """Build the executor's task prompt for one rollout of `case`.

    `diff_text` is the resolved diff (from `case_loader.resolve_diff`), or
    None for a narrative-only case (no resolvable sha) — matching the
    eval-set README's documented shape ("cases with diff_ref: null are
    narrative-only: the ask_summary plus the ticket's own transition-log
    text... is the artifact under review"). This function does not itself
    read `source_path` (that's a caller concern if narrative text beyond
    ask_summary is desired); it only guarantees `ask_summary` is always
    present so the task is never empty.
    """
    sections = [
        "You are reviewing the following change/decision as the project's reviewer persona.",
        "",
        f"## What is under review\n\n{case.review_material.ask_summary}",
    ]
    if diff_text is not None:
        sections.append(f"## Diff\n\n```\n{diff_text}\n```")
    else:
        sections.append(
            "## Diff\n\nNo resolvable diff for this case — review the described change on its "
            "narrative merits (ask_summary above)."
        )
    if fixed_skills_context:
        sections.append(f"## Project discipline context (fixed, not part of what you're evaluating)\n\n{fixed_skills_context}")
    sections.append(f"## Output contract\n\n{OUTPUT_CONTRACT}")
    return "\n\n".join(sections)
