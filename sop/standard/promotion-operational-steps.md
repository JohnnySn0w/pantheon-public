---
title: Promotion operational steps
status: Drafted 2026-06-05 by Specialist (T-0832, G-1 — ORG-012 Phase D); awaiting the operator ratification.
date: 2026-06-05
---
# Promotion operational steps

## When to use this

You have already **decided** to promote a piece of knowledge to a higher tier — per ORG-012's promotion criteria (the "when/why" methodology). This SOP is the "how you actually do it" companion. It covers the mechanical execution steps, the filing contract, who ratifies what, and the supersedure bookkeeping on the source artifact.

If you haven't applied ORG-012's criteria yet (threshold tests, ratification gates, two-axis model) — stop and read `adr/ORG-012-information-inflow-bubble-up-methodology.md` first. That ADR owns the decision; this SOP owns the execution.

**What counts as a promotion:** the move of a rule, pattern, or artifact from a lower-weight tier to a higher-weight one, or into a different *kind* of container when the kind is a better fit (e.g., lesson → ADR when the shape is architectural, not just a behavioral rule bump). ORG-012 §Tier map enumerates the full vertical and horizontal axes.

**What this SOP does NOT cover:**
- Demotion mechanics (ORG-012 §Demotion criteria).
- The cross-quarter sweep that *discovers* promotion candidates (ORG-012 Cadence #6 + `throughline-sweep-result-filing.md`).
- Ratification of the promotion itself — that is the operator's step.

---

## Tier-target map

Pulled verbatim from ORG-012 §Tier map and §Promotion criteria. The *target* column is the promotion destination for each source container.

| Source tier | Source container | Target tier / container | Ratification gate |
|---|---|---|---|
| Working scratch | `notes/`, ledger comments | `lessons/` (if pattern emerges — 2+ instances, same shape) | HO autonomous at write |
| Concrete gotcha | `bugs/` | No upward promotion (bugs stay bugs; archive at decay) | N/A |
| Postmortem | `incidents/` | No upward promotion (incidents stay incidents; lessons abstract from them) | N/A |
| Abstracted pattern | `lessons/` | `sop/standard/` or `sop/remediation/` (role-defining operational sequence); `rulebook/directives.md` (structurally enforceable + org-tier); `adr/ORG-NNN` (decision-among-alternatives with named tradeoffs) | the operator pre-merge |
| Bug cluster | `bugs/` (multiple) | `sop/remediation/` (when the cluster defines a class of failure with a reproducible mitigation pattern) | the operator pre-merge |
| Operational playbook | `sop/standard/` or `sop/remediation/` | `rulebook/directives.md` (structurally enforceable + org-tier) | the operator pre-merge |
| Org-level rule | `rulebook/directives.md` (D-class) | `rulebook/principles.md` (foundational; cited in 2+ other directives' `justifies-via` chains as ground reason) | the operator pre-merge, explicit |
| HO memory pin | `feedback_*.md` | `sop/standard/` (role-defining for any agent, not just HO habit); `rulebook/directives.md` (structurally enforceable + multi-workflow); `adr/ORG-NNN` (architectural shape) | the operator pre-merge of the SOP/ADR |

**Two-axis note.** A single observation can land in multiple containers if it has both an architectural and an operational shape (ORG-012 §Tier map §Two-axis distinction). Build both artifacts; file one ratify ticket per artifact.

---

## Procedure

### 1. Verify the ORG-012 threshold is met

Before writing anything, confirm the applicable threshold test from ORG-012 §Promotion criteria:

- **lesson → SOP**: "would naming this in a worker persona's read-order be load-bearing?"
- **lesson / SOP → directive**: "can this rule be encoded in tooling / hooks / monitor cadence?"
- **directive → principle**: both conditions required — (a) foundational constraint; (b) explicitly cited in 2+ other directives' `justifies-via` chains as the ground reason.
- **anything → ADR**: "does framing this require Forces / Candidate mechanisms / Recommendation / DECISIONS structure?"
- **bug cluster → remediation-SOP**: 2+ bugs with the same failure shape and a reproducible mitigation pattern.
- **memory pin → SOP**: "is this role-defining for any agent, not just HO's habit?"

If the threshold isn't clearly met, do not proceed — file a noodle ticket instead.

### 2. Build the artifact in the target tier

Create the new file at the target path with the correct structure for that tier:

**`sop/standard/<slug>.md` or `sop/remediation/<slug>.md`**
```
# <SOP name>

## When to use this
## Procedure
## Common failure modes
## Boundary
## Cross-references

---
**Status:** **Ratified 2026-06-05 by the operator.** Drafted <date> by <author> (<ticket>).
```

**`adr/ORG-NNN-<slug>.md`** — full ADR shape:
```
---
title: ...
status: Drafted <date>
date: <date>
---
# ORG-NNN — <title>

## Context
## Forces
## [Candidate mechanisms / Options considered]
## Recommendation
## DECISIONS
## Cross-references
```

**`rulebook/directives.md` amendment** — append the new D-class entry in the established format; include `justifies-via` pointer chain. The whole file is the artifact; the amendment is the promotion artifact for ratification purposes.

**Frontmatter status line** (all tier targets): always include a one-line status declaring draft, authoring ticket, and "awaiting the operator ratification" for any operator-gated tier (all targets except scratch→lesson writes, which are HO autonomous).

**Slug convention:** use the same kebab-case descriptive slug pattern existing files use (`<topic-summary>.md`). For ADRs, the next sequential `ORG-NNN` — check the `adr/` directory for the current highest number.

### 3. Cross-reference the promotion in the new artifact

In the new artifact's Cross-references section (or analogous), cite:
- The source artifact (the thing being promoted from).
- ORG-012 — the promotion methodology that motivated this.
- This SOP (`sop/standard/promotion-operational-steps.md`) — so the execution trail is traceable.
- Any related tickets (promotion ticket + the ratify ticket you will file next).

### 4. File an `intent=ratify` ticket

Per `sop/standard/throughline-sweep-result-filing.md` §4 (two-step contract): the greenlight approval is the *direction* decision; the ratification is the *artifact* decision. They are separate tickets.

Once the artifact exists and is committed (or at minimum drafted in a committed file):

```
ticket.py file \
  --requester <your-agent-name> \
  --target operator \
  --intent ratify \
  --ratifiable-file "pantheon-config:<path/to/new-artifact.md>" \
  --ask "Ratify: <artifact name> (promoted from <source> per ORG-012 + T-NNNN)"
```

If the promotion was greenlit as part of a sweep (like T-0780 → T-0832), set `parent_ticket` to the greenlight ticket:

```
ticket.py file ... --parent-ticket T-NNNN
```

**One `intent=ratify` ticket per artifact.** If a promotion produces two artifacts (e.g., a lesson spawning both an SOP and an ADR), file two ratify tickets, one per artifact. A single ticket cannot ratify multiple artifacts (T-0556 anti-pattern — auto-bounces).

**Do not pre-file the ratify ticket for an unbuilt artifact.** The tooling requires `--ratifiable-file` to point at something real. Greenlight first, build, then ratify.

### 5. Leave the source artifact in place (pending ratification)

Do NOT edit the source artifact until the operator ratifies the promotion. Rationale: if the promotion is rejected, the source needs to remain the load-bearing reference. Editing it prematurely orphans the rule.

The source artifact stays unchanged. The new artifact exists as a draft. The ratify ticket sits in the operator's queue.

### 6. On ratification — write the supersedure trail on the source

When the operator ratifies (resolves the intent=ratify ticket), update the source artifact with a supersedure marker at the top:

**For `lessons/` files:**
```
> **Superseded as load-bearing by** `<target-tier-path>` (ratified <date> per T-NNNN).
> The rule forward is carried by that artifact; this file is the historical record.
```

**For `sop/` files promoted to directive:**
```
> **Superseded as load-bearing by** D-NN `<directive name>` (ratified <date> per T-NNNN).
```

**For `rulebook/directives.md` entries promoted to principle:**
Mark inline as `Superseded: <date> per T-NNNN → P-NN`. Do not delete the directive entry.

**For `adr/` supersedures:**
Add an explicit `Superseded by ORG-NNN` block at the top per the existing ADR supersedure pattern.

**For `feedback_*.md` memory pins promoted to SOP:**
Per ORG-012 §Demotion criteria + §Promotion criteria #6: convert the pin to a reference pointer (one line: "Promoted to `sop/standard/<slug>.md` T-NNNN") OR remove it entirely (deletion is the demotion for memory pins). HO judgment on which is cleaner.

The supersedure trail is what makes the audit chain legible: future readers see both "what was believed" and "what superseded it." ORG-012 §Demotion criteria §Audit chain mechanics is the governing rule.

### 7. On rejection — close the draft artifact

If the operator rejects (or redirects) the promotion:

1. Update the new artifact's Status line: `Rejected <date> per T-NNNN — <1-line reason>. Not merged.`
2. Do not delete the draft file from the repo (audit chain). Mark it rejected inline.
3. The source artifact remains the load-bearing reference, unchanged.
4. If the operator gave a redirect ("this should be a directive not an SOP"), treat that as a new direction decision and restart from Step 1 with the redirected target tier.

### 8. Index / README suggestions (operator-curated constraint)

Top-level index files (`sop/standard/README.md`, `adr/README.md`, `rulebook/directives.md` preamble, etc.) are **operator-curated** — HO and agents suggest rows but do not auto-merge index edits. The pattern:

- After building the artifact, **suggest the index row** in the ratify ticket's ask: include the one-line README entry the operator should add when they merge the ratification.
- Do not auto-edit `sop/standard/README.md` or other top-level indexes as part of the promotion build.
- The AUTOGEN-START spine-index block in `sop/standard/README.md` is auto-generated; do not hand-edit it.

This preserves the operator's curation role on the index surface while making the suggestion cheap (it's inline in the ratify ticket).

---

## Common failure modes

- **Pre-filing the ratify ticket before the artifact exists.** Tooling auto-bounces (T-0556): `intent=ratify` requires `--ratifiable-file`. Build first, ratify second.
- **Filing a single ratify ticket for multiple artifacts.** One ticket per artifact. A menu of promotions in a single ratify ticket is unreviewable and auto-bounces.
- **Editing the source artifact before ratification.** The promotion may be rejected; editing it prematurely destroys the load-bearing reference. Wait for ratification step (Step 6).
- **Skipping the supersedure trail.** Without the trail, `lessons/` and other lower-tier files become invisible dead weight — the audit chain breaks. The trail is not optional.
- **Applying lesson→SOP threshold loosely.** The test is "would naming this in a worker persona's read-order be load-bearing?" — not "this is useful to know." Discipline reminders stay lessons; operational sequences become SOPs.
- **Promoting to the wrong horizontal kind.** ADRs are architectural decisions-among-alternatives; directives are structurally enforceable org-tier rules; SOPs are operational sequences. A lesson about a design decision → ADR, not SOP. Misrouting creates the kind-conflation ORG-012 §Tier map §Two-axis note explicitly guards against.
- **Batching distinct promotions into one ratify ticket.** Batching is encouraged for *batched ratification efficiency* (surface multiple items in one Telegram exchange per ORG-012 §Ratification gates), but only as a conversation — each artifact still needs its own `intent=ratify` ticket with its own `--ratifiable-file`.

---

## Who does what

| Step | Owner | Autonomy bar |
|---|---|---|
| Threshold verification (Step 1) | Drafting agent (HO or Specialist) | Autonomous — judgment call on threshold tests |
| Build the artifact (Step 2) | Drafting agent | Autonomous — below-bar; artifact is a draft until ratified |
| File the ratify ticket (Step 4) | Drafting agent | Autonomous — filing a ticket is never an operator-gated action |
| Ratification decision | **the operator** | Operator-gated — all D2-surface promotions (SOPs, directives, principles, ADRs) |
| Supersedure trail on source (Step 6) | Drafting agent | Autonomous post-ratification — bookkeeping, not a new decision |
| Index row suggestion (Step 8) | Drafting agent | Autonomous to suggest; operator to merge |

HO (Orchestrator) is the primary drafter for most promotions per `org-012-per-agent-portions.md`. Specialist operates on the producer-side (flags promotion candidates in research/watch reports; HO drafts the strawman). Specialist may build promotion artifacts when directly tasked (as with T-0832 G-1 this document).

---

## Cross-references

- `adr/ORG-012-information-inflow-bubble-up-methodology.md` — the governing methodology (when/why/criteria). This SOP operationalizes its execution path. Specifically: §Tier map (the two-axis model + containers), §Promotion criteria (all threshold tests), §Demotion criteria (supersedure mechanics), §Ratification gates (who ratifies what).
- `sop/standard/throughline-sweep-result-filing.md` — the two-step filing contract (greenlight ticket → build artifact → ratify ticket). Step 4 of this SOP aligns with §4 of that SOP.
- `sop/standard/org-012-per-agent-portions.md` — who operationalizes which portions of ORG-012 per agent.
- `adr/ORG-012-information-inflow-bubble-up-methodology.md` §Implementation arc Phase D — explicitly planned that this SOP would be authored once a few promotions had been observed (precondition met 2026-06-04 per T-0780 sweep).
- T-0832 (this SOP's authoring ticket, G-1 in the T-0780 greenlit batch).
- T-0780 (the cross-quarter throughline sweep that surfaced the G-1 gap and provided the observed-promotions precondition).

---

— Specialist (specialist), drafted 2026-06-05 for T-0832 (G-1, ORG-012 Phase D).
