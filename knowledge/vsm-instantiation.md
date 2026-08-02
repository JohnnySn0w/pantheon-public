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

| VSM function | Realization in this org |
|---|---|
| **S1 — Operations** | Conditional on N (third-party-audit correction, 2026-07-22): at **N=0** (the base config), S1 is the **orchestrator's own operational work** — S1 must exist in any viable system, so the base is one persona wearing S1 and S3 at once (see §3's "cell-sized" gap note). When **sub-orchestrator seats are deployed**, they become the S1 units: each drives a project arc end-to-end and is itself a full viable system (level −1, see §0), and the orchestrator sheds direct operations to hold S3. N is a per-deployment choice. |
| **S2 — Coordination / anti-oscillation** | Deliberately **structural, not a persona**: the ticket system + WIP cap (serializes claims, prevents resource fights), agent-branch + single-push-actor merge flow (serializes writes to main), the mailbox protocol, the lint/guard suite, shared SOPs. Beer: without S2, S1 units "fight for resources and against entropy" — S2 here is the convention-and-tooling layer that makes that structurally hard. **What you install for S2 is the conventions and tooling, not another agent** — this is the answer a deploying operator needs, and it is load-bearing: S2 is a design property of the system, not a seat you staff. |
| **S3 — Internal regulation / resource bargain** | **The orchestrator** (one persona, always exactly one — see §3 and §4). Dispatch briefs ARE the resource bargain: scoped autonomy + worktree/branch resources in exchange for decisions-with-justification accountability. The intervention channel is direct messaging/status-checks; the synergy function is the cross-arc knowledge lift (`bugs/`, `lessons/`, the ledger). |
| **S3\* — Sporadic audit** | Two realizations. (a) The **third-party-auditor extra** — a different-vendor harness auditing the org's claims (opt-in, off by default — see §3). (b) The orchestrator's own read-the-defining-surface discipline (verifying claims in durable artifacts; direct inspection of worker output bypassing routine reports). Both bypass the routine reporting channel, which is Beer's actual independence requirement. **Documented extension, not Beer's specification:** Beer's S3\* is internal-but-channel-independent — the literature is silent on vendor-heterogeneity. Reading S3\* as "a different-vendor harness" is a defensible extension (tooling heterogeneity guarantees the auditor doesn't share the audited channel's blind spots), and this seed presents it as an extension, not as something Beer stated. |
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

## 4. Second head — reconciled via recursion, not via a second orchestrator

A deployment that wants a second "head" (a separate personal/creative or otherwise
differently-scoped hemisphere alongside this org) does NOT put two orchestrators inside one
viable system. The correct frame is level +1: **the operator's operation contains multiple
viable systems as S1 units** — this org (one hemisphere) and any other hemisphere (its own,
separately-instantiated head) — with the operator as the shared S5 and an explicit hand-over
protocol as level-+1 S2 (anti-oscillation between hemispheres: clean ownership transfer, no
double-driving). Each hemisphere is base-one-orchestrator internally. **Seed consequence:** ship
exactly one orchestrator persona per instantiated system; "multiple heads" is multiple seed
*instances* under one operator's S5, coordinated by an explicit hand-over convention — never
two orchestrators in one instance.

## Sources

- Beer, S. (1984). "The Viable System Model: its provenance, development, methodology and
  pathology." *Journal of the Operational Research Society*, 35, 7–26. Reprinted as ch. 1 of
  Espejo, R. & Harnden, R. (eds.), *The Viable System Model: Interpretations and Applications of
  Stafford Beer's VSM*, John Wiley & Sons, 1989. The direct-quotation source for every Beer
  attribution above.
- See `knowledge/vsm-deployment-self-check.md` for Beer's own four diagnostic questions as a
  standing checklist, separate from this mapping.
