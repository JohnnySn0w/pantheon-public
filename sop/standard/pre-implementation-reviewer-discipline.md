# Pre-implementation reviewer discipline

**Status:** Ratified 2026-05-20 by the operator (Telegram msg_1632).
**Author:** Recovery.
**Origin:** Surfaced by the operator Telegram msg_1629+1630 on 2026-05-20 — "even for one-offs, having a reviewer is good it seems" + "might be SOP?" After T-0471 (per-cron git-worktree isolation) reviewer-subagent dispatch caught 6 specific gaps in the test plan + 1 open architectural design call that would have bounced the implementer mid-flight.

## When to use this

Dispatch a reviewer subagent BEFORE the implementer subagent for any ticket that fits ONE OR MORE of these criteria:

- **Multi-file scope** — retrofit-N-files, amend-multiple-docs, cross-cutting concern that touches multiple agents' workflows
- **Structural fix** — new helper + retrofits, ADR-tier amendment, new substrate primitive
- **Non-trivial test plan** — anything requiring multiple tiers per `sop/standard/testing-depth-and-scope.md` matrix
- **External-API adapter work** — per testing-SOP Amendment A, live-integration coverage REQUIRED, easy to mis-spec
- **Cron / auto-commit script work** — per testing-SOP "Cron" row, Integ-same-process REQUIRED; race-condition fixtures easy to miss

Plus a non-build trigger: a **close-not-build / "satisfied-by-construction" disposition** against a governing spec gets the same second-read gate — see §Close-not-build dispositions are reviewable artifacts too below.

## What the reviewer does

Pre-implementation reviewer reads:
- The ticket body (Ask, Context, Test Plan, Acceptance, Out-of-scope)
- Relevant existing files the ticket touches (so reviewer can sanity-check feasibility + scope)
- The applicable testing-depth matrix entries

Returns:
- One-line verdict (APPROVE / REBOUND / APPROVE-WITH-AMENDMENTS)
- Bullet list of specific gaps + amendments needed
- Bullet list of strengths (so implementer knows what's right)
- Concrete suggested edits (paste-ready Markdown fragments where amendments apply)
- Names any "open design calls" surfaced — items that need HO/the operator architectural judgment before implementer can start, NOT implementer judgment

Reviewer brief shape (paste-ready template):

```
You are a pre-implementation reviewer for a Pantheon ticket. Your job is to
assess whether the TEST PLAN + ACCEPTANCE sections of the ticket adequately
cover the bug class it's meant to fix + the failure modes the implementation
will introduce.

**The ticket**: <path to ticket file>

**Context you'll need to read**:
1. The ticket itself
2. `sop/standard/testing-depth-and-scope.md` (matrix the test plan claims to apply)
3. <relevant existing files the ticket modifies>
4. <existing tests for those files, found via grep>

**Original trigger / bug**: <one-line summary>

**Your assessment** (be specific):
1. Bug-regression coverage — does the test plan EXPLICITLY catch the trigger?
2. Failure-mode coverage — what edge cases are missing?
3. Per-cron / per-file retrofit coverage — does each retrofit get its own regression case?
4. Tier mapping per testing-depth-and-scope.md — correctly applied? Any RECOMMENDED cells skipped without justifies-via?
5. Acceptance verifiability — concrete enough for reviewer at close to mechanically check?
6. What's MISSING — list specific gaps.

**Report format**: verdict + gaps + strengths + suggested edits. Keep ≤600 words.
Don't speculate beyond ticket + files. If genuinely ambiguous, name as "needs
HO design call" rather than answering.
```

## What the orchestrator does with the reviewer's report

1. **Fold amendments inline** into the ticket body + transition log. Don't carry "reviewer found X, will fix later" as a TODO. The whole point is implementer gets the tightened brief.
2. **Surface open design calls** — these are HO/the operator judgment, not implementer-tier. Resolve before dispatch.
3. **Log the review pass** in the ticket transition log: verdict + headline gaps + amendments-applied confirmation.
4. **Don't dispatch implementer** until reviewer pass closes (verdict APPROVE or APPROVE-WITH-AMENDMENTS with edits applied).

## When NOT to dispatch the pre-impl reviewer (the cheap-action exception)

Per `feedback_cheap_action_just_try.md` + the cost-asymmetry framing — skip the reviewer step ONLY when:

- **Single-file content edit** — typo fix, transition log append, ratify-status field flip
- **Cron output / mechanical log artifact** — ledger append, journal capture, sync-alert recording
- **Ticket of pure conversational ack** — "the operator said yes, mark resolved"

Skipping the reviewer for cycles that fit one of the "when to use" criteria above is the wrong default — the implementer cycle bounces on missing coverage cost more than the reviewer pass.

## Close-not-build dispositions are reviewable artifacts too

A verify-then-decide that **closes** a workstream as "satisfied-by-construction / already-done / no-build-needed" is a one-sided, easy-to-rationalize call with **no build artifact for a reviewer to check** — so it tends to skip the adversarial gate that build tickets get. It is exactly where a shallow spec-read hides. Give it the same gate (T-1298→T-1304, [`lessons/satisfied-by-construction-claims-need-full-spec-verification.md`](../../lessons/satisfied-by-construction-claims-need-full-spec-verification.md)):

1. **Verify the claim against the spec's OWN forward-looking sections** — not just the current code + the MVP/context rationale. A spec can *accept* something at MVP and *un-defer* it later in the same document (a "Phase-N", "deferred to", "future work", "revisit when" section); reading only the MVP framing inverts the intent. Read the whole governing spec section before declaring a spec'd workstream already-met.
2. **Route the close-disposition through a second read before ratification** — a one-shot reviewer (or HO escalation) reads the disposition against the full spec before the ratify-gated annotation is surfaced. Do not let "we decided NOT to build it" bypass the adversarial check that "we built it" gets. Treat the close-disposition as the reviewable artifact.

**HO half:** before routing any agent's "already-satisfied" finding to the operator for ratification, read the governing spec's forward-looking sections yourself. (In the founding case, the operator's instinct supplied the catch HO should have supplied first — a D1 withdrawal.)

The cheap-action exception still applies in proportion: a close that is genuinely trivial and has **no governing forward-spec** (a pure conversational-ack close, a mechanical status flip) does not need the second read. The gate is for close-as-satisfied against a *spec* that could have un-deferred the work.

## Boundary

This SOP covers **pre-implementation** reviewer dispatch (review the TICKET before code is written) **and close-not-build dispositions** (review the disposition artifact before ratification, §above). It does NOT cover:

- **Post-implementation reviewer dispatch** (review the PR after implementer ships). Existing pattern per `feedback_implementer_dispatch_isolation_worktree_pattern.md`; reviewer subagent reads diff + smoke output + DECISIONS section, returns APPROVE / REBOUND.
- **Plan-tier review by HO** (HO reads the implementer's plan READY response per D6). That's the orchestrator's own pre-flight, not a separate dispatch.

Both pre-impl reviewer + post-impl reviewer + HO's plan-tier review are independent passes. They reinforce; they don't replace each other.

## Re-evaluate when

- A cycle ships clean WITHOUT pre-impl review and the reviewer-tier value-add was clearly absent — pattern may be over-specified.
- A cycle bounces post-pre-impl-review on a gap the reviewer should have caught — reviewer brief shape needs amendment.
- Reviewer-subagent token cost becomes meaningful (>>50K per dispatch routinely) — cost/benefit changes.

## Cross-references

- `sop/standard/testing-depth-and-scope.md` — the matrix the reviewer applies
- `feedback_implementer_dispatch_isolation_worktree_pattern.md` — sibling discipline for implementer dispatch isolation
- `feedback_cheap_action_just_try.md` — P7 cost-asymmetry counterpart (cheap to dispatch reviewer; expensive to skip)
- `feedback_reviewer_step_valuable_even_for_one_offs.md` — Recovery's per-account memory pin
- T-0471 (the trigger case that surfaced this pattern)
