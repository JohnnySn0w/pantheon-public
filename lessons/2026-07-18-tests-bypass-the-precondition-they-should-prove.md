# A green test proves the tested path works — not that the tested path is the real one

**Date authored:** 2026-07-18 (distilled from the ADR-precondition deep sweep — T-1806 + T-1807/1808/1809 + T-1602, incident `2026-07-18-adr-precondition-drift-shipped-inert-features`)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015). The operator endorsed formalizing the through-line (TG 6057/6065). **Promotion candidate:** a test-methodology directive + a structural bridge fix — the persist-by-default-bridge ADR is pending the operator's ratification (TG 6066).
**Re-evaluate when:** a 5th+ instance of this family lands (the recurrence has already crossed the "harden it" threshold — see Grounding), OR the persist-by-default-bridge ADR ratifies (then this lesson's structural-fix half is discharged and it narrows to the test-methodology half).

## Observation

A ratified feature ships GREEN-but-INERT: it merges with a passing test suite, is treated as done, and does nothing in production — because its tests exercise a FAKE path that bypasses the very precondition the feature depends on. The green suite is not just failing to catch the gap; it is the *reason* nobody looks. Two collaborating halves:

1. **A precondition is assumed, not verified.** An ADR (or a caller) states "X is already persisted / implemented / guaranteed elsewhere" and builds on it without checking that the other end actually provides X. When it doesn't, the feature is inert.
2. **The test injects state past the missing precondition.** Synthetic DB rows inserted directly (never driven through the real producer), a fake event published at t+20ms (bypassing the ≥500ms real-world precondition that makes it unreachable), a wiremock that mirrors the *assumed* wire contract. The test proves the code-under-test is internally correct given its inputs — and says nothing about whether those inputs ever arrive in production.

The abstract lesson: **a ratified ADR is a claim of intent, not a guarantee of implementation; and a green test proves the tested path is correct, not that the tested path is the one production takes.** Both must be re-verified against the actual wiring, by exercising the real end-to-end path, before "done."

## Grounding

The 2026-07-18 sweep (6 auditors over the ADR set, operator-requested after T-1806) found this shape SIX times, verified at source:
- **T-1806** — the AUTHORIZED crash-durability checkpoint is published under the `auth` tap, which the j17 bridge silently drops (`mapping.rs` allowlist). ADR-245 §2.4 assumed it persisted. T-1760's reconciler tests passed via **synthetic row inserts** that never drove the bus→bridge path → shipped inert on both backends.
- **T-1807** — ADR-245 §3's domain-key idempotency upsert was never implemented; masked *only because* T-1806 keeps the reconciler from ever firing (a gap hidden behind another gap).
- **T-1602** — ADR-239's ratified duck-then-decide RESUME redesign was never built; its tests **inject a fake `vad_speech_end` at t+20ms**, bypassing the ≥500ms-silence precondition that makes RESUME arithmetically unreachable in real audio → tests green, capability inert.
- **T-1808 / T-1809** — ADR-242 / ADR-001/003 assume the j17 bridge persists `perceived_latency` / `had_speaker_resolved`; the allowlist doesn't → no history / no durable record.

This is not a new pattern — it is the Nth instance of a **recurring family** already in this corpus: [[2026-06-26-ships-green-while-incomplete]] (green ≠ complete), [[2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing]] (test *timing* masks a race — the direct sibling of the T-1602 fake-injection), [[2026-06-26-silent-no-op-wrong-subsystem-wiring]] (a wiring gap that no-ops silently — the T-1806 shape). The recurrence IS the signal: four-plus instances across months means prose vigilance ("remember to test the real path") has failed and the fix must be structural (P1).

## Design implications

Three structural fixes (the incident's §Fixes, elevated here to the pattern level):

1. **Reconciler / gated-capability / durability tests MUST drive the real producer→transport→store path, never inject past the precondition.** Synthetic-insert and fake-event-at-favorable-timing are the common mask. Candidate hardening: a **directive** (D-class) — "a test for a capability whose value is end-to-end delivery must exercise the delivery, not a hand-placed intermediate state" — and/or a reviewer-persona standing check on reconciler/durability/gated-capability diffs.
2. **ADR persistence/precondition claims verified against the actual provider at authorship.** The single highest-recurrence surface is the j17 bridge allowlist (a hand-maintained list ADRs keep assuming is a wildcard). Structural fix: **make the bridge persist-by-default** — a real wildcard subscribe + an explicit, visible skip-list — so a new tap persists unless someone opts it out on a visible line, not via silent omission. This makes the entire T-1806/1808/1809 class *impossible* rather than documented. (Pending ADR, the operator TG 6066.)
3. **Ratified-but-unbuilt surfaced, not silent.** A ratified ADR whose carved build ticket sits open (T-1602's shape) should surface in grooming as "ratified, unbuilt" — otherwise the docs/tests advertise a capability that doesn't exist.

The meta-move for any future reviewer/auditor: for a claim of the form "X is already Y," go read the provider of X, not the doc that asserts it (`feedback_read_the_spec_not_the_ADRs`, `feedback_verify_negative_claims_from_logs`).

## Open questions

- Does fix #1 promote to a full **directive**, or stay a reviewer-persona check? The four-instance recurrence argues for directive-tier; the counter is that "test the real path" is nearly a restatement of D5 (smoke discipline) / D11 (real-surface verification) — maybe this is an *amendment* to D5/D11 (add: "tests that inject past a precondition do not count as exercising it") rather than a new D.
- Is persist-by-default (fix #2) the right shape, or does it trade silent-drop for silent-over-persist (volume/cost)? The skip-list makes drops visible; the volume cost is the intended durability cost (per ADR-245 §2.4's own accounting). The operator's ratification call.
- How far does the family generalize beyond persistence + timing — to any "the other end provides X" assumption (auth boundaries, config presence, capability gates)? The T-1810 (shipped-config lacks the wiring the live config has) and T-1764 (a leaked process a prior session mis-modeled as a live service) cases suggest the "assumed vs actual provider" root is broader than tests alone.
