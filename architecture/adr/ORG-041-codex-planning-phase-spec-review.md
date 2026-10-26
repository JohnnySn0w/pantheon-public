---
aliases: [ORG-041]
title: Codex as turnkey planning-phase spec reviewer (adversarial review loop before ratification)
status: RATIFIED 2026-07-11 by the operator (TG 5624, stated directly; this ADR records the mechanism)
date: 2026-07-11
author: Orchestrator (Orchestrator)
relates: ORG-031 (Codex worker-harness, Option A), ORG-038 (Codex S3* assurance auditor — the OTHER codex function; distinct channel), ORG-027 (orchestrator tiers), D7 (team SOPs / implementation flow), feedback_ratify_tickets_need_ratifiable_files, ORG-089 (deference heuristic — planning-phase review checklist gains the three viability questions per §Amendment 2026-09-06)
build_disposition: built
---
# ORG-041 — Codex as turnkey planning-phase spec reviewer

**Status:** Ratified 2026-07-11 by the operator (TG 5624: "treat codex as the turnkey reviewer going forward… for planning phase… Fable generating spec, then codex reviewing (on Sol preferably), going through a few rounds (3?) and then ticketing for ratification. After that, standard implementation flow.") This ADR records the mechanism and defaults; the flow itself is the operator's.

## Context

Spec/planning artifacts (ADRs, project specs, arc plans) have until now gone HO-draft → operator-ratify, with codex consulted ad-hoc for falsifiable detail-checks (the narrow fit recorded in `feedback_codex_second_opinion_fit`). The operator's directive upgrades codex to the **standing adversarial reviewer of the planning phase**: every spec gets a different-model-tier review loop BEFORE it reaches the ratification ticket. Independence rationale mirrors ORG-038: codex runs a different model + harness from the Claude tier that drafts the specs — genuine cross-model scrutiny, cheap (ORG-018: Plus-quota compute).

## Decision — the flow

**Planning phase:**
1. **HO (Fable) drafts the spec** (ADR / project spec / arc plan). HO holds the pen throughout.
2. **Codex reviews adversarially** — dispatched per ORG-031 Option A (codex never holds the arc). Model: **"Sol" preferred** (the operator's naming, TG 5624); operationalization = codex-side model selection (see §Wiring). Review brief asks for: unsound assumptions, missing failure modes, underspecified interfaces, cheaper-shape alternatives, and explicit "no remaining substantive objections" when clean.
3. **Iterate, ≤3 rounds** (default cap, the operator's "(3?)" adopted as the number; HO may stop early when codex returns no remaining substantive objections — most specs should exit in 1–2). **Cap-exhaustion rule (mirrors the 3-REBOUND budget):** if round 3 still carries an unresolved substantive objection, the disagreement is NOT silently dropped — it goes into the ratification ticket verbatim as a named open point for the operator's eyes.
4. **Ticket for ratification** — standard ratify ticket with `ratifiable_files` (committed spec diff, never prose-only), noting rounds used and codex's final verdict.

**Implementation phase (unchanged, D7):**
5. HO spawns a **project-orchestrator (opus tier)** which parts the ratified spec out to **implementer/reviewer workers (sonnet tier)**. Codex remains available as an ORG-031 implementation worker where dispatched.

## Scope

- Applies to any spec that would get a ratification ticket — i.e. the planning phase of substantive builds. Goal-neutral maintenance, trivial fixes, and operational runbooks stay outside the loop (HO judgment, same altitude as the msg-3733 goal-level bar).
- Codex is the **reviewer, not the decider**: HO integrates or rebuts each round with justification; the operator ratifies. (This supersedes the "detail-checks only" narrowness of the old fit-pin, not its "not-decider" core — pin updated same-day.)
- Distinct from ORG-038: S3* audits verify deployed reality on a cadence; this reviews plans pre-ratification. Same worker, two contracted channels — no conflict because neither channel self-closes and HO routes both.

## Wiring (implementation notes)

- Tooling: `codex_second_opinion` takes a per-call `model` arg (T-1685, landed 2026-07-12; model stamped into the call log). **Sol = gpt-5.6 (the operator, TG 5637)** — NOT yet available on the ChatGPT-account Codex path (live-tested 2026-07-12: 400 invalid_request). **Interim review model: gpt-5.5** (account frontier). No availability probing (the operator, TG 5640) — switch to gpt-5.6 when he says it's live.
- Review context: pass the spec FILE path via `context_path` (full contents), not prose-summaries — codex reviews the artifact, not HO's gloss of it.
- Each round's codex verdict + HO disposition (integrated / rebutted-with-justification) is kept with the spec (ADR appendix or ticket transition log) — P4 traceability for the ratification reader.

## Amendment 2026-09-06 — RECORD-mode viability-question checklist (Ratified via [[ORG-089]] TG 9047)

Per [[ORG-089]] §Decision.1-2 (deference heuristic + appetite circuit breaker, Ratified 2026-09-06 by the operator via Telegram 9047): every non-vault ADR under this review loop carries populated `appetite:` frontmatter + §Appetite + §Riskiest assumption body sections (RECORD-mode, unconditional). Consultant's ORG-041 review adds the three viability questions to the adversarial planning-phase checklist:

1. **Riskiest assumption + cheap test** — what unknown would kill the bet, and how cheaply can it be probed before commit?
2. **Appetite / is-this-worth-it-now** — what depth and mechanism is warranted right now, measured against outcome value, strategic alignment, and reversibility (per `knowledge/episodic/2026-09-05-deference-heuristic-proposal/appetite-scale.md`).
3. **Strongest genuine alternative + why-not** — the best other option a serious adversarial reader would raise, and the reason not to take it.

**SURFACE-mode** (§ORG-089 §Decision.1) fires only for directives on the D3 reserved set; SURFACE routes results to the operator via the ratification-ticket surface, unchanged from the pre-amendment flow.

Consultant's review checklist verifies: fields populated (§Appetite + §Riskiest assumption present + `appetite:` frontmatter integer 1-10); the three questions substantively answered (not boilerplate); pass-outcome coherent with the ADR's actual claims. Empty/boilerplate fields on a ratified non-vault ADR trip the grooming-completeness check ([[ORG-089]] §Surface 4a-completeness).

Scope-of-critique bounds for the ORG-041 review lane itself are followed up separately in T-3439 (reviewer-anchors sibling arc to ORG-089).

## DECISIONS

- **3-round default cap with early exit + surface-on-exhaustion** — serves the operator's stated flow — justifies via his "(3?)" + the existing 3-REBOUND escalation-budget shape (B5); disagreement surfacing via the ratify ticket keeps D1 (the operator sees only the residue that needs him).
- **Codex reviews the committed artifact via context_path, not a summary** — goal-neutral correctness — justifies via P4 + the ratifiable-files rule (review what will be ratified, not a paraphrase).
- **Flow adopted before model-preference wiring lands** — serves velocity — justifies via P7 (cheap action: the loop is runnable today on the default model; Sol is a config fast-follow, T-1685).
