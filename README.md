# Pantheon — public scaffolding seed

This is the **portable scaffolding** for Pantheon: a multi-agent orchestration system
run as a small fleet of Claude Code (and peer-harness) instances on a single host. It is
the clean, shareable mirror of a live reference instance — the *system* (governance +
methodology + tooling), with **none of the reference instance's operational history**.

## What's here (and deliberately not)

**Included — the durable framework:**
- `rulebook/` — principles, directives, policies, the goals registry
- `adr/` — architecture decisions (ORG-NNN) that define the system
- `sop/` — standard operating procedures (incl. `sop/standard/pantheon-bringup.md`, the bring-up runbook) + remediation runbooks
- `tools/` — the operational tooling the orchestrator + agents run
- `tests/`, `skills/`, `.githooks/`
- `bugs/`, `lessons/` — reusable technical gotchas + methodology lessons (generic)
- `knowledge/` — generic reference (glossary, infra-abstractions, the VSM instantiation + deployment self-check) only
- `instance.yaml`, `repos.yaml`, `pinned-rules/` — manifest-class: framework *structure*, reference-instance *fill* (replace for your instance — see below)

**Excluded by construction — the reference instance's private microcosm:**
`ledger/`, `tickets/`, `notes/`, `incidents/`, `reports/`, `finances/`,
`knowledge/episodic/`, `knowledge/watch/` — none of it is here, and this repo's
git history starts clean (a fresh root commit), so none of it is recoverable from
history either. This is the keep/clear path-partition defined in **ORG-029**, applied
at generation time.

## Standing up your own instance

1. Read `sop/standard/pantheon-bringup.md` end-to-end — it walks a fresh host → working fleet. (It was clean-room-validated: naive operators reached a working bring-up from the doc alone.)
2. Replace `instance.yaml` (role → persona/account/harness bindings) and `repos.yaml` (your repo registry + grants) with your own. The shipped versions are the **reference instance's** config, kept as a worked example — see the ⚠️ banners in each.
3. The fleet ships with **generic role names** — `orchestrator`, `specialist`, `recovery`. These are the stable axis the tooling keys on (the `AGENT_CONFIG` keys in `tools/context_fill_monitor.py`, ticket `target:`/`requester:` fields, the per-role state-file names, `instance.yaml`). `pinned-rules/` holds the reference instance's per-role briefs as worked examples; author your own. Two more pieces of the role set are documented in `instance.yaml` but **not part of the base config**: a **sub-orchestrator N+ tier** (horizontally scalable — you choose the count, including N=0, the base config shown) for scaling operations beyond one orchestrator, and two **opt-in extras, both off by default** — `memory-hub` (a channel/memory substrate) and `third-party-auditor` (a different-vendor harness auditing the org). The persona/role set is presented explicitly as a **Viable System Model instantiation** in `knowledge/vsm-instantiation.md`, so you can see what each piece buys and what skipping it leaves — pair it with `knowledge/vsm-deployment-self-check.md` for a standing "is your deployment still viable" checklist.
4. **Name your daemons whatever you like.** The role tokens above are just defaults — nothing forces you to keep them. If you want your agents to have personality (the reference instance used persona names), pick names and set them as the `AGENT_CONFIG`/`instance.yaml` keys and the `%i` systemd-template instances; the bring-up SOP (§2 accounts, §6 monitors, §10 generalizing to N agents) walks exactly where each name plugs in. Roles are the generic axis; the persona name is an instance-local label you layer on top — or skip.
5. ⚠️ The recovery tooling (`tools/restart_drain.py`, `tools/context_fill_monitor.py`) ships with the reference instance's service names pre-filled and currently fails *silently* on a mismatch — `pantheon-bringup.md` §6 flags this; replace the names and confirm against your real service. (Hardening tracked upstream.)

## Distribution model (ORG-029)

This seed is **pull-only reference scaffolding**. You fork it, your fork grows its **own**
microcosm (its own ledger/tickets/etc., started fresh), and you never push back upstream.
As the framework improves upstream, you reconcile by pulling *only the scaffolding paths* —
the keep/clear sets are path-disjoint, so a path-scoped pull can't touch your history. See
`adr/ORG-029-pantheon-distribution-model.md` for the full model.

## Provenance

Generated from a reference instance's `pantheon-config` via a path-scoped extract of the
ORG-029 KEEP set into a clean repo, then **neutralized**: persona names and operator-specific
details are converged onto generic role tokens (`orchestrator`, `specialist`, `recovery`,
plus the optional `sub-orchestrator` tier and the `memory-hub`/`third-party-auditor` extras)
and scrubbed, so the scaffolding reads role-generic. The neutralization is a deterministic,
re-runnable transform, so the seed can be refreshed forward from upstream as the framework
evolves (ORG-030 Phase-3).
