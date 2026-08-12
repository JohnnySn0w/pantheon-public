# Plan an ADR from decision to buildout: spec-layers + linked build tickets + a ratify surface

**Status:** Drafted 2026-08-11 by Orchestrator (HO) per the operator (TG 7630 — "we need an SOP for the ADR→layered-spec→buildout planning"), motivated by the ORG-064 miss (TG 7629). Filed as T-2837. Adopted autonomously under msg-3733 (below-goal-level org-process methodology codifying an operator-directed procedure; ORG-012 — HO authors SOPs). The operator override-available. **Re-evaluate when** ORG-065 (the typed lineage graph) builds — at that point this reduces toward a pointer at the machine-checked shape (see Cross-references).

## When to use this

An ADR (`ORG-NNN` / `ADR-NNN`) is being drafted, ratified, or has just landed, and the design it names still has to become code. This SOP is the discipline that makes the **ADR → layered spec → concrete buildout** lineage legible and machine-checkable, so an ADR cannot read as *decided* while the work that implements it is invisible.

Trigger conditions:
- Authoring or landing a new ADR (the prevent-at-creation lint fires here — see below).
- Ratifying an ADR whose implementation is non-trivial (more than a one-liner).
- Grooming surfaces a ratified ADR with zero associated build tickets (the backstop fired — you are closing that gap).

If the ADR genuinely needs no code (`build_disposition: no-build-needed`), you still declare that in frontmatter; the rest of the procedure is skipped.

## The gap this closes (Exhibit A — the ORG-064 miss)

ORG-064 (General Scheduler) got its substrate settled in conversation and was briefly flipped to Ratified — while its build ticket (T-2662) carried an `org-064` label but **no `build` label**, and no ratify ticket existed. Result: the ADR looked decided, but (a) the ratified-unbuilt guard could not join the build ticket to the ADR, so it read as "zero associated tickets," and (b) there was no ratify surface targeted at the operator. The decision floated free of its buildout. This SOP is the near-term human process that prevents that class; ORG-065 (typed obligation→ticket→evidence graph) is its durable structural successor (see Cross-references).

## Procedure

Each step names the action, the verification that it took, and the escalation if it can't.

### 1. Declare the ADR's build disposition in frontmatter

Every ADR frontmatter carries `build_disposition: needs-build | no-build-needed | built`.
- `no-build-needed` / `built` → exempt from the ratified-unbuilt check (both mean "not an open build gap"). Stop here for these; no build tickets to stand up.
- `needs-build`, or the field **absent** (treated as `needs-build`) → the ADR is eligible for the check and MUST have build tickets linked before it can read as cleanly decided.

**Prevent-at-creation:** a landing ADR is rejected by `.github/workflows/adr-scoping-lint.yml` (`tools/adr_scoping_lint.py`) unless its frontmatter carries EITHER `build_disposition: no-build-needed`/`built`, OR a `scoping_ticket: T-NNNN`. A `needs-build` ADR must therefore point at a scoping ticket at landing time — it may not silently skip the question.

*Verify:* `python3 tools/adr_scoping_lint.py adr/ORG-NNN-*.md` passes (or the ADR-scoping-lint Action is green on the PR).

### 2. Stand up the scoping ticket (the planning node)

File one `scoping_ticket` per ADR (target `orchestrator`; it is HO planning work). Its job is to decompose the ADR into spec-layers and file the build tickets on ratification. Record it in the ADR frontmatter as `scoping_ticket: T-NNNN`.

The scoping ticket's disposition satisfies the guard's exemption in one of two terminal ways: it stays open until the build tickets exist and are linked, or it closes (`done/`) once they do — a `done` scoping_ticket also exempts the ADR from the zero-ticket backstop.

### 3. Decompose the ADR into spec-layers

An ADR names a decision; a spec-layer names an implementable slice with its own acceptance surface. Cut layers along the ADR's own component/isolate boundaries — the same seams the ADR argues for (e.g. ORG-063's security posture cut into per-harness / UID-OS / broker / repair-tier layers). Each layer becomes one build ticket (or a small `depends_on` chain of them). A layer is right-sized when a single implementer dispatch can plan → stub → test → land it.

*Verify:* every `needs-build` obligation the ADR states maps to at least one layer; no layer spans two unrelated obligations. If a layer won't decompose cleanly, that is a signal the ADR under-specifies — FLAG it back into the ADR, don't paper over it in the tickets.

### 4. Stand up + LINK the build tickets (the load-bearing step)

For each spec-layer, file a build ticket carrying **BOTH** join labels — this is exactly the ORG-064 miss:

- `org-NNN` (or `adr-NNN`) — the ADR-namespace join label. ORG/ADR cross-accept both during the migration shim, so either satisfies the join.
- `build` — marks it as buildout work.

Both are required for the ticket to be **join-visible to the guard**: `tools/ticket_grooming.py`'s ratified-unbuilt check joins build tickets to an ADR by the `org-NNN`/`adr-NNN` label, and the `build` label is what distinguishes buildout from adjacent planning/ratify tickets. A build ticket with only one label reproduces the "zero associated tickets" false gap.

Sequence dependent layers with `depends_on: [T-NNNN]` (ORG-013 cascade-unblock removes a resolved dep in-place). Express calendar gates as `not_before:`; never move a ticket to a `blocked/` dir — that state is retired (ORG-019).

*Verify:* `python3 tools/ticket_grooming.py` (or read its digest) shows the ADR with its build tickets joined, NOT under the ratified-unbuilt / zero-ticket surface.

### 5. Stand up the ratify surface

The ADR needs one ticket that IS its ratification act, targeted at the human:

- `target: operator`, `intent: ratify`, `ratifiable_files: [pantheon-config:adr/ORG-NNN-*.md]`.
- If ratification needs no implementation follow-up (the impl ticket already exists from step 4), mark it TERMINAL (`[T-0852] TERMINAL ratify`) so it doesn't imply a further build ticket.

This is the surface the operator signs off on. A canonical ADR status flips to **Ratified only** on an unambiguous ratify against this surface — never on a conversational "keep what we have," which settles a sub-question, not the ADR (P5: humans ratify; the ORG-064 over-ratify was caught on exactly this).

*Verify:* the ratify ticket exists in `inbox/` with `intent: ratify` + `ratifiable_files`; the ADR status still reads "awaiting ratification" until the sign-off returns.

### 6. On ratification — flip status, close the scoping ticket, ledger it

When the operator ratifies against the surface: flip the ADR status line to Ratified (with SHA/timestamp), close the scoping ticket, confirm the build tickets sit in `inbox/`/`active/` ready to pull, and append a ledger entry linking ADR ↔ ratify ticket ↔ build tickets. The buildout then flows through the normal agent-branch dispatch flow.

## Common failure modes

- **Filing build tickets that already exist** — before filing at step 4, INVENTORY what's already there, or you duplicate work. Two sources routinely pre-exist: (a) the ratify flow auto-spawns a generic `ratify-impl` child of the ratify ticket at file-time (the T-0852 pattern — "build X, see the ratified artifact"), and (b) prior triage or a direct the operator instruction may already have filed specific build/UI tickets under the ADR's `org-NNN` label. Run `list`/grep on the `org-NNN` label across inbox/active/done AND check the ratify ticket's `ratify-impl` child *first*; then decompose or absorb what exists rather than filing fresh. *Recurrence:* the ORG-065 buildout filed 5 fresh tickets while T-2673 (the generic ratify-impl placeholder) and T-2676 (an operator-directed Kino-view ticket) already existed — both had to be reconciled post-hoc (T-2673 closed superseded, T-2676 re-gated onto the real model ticket, the duplicate closed).
- **Build ticket missing the `build` label** (the ORG-064 shape) — it has `org-NNN` but the guard's build-vs-planning distinction can't see it, so the ADR reads as unbuilt. *Recognize early:* the grooming digest lists the ADR under ratified-unbuilt despite a ticket existing. *Fix:* add the `build` label; both labels are mandatory.
- **Recording a ratify on ambiguous acceptance** — a conversational "keep it" is a sub-decision, not a ratification. Flip to Ratified only against the step-5 surface. Structural fix: the ratify ticket must exist *before* the status flips.
- **ADR landed with no scoping ticket and no `no-build-needed`** — caught at creation by the scoping lint; if a grandfathered ADR predates the rule, backfill `build_disposition` + a scoping ticket when you next touch it.
- **Layers that won't decompose** — usually the ADR under-specifies. Don't invent structure in the tickets; FLAG the gap back into the ADR (D6 deviation-flag).
- **Silent scope drift between ADR and build tickets** — a build ticket implementing more than its layer. Keep one layer ⇄ one obligation; anything broader bubbles up per the chain-of-justification criteria.

## Boundary

- **Not** the worker plan-first protocol — that's `plan-first-dispatch.md` (D6; what an implementer does inside one dispatch). This SOP is the tier above: turning a *decision* into the *set of dispatches*.
- **Not** the codex spec-review step — that's `codex-planning-spec-review.md` (a second opinion on an implementer's spec before code).
- **Not** the branch-freeze discipline — that's `freeze-at-ratification-surface.md` (freezing the branch once an artifact is surfaced). This SOP hands off TO that flow at step 5.
- Does **not** cover the buildout itself (agent-branch dispatch, review, merge) — that's the standard dispatch SOPs + HO push/PR flow.

## Cross-references

- **Motivating case:** the ORG-064 miss (the operator TG 7629–7636; scoping ticket T-2837).
- **Structural successor:** `adr/ORG-065` — the typed ADR-obligation → ticket → evidence lineage graph with closure-validation is the durable enforcement of this same lineage (this SOP is the near-term human process; ORG-065 is the machine-checked shape, T-2169-prose → ORG-065-structural). **Downstream vision (the operator TG 7636):** once ORG-065's graph provides a task-tier ontology — typed nodes + edges modeling which items are ready/blocked and at which tier — "plan out work" itself becomes safe autonomous idle work. This SOP is layer one of that progression.
- **Guard mechanics:** `tools/ticket_grooming.py` (ratified-unbuilt + zero-ticket backstop; `build_disposition`/`scoping_ticket` exemptions, T-2806); `tools/adr_scoping_lint.py` + `.github/workflows/adr-scoping-lint.yml` (prevent-at-creation); `adr/README.md` §build_disposition.
- **Ticket conventions:** ORG-003 (ticket system); ORG-013 (cascade-unblock); ORG-019 (block state in frontmatter, `blocked/` dir retired); the `agent/<task>` branch + HO-push flow.
- **Principles + directives:** P4 (traceability — the ADR↔build lineage is the audit chain); P5 (humans ratify — the step-5 surface); D6 (plan-first + deviation-flag); D7 (decision/dispatch flow); chain-of-justification (DECISIONS carry the lineage across dispatch boundaries).

## Authoring discipline

Hand-curated per D2 (HO authors). This SOP is the near-term prose enforcement of a lineage ORG-065 will later enforce structurally; when ORG-065 builds, revisit whether this reduces to a pointer at the graph.
