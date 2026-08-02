# The role set as a Viable System Model instantiation

**Status:** Reference doc, generic (not episodic/watch content). Presents the seed's role set —
`orchestrator`, the optional `sub-orchestrator` N+ tier, `specialist`, `recovery`, and the two
opt-in extras `memory-hub` / `third-party-auditor` — explicitly as an instantiation of Stafford
Beer's Viable System Model (VSM), so a deploying operator can see what each piece buys and what
opting out leaves. Grounded in Beer's own retrospective paper (see Sources below); claims here
follow that source's own sourcing flags — anything not directly attributable to Beer's text is
marked as such.

See also: `lessons/cybernetics-mapping-vsm-lens.md` — an earlier, independently-arrived-at VSM
reading of this same design (dated prior to this doc). This doc supersedes it as the more
rigorously Beer-grounded account; the two are not in conflict, but read the lesson for the
design-discipline framing (P1–P6/D1–D10) this doc doesn't repeat.

## 0. Nominating the recursion levels

Beer is explicit that boundaries and the choice of system-in-focus are the modeler's own
closure-act, with no privileged top ("the topology of recursion demands an exact replica in
each case," Beer 1984). This seed nominates:

- **Level +1 — the operator's operation.** The containing viable system: everything the
  deploying operator runs, of which this org is one operational unit. The operator is this
  level's S5.
- **Level 0 — this org (the system-in-focus).** The org this repo instantiates. Its
  environment: the repos, services, and platforms it acts on, plus the vendor/platform weather
  (billing policy, harness changes) it must sense.
- **Level −1 — each sub-orchestrator seat.** Per Beer's recursion theorem, each seat is a FULL
  viable system: its own S1 (its worker subagents), its own S2 (its worktree/branch
  conventions), its own S3 (its plan-approval + review discipline over workers), its own S4
  (reading the repo/ticket environment), its own S5 (its persona identity and the rules it
  holds). This is why chain-of-justification is meant to propagate explicitly at every dispatch
  boundary — the discipline is the same system one level down, not a diluted copy.

## 1. The mapping (level 0)

**How to read this table — the load-bearing correction.** Map **functions to systems, not personas
to systems.** The five systems (plus the channels) are *roles the org performs*; a persona or
artifact **occupies whichever functions it is performing**, and most occupy **several, across
different systems**. Persona→system 1:1 is the wrong shape — it cannot come out cleanly, because
actors are cross-system by nature.

So the rows below read as *what serves each function*, not *who is assigned to which box*. The
recurring cross-system actors are collected in §1a, and that section is not a list of exceptions to
the table: **the spanning IS the structure.** A deployment that reads only the table and skips §1a
will re-derive the persona→system 1:1 error this frame exists to prevent.

| VSM function | Realization in this org |
|---|---|
| **S1 — Operations** | Up to three things serve S1, each a value-producing unit. (a) The **N+ sub-orchestrator seat tier** — each seat drives a project arc end-to-end and is itself a full viable system (level −1, see §0); N is a per-deployment choice, **including N=0**. (b) The **orchestrator's own operational acts** — the orchestrator *when producing*: authorship, decision-log entries, dispatch execution. **These acts are S1 even though the same persona's orchestration is S3** — that is not an N=0 special case, it stays true at every N (see §1a). (c) Optionally, a **privacy-partitioned specialized unit** — an integral level-0 S1 unit **on par with the seats**, separated for concern reasons (implementation, not topology): its own account, its own private-data domain, its own operator-facing channel. "On par" means on par in VSM level and S1 status — **not** identical in channels or accountability routing (see §1a). *At N=0 with no specialized unit, (b) is the whole of S1 — one persona wearing S1 and S3 at once, which is viable but cell-sized; see §3.* |
| **S2 — Coordination / anti-oscillation** | Deliberately **structural, not a persona**: the ticket system + WIP cap (serializes claims, prevents resource fights), agent-branch + single-push-actor merge flow (serializes writes to main), the mailbox protocol, the lint/guard suite, shared SOPs. Beer: without S2, S1 units "fight for resources and against entropy" — S2 here is the convention-and-tooling layer that makes that structurally hard. **What you install for S2 is the conventions and tooling, not another agent** — this is the answer a deploying operator needs, and it is load-bearing: S2 is a design property of the system, not a seat you staff. |
| **S3 — Internal regulation / resource bargain** | Two things serve S3. (a) The **written governing documents** — the principles, directives, policies, and **ratified architecture decisions functioning as governance**: the standing control surface of what the org has decided must hold. This half is easy to miss and load-bearing; without it S3 collapses into "whoever is orchestrating today". (b) The **orchestrator's regulation function** — the orchestrator *when orchestrating*: dispatch, resource bargains, priority ordering, merge/push authority, accountability for the S1 collective. Dispatch briefs ARE the resource bargain: scoped autonomy + worktree/branch resources in exchange for decisions-with-justification accountability. The intervention channel is direct messaging/status-checks; the synergy function is the cross-arc knowledge lift (`bugs/`, `lessons/`, the decision log). **This is only the orchestrator's S3 arm** — the same persona's producing acts are S1 (§1a). One orchestrator per instance remains the invariant (§3, §4), but that is a claim about *dispatch authority*, not about S3 being one persona. |
| **S3\* — Sporadic audit** | **A channel/class, not a single tool** — direct inspection of S1's output that bypasses normal S3 reporting. Reading S3\* as "the auditor persona" is the same 1:1 error the frame warns about; it has several instances at once: the **reviewer role** (adversarial review of an implementer's diff), a **standing audit worker** filing severity-ranked findings, the **guard / lint / leak-scan hooks** (structural audit with no persona at all), the orchestrator's own **read-the-defining-surface discipline** (verifying claims in durable artifacts; inspecting worker output directly rather than trusting its report), and the **third-party-auditor extra** — a different-vendor harness auditing the org's claims (opt-in, off by default — see §3). What unites them is bypassing the routine reporting channel, which is Beer's actual independence requirement. **Documented extension, not Beer's specification:** Beer's S3\* is internal-but-channel-independent — the literature is silent on vendor-heterogeneity. Reading S3\* as "a different-vendor harness" is a defensible extension (tooling heterogeneity guarantees the auditor doesn't share the audited channel's blind spots), and this seed presents it as an extension, not as something Beer stated. |
| **S4 — Intelligence (outside-and-future)** | The **specialist** role: watch topics = environmental scanning (vendor policy, billing, harness landscape); research dispatches = directed sensing. The orchestrator's own design/ADR work is the S3–S4 homeostat in action (operational reality ↔ what's coming). |
| **S5 — Identity / policy / closure** | **The operator.** Ultimate authority, ethos, closure; the operator holds goal-level ratification and externals. Nobody sits outside the model: **the operator is IN the system** as S5 (and as the level-+1 metasystem), not an external user of it. **Where the rulebook and the orchestrator sit (corrected 2026-08-02, superseding the prior "operator + rulebook + orchestrator, jointly"):** the rulebook is the artifact S5's closure is *inscribed in*, but as a standing control surface it **serves S3**, and it is cross-system *by lifecycle* — S1 as authored → S3 as governance. The orchestrator **drafts** policy (an authoring act, S1) and holds S3 regulation, but does **not** hold S5 closure. Seating all three in S5 jointly is the persona→system 1:1 error this model exists to prevent. |
| **Memory/continuity substrate** | The **memory-hub extra** — see §2; deliberately mapped as substrate, not as a sixth system. |
| **Algedonic channels** | Escalation rules that BYPASS normal reporting: failure-triggered alerts, "urgent enough to skip the queue" escalation, cap-hit/recovery chains, a sibling instance's poke into a drifted orchestrator. Beer's two design features hold: they fire after the normal channel had its chance (timeout-gated), and they are not restricted to negative signals — a seat surfacing an unexpected win routes the same way. |

**Recovery — the recovery FUNCTION is autonomic, not a numbered system.** The recovery function is
not a sixth system and not S3\*: it is the org's *autonomic loop* — the machinery that keeps the
plant alive (restarts, repair, drift-pokes) without engaging planning or identity. This is distinct
from the audit function (which inspects claims, not liveness).

**Scope note (corrected 2026-08-02).** That is a claim about the *function*, **not** about whoever
performs it. In this org's own deployment the recovery seat is cross-system: recovery /
algedonic-response **+ S2-construction** (building the substrate itself) **+ S3\*** (grooming and
index audits). Read "recovery is not S3\*" as a persona-level claim and you have reproduced exactly
the persona→system 1:1 error §1 exists to prevent — it is *because* an actor is not a box that the
same seat's repair work can be autonomic while its grooming audits are S3\*.

## 1a. Cross-system occupancy (this spanning IS the structure)

Per §1's central rule, actors routinely occupy several systems at once. **This section is not a list
of exceptions to the table — it is the table's actual shape.** A deployment that omits it will
re-derive the persona→system 1:1 error the frame exists to prevent. The load-bearing cases:

- **The orchestrator:** **S1** (authorship, decision-log entries, dispatch execution) **+ S3**
  (orchestration, resource allocation, merge authority).
- **The operator:** **S5** (policy, closure) **+ bounded S3** for the specialized private-domain
  unit's resource/accountability channel (guardrails below).
- **The third-party auditor:** **S1** (dispatched worker) **+ S3\*** (spec-review auditor).
- **The recovery seat:** **recovery / algedonic response** (restoring wedged units; the operator arm
  of the alarm channel) **+ S2-construction** (building the substrate itself: hosts, git server,
  container layout) **+ S3\*** (grooming / index audits).
- **Governing documents:** **S1 as authored** (the act of writing) **→ S3 as governance** (once
  ratified and binding) — cross-system *by lifecycle*. The same artifact, different system,
  depending on where it is in its life.
- **The specialized unit's knowledge base:** **S1** (that unit's operational domain) **+
  S2-adjacent** (organizational-memory store).

**Accountability can partition between two holders.** The seats' accountability channel (resource
bargain, dispatch briefs, merge review) attaches to the orchestrator's S3; a privacy-partitioned
unit's attaches **directly to the operator**, by privacy design — routing personal-domain
accountability through the orchestrator would defeat the separation that unit exists for. So
level-0 S1 units can partition between two accountability holders.

**Guardrails on the operator's S3-attachment.** For that one unit the operator deliberately wears
S3-attachment and S5 at once. To keep this distinct from the "S5 collapsed into S3" pathology (§3),
the collapse is **BOUNDED**: (a) it applies ONLY to that unit's private-domain
resource/accountability channel; (b) it does NOT generalize to the technical seats; (c) it does NOT
replace the orchestrator's S3 for any org operation; (d) it MUST be re-evaluated if that unit ever
gains dispatch, merge, or decision-log-curation authority. Any breach of (a)–(c) observed in
practice is a **pathology signal to surface, not a precedent**.

**The one-orchestrator invariant survives, precisely stated:** one *orchestrator / one dispatch
authority per instance* — **not** "one accountability holder", and not "S3 is one persona". A
privacy-partitioned unit is not an orchestrator (no dispatch, merge, or curation role), which is
exactly what places it as S1 rather than a second S2/S3/S5. One instance, one orchestrator, N+
seats, plus optionally one privacy-partitioned specialized unit.

## 2. memory-hub: substrate beneath the model

- **Substrate reading (adopted here):** memory-hub is the *channel infrastructure* the model's
  arrows run on — the mailbox hub is what S2 coordination and algedonic signaling physically
  traverse; the shared memory is organizational continuity. Beer's Second Principle makes
  channel capacity a first-class design object; memory-hub is where this org makes channels
  durable, auditable, and account-independent. Substrate failure is not one system failing —
  it degrades EVERY vertical and horizontal channel at once.
- **Rejected reading, trigger named:** one could model memory-keeping as an operational unit
  producing "continuity" as its product (an S1 unit). Rejected because memory-hub exercises no
  autonomy and holds no resource bargain — it is a mailbox and a memory, not a decider.
  **Rebalance trigger:** if the role ever gains active curation duties (grooming memory,
  arbitrating contested records), revisit — that would make it an S1 unit (or part of S2) with
  real variety of its own.

## 3. Viability gaps — what opting out leaves

Beer's own pathology narratives, applied per deployable piece:

- **N=0 seats (base config, one orchestrator only):** viable but *cell-sized* — the
  orchestrator absorbs S1 and does operations directly. Nothing oscillates (there's one actor),
  but there is no recursion depth, no parallel variety, and the orchestrator's S3/S5 time is
  consumed by S1 work — the org cannot scale its variety to match a growing environment
  (Ashby). This is a legitimate starting state, not a failure state; the gap is
  growth-limiting, not fatal.
- **No S2 discipline (skipping the conventions/tooling):** Beer, directly: S1 units "fight for
  resources and against entropy." Concretely: branch collisions, double-claimed work,
  conflicting writes to shared state — the exact failure classes the WIP cap and
  single-push-actor flow exist to prevent. First thing that breaks when N goes from 1 to 2.
- **No S4 (no specialist seat):** "cannot adapt" — and under-recognized precisely because the
  cost is invisible until the environment shifts (Beer). For an agent org the environment shifts
  often (vendor policy, billing, harness capabilities); a deployment that opts out of S4 should
  be told it is choosing blindness to exactly the class of change that repeatedly costs real
  money.
- **No third-party-auditor extra:** audit rides the same channels it audits; self-confirming
  green. The wrong-layer-GREEN / false-verification failure class is the concrete evidence for
  why the extra exists. Weakest form of the gap: the orchestrator's own direct-inspection
  discipline remains (internal 3\*), so the loss is the *heterogeneity* guarantee, not all audit.
- **No memory-hub extra:** every channel becomes ad-hoc and account-local; coordination and
  algedonic signaling lose their durable medium; continuity across session resets depends on
  each agent's private state. Opting out is only sensible for single-agent deployments where
  there is nobody to coordinate with.
- **S5 collapsed into S3 (not an opt-out — a standing pathology risk):** Beer's "decerebrate
  cat." The orchestrator is structurally exposed to this exact failure (S5-adjacent,
  promoted-from-S3 shape: dives into operational problems "never to emerge"). Guards against
  this: keeping goal-level ratification with the operator, the rulebook-as-artifact (S5 closure
  exists outside any session), and dispatch thresholds that push against the orchestrator
  absorbing S1/S3 work. Name this pathology and its guards when standing up an instance — it is
  the most likely failure mode of the whole one-orchestrator-plus-seats shape.
- **3-4-5 non-interaction:** S3 "narrow tunnel" / S4 "flights of imagination" (Beer). Bind
  research reports to FILE surfaced follow-ups into the ticket system (S4 output lands in S3's
  workflow), and require design/ADR work to cite operational evidence (S3 reality lands in
  S4/S5 deliberation). The mechanism should be a shared ticket+ledger loop, not goodwill.

## 4. A "second head" — two different shapes, and they resolve differently

Neither shape puts two orchestrators inside one viable system. But they are **not the same
deployment**, and collapsing them is how the persona→system 1:1 error gets back in through the
side door. Decide which one you are building.

**Shape A — a separately-instantiated head (level +1).** Two differently-scoped hemispheres, each
its own full deployment. The frame is level +1: **the operator's operation contains multiple viable
systems as S1 units**, with the operator as the shared S5 and an explicit hand-over protocol as
level-+1 S2 (anti-oscillation: clean ownership transfer, no double-driving). Each hemisphere is
base-one-orchestrator internally. "Multiple heads" here means multiple seed *instances* under one
operator's S5 — never two orchestrators in one instance.

**Shape B — a privacy-partitioned specialized unit inside ONE instance (level 0).** The far more
common want, and the one the level-+1 reading gets wrong: a unit with its own account, its own
private-data domain, and its own operator-facing channel, but sharing this instance. That is **not**
a second head at all — it is an **integral level-0 S1 unit on par with the seats** (§1 row S1(c)),
separated for *concern* reasons rather than topological ones, with the accountability partition and
bounded S3+S5 guardrails in §1a. It is not an orchestrator because it holds no dispatch, merge, or
curation authority — which is precisely what makes it S1 rather than a second S2/S3/S5.

**The framing that gets retired:** for Shape B, "exception" / "outside the model" language is wrong
and should not be carried into a deployment's own docs. Such a unit is integral, not a carve-out.
If your deployment calls something a "second head", check which shape you actually have — the
hemisphere/second-head vocabulary is useful **domain and comms-surface description**, and it is
**not a VSM placement**.

**Seed consequence, unchanged:** ship exactly one orchestrator persona per instantiated system.

## Sources

- Beer, S. (1984). "The Viable System Model: its provenance, development, methodology and
  pathology." *Journal of the Operational Research Society*, 35, 7–26. Reprinted as ch. 1 of
  Espejo, R. & Harnden, R. (eds.), *The Viable System Model: Interpretations and Applications of
  Stafford Beer's VSM*, John Wiley & Sons, 1989. The direct-quotation source for every Beer
  attribution above.
- See `knowledge/vsm-deployment-self-check.md` for Beer's own four diagnostic questions as a
  standing checklist, separate from this mapping.
