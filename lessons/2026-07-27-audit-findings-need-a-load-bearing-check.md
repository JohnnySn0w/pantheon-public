# Audit findings need a "is this thing load-bearing?" check before they spawn build work

**Date authored:** 2026-07-27 (surfaced by the operator, TG 6520/6522, during the agent-monitor watchdog arc)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a second audit-spawned arc reaches merge before anyone asks who consumes the audited thing — a 2nd recurrence promotes this from anecdote to pattern; or when the audit tooling gains a structural place to record consumer/criticality, at which point the check stops being prose discipline.

## Observation

The agent-monitor dashboard arc ran roughly like this:

1. **2026-07-20** — the S3\* audit (third-party auditor pass) filed `S3STAR-WN-2026-07-20-001`: "agent monitor listener matches documented stalled-dashboard signature." Filed as **T-1885**.
2. A liveness watchdog was designed, implemented, reviewed (one REBOUND for a ship-dark gate), merged as PR #110, deployed, and armed. A **duplicate** pre-existing bash watchdog was discovered mid-arc and retired (**T-2070**).
3. Once armed, the watchdog worked: it surfaced that the wedge was recurring on a tightening interval (~2.4h → ~30min) and escalated.
4. That escalation is what finally put the dashboard in front of the operator — at which point the answer was **"I basically don't use it"** (TG 6511), and shortly after, **"it's crashing itself with the newest update anyways. Let's not spend more time on it"** (TG 6520).

The work was individually well-executed at every step. The finding was real; the wedge was real; the watchdog worked and proved itself against a live wedge. **The question nobody asked was whether the audited service was load-bearing** — and it was askable on day one, in one sentence, before any code.

## What this actually is

**An audit that does not check criticality becomes a work generator.** An audit's job is to find divergence between claimed and actual state. It is extremely good at that, and it is *indifferent to whether the diverging thing matters*. Left unchecked, every finding converts into a ticket, and tickets convert into build arcs, because the finding format itself carries urgency ("matches documented failure signature") but carries no consumer.

This is a **variance-amplifier**, not a variance-absorber, and it is the exact inverse of what D1 (the operator's bandwidth) asks the chain to do. The org spent an implementer cycle, a reviewer cycle, a deploy, an arming step, a duplication reconciliation, and a follow-up hardening ticket — on infrastructure whose sole consumer turned out to be the monitoring of that infrastructure.

The tell was available early and was visible in the artifacts: the watchdog's own alert routing was *direct-to-operator*, and yet at no point had anyone established that the operator read the dashboard it protected. A monitor whose escalation target is not the monitored thing's user is a monitor without a user.

## The check

Before an audit finding spawns build work, answer in one line, in the ticket:

> **Who consumes this, and what breaks for them if it stays broken?**

Three honest outcomes:

- **A named consumer with a named breakage** → build, with the criticality recorded so the effort can be sized against it.
- **No consumer, or the only consumer is other infrastructure** → the finding may be a *retire* candidate, not a *fix* candidate. Surface the retire/keep fork to the operator **before** building, not after.
- **Genuinely unknown** → that is itself the first ticket, and it is a one-message ask, not an arc.

Note the asymmetry that makes this cheap: asking costs one line and one operator message. Not asking cost this arc a full build-review-deploy-arm cycle plus a duplication cleanup, and still ended at "don't spend more time on it." The reroll cost is far below the asking cost here — which is the same calculus as `feedback_calibrate_autonomy_higher_per_msg1271`, running in the other direction: autonomy means deciding *how* to do the work, not *whether the work has a customer*.

## Scope — what this does NOT say

This is **not** "distrust audit findings" or "audit less." The S3\* audit did its job correctly; the finding was accurate and the wedge was real. Nor does it mean every finding needs operator sign-off — that would route variance upward and violate D1 in the opposite direction.

It says only: **criticality is an input to sizing, and it belongs at the top of the arc**, where it is one sentence, rather than at the end, where it is a sunk build.

## Instrument-trust corollaries (added 2026-08-06, T-2540)

The original check asks whether the audited **thing** is load-bearing. These two ask a prior
question: whether the audit's own **instrument** can be trusted to have measured anything at all.
Both came out of specialist's 2026-07-29 flag-audit arc (T-2315/T-2321), verified by hand-adjudicating
the unmeasured pile — which is itself the point of the second one.

**1. Trustworthiness is a property of the CHANNEL, not the hit.** Decide it once, where the channel
is defined — not per-result, by eye, after the fact. A prose-contaminated channel (one where your
own notes, TL;DRs, or examples share the surface you are grepping) gets you an eyeball list that
yields no verdict, because you cannot tell a real hit from a mention. The measurable corollary is
the sharp one:

> **A number that moves 8× when you fix the channel was never a measurement.**

Specialist's count went 34 → 4 once the channel was cleaned; the residue was his own prose being counted
as hits. A denominator that swings with the instrument revision while the verdict stays put (see
`bugs/transcript-command-sweep-segmentation-shatters-invocations.md`) is the same tell one layer
down: *the null may be defensible while the count is not — they are two different claims and deserve
to be reported separately.*

**2. A sweep that ends in a JUDGEMENT call has an unmeasured step.** Either the classifier is
**mechanical** or the sweep is not finished. The value of this framing is that it is checkable
*before* you run anything — you do not need the results to know the sweep is incomplete. Instance:
T-2243/T-2299, where the scan **did** find the items and the *judgement* step lost them.

> **The tell is the word.** If you catch yourself needing "plausibly" — "these N plausibly need X" —
> write "these N need check X" instead, and the finding survives into the next session as work
> rather than as an impression that evaporates.

## Local evidence

- **T-1885** — the audit finding and the watchdog arc (PR #110).
- **T-2070** — the duplicate 07-20 bash watchdog discovered and retired mid-arc; itself a symptom of building before checking what already existed (see `feedback_check_installed_units_before_building_infra`).
- **T-2084** — the escalation-severity fix; a failed restart alerted nobody while a *successful* repeat restart cried outage. Worth noting this defect survived a review cycle: the alerting was reviewed for correctness of mechanism, not for whether its severities matched what an operator would need to do.
- **T-2083** — the surviving genuine value: the stats (prompt cache hit/miss among them) that the operator *does* consider worth having, queued to land on the kino stats page rather than in a dashboard nobody opens.
