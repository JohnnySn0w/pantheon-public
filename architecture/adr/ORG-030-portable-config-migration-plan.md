---
aliases: [ORG-030]
id: ORG-030
title: Portable config — open-questions resolution + migration plan (resolves ORG-028's 4 deferred questions)
status: RATIFIED 2026-06-07 by the operator (T-0881). Resolves the open questions ORG-028 deferred; execution stays gated per ORG-028's "no forcing function today" (phase tickets T-0913/0914/0915 parked pending un-gate).
date: 2026-06-06
# elements: — ORG-065 machine index of the "Migration plan" table's row
# order (T-2946 Tranche A, pair 8/8). Phase numbers repeat across rows
# (0/0/1/2/2/3/3 — a collision, since the schema requires unique E<n>
# per ADR), so E1-E7 == migration-plan table rows 1-7 (row order is the
# id); text opens with "Phase N: ..." verbatim as the audit anchor.
# Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "Phase 0 (instance.yaml manifest): author instance.yaml (role->persona/account/token-ref/session/harness); define its schema — additive new file, fully reversible"}
  - {id: E2, text: "Phase 0 (ORG-029 amendment): amend ORG-029's operator-judgment note to add repos.yaml/instance.yaml/pinned-rules/ as manifest-class; correct ORG-028 status->Ratified — doc-only, reversible"}
  - {id: E3, text: "Phase 1 (repos.yaml re-key): re-key repos.yaml grants persona->role; tooling reads role->persona via the manifest — reversible registry-file change"}
  - {id: E4, text: "Phase 2 (deploy keys): add per-agent deploy keys (Specialist, Recovery) to the claude-code harness repo (Q1) — operator-side upload, reversible via key revoke"}
  - {id: E5, text: "Phase 2 (Recovery's brief move): move Recovery's brief from pantheon-config back into the harness repo; retire the deploy-key exception — reversible (move back)"}
  - {id: E6, text: "Phase 3 (role-rename brief files): re-key persona-named brief files -> role-named (recovery.md->recovery.md, specialist.md->specialist.md, orchestrator.md->orchestrator.md) — irreversible-ish (git mv; refs churn)"}
  - {id: E7, text: "Phase 3 (Q2 repo rename): rename claude-config->claude-code-config; update ssh alias/deploy-key/remotes/refs — reversible (rename back) but churny"}
---

# ORG-030 — Portable config: open-questions resolution + migration plan

**Status: RATIFIED 2026-06-07 by the operator (via T-0881, "Ratified.").** Drafted 2026-06-06 by Orchestrator (`claude`, HO) overnight under the raised-autonomy directive (Telegram msg 3454). Per ORG-015, `adr/` is the ratifiable substrate. This ADR **resolves the 4 open questions [[ORG-028]] deferred** and lays out the migration as a reversibility-classified, sub-ticketed plan. It does **not** trigger execution — per ORG-028 the irreversible moves wait for a forcing function (a second instance imminent, or Q1 solved for an independent benefit). What ratification buys: the open questions are *answered* so the migration is turnkey when the trigger arrives.

**Author:** Orchestrator (HO) — orchestrator-drafted; the operator ratifies.

**Related:** [[ORG-028]] (the topology + the 4 open questions this resolves), [[ORG-029]] (distribution model — its KEEP/CLEAR path-disjoint partition **is** the answer to ORG-028 Q3; this ADR defers to it rather than re-deciding), [[ORG-027]] (role/domain is the stable axis), `tools/agent_role_map.py` (persona↔role binding the manifest generalizes), `repos.yaml` / [[T-0761]] (the grant registry Q4 re-keys), [[T-0496]] (Specialist brief — fills the `specialist` slot), [[T-0834]] (consumes the role-keyed layout), [[T-0399]] (this session's per-repo-deploy-key cutover — the concrete precedent for Q1's mechanism).

## Pre-resolution housekeeping

ORG-028's own status line still reads "DRAFT — awaiting ratification via T-0845," but **T-0845 was ratified** (the operator, 2026-06-06T00:43:37Z, "Ratified."). ORG-028 is therefore **ratified**; its status line is stale and is corrected in the same change that lands this ADR.

## Resolutions

### Q1 — Multi-writer write-access to a harness repo  *(the named "genuinely hard part")*

**Decision: per-agent deploy keys on the shared harness repo, write-scoping by convention + the existing `repos.yaml`/`context_fill_monitor` audit — NOT hard path-MAC, NOT a write-broker, NOT submodules.**

Rationale:
- GitHub deploy keys grant **whole-repo** write, not path-scoped write; there is no cheap native path-MAC on non-Enterprise GitHub (no custom pre-receive). So "per-agent *path-scoped* deploy keys" is not literally expressible — the honest mechanism is per-agent **whole-repo** keys + **convention + audit** scoping (each agent writes only its role-path; off-path writes are *flagged*, as `context_fill_monitor`/`repos.yaml` already flag off-path repo presence — descriptive-and-audited, not hard-blocked). This matches the existing posture (`repos.yaml` grants are "descriptive of access reality, not a new policy").
- **[[ORG-029]] removes the portability objection:** multi-writer write-coordination is an **intra-instance** concern (one operator's N agents sharing one repo). ORG-029 explicitly rules that intra-instance machinery is correct *there* and is **not** part of the distribution model (a cross-operator fork has one operator and never pushes upstream). So Q1's solution may freely use intra-instance machinery (multiple deploy keys, `sg pantheon-shared`, FF-before-merge) without any portability cost.
- **Precedent exists, proven this session:** [[T-0399]] cut HO's push identity to per-repo deploy keys. GitHub allows **N deploy keys per repo**, so giving Specialist and Recovery their own deploy keys on the claude-code harness repo (alongside Orchestrator's) is the same mechanism, already validated.
- **Write-broker (route writes through the ORG-026 message substrate with path validation)** is the only mechanism that gives *hard* path-MAC. It is heavier and unjustified for the current single-operator 3-agent reality; **reserved** for an explicit future need (e.g. a multi-operator shared instance). Named, not adopted.

**This unblocks the gating dependency:** with per-agent deploy keys + audit scoping, **Recovery's brief can rejoin the claude-code harness repo** (it was exiled to `pantheon-config` only because Recovery's key couldn't push `claude-config`). Add a Recovery deploy key to the harness repo → Recovery pushes its own role-path → the `pantheon-config` exception retires.

### Q2 — Rename `claude-config` → `claude-code-config`

**Decision: DO it (it is the portability point — the repo must be harness-named, not assistant-branded), but as a LATE, mechanical migration step, bundled with the Q4 repos.yaml re-key (same reference surface).**

- Low-risk: a GitHub repo rename **auto-redirects** old remote URLs, so nothing breaks at the instant of rename; references are updated at leisure.
- Touch list (all mechanical): the `~/.ssh/config` alias `github-claude-config` + its deploy-key `IdentityFile` (from T-0399), git remotes, `repos.yaml`, `claude-config/pinned-rules/README.md`, the symlink targets, and any doc cross-refs. Reversible (rename back).
- Sequenced LATE so it doesn't churn references mid-migration.

### Q3 — Framework / instance split inside `pantheon-config`

**Decision: ALREADY RESOLVED by [[ORG-029]] — do not re-decide. The framework/instance boundary IS ORG-029's KEEP/CLEAR path-disjoint partition.**

- **Framework (ports; KEEP set):** `rulebook/`, `sop/`, `adr/`, `tools/`, `tests/`, `skills/`, `.githooks/`, directory README/TEMPLATE spines.
- **Instance content (stays; CLEAR set):** `ledger/`, `tickets/`, `notes/`, `incidents/`, `reports/`, `finances/`, `knowledge/episodic/`, `knowledge/watch/reports/`.
- **Operator-judgment (per-file):** `bugs/`, `lessons/` (mixed generic/instance).
- **No physical repo split is performed** — ORG-029's mechanism is a *path-scoped pull from upstream*, not two repos. The "split" is the path partition, already enforced by directory structure. This honors ORG-028's "principle recorded, mechanism is a path partition not a repo fork" intent and ORG-028's "no forcing function today."
- **Residual (the only genuinely-open part of Q3):** classify the files ORG-029 did **not** list — notably `repos.yaml` and the future `instance.yaml`. Resolved under Q4 below: their *structure* is framework, their *grants/bindings* are instance data → they are **manifest-class** files (a framework-shaped template populated with instance data), handled like the per-instance manifest, NOT placed wholesale in KEEP or CLEAR. Add them to ORG-029's operator-judgment note. (`pinned-rules/` is likewise manifest-class: the README/structure is framework, the per-persona brief *content* is instance.)

### Q4 — `repos.yaml` relationship

**Decision: re-key the grant table from PERSONA to ROLE, make repo names harness-aware, and have the per-instance manifest (`instance.yaml`) supply the role→persona binding. `repos.yaml` stays org-level (not folded into the manifest) but becomes a framework-shaped template whose grant rows key on role.**

- Today: `grants: { orchestrator: rw, specialist: rw, recovery: rw }` (persona-keyed) + repo names like `claude-config` (assistant-branded).
- Target: `grants: { orchestrator: rw, specialist: rw, recovery: rw }` (role-keyed) + harness-named repos (`claude-code-config`); `instance.yaml` maps `orchestrator → orchestrator (claude acct)`, etc. Tooling that reads `repos.yaml` (ticket_lint, the provisioning helper, `context_fill_monitor`) resolves role→persona through the manifest.
- **Sequencing:** the manifest (`instance.yaml`) must exist first (it is the new indirection layer); then `repos.yaml` re-keys to role; the rename (Q2) folds in here.
- Keep `repos.yaml` as its own file (not merged into `instance.yaml`): it is the structural gate ([[T-0761]]) and the access-policy surface; the manifest is the role→who binding. Separation of concerns (P1).

## Migration plan — reversibility-classified, sub-ticketed

Per ORG-028, **execution is gated on a forcing function**; this is the ready sequence, not a trigger. Phases ordered so all-reversible/additive work can land early (even pre-trigger, as low-risk groundwork) and irreversible moves cluster last behind the operator sign-off.

| Phase | Work | Reversibility | Gate | Proposed sub-ticket |
|---|---|---|---|---|
| **0** | Author `instance.yaml` manifest (role→persona/account/token-ref/session/harness); define its schema. Additive new file. | Fully reversible (delete) | None — safe groundwork | T-0866-a |
| **0** | Amend [[ORG-029]] operator-judgment note to add `repos.yaml`, `instance.yaml`, `pinned-rules/` as manifest-class; correct ORG-028 status→Ratified. Doc-only. | Reversible | None | T-0866-b (or fold into ratify) |
| **1** | Re-key `repos.yaml` grants persona→role; tooling reads role→persona via manifest. | Reversible (registry file) | None — but verify ticket_lint/provisioning/monitor green | T-0866-c |
| **2** | Add per-agent deploy keys (Specialist, Recovery) to the claude-code harness repo (Q1). Operator-side upload like T-0399. | Reversible (revoke keys) | **the operator operator-side** (uploads keys) | T-0866-d |
| **2** | Move Recovery's brief from `pantheon-config` back into the harness repo; retire the deploy-key exception. | Reversible (move back) | Depends on Phase-2 keys | T-0866-e |
| **3** | Re-key persona-named brief files → role-named (`recovery.md`→`recovery.md`, `specialist.md`→`specialist.md`, `orchestrator.md`→`orchestrator.md`). | Irreversible-ish (git mv; refs churn) | **the operator sign-off** | T-0866-f |
| **3** | Rename `claude-config`→`claude-code-config` (Q2); update ssh alias/deploy-key/remotes/refs. | Reversible (rename back) but churny | **the operator sign-off** | T-0866-g |

**Forcing-function note:** Phase 0–1 are additive/reversible groundwork that *could* land anytime (they make the manifest real + de-persona the grant table at zero risk). Phase 2 delivers an *independent* benefit (Recovery's brief rejoins the harness repo + retires the `pantheon-config` exception) — per ORG-028 that independent benefit is itself a legitimate trigger for Q1. Phase 3 (renames/role-renames) is cosmetic-on-message and should wait for the second-instance pressure unless the operator wants it sooner.

## Justifies via

- **P1 (architecture over policy):** Q1 leans on audit-over-MAC (matching the existing descriptive-grant posture) and Q3 defers to ORG-029's path-partition — boundaries enforced by structure, not new policy. Q4 keeps `repos.yaml` (gate) and `instance.yaml` (binding) as separated concerns.
- **D4 (read related specs at source):** the resolution is reconciled against the ratified ORG-029 (which already answered Q3 and reframed Q1's portability) and ORG-027 (role axis) before deciding — not drafted in isolation.
- **P6 (tensions preserved):** the hard-MAC write-broker is named-and-reserved, not collapsed away; the rename's cost is stated, not hidden.
- **ORG-028 sequencing honored:** resolutions recorded now; irreversible execution stays gated on the forcing function.

## DECISIONS

- **Q1 = per-agent deploy keys + audit-scoping, not hard path-MAC — justifies via P1 + ORG-029** — GitHub gives no cheap path-MAC; ORG-029 removes the portability objection to intra-instance machinery, so the cheapest mechanism that matches the existing descriptive-grant posture wins; the write-broker (hard MAC) is reserved for a real multi-operator need.
- **Q3 deferred wholesale to ORG-029 — justifies via D4 + D10** — re-deciding a question a ratified ADR already answers would fork the spec; the only genuine residual (classify `repos.yaml`/manifest) is folded into Q4 + an ORG-029 operator-judgment-note amendment.
- **Execution stays gated; only the answers land now — justifies via ORG-028 sequencing + feedback_fluid_not_default_deny** — the irreversible repo-moves/renames are real outward changes; resolving the design is autonomous, executing it is operator-gated (and ORG-028-trigger-gated). This ADR is the answer set, not the trigger.
