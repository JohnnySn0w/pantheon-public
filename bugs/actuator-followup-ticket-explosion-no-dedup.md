---
symptom: "A single watch-fire's follow-up-ticket actuator emits a large burst of tickets (observed: ~35 in one fire, T-1541–T-1575, 2026-07-06) with near-duplicate pairs (e.g. T-1541/T-1544 same permission-mode item from two reports' overlapping follow-ups; T-1542/T-1543/T-1545 three near-identical permission-mode self-checks). No dedup or rate-limit at creation time; the burst is only caught by a human/orchestrator reading the resulting inbox. Same class the operator flagged at TG-638 (a prior weekly fire over-produced ~46 self-tickets)."
fix: "FIXED 2026-08-24 by T-3172 (PR #333, squash-merge a0e3f871fb): two mechanisms landed in tools/watch_runner.py actuate_phase — (1) per-fire normalized-title dedup via sorted-token-bag key (word-order-invariant, function-local state, catches the reordered-title cluster shape), (2) soft per-fire ceiling (_FOLLOWUP_FIRE_CEILING default 20, env override PANTHEON_WATCH_ACTUATION_CEILING) with overflow-summary ticket enumerating deferred items. 11 tests. Retires the fix candidates (a)+(c) named below; (b) subsumed by (a). This is Pantheon's own resolved instance of claude-code#54393 bug #12 (task-list explosion: no rate limit/dedup on task creation)."
domain: pantheon, watch, actuator, tickets, dedup, over-production, claude-code-54393
---

**RETIRED 2026-08-24 — FIXED by T-3172 (PR #333, squash-merge a0e3f871fb).** Actuator now dedups follow-ups by normalized-title within a single fire (sorted-token-bag key catches reordered-title variants) and enforces a soft per-fire ceiling (default 20, env `PANTHEON_WATCH_ACTUATION_CEILING`) with overflow-summary ticket for deferred items. See `tools/watch_runner.py::_normalize_followup_title` + `actuate_phase` + `file_followup_overflow_summary` and `tools/test_watch_runner.py::TestNormalizeFollowupTitle`/`TestActuatePhaseFollowupDedup`/`TestFollowupFireCeiling`/`TestFollowupOverflowSummary`. Banner-retained rather than deleted because the failure history (T-1541–T-1575 2026-07-06, TG-638 prior) is instructive for anyone touching the actuator surface.

**Verify retirement:** run the next `--land-commit` fire and grep the resulting inbox for `source-topic-` label pattern. Expect ≤20 filed tickets per fire (excluding overflow-summary) with no exact-title duplicates.

---

## Symptom

The watch-dispatch flow (ORG-025) ends with an actuator that reads each landed report's "Follow-ups" section and files a ticket per item. When 8 topics land and several share overlapping follow-ups (permission-mode appeared in both `anthropic-claude-code-changelog` and `agentic-harness-landscape`; the #54393 12-bug postmortem appeared in both `agentic-harness-landscape` and `llm-agent-failure-modes-research`), the actuator files each independently with no cross-report dedup. Result on 2026-07-06: T-1541–T-1575 (~35 tickets) from one fire, including at least three near-duplicate clusters:

- T-1541 / T-1544 — v2.1.200 Manual-default permission-mode, filed from two reports.
- T-1542 / T-1543 / T-1545 — three overlapping "verify permission-mode / AUQ-hang" self-checks.
- T-1550 / (parts of) T-1547 / T-1551 — #54393 12-bug cross-checks filed from two reports.

## How to distinguish from legitimate volume

A high-signal fire genuinely CAN produce many follow-ups (8 above-bar topics is real signal). The bug is the DUPLICATION and the absence of a warn, not the count. Tell them apart: `python3 tools/ticket.py list | awk '$0 ~ /source-topic-/' ` then eyeball for near-identical titles filed within the same minute. Legitimate = distinct failure modes; bug = same headline from N reports.

## Recovery (downstream, until an actuator fix lands)

1. On a burst, triage highest-value first (CVE batch, outage-risk items) rather than top-to-bottom.
2. Dedup by hand: claim the canonical ticket of a cluster, resolve the duplicate(s) with `--with "Duplicate of T-NNNN; <one-line why>"` pointing at the canonical (pattern: T-1545 → T-1542).
3. Leave genuinely-distinct low-pri items queued; work a few per keep-going fire (don't burn the whole batch to zero in one sitting — that's just inverting the over-production into over-consumption).

## Relation

- claude-code#54393 bug #5 (task-list explosion) and #11 (list bloat) — this is Pantheon's own instance; surfaced by the T-1547 cross-check (2026-07-06).
- Prior occurrence: the operator TG-638 (a weekly fire over-produced ~46 self-tickets). Recurring, not one-off — hence documenting as a bug rather than re-noting each time.
- `feedback_idle_nudge_loop_discipline` (auto-memory) — the consumption-side discipline that keeps burst-triage from becoming its own churn.
