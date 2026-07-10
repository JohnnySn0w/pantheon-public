---
name: verification-before-completion
description: Use before claiming any work complete, fixed, or passing. Requires running verification commands and reading their output BEFORE making success claims. Vendor-forked from obra/superpowers, adapted to Pantheon vocabulary (P/D citations, smoke loops, wet-runs, REBOUND/DECISIONS). Composes with D5 (smoke verification discipline), D6 (plan-first protocol), D8 (HO self-discipline — cross-check claims pre-commit), D11 (third-party surface verification).
---

# Verification before completion

**Status:** **Ratified 2026-05-27 by the operator.** Authored 2026-05-27 by Orchestrator (Orchestrator). — vendor-forked from [obra/superpowers — verification-before-completion](https://github.com/obra/superpowers/blob/main/skills/verification-before-completion/SKILL.md) and adapted to Pantheon vocabulary, citing concrete Pantheon failure-evidence from the 2026-05-26 Echo speaker-id arc (T-0603 → T-0616). Closes T-0406.

**HITL:** Authored; awaiting the operator ratification.

**Adaptation:** Cross-references Pantheon P/D ids rather than superpowers siblings; uses Pantheon vocabulary (smoke loop, wet-run, REBOUND, DECISIONS, justifies-via). Concrete failure-mode examples drawn from Pantheon's own ledger, not the upstream's failure memories.

## Overview

**Claiming work is complete without verification is dishonesty, not efficiency.** Evidence before claims, always.

**Meta-rule:** Violating the *letter* of this rule is violating the *spirit* of this rule. Wording the claim differently (synonym, paraphrase, implication, hedge) doesn't exempt the discipline. The discipline applies to ANY communication that asserts work-state.

## The Iron Law

**NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.**

If you haven't run the verification command in this turn — for this iteration of the work — you cannot claim it passes. Past runs decay; the code may have changed; the environment may have shifted; "should work" is not evidence.

## The Gate Function

Five-step protocol before any status claim:

1. **IDENTIFY** — what command (or query, or live probe) would prove this claim true?
2. **RUN** — execute the full command. Fresh. Complete. Not a partial.
3. **READ** — read the full output. Check exit code. Count failures. Surface raw error frames if the API reports them.
4. **VERIFY** — does the output confirm the claim?
   - If NO: state the actual status with the evidence quoted.
   - If YES: state the claim *with* the evidence quoted (D5 — literal command-tail in fenced block; never delta-claims).
5. **ONLY THEN** make the claim.

Skipping any step is lying, not verifying.

## Pantheon-specific concrete forms

**Smoke loop claims** (`cargo test passes`, `pytest green`, `bun test ok`): RUN the project's smoke loop literally, paste the literal `test result:` lines per crate / per binary in a fenced block. Per D5: orchestrators run smokes directly before relaying upward — don't relay implementer claims as smoke proof. Per `feedback_smoke_grep_all_test_result_lines.md`: workspace runs emit one `test result:` per binary; enumerate every FAILED line, never infer "rest passed."

**Wet-run claims** (`live API accepted the payload`, `the daemon does X`): RUN the wet-run against the real endpoint or running daemon. Capture the wire-level request + response (or sqlite payload, or log line). Quote it. Per D11: real-surface validation is the load-bearing gate; fake-server unit tests are necessary but insufficient.

**Build claims** (`binary rebuilt`, `daemon running new code`): VERIFY by mtime + version stamp + a known new log line on startup. Per D11 deploy-verification corollary: a rebuild does not guarantee deployed. The cargo build command succeeding is not the same as the running process executing the new code.

**Task / phase completion** (`PR ready`, `ticket can resolve`, `Phase N done`): RE-READ the acceptance bar in the ticket / brief. Build a checklist. Verify each item with evidence. Report gaps explicitly rather than ambiguously implying done. Per D9 orchestrator response contract: categorical answer (done/partial/blocked with X) + evidence + format.

**Agent delegation** (`worker reports success`): VERIFY independently. Check the git diff. Run the smoke loop yourself. Query the sqlite event store. Per D8 HO self-discipline: cross-check claims pre-commit. Per the team-pattern feedback memory: trust dispatch-success acks only after you've seen the artifact.

## Common failure modes (Pantheon-recurrent)

| Claim | Requires | Not sufficient |
|---|---|---|
| Tests pass | Literal `test result:` lines per binary; 0 FAILED across all | Previous run's output; "tests should pass with this change" |
| Smoke green | Full `cargo fmt && cargo clippy && cargo test` literal output | A subset; "I ran clippy" |
| Wire schema correct | Live API accepted the payload (D11) | Unit tests against your own mocks |
| Bug fixed | The original failing case now passes | Code changed; "this should fix it" |
| Daemon picked up the fix | Restart + new log line + mtime + behavior change | Rebuilt; restarted; "should be running new code" |
| Reviewer APPROVE | Reviewer ran the smoke + verified DECISIONS + checked merge surface | Reviewer's general impression |
| Ticket can resolve | Every acceptance-bar item verified with evidence | "I addressed the main thing" |

## Red flags — STOP and verify

- Using "should", "probably", "seems to", "I think"
- Expressing satisfaction before evidence ("Great!", "Perfect!", "Done!", "Working!", "Looking good!")
- About to commit / push / open PR / report up / lift to ledger
- Trusting an agent's success report at face value
- Relying on partial verification (clippy passed → tests pass; subset of tests → workspace clean)
- Thinking "just this once"
- Tired and wanting the task over
- Anything implying success without having run the verification THIS turn

If any of these fire: stop. Run the verification command. Read the output. THEN decide what to say.

## Rationalization prevention

| Excuse | Reality |
|---|---|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence is not evidence |
| "Just this once" | No exceptions; the meta-rule applies |
| "Linter passed" | Linter ≠ compiler ≠ tests ≠ live behavior |
| "Agent reported success" | Verify independently (D8) |
| "I'm tired" | Exhaustion is not an excuse; the cost of wrong claims compounds |
| "Partial check is enough" | Partial proves nothing about the whole |
| "Different wording so the rule doesn't apply" | Spirit over letter (meta-rule) |
| "We're in alpha, it's fine" | Alpha means failures hit YOU not customers; verification is still the cost-floor (msg_2364 retired the deprecation-shortcut, not the verification-shortcut) |

## Patterns / exemplars

**Tests:**
```
✅ [run cargo test --workspace] [paste literal "test result: ok. 414 passed; 0 failed" for every binary] "Smoke green."
❌ "Tests should pass with this change."
```

**Wet-run (third-party API):**
```
✅ [pkill daemon → rebuild → restart → speak → query sqlite → quote the speaker_label='operator' row] "Live audio matched the enrolled speaker."
❌ "The wire shape is correct, so matching should work."
```

**Daemon picked up the fix:**
```
✅ [ls -la binary] [grep echo.log for a NEW debug log line introduced in this PR] "Daemon at pid X is running the new binary, mtime confirms, log line proves the new code path fired."
❌ "I rebuilt, then restarted."
```

**Reviewer APPROVE:**
```
✅ [ran smoke; verified DECISIONS justifies-via against P/D; spot-checked 2-3 wire-shape claims; confirmed merge-base against origin/main; HERMES audit clean] "APPROVE."
❌ "The diff looks correct."
```

**Ticket resolution:**
```
✅ [enumerate every acceptance-bar item from the ticket; quote evidence per item] "Acceptance items 1-N satisfied. Resolving."
❌ "Done."
```

## Why this matters in Pantheon

The 2026-05-26 → 27 Echo speaker-id arc surfaced four rounds of bugs that prose-tier verification discipline would have caught earlier than structural-tier alone:

1. **T-0609** — schema bug invisible to tests because test fixtures mirrored the wrong struct shape (hand-invented, not derived from spec). D11 test-fixture corollary applies. Tests passed; live API rejected.
2. **T-0612** — model-compat bug surfaced only when daemon loaded the enrolled profile; T-0609's wet-run only covered enrollment, not the full roundtrip. "Wet-run covers every state transition the change touches" — not just the immediate call.
3. **T-0614** — inbound parsing bug invisible because NO test ever ran `serde_json::from_str::<AddTranscript>` on a real-API frame. Boundary-contract tests were missing entirely.
4. **T-0616** — hallucinated `entity_id` parameter because the adapter discarded HA-MCP tool schemas; the classifier had nothing to ground against. Live API accepted but downstream failed.

Each round was a verification gap. The pattern: structural enforcement caught what it could; prose discipline would have caught the rest if applied. The post-Phase-1 diagnostics SOP (`pantheon-config/notes/2026-05-26-echo-speaker-id-diagnostics.md`) layers the evidence-tiers; this skill is the operator-tier "what do I check before claiming?" complement.

## Pantheon-specific composition with existing P/D

- **D5** (smoke verification discipline) — covers smoke loop output format + orchestrator-runs-smokes. This skill operationalizes: D5 says "run the smoke + paste literal output"; this skill says "you cannot claim 'tests pass' without that artifact in this turn."
- **D6** (plan-first protocol) — covers planning + locked-answer-deviation. This skill operationalizes: a plan-tier claim ("the plan is ready") requires verification that the brief was actually re-read + each item addressed.
- **D8** (HO self-discipline) — cross-check claims pre-commit. This skill is the unified discipline-form of D8's specific HO behaviors, applied to any tier.
- **D11** (third-party surface verification) — wet-run discipline for external APIs. This skill is the GENERAL form; D11 is the specific D11-tier variant. D11 applies the iron law to outbound-API boundaries.
- **P3** (decisions justify upward) — DECISIONS sections require justifies-via P/D citations. Verification-before-completion underwrites the claim that the citation is *actually accurate* — not "I think P1 applies" but "P1 applies because [evidence]."
- **P4** (traceability) — append-only ledger requires entries are TRUE at write time. Verification-before-completion is the precondition for ledger entries that survive audit.

## When this skill applies

**ALWAYS, before any of:**

- Any variation of "done", "complete", "fixed", "passing", "working", "looking good", "ready"
- Any expression of satisfaction or progress beyond what the evidence directly supports
- Committing, push, PR open, PR merge
- Moving to the next ticket / phase / task
- Reporting up (worker → orchestrator; orchestrator → HO; HO → the operator)
- Filing a ratify ticket (ratifying something that doesn't actually verify is a downstream harm)
- Closing a ticket
- Lifting to ledger

The rule's scope is exact phrases, paraphrases, synonyms, implications, ANY communication suggesting completion or correctness.

## Bottom line

No shortcuts for verification. Run the command. Read the output. THEN claim the result. This is non-negotiable.

Pantheon's structural-tier (HERMES guard, scope-asserts, fail-fast on every gate, hooks) catches what it can. This skill is the prose-tier complement that catches the rest. They compose; neither replaces the other.
