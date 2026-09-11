# Reviewer methodologies — the three-lens review

## When to use this

Every reviewer dispatch of substance — post-implementation code review, cross-clone artifact review, fold-review, ADR/spec review. Load the appropriate methodologies against the change under review and apply them independently. Not every review needs all three lenses (light bug-fixes typically warrant only per-blocker-match), but the reviewer brief SHOULD name which lenses are in scope, and reviewers SHOULD flag when a lens they weren't briefed on surfaces a defect the briefed lenses would miss.

**Ratification:** Adopted 2026-08-30 (T-3348 backfill) synthesizing the T-3308 → T-3323 arc lessons. Autonomous per D3 (methodology, below goal-level bar).

## The three methodologies

### 1. Per-blocker-match

**Definition.** Given a specific triggering defect (a prior REBOUND round's cited items, a filed bug's stated failure, a plan's named acceptance criteria), verify that the change addresses THAT defect — the exact items, the exact acceptance criteria — not merely a plausible-looking or superficially-related fix.

**When it fires.** Every round-N REBOUND cycle. Every plan-referenced acceptance test. Every filed-bug fix. Any review where a prior round produced enumerated blockers.

**What it catches.** Fixes that pass smoke but miss the actual defect (e.g. impl "addressed" the reviewer's item 3 by adding validation elsewhere, but the specific test case in item 3 still fails). Fixes that partially satisfy an enumerated list (5 blockers listed, 3 addressed, 2 silently deferred). Fixes that resolve the surface complaint but not the underlying class.

**Anti-pattern.** Reviewer accepts the implementer's own framing of "which item I fixed" without independently reading each cited blocker and confirming the fix touches it.

**Discipline.** Reviewer keeps a checklist of the enumerated items from the trigger, and each item is marked verified/unverified against the diff — never marked verified by transitivity ("the whole area was refactored, so this is probably covered").

### 2. Architecture-vault (founding-principle check)

**Definition.** Given the project's hand-curated architectural documents — the "vault" (per project: for Pantheon, `rulebook/principles.md` + `rulebook/directives.md` + `adr/`; for echo-prismatic, the ADR vault + visual-grammar + source-reference-status-policy + ORG-076/050/080; for other projects, whatever hand-curated docs act as the founding surface) — verify that the change does not contradict any of them, and that it aligns with the principles they express.

**When it fires.** Any change touching architectural surfaces (schemas, protocols, contracts, spawn/lifecycle mechanisms, role boundaries, authority topologies). Any change to files the vault names as authoritative. Any change where a smoke-passing diff could still violate a founding invariant.

**What it catches.** Structural drift that smoke tests can't detect — a fix that passes tests but violates a principle the tests weren't written to enforce (e.g. authority-topology change that leaves all tests green because they never asserted the topology). Consequential-but-invisible violations of ratified spec (e.g. treating "APPROVED base" and "reference-not-normative content" as equivalent because they both parse). Silent framing drift ("on behalf of" persona semantics where ORG-050 explicitly rules against forwarding-as-impersonation).

**Anti-pattern.** Reviewer confirms tests green and moves to APPROVE without independently reading the vault docs the change interacts with. "Tests pass" is a necessary condition, never a sufficient one on vault-adjacent surfaces.

**Discipline.** Reviewer names the specific vault files interacting with the change in their report, cites specific lines/clauses that either support or would-contradict the change, and produces a contradiction-check ("no contradiction found against X:line" is a first-class review output, not implicit).

**Worked example (T-3308 → T-3323 arc, 2026-08-27).** Internal reviewer applied per-blocker-match only against Cairn round-1's 5 items and returned APPROVE at 6cb1d60ef7. Cairn's independent round-2 review applied both per-blocker-match AND architecture-vault (against source-reference-status-policy.md + visual-grammar.md + ORG-076/050/080) and REBOUND'd with 5 fresh founding-principle-drift blockers the per-blocker-match discipline had caught none of. Lesson: the two methodologies are structurally different; a reviewer briefed on only per-blocker-match cannot surface architecture-vault defects even when they're staring at the diff.

### 3. Rendered-surface-legibility

**Definition.** Given the artifact's user-facing rendered surface (a diagram's on-canvas visible text, a UI's actual paint output, a document's rendered markdown, a skill's rendered SKILL.md, a report's rendered PDF), verify that the RENDERED surface — what an operator or reader actually sees — carries the substance the change claims to deliver, coherently, on the rendered artifact itself, not merely in surrounding prose or schema.

**When it fires.** Any change where the surface-of-consumption differs from the source-of-authorship: rendered diagrams (Excalidraw canvases, mermaid), rendered documents (a manifest → SKILL.md render), rendered UIs (component paint), any pipeline where a `render.py`/`build`/`compile` step produces the visible artifact.

**What it catches.** Content that lives in schema/source but never reaches the rendered artifact (e.g. `must_not_become` field declared in JSON but never emitted as an on-canvas text element — Cairn's T-3308 finding 1a). Rendering pipelines that silently drop content (blank-line block-boundary in an extractor dropping list continuations — T-3339 Phase 1 reviewer round-1). Rendered artifacts that pass shape-validation but read as incoherent to a human ("Personas MUST NOT:" with nothing following; a rhetorical question with no answer). Cases where the reviewer's schema-check passes but the rendered artifact is user-hostile.

**Anti-pattern.** Reviewer inspects source YAML/JSON/markdown-source and confirms it's well-formed + tests pass, without ever rendering + reading the actual artifact a human will see. "The manifest declares X" is not the same as "X is legibly present in the rendered output."

**Discipline.** Reviewer renders the artifact (or verifies the rendered artifact directly), reads it as a downstream consumer would, and reports whether the rendered surface carries the substance. For diagram changes: does the on-canvas visible text carry the claimed content? For manifest changes: does the rendered SKILL.md read coherently to a fresh spawn? For UI changes: does the paint output show the field?

**Worked example (T-3339 Phase 1 review-cycle, 2026-08-30).** Impl's block-extraction primitive was schema-valid, tests-green, and produced byte-idempotent output — all shape checks passed. Reviewer's rendered-surface-legibility check found 3 truncations invisible to the shape checks: D14 ended `"Personas MUST NOT:"` with the 3 rule bullets orphaned; mcp-adoption ended `"The three live classes:"` with the enumeration missing; goal-mapping shipped as a rhetorical question with no answer. All 3 rendered surfaces read as broken to a human even though every schema check passed. The lens catches what shape-check cannot.

## How to apply — the combinations

- **Light bug-fix, isolated function, no vault surface, no rendering pipeline:** per-blocker-match is sufficient. Don't over-review.
- **Any change touching a rendered artifact (diagram, UI, skill, PDF, doc-render):** per-blocker-match + rendered-surface-legibility, minimum. Skip architecture-vault only if the change is confined to render mechanics + doesn't shift authority/topology.
- **Any change touching schema, protocol, spec, contract, architecture:** all three lenses. Architecture-vault is load-bearing.
- **REBOUND-cycle round-N:** always all three lenses that were named in the prior round, plus a per-blocker-match against the round-N-1 blockers, plus any lens the current diff surfaces.

## Reviewer brief SHOULD name

- Which of the three lenses are in scope for this review.
- The specific vault files (if architecture-vault is in scope) the change interacts with.
- The specific rendered artifacts (if rendered-surface-legibility is in scope) to inspect.
- The prior-round blockers (if per-blocker-match is in scope) as an enumerated list.

## Reviewer report SHOULD produce

- Per-lens verdict — never a monolithic "APPROVE"/"REBOUND" that doesn't distinguish which lens surfaced what.
- Explicit contradiction-check results ("no contradiction found against X:line" / "contradiction at Y:line").
- For rendered surfaces: the rendered content the reviewer inspected, quoted or linked.

## Re-evaluate when

- Every ~10 reviews, sample-check whether reviewers are applying the right lens combinations (per the naming above) or defaulting to per-blocker-match regardless of what the brief scoped.
- Any REBOUND cycle where a lens NOT in the reviewer's briefed scope surfaces a defect — the brief's lens-scoping was under-set; adjust the SOP or the brief pattern.
- Any principle change to `rulebook/principles.md` that adds a new founding constraint the architecture-vault lens needs to know about.
