# Testing depth and scope

**Status:** **Ratified 2026-05-20 by the operator (Telegram msg_1742, via T-0488).** Base drafted 2026-05-13 (T-0305 per the operator msg_1212 + msg_1214 + msg_1215; ratified via T-0307). Amended 2026-05-19 (T-0437 per the operator msg_1526 + msg_1528) with: External-API adapter row + Schema-source check column (Amendments A+B), CI-portability requirements section (Amendment C), command-output-content discipline (Amendment D) — these amendments ratified by msg_1742. The msg_1215 **Echo-resumption gate is now LIFTED** — Echo work may resume. Amendment E (TDD cross-ref) deferred pending T-0401.
**Amendment F (§Test-tier ladder) Ratified 2026-06-02 (the operator, T-0699; ORG-024 gap-3):** added §Test-tier ladder (depth axis) — the existence→property→behavioural→adversarial reviewer checklist.
**Amendment G (§Tests must exercise the real failure condition) Ratified 2026-06-26 by the operator (T-1326); drafted by Orchestrator (HO).** Folds the three Echo Phase-3 "green-but-hollow" lessons into a new section + reviewer-checklist additions: G1 concurrency/restart tests trigger AFTER steady-state + assert effect-propagation; G2 completeness-checklist gate (smoke-green ≠ done); G3 control-surface wiring scope-match + not-wired guard.
**Amendment H (§Falsification must be RUN, not reasoned about) Adopted 2026-07-28 by Orchestrator (HO)** under D3/msg-3733 (below-goal-level — adopted + ledgered, not filed for ratification; see §Adoption note, which records the deliberate divergence from the matrix's Ratification-REQUIRED row). Origin: the 2026-07-28 mailbox/monitoring arc. Where G asks *did the test reach the failure condition*, H asks *can this test fail at all* — H1 a recorded red observation is not substitutable by argument; H2 a check that cannot tell healthy from broken is not an assertion; H3 a predicate validated as a moment and deployed as a standing rule; H4 the pin pair.

This is the authoritative org-level discipline for *how much testing each cycle should land*, indexed by ticket type and test tier. Per-domain expansion lives in sibling SOPs ([visual-verification-for-ui-tickets.md](visual-verification-for-ui-tickets.md), [auto-commit-cron-default-safe-contract.md](auto-commit-cron-default-safe-contract.md), [team-orchestrator-dispatch-checklist.md](team-orchestrator-dispatch-checklist.md)); this SOP is the rollup that says *which ticket types must clear which tiers before close*.

## When to use this

Every cycle. The SOP applies at three decision points:

1. **Brief-construction time.** HO or project-orchestrator writes the brief; the brief names which test tiers are REQUIRED (reviewer REBOUNDs if absent) vs RECOMMENDED (reviewer notes the gap; implementer judgment) vs N/A. Reference the matrix below per the ticket's type.
2. **Plan-tier emission.** Implementer's plan READY enumerates the brief-named tests (per D6 §brief-named tests convention). Reviewer pre-flight checks plan covers the REQUIRED tiers; absent = REBOUND.
3. **Reviewer close-out.** Before APPROVE, reviewer verifies REQUIRED tiers fired green + RECOMMENDED tiers either fired or were explicitly skipped with justifies-via. Missing REQUIRED = REBOUND.

Not used: routine tooling work below the HO-direct edit threshold (single-line config fix, transition log append, etc.) — these don't go through a brief→plan→reviewer cycle, so there's no tier-decision to make.

## The matrix

Rows = ticket type. Columns = test tier (cost-ordered, cheapest first). Cells = REQUIRED / RECOMMENDED / N/A.

| Ticket type | Unit | Integ (same-proc) | Integ (cross-proc) | Visual / Manual smoke | Ratification check | Schema-source check |
|---|---|---|---|---|---|---|
| **Feature** (new functionality / new file / new endpoint) | **REQUIRED** | **REQUIRED** | RECOMMENDED | REQUIRED if UI; RECOMMENDED if CLI-output-matters | RECOMMENDED if architectural | N/A |
| **Bugfix** (regression OR live `bugs/` entry) | **REQUIRED** (regression test capturing the bug) | RECOMMENDED | N/A | REQUIRED if UI; RECOMMENDED if CLI | N/A | N/A |
| **Refactor** (no behavior change; mechanical or structural) | **REQUIRED** (existing tests green + new tests if interfaces shifted) | RECOMMENDED | N/A | RECOMMENDED if UI surface touched | N/A | N/A |
| **Cron / auto-commit script** | **REQUIRED** (handler-tier unit coverage) | **REQUIRED** (T-0259 Layer-3 fixture: `tools/_test_fixtures.py`) | RECOMMENDED if external services touched | N/A | N/A | N/A |
| **UI work** | RECOMMENDED (component-tier unit; framework-dependent) | RECOMMENDED (component-tree integration) | N/A | **REQUIRED** (per [visual-verification-for-ui-tickets.md](visual-verification-for-ui-tickets.md): mobile + desktop screenshots) | RECOMMENDED if nav-surface or design-call material | N/A |
| **ADR / SOP / docs-only** | N/A | N/A | N/A | N/A | **REQUIRED** (per [T-0157 ratified SOP gate](../remediation/orchestrator-tier-skip-on-approval.md): the operator ratifies content before precedent) | N/A |
| **External-API adapter** (vendor-API integration: HTTP/WS client, SDK wrapper, gRPC adapter, etc.) | **REQUIRED** | **REQUIRED** (wiremock or equivalent fixture-driven coverage) | **REQUIRED** (LIVE API hit — quota cost accepted; surfaces schema-drift during the implementer cycle, not in prod) | **REQUIRED** (CLI output captured + visually verified — actual response text/payload, not just "non-empty") | RECOMMENDED if architectural | **REQUIRED** (per Amendment B below) |

### Test-tier definitions

- **Unit** — in-process, mocked dependencies. Handler-tier or function-tier coverage. Fast (sub-second per test). Lives in `tools/test_<name>.py` (Python) or `tests/<name>.test.ts` (Bun/JS) or `crates/<crate>/tests/<name>.rs` (Rust).
- **Integration (same-process)** — real subprocess or in-process integration with tmp infra (tmp git repo, tmp state dir, tmp ledger). Per T-0259 Layer-3: spawn the real script in a subprocess against tmp resources; assert effects landed in tmp + real `pantheon-config` repo HEAD didn't move + no `git push` reached real `origin`. Fixture lives at `tools/_test_fixtures.py`.
- **Integration (cross-process)** — real services on the same machine OR against staging. Real bridge endpoint, real systemd timer cycle, real PWA daemon hit. Slower (seconds-to-minutes); fewer per-test. Reserved for boundary-crossing work where the same-process integration can't simulate the boundary (HTTP, IPC, systemd).
- **Visual / manual smoke** — Playwright e2e or manual screenshot for UI; CLI output capture for command-output-matters work; bridge HTTP probe for endpoint-shape work. The "did a human eye verify rendered output" tier. Required for UI per the sibling SOP.
- **Ratification check** — the operator approves shape on architectural / convention / docs-tier calls. Surface via ratification ticket (target=operator, labels:[ratification], ratifiable_files cites the artifact). Required for ADR/SOP/docs because content-shape is the load-bearing artifact; reviewer-tier verification of conventions doesn't substitute for the human eye on conventions.
- **Schema-source check** — for external-API adapters, the wiremock (or equivalent) fixtures MUST reference the vendor's spec document by URL in a comment AND carry a `schema-checked YYYY-MM-DD` date. Reviewer pre-flight verifies: (a) the URL still resolves; (b) the date is recent (within ~30 days for actively-evolving APIs; longer windows acceptable for stable/v1-frozen APIs with explicit justifies-via). The goal is preventing the failure mode where the wiremock fixture encodes the wrong schema, all unit + same-process-integ tests pass green against the wrong schema, and the real vendor API rejects the request in prod. Failure-mode class: when fixture-encoded shape diverges from vendor's actual schema (typo, version-drift, undocumented field rename, nested-vs-flat structure mismatch), local tests stay green against the fixture and the divergence only surfaces when the live API rejects the call. Making the schema's authority chain explicit + reviewable forces the surface during the implementer cycle rather than in prod.

### Reading the matrix

- **REQUIRED** means *reviewer REBOUNDs on close if absent*. Brief MUST name the specific test(s) at plan-tier (per D6 §brief-named tests). Implementer cannot ship without firing them green.
- **RECOMMENDED** means *reviewer notes the gap; implementer judgment whether to land*. Implementer who skips RECOMMENDED tier must emit a DECISIONS line justifying-via (typically D10 minimum-traversal — "the test's signal value is below its construction cost"). Reviewer either accepts the justification or REBOUNDs on the justification's substance.
- **N/A** means *not applicable to this ticket type*. Don't fold in cargo-cult tests just because the column exists.

### Cross-type cycles

When a cycle spans multiple ticket types (e.g., a feature that's also UI work), apply the *strictest* cell across the relevant rows. Example: "add new `/api/stats/bridge-health` route + matching frontend sub-view" is Feature + UI; Unit REQUIRED (Feature row) + Visual REQUIRED (UI row) + Integ-same-process REQUIRED (Feature row, server-side route handler).

When a cycle is genuinely just one type but spans multiple repos (e.g., cycle_stats.py extension in pantheon-config + frontend consumer in pantheon-pwa), apply the matrix per repo. Each repo's REQUIRED cells fire; integration tier RECOMMENDED across the repo boundary unless the boundary itself is the load-bearing surface (then REQUIRED).

## Isolation discipline

Tests at the Integration tier and beyond touch shared resources (real repo paths, real state files, real ledger). All such tests MUST use:

1. **T-0259's `tools/_test_fixtures.py`** for auto-commit / git-touching scripts. The fixture provides tmp repo + bare origin + state dir + `assert_real_repo_untouched` final check. Per the [auto-commit-cron-default-safe-contract.md](auto-commit-cron-default-safe-contract.md) SOP, every auto-commit script gets Layer-3 coverage via this fixture.
2. **`mkdtemp` + per-test teardown** for non-git temp infra (tmp state dirs, tmp config files, tmp JSONL outputs). Each test gets a fresh tempdir; teardown removes it. Mirrors the existing `tools/test_session_reset_log.py` + `tests/ratifiableFiles.test.ts` patterns.
3. **Mock at the boundary, not at internals** — when a real subprocess can't be spawned (e.g., live network call to Anthropic API), mock the boundary (the HTTP client), not the internal logic that uses it. Per the implementer-tier persona convention from B5.

The anti-pattern this avoids: tests that mutate the real `pantheon-config` repo, real ledger, real state dirs, or real `~/.local/state/pantheon/` JSONLs. Such tests pass locally + corrupt the real environment for subsequent cycles. T-0259 surfaced this pattern in `tools/test_session_reset_reconcile.py` (pre-fixture); fixture extraction was the structural fix.

## CI-portability requirements

Tests that pass locally but fail in CI silently rot the test suite's signal value. Any code path under test that depends on resources outside the cloned repo MUST be CI-portable, where "CI-portable" means *a fresh checkout on a fresh CI runner can run the test without manual intervention*.

The dependency classes that trip this most often:

- **System binaries** — libonnxruntime, ffmpeg, libsndfile, native Rust deps. If `cargo build` or `npm install` needs the binary present, CI must provision it.
- **Environment variables** — API keys, paths, model-dir locations. If a test reads from `$ANTHROPIC_API_KEY` or `$XAI_API_KEY` etc., CI must inject the secret OR the test must skip-with-justifies-via when the env var is absent.
- **Filesystem layout outside the repo** — `vendor/` (gitignored binary blobs), `~/.cargo/`, `/opt/<vendor>/`, `~/.local/share/<x>/`. If a test reads from a path outside the cloned tree, CI must populate it.
- **Network availability** — during build (cargo fetch, npm install) OR during test (live API hit, real DNS lookup). If a test makes a network call, CI must allow that egress OR the test must guard with skip-conditional.

For any code path depending on the above, the implementer's plan-tier emission MUST name ONE of:

- **(a) CI bootstrap step** — a CI workflow step that provisions the dep (apt-get install, ssh-keyscan, env var injection from secrets, etc.). The step is named in the plan; reviewer verifies it lands in `.github/workflows/<x>.yml` or equivalent.
- **(b) Test-skip with explicit `--cfg ci` (or equivalent skip-conditional) gate** — the test runs locally but skips in CI; the skip condition is structured (compile-time gate or runtime env-var check), not "best effort try/except." Reviewer verifies the skip is intentional + reviewable, not accidental silence.
- **(c) Explicit "not CI-portable" justifies-via in the implementer's DECISIONS section** — the implementer names *why* this can't be made CI-portable and the trade accepted (e.g., "vendor dep is 4GB binary; CI provisioning cost exceeds signal value of running these tests on every PR; tested locally pre-merge per <discipline>"). Reviewer either accepts the trade or REBOUNDs on the substance.

The reviewer pre-flight gains one item: **"tested on fresh CI runner since last dependency change."** The default-green view is a CI run that finished green on the latest commit AFTER the dependency-touching change landed; not "the most recent CI run on this PR which may have been before the dep change."

Origin case: T-0379 silero crate tests passed locally because `vendor/onnxruntime-linux-x64-1.22.0/lib/libonnxruntime.so` was downloaded manually at some point; CI runners check out fresh, `vendor/` is gitignored, the .so was missing, all 9 silero adapter tests panicked. Failure existed for 5+ commits before the operator noticed in the GH Actions UI. The structural fix would have been: at silero adapter authoring, plan-tier emission names path (a) [`vendor/` populated by a bootstrap step], path (b) [tests skip when .so absent], or path (c) [explicit "not CI-portable" with reviewer-accepted trade]. The brief landed without naming any of these, so the implementer cycle silently shipped path (d) [pass locally, fail in CI without anyone noticing].

## Common failure modes

### Under-testing surfacing late

Ship a Feature without Integ-same-process coverage; bug surfaces post-deploy when real subprocess interaction hits a path the unit tests' mocks didn't cover. Examples in the b-cycle corpus: b42 v1 schema-cache-instance-vs-module (asserted broken behavior in unit tests; passed; reviewer pre-flight caught the spec violation); T-0288 chat-bubble-width "paper fix" landed because no visual test ran on the actual rendered output.

**Recovery:** when a bug surfaces post-close that the matrix says should have been REQUIRED, file the bug as a `bugs/` entry citing the failure-mode + the missed tier; reviewer's pre-flight on subsequent cycles tightens on that ticket-type's REQUIRED tier.

### Over-testing the cargo-cult column

Ship a Refactor with cross-process integration tests because "more tests = more safety". The tests slow the cycle without adding signal (the refactor didn't change behavior, so cross-process integration just re-runs the same green path). Implementer judgment + reviewer pre-flight should catch this; the matrix's N/A cells are intentional.

**Recovery:** retroactively delete the cargo-cult tests in a Refactor cycle; document the deletion's rationale in the close DECISIONS section.

### Skipping REQUIRED with vague justifies-via

Implementer emits "skipped Integ-same-process per D10" without naming what specifically was minimum-traversal-violating. Reviewer REBOUND on the justification's substance, not on the absence — the matrix says REQUIRED, so the rebound is *prove the cell shouldn't apply here*, not "fire the test you skipped."

### Treating Ratification as just-a-comment

Docs-tier work landing without filing a ratification ticket because "I'll just commit and let the operator see it in the PWA." The matrix says REQUIRED for ADR/SOP/docs because the ratification is *the test*; without it, the content has no precedent-binding force per D7's variance-absorption framing. Always file the ratification ticket as part of the close, not as a forgotten followup.

### Success-path silent on output content

For test cycles where the OUTPUT TEXT MATTERS (transcript content, command response payload, generated code, formatted report, LLM response) — not just the output's *existence* — the success path of the Visual / Manual smoke tier MUST print the actual content, not just assert non-emptiness.

The failure mode: a test asserts `assert!(events.iter().any(|e| !e.transcript.is_empty()))` and prints "test passed." The actual transcript was "uhhhhh" or had garbled tokens or was the wrong language. Test green; reality wrong; surface invisible until prod use.

The discipline:
- **Success-path println of actual content REQUIRED**, formatted readably (e.g., `eprintln!("[live_smoke] transcript={transcript:?}")` for Rust, `print(f"transcript: {payload!r}")` for Python).
- Reviewer pre-flight verifies the printed content matches expected *semantically*, not just structurally. "Print shows the transcript said the word 'hello' as expected" beats "print shows non-empty string."
- For binary/large outputs, print a hash or summary + sample (first 200 chars / first 5 records / etc.).

Origin case: T-0391 Speechmatics live_smoke v1 asserted "at least one event has non-empty transcript" but didn't print the actual text. Was "GREEN" without confirming the transcript actually matched the fixture's expected speech. v2 amendment added the println; the bug it would have caught earlier surfaced via T-0381 schema-drift investigation.

This SOP's matrix marks Visual REQUIRED for External-API adapter row + RECOMMENDED-if-CLI-output-matters for Feature/Bugfix rows. When that cell fires, the discipline above is the substance of "fired green" — not just "the test ran without panicking."

## Test-tier ladder — depth axis (reviewer checklist)

The matrix above answers WHICH tiers a ticket needs (unit / integ / visual). This ladder answers HOW DEEP each test goes — the kinds of assertions. We default-write behavioural + occasional adversarial; existence + property are written inconsistently. The reviewer applies this as a checklist against an implementer's test set:

1. **Existence** — does the thing exist and get *called*? The function is defined, wired in, and actually invoked on the path under test. Catches dead/unreachable code + never-exercised branches (a "test" that imports but never calls the unit).
2. **Property** — does the output satisfy invariants across the input space, not just one example? Counts preserved, ordering stable, idempotency, round-trip identity, no-dupes. The assertions that hold regardless of the specific input.
3. **Behavioural** — does it do the documented thing on representative input? Happy path + named edge cases. (What we already write most.)
4. **Adversarial** — what's the malicious or worst-case input? Boundary values, hostile/garbled/empty input, races, the failure mode a pre-mortem surfaces (see [plan-first-dispatch.md](plan-first-dispatch.md) §Pre-mortem).

**Reviewer question:** "Does this test set cover all four rungs for the surface it guards — or only behavioural?" A non-trivial change with behavioural-only coverage = flag the missing rungs, especially **property + adversarial** (highest-signal, least-written). Scope-proportionate: a trivial change may legitimately need only existence + behavioural; the ladder forces *consideration* of all four, not four tests on everything.

Worked examples (2026-05-29): the acceptance-check cycle's **adversarial** rung (a regex word-boundary test) pinned a real false-positive bug behavioural tests missed; the ledger migration's **property** rung (id-multiset + line-count preservation) is what proved 489 entries survived the regroup.

## Tests must exercise the real failure condition (Amendment G)

**DRAFTED 2026-06-26 by Orchestrator (HO); awaiting ratification (T-1326).** Origin: the Echo Phase-3 configurator arc surfaced three distinct ways a test passes green while never exercising the bug it ostensibly guards — a *green-but-hollow* test. The matrix above says WHICH tiers a ticket needs and the ladder says HOW DEEP each test goes; this section says the test at any tier must actually *reach the failure condition*. Each discipline below broke a real recurrence live (lessons filed 2026-06-26).

The shared root across all three: **a test that LOOKS like it validates but doesn't exercise the real failure condition** — because the timing dodges it, the test was never written, or the assertion checks construction instead of effect. All three are invisible to a green smoke run, which is exactly why the matrix + ladder don't catch them on their own.

### G1 — Concurrency / restart-path tests trigger AFTER steady-state, and assert effect propagation

A concurrency or restart mechanism's test MUST deliver its trigger AFTER the system has reached its steady-state wait (parked in its select/recv loop), not at construction-time or before the loop starts — otherwise the bug's precondition (the system already parked) never occurs in the test. And it must assert the *propagated effect* (audio reaches the new session; the new config value is in force), NOT merely that a factory/session/object was *constructed*.

- **Mechanic:** spawn the mechanism on a never-exiting subsystem, yield to let it park, THEN send the trigger, and assert it's handled within a short timeout. A correct guard TIMES OUT under the buggy code (so it's a real regression test, not a construction check).
- **Tell (REBOUND):** a restart/concurrency test that sends its trigger before the event loop starts, or asserts "a thing was built/constructed" rather than "the effect propagated end-to-end."
- Reviewer applies this on any ticket touching restart, supervision, channel-draining, or event-loop concurrency.
- Origin + template: [`lessons/2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing.md`](../../lessons/2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing.md); the worked guard is `deliberate_restart_responsive_while_monitor_parked` (echo T-1313).

### G2 — Completeness-checklist gate: enumerate every required test; smoke-green ≠ done

"Smoke is green" is NOT evidence that every required test was written — an omitted test simply doesn't exist to fail, so green is a *false* completion signal. This sharpens §When to use #3 (reviewer close-out): the reviewer ENUMERATES every brief-named REQUIRED test (per the matrix + D6 §brief-named tests) and confirms each one's passing result line is present in the implementer's report. The implementer's report MUST carry that enumerated checklist; a bare "smoke green" is rejected as completion evidence.

- **Tell (REBOUND):** "smoke green" offered as the done-signal with no per-required-test accounting; acknowledged-but-unfilled coverage gaps sitting in code comments.
- Distinct from the existing reviewer "tests-meaningful, not hollow" check: that one targets tests that are *vacuous*; this targets tests that are *absent*. A green smoke run says nothing about a test never written.
- Origin: [`lessons/2026-06-26-ships-green-while-incomplete.md`](../../lessons/2026-06-26-ships-green-while-incomplete.md) (echo T-1313 REBOUND #3 — a brief-required relay test omitted while shipping smoke-green; the gate broke the recurrence in one gated attempt).

### G3 — Control-surface wiring: verify the triggered handler actually produces the control's implied effect; guard the not-wired case

Before wiring any control affordance (GUI button, CLI command, settings banner) to a handler meant to apply its effect, verify the handler's *actual scope* (what it really changes) MATCHES the control's *implied binding* (what a user expects that control to change). An existing, convenient handler assumed to also cover an adjacent control is a **silent no-op** — the affordance appears to work, no error surfaces, and the change never takes effect. (Canonical instance: an `apply-restart` op scoped to one daemon subsystem, wired to the banner for a *different* subsystem's config — restarting subsystem A never applies subsystem B's change.) When the handler's scope ≠ the control's binding:

1. Leave the affordance **manual**, with a boundary comment naming WHY.
2. Add a **not-wired regression guard** — a test asserting the affordance is ABSENT — so a future "wire all the banners" sweep can't silently re-introduce the no-op.
3. File a **fast-follow** to build the correctly-scoped op.

- **Tell (REBOUND):** "reuse the existing apply/restart action for this other banner/config" without verifying the action's subsystem scope matches the binding. A ratified design doc is not immune — the Echo A2 case had a wrong "reuse B's restart mechanism" premise *ratified* before code-grounding exposed it.
- Origin: [`lessons/2026-06-26-silent-no-op-wrong-subsystem-wiring.md`](../../lessons/2026-06-26-silent-no-op-wrong-subsystem-wiring.md) (echo Phase-3 B2/A2; the not-wired Suite F is the worked guard).

**Reviewer checklist additions (Amendment G):** for concurrency/restart surfaces → G1 (trigger-after-park + effect-not-construction); at every close → G2 (enumerated required-test checklist, not bare smoke-green); for control surfaces wired to apply/effect ops → G3 (scope-matches-binding + not-wired guard). Like the rest of this SOP, scope-proportionate — G1/G3 fire only on their respective surfaces; G2 applies to any ticket with a brief-named REQUIRED test set.

## Falsification must be RUN, not reasoned about (Amendment H)

**Adopted 2026-07-28 by Orchestrator (HO) under D3/msg-3733 — below-goal-level, so no ratification ticket; see §Adoption note.** Origin: the 2026-07-28 mailbox/monitoring arc, where multiple guards were green, correct-looking, and structurally incapable of failing.

**THE RULE. A test you have not watched FAIL is not evidence that it works.** Before reporting a test as coverage: break the thing it guards, watch it go red, restore, watch it go green — and report **both** observations, not just the green one.

This is a distinct rung from Amendment G, and the distinction is what makes it worth its own section:

| | what is wrong | why green is misleading |
|---|---|---|
| **G1** | the test exists and runs, but its *timing* dodges the precondition | the bug's precondition never occurs |
| **G2** | the test was never **written** | an absent test cannot fail |
| **H** | the test exists, runs, and is green — and **cannot go red** | the assertion is unreachable, vacuous, or true by construction |

G2 asks *did you write it*. H asks *can it fail*. A suite can be complete by G2's enumeration and entirely hollow by H's.

### H1 — Reasoning about a falsification is not performing one

"If X were broken this test would catch it" is a hypothesis about your own code, produced by the same understanding that wrote the code. It fails exactly where the code fails. The check costs seconds and is not substitutable by argument.

- **Mechanic:** revert the fix (or corrupt the config, or delete the call site), run, capture the failure line, restore, run, capture the pass line. Both lines go in the report.
- **Counterfactual upgrade (for a test guarding a specific fix):** a mutation going red proves the test CAN fail — not that it fails *for the reason claimed*. The stronger form: revert the fix while KEEPING the mutation and confirm the test passes again — direct proof the fix is what the test detects, not something incidental. (Metis's T-2245 reviewer ran this unbriefed, 2026-07-29; adopted as the default ask for fix-guarding tests.)
- **Sabotage the THREAT, not the assertion:** the red run must break the guarded thing the way you FEAR it will break, not a way your assertion happens to detect. Worked instance (atlas's T-2256 reviewer, 2026-07-29): an anti-flattening guard's substring assertions went red under the implementer's *replacement*-shaped sabotage — honest record, hollow coverage — while the feared mutation class (*widening by prepending* to the case-arm) passed both assertions green. The guard would have shipped carrying a docstring asserting protection it did not provide, which is worse than no guard. Enumerate the feared mutation classes first; each one is a falsification to run.
- **The threat enumeration must be adversarially generated — a checklist can carry the author's blind spot with a tick next to it.** Same cluster, second cycle (T-2256): the rebound brief DID enumerate three shapes (prepend/append/replace); the implementer covered the enumeration exactly; the rebuilt guard was still hollow — and a *strict regression* — because all three were ways to *edit the existing arm*, and nobody enumerated *adding a new one*, which is how a person actually widens a shell case. Three people passed the substitution within hours of the rule above being written, two having just read it. The structural form that survived: (1) enumerate threat shapes BEFORE implementation; (2) justify the enumeration against **how the artifact is actually edited in practice** ("how would a person make this change to this file?"), never against "what shapes does my assertion handle?" — the second question feels like diligence and produces the tick; (3) the reviewer **attacks past the enumeration** (construct something that fools the guard), not merely audits coverage of it — coverage-auditing the flawed checklist would have approved the hollow guard. For unrecoverable-class guards, have someone who is not writing the fix generate the enumeration.
- **Tell (REBOUND):** a report asserting a test "would fail if the bug returned", with no recorded red observation. Also: "verified" applied to an inference rather than a run.
- **Worked instance — the one that names the shape.** T-2249: a test was made to `skipTest` on hosts lacking fleet topology. The skip was correct. Recovery then broke the config deliberately *with* topology present, to check the test still screamed — *"a skipping test that can no longer fail is decoration, and I would not have known without breaking the config and watching it scream."* A skip is the cheapest way to make a green test unfalsifiable, and it looks like diligence.

### H2 — A check that cannot distinguish the healthy state from the failure state is not an assertion

The most common hollow guard is not a wrong assertion but an **inert** one: an operation performed in the belief that performing it constitutes checking.

- **Worked instance.** T-2227: `canonical_is_pushed()` read `origin/main`, a *local cached ref*, with nothing fetching — precondition and verification derived from one snapshot and could not disagree. Adding the fetch alone would still not have been an assertion: *it cannot distinguish the failure state from the healthy one.* The fix asserts the post-fetch ref equals `git ls-remote`, and refuses on failure rather than falling back to cache. Specialist's framing — **a reminder wearing a mechanism's clothes** — is the reusable name for it.
- **Reviewer question:** "what input makes this check fail?" If nobody can answer with a concrete input, it is not a check.

### H3 — A predicate validated as a MOMENT and deployed as a STANDING RULE

A guard evaluated repeatedly over time must be falsified **across samples**, not at one instant. A single-snapshot test cannot tell a bounded predicate from an unbounded one — it agrees with both.

- **Worked instance.** T-2257/T-2258: the mail-staleness suppressor asked "is this queue draining?" via `recency < oldest`, where both terms are `now - (a fixed timestamp)`. They grow at the same rate, so their difference is constant and **the sign can never flip** — one partial drain immunizes an inbox against alerting permanently. Both validation cases recorded in the docstring were correct and both were instantaneous. The predicate was validated as a moment and shipped as a standing rule, and no single-sample test could have caught it. Live cost: two agents were invisible to the detector for hours, one of them for over three.
- **Mechanic:** sample, advance the clock (inject `now`; do not sleep), sample again, assert the alert **fires**. Then break it and watch the test fail.
- **Reviewer question:** on any predicate that runs on a timer — "does the test observe more than one instant?"

### H4 — The pin pair: two individually-correct constraints that are jointly unsatisfiable

Two assertions, each defensible when read alone, that no state can satisfy simultaneously — so the test is dead regardless of the code under it. Reasoning about either constraint separately looks fine, which is why H1's argument-instead-of-a-run reliably misses it. Running it once surfaces it immediately.

*Provenance note: this shape was reported by the T-2245 orchestrator during the 2026-07-28 arc; that worker was lost to a session restart and I have not recovered the specific artifact. The shape is recorded because it is instructive and cheap to check for; it is **not** backed by a ticket I have read. Replace this note with the citation if the instance is recovered.*

### Known-gap markers (expected-failure) — mechanics that determine whether the flip works

When a real gap is documented as a test (per an HO ruling of the T-2245 shape: green while the gap is real, suite-breaking the moment the gap closes, forcing promotion to a real assertion), the marker implementation decides whether that mechanism exists at all:

- **Use `unittest.expectedFailure`** (or `pytest.mark.xfail(strict=True)` explicitly). Measured 2026-07-28 (metis, T-2245): `unittest.expectedFailure` yields a FAILING run (exit 1) on unexpected success under both `python3 -m unittest` and `python3 -m pytest`.
- **Never bare `pytest.mark.xfail`** — it defaults `strict=False`, under which an XPASS does **not** fail the suite. In a repo with no `xfail_strict` ini (this one), the bare marker silently destroys the flip: the gap closes, the marker passes quietly, nobody is forced to promote it. The silent-green disease one level up, one import away.
- **The marker itself is H1-subject:** demonstrate the flip both ways before landing — fake the gap-closing surface (an env seam is fine), observe unexpected-success break the run, remove it, observe expected-failure. A marker never watched flipping is a hollow guard one level up.

### Reporting format

An implementer's report claiming a test as coverage carries, per test or per coherent group:

```
falsification: <what I broke>  ->  FAILED at <assertion/line>   (red observed)
restored                       ->  PASSED                        (green observed)
```

Absent the red line, the reviewer treats the test as **unverified coverage** — not as a failure, but not as evidence either. This is a REBOUND on any REQUIRED-tier cell.

**The pair must land in a durable artifact** — the commit message or a ticket annotation, not only the agent's report. An agent report is a transcript: it dies with the agent that produced it, so a reviewer (or any later reader) cannot check the recorded red against anything. Found live 2026-07-29 (atlas's T-2261 cycle): the reviewer could not read the implementer's ten-falsification record at all and had to rebuild the evidence as an independent sabotage superset — which worked, but only because the reviewer was strong; the rule must not depend on that. The durable copy may be the summary form (what was broken → what fired), not full logs.

### Boundary — where H does not apply

Scope-proportionate, like the rest of this SOP. H is not a demand to mutation-test every assertion in a suite. It fires on: any test landed as the guard for a **specific named defect**; any test whose green is being offered as the reason to close a ticket; any **skip** added to a previously-running test; any predicate on a timer (H3). It does not fire on incidental assertions inside a larger behavioural test, or on tests whose failure was already observed naturally during development — if you watched it go red while writing it, that counts; say so.

### Adoption note

The matrix above marks Ratification REQUIRED for SOP/docs work. That row predates the operator's msg-3733 ruling retiring below-goal-level ratification asks (D3, amended 2026-06-11), under which HO adopts and records in the ledger. This amendment is below goal level — it tightens an existing reviewer checklist and creates no new obligation on the operator — so it is **adopted, not filed for ratification**, and this note exists so the divergence from the matrix row is visible rather than silent. Goal-level SOP changes still route to him.

**Reviewer checklist additions (Amendment H):** for any test offered as coverage for a named defect → H1 (recorded red observation, not an argument); for any guard that performs an operation → H2 ("what input makes this fail?"); for any predicate evaluated on a timer → H3 (multi-sample test); for any newly-added skip → H1's mutation check specifically (the skip must not have made the test unfalsifiable).

## Boundary

This SOP does NOT cover:

- **Per-domain test mechanics** — those live in the sibling SOPs ([visual-verification-for-ui-tickets.md](visual-verification-for-ui-tickets.md) for UI-tier mechanics; [auto-commit-cron-default-safe-contract.md](auto-commit-cron-default-safe-contract.md) for cron-script Layer-3 fixture mechanics).
- **The brief-construction checklist** — that's [team-orchestrator-dispatch-checklist.md](team-orchestrator-dispatch-checklist.md). This SOP says *which cells of the matrix to honor*; the dispatch checklist says *how to operationalize those cells in the brief*.
- **The reviewer's REBOUND criteria** — see worker-persona files in `claude-config/personas/` for the canonical reviewer convention. This SOP says *what counts as a REBOUND-able test-tier absence*; the persona says *how the reviewer REBOUNDs*.
- **Test-naming conventions** — per D6 §brief-named tests + [plan-first-dispatch.md](plan-first-dispatch.md). This SOP determines *which tests must exist*; the brief determines *what they're called*.

## Cross-references

- **Principles + directives:** P1 (architecture over policy — test discipline is structural enforcement, not prose-tier); P3 (decisions justify upward — RECOMMENDED-skip + REQUIRED-skip both carry justifies-via); P4 (traceability — REQUIRED tier coverage is part of the audit chain at close); D1 (the operator's bandwidth — the matrix is the calibration; eliminates per-cycle re-derivation); D6 (plan-first protocol — brief-named tests at plan-tier flow from this SOP's REQUIRED cells); D10 (context-frugality — the matrix's N/A cells prevent cargo-cult test sprawl).
- **Sibling SOPs:**
  - [`visual-verification-for-ui-tickets.md`](visual-verification-for-ui-tickets.md) — per-domain expansion for the UI row's Visual REQUIRED cell.
  - [`auto-commit-cron-default-safe-contract.md`](auto-commit-cron-default-safe-contract.md) — per-domain expansion for the Cron-script row's Integ-same-process REQUIRED cell (T-0259 Layer-3 fixture is the concrete mechanism).
  - [`team-orchestrator-dispatch-checklist.md`](team-orchestrator-dispatch-checklist.md) — brief-construction discipline that operationalizes this SOP's REQUIRED cells at dispatch time.
  - [`plan-first-dispatch.md`](plan-first-dispatch.md) — the §brief-named tests convention this SOP's REQUIRED cells flow into.
- **Fixtures + tooling:**
  - [`tools/_test_fixtures.py`](../../tools/_test_fixtures.py) — T-0259 Layer-3 fixture. Required for cron-script Integ-same-process tests.
  - [`web/tests/visual/visual-mobile.spec.ts`](https://github.com/example-org/pantheon-pwa) (pantheon-pwa) — Playwright mobile-viewport infrastructure for UI Visual REQUIRED. Per T-0288 visual-regression infra.
- **Remediation SOPs:** [`../remediation/plan-first-violation.md`](../remediation/plan-first-violation.md) (worker skips plan-tier test enumeration); [`../remediation/jumps-ahead-without-brief.md`](../remediation/jumps-ahead-without-brief.md) (worker emits code before brief's REQUIRED tier list lands).
- **Tickets (Amendment H origin cases):** T-2249 (the skip that could no longer fail — H1's worked instance); T-2227 (`origin/main` read from a local cached ref; "a reminder wearing a mechanism's clothes" — H2); T-2257 / T-2258 (the staleness suppressor whose sign can never flip — H3); T-2255 (a falsification run and reported in the prescribed red-then-green form); T-2245 (the arc that surfaced the class).
- **Tickets:** T-0305 (this SOP's authoring); T-0437 (the 2026-05-19 amendment cycle: Amendments A+B+C+D); T-0259 (Layer-3 fixture genesis); T-0288 (visual-regression infra genesis); T-0157 (per-SOP ratification gate framework); T-0160 (standard-bucket SOP framework extension); T-0238 (Playwright e2e conventions); T-0381 (Speechmatics schema-drift case study; Amendment B origin); T-0379 (silero CI-env-drift case study; Amendment C origin); T-0391 (success-path-silent-on-content case study; Amendment D origin); T-0401 (TDD eval — pending; Amendment E will cross-reference once T-0401 ratifies adoption).
- **Telegram refs:** msg_1212 (the operator's originating ask — "have we done a sop on how extensive testing should be in projects?"); msg_1214 (HO-authors-not-Recovery ratification); msg_1215 (priority elevation — blocks Echo resumption); msg_1526 (the operator's amendment-driving observation — "what's the pattern for designing + agreeing on tests for our current system? bc they often seem to have a lot of holes"); msg_1528 (the operator confirms file-consolidating-ticket framing for the amendments).
- **Lessons:** [`lessons/orchestrator-tier-substrate-verification.md`](../../lessons/orchestrator-tier-substrate-verification.md) — adjacent class (orchestrator verifies substrate; this SOP verifies test-tier coverage; both are at brief-construction tier).
