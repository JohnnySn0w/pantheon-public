# Deployment self-check — is your instance still viable?

Stafford Beer's 1984 retrospective paper reproduces four diagnostic questions, quoted there
"exactly as he gives them" from his own earlier piece (*Brain and Strategy*, 1983). They read as
a ready-made checklist for a deployed instance of this seed: run them whenever you add or remove
a role, add or drop an opt-in extra (`memory-hub`, `third-party-auditor`), or change the
`sub-orchestrator` seat count. See `knowledge/vsm-instantiation.md` for the full role-to-VSM
mapping these questions are checking against.

## The four questions (Beer 1984, verbatim)

**1. Has any of the five functions been removed or crippled in a unit?**

> "If any of Beer's five necessary functions are removed from, say, a subsidiary, then its
> abilities to operate successfully may well be killed."

Applied here: each `sub-orchestrator` seat is supposed to be a full S1–S5 system at its own
recursion level (`vsm-instantiation.md` §0). If a seat is stripped of its own review discipline,
its own read of the environment, or its own identity/rules — that seat is not leaner, it is
crippled, and recursion demands the full complement at every level.

**2. Does S5 truly represent the whole system within its containing systems?**

Beer's own framing: S5's closure is "a profoundly difficult" determination, not an org-chart
box — his sharpest illustration is that a single office-holder is not automatically the same
thing as System Five itself. Applied here: the operator is S5 (`vsm-instantiation.md` §1,
§4). If the orchestrator's day-to-day identity-embodiment ever drifts into *substituting for*
the operator's goal-level closure rather than representing it, that is the question failing.

**3. Is S2 present, or are units fighting?**

> "If Two is missing, activity in One can turn deadly and self-defeating as units fight for
> resources and against entropy."

Applied here: S2 in this org is structural (the ticket system, WIP cap, agent-branch +
single-push-actor flow, shared SOPs — `vsm-instantiation.md` §1), not a persona. If you deploy
more than one `sub-orchestrator` seat without that structural layer, this is the first thing
that breaks — branch collisions, double-claimed work, conflicting writes to shared state.

**4. Are 3-4-5 in constant exchange, or is S3 tunneling while S4 free-floats?**

> "Without a constant interaction and exchange of information between these three functions,
> Three is vulnerable to 'narrow tunnel' syndrome and Four is exposed to the perils of 'flights
> of imagination'."

Applied here: the mechanism binding S3 (orchestrator) to S4 (specialist) and S5 (operator +
rulebook) should be the shared ticket+ledger loop — S4 findings FILE surfaced follow-ups into
the ticket system, and design/policy work should cite operational evidence. If that binding is
goodwill rather than a mechanism, this question is failing even if all three functions
individually exist.

## Using this checklist

A deployment answering any of these questions badly has a named pathology (Beer's own language
above) and a named fix surface in `knowledge/vsm-instantiation.md` §3 (the viability-gap notes
per role/extra) and §4 (the second-head reconciliation, if relevant). This is a standing
check, not a one-time bring-up step — re-run it whenever the deployed role/extra set changes.

## Source

Beer, S. (1984). "The Viable System Model: its provenance, development, methodology and
pathology." *Journal of the Operational Research Society*, 35, 7–26. The four questions are
reproduced there from Beer's *Brain and Strategy* (1983, vol. 4, issue 9), which this seed has
not independently verified firsthand — treat the 1984 reprint (quoted above) as the source of
record.
