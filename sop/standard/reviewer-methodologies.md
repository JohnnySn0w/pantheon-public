# Reviewer methodologies — the four-lens review

## When to use this

Every reviewer dispatch of substance — post-implementation code review, cross-clone artifact review, fold-review, ADR/spec review. Load the appropriate methodologies against the change under review and apply them independently. Not every review needs all four lenses (light bug-fixes typically warrant only per-blocker-match), but the reviewer brief SHOULD name which lenses are in scope, and reviewers SHOULD flag when a lens they weren't briefed on surfaces a defect the briefed lenses would miss.

**Ratification:** Adopted 2026-08-30 (T-3348 backfill) synthesizing the T-3308 → T-3323 arc lessons. Autonomous per D3 (methodology, below goal-level bar). **4th lens (dynamic-invocation / live-repro) added 2026-09-19 (T-3803, orchestrator), on the trigger the SOP itself named: T-3782 round-4 REBOUND surfaced a HIGH bug via an unbriefed lens (the fence-self-heal-destroys-worktree defect was invisible to per-blocker-match + architecture-vault + rendered-surface-legibility; only live-repro against a real worktree surfaced it). Autonomous per D3.

For reviewing a ticket or a close-not-build disposition BEFORE code is written, see the sibling `sop/standard/pre-implementation-reviewer-discipline.md`.

## The four methodologies

### 1. Per-blocker-match

**Definition.** Given a specific triggering defect (a prior REBOUND round's cited items, a filed bug's stated failure, a plan's named acceptance criteria), verify that the change addresses THAT defect — the exact items, the exact acceptance criteria — not merely a plausible-looking or superficially-related fix.

**When it fires.** Every round-N REBOUND cycle. Every plan-referenced acceptance test. Every filed-bug fix. Any review where a prior round produced enumerated blockers.

**What it catches.** Fixes that pass smoke but miss the actual defect (e.g. impl "addressed" the reviewer's item 3 by adding validation elsewhere, but the specific test case in item 3 still fails). Fixes that partially satisfy an enumerated list (5 blockers listed, 3 addressed, 2 silently deferred). Fixes that resolve the surface complaint but not the underlying class.

**Anti-pattern.** Reviewer accepts the implementer's own framing of "which item I fixed" without independently reading each cited blocker and confirming the fix touches it.

**Discipline.** Reviewer keeps a checklist of the enumerated items from the trigger, and each item is marked verified/unverified against the diff — never marked verified by transitivity ("the whole area was refactored, so this is probably covered").

**Decisive-check rule (T-4614).** For each acceptance criterion or enumerated blocker, the reviewer runs at least one decisive check themselves in the review worktree and quotes its command and result. A decisive check is the named test, a targeted repro, or a command whose output would differ if the criterion failed. The implementer's reported output is context, not evidence. Where running is impossible (live-only, cost, a forbidden actuation), the reviewer says so and marks the item UNVERIFIED rather than verified. Source: arXiv:2610.01023 (decisive checks improved both catch and over-rejection; reviewer size was not a consistent predictor of review quality).

### 2. Architecture-vault (founding-principle check)

**Definition.** Given the project's hand-curated architectural documents — the "vault" (per project: for Pantheon, `rulebook/principles.md` + `rulebook/directives.md` + `architecture/adr/`; for echo-prismatic, the ADR vault + visual-grammar + source-reference-status-policy + ORG-076/050/080; for other projects, whatever hand-curated docs act as the founding surface) — verify that the change does not contradict any of them, and that it aligns with the principles they express.

**When it fires.** Any change touching architectural surfaces (schemas, protocols, contracts, spawn/lifecycle mechanisms, role boundaries, authority topologies). Any change to files the vault names as authoritative. Any change where a smoke-passing diff could still violate a founding invariant.

**What it catches.** Structural drift that smoke tests can't detect — a fix that passes tests but violates a principle the tests weren't written to enforce (e.g. authority-topology change that leaves all tests green because they never asserted the topology). Consequential-but-invisible violations of ratified spec (e.g. treating "APPROVED base" and "reference-not-normative content" as equivalent because they both parse). Silent framing drift ("on behalf of" persona semantics where ORG-050 explicitly rules against forwarding-as-impersonation).

**Anti-pattern.** Reviewer confirms tests green and moves to APPROVE without independently reading the vault docs the change interacts with. "Tests pass" is a necessary condition, never a sufficient one on vault-adjacent surfaces.

**Discipline.** Reviewer names the specific vault files interacting with the change in their report, cites specific lines/clauses that either support or would-contradict the change, and produces a contradiction-check ("no contradiction found against X:line" is a first-class review output, not implicit).

**Worked example (T-3308 → T-3323 arc, 2026-08-27).** Internal reviewer applied per-blocker-match only against Consultant round-1's 5 items and returned APPROVE at 6cb1d60ef7. Consultant's independent round-2 review applied both per-blocker-match AND architecture-vault (against source-reference-status-policy.md + visual-grammar.md + ORG-076/050/080) and REBOUND'd with 5 fresh founding-principle-drift blockers the per-blocker-match discipline had caught none of. Lesson: the two methodologies are structurally different; a reviewer briefed on only per-blocker-match cannot surface architecture-vault defects even when they're staring at the diff.

### 3. Rendered-surface-legibility

**Definition.** Given the artifact's user-facing rendered surface (a diagram's on-canvas visible text, a UI's actual paint output, a document's rendered markdown, a skill's rendered SKILL.md, a report's rendered PDF), verify that the RENDERED surface — what an operator or reader actually sees — carries the substance the change claims to deliver, coherently, on the rendered artifact itself, not merely in surrounding prose or schema.

**When it fires.** Any change where the surface-of-consumption differs from the source-of-authorship: rendered diagrams (Excalidraw canvases, mermaid), rendered documents (a manifest → SKILL.md render), rendered UIs (component paint), any pipeline where a `render.py`/`build`/`compile` step produces the visible artifact.

**What it catches.** Content that lives in schema/source but never reaches the rendered artifact (e.g. `must_not_become` field declared in JSON but never emitted as an on-canvas text element — Consultant's T-3308 finding 1a). Rendering pipelines that silently drop content (blank-line block-boundary in an extractor dropping list continuations — T-3339 Phase 1 reviewer round-1). Rendered artifacts that pass shape-validation but read as incoherent to a human ("Personas MUST NOT:" with nothing following; a rhetorical question with no answer). Cases where the reviewer's schema-check passes but the rendered artifact is user-hostile.

**Anti-pattern.** Reviewer inspects source YAML/JSON/markdown-source and confirms it's well-formed + tests pass, without ever rendering + reading the actual artifact a human will see. "The manifest declares X" is not the same as "X is legibly present in the rendered output."

**Discipline.** Reviewer renders the artifact (or verifies the rendered artifact directly), reads it as a downstream consumer would, and reports whether the rendered surface carries the substance. For diagram changes: does the on-canvas visible text carry the claimed content? For manifest changes: does the rendered SKILL.md read coherently to a fresh spawn? For UI changes: does the paint output show the field?

**Worked example (T-3339 Phase 1 review-cycle, 2026-08-30).** Impl's block-extraction primitive was schema-valid, tests-green, and produced byte-idempotent output — all shape checks passed. Reviewer's rendered-surface-legibility check found 3 truncations invisible to the shape checks: D14 ended `"Personas MUST NOT:"` with the 3 rule bullets orphaned; mcp-adoption ended `"The three live classes:"` with the enumeration missing; goal-mapping shipped as a rhetorical question with no answer. All 3 rendered surfaces read as broken to a human even though every schema check passed. The lens catches what shape-check cannot.

### 4. Dynamic-invocation (live-repro)

**Definition.** Given code with real, destructive side effects on real state (git resets, filesystem removals, force-pushes, DELETE statements, live-network POSTs), verify by ACTUALLY INVOKING it in a controlled environment where the side effect would matter — reflog / audit-log / process-level evidence of non-invocation, not just source reading or happy-path smoke. Static + smoke passes prove internal consistency; they do not prove safety against invocation shapes the tests don't cover.

**When it fires.** Any change where the code under review:
- Calls destructive shell operations (`git reset --hard`, `rm -rf`, `truncate`, force-pushes, `DELETE`) with a cwd or path argument that isn't a tempfile literal.
- Depends on runtime-scoped isolation (pytest autouse, unittest setUp) rather than module-level constants or constructor injection.
- Has a failure mode that is silent (reflog-recoverable but LOOKS LIKE data loss to a naive observer).
- Could be fired against unintended state by a completely standard command a human or automation would run without thinking (crucially: **the test suite invocation itself is one of those invocations** — running the tests can EXECUTE the side effect against whatever real state the test process sits on).

**What it catches.** Destructive paths that fire under invocation shapes the tests don't cover. Fixture claims that don't match fixture behavior ("this autouse fixture prevents X" — but under pytest-xdist timing-race, X fires anyway). Cases where the smoke loop itself IS the failure vector.

**Anti-pattern.** Reviewer confirms "no test fires the destructive path" from a `grep` of test files, or "the fixture excludes X" from reading the fixture code, without instrumenting a real invocation and watching whether the side effect actually fires. Mason's rigorous grep on T-3782 concluded "no test resets the worktree" — but the grep was scoped narrowly and missed 2 tests plus the pytest-xdist timing-race. The traceback from live-repro was authoritative where the grep was not.

**Discipline.** Reviewer runs the code (or the test suite that invokes it) in a controlled environment — a dedicated `/tmp/<repo>-review` worktree, an `agent/*` branch worktree, a container — with reflog / audit-log / subprocess-interceptor instrumentation. Reports the invocation shape tried + the evidence of firing-or-not-firing. Dispatcher-side: reviewer brief SHOULD name the destructive call site in "failure surface", provide a nukeable fixture worktree, include an axis for "does the test suite invocation itself fire the destructive path against unintended state", and MUST NOT treat "smoke loop green" as sufficient safety evidence when the smoke loop could BE the failure vector.

**Worked example (T-3782 round 4, 2026-09-15).** The fence-lifecycle code called `git reset --hard origin/main` on divergent-HEAD detection. Static-read + smoke covered axes A-H (predicate correctness, atomic persistence, autouse fixture existence, etc.) — all 8 passed on static analysis. Axis H (autouse fixture verification) is where the reviewer went beyond "read the fixture, check what it excludes" and asked: does the fixture actually PREVENT the destructive path from firing during real test invocations? Live-repro against a real worktree with reflog watching caught the reset firing at 22:09:03Z under plain unittest AND at 22:32:50Z under xdist-parallel pytest. Neither the static reading nor the fixture-code-inspection would have surfaced the pytest-xdist nondeterminism — it required live-repro to see the timing-race. HIGH-severity bug that would have shipped clean on the other three lenses. Reference: [`lessons/2026-09-16-reviewer-live-repro-when-code-has-real-side-effects.md`](../../lessons/2026-09-16-reviewer-live-repro-when-code-has-real-side-effects.md).

## How to apply — the combinations

- **Light bug-fix, isolated function, no vault surface, no rendering pipeline, no destructive side effects:** per-blocker-match is sufficient. Don't over-review.
- **Any change touching a rendered artifact (diagram, UI, skill, PDF, doc-render):** per-blocker-match + rendered-surface-legibility, minimum. Skip architecture-vault only if the change is confined to render mechanics + doesn't shift authority/topology.
- **Any change touching schema, protocol, spec, contract, architecture:** per-blocker-match + architecture-vault + rendered-surface-legibility. Architecture-vault is load-bearing.
- **Any change touching destructive shell / filesystem / network / DB operations with non-tempfile paths, or any change whose test isolation depends on runtime-scoped fixtures:** add dynamic-invocation (live-repro) to whatever combination the change would otherwise warrant. This is the lens whose absence let T-3782's HIGH bug through 8 axes of static+smoke.
- **REBOUND-cycle round-N:** always all four lenses named in the prior round, plus a per-blocker-match against the round-N-1 blockers, plus any lens the current diff surfaces.

## Reviewer brief SHOULD name

- Which of the four lenses are in scope for this review.
- The specific vault files (if architecture-vault is in scope) the change interacts with.
- The specific rendered artifacts (if rendered-surface-legibility is in scope) to inspect.
- The specific destructive call sites (if dynamic-invocation is in scope) that the live-repro axis targets, plus a nukeable fixture worktree the reviewer can spend on.
- The prior-round blockers (if per-blocker-match is in scope) as an enumerated list.

## Reviewer report SHOULD produce

- Per criterion, the decisive check run and its quoted result, or UNVERIFIED with the reason it could not be run.
- Per-lens verdict — never a monolithic "APPROVE"/"REBOUND" that doesn't distinguish which lens surfaced what.
- Explicit contradiction-check results ("no contradiction found against X:line" / "contradiction at Y:line").
- For rendered surfaces: the rendered content the reviewer inspected, quoted or linked.
- Security or privacy findings carry their ORG-105 §4 chain (foothold, reachable path, asset impact). An incomplete chain is returned to its author, or noted out of scope on its ticket (ORG-105 §4); it is never rated (T-5288).

## Re-evaluate when

- Every ~10 reviews, sample-check whether reviewers are applying the right lens combinations (per the naming above) or defaulting to per-blocker-match regardless of what the brief scoped.
- Any REBOUND cycle where a lens NOT in the reviewer's briefed scope surfaces a defect — the brief's lens-scoping was under-set; adjust the SOP or the brief pattern.
- Any principle change to `rulebook/principles.md` that adds a new founding constraint the architecture-vault lens needs to know about.
