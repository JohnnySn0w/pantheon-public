---
aliases: [ORG-022]
title: Ratification label ↔ ratifiable_files pairing enforcement at ticket-file-tier
status: Ratified 2026-05-19
date: 2026-05-19
build_disposition: built
---
# ORG-022 — Ratification label ↔ ratifiable_files pairing enforcement at ticket-file-tier

**Status:** **Auto-ratified 2026-05-19 by the operator (Telegram msg_1705):** *"it should be an adr explaining the decision, which is auto-ratified, plus the change to the scripting so the architecture shifts with it."* This ADR documents the decision; the substrate code change ships in the same commit chain. No separate ratify ticket.

**Amended 2026-05-19 (msg_1708):** the label is `ratification` (the PWA's existing filter). Detail: `notes/2026-10-08-org-022-review-history.md`.

**Date authored:** 2026-05-19

**Author:** Orchestrator (Orchestrator) — per the operator msg_1701 → msg_1702 → msg_1705 dialogue.

**Related:** ORG-003 (ticket-system spec); ORG-015 (ratifiable-document-class allowlist — the rule this enforces); ORG-021 (3-state taxonomy + substates-as-frontmatter — this fits as substate-validation discipline).

**HITL:** Pre-ratified (the operator directive msg_1705).

## Context

ORG-015 defines the ratifiable-document-class allowlist + the `ratifiable_files` frontmatter field shape. The convention since ratification has been: a ratification ticket carries (a) `labels: [..., ratification, ...]` AND (b) `ratifiable_files: [...]` in frontmatter, paired. Both signals together mean "this is a ratify-request; PWA renders accordingly; the writer expects the operator to act on the named files."

Today (2026-05-19 msg_1701): the operator called out that `tools/ticket.py file` doesn't enforce the pairing. A writer can:
- File a ticket with `labels: [ratification]` but no `--ratifiable-file` → label without a target file (malformed)
- File a ticket with `--ratifiable-file` but no `ratification` label → file specified, but ratification-flavor signal missing

Either malformed state makes downstream PWA rendering ambiguous and contradicts ORG-015's intent. The current substrate accepts both as valid; convention catches the discipline manually.

The operator msg_1701 → msg_1702 → msg_1705: codify the rule at file-tier so ticket.py refuses malformed combinations. Ship as ADR + ticket.py change together.

## Decision

**`tools/ticket.py file` enforces ratifiable-pairing at file-time:**

1. **If `labels` contains `ratification` AND `ratifiable_files` is absent or empty** → refuse with `TicketError`:
   `"ratification label requires --ratifiable-file <repo>:<path> (one or more)"`.
2. **If `--ratifiable-file` is provided AND `labels` does NOT contain `ratification`** → auto-add the `ratification` label (do not refuse; the file presence is the stronger signal, and writers shouldn't be forced to remember both).
3. Same pairing check applies to `tools/ticket.py retarget` when retargeting modifies `ratifiable_files` or `labels` such that the invariant would break.

The asymmetry (refuse one direction, auto-add in the other) reflects which signal is structurally stronger:
- `ratifiable_files` non-empty is a concrete data signal (specific repo:path entries).
- `ratification` label is a categorical hint that needs the file signal to be load-bearing.

Auto-adding the label when files are present preserves writer ergonomics; refusing when the label is present-without-files prevents the malformed-stub case the operator surfaced.

**Other ticket states (`active/`, `done/`) are not retroactively validated.** Pre-existing tickets keep their frontmatter as-is. Migration is implicit: each ticket's next transition that touches the labels/ratifiable_files invariant gets the same check.

## Forces

- **P1 (architecture over policy)** — the pairing was prose-discipline; this ADR makes it structural at the file-tier. Future writers can't accidentally file a malformed ratify ticket.
- **P3 (decisions justify upward)** — ratification is a load-bearing class per ORG-015; the substrate enforcing its precondition is the operational form of the decision.
- **D1 (the operator's bandwidth)** — operator should see only well-formed ratify tickets in his queue. Malformed stubs surfacing waste a per-ticket judgment cycle.
- **D10 (context-frugality)** — one validation + one auto-add covers both failure shapes; no separate "needs-classification" handling at PWA-tier needed for new tickets (ADR-052 / T-0367 still covers existing pre-amendment tickets that escaped).
- **`lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md`** — applied inversely: the pairing-discipline isn't a NEW mechanism on top of broken substrate; it's the substrate-tier completion of an existing rule (ORG-015) that already had the discipline on paper. The mechanism count stays bounded.

## Alternatives considered

- **(A) PWA-tier validation only.** Render malformed tickets as "needs-classification" (per ADR-052 / T-0367). **Rejected as sole mechanism:** treats the symptom not the cause; malformed tickets still land in `inbox/` and have to be triaged out. File-tier refusal is upstream + cheaper. PWA-tier "needs-classification" still useful for tickets that pre-date this ADR or are filed via paths that bypass ticket.py (rare).
- **(B) Refuse BOTH directions** (require explicit `ratification` label when --ratifiable-file given). **Rejected:** forces writers to remember both signals; cargo-culting risk. Auto-add is the right shape per discipline-meets-ergonomics.
- **(C) Auto-add label only; never refuse.** **Rejected:** allows the "ratification label without files" malformed stub the operator surfaced; that's the failure mode this ADR exists to prevent.
- **(D) Selected — asymmetric: refuse label-without-files; auto-add label-when-files.** Captures the substrate intent (files are the concrete signal); preserves ergonomics; surfaces the explicit malformed case at file-time.

## Consequences

### Code and tests

`tools/ticket.py` validates the pairing in `file` and in `retarget`, with unit tests for both failure shapes. Size and test-plan detail: `notes/2026-10-08-org-022-build-detail-relocated.md`.

### No migration

Pre-existing tickets with the malformed state (if any exist) are not retroactively rejected. The ADR is forward-applied. If a writer wants to retroactively fix an old ticket's labels, they edit the frontmatter directly or use `cmd_retarget` to amend.

### No PWA change in this ADR

The PWA-side rendering for ratify tickets is ADR-052 / T-0367's scope (pending the operator ratification at T-0462). The intent= field (T-0465) provides the canonical signal for the PWA to switch to once ADR-052 ratifies. ORG-022's file-tier enforcement is independent of ADR-052's PWA work; both can land in any order.

## Cross-references

- ORG-003 — ticket-system spec
- ORG-015 — ratifiable-document-class allowlist (this ADR enforces its precondition)
- ORG-021 — 3-state taxonomy + substates-as-frontmatter (this ADR fits as a substate-validation discipline)
- T-0367 / ADR-052 — PWA UI: action-request vs ratify (the PWA-side counterpart, pending ratification at T-0462)
- T-0465 — `intent` frontmatter field (provides PWA canonical signal once ADR-052 ratifies)
- The operator Telegram msg_1701 / msg_1702 / msg_1705 — the dialogue that ratified this.

Review history (the 2026-05-19 label-rename amendment and the status footer): `notes/2026-10-08-org-022-review-history.md`.
