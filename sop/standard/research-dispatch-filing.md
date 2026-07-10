# Research-dispatch follow-up filing

## When to use this

Any time a `researcher` dispatch (autonomous watch-loop OR manual HO/Specialist dispatch) produces a report that **surfaces follow-up work** — tooling gaps, harvest candidates, adjacent bugs, "worth evaluating" items. Codifies the convention ratified by the operator 2026-06-02 (msg_2997): *research files what it surfaces; it does not leave follow-ups stranded in report prose for a human to re-capture.*

## The rule

A surfaced follow-up becomes a **triageable ticket**, not a prose bullet that dies in the report. Tickets are cheap (P7); round-tripping a surfaced idea back through a human's attention to get it filed is the expensive path.

## Layering (who surfaces vs who files)

The researcher persona has restricted tools ("no code", no `ticket.py`) — it **surfaces + enumerates**, it does not file. Filing happens at the dispatching tier:

| Dispatch kind | Researcher does | Filing tier |
|---|---|---|
| Autonomous watch-loop (`watch_runner.py`) | enumerates follow-ups in a `## Follow-ups` section of the report | `watch_runner` B11 actuation already files one ticket per headline finding; in-report sub-follow-ups are a flagged future B11 enhancement (see Boundary) |
| Manual HO/Specialist dispatch (via ticket Context / Agent brief) | enumerates follow-ups in a `## Follow-ups` section + its summary | **HO files them** on report return (do not leave them in prose) |

This resolves the P5-vs-P7 tension explicitly: the researcher **enumerating** a follow-up is *not* role-hopping (P5 intact — it surfaces, it doesn't decide or act on orchestrator-tier work). The dispatcher **filing** the enumerated follow-up as a low-priority triage ticket is the P7 cheap-action. Triage/prioritization still happens at the orchestrator tier.

## Procedure

1. **Dispatch brief** (manual) / **`build_brief`** (autonomous) instructs the researcher: surface any follow-ups in a `## Follow-ups` section, each as a one-line filable item — `<title> — <1-line ask>`.
2. **On report return**, the dispatching tier files each enumerated follow-up: `ticket.py file --requester <persona> --target <orchestrator> --labels from-research,... --ask "<the 1-line ask>"`. Default low-priority / for-triage; target the orchestrator (orchestrator/recovery), not the operator.
3. HO triages on the next grooming pass (prioritize, `block`, or resolve-as-stale).

## Common failure modes

- **Surface-don't-file** (the retired anti-pattern): listing follow-ups in prose and leaving filing "to HO". Recurred on the deep-research eval AND T-0735 (Yegge). Fix: file them.
- **Filing pure no-action captures** (the over-correction): a "someday polish / no action now" musing is NOT a follow-up — it goes to `BACKLOG.md` or the ledger, not an inbox ticket (the inbox-consumer treats fresh inbox tickets as actionable). The boundary: a *genuine, actionable* follow-up files; a vague "someday" does not. See [[feedback_no_action_captures_not_inbox_tickets]].
- **Targeting the operator**: surfaced follow-ups target the orchestrator for triage, never `target=operator` (that queue is for operator actions/ratifies).

## Boundary

- Covers the brief instruction + the filing convention. Does NOT (yet) cover B11 auto-parsing the `## Follow-ups` section of autonomous watch reports to file sub-follow-ups — B11 currently files one ticket per headline finding. That auto-parse is a flagged future enhancement; until it lands, autonomous in-report sub-follow-ups are enumerated-but-not-auto-filed (rare for single-topic watch reports; HO can file on digest review).
- Does not change the researcher persona's tool grants — it stays surface-only.

## Cross-references

- ORG-012 (information-inflow bubble-up — HO authors SOPs)
- P7 (cost asymmetry: cheap action), P5 (roles don't hop), D1 (preserve the operator's bandwidth)
- `tools/watch_runner.py` `build_brief` (autonomous brief) + B11 actuation phase
- Memory pin `feedback_research_dispatch_files_surfaced_followups`
- T-0740 (this convention's encode); T-0735 (the Yegge dispatch that surfaced the gap)
