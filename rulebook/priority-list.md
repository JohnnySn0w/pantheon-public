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
  # the operator TG 12363 (2026-10-27): ORG-052 installer + ORG-106 lead the stack.
  # the operator TG 12753 (2026-11-02): "I want the germination engine but we want to
  # build the sub-parts as we go" -- the ORG-080 germination engine is the P1
  # target; its sub-parts are built along the way. TG 12754 named them: the
  # brokers (ORG-051 auth/credential, ORG-052 capability), health monitoring
  # (ORG-076), seat management (harness-manager, in ORG-080).
  - org-080
  - germinate
  - germination
  - org-051
  - org-052
  - org-076
  - org-106

priority-2:
  # the operator TG 12385 (2026-10-27): Echo moves up to priority 2.
  - echo
  # the operator's top-of-stack per TG 8027 (2026-08-20)
  - harness-controller
  - t-2953-arc
  # the operator TG 12794 (2026-11-02): the T-4924 DeepSeek/Hermes third recovery
  # seat is P2, contingent on Watchman's app-server cutover (T-5740).
  - deepseek-seat

priority-3:
  # Ticket-system flow — the layer that enforces cascade, priority, dependency
  - ticket-flow
  - org-065

priority-4:
  # Persona-orientation phases 3-5 (T-5674/75/76, the operator TG 12358)
  - persona-orientation

priority-5:
  # Concordance / retrieval — decision-corpus recall + injection layer
  - concordance
  - org-073

# Labels broad by design (T-6239): exempt from the validator's high-volume and
# semantic-drift heuristics only (dead-label still applies). Not a tier; ranks
# are unaffected. Re-check an entry when its label's meaning changes.
expected-broad:
  echo: "project label, not an arc (Echo, priority-2, TG 12385)"
  org-080: "ADR-family umbrella: the germination engine and its sub-parts (priority-1)"
  germination: "germination-engine umbrella label (priority-1, ORG-080)"
  germinate: "germination-engine CLI label; spans every engine child (priority-1, ORG-080)"
  org-051: "ADR-family label: auth/credential broker, a germination sub-part (priority-1)"
  org-052: "ADR-family label: host capability broker (priority-1)"
  org-076: "ADR-family label: health monitoring (priority-1)"
  harness-controller: "substrate every persona runs on (priority-2, T-2953 arc)"
  ticket-flow: "cross-cutting ticket-system layer (priority-3, with ORG-065)"
```
<!-- PRIORITY-LIST-END -->

## Rationale for the current stack

Rewritten 2026-11-02 to match the machine-parseable list above (the prior prose still described the 08-29 ordering).

- **priority-1** = the germination engine (ORG-080, labels `org-080` / `germinate` / `germination`) and its sub-parts, built along the way: the brokers (ORG-051 auth/credential, ORG-052 host capability), health monitoring (ORG-076), seat management (the harness manager), plus ORG-106 (seat states). The operator TG 12363, 12753, 12754.
- **priority-2** = Echo (TG 12385) and the harness controller / T-2953 arc, the substrate every persona runs on.
- **priority-3** = ticket flow / ORG-065: tasks don't get lost, and completing a subtask moves its parent forward.
- **priority-4** = persona orientation phases 3-5 (T-5674/75/76, TG 12358).
- **priority-5** = concordance / ORG-073, the retrieval and injection layer.

## Deprioritized (not in the priority list)

Labels absent from the priority-list sink to the FIFO tail by design — no explicit demotion needed. Current tail-FIFO includes (non-exhaustive): `passport` / `org-047` / `org-069`-`org-072`, `kino`, `gmail-mcp`, `pantheon-public`, `rodeo-baby`, `vibecamp-*`, `sentient-*`. Echo was returned to the active stack by the operator TG 1902 on 2026-09-06.

When the operator's stack shifts (e.g. echo-AEC reopens as an arc), the relevant label moves into the priority-list at its designated rank + the queue re-sorts on the next `ticket.py list` call. Zero data migration; deprioritized items don't need explicit demotion — they sink naturally as other groups fill the top slots.

## Re-evaluate

- On any operator-directed stack change (TG or arc-planning).
- When ORG-065 typed-graph consumption matures — evaluate migrating this file's role to a graph-attribute query. (Base lineage graph BUILT 2026-08 per T-2673 + BT1-5 + T-3328; migration hinges on priority-labels becoming typed graph attributes with sort consumers, not on graph existence.)
- On observation that a listed label's semantic drift makes its priority-rank misleading (label hygiene concern; edit the label OR remove the entry).
