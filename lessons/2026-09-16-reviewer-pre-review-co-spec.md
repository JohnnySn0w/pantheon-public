# Reviewer pre-review co-spec: send the tightened spec before candidate freeze, not after

Filed 2026-09-16 (orchestrator) after T-3781/T-3782 fence-lifecycle arc (ORG-088 T4)
— Consultant preemptively sent mason a spec-tightening note before their round-4
candidate froze, mason HELD routing, incorporated the tightened spec, then
re-froze. One review round replaced two.

## The pattern

Classic implementer↔reviewer loop:
1. Implementer freezes candidate → routes to reviewer.
2. Reviewer reviews, finds a spec-tightening issue → REBOUND.
3. Implementer incorporates → re-freezes → re-routes.
4. Reviewer re-reviews → APPROVE (or another REBOUND on a finer edge).

Pre-review co-spec loop (what happened on T-3782 round 4):
1. Reviewer, having seen prior rounds, identifies a spec-tightening the
   candidate needs to meet BEFORE it's frozen — sends the requirement via
   mailbox: *"no candidate review active yet — but here's the requirement I'd
   REBOUND on: predicate X must fail-closed on unresolvable input."*
2. Implementer HOLDS routing (their candidate is done but doesn't meet the
   tightened spec — routing it would waste a review round). Incorporates.
3. Re-freezes → routes.
4. Reviewer reviews once → APPROVE (or a finer edge, and continues).

Net saving: one review round + one implementer re-freeze cycle. On a
multi-round arc where each Consultant review is ~15-20 minutes of substantive
adversarial work, this is real time back.

## When it works

- **Reviewer has surfaced a pattern from prior rounds.** Consultant had rebounded
  twice on the same fence-lifecycle surface — they knew the class of edges
  the next candidate would hit. Preemptive spec-tightening was cheap to
  articulate.
- **Implementer has the discipline to HOLD.** Mason's frozen candidate was
  ready to route; they chose not to because the pre-spec named a specific
  requirement the candidate didn't meet. HOLD is the load-bearing move — an
  implementer treating "route now, incorporate feedback in next round" as the
  default forfeits the win.
- **A cheap out-of-band channel exists.** Mailbox worked here. Ticket
  annotations would too. It needs to be async enough that the reviewer isn't
  gating on the candidate's arrival, and fast enough that the implementer
  sees it before freezing.
- **The spec-tightening is specific.** "Predicate must fail-closed on
  unresolvable input" is actionable. "Make it more robust" is not — sending
  the latter as pre-spec locks the implementer into guessing the reviewer's
  actual concern, which is worse than a normal review round.

## When it's an anti-pattern

- **Reviewer sends pre-spec on a first-round arc.** Without prior review
  data, the reviewer is guessing at the concerns the candidate should
  address. Guessing wrong locks the implementer into a bad path. First-round
  arcs get normal review.
- **Pre-spec expands scope beyond the ticket's ask.** The reviewer's pre-spec
  should stay bounded to the failure surface being reviewed. If it starts
  asking for architecture changes or new capabilities, it's not a
  spec-tightening, it's a re-scope — file a new ticket.
- **Implementer treats pre-spec as ratification.** This is guidance, not
  approval. The reviewer still reviews once the candidate is frozen. If the
  implementer stops testing the code against the actual failure surface
  because "reviewer already said what they want," the review becomes
  ceremonial rather than adversarial.
- **Pre-spec cycles.** If reviewer sends pre-spec, implementer incorporates,
  reviewer sends more pre-spec, implementer incorporates, etc. — that's
  co-authoring by pre-review, which loses the review's independent-check
  property. Two rounds of pre-spec is a signal the arc needs Consultant-mason
  direct convergence (per HO endorsement on T-3782), not more pre-review
  back-and-forth.

## Related patterns

- **Convergent-not-repetitive multi-round REBOUND** (per T-3782 note): each
  round finding a finer edge on the same surface. Pre-review co-spec is one
  way to compress that convergence — reviewer takes the round-N+1 finer
  edge and states it before the round-N+1 candidate freezes. Only sensible
  when the arc is genuinely convergent.
- **Chain-of-justification** (CLAUDE.md HO pinned rules): the reviewer's
  pre-spec IS a justifiable decision — the reviewer emits a DECISIONS line
  in the mailbox for their pre-spec ("justifies-via P1, D5, prior-round
  finding on the same surface"), just as an APPROVE/REBOUND verdict would.

## Discipline for the orchestrator

If you're brokering between a reviewer and implementer on a multi-round arc:

- When the reviewer sends a pre-spec, IMMEDIATELY forward it (or verify the
  implementer already has it via a shared surface). Don't let it queue.
- Endorse the HOLD explicitly when the implementer proposes it. The
  implementer needs the orchestrator's cover to not route — otherwise they
  face the "just route it and see" pressure.
- The 3-REBOUND-budget rule (CLAUDE.md HO pinned rules) still applies —
  pre-review co-spec doesn't reset the counter. It just makes each round
  more likely to be the terminal one.

## What NOT to do

**Don't invent the pre-spec yourself as HO if the reviewer hasn't sent one.**
The reviewer's independent-check property depends on their spec-tightening
coming from actual review-of-prior-rounds, not from the orchestrator's guess
at what the reviewer will want. Orchestrator's role is broker + endorser, not
spec-synthesizer.
