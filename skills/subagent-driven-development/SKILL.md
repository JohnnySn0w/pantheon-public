---
name: subagent-driven-development
description: Use when delegating implementation, review, research, or orchestration work to subagents. Covers brief design (self-contained context, scope, acceptance), failure-mode handling (timeout, drift, hallucination, brief-mismatch REBOUND cycles), synthesis across N subagent outputs, and the 5-phase signatures-first sequence. Vendor-forked from obra/superpowers, adapted to Pantheon's 3-tier dispatch chain (HO ↔ project-orchestrator ↔ worker). Composes with D6 (plan-first), D7 (decision flow + 3-REBOUND budget + chain-of-justification), D11 (third-party surface verification), the dispatching-parallel-agents skill (when-to-parallelize), the verification-before-completion skill (acceptance-gate discipline).
---

# Subagent-driven development

**Status:** **Ratified 2026-05-27 by the operator.** Authored 2026-05-27 by Orchestrator (Orchestrator). — vendor-forked from [obra/superpowers — subagent-driven-development](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/SKILL.md) and adapted to Pantheon's vocabulary + 3-tier dispatch chain. Closes T-0408.

**HITL:** Authored; awaiting the operator ratification.

**Trigger fired (concrete evidence):** T-0408's original deferral required "any subagent dispatch that fails in a way the discipline would have caught." The 2026-05-26 Echo arc supplied multiple instances:

- **T-0603** — implementer hand-invented test fixtures that mirrored the wrong wire shape (Speechmatics speaker-id schema). Tests passed against the wrong schema, hiding the bug until first live wet-run. D11's test-fixture corollary ("fixtures MUST be spec-derived or captured from real API; NEVER hand-invented") was effective 2026-05-22; T-0603 implementer didn't apply it. Brief-design discipline would have caught this: an explicit "fixtures must be captured-from-real-API" requirement in the brief.
- **T-0603 reviewer** — noticed a stale-comment-vs-code contradiction, but PUNTED to a follow-up ticket (T-0609) instead of REBOUNDing. Brief-design discipline would have specified: reviewer's response to outbound-API boundary uncertainty MUST be APPROVE / REBOUND / ESCALATE; "punt to follow-up ticket" is not on the menu.
- **T-0616 (ticket vs reality)** — ticket framing was symptom-level ("entity_id missing from prompt"). Live HA-MCP probe revealed deeper architecture (HA-MCP tools take name/area/floor/domain, not entity_id at all). The implementer corrected the framing via D11 spec-verification. Discipline would have led with "probe the API first" rather than relying on implementer initiative.

## Overview

Subagents have isolated context and finite attention. The dispatcher (HO or project-orchestrator) controls the subagent through one thing: **the brief**. The brief determines whether the subagent will succeed.

This skill codifies the brief-design pattern + the dispatch lifecycle + failure-mode handling.

**Core principle:** the dispatcher pays for what they don't specify. Subagent failures are usually brief failures — the discipline lives at the dispatch boundary, not in the subagent.

## Pantheon's subagent personas

| Persona | Purpose | Tools | When to use |
|---|---|---|---|
| **implementer** | Make a focused code change. Plan-first; TDD; smoke must pass green. | Read, Edit, Write, Bash, Grep, Glob, TodoWrite | Most code-writing work. Default to **sonnet** (matches the persona frontmatter); swap to opus if the current round of skill changes doesn't pan out for sonnet models. |
| **reviewer** | Read-only review of an implementer's diff. APPROVE / REBOUND / ESCALATE. | Read, Grep, Glob, Bash, TodoWrite | After every implementer dispatch, before HO sees the diff. |
| **researcher** | Single-topic research dispatch; produces structured markdown report. | Read, Write, WebFetch, WebSearch, Bash, Grep, Glob, TodoWrite | Cron'd watch topics; ad-hoc deep-dives; specific URL/source investigations. |
| **project-orchestrator** | Per-project orchestrator. Dispatches implementer + reviewer within their own context. | Read, Edit, Write, Bash, Grep, Glob, TodoWrite, Agent | Multi-step arcs spanning multiple worker dispatches. |

For full persona definitions, read `/home/claude/.claude/agents/<persona>.md` (or the source in `claude-config/agents/`).

## Brief design — the load-bearing artifact

A subagent sees ONLY the brief plus the project's auto-loaded skills (e.g., `pantheon-org-discipline`). They do NOT see the dispatcher's conversation. Every brief must be self-contained.

### The required-content checklist

Every brief MUST include:

1. **Identity + branch / worktree pre-flight.** Where they're working. What branch. How to verify they're in the right place.
2. **Why this work exists** (one paragraph). Cross-references to predecessors, sibling tickets, concrete failure-evidence.
3. **What to change** (scope). File list. Specific line / function / struct names. Out-of-scope explicitly named.
4. **Constraints.** Minimum-touch; what NOT to refactor; hand-curated docs untouched; HERMES escape rule.
5. **Plan-first directive** (D6). Plan returned as first message; HO approves via SendMessage; no code before approval.
6. **DECISIONS requirement** (chain-of-justification). Every non-obvious call → `justifies-via <P/D-id>`. Memory-pin shorthand (`feedback_*`) is NOT a substitute for P/D citation — pins compose with but don't replace ratified rules.
7. **Acceptance bar** (D11 if applicable). Smoke loop literal command. Wet-run probe if outbound-API. Concrete evidence-format the report must include.
8. **5-phase signatures-first sequence** (msg_2371; ratified 2026-05-26). Plan → signatures+docstrings → tests → code → smoke+wet-run+ship. Surface phases in the report even if collapsed for small changes.
9. **Branch convention.** `agent/<task>` per the HO dispatch-discipline pin in `~/CLAUDE.md`. Workers commit on the branch; **do not push**. HO pushes + opens PR + admin-merges.
10. **3-REBOUND budget reference** (D7). If reviewer REBOUNDs 3× on the same ticket, surface to HO; the brief or the implementer-scope is wrong.

### Brief anti-patterns (Pantheon-specific)

| Anti-pattern | Why it fails | Fix |
|---|---|---|
| "Fix the bug" (no scope) | Subagent expands scope unpredictably | Specific files + specific symptoms + explicit out-of-scope list |
| References dispatcher's prior conversation | Subagent can't see it | Quote the relevant context in the brief itself |
| No acceptance bar | Subagent doesn't know when "done" | Concrete smoke loop + wet-run probe + report format |
| No DECISIONS requirement | Subagent buries contestable choices in prose | Explicit `DECISIONS section, justifies-via P/D-id per item` |
| No plan-first directive | Subagent rolls past your approval gate | Explicit "return plan; await SendMessage approval; no code before" |
| Hand-invented fixture spec | Brief asks subagent to invent test data | "Fixtures MUST be derived from spec OR captured from real API; never invented" (D11 corollary; T-0603 lesson) |
| No D-id enumeration | Subagent doesn't know which directives apply | Cite the specific D-ids that govern this dispatch (T-0611 future work on this pattern) |

## The dispatch lifecycle

For a single implementer dispatch:

### 1. Pre-flight (HO does this)

- Pre-stage the worktree: `git worktree add /tmp/<repo>-T-NNNN -b agent/T-NNNN origin/main`. Per the `agent/<task>` branch convention.
- Copy any per-machine dependencies into the worktree (e.g., echo-prismatic's `vendor/` ONNX runtime dir — pre-copying prevents reviewer's smoke loop from false-failing on missing deps, per T-0614 reviewer feedback).
- Claim the ticket: `ticket.py claim T-NNNN`.

### 2. Brief construction (HO does this)

Per the brief-design checklist above. Include ALL 10 required items. For trivial work, phases 2/3/4 of the 5-phase sequence can collapse inline; for non-trivial work, surface them as report sections.

### 3. Plan-tier (subagent → HO via final message)

Subagent returns plan as final message. HO reads:

- Are the proposed files in scope?
- Do the DECISIONS justifies-via cite real P/D-ids accurately?
- Are open questions surfaced explicitly?
- For 5-phase work: does the signatures phase reflect the API shape we want?

**HO responds via SendMessage:** "approve" + any open-question answers + any plan refinements. Do NOT let the subagent roll past plan-tier without explicit approval.

### 4. Implementation (subagent → HO via final message + commit)

Subagent fills phases 2/3/4 (signatures → tests → code), runs smoke, runs wet-run if applicable, commits, returns report.

### 5. Reviewer dispatch (HO → reviewer subagent)

`cd` to the worktree before dispatching reviewer (cwd inheritance per the implementer-dispatch feedback memory). Brief includes:

- The 7-8 acceptance checks (smoke green; wet-run evidence; code inspection; DECISIONS verification; scope discipline; HERMES; merge surface; methodology feedback if relevant)
- The wet-run evidence that HO has already verified (so reviewer doesn't redo it; just confirms the artifact exists)
- 3-REBOUND budget tracking ("this is round N")

Reviewer returns: APPROVE / REBOUND / ESCALATE. Each with concrete evidence.

### 6. Merge (HO does this)

After APPROVE: push, open PR (using `gh pr create --head agent/<task> --base main`), admin-merge with squash. Clean up worktree + local branch. Pull canonical main. If the change requires daemon restart, rebuild canonical binary + restart + verify with debug log lines.

## Subagent status handling

| Status | Action |
|---|---|
| **Plan complete** | HO reviews + approves via SendMessage. Don't let subagent self-roll. |
| **DONE + smoke green + wet-run evidence** | Proceed to reviewer dispatch. |
| **DONE_WITH_CONCERNS** | Read the concerns. If they're correctness-tier (e.g., "I'm not sure this handles X edge case"), address before reviewer. |
| **NEEDS_CONTEXT** | Provide missing info via SendMessage; subagent continues. |
| **BLOCKED** | Assess: brief wrong, scope wrong, environment wrong? Re-brief or escalate. |
| **3rd REBOUND on same ticket** | Brief is wrong OR implementer mis-scoped. Inspect + re-brief + re-dispatch (possibly fresh subagent). Don't grind. |

## Failure-mode catalog (Pantheon-recurrent)

| Mode | Symptom | Cause | Mitigation |
|---|---|---|---|
| **Hand-invented fixtures** | Tests pass against assumed schema; real API rejects | Brief didn't require spec-derived/captured fixtures | D11 corollary in brief; cite as load-bearing |
| **Symptomatic ticket framing** | Subagent fixes the surface symptom; deeper architecture problem persists | Brief framed the problem at the wrong tier | Encourage live spec-probe in plan-tier; D11 |
| **Punted boundary uncertainty** | Reviewer files "verify later" ticket instead of REBOUND | Reviewer's tier confusion (file vs block) | Brief explicitly: APPROVE/REBOUND/ESCALATE are the only options; punt is retired (ORG-023 / D11) |
| **CWD inheritance halt** | Subagent's pre-flight check refuses to operate on a canonical main checkout | Dispatcher's last Bash cwd was the canonical worktree | HO `cd` to neutral or worktree dir before Agent dispatch (feedback memory `feedback_implementer_dispatch_isolation_worktree_pattern.md`) |
| **Phantom-deletion REBOUND** | Reviewer REBOUNDs on "deleted files" that main moved during the cycle | Reviewer diffed against current main, not merge-base | HO merges origin/main into agent branch pre-review OR reviewer checks merge-base diff |
| **Stale-main false negatives** | "Tests fail" reports against pre-staged worktrees | Worktree pre-staging didn't copy per-machine vendor deps | HO copies vendor/ when pre-staging (T-0614 reviewer note) |
| **Memory-pin citation in DECISIONS** | Subagent cites `feedback_*` shorthand instead of P/D | Subagent treats memory pins as ratified rules | Brief: P/D citations only; memory pins compose, don't substitute (ORG-015) |
| **Brief-mismatch REBOUND budget burn** | Multiple REBOUND rounds on the same ticket | Brief didn't specify acceptance clearly | Re-brief at round 2; re-dispatch fresh at round 3 (D7 escalation criteria) |

## Synthesis across N subagent outputs

For dispatcher consuming N parallel subagent reports:

1. **Don't duplicate.** Each subagent's output is independent. Don't paraphrase or re-summarize what's already in the report.
2. **Cross-check overlaps.** Where two reports cover related ground, look for contradictions. If they exist, dig.
3. **Lift load-bearing findings to durable artifacts.** Move from subagent transcript → ledger entry / notes file / discipline doc. Subagent transcripts decay; durable artifacts survive.
4. **Verification before claiming "all dispatches landed"** (per the `verification-before-completion` skill). Smoke loop ran on the COMPOSED state, not just per-dispatch.

## Composition with existing P/D + skills

- **D6** (plan-first protocol): per-dispatch; brief MUST require it; HO approves before code.
- **D7** (decision flow): the 5 bubble-up criteria + 3-REBOUND budget; chain-of-justification propagation at every dispatch boundary.
- **D11** (third-party surface verification): wet-run probes + spec-derived fixtures; the test-fixture corollary is load-bearing here.
- **`dispatching-parallel-agents` skill** (sibling, T-0407 / T-0621): when to parallelize vs serialize. This skill = HOW to brief one subagent; sibling skill = WHEN to fan out.
- **`verification-before-completion` skill** (T-0406 / T-0620): the iron law of fresh evidence applies to subagent "done" claims too. Don't trust dispatch-success acks blindly — verify the artifact.
- **P5** (roles don't hop): dispatcher decides scope + acceptance; subagent decides implementation choices within that scope. Workers don't decide architecture; orchestrators don't decide implementation details.
- **ORG-019** (block discipline): subagent dispatches that depend on prior ticket resolutions use `depends_on` in frontmatter, not separate blocked-state directories.
- **The 5-phase signatures-first methodology** (msg_2371): the sequence subagents follow within a single dispatch.

## Divergence from obra/superpowers

| Upstream rule | Pantheon adaptation | Why |
|---|---|---|
| "Never dispatch multiple implementers in parallel" | Parallel implementer dispatches OK when domains are independent (no shared files, no dep) | The upstream rule covers a single-plan-many-tasks model where tasks share state; Pantheon's fan-out covers independent tickets. The `dispatching-parallel-agents` skill is the discriminator. |
| Two-stage review (spec then quality) | Single reviewer covers both (or REBOUND across multiple rounds) | Pantheon's reviewer persona is generalist; spec/quality split adds tier complexity not yet justified. Possible future: split if quality REBOUNDs dominate. |
| "Do not pause to check in with your human partner between tasks" | HO pauses at plan-tier for SendMessage approval; D6 plan-first is non-negotiable | Pantheon's escalation tier is the operator (HO above the autonomy bar); subagents bubble plan-tier decisions to HO who decides whether to escalate further. |
| References sibling skill files via `superpowers:<name>` | Pantheon refs by ticket / skill name / P/D-id | Pantheon's skill / discipline graph is org-defined; superpowers references decay across vendor-forks. |

## Bottom line

The brief is the artifact. Subagent success or failure traces back to brief design more than to subagent capability. Spend the design cost up-front; pay the REBOUND cost later otherwise.

When in doubt: spec-derived fixtures, explicit DECISIONS, plan-first, wet-run gates. The discipline accumulates across dispatches; the failure modes recur.
