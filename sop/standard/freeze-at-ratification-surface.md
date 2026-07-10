# Freeze the branch the moment its artifact is surfaced for ratification

**Status:** Adopted 2026-06-16 by Orchestrator (HO) — autonomous per msg-3733 (below-goal-level dispatch methodology; ratify-ask retired). Promoted from `lessons/2026-06-11-freeze-at-ratification-surface.md` (T-1196, ORG-012 lesson→SOP) — recurrence bar met: 3 HEAD-desync incidents in one arc (echo-arc-w24: T-1075 ×2, T-1067 ×1; ledger -166/-168/-172). The operator override-available.

When a worker's artifact (ADR, ratifiable doc, review target) goes up for ratification/adoption, the branch it lives on MUST freeze — or HEAD silently desyncs from the commit-under-review and the relay tier (or the operator) ends up evaluating bytes the branch no longer carries. This is the orchestrator-tier (HO + project-orchestrators) discipline that prevents that desync. It binds from dispatch one of every team arc that will surface an artifact upward.

## The failure shape

A worker keeps committing to a branch *after* its artifact has been surfaced upward. HEAD desyncs from the commit-under-review; the ratification is being evaluated against stale bytes. Under flaky message transport this compounds — stale "author/revise" instructions keep arriving and a conscientious worker keeps executing them (see `bugs/team-pattern-sendmessage-intermittent-wake-failure.md`, queue-replay addendum).

## The discipline (two halves — both required)

### 1. Dispatcher half — declare the freeze explicitly

The moment an artifact goes up for ratification/adoption, send the worker an explicit **freeze** on that branch/worktree: *"zero commits until the verdict returns through me."* Do NOT rely on the worker inferring it from "I sent the package" — the inference gap is the bug.

### 2. Relay half — cite HEAD, verify bytes

- Every ratification package cites **HEAD-at-package-send** (the exact commit SHA).
- The relay tier verifies the committed bytes match the package summary **before relaying** (the T-1075 fork was caught exactly this way).
- On verdict-return, **re-verify HEAD still equals the cited commit** before lifting the freeze into implementation. If HEAD moved, the verdict may not apply to current bytes — reconcile before proceeding.

## Sibling discipline — adjudicate once, against verified facts, then lock hard

The arc's design flip-flop happened because a lock was reversed on an *unverified* premise (a dangling-reference hazard that ground-truth code reading disproved). Under crossed/stale messaging:

- **Verify the load-bearing fact in source before locking** — don't lock or reverse a lock on a message-asserted premise.
- **Treat stale inbound instructions as suspect, not authoritative** — especially ones that predate an adjudication.
- **Once adjudicated on verified facts, do not re-litigate** on messages that predate the adjudication. (Pairs with `feedback_isolating_test_before_root_cause_claim` — ground-truth over narrative.)

## Where this worked

T-1067's cycle applied both halves cleanly: freeze declared at surface; HEAD cited; the one deviation (ADR-222 §6) was flagged openly, verified at two tiers, and re-confirmed by the adopter rather than silently absorbed. The pattern holds — apply it from dispatch one.

## Cross-references

- **Source lesson:** `lessons/2026-06-11-freeze-at-ratification-surface.md` (the three-incident evidence).
- **Sibling dispatch SOPs:** `team-orchestrator-dispatch-checklist.md`, `plan-first-dispatch.md` — the dispatch flow this freeze-discipline overlays (neither carried a freeze clause before this SOP).
- **Bug:** `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` — the flaky-transport / stale-instruction-replay failure that makes the freeze load-bearing.
- **Principles + directives:** P4 (traceability — HEAD-cite makes the ratified bytes unambiguous); D6 (plan-first + deviation-FLAG handling — the deviation path the T-1067 cycle used); D7 (decision-flow / dispatch).

## Authoring discipline

Hand-curated per D2 (HO authors). Refresh if a fourth desync surfaces a half this doesn't cover.
