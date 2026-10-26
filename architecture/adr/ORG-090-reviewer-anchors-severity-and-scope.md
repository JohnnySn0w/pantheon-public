---
aliases: [ORG-090]
id: ORG-090
title: "Reviewer anchors for spec-review — severity taxonomy + scope-bounds + APPROVE-with-nit + HO filter (sibling to ORG-089 drafter anchors)"
status: "Ratified 2026-09-06 by the operator via TG 9081 ('org 90 looks good to me. Ratified.'). Author of record: Orchestrator (HO), approach B (new ORG-NNN, not amend ORG-041 in-place), per the operator pick TG 9076 (2026-09-06T03:46Z). Sibling to ORG-089 (drafter-side altitude anchors); extends ORG-041 (Codex-as-reviewer flow) with the reviewer-side taxonomy + APPROVE-with-nit variant + HO/dispatcher filter that ORG-041's 3-round loop was missing."
date: 2026-09-06
author: Orchestrator (HO)
appetite: 6
build_disposition: no-build-needed
scoping_ticket: T-3439
relates: [ORG-041, ORG-089, T-3439, T-3431]
---

# ORG-090 — Reviewer anchors for spec-review

**Status:** Ratified 2026-09-06 by the operator via TG 9081 **Date:** 2026-09-06 **Author:** Orchestrator (HO) **HITL:** Autonomous **Related:** [[ORG-041]], [[ORG-089]], [[T-3439]], [[T-3431]]

**Appetite:** 6/10

## Context

[[ORG-041]] (Ratified 2026-07-11 by the operator via TG 5624) established Codex as the standing adversarial spec-reviewer with a default 3-round cap and cap-exhaustion escalation. It named the flow but did NOT specify how reviewers should CLASSIFY findings, what SCOPE-BOUNDS govern the review, or how HO/dispatchers should FILTER findings on the way back.

Session 2026-09-06 surfaced the gap. The operator steer TG 9034 + 9036 (during the ORG-089 rev-3 arc): *"does consultant have anchors and bounds for how to determine what to critique in a review"* → *"yeah this needs work."* Motivating exhibit is the ORG-089 rev-2 balloon (218 → ~500 lines when HO accepted every Consultant REBOUND wholesale, corrected via the operator TG 9040 rev-3 revert to design-only scope). Session-cumulative pattern: 4 verdicts today (ORG-089 rev-3 + ORG-071 WITH-TRIM + ORG-088 shape-rewrite + ORG-072 WITH-RELOCATION) all steered by the same altitude discipline — the reviewer + HO filter now needs a formal framework so upstream discipline enforces itself rather than requiring the operator's intervention.

This ADR is the reviewer/dispatcher counterpart to [[ORG-089]]'s drafter anchors (Appetite + Riskiest-assumption), operating on the same altitude discipline (`sop/standard/ratifiable-adr-shape.md`).

## Appetite

**Score: 6/10** (value 6 / strategic 7 / reversibility 6, average 6.33).

This is a bounded methodology addition to an already-ratified flow ([[ORG-041]]). What it's worth NOW: prevents the "accept every finding as ratifiable" failure mode (real dollar cost was HO time + the operator TG interventions across today's arcs); doesn't unlock anything net-new. Strategic: same discipline class as ORG-089's drafter anchors + the altitude/clutter SOP rules landed today — completes the review-loop side of the discipline. Reversibility: convention-tier, no code + no infra change; a revert would be a doc revert.

Explicit ceiling: SEVERITY taxonomy + SCOPE-BOUNDS + APPROVE-WITH-NIT variant + HO FILTER. **Not-in-scope:** enforcement in tooling (auto-classification, severity linter, verdict-schema JSON) — deferred; scoring rubric for reviewer performance; obligation for reviewer to file build tickets for OUT-OF-SCOPE findings (they name the concern, HO/author decides ticket-worthiness). If enforcement-in-tooling becomes needed, that's a separate arc, not scope creep here.

## Riskiest assumption

**Reviewers can self-classify findings by severity + scope without additional tooling — a docs-tier convention is enough discipline.**

Cheap test / mitigation: apply the taxonomy on the next 3-5 Consultant/Codex review passes; observe whether reviewers cluster at one severity, fail to distinguish scope, or ignore the taxonomy entirely. If reviewers can't self-classify usefully, the taxonomy needs enforcement (tooling gate at report boundary, or manual gate in HO's dispatch brief). Re-evaluate at 5 review-passes post-ratify; if the self-classification signal is noisy or absent, file the enforcement follow-up as its own arc.

## Decision

### D-1 — Severity taxonomy for findings

Reviewers self-classify each finding into one of 4 levels:

- **HIGH** — design-invalidating (blocks ratify). The spec's decision cannot be ratified as drafted; the finding names a real gap in the DESIGN that would break the decision. E.g., "the mechanism as described cannot be atomic under concurrent writers, but the invariant assumes atomicity."
- **MED** — enforceability gap (design-tier but not blocking; author's judgment on how to close). The design decision is sound, but an enforceability path is underspecified. E.g., "the invariant is stated but no wire-level surface visibly enforces it — where does that fire?"
- **LOW** — style/prose/nit (not blocking; APPROVE-with-nit-noted). Minor readability, non-load-bearing prose issues. E.g., "typo in the third paragraph of §Decision."
- **OUT-OF-SCOPE** — belongs in a build ticket or referenced note, NOT the ratifiable ADR body. E.g., "no method signature for `_foo()` is given" — mechanism-tier detail is deferred to build tickets per the altitude rule (`sop/standard/ratifiable-adr-shape.md`).

*Justifies-via:* [[ORG-041]] extension + P4 (traceability: severity-classified findings are auditable) + P1 (mechanism-over-convention: a discrete taxonomy vs "reviewer intuition").

### D-2 — Scope-bounds for reviewers

Reviewer scope is the **ratifiable ADR body as drafted**. Out of scope for the reviewer:

- REBOUND logs, review-round notes (belong in companion `notes/<date>-<slug>-review-history.md` per clutter rule).
- Build-tier detail (belongs in build tickets or referenced notes per altitude rule).
- Related ADRs' contents (cite + note, don't drift into re-reviewing them).
- Frontmatter fields (linters own frontmatter; reviewer flags substance in the body only).

A finding that touches out-of-scope content is legitimate to raise but self-classifies as OUT-OF-SCOPE per D-1 — not HIGH or MED.

*Justifies-via:* [[ORG-041]] + `sop/standard/ratifiable-adr-shape.md` (altitude + clutter rules already ratified) + P7 (minimal-footprint reviewer scope — one document, at design altitude).

### D-3 — Verdict vocabulary — APPROVE-with-nit variant

Reviewer verdicts:

- **APPROVE** — no findings, or findings only OUT-OF-SCOPE (raised, not blocking).
- **APPROVE-WITH-NIT-NOTED** — LOW findings only; do NOT REBOUND, do NOT block ratify; nit-list annotated on the ratify ticket for the author's optional address.
- **REBOUND** — at least one HIGH or MED finding; author addresses + resubmits per [[ORG-041]]'s existing loop.
- **CAP-EXHAUSTED** — 3 rounds complete with unresolved HIGH; ratification ticket carries residuals verbatim for the operator's disposition (unchanged from [[ORG-041]]).

*Justifies-via:* [[ORG-041]] (extends its 3-round rule with APPROVE-with-nit to prevent LOW-only findings from triggering spurious REBOUND cycles).

### D-4 — HO/dispatcher filter on reviewer findings

When the reviewer returns findings, HO (or the project-orchestrator equivalent) applies:

- **HIGH** → accept + REBOUND to author.
- **MED** → accept + REBOUND (author's judgment on how to close).
- **LOW** → APPROVE-with-nit; do NOT REBOUND for body edit.
- **OUT-OF-SCOPE** → do NOT REBOUND for ADR body change; open a build ticket or referenced-note followup if the concern is real; the ratifiable body stays clean.

This filter replaces the failure mode "accept every finding as ratifiable" — which grew ORG-089 rev-2 from 218 → ~500 lines by accepting every REBOUND wholesale, corrected via the operator TG 9040 rev-3 revert. HO's filter is the enforcement point; reviewer-severity is advisory input to it, not binding.

*Justifies-via:* [[ORG-041]] + `sop/standard/ratifiable-adr-shape.md` §altitude gate + P7 (minimal-footprint author response — not every finding needs a body-inline fix).

## Consequences

**positive:**
- Reviewers have a discrete + auditable classification framework instead of "reviewer intuition."
- LOW-only findings don't spuriously trigger REBOUND rounds; APPROVE-with-nit preserves the note without churn.
- HO/dispatcher's filter is explicit; ADR authors know what will be REBOUND'd vs relocated to build vs approved-with-nit.
- Reviewer scope is bounded to the ratifiable body; review clutter + build-tier fishing stay outside.
- Completes the review-loop side of the altitude discipline (drafter side = ORG-089 + shaping-and-viability SOP; review side = this ADR).

**negative:**
- Introduces classification-work at review time (bounded — 4 levels, per-finding; reviewer picks by inspection, not scoring).
- Reviewer-severity may undershoot (self-classify LOW when actually MED); countered by HO's filter step treating reviewer-severity as advisory, not binding.
- Convention-tier without tooling enforcement — a reviewer who ignores the taxonomy stays uncaught until HO reviews the output.

**deferred:**
- Enforcement in tooling (auto-classification prompt, severity linter, verdict schema JSON, report-boundary gate that refuses uncategorized findings).
- Scoring rubric for reviewer performance across arcs.
- Obligation for reviewer to auto-file build tickets for OUT-OF-SCOPE findings (proposed: no — reviewer names the concern, HO/author decides ticket-worthiness).
- Extending this ADR beyond Consultant/Codex to Fable + Claude-tier reviewers (they use the same review flow; the anchors apply, but formalizing that composition is a separate pass).

**open questions:**
- Should OUT-OF-SCOPE findings receive an explicit acknowledgement in the reviewer's report (so authors know they were seen + intentionally deferred, not silently dropped)? Proposed: yes, as a "noted, out-of-scope for ratify body" line per finding; author decides follow-up.

**relates to tensions:** the altitude rule (`sop/standard/ratifiable-adr-shape.md`) fires at DRAFTING; this ADR fires at REVIEW; both defend against build-tier detail crowding ratifiable ADR bodies. Together with [[ORG-089]]'s drafter anchors (Appetite + Riskiest-assumption) they form a coherent altitude-discipline surface across the full author→review→ratify loop.

## Review + build history

Where the anchors were sourced (the 2026-09-06 altitude- and clutter-rule cases, and the reviewer-filter failure mode): `notes/2026-10-08-org-090-review-history.md`.

Build disposition: `no-build-needed` — this is convention-tier methodology captured in the ADR body + the SOP surfaces already ratified (`sop/standard/ratifiable-adr-shape.md` + `sop/standard/adr-to-buildout-planning.md` + `sop/standard/plan-first-dispatch.md`). No code lands from this ADR; discipline lands via reviewer briefs applying the taxonomy + HO briefs applying the filter.
