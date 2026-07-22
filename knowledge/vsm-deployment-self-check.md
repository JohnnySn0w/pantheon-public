# Deployment self-check — is your instance still viable?

Stafford Beer's 1984 retrospective paper reproduces four diagnostic points "made in a learned
journal (*Brain and Strategy*, 1983)" — not Beer's own writing; his words: "All four have been
expounded in my own writings, but not I think with such pith; therefore I take leave to reproduce
them here as direct quotations." They read as a ready-made checklist for a deployed instance of
this seed: run them whenever you add or remove a role, add or drop an opt-in extra (`memory-hub`,
`third-party-auditor`), or change the `sub-orchestrator` seat count. See
`knowledge/vsm-instantiation.md` for the full role-to-VSM mapping these questions check against.

*(Attribution corrected 2026-07-22 after third-party audit: an earlier revision presented the
explanatory glosses as the questions and attributed the set to Beer's own earlier piece. The
question texts below are now verbatim from the 1984 paper, primary-source-verified; each is
followed by the paper's own explanatory gloss, then this seed's application.)*

## The four questions (verbatim, as reproduced in Beer 1984 from *Brain and Strategy* 1983)

**1. "Is management presiding over a 'viable system'?"**

Gloss (quoted): "If any of Beer's five necessary functions are removed from, say, a subsidiary,
then its abilities to operate successfully may well be killed."

Applied here: each `sub-orchestrator` seat is supposed to be a full S1–S5 system at its own
recursion level (`vsm-instantiation.md` §0). If a seat is stripped of its own review discipline,
its own read of the environment, or its own identity/rules — that seat is not leaner, it is
crippled, and recursion demands the full complement at every level.

**2. "Does subsystem Five truly represent the entire system within the context of larger, more
comprehensive and more powerful systems?"**

Gloss (quoted): "If this function, or subsystem, is unable to find a way to represent the
essential qualities of the whole system to the larger meta-system, then the system's survival is
in question."

Applied here: the operator is S5 (`vsm-instantiation.md` §1, §4). If the orchestrator's
day-to-day identity-embodiment ever drifts into *substituting for* the operator's goal-level
closure rather than representing it, that is this question failing.

**3. "Do managers often fail to understand the need for subsystems Two and Four?"**

Gloss (quoted): "Business people have little difficulty recognizing the need for subsystems One,
Three and Five. If Two is missing, activity in One can turn deadly and self-defeating as units
fight for resources and against entropy; if Four is missing, Three and Five can collapse into
each other, leaving the critical Five subsystem a mere functionary."

Applied here, both halves: S2 in this org is structural (the ticket system, WIP cap, agent-branch
+ single-push-actor flow, shared SOPs — `vsm-instantiation.md` §1), not a persona — deploy more
than one `sub-orchestrator` seat without that layer and unit-fighting (branch collisions,
double-claimed work, conflicting writes) is the first thing that breaks. And skipping S4 (the
`specialist` role) is not merely "no research": per the gloss it invites S3/S5 collapse — the
policy tier degrading into an operations functionary with no outward or forward sensing.

**4. "Do the Three, Four and Five subsystems need to form a Three-Four-Five subsystem to
encourage 'synergy' and interactivity?"**

Gloss (quoted): "Without a constant interaction and exchange of information between these three
functions, Three is vulnerable to 'narrow tunnel' syndrome and Four is exposed to the perils of
'flights of imagination'."

Applied here: the mechanism binding S3 (orchestrator) to S4 (specialist) and S5 (operator +
rulebook) should be the shared ticket+ledger loop — S4 findings FILE surfaced follow-ups into
the ticket system, and design/policy work should cite operational evidence. If that binding is
goodwill rather than a mechanism, this question is failing even if all three functions
individually exist.

## Using this checklist

A deployment answering any of these questions badly has a named pathology (in the quoted glosses
above) and a named fix surface in `knowledge/vsm-instantiation.md` §3 (the viability-gap notes
per role/extra) and §4 (the second-head reconciliation, if relevant). This is a standing check,
not a one-time bring-up step — re-run it whenever the deployed role/extra set changes.

## Source

Beer, S. (1984). "The Viable System Model: its provenance, development, methodology and
pathology." *Journal of the Operational Research Society*, 35, 7–26 (reprinted as ch. 1 of
Espejo & Harnden, eds., 1989). The four questions originate in *Brain and Strategy* (1983,
vol. 4, issue 9) — a journal commenting on Beer's model, which Beer quotes approvingly — and are
reproduced above verbatim from the 1984 paper's text, verified against the primary source
2026-07-22.
