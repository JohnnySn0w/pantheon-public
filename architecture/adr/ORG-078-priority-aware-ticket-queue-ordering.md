---
aliases: [ORG-078]
title: Priority-aware ticket queue ordering — arc-label-group priority-list, transparent list re-sort
status: Ratified 2026-08-22 by the operator via T-3019 (TG 8217). Design DIRECTED by the operator 2026-08-21 (TG 8108/8110/8111/8113).
date: 2026-08-21
author: Orchestrator (Orchestrator)
relates: ORG-003, ORG-013, ORG-015, ORG-019, ORG-041, ORG-065, T-3018
build_disposition: needs-build
scoping_ticket: T-3018
---

# ORG-078 — Priority-aware ticket queue ordering

## Intent

Make the ticket queue **surface critical-path work automatically** to the persona who owns it, so a Phase-N pre-flight gate or priority-1 arc-item doesn't sit behind older FIFO items until a human intervenes with a manual priority bypass. Serves P1 structural-enforcement (priority becomes a queue mechanism, not per-item nudging discipline) + the operator's operational priority stack (TG 8027: harness → ticket-flow → retrieval), which currently exists only as a memory pin, not a machine-readable form the queue can honor.

## Context — the discipline gap this closes

The ticket queue (`ticket.py list` + per-persona pull loops via `inbox_consumer.py`) is a **general work-tracking structure with no baked-in priority rules**. Personas pull FIFO or by their own local heuristics. The gap surfaced concretely on 2026-08-21 when T-2984 (Phase-3 pre-flight blocker for the T-2953 harness controller cutover — priority-1) sat behind ~30 older FIFO items in Recovery's queue for ~12h until the operator's TG 8108 directed a manual priority-bypass. Bypass worked but depends on the operator noticing; the pattern is fragile. The structural fix is to teach the queue about priority so the target persona pulls priority-N items ahead of FIFO tail without human intervention.

## Design

Four elements, composed. Each is small; the value is the composition + operator-single-source editability.

### 1. Priority is at the ARC-LABEL group level, not per-ticket

Priority is declared **once per arc-label** in a rulebook file, not per-ticket. A ticket inherits priority via its already-existing arc-labels (e.g. `t-2953-arc`, `org-073`, `harness-controller`). No new per-ticket labels or frontmatter fields.

**Why:** per-ticket `priority-N` labels are too cluttery; the elegant shape is priority-list keyed on goal/ADR labels that groups of tickets already share. A group-level declaration matches how tickets are naturally organized, avoids label-spam on every ticket, and makes operator-edits atomic (change one file, all tickets re-sort).

**Ceiling-priority over-promotion is ACCEPTED:** the "highest-match-wins" rule (§3) can elevate a ticket whose priority-labeled arc is only an incidental touch, not the primary reason for the ticket. Example: `[echo-phase-4, harness-controller]` becomes P1 even if harness is only affected, not the reason for the ticket. This over-promotion is accepted because (a) under-promotion is the failure this ADR exists to close, and pushing an incidentally-labeled ticket up the queue costs at most one persona-turn of "this isn't actually mine" and a re-triage; (b) a "primary-priority-label" convention adds per-ticket authoring burden that violates the cluttery-is-bad direction; (c) label hygiene (arc-label-should-match-arc-reality) remains an ORG-013 / ORG-065 concern, not this ADR's. Post-ratify observation (§Cadence-observation) reports over-promotion rate; if it materially degrades persona-turn efficiency, a follow-up ADR adds a "primary-priority-label" convention.

### 2. Priority-list — one operator-editable rulebook file

**Location:** `rulebook/priority-list.md`. HO-drafted, operator-ratified per rulebook convention.

**Shape:** Markdown document with a machine-parseable region delimited by `<!-- PRIORITY-LIST-START -->` and `<!-- PRIORITY-LIST-END -->` sentinels (see the companion `rulebook/priority-list.md` file). Between the sentinels is a YAML block:

```yaml
priority-1:
  - harness-controller
  - t-2953-arc
priority-2:
  - ticket-flow
  - org-065
  - org-013
priority-3:
  - concordance
  - org-073
  - t-3006
  - t-3007
```

Labels match exact-string case-sensitive against ticket frontmatter `labels: [...]`. Rank 1 = highest priority; ticket-labels absent from the priority-list sink to the FIFO tail.

**Editability:** HO drafts on operator-direction (TG or arc-planning); the operator ratifies via the standard rulebook-ratify flow. Additive edits are constrained per §Build sequence (post-ratify ORG-015 extension); non-additive edits require operator-ratify.

### 3. Sort semantics — total order, deterministic

`ticket.py list` (and by transitive consumption, `inbox_consumer.py`'s scan gate) applies:

1. **Read** priority-list from disk — single filesystem-read snapshot per `list` invocation; no cross-invocation consistency guarantee (concurrent-write case handled by fresh read each `list` invocation).
2. For each ticket, extract its `labels: [...]` set.
3. **Ceiling priority-rank** — for each label, look up its rank in the priority-list; the ticket's rank is the **minimum** rank across its labels (lowest number = highest priority). See §1 for the over-promotion trade-off.
4. Tickets with NO priority-list-matching label default to rank `∞` (tail).
5. **Sort keys, in order** (all ASC): `(priority_rank, effective_queue_ts, ticket_id)`.
   - **`priority_rank`** ASC — lower rank sorts first (rank 1 = highest priority).
   - **`effective_queue_ts`** ASC — the ticket's "when did it land in current target's queue" timestamp. Sourced from a new frontmatter field `retargeted_at` (added transactionally by `ticket.py retarget` when set). If absent (never retargeted), falls back to `created`. Historic tickets with no `retargeted_at` field get `created` as effective ts; backfill by the retargeted-at from the last `[orchestrator] retargeted` transition-log entry if a follow-up hygiene pass runs.
   - **`ticket_id`** ASC (lexicographic on `T-NNNN`) — deterministic final tie-breaker for tickets with same rank + same ts.

Total order guaranteed. Two concurrent `ticket.py list` calls with the same filesystem state return identical ordering.

### 4. Consumer integration — transparent re-order + telemetry on failure

`ticket.py list` re-orders in-place by default. All existing callers see the new order automatically.

**Opt-out:** `ticket.py list --sort priority|created|retargeted`. Default is `priority`. `created` and `retargeted` return the corresponding FIFO views for hygiene/audit sweeps.

**Failure-mode telemetry:** if `rulebook/priority-list.md` is missing, unparseable, or has an empty ranked-labels set:
- `ticket.py list` **falls back to** `created` FIFO with a stderr warning (never brick the queue).
- **AND** writes a structured warning to `~/.local/log/pantheon/priority_list_health.jsonl` with timestamp + failure-class + attempted-source-path.
- **AND** a periodic health-check reads the log; if a broken/empty priority-list state persists >1 fire-interval, emit a `pantheon-priority-list-broken` fault via the ORG-076 detector → episode → ticket path (goes to **Recovery's queue** as a real ticket, not just a stderr note personas may miss — Recovery owns as fleet-hygiene domain, and HO may itself be the source of the break since HO drives priority-list edits, so self-alerting HO would be circular).
- **Pre-commit hook** (installed with this ADR's build) validates the YAML region of `rulebook/priority-list.md` on commit; rejects commits that would land a broken priority-list. Repo-side safety net that catches most edit errors before they escape.

**Escalation-path dependencies:** the failure-telemetry pipeline has explicit dependencies worth naming so degraded-substrate cases don't silently drop the signal:
- **ORG-076 detector-path** is load-bearing for the "persistent-broken → repair-ticket-to-Recovery" step. If ORG-076 itself is degraded (its own detectors down, its own emission path broken), the fallback degrades to stderr + local JSONL only. Priority-list health cannot alert past the local seat in that state.
- **Repair-ticket target = Recovery**: fleet-hygiene domain owner + avoids the HO-self-alerting circularity (HO drives priority-list edits so may be the source of the break). Recovery-target sidesteps the "HO down/capped fallback" concern that would otherwise require systemd OnFailure= gymnastics.
- OnFailure= belt-and-suspenders on the periodic-health-check unit itself still routes to Recovery's mailbox as a redundant path (same escalation as ORG-076's own OnFailure=).

## Priority inversion — dependency inheritance

A P1 ticket blocked by an unprioritized/P3 dependency is a real priority inversion, not covered by the base sort. Rule:

- **A ticket's effective priority-rank is min(own_rank, min(rank of tickets that depend_on this ticket in the ORG-065 graph)).** In prose: if any P1 ticket is currently blocked on you (directly or transitively), you're at least P1-effective, regardless of your own label-derived rank.

**Implementation shape:**

- **Single graph snapshot per `ticket.py list` invocation.** Read ORG-065's typed-edge state ONCE at invocation-start (filesystem read snapshot; no cross-invocation consistency guarantee). All ceiling-priority computations for that list call operate against that snapshot; concurrent graph mutation during the list call has no effect on that call's output.
- **Reverse-edge priority propagation.** From the snapshot, build the reverse-`depends_on` adjacency once. For each ticket T with own_rank R, propagate `min(R, downstream-min-rank)` outward via the reverse edges. Total time: **O(V+E)** where V = ticket count and E = `depends_on` edges in the graph. Linear in graph size, load-bearing because `inbox_consumer.py` calls `ticket.py list` operationally at per-persona timer cadence.
- **`ticket.py list --explain-priority T-NNNN`** uses the SAME snapshot as its parent `list` call to guarantee report-consistency. If called standalone (no parent list), takes its own snapshot.
- **ORG-065 DAG-acyclicity §3.6** guarantees the reverse-propagation terminates without cycle-detection overhead.

**Pull-eligibility invariant:**

Dependency-inheritance elevates the EFFECTIVE priority-rank of BLOCKED tickets, but blocked tickets remain **NOT pull-eligible** to prevent consumers from injecting a prompt on a ticket that isn't actionable:

- **Blocked = pull-ineligible.** A ticket with unmet `not_before` (calendar gate) or unresolved `depends_on` (dep gate) per ORG-019 is filtered from `inbox_consumer.py`'s pull surface and from any persona-active-surface. Injection would prompt work on a still-blocked ticket.
- **Dependency-inheritance promotes the PREREQUISITES, not the blocked ticket itself.** If P1 ticket X is blocked on P3 ticket Y, the effective-rank of Y becomes 1 (inherited from X). Y is the ACTIONABLE prereq; consumers pull Y first. When Y resolves, ORG-013 cascade-unblocks X; on the next `list` call, X's own_rank (P1) makes it pull-eligible via the base sort.
- **Blocked tickets stay VISIBLE for audit.** `ticket.py list --sort priority` output includes blocked tickets with their effective-rank (so HO can see "P1 arc X is blocked on Y"); the pull-eligibility filter is applied by `inbox_consumer` at the moment of prompt-injection, not by the sort itself. `--include-blocked` flag exposes them explicitly in the sort-only output.

**Cascade-composition (ORG-013):** unchanged. When a dep resolves, ORG-013 removes the resolved id from `depends_on` in-place; the next `ticket.py list` call recomputes effective priority without the discharged dep, so the previously-elevated ticket may sort down + the previously-blocked ticket becomes pull-eligible. Natural + correct.

## Missing-label detection

Loose labeling means a critical-path ticket can be filed WITHOUT the arc-label that would earn it its priority. Detection layer:

- **First-week audit** (owner: HO; artifact: `notes/YYYY-MM-priority-list-hygiene-week-1.md` — YYYY-MM set at build-ship time): after the ADR builds ship, HO runs a weekly hygiene pass for the first month: `ticket.py list --sort priority` output audited against the ORG-065 typed-graph closure of each priority-N arc. Tickets in an arc's closure (via `implements` / `verifies` edges) but missing the arc-label surface as candidate priority-label additions.
- **`ticket.py list --explain-priority`** (per §Priority-inversion) doubles as an operator-facing sanity check.
- **Fleet-monitor hook** (post-ratify follow-up): if the ORG-065 graph shows N open tickets in a P1 arc's closure but their label-derived ranks are P3+ or `∞`, emit an alert via ORG-076's detector path. Not scope of this ADR; ticketed separately as a follow-up.

## Cadence-observation

**Owner:** HO (orchestrator).

**Artifact:** `notes/YYYY-MM-org-078-cadence-observation.md` (template — actual filename set at build-ship time with concrete YYYY-MM), week-1 through week-4 post-build-ship.

**What it measures:** (a) over-promotion rate — tickets whose ceiling-priority is P-N but whose ORG-065 `implements`/`verifies` closure indicates their primary arc is a DIFFERENT (lower-priority) arc; retarget-reason cross-check where present; residual "not-covered-by-graph" cases marked as author-judgment manual review. (b) missing-label rate — tickets in a P-N arc's ORG-065 closure but no matching label; (c) inversion-inheritance hit rate — how often does dependency-inheritance elevate a ticket above its own_rank; (d) YAML-parse-fail rate — how often does the fallback fire.

**Decision surface:** if any rate materially degrades operator-visible cadence, file a follow-up ADR with the targeted mitigation. NOT a scheduled 4-week deadline; folds into normal HO discipline once one observation cycle completes.

## Priority-list drift hygiene

Labels whose meaning drifts (e.g. `concordance` starts covering post-PoC work that shouldn't be priority-3) are a real hazard. Mitigation layer:

- **Weekly audit** (folded into the cadence-observation artifact above): report on labels in the priority-list — spread of usage over the last N days, new use-cases that surfaced, label semantic drift heuristics (e.g. a label suddenly appearing on many tickets outside its original arc).
- **`ticket.py priority-list validate`** (build-time command): dry-run inspection of the priority-list vs current corpus — reports labels that are UNUSED (no open ticket carries them), labels that are HIGH-VOLUME (may be over-broad), labels with semantic-drift heuristics fired, AND shows sample matching tickets per added label (typically 3–5 titles) so the editor confirms the label attaches to the intended tickets, not just that it exists / is a substring of something. Catches "silent priority attachment" — a real but wrong label matching tickets the editor didn't intend.
- operator-edit hygiene: when adding a label to the priority-list, run `ticket.py priority-list validate` first; add to priority-list only if the label passes the sanity check (unused/high-volume/drift + sample-matching all reviewed).

## Consequences

- **Positive:** critical-path work self-surfaces to the target persona; retires the manual-bypass workaround. operator-single-source (one file edit, whole queue re-sorts). Zero data migration. Cheap to reverse. Compact. Dependency-inheritance closes the priority-inversion hazard. Failure telemetry makes broken priority-lists loud, not silent.
- **Negative:** over-promotion accepted with observation-based decision surface (see §1). Missing-label hazard mitigated by ORG-065-graph audit + explain-priority tooling. Small semantic-drift risk on priority-list labels; hygiene layer above addresses.
- **Composition with ORG-065:** priority-inheritance queries ORG-065's typed-edge graph; explain-priority + missing-label detection both rely on the graph. Not a separate index; leverages existing infrastructure.
- **Composition with ORG-013:** cascade-unblock is orthogonal + composes correctly (unblock discharges deps, next list re-sorts naturally).
- **Composition with ORG-019:** blocked-state retirement (blocks-in-frontmatter) is unchanged; blocks feed dependency-inheritance rather than blocking priority sort itself.
- **Composition with ORG-076:** priority-list health becomes a fleet detector; persistent broken-priority-list emits a repair ticket on **Recovery's queue**.
- **No change to:** WIP-cap (still 1 active per agent), ticket state (inbox/active/done), retarget/re-file, ratifiable-file mechanics, worker-guard, mailbox delivery.

## Build sequence

1. **Author** `rulebook/priority-list.md` with the initial stack (done in this ADR's ratify tranche; see companion file). Sentinel-delimited YAML region.
2. **Extend `tools/ticket.py`** — `list` function: read priority-list at invocation with snapshot semantics; compute ceiling priority-rank; apply dependency-inheritance from ORG-065 graph (bounded transitive closure); sort by `(priority_rank, effective_queue_ts, ticket_id)`; expose `--sort priority|created|retargeted` (default `priority`); implement failure-mode telemetry (stderr + `priority_list_health.jsonl`).
3. **Add `tools/priority_list_parse.py`** helper: parse the sentinel-delimited YAML region; return `dict[str, int]` label→rank. Reused by ticket.py + validate command + fleet-monitor.
4. **Add `retargeted_at` frontmatter field** — `ticket.py retarget` writes/updates this transactionally; historic tickets get `created` as fallback until a hygiene backfill runs. `ticket.py priority-list validate` (or a one-time audit command) reports tickets that HAVE `[<agent>] retargeted` entries in their transition-log but lack the `retargeted_at` frontmatter field — surfaces the pre-existing-drift so we know how much distortion the `created`-fallback introduces to historic-ticket ordering.
5. **Add `ticket.py list --explain-priority T-NNNN`** — reports own_rank + inherited_rank + which downstream ticket elevated it, if any.
6. **Add `ticket.py priority-list validate`** — inspects priority-list vs corpus; reports unused/high-volume/drift heuristics + sample-matching tickets per label.
7. **Add pre-commit hook** — validates `rulebook/priority-list.md` YAML region on commit; rejects malformed edits.
8. **Add fleet-monitor hook** — ORG-076 detector for persistent broken-priority-list state → repair ticket to Recovery.
9. **Tests:**
   - `tools/test_priority_list_parse.py` — parse valid/missing/unparseable priority-lists + sentinel-delimited region.
   - `tools/test_ticket_list_priority_sort.py` — INTEGRATION tests: valid priority-list end-to-end through `ticket.py list`, multi-label conflict cases, dependency-inheritance cases, bad YAML through `ticket.py list` (fallback + telemetry), tie-breaker determinism.
10. **Smoke:** `pytest tools/ -q`.
11. **Observation** (§Cadence-observation): week-1 through week-4 post-ship; artifact as named.
12. **Post-ratify:** ORG-015 ratifiable-doc allowlist extension for `rulebook/priority-list.md`. **Additive edits auto-approve HO-tier ONLY IF** the added label is already observed in the corpus (`ticket.py priority-list validate` passes with the addition). New ranks + label REMOVALS + brand-new-label additions stay operator-ratify. Validate runs at COMMIT/REVIEW time against the exact post-edit file content, not against a pre-edit sanity check (closes the TOCTOU gap between manual validate + actual commit). Implementation: pre-commit hook OR CI step that reads the diff'd file + validates the added labels against the current corpus.
13. **Consumer-inventory grep:** before flipping `ticket.py list` default to `--sort priority`, grep the codebase + operator surface for known consumers that IMPLICITLY assume FIFO order (e.g. hygiene scripts, audit sweeps that pick first-N as oldest, cron helpers). Pin those to `--sort created` or `--sort retargeted` explicitly. Prevents silent behavior-change on tooling that relied on the old order.

## Re-evaluate

- When ORG-065 typed-graph consumption matures, evaluate migrating priority-list to a graph-attribute query. (Base lineage graph BUILT 2026-08 per T-2673 + BT1-5 + T-3328; migration hinges on priority-labels becoming typed graph attributes with sort consumers, not on graph existence.)
- When the operator's priority stack changes (per Telegram or arc-planning), HO edits `rulebook/priority-list.md` under this ADR's authority.
- After first month observation: cadence-shift report → tuning follow-up if any rate materially degrades.
- If over-promotion rate exceeds an observed-nuisance threshold, revisit the ceiling-priority + primary-priority-label question.
