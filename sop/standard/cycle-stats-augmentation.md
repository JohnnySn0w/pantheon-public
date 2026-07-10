# Cycle-stats augmentation — HO discipline for the ledger metrics field

**Status:** Established 2026-05-10 by T-0193 ratification (the operator msg_1214) + T-0205 authoring. No per-SOP ratification gate per T-0205 ticket body — this SOP documents an already-ratified discipline; reading material rather than a fresh decision.

## When to use this

Every ledger close-entry written by HO (or any agent writing a close-entry — Recovery + Specialist inherit the discipline). Effective from 2026-05-10 forward; pre-2026-05-10 entries get the metrics field via the T-0204 one-time retro pass when that lands.

The discipline applies to:

- Cycle close entries (action shape `t<NNNN>_resolved_*` or `t<NNNN>_landed_*`).
- Sub-arc close entries within multi-ticket cycles (each closing ticket gets its own metrics block).
- HO-direct cycles AND team-pattern cycles AND cross-account cycles (the discipline crosses unix-user boundaries; each agent appends per their cycle).

The discipline does NOT apply to:

- Process / state ledger entries (e.g., `ratified_*`, `acknowledged_*`, `surfaced_*` — these are not cycles in the count sense).
- Auto-commit-related entries from cron-fired tooling (Specialist's `watch_runner.py`, Recovery's `ticket_grooming.py` — those are scheduled-not-dispatched per `~/CLAUDE.md`).
- Quick procedural entries that aren't cycle-bounded (housekeeping ledger writes, ratification confirmations, status flags).

## Procedure

### Schema (9 fields)

Append a `metrics` field at the end of the JSON object, after the standard `lifts_from`. Schema:

```json
"metrics": {
  "wall_clock_min": <int>,           // claim → resolve duration in minutes
  "plan_rebounds": <int|null>,       // count of plan-tier REBOUNDs; null for non-team-pattern cycles
  "code_rebounds": <int|null>,       // count of code-tier REBOUNDs; null if no code emitted
  "reviewer_rebounds": <int|null>,   // count of reviewer-tier REBOUNDs; null if no reviewer pass
  "implementer_count": <int>,        // distinct implementer subagents dispatched (HO-direct = 0)
  "reviewer_count": <int>,           // distinct reviewer subagents dispatched
  "scope_loc_delta": <int>,          // git diff --shortstat insertions+deletions; 0 for no-code cycles
  "cycle_shape": <string>,           // see vocab below
  "outcome_class": <string>          // see vocab below
}
```

Use `null` (not 0) when a metric category doesn't apply to the cycle shape — e.g., a pure-authoring cycle has `code_rebounds: null` not `code_rebounds: 0`. The distinction matters at aggregation time (a true 0 contributes to the average; null is excluded).

### Cycle-shape vocab

Six categories (per T-0193 ratified shape, expandable with future ratification):

- `feature` — new functionality landed (code OR new structural artifact like ADR / SOP).
- `refactor` — change with no functional/behavioral effect (rename, restructure, simplify).
- `migration` — data shape change OR file relocation OR rulebook restructure.
- `authoring` — lessons / SOPs / ADRs / research / methodology docs (this very SOP is `authoring`).
- `drift-cleanup` — restoring state to a ratified-target after observed drift (e.g., b33 sweep cases).
- `bugfix` — fixing a defect; behavior was wrong, now correct.

Pick the dominant shape; mixed cycles take the largest contribution. If a cycle is genuinely unclear (e.g., feature + refactor merged), pick the *intent* category (what was the cycle dispatched for).

### Outcome-class vocab

Five categories (per T-0193 ratified shape):

- `clean` — 0 REBOUNDs at any tier; cycle landed first-pass.
- `one-rebound` — exactly 1 REBOUND total across all tiers; minor correction needed.
- `multi-rebound` — 2 REBOUNDs total; significant rework but stayed within the 3-REBOUND budget.
- `escalated` — 3-REBOUND budget exhausted on one ticket; bubbled up per D7.
- `aborted` — cycle terminated without delivering; usually paired with a fresh-context redispatch sub-cycle.

### Computing `scope_loc_delta`

```bash
git diff --shortstat <start-SHA>..<resolve-SHA> -- <paths>
```

Where `<start-SHA>` = HEAD at cycle claim (record this when you claim); `<resolve-SHA>` = HEAD at resolve commit; `<paths>` = the cycle's deliverable paths (exclude unrelated drift like ledger appends, README index updates that aren't the cycle's primary product).

Report as insertions + deletions (e.g., a cycle that adds 100 lines and removes 20 reports `120`). Sign-loss is intentional — we want the magnitude of the change, not the net.

For pure-authoring cycles where everything new is the deliverable, `git diff --shortstat <start-SHA> -- <new-file-paths>` works (no need to subtract).

For HO-direct cycles where you don't claim a ticket explicitly, use the cycle-start time you noted in the ledger entry's `wall_clock_min` calculation as the start point; `git log --since=<start-time>` to find the start-SHA.

### Stats-correction follow-up

If post-close inspection reveals a metric was wrong (e.g., wall_clock_min off by >2× actual; cycle_shape mis-categorized; outcome_class missed a REBOUND in the count), file a thin correction entry:

```jsonl
{"id": "<ts>-NNN", "ts": "<now>", "agent": "orchestrator", "action": "stats_correction_t<NNNN>_<field>_<old>_to_<new>", "rationale": "Per-field correction: <field> was <old>, actually <new> per <evidence>. Original entry remains; this corrects the metrics for aggregation purposes.", "justifies_via": ["P4"], "depends_on": ["T-NNNN"], "supersedes": [], "addresses_principle_drift": [], "lifts_from": [], "metrics_correction": {"target_entry_id": "<original-entry-id>", "field": "<field-name>", "old_value": <old>, "new_value": <new>}}
```

The original entry stays untouched (D2 + ledger append-only discipline); the correction entry is the structured override that `tools/cycle_stats.py` (T-0203) honors at aggregation time.

## Common failure modes

- **Forgot to record start-SHA at claim.** Mitigation: use `git log --before=<claim-ts>` to recover the SHA from history; or compute scope_loc_delta from the resolve commit's diff against its parent if the cycle was a single-commit close.
- **Cycle had multiple resolve commits (e.g., revert + re-implement).** Use the FINAL resolve commit's SHA + the ORIGINAL claim-time start-SHA. Intermediate revert state is part of the cycle's wall-clock cost, not subtracted.
- **Misclassified cycle_shape post-close.** File a stats-correction entry per the procedure above.
- **REBOUND count uncertainty (was that a REBOUND or a clarification?).** A REBOUND is an explicit "rework + re-emit" instruction in the dispatch chain. A "could you also do X" expansion is a clarification, not a REBOUND. When in doubt, count it as a REBOUND (over-counting REBOUNDs is the conservative bias — keeps `clean` outcome-class genuinely clean).

## Boundary

- **What this SOP does NOT cover:** the read tool (`tools/cycle_stats.py`, T-0203) — that's its own implementation cycle; the retro pass on pre-2026-05-10 entries (T-0204) — that's a one-time Recovery sub-cycle; aggregation surfaces (weekly digest, etc.) — defer until corpus depth justifies.
- **What this SOP does NOT replace:** the close ledger entry's `rationale` field — narrative context still belongs there; metrics is structural-augmentation, not narrative-replacement.
- **Project-scope cycles:** revenue-project + future-projects each have their own ledger-equivalent; metrics field discipline propagates per project's own ratification cadence (not auto-applied cross-repo). Pantheon-config is the founding instance.

## Cross-references

- **Principles + directives:** P3 (decisions justify upward — metrics is structural-justifies-via at the cycle-tier); P4 (traceability — structured fields > regex-parsed prose); D1 (the operator's bandwidth — structured cycle-stats reduce HO's estimation drift, the original surfacing problem from msg_1199); D4 (brief quality bar — wall_clock_min calibration informs future estimate accuracy); D10 (context-frugality — structured fields are minimum-traversal-to-precise-info compared to grepping prose).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope for ledger appends + close-entry writes).
- **ADRs:** `adr/ORG-002` (principles/directives split — this SOP slots at the policy-tier under the framework).
- **Ratification chain:** T-0193 (parent — shape ratification per the operator msg_1214; the founding shape this SOP documents).
- **Sibling implementation cycles:** T-0203 (tools/cycle_stats.py — Recovery-target read tool that consumes the metrics field); T-0204 (Recovery-target one-time retro pass on pre-2026-05-10 entries; blocked on T-0203).
- **Telegram refs:** msg_1199 + msg_1200 (the founding observations — HO's cycle-time estimates anchored on prose-buried numbers; structural fix needed); msg_1213 (HO surfaces strawman shape); msg_1214 (the operator ratifies all 6 points).
- **Founding examples in ledger:** `2026-05-10T16:42:00Z-001` (T-0193 resolution — first metrics block applied; schema-by-example); `2026-05-10T17:08:00Z-001` (T-0196 #2 + T-0197 #1 ratification batch — second metrics application).
