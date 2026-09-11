# Weekly arc reconciliation — HO's S3 pass over in-flight work

**Status:** Adopted 2026-09-06 by Orchestrator (HO) pending the operator ratify — under D3/msg-3733 this pattern would normally be HO-autonomous adoption + ledger, BUT the mechanism affects operator-visible outputs (leans in the weekly digest) + establishes ongoing HO discipline that shapes priority-list re-ordering, so surfacing to the operator for the formal ratify is the honest posture. Direction ratified by the operator 2026-09-05 via Telegram 1754 (org-planning Decision 3). Owned by HO (orchestrator) as author + operator; Specialist's S4 read feeds the input signal; no new portfolio-manager role is created.

**Serves:** the org-planning Decision-3 gap this closes — HO's ability to keep the below-goal priority-list current with reality without either (a) idle drift (nothing re-orders as arcs converge, so stale ranks accumulate), or (b) a new dedicated role (which would recreate the S3-S4 seam bottleneck the ratified deference-heuristic already retired). Instead: existing HO+Specialist roles run a weekly pass, folded into the existing weekly cadence.

**Related:** [[rulebook/priority-list.md]] (the artifact being re-sequenced below-goal), [[rulebook/policies/trust-posture.md]] §Below-goal priority-list re-sequencing (the authority this pass exercises), [[sop/standard/weekly-ticket-summary-generation.md]] (the weekly cadence this rides), [[architecture/adr/ORG-034]] (arc-intent yardstick), [[architecture/adr/ORG-089]] (deference-heuristic + appetite budget), [[T-3437]] (the ratify + build ticket).

## What this pass does

A weekly step-back over in-flight arcs, checking they cohere and converge. Outputs three things:

1. **Below-goal priority-list re-orderings applied to `rulebook/priority-list.md`** — HO acts under the deferred-judgment authority named in `rulebook/policies/trust-posture.md` §Below-goal priority-list re-sequencing. Goal-level ranks (reserved) never move; only the below-goal residue.
2. **DROPPED-state calls on in-flight arcs that no longer earn their place** — HO acts via `python3 tools/ticket.py drop <ticket>` per the DROPPED-state schema landed via T-3440 (ORG-089 appetite circuit breaker + DROPPED substate). Reversible via reopen.
3. **A "reconciliation" section in the weekly digest** — surfaces the re-orderings + drops as reasoned leans for the operator's read, so he can see the below-goal reshaping without needing to inspect the priority-list diff himself.

The pass is NOT gated on the weekly cadence for the actual re-sequencing — HO may re-order below-goal continuously under deferred-judgment authority. The weekly pass is the STEP-BACK that catches drift + the SURFACE that keeps the operator informed. Continuous small re-orderings + weekly summary is the shape.

## Inputs (what the pass reads)

- **In-flight arcs.** Open tickets grouped by parent-chain / by priority-list label. Sourced from `python3 tools/ticket.py list --state inbox` and `--state active`.
- **Arc-intents.** Each arc's stated goal per [[ORG-034]] (arc-intent is the first-line-of-brief opening; `serves:` frontmatter carries it durably). The yardstick for "does this arc still earn its place?"
- **Current priority-list ranks.** `rulebook/priority-list.md` §Priority list (machine-parseable region).
- **Specialist's S4 read.** Specialist's ongoing watch outputs (`knowledge/watch/reports/<date>/`) + any high-signal environmental signals from her weekly report. Specialist does not run this pass but her surface feeds it.
- **Appetite budget signals.** Per [[ORG-089]] appetite gate, arcs that overshot their appetite band are candidates for either DROPPED (if the work is no longer worth it at cost-so-far) or explicit appetite-adjust. The `_appetite_overrun_check` circuit-breaker output is one input signal; `T-3430` calibration remains open, so this input is currently generate-only (report-only until calibration lands).

## Outputs (what the pass emits)

- **Direct edits** to `rulebook/priority-list.md` for below-goal re-orderings (adds / re-ranks label → priority-N mappings that are additive per ORG-078's auto-approve rule for additive edits; REMOVALS still operator-ratify per priority-list.md §Editability).
- **`ticket.py drop <T-NNNN>`** calls with a reasoned annotation for each dropped arc. DROPPED is reversible (reopen restores).
- **A "Reconciliation" section** appended to the weekly digest, plain-English, ~3-6 bullets, framed as leans:
  - What re-ordered (below-goal, so no operator-approval-needed but surfaced for awareness).
  - What DROPPED + why (the operator can reopen if he disagrees).
  - Which arcs are near the goal-level line + wanting explicit ratify (surfaced separately as `target=operator` tickets, not folded into this section).

## Cadence + wiring

- **Weekly cadence** rides the existing `pantheon-weekly-ticket-summary.timer` (Monday 09:00 UTC per `sop/standard/weekly-ticket-summary-generation.md`).
- **HO's session** picks up the weekly-ticket-summary generation request (target=orchestrator, labels weekly-summary). While generating the digest, HO executes this reconciliation pass and folds the "Reconciliation" section into the output.
- **No new cron, no new timer, no new role.** The pass rides the existing weekly cron; the S3+S4 collaboration is HO reading Specialist's outputs at the same cadence Specialist already produces them.

## Discipline (what the pass MUST do + MUST NOT do)

### MUST

- Read each in-flight arc's stated intent (arc-intent line + `serves:` frontmatter) before ruling on it.
- Cite the specific evidence for a re-order or drop (arc-intent mis-alignment, appetite overrun, downstream unblock retiring the arc's need, ORG-034 yardstick fail).
- Surface every below-goal re-order + drop in the weekly digest's "Reconciliation" section, plain-English, framed as leans not commands.
- File a `target=operator` ticket for any arc near the goal-level line where HO's read is "should be reserved / operator-review needed" — do not silently keep such arcs in below-goal.
- Preserve the DROPPED-state audit trail via `ticket.py drop`'s annotation requirement.

### MUST NOT

- Re-order goal-level ranks silently (reserved per priority-list.md §Editability; operator-ratify required).
- Drop an arc without an annotation naming the reason (P4 traceability).
- Skip the digest surface for a below-goal action ("no news is good news" is not compatible with this SOP; every re-order or drop shows up).
- Create a new ongoing role or dedicated portfolio-manager persona to run this pass (the operator's explicit constraint at TG 1754; the ratified deference-heuristic exists precisely to avoid this bottleneck).
- Skip the pass because the queue "seems fine" — the pass IS the step-back that catches drift; declining it defeats the purpose.

## Boundary — what this SOP does NOT cover

- **Goal-level priority-list changes.** Reserved to operator-ratify per priority-list.md §Editability. This SOP touches only below-goal.
- **Arc-intent authoring.** ORG-034 owns that discipline. This SOP consumes arc-intents; it does not author them.
- **Specialist's S4 read cadence or content.** Specialist's watch cron + report SOP owns that. This SOP references Specialist's outputs as inputs; it does not shape them.
- **The DROPPED-state schema.** ORG-089 §Circuit-breaker + T-3440 landing own the schema + enforcement. This SOP uses `ticket.py drop`; it does not define what DROPPED means.
- **Appetite calibration.** T-3430 (specialist-target, deferred until enough bets accumulate) will produce the appetite → time-budget assignment; until that lands, this SOP's appetite-overrun input is generate-only signal.
- **The weekly digest generation shape.** `sop/standard/weekly-ticket-summary-generation.md` owns the report format. This SOP adds a "Reconciliation" section; it does not restructure the digest.
- **HO-operator-facing tickets.** Any arc HO believes should be surfaced to the operator for explicit ratify (or de-ratify) is filed as a normal `target=operator` ticket, not folded into this pass's outputs. Keeps the reconciliation stream and the operator-tier stream distinct.

## Cross-references

- **Direction ratify:** The operator TG 1754 (2026-09-05, org-planning Decision 3).
- **Authority basis:** [[rulebook/policies/trust-posture.md]] §Below-goal priority-list re-sequencing.
- **Artifact edited:** [[rulebook/priority-list.md]] §Below-goal ranks.
- **Cadence host:** [[sop/standard/weekly-ticket-summary-generation.md]].
- **Ticket-side mechanism:** `python3 tools/ticket.py drop` (T-3440 landing; DROPPED-state schema).
- **Yardstick:** [[architecture/adr/ORG-034]] (arc-intent).
- **Circuit-breaker upstream:** [[architecture/adr/ORG-089]] (deference-heuristic + appetite budget).
- **This SOP's ticket:** [[T-3437]].
- **Appetite calibration downstream:** [[T-3430]] (specialist-target; when it lands, the appetite-overrun input transitions from generate-only to enforcing).

## Authoring discipline

Hand-curated per D2 (HO authors). Draft-only until the operator ratifies; direction ratified TG 1754 per the two-touch flow (direction ratified first, SOP body drafted second, the operator ratifies the body). Amendments post-ratify follow the standard ratify surface flow (operator-ratified via a ratify ticket against this file).
