---
aliases: [ORG-102]
id: ORG-102
title: "Fleet quantification: prove the guidance works and measure delivery, from automated exhaust only"
status: "Ratified 2026-10-09 by the operator (TG 11575, after summary TG 11570). Requested in TG 11558-11566. Reversible via amend/supersede."
date: 2026-10-09
author: Orchestrator (head_engineering_orchestrator)
steward: orchestrator
appetite: 6
riskiest_assumption: "That reversal and override events can be detected reliably from git, ticket and ledger exhaust, without agents hand-tagging them. If most reversals are silent (a later PR quietly undoes an earlier call with no supersedes link), H1 undercounts failures and reads as falsely good. The weekly blind grade (H2) is the cross-check that doesn't depend on that detection."
build_disposition: needs-build
scoping_ticket: T-4780
relates: ORG-001, ORG-012, ORG-034, ORG-089, ORG-100
ratifiable_files: [pantheon-config:architecture/adr/ORG-102-fleet-quantification-judgment-calls-and-delivery.md]
---

# ORG-102: Fleet quantification, judgment calls and delivery

**Status:** Ratified 2026-10-09 by the operator (TG 11575). Requested in TG 11558-11566.

## Intent

The operator can see, with numbers, (a) whether the principles and directives produce the judgment he would make, and (b) what the fleet delivers on larger software projects. Serves D1 (his bandwidth: trust from evidence, not from reading transcripts) and P4 (traceability).

## Context

- **What exists.** The ledger records orchestrator-tier calls with `justifies_via` (ORG-001). Workers and reviewers emit DECISIONS sections (chain of justification, ORG-034), but these are prose, scattered across ticket notes, reports and PR bodies, and nothing aggregates them. GitHub, ticket transition logs and the ledger already hold the delivery history: PRs, merges, REBOUND rounds, reverts.
- **Lesson from prior art.** T-0193 added a hand-filled `metrics` field to ledger close entries (`tools/cycle_stats.py`). It decayed: zero entries in W40-W41. **Manual collection does not survive.** Every number here must come from exhaust the fleet already emits as a side effect of working.
- **the operator's two constraints (TG 11566):** it must *prove something*, and collection must be automated.

## Decision

1. **Hypotheses first.** The system exists to test these four. Each has a stated falsifier, and the weekly digest reports pass, fail or insufficient-n for each, not just charts.
   - **H1 (calls hold):** calls that cite a principle or directive are reversed less often than uncited calls, and less than 10% overall within 14 days. *Falsified if* cited and uncited reversal rates are indistinguishable (overlapping 90% Wilson intervals at n ≥ 50 each), or the overall rate exceeds 10%. Reported per cited P/D, so a principle whose calls keep failing is named.
   - **H2 (the fleet decides like the operator):** in a weekly blind sample, the operator agrees with at least 80% of calls. *Falsified if* the lower bound of the 90% Wilson interval stays below 70% for 4 consecutive weeks.
   - **H3 (autonomy grows without quality loss):** operator-touches per shipped arc trend down over 8 weeks while change-failure rate (H4) does not rise. *Falsified if* touches are flat or rising, or if falling touches coincide with rising failure (autonomy bought with quality).
   - **H4 (capability):** for multi-PR arcs (3 or more merged PRs), change-failure rate (reverts, hotfix PRs, rollbacks per merged PR) stays at or below that of single-PR work, and lead time scales sub-linearly with arc size. *Falsified if* failure rate climbs with arc size. That would mean the fleet is good at small changes but not at larger projects.
2. **Structured DECISIONS line.** The one format change, enforced, not hand-counted. A DECISIONS item is one line: `DECISION: <call> | serves: <intent or "goal-neutral maintenance"> | via: <P/D/ORG/ADR/T ids, each optionally followed by a section qualifier, e.g. ORG-052§3 or ORG-052 §3/§5; or a ruling cite operator:tg:<message number> or ho:mail:<mailbox message id>> | ref: <one or more of T-NNNN, repo#PR, or a 7-40 hex commit sha, comma-separated>`. A qualifier is kept as data and the base id is what is counted; a ruling cite counts as the class "ruling" (amended 2026-11-09, see §Amendment 2026-11-09). Prose explanation may follow on indented lines. ticket.py annotate/resolve and the PR-body check WARN on DECISIONS sections with no parseable line; later they REFUSE (WARN-first, same rollout pattern as ORG-021). The reviewer persona's existing DECISIONS pre-flight checks the format.
3. **Collectors read exhaust only.** A daily timer runs read-only collectors that write one JSONL feed under `/var/lib/pantheon/quant/`:
   - **calls:** structured DECISIONS lines from ticket transition logs, PR bodies (GitHub REST) and ledger `justifies_via`;
   - **outcomes per call:** *reversed* when a later commit reverts the ref'd PR, the ticket reopens, a REBOUND cites it, a ledger entry or ADR supersedes it, or a rollback names it; *overridden* when the operator's Telegram ruling (recorded on the ticket as `the operator TG <id>`) contradicts it; otherwise *held* at 14 days;
   - **delivery:** merged PRs, lead time (ticket filed → merged; PR open → merged), REBOUND rounds per ticket, reverts and hotfix PRs, and arc size (PRs per parent ticket);
   - **operator-touches:** Telegram ruling citations per arc (`the operator TG` mentions on the arc's tickets);
   - **cost:** weekly quota % from `fleet_usage_gate` readings, divided by merged PRs.

   No agent hand-fills a metric. A metric that can't be derived from exhaust is dropped or deferred, not made manual.
4. **The one human input is the operator's weekly grade, made cheap.** Once a week, about 10 randomly sampled calls, stratified across agents, are sent as a Telegram message: the situation and the call, with the citation and reasoning hidden. The operator replies with a string such as `yyny…`. The reply is parsed automatically. This is H2's instrument, and it is the only number that measures "decides like me" directly instead of by proxy.
5. **Weekly digest.** An on-VPS markdown file plus a short Telegram pointer: H1-H4 verdicts (pass, fail or insufficient-n) with intervals; never-cited P/Ds (dead guidance); the top uncited calls; delivery trends. The digest states plainly that H1, H3 and H4 are observational (correlation, not causation). H2 is the closest thing to a direct test.

## Responsibilities ([[ORG-100]] roles)

| Responsibility | Role | Seat |
|---|---|---|
| This ADR; hypothesis definitions; digest wording | `head_engineering_orchestrator` | Orchestrator |
| Build: collectors, DECISIONS-format WARN gate, sampler, digest | `engineering_orchestrator` | Mason (on codex) |
| Daily timer hosting, if not on the build seat | `head_system_orchestrator` | Recovery |
| Weekly blind grade | Operator | the operator |

## Non-goals

- Not a performance review of individual agents. Per-agent breakdowns exist to find where guidance fails, not to rank seats.
- Not a causal claim. A/B testing guidance on live work is out of scope.
- Not a replacement for the ledger or tickets. The feed is derived from them and can be rebuilt from them at any time.

## Alternatives considered

- **Hand-filled metric fields** (T-0193 shape). Rejected: they decayed to zero within weeks.
- **LLM-graded agreement instead of the operator's grade.** Rejected for H2: it measures agreement with a model, not with the operator. An LLM pre-classifier may still triage reversal detection (H1) as a build detail.
- **Dashboards without hypotheses.** Rejected per TG 11566 ("it needs to prove something").

## Build sequence

1. Delivery collectors (H4 and H3 data), all from existing exhaust; backfill 60 days.
2. Structured DECISIONS line plus the WARN gate; calls collector.
3. Outcome detector (reversed, overridden, held); H1 reporting.
4. Weekly blind sampler plus reply parser (H2).
5. Weekly digest.

## Re-evaluate when

- After 8 weekly digests: are the hypotheses still the right questions, and is n sufficient?
- If H2's sample is graded fewer than 3 weeks in 4 (the one human step has failed; redesign it or drop it).
- If DECISIONS-format WARNs stay high after a month (the format is wrong, not the agents).

## Amendment 2026-11-09 — DECISIONS-line field grammar (T-4819)

Ratified by the operator via Telegram 13201 (2026-11-09). Decision item 2's field descriptions are widened, with no other decision changed:

1. `via` ids may be P/D/ORG/ADR/T ids, each optionally followed by a section qualifier (e.g. `ORG-052§3`, `ORG-052 §3/§5`). The qualifier is kept as data; the base id is counted.
2. `via` also accepts a ruling cite, `operator:tg:<message number>` or `ho:mail:<mailbox message id>`, counted as the class "ruling".
3. `ref` accepts one or more of `T-NNNN`, `repo#PR`, or a 7-40 hex commit sha, comma-separated.

Why: in the 2026-10-09..11-09 WARN month 26% of structured lines were malformed, flat, and about two-thirds of those were legitimate citations the literal grammar could not express. Enforcing the literal grammar would push seats to drop citations, a P4 traceability loss. The WARN-to-REFUSE sentence is unchanged: the flip happens only when the re-measured malformed rate is under 10% (T-6300) and this amendment is ratified (now true). Draft: mason on T-4819 (2026-11-09).
