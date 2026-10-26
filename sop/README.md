# sop/

Standard Operating Procedures. The "how to do X correctly" tier (`sop/standard/`) and the "what to do when X breaks" tier (`sop/remediation/`). Hand-curated under D2. `standard/` SOPs are adopted by HO below D3's goal-level bar; `remediation/` SOPs keep per-SOP the operator ratification (see §Authoring + adoption flow).

Per the operator msg_822 + msg_826 (military-FM analogy: FM 3-0 doctrine + FM 6-0 SOPs + lessons-learned feedback loop) — SOPs sit in the operational-tier sandwich between principles/directives/policies above and real-time decisions below.

## Buckets

| Folder | Tier | Owns | When to add |
|---|---|---|---|
| `standard/` | "How to do X correctly" | Procedural runbooks for recurring ops; pre-flight checklists; reference cards. | When a procedure has stabilized + multiple agents would benefit from following the same exact steps. |
| `remediation/` | "What to do when X breaks" | Incident-pattern playbooks: failure shape → recommended remediation paths (3-5 ranked) → autonomy-vs-bubble-up D3 placement. | When an incident pattern recurs ≥2× and HO would otherwise re-derive the discipline maneuver each time. |

## Per-SOP file shape

### standard/ entries

```markdown
# <SOP name — distinctive one-line>

## When to use this
<Trigger conditions; entry-criteria for this SOP being applicable.>

## Procedure
<Step-by-step. Each step: action + verification + escalation criterion if step fails.>

## Common failure modes
<What goes wrong; how to recognize early; structural vs tactical fixes.>

## Boundary
<What this SOP does NOT cover; pointers to neighbors.>

## Cross-references
<Principles, directives, policies, ADRs, prior tickets.>
```

### remediation/ entries (per T-0157 ratified shape)

```markdown
# <Pattern name — distinctive one-line>

## Failure shape
<What the maneuver looks like in flight; characteristic signal HO sees.>

## Precedent cases
<List incidents / b-cycle reports / tickets where this fired. Cite specifics.>

## Recommended remediation
<3-5 paths, ranked by precedent + cost. Each path: what HO does, why,
expected outcome, when to escalate.>

## Autonomous-vs-bubble-up D3 placement
<When HO applies a path autonomously vs surfaces to the operator. Cite D3 wait-time-bound + specific bubble-up criteria.>

## Cross-references
<Principles, directives, policies, ADRs, prior tickets, this ticket's parent.>
```

## Authoring + adoption flow

- **`standard/`:** HO (or the authoring seat) drafts and adopts. An SOP is methodology, below D3's goal-level bar (D3 as amended 2026-06-11, the operator msg 3733/3735), so there is no ratify-ask. The status line reads `Adopted <date> by <author>` with the justification. Adoption gets a ledger entry, and the operator sees it in the digest and can override. An SOP that would set a goal-level rule (D3's reserved list: money, secrets, access grants, outward-facing or irreversible acts) goes to the operator. SOPs that the operator ratified before the amendment keep that status.
- **`remediation/`:** per-SOP the operator ratification stays (T-0157 ratified design), because a remediation SOP pre-authorizes HO's autonomous discipline maneuvers and fixes their D3 placement. Draft one SOP at a time, don't bulk-author, and log each ratification in the ledger. The rule is still in use: `persona-daemon-liveness-recovery.md` was ratified 2026-10-17 (TG 8525).

Pre-amendment history: until 2026-06-11 every SOP went through per-SOP the operator ratification, to prevent bulk-author drift.

## Boundary

- **Not principles** (org-identity rules — see `rulebook/principles.md`).
- **Not directives** (project-calibrated rules with multi-month scope — see `rulebook/directives.md`).
- **Not policies** (operational calibrations of directives — see `rulebook/policies/`).
- **Not lessons** (post-incident synthesis on class-of-failure — see `lessons/`).

SOPs are the *procedural* tier — what you do, in what order, when. Higher tiers are *what to be / what to do at high level*; lessons are *what we learned*; SOPs are *here's the script*.

## Founding entries

Historical record of the 2026-05-10 founding set. The Ratification column is not maintained. Each SOP's own status line, mirrored in the `standard/` and `remediation/` Auto-indexes, is current.

| File | Tier | Source | Ratification |
|---|---|---|---|
| `standard/team-orchestrator-brief-construction.md` | standard | migrated from `notes/brief-template.md` (T-0160 + T-0195) | inherited from prior brief-template ratification |
| `standard/b9-team-brief-reference.md` | standard | migrated from `notes/b9-team-brief.md` (T-0160 + T-0195) | inherited |
| `standard/auto-resume-operator-runbook.md` | standard | migrated from `notes/auto-resume.md` (T-0160 + T-0195) | inherited |
| `standard/agent-role-map.md` | standard | migrated from `notes/agent-role-map.md` (T-0160 + T-0195) | inherited |
| `standard/handoff-restart-runbook.md` | standard | T-0197 (pending HO authoring + per-SOP ratification) | pending |
| `standard/plan-first-dispatch.md` | standard | T-0197 (pending) | pending |
| `standard/cross-account-ops-discipline.md` | standard | T-0197 (pending) | pending |
| `standard/team-orchestrator-dispatch-checklist.md` | standard | T-0197 (pending) | pending |
| `remediation/<9 patterns>` | remediation | T-0196 (pending HO authoring + per-SOP ratification of 9 patterns) | pending |

## Related

- `rulebook/README.md` — three-tier framework these SOPs operate beneath.
- `rulebook/directives.md` §D2 (hand-curated docs) + §D6 (plan-first dispatch).
- `lessons/` — class-of-failure synthesis that often surfaces the need for a new SOP.
- `incidents/` — primary-source incident reports SOPs cite as Precedent.
- T-0157 (remediation framework) + T-0160 (standard-bucket extension) + T-0195 (this migration) + T-0196 + T-0197 (founding-entry authoring).
