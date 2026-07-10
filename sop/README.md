# sop/

Standard Operating Procedures. The "how to do X correctly" tier (`sop/standard/`) and the "what to do when X breaks" tier (`sop/remediation/`). Hand-curated under D2; per-SOP operator-ratification at surface time before the SOP becomes precedent HO autonomously cites.

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

## Authoring + ratification flow

1. **HO drafts** the SOP (per D2 hand-curated).
2. **HO surfaces draft** to the operator for ratification at surface-time.
3. **the operator ratifies** the recommended-remediation path rankings (for `remediation/`) or the procedure steps (for `standard/`) before the SOP becomes precedent.
4. **Per-SOP ratification logged** in ledger entries — separate audit chain per SOP, not bulk-ratified.

This per-SOP gate prevents bulk-author drift where HO compiles many SOPs without validating individual paths.

## Boundary

- **Not principles** (org-identity rules — see `rulebook/principles.md`).
- **Not directives** (project-calibrated rules with multi-month scope — see `rulebook/directives.md`).
- **Not policies** (operational calibrations of directives — see `rulebook/policies/`).
- **Not lessons** (post-incident synthesis on class-of-failure — see `lessons/`).

SOPs are the *procedural* tier — what you do, in what order, when. Higher tiers are *what to be / what to do at high level*; lessons are *what we learned*; SOPs are *here's the script*.

## Founding entries

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
