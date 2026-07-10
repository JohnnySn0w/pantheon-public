---
aliases: [ORG-015]
title: Ratifiable document class allow-list
status: Ratified 2026-05-17 by the operator (T-0400 verdict 17:04Z).
date: 2026-05-17
---
# ORG-015 — Ratifiable document class allow-list

**Status:** Ratified 2026-05-17 by the operator (T-0400 verdict 17:04Z).

**Date authored:** 2026-05-17

**Author:** Orchestrator (Orchestrator) — surfaced from the operator Telegram msg_1614 (2026-05-17 15:38Z) "notes should not be submitted as ratifiable documents" + follow-up msg_1618 (15:54Z) "ratifiable files being adrs and maybe some other types is probably an architectural decision tbqh, not a memory pin." First message drove the T-0398 → ORG-014 notes→ADR promotion same session. Second message reframes the rule itself as ADR-tier, declining HO's initial memory-pin instinct.

**Related:** [[T-0398]] (ORG-014 ratification, done; precedent for the notes→ADR pattern), [[ORG-014]] (RequestSpawn primitive — the artifact that surfaced this question), [[ORG-001]] (ledger format; one of the document classes considered + excluded), [[ORG-003]] (ticket system; another class considered + excluded), [[ORG-002]] (principles+directives split — the rulebook framework this decision intersects), msg_1614 + msg_1618 + msg_1619.

**HITL:** Authored

## Context

The Pantheon org has accumulated ~14 document classes across the repo: `adr/`, `rulebook/principles.md`, `rulebook/directives.md`, `rulebook/policies/`, `sop/standard/`, `sop/remediation/`, `notes/`, `ledger/`, `tickets/`, `incidents/`, `bugs/`, `lessons/`, `knowledge/watch/reports/`, `knowledge/episodic/`. Each class serves a distinct epistemic role in the org's substrate.

Ratification (the operator's "ratified" verdict) flips a document's status from Authored/Draft to operationally binding. Pre-this-ADR, the ratification flow has been driven by the `ratifiable_files: [<repo>:<path>]` frontmatter on ratification tickets (`target=operator`) — the operator reads the referenced file in the PWA, posts a verdict on Telegram, HO/Recovery lift the document's status.

The implicit assumption to date was "any path can be a ratifiable_files target." Today's chain made the gap concrete:

- T-0174 → T-0398 (RequestSpawn design): HO drafted at `notes/2026-05-17-request-spawn-primitive-design.md`, filed T-0398 with that path as the ratification target. The operator ratified the reasoning (msg_1615 "I do approve of the reasoning, convert and ratify") but redirected the substrate (msg_1614 "notes should not be submitted as ratifiable documents; this should be an adr tbh"). HO promoted notes→ADR (ORG-014, same session).
- Memory-pin attempt (15:43Z): HO captured the lesson as a `feedback_*.md` memory pin. The operator (msg_1618): "ratifiable files being adrs and maybe some other types is probably an architectural decision tbqh, not a memory pin." → this ADR.

The substantive question the operator's "maybe some other types" surfaces: **which document classes are legitimate ratification targets, and which are working-tier / audit-chain / narrative artifacts that should never be the ratification surface?**

This is a P1-architectural decision (the org's information architecture) + a D1 surface (the operator's bandwidth — wrong-substrate ratifications cost a re-promotion round-trip).

## Relationship to ORG-012

ORG-015 builds on the **author/drafter split** that [[ORG-012]] (information-inflow bubble-up methodology, ratified 2026-05-14) already embeds implicitly. ORG-012 names: *"HO is the primary **author** of `lessons/`, `bugs/`, `incidents/`, `notes/`; the primary **drafter** of `sop/`, `adr/`, directive amendments + principle amendments; the maintainer of `feedback_*.md` memory pins."* That split tracks the ratifiable/not-ratifiable split exactly:

- **"Drafted"** classes (decision-pending-ratification) = ratifiable surface.
- **"Authored"** classes (observation/narrative) = not ratifiable; they support the ratifiable surface by feeding observations that promote upward.
- **"Maintained" memory pins** = HO-internal behavior calibration; not ratifiable as org-wide rules.

ORG-015 is the **operationalization** of ORG-012's implicit split for ratification targets specifically. ORG-012 names the methodology + per-agent author/drafter mapping; ORG-015 makes the ratifiability test explicit + provides the exclusion list for classes ORG-012 doesn't address directly (`ledger/`, `tickets/`, README files, source code — these aren't HO-authored at all but still surface as candidate ratifiable_files paths in practice).

The two ADRs are complementary, not redundant:
- ORG-012 answers *"how does information flow between document classes; who owns each class."*
- ORG-015 answers *"which classes can be the target of a `ratifiable_files` frontmatter on a ratification ticket."*

A future reader who only reads ORG-015 has the ratification-target rule; a reader who only reads ORG-012 has the broader bubble-up methodology. Reading both gives the full picture; either alone is sufficient for its scope.

## Decision (proposed)

### Allow-list — document classes that CAN be `ratifiable_files` targets

| Document class | Path pattern | Why ratifiable | Status-field shape |
|---|---|---|---|
| **ADRs (org-tier)** | `adr/ORG-NNN-*.md` | Canonical durable architectural decision record. Has explicit Authored → Ratified status field. ORG-014 is the most recent example. | `**Status:** Authored YYYY-MM-DD` → `**Status:** Ratified YYYY-MM-DD by the operator msg_XXXX` |
| **ADRs (project-tier)** | `<project>/adr/ADR-NNN-*.md` (e.g., `pantheon-pwa/adr/ADR-050-*.md`, `echo-prismatic/adr/ADR-NNN-*.md`) | Project-scope counterpart per the operator msg_1205 project-ADR convention. Same status-field shape. | Same as above |
| **Principles** | `rulebook/principles.md` (NEW principle proposals) | Top-tier framing rules (P1-P7+). Currently amended via notes/ proposals → ratify → body lift; per the operator's "notes shouldn't be ratifiable" lean, NEW principles should also flow through ADR-shape drafting (the ADR captures the WHY + tensions; principles.md gets the final principle body lifted in). | ADR-driven; principles.md itself doesn't need per-amendment status |
| **Directives** | `rulebook/directives.md` (NEW directive proposals or amendments) | Operational targets (D1-D10+). Same flow as principles — ADR-shape draft → ratify → body lift. | Same as principles |
| **Policies** | `rulebook/policies/<name>.md` (NEW policies or material amendments) | Project-specific calibrations of directives (e.g., `trust-posture.md`, `autonomy.md`). Each policy file has its own Status field. Material amendments to a policy can be ratified in-place via the policy file's own status. | `**Status:** Ratified YYYY-MM-DD by the operator msg_XXXX` (in-file) |
| **SOPs** | `sop/standard/*.md`, `sop/remediation/*.md` (NEW SOPs or material amendments) | Durable how-to / what-to-do-when. Each SOP file has its own Status field. | Same as policies (in-file Status) |

### Exclusion list — document classes that MUST NOT be `ratifiable_files` targets

| Document class | Why excluded |
|---|---|
| `notes/` | Working-tier drafts. Per the operator msg_1614. Promote to ADR if the content warrants ratification. |
| `ledger/2026.jsonl` (or other ledger jsonl) | Audit chain. Records what happened, not what should happen. Decisions are recorded here AFTER ratification on another document. |
| `tickets/{inbox,active,blocked,done}/T-NNNN.md` | State machine entries. Ratification ticket is the request-for-ratification; the ratification target sits in `ratifiable_files`, not the ticket itself. |
| `incidents/<date-Bnn>/` | Postmortems are narrative + analysis. They surface structural-prevention candidates which then become ADRs (see ORG-014 ← b33 incident); the incident itself isn't ratified. |
| `bugs/*.md` | Technical-symptom-with-fix catalog. Reusable gotcha notes; not architectural decisions. |
| `lessons/*.md` | Umbrella names for recurrent pattern observations. Lessons typically point AT a future ADR rather than being ratified themselves. |
| `knowledge/watch/reports/<date>/*.md` | Research findings (Specialist auto-commit). Above-the-bar items lift to ratifiable tickets pointing to ADR drafts; the research report itself isn't ratified. |
| `knowledge/episodic/<topic>/*.md` | Ad-hoc deep-dive research. Same shape as watch/reports — findings lift to ADRs. |
| README.md (any path) | Documentation surfaces. Updates flow via normal PR review, not ratification. |
| Source code (`tools/*.py`, `tools/*.ts`, etc.) | Implementation, not architectural decision. Decisions about source code shape ARE ADR-worthy; the ADR is the ratification target, not the code. |

### The general rule

A document class is ratifiable iff:
1. It carries an explicit Status field (Authored → Ratified) OR it conceptually has one (ADRs are the prototype).
2. The operator's "ratified" verdict makes the document operationally binding (vs. just acknowledged).
3. The document is durable architectural surface, not working tier / audit chain / narrative.

If a candidate document doesn't meet all three, it's not ratifiable — promote the content to a class that does.

### Memory pins / feedback files — explicitly out

Per msg_1618 — durable rules don't live in `~/.claude/projects/-home-claude/memory/feedback_*.md` when they're org-wide architectural surface. Memory pins are HO's per-instance behavior notes; org-wide rules live in directives, policies, principles, or ADRs depending on tier. (Memory pins remain valid for: per-HO-instance behavior calibration, cognitive-load drift catches, user-preference captures that don't change the org's architecture.)

## Consequences

**Positive:**
- Ratifiable_files frontmatter has a stable allow-list; HO drafts straight into the right substrate the first time, no notes→ADR promotion round-trips.
- Decision catalog stays clean — ADRs accumulate as the authoritative decision surface; working drafts in `notes/` remain working drafts.
- Principles + directives amendments flow through ADR-shape drafting (captures the WHY + alternatives + tensions), then body lifts into principles.md / directives.md — current pattern stays the same, but the drafting substrate flips from notes/ to adr/.
- Policies + SOPs ratify in-place via their own Status field — no separate ADR needed for material amendments (the SOP/policy IS the durable surface).
- Future-HO (or fresh-HO post-restart) reads this ADR + the allow-list table without needing to re-derive the rule from past Telegram exchanges.

**Negative:**
- Forces ADR-drafting overhead for things that previously could draft in notes/ casually. Mitigation: ADRs are not heavyweight if the content is genuinely durable (ORG-014 was 141 lines, drafted in ~20 mins).
- Principles + directives amendments now have two artifacts (the ADR + the principles.md / directives.md body lift) where they previously had one notes/ + the lift. Mitigation: the ADR retains the rejected alternatives + tensions; principles.md keeps only the final principle text. The two-artifact shape mirrors `notes/2026-05-17-p7-cheap-action-proposal.md` → `principles.md` P7; just moves the proposal from notes/ to adr/.

**Deferred:**
- Migration of in-flight ratification tickets that already point at notes/ paths — case-by-case at HO discretion. Today's session has zero outstanding (T-0398 already converted, the only other was T-0399 which doesn't have ratifiable_files).
- Whether `ledger/INDEX.md` (the curated ledger index) should be ratifiable — it has a Status field of sorts (groomed periodically) but isn't "binding" in the same sense as an ADR. Lean: no, treat as derived artifact. Surface as follow-up if the operator wants it explicit.

## DECISIONS

- ADRs (both org-tier `ORG-NNN` + project-tier `ADR-NNN`) are the canonical ratifiable surface — justifies via P1 (architectural surface for architectural decisions; symmetric across org + project scopes) — the operator ratified the org-tier shape with ORG-014 promotion same session.
- SOPs + policies + principles + directives amendments included in the allow-list — justifies via P3 (decisions justify upward; if the operator's verdict is needed to make the document binding, it's a ratification target) — completes the operator's "maybe some other types" prompt with the concrete classes.
- Notes/ + ledger/ + tickets/ + incidents/ + bugs/ + lessons/ + knowledge/watch/reports/ + knowledge/episodic/ + README + source code on the exclusion list — justifies via P5 (roles don't hop — each document class has its epistemic role) — the working-tier / audit-chain / narrative classes have valuable distinct roles that ratification would muddy.
- Memory pins explicitly out — justifies via D1 + ORG-012 (information-inflow bubble-up methodology — org-wide rules belong in org-wide substrate, not per-instance HO memory) — the operator's msg_1618 named this directly.
- The "general rule" (3-point test: explicit Status field; the operator's verdict makes binding; durable architectural surface) — justifies via P2 (architecture over policy — give a structural test rather than a policy-by-enumeration that could drift) — enumeration is helpful but the test is the load-bearing piece.

## Open ratify

This ADR is the ratifiable_files target for the follow-up ratification ticket (T-XXXX, filed on draft completion). The operator ratifies the allow-list + exclusion list via Telegram on that ticket. On verdict:

- If ratified as-is → status flips Authored → Ratified on this file; HO surfaces the rule to Specialist + Recovery via their channels for symmetric application.
- If the operator modifies the allow-list / exclusion list → HO edits this ADR in-place + re-files for ratification (one round-trip).

The memory pin HO created at 15:43Z (`feedback_ratifiable_files_must_be_adrs.md`) is deleted in the same commit chain as this ADR's filing — durable rules belong here, not there.
