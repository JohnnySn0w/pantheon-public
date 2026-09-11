# Priority list — ticket queue ordering ranks

**Status:** Ratified 2026-08-22 by the operator via T-3019 (with ORG-078). HO-drafted 2026-08-21 per T-3018.
**Editability:** HO-drafts on operator-direction (TG or arc-planning). Additive edits (adding a label to an existing rank) will be auto-approve HO-tier once ORG-015 is extended to include this file. New ranks or label REMOVALS require operator-ratify. **Below-goal re-sequencing authority (the operator TG 1754, 2026-09-05, org-planning Decision 3):** HO may re-order the priority list BELOW the goal line as reality shifts, under the act-vs-park bar (deferred judgment → proceed reversibly + log + surface a lean per `rulebook/policies/trust-posture.md`). The operator keeps the goal-level ranks (reserved), never silently reordered. Weekly reconciliation cadence per `sop/standard/weekly-arc-reconciliation.md` (T-3437 build) surfaces re-ordering leans + drops in the weekly digest.

## What this is

The **queue-ordering ranks** for `ticket.py list`. Each entry maps an **arc-label** (a label conventionally used on tickets that share an ADR / project / arc) to a priority rank. Rank 1 = highest priority (surfaces first). Tickets that don't carry any listed label sink to the FIFO tail.

Sort algorithm (per ORG-078 §3):
- Primary key: **ceiling** priority-rank across a ticket's labels (multi-label tickets get their highest-priority match).
- Secondary key: retarget-at ASC (fallback: created-at ASC).
- Missing / unparseable priority-list: fall back to created-at FIFO with stderr warning.

## Priority list (machine-parseable region)

<!-- PRIORITY-LIST-START -->
```yaml
priority-1:
  # the operator TG 1676 (2026-08-29): T-3338 and T-3339 jointly occupy the
  # top slot while the fleet persona-orientation substrate is built.
  - persona-orientation

priority-2:
  # the operator's current top-of-stack per TG 8027 (2026-08-20)
  - harness-controller
  - t-2953-arc
  # the operator TG 8228 (2026-08-22): promote the activity_monitor Signal-1
  # structural rewrite — replace language-regex await-detection with
  # tool_use-absence structural detector. Fixes the model-level tendency
  # class surfaced when T-3070 (regex extension) exposed the underlying
  # brittleness of language-detection against stochastic model output.
  - t-3071-arc

priority-3:
  # Ticket-system flow — the layer that enforces cascade, priority, dependency
  - ticket-flow
  - org-065
  - org-013

priority-4:
  # Concordance / retrieval — decision-corpus recall + injection layer
  - concordance
  - org-073
  # the operator TG 1902 (2026-09-06): return Echo to the active stack. Lowest
  # current active rank preserves the persona/harness/ticket-flow prerequisites.
  - echo
```
<!-- PRIORITY-LIST-END -->

## Rationale for the current stack

Per the operator TG 1676 (2026-08-29), the T-3338/T-3339 persona-orientation pair now jointly occupies the top slot and every prior rank shifts down by one without changing its internal order. The prior three-layer stack came from TG 8027 (2026-08-20): stable harness substrate, stable ticket-system flow, then excellent information traversal / Concordance. Layers cascade: upper layers depend on lower being stable.

- **priority-1** = persona-orientation: the shared label carried by T-3338 and T-3339, covering Codex spawn preload and cross-harness/base-plus-child context parity.
- **priority-2** = harness-controller / t-2953-arc: the substrate every persona runs on. Instability here means everything else is on shifting ground.
- **priority-3** = ticket-flow / org-065 / org-013: the mechanism for tasks not getting lost + subtask completion triggering higher-order progress. Includes the first-class dependency graph work.
- **priority-4** = concordance / org-073 plus Echo: the retrieval/injection layer shares the lowest active rank with the reactivated Echo arc. Arc tickets T-3006 (ORG-073 v2 PoC build) and T-3007 (concordance indexing pipeline coord) land here via their existing labels; Echo tickets carrying the broad `echo` label now leave tail FIFO without displacing persona, harness, or ticket-flow prerequisites.

## Deprioritized (not in the priority list)

Labels absent from the priority-list sink to the FIFO tail by design — no explicit demotion needed. Current tail-FIFO includes (non-exhaustive): `passport` / `org-047` / `org-069`-`org-072`, `kino`, `gmail-mcp`, `pantheon-public`, `rodeo-baby`, `vibecamp-*`, `sentient-*`. Echo was returned to the active stack by the operator TG 1902 on 2026-09-06.

When the operator's stack shifts (e.g. echo-AEC reopens as an arc), the relevant label moves into the priority-list at its designated rank + the queue re-sorts on the next `ticket.py list` call. Zero data migration; deprioritized items don't need explicit demotion — they sink naturally as other groups fill the top slots.

## Re-evaluate

- On any operator-directed stack change (TG or arc-planning).
- When ORG-065 typed-graph consumption matures — evaluate migrating this file's role to a graph-attribute query. (Base lineage graph BUILT 2026-08 per T-2673 + BT1-5 + T-3328; migration hinges on priority-labels becoming typed graph attributes with sort consumers, not on graph existence.)
- On observation that a listed label's semantic drift makes its priority-rank misleading (label hygiene concern; edit the label OR remove the entry).
