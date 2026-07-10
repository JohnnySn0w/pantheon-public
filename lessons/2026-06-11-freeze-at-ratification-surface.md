# Freeze the branch the moment its artifact is surfaced for ratification

**Date:** 2026-06-11 · **Source:** echo-arc-w24 team cycle (T-1075 ×2, T-1067 ×1 — three HEAD-desync incidents in one arc) · **Tier:** orchestrator discipline (HO + project-orchestrators) · **Ledger:** -166/-168/-172

## The failure shape

A worker keeps committing to a branch *after* its artifact (ADR, ratifiable doc, review target) has been surfaced upward for ratification/adoption. HEAD silently desyncs from the commit-under-review; the relay tier (or the operator) is then evaluating bytes the branch no longer carries. Under flaky message transport this compounds: stale "author/revise" instructions keep arriving and a conscientious worker keeps executing them (see `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` — queue-replay addendum).

## The discipline (two halves, both required)

1. **Dispatcher half:** the moment an artifact goes up for ratification/adoption, send the worker an explicit **freeze** on that branch/worktree ("zero commits until the verdict returns through me"). Don't rely on the worker inferring it from "I sent the package."
2. **Relay half:** every ratification package cites **HEAD-at-package-send**, and the relay tier verifies the committed bytes match the package summary *before* relaying (the T-1075 fork was caught exactly this way). On verdict-return, re-verify HEAD still equals the cited commit before lifting the freeze into implementation.

## Sibling lesson: adjudicate once, against verified facts, then lock hard

The arc's design flip-flop happened because a lock was reversed on an *unverified* premise (a dangling-j17-reference hazard that ground-truth code reading disproved — partials are never persisted). Under crossed/stale messaging: verify the load-bearing fact in source before locking; treat stale inbound instructions as suspect, not authoritative; once adjudicated on verified facts, do not re-litigate on messages that predate the adjudication.

## Where this worked by arc-end

T-1067's cycle applied both halves cleanly (freeze declared at surface; HEAD cited; the one deviation — ADR-222 §6's coherence-violating From impl — was flagged openly, verified at two tiers, and re-confirmed by the adopter rather than silently absorbed). The pattern holds; apply it from dispatch one of the next team arc.
