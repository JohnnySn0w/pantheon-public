# Research-dispatch follow-up filing

## When to use this

Any time a `researcher` dispatch (autonomous watch-loop OR manual HO/Specialist dispatch) produces a report that **surfaces follow-up work** — tooling gaps, harvest candidates, adjacent bugs, "worth evaluating" items. Codifies the convention ratified by the operator 2026-06-02 (msg_2997): *research files what it surfaces; it does not leave follow-ups stranded in report prose for a human to re-capture.*

## The rule

A surfaced follow-up becomes a **triageable ticket**, not a prose bullet that dies in the report. Tickets are cheap (P7); round-tripping a surfaced idea back through a human's attention to get it filed is the expensive path.

## Layering (who surfaces vs who files)

The researcher persona has restricted tools ("no code", no `ticket.py`) — it **surfaces + enumerates**, it does not file. Filing happens at the dispatching tier:

| Dispatch kind | Researcher does | Filing tier |
|---|---|---|
| Autonomous watch-loop (`watch_runner.py`) | enumerates follow-ups in a `## Follow-ups` section of the report | `watch_runner` B11 actuation files one headline ticket per finding AND parses the report's `## Follow-ups` section into sub-tickets (each with `parent_ticket=<headline>`), with cross-report dedup + a per-fire ceiling (T-3172). See Boundary for the guards. |
| Manual HO/Specialist dispatch (via ticket Context / Agent brief) | enumerates follow-ups in a `## Follow-ups` section + its summary | **HO files them** on report return (do not leave them in prose) |

This resolves the P5-vs-P7 tension explicitly: the researcher **enumerating** a follow-up is *not* role-hopping (P5 intact — it surfaces, it doesn't decide or act on orchestrator-tier work). The dispatcher **filing** the enumerated follow-up as a low-priority triage ticket is the P7 cheap-action. Triage/prioritization still happens at the orchestrator tier.

## Procedure

1. **Dispatch brief** (manual) / **`build_brief`** (autonomous) instructs the researcher: surface any follow-ups in a `## Follow-ups` section, each as a one-line filable item — `<title> — <1-line ask>`.
2. **On report return**, the dispatching tier files each enumerated follow-up: `ticket.py file --requester <persona> --target <orchestrator> --labels from-research,... --ask "<the 1-line ask>"`. Default low-priority / for-triage; target the orchestrator (orchestrator/recovery), not the operator.
3. HO triages on the next grooming pass (prioritize, `block`, or resolve-as-stale).

## Brief corrections (T-2614)

Distinct from `## Follow-ups` (new work to file) — this is for a defect in the researcher's OWN dispatch brief (stale scope, dead seed URL, wrong prior-anchor citation). Emit it as a `brief_corrections:` frontmatter list, not prose:

```
---
surface: routine
headline: ...
brief_corrections:
  - seed source X is 404; correct URL is Y
---
```

`watch_runner.py`'s land path accumulates these per-topic (stable content-hash id, deduped on re-land) and `compute_due_briefs()` appends any un-cleared ones to that topic's next brief as a marked "pending — not yet folded into topics.md" section, so the next cycle's researcher sees the correction instead of re-deriving it. `knowledge/watch/topics.md` is never auto-edited (D2) — an operator folds the correction in by hand, then clears it: `python3 tools/watch_runner.py --clear-correction --topic <slug> --id <id> [--note ...]`. See T-2509 (design ruling) / T-2614 (build).

## Cross-topic ownership (T-2523)

Distinct from `## Follow-ups` (new work) and `brief_corrections:` (own-brief defects) — this governs which topic OWNS a finding the B11 actuation phase files. A researcher may declare an OPTIONAL `belongs_to_topic: <slug>` frontmatter field (alias `duplicate_of_topic`) when the SAME event it reports, raising the SAME ask, is genuinely owned by another topic. When it is set AND the named primary topic also fired that run, `watch_runner.actuate_phase` files a cross-reference NOTE pointing at that topic instead of a second peer actuation ticket — so triage adjudicates the one event once (closes the T-2488/T-2502 one-event-two-tickets case). Leave it OFF when the same event legitimately raises a DIFFERENT ask in your topic (both angles deserve their own ticket); a self-reference is ignored. The field lives in the researcher prompt contract in `build_brief` (`tools/watch_runner.py`); see T-2523 (commit e825bffb7).

## Common failure modes

- **Surface-don't-file** (the retired anti-pattern): listing follow-ups in prose and leaving filing "to HO". Recurred on the deep-research eval AND T-0735 (Yegge). Fix: file them.
- **Filing pure no-action captures** (the over-correction): a "someday polish / no action now" musing is NOT a follow-up — it goes to `BACKLOG.md` or the ledger, not an inbox ticket (the inbox-consumer treats fresh inbox tickets as actionable). The boundary: a *genuine, actionable* follow-up files; a vague "someday" does not. See [[feedback_no_action_captures_not_inbox_tickets]].
- **Targeting the operator**: surfaced follow-ups target the orchestrator for triage, never `target=operator` (that queue is for operator actions/ratifies).

## Boundary

- **B11 sub-follow-up filing (now in production)**: `watch_runner.actuate_phase` parses each report's `## Follow-ups` section and files a sub-ticket per line, `parent_ticket=<headline>`, `labels=[from-watch, ...]`. Guards, in order:
  - **`_FOLLOWUP_SOFT_CAP` (20)** caps how many lines are parsed per report before dedup runs (defense against a runaway single report).
  - **Sentinel skip** — a bare `None`/`none`/empty Follow-ups body files nothing (T-3069 Part-A landed 41b123eae9; `TestParseFollowups::test_t3069_none_titled_line_with_prose_ask_is_no_op`).
  - **Cross-report dedup within one fire** (T-3172) — normalized title key collapses filler-word variance; a duplicate title across reports files once, subsequent hits log-skip.
  - **`_FOLLOWUP_FIRE_CEILING` (default 20, `PANTHEON_WATCH_ACTUATION_CEILING`)** caps per-fire unique follow-up tickets after dedup; overflow lines fold into one summary ticket (T-3172).
  - **`belongs_to_topic:` frontmatter** on the report re-owns duplicate findings across topics as a cross-ref note instead of a peer ticket (T-2523 — see §Cross-topic ownership above).
- **Sub-tickets stay peer-shaped, not folded** — each follow-up files its own low-pri ticket with `parent_ticket=<headline>` linkage rather than being appended to the headline body. Peer-shape preserves independent claim/state/label surface for downstream triage; the parent_id keeps the audit trail intact. (Alternative shape — one headline with N sub-items in body — considered under T-3069 Part-C and declined: coalescing loses per-item claimability + label-search discoverability, and T-3172's ceiling/dedup already addressed the ~35-46-ticket-explosion volume complaint that motivated the ask.)
- Does not change the researcher persona's tool grants — it stays surface-only.

## Cross-references

- ORG-012 (information-inflow bubble-up — HO authors SOPs)
- P7 (cost asymmetry: cheap action), P5 (roles don't hop), D1 (preserve the operator's bandwidth)
- `tools/watch_runner.py` `build_brief` (autonomous brief) + B11 actuation phase
- Memory pin `feedback_research_dispatch_files_surfaced_followups`
- T-0740 (this convention's encode); T-0735 (the Yegge dispatch that surfaced the gap)
- T-3069 (over-fan-out + None no-op — Part-A None-guard 41b123eae9, Part-B this SOP reconcile, Part-C peer-shape retained)
- T-3172 (per-fire dedup + `_FOLLOWUP_FIRE_CEILING` overflow-summary — the guard against the ~35-46-ticket explosion class)
