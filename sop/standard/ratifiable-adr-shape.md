# Ratifiable-ADR shape — the altitude rule + the clutter rule

Two operator-ratified rules govern what an ADR looks like when it is handed to him for ratification. They are the same steer class, so they live together here rather than scattered across `adr/README.md` §Format. This SOP is the discovery surface; `adr/README.md` §Format remains the authoritative statement of each rule and the echo-prismatic `adr/TEMPLATE.md` carries the inline reminders for project ADRs.

## When to use this

Reach for this before you draft or land any agent-authored ADR (every `ORG-NNN` here, every `ADR-NNN` 200+ in a project ADR dir), and again before you assemble the copy you surface to the operator for ratification. The two rules fire at two moments:

- The **altitude rule** shapes the ADR body as you write it.
- The **clutter rule** shapes the ratify-package as you hand it over.

Both apply to the ratifiable copy on main, in a mailbox, or attached to a chat. Neither applies to intermediary drafts (agent branches, review passes, peer mailbox drafts), which may carry whatever the review flow needs.

## Procedure

### The altitude rule — decisions in, build-detail out

A ratified ADR records design decisions and intent. It does not carry build-tier detail. When a fact is a mechanism, a numeric target, a method signature, a per-consumer enumeration, or a state machine, it is build-tier and belongs in a build ticket or a referenced note, not in the ratified body.

1. Draft the ADR as decisions plus the reasoning that makes them decidable.
2. For each concrete mechanism or number, ask whether the operator needs it to say yes. If not, it is build-tier.
3. Move build-tier detail out. Two mechanisms are precedented, both keep the substance and only relocate it:
   - **Trim** — the detail was never load-bearing for the decision; drop it, and let the build ticket derive it (ORG-071 ratify-with-trim, TG 1784).
   - **Relocate** — the detail matters at build time; move it verbatim to a companion `notes/<date>-<slug>-<topic>.md` and link it from the ADR body (ORG-072 ratify-with-relocation, TG 1786-1791; the A1–A8 normative additions moved to `notes/2026-09-06-org072-normative-additions-relocated.md`).
4. Keep the audit chain intact. Relocated content stays verbatim and linked, never silently dropped (P4).

The worked example is ORG-089 itself, whose rev-3 shape-strip (TG 9040) is the altitude rule applied to a live ADR.

### The clutter rule — the ratify-package is clean

The copy handed to the operator is free of review-cycle content. REBOUND logs, finding-to-amendment tables, round-by-round review notes, and provenance-of-intermediary-reviews chains are cutting-room artifacts. They belong in a companion `notes/<date>-<slug>-review-history.md` or in the ticket transition log.

1. Assemble the ratify-package from the clean ADR body, not from the working draft.
2. Strip any review-cycle content into the companion review-history note (keep it, do not delete it).
3. Confirm the frontmatter carries the ORG-089 fields (`appetite:` plus §Appetite and §Riskiest assumption). Those are decision-altitude, so they stay; they are not clutter.
4. Hand over the clean copy.

Worked examples: `notes/2026-09-06-org088-review-history.md` (ORG-041 REBOUND log plus typed-contract pseudo-code plus build-tranche enumeration relocated out of the ORG-088 body) and `notes/2026-09-06-org072-normative-additions-relocated.md` (build-tier detail relocated per RATIFY-WITH-RELOCATION).

## Common failure modes

- **Treating a verbal directive as settled and transcribing it at build-detail altitude.** The highest-stakes calls are the ones made verbally, so this is where over-specification concentrates. Run the altitude pass on verbal directives too (this is the ORG-089 §Surface 2 gate).
- **Deleting instead of relocating.** The rule keeps substance; relocation is verbatim-and-linked, not a cut. A deletion loses build-time detail and breaks the audit chain.
- **Cleaning the working draft instead of the ratify-copy.** The intermediary draft is allowed to be messy. Build the clean copy separately; do not strip the draft the reviewers are still using.
- **Mistaking the ORG-089 viability fields for clutter.** Appetite and Riskiest-assumption are decision-altitude and required; they stay in the ratifiable body.

## Boundary

This SOP governs ADR shape only. It does not change what gets ratified (that authority is the operator's), and it does not touch the ORG-089 requirement that the viability fields exist. Grooming enforcement of empty or boilerplate Appetite / Riskiest-assumption fields is ORG-089 §Surface 4a, not this SOP. Project ADR directories inherit these rules via the echo-prismatic `TEMPLATE.md` reminders; the authoritative wording is `adr/README.md` §Format.

Adopted at methodology tier, below the goal-level bar per amended D3 (the two underlying rules are already operator-ratified; this SOP only consolidates and makes them discoverable). Precedent for agent-adopted methodology SOPs: `refreshing-an-org-039-release-mirror.md`.

## Cross-references

- `adr/README.md` §Format — authoritative statement of the altitude rule, the clutter rule, and the ORG-089 required fields.
- `architecture/adr/ORG-089-*.md` — the deference heuristic; the worked example that dogfoods the viability fields and the altitude rule (rev-3 shape-strip).
- `architecture/adr/ORG-072-passport-lifecycle-health-recovery.md` + `notes/2026-09-06-org072-normative-additions-relocated.md` — the relocate mechanism.
- `notes/2026-09-06-org088-review-history.md` — the clutter-strip mechanism.
- echo-prismatic `adr/TEMPLATE.md` — inline reminders for project ADRs (HO pushes; the Specialist deploy key cannot).
- `knowledge/episodic/2026-09-05-deference-heuristic-proposal/` — design provenance.
