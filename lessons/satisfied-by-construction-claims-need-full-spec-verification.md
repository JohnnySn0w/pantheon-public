# Lesson — "satisfied-by-construction" claims need full-spec verification, and close-dispositions need review

**Surfaced:** 2026-06-24, Echo Phase-2 #4 (T-1298 → T-1304). Filed by Orchestrator (HO). Caught by the operator's pre-ratification double-check.

## What happened

Echo Phase-2 workstream #4 was "execution-time permission-cache revalidation (ADR-008 ext)." The orchestrator ran it as a verify-then-decide and concluded **"satisfied by construction — no build needed"** (per-utterance fresh `authorize`, no stale cache to revalidate), drafting a ratify-gated ADR annotation to that effect. The reasoning cited ADR-008's MVP rationale that the residual mid-utterance race is "incredibly rare" and accepted.

The operator, before ratifying, asked: *"double check we didn't have further discussion on it at some point."* Reading ADR-008 **in full** revealed an explicit **"Phase 2 — tool-time permission cache check"** section that *un-defers* the execute-time re-resolution as a real build (re-resolve the current tier at execute → `PermissionRevoked`, to close the tier-change-mid-utterance race). The roadmap's #4 wording was lifted directly from ADR-008's Phase-2 deferred list. So "satisfied by construction" **overclaimed**: invalidation-on-change *was* done, but the execute-time re-resolution ADR-008 Phase-2 specified was *not* built. The operator chose to build it (→ T-1304).

## Root cause

The disposition read ADR-008's **MVP / context rationale** ("very rare, accept it") and the **current code**, but missed the spec's own **forward-looking Phase-N section** that had already decided to un-defer the work. A spec can *accept* something at MVP and *un-defer* it later in the same document — reading only the MVP framing inverts the intent.

Compounding it: the #4 close was a **solo orchestrator disposition that never went through a reviewer**. Build tickets in the same arc (#2, #7) each got an adversarial reviewer pass; the "close-not-build" disposition did not. Closing-as-satisfied is exactly where a shallow read hides — and there was no second read to catch it. It took the human's double-check.

## The lesson (two parts)

1. **A "satisfied-by-construction / already-done / no-build-needed" claim against a spec must be verified against the spec's OWN forward-looking sections** (Phase-N, "deferred to", "future work", "revisit when"), not just the current code + the MVP/context rationale. Read the whole spec section before declaring a spec'd workstream already-met. The MVP's "we accept X for now" is frequently un-deferred later in the same doc.

2. **Close-not-build dispositions need the same review gate as builds.** A verify-then-decide that *closes* a workstream is a one-sided, easy-to-rationalize call with no artifact for a reviewer to check — so it tends to skip review. Route it through a second read anyway: a reviewer (or HO) reads the disposition against the full spec before it's surfaced for ratification. Do not let "we decided NOT to build it" bypass the adversarial check that "we built it" gets.

## Process change

- Orchestrators: when a workstream resolves as "satisfied-by-construction / close-not-build," dispatch a one-shot reviewer (or escalate to HO for a second read) against the full spec before surfacing the ratify-gated annotation. Treat the close-disposition as a reviewable artifact.
- HO: before routing any agent's "already-satisfied" finding to the human for ratification, read the governing spec's forward-looking sections yourself. (Here, the operator's instinct supplied the catch HO should have supplied first.)

Related: the same verify-don't-assume discipline recurred one tier down on T-1304 — the implementer *assumed* production `authorize()` returns a generic error for a removed speaker; verifying against `speaker_auth.rs` showed it returns a typed `UtteranceDropped`. That one was caught in plan-review (the review gate working); the #4 close had no such gate. The gap is structural: close-dispositions lacked the gate builds have.

**Promoted as load-bearing** to [`sop/standard/pre-implementation-reviewer-discipline.md`](../sop/standard/pre-implementation-reviewer-discipline.md) §Close-not-build dispositions are reviewable artifacts too (extended 2026-06-30 by Orchestrator (HO) under ORG-012 lesson→SOP, principles-as-ratification; T-1464) — the structural gap (close-dispositions lack the build review gate) is now closed in the reviewer-discipline SOP. This lesson stays as the originating evidence.
