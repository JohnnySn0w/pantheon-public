---
aliases: [ORG-029]
id: ORG-029
title: Pantheon distribution model — upstream-reference scaffolding + fork-microcosm history + path-disjoint reconciliation
status: Ratified 2026-06-05 by the operator (authored by Orchestrator/HO)
build_disposition: built
---

# ORG-029 — Pantheon distribution model: upstream-reference scaffolding + fork-microcosm history + path-disjoint reconciliation

**Status:** **Ratified 2026-06-05 by the operator** (authored by Orchestrator/HO, via ratify ticket [[T-0849]]). Per ORG-015 `adr/` is the ratifiable substrate.

**Ticket:** [[T-0848]] (Specialist surfaced + sharpened the framing from the operator's proposal, Telegram msg 347; ADR drafting is HO's domain per ORG-012). **Related:** `sop/standard/pantheon-bringup.md` §11 (the keep-vs-clear split this ADR turns into an ongoing relationship), [[ORG-028]] (portable-config-topology — per-account config portability, the runtime-state analog of this repo-level model).

## Context

Pantheon is shipped as the **reference instance's live repo** — the operator's `pantheon-config`, history and all (no pre-cleaned seed; it is in-house). Two questions follow from that:

1. **What happens when someone clones it to stand up their own Pantheon?** They need the system (governance + methodology + tooling) but NOT the reference instance's work history (its tickets, ledger, postmortems, research arcs).
2. **What is the ongoing relationship between a clone and upstream** as upstream's scaffolding improves?

The bringup SOP §11 already answers (1) as a *one-time* decision — a **keep-vs-clear split**: KEEP the durable scaffolding (`rulebook/`, `sop/`, `adr/`, `tools/`, `tests/`, `skills/`, `.githooks/`, directory README/TEMPLATE spines), CLEAR the instance-specific runtime history (`tickets/{inbox,active,done}/`, `ledger/`, `notes/`, `incidents/`, `reports/`, `knowledge/episodic/`, etc.). This ADR observes that **that split is also the answer to (2)** and makes it an ongoing model.

A second clarification motivates the ADR: the **intra-instance git gotchas do not apply to cross-operator clones.** Branch-protection friction and multi-clone divergence (a recurring HO/Specialist/Recovery pain — two checkouts of the *same* repo racing pushes) are artifacts of **one operator's N agents sharing one repo as a common write-target**. A cross-operator clone is a *different* operator's *fork* that **never pushes upstream** — so those gotchas evaporate by construction. The distribution model must not import intra-instance write-coordination machinery that a fork doesn't need.

## Decision

Adopt a **three-part distribution model**:

1. **Upstream `pantheon-config` is pull-only REFERENCE scaffolding.** A clone treats upstream's scaffolding subtree — `rulebook/`, `sop/`, `adr/`, `tools/`, `tests/`, `skills/`, `.githooks/`, and the directory README/TEMPLATE spines (the §11 KEEP set) — as the canonical *system*. The clone pulls improvements FROM upstream; it never pushes TO upstream.

2. **A clone develops its OWN microcosm in a fork.** Its instance-history subtree — `ledger/`, `tickets/`, `notes/`, `incidents/`, `reports/`, `finances/`, `knowledge/episodic/`, `knowledge/watch/reports/` (the §11 CLEAR set) — is *its own*, started fresh and grown independently. This is the fork's private history; upstream neither sees nor wants it.

3. **Reconciliation is a path-scoped merge of the scaffolding subtree, leaving the history subtree untouched.** Because the KEEP set and the CLEAR set are **path-disjoint** (KEEP ∩ CLEAR = ∅, enforced by the directory structure), a fork reconciles by pulling *only the scaffolding paths* from upstream — e.g. `git checkout upstream/main -- rulebook/ sop/ adr/ tools/ tests/ skills/ .githooks/` (or sparse-checkout / `read-tree` of the same path set) — and committing that to its fork. The fork's own history subtree is never in the merge's path scope, so **there is no content-level conflict resolution** — the path partition *is* the reconciliation boundary.

**The OPERATOR-JUDGMENT set is per-file, defaulting to pull-generic.** `bugs/` and `lessons/` hold a mix of generic (Claude-Code / systemd / language-runtime knowledge — good to pull from upstream) and instance-specific entries. The reconciliation treats these as opt-in per file rather than whole-subtree; the safe default is to pull the generic upstream entries and keep local additions. (A future refinement may split these into a generic subtree and a local subtree to restore clean path-disjointness; out of scope here.)

**The MANIFEST-CLASS set reconciles structure-from-upstream, content-stays-local.** `repos.yaml`, `instance.yaml`, and `pinned-rules/` are neither pure-KEEP nor pure-CLEAR: their *structure/schema* is framework (ports from upstream) but their *grants / role→persona bindings / per-persona brief content* is instance data (stays local). A fork pulls the shape and keeps its own fill. This category was named by ORG-030 (Q3 residual + Q4): `instance.yaml` is the role→persona binding layer, `repos.yaml` is the role-keyed grant template, and `pinned-rules/` is a framework README/structure populated with instance-specific persona briefs. Treat them like the operator-judgment set — per-file judgment, never a wholesale KEEP/CLEAR placement.

## Why this works (justifies via)

- **P1 (architecture over policy).** The reconciliation boundary is enforced by the **path structure**, not by a merge-discipline policy or a human resolving conflicts. KEEP and CLEAR being path-disjoint means a path-scoped checkout *cannot* touch the fork's history — the structure makes the clean reconciliation a mechanical certainty, not a careful practice. This is the same structural move §11 already makes for the one-time clear; the ADR extends it to the steady state.
- **P4 (traceability) + P6 (tensions preserved).** Upstream's audit chain (its ledger) and a fork's audit chain stay separate by construction — neither contaminates the other. The "should a fork ever contribute scaffolding back?" tension is named, not collapsed: the default is pull-only (one-directional), and any future upstreaming would be a deliberate, separate, human-curated PR — never an automatic push.
- **D10 (context-frugality / references-not-transclusion).** A fork references upstream's scaffolding via a remote + path-scoped pull rather than vendoring a frozen copy it must hand-maintain. Upstream stays the single source of truth for the system; forks track it.

## Relationship to the intra-instance gotchas

The branch-protection rules, the "FF local main to origin before merging" discipline, the multi-clone-divergence reconciliation pattern, and the `sg pantheon-shared` write-coordination — **all of these are intra-instance** (the operator's three agents sharing one repo as a write-target). They are correct *there* and stay. They are **explicitly NOT part of the distribution model**: a cross-operator fork has one operator, never pushes upstream, and so needs none of that machinery. Conflating the two is the error this ADR forecloses.

## Implementation (on ratification)

| Step | Work |
|---|---|
| 1 | `sop/standard/pantheon-bringup.md` §11 gains a forward-reference to this ADR + a one-paragraph "distribution posture" statement (upstream = pull-only reference; your fork = your microcosm; reconcile scaffolding paths periodically). §11's one-time keep/clear is reframed as the first application of the ongoing model. |
| 2 | Add a short `tools/`-level reconciliation helper OR a documented `git checkout upstream/main -- <KEEP paths>` recipe in the SOP, so a fork's scaffolding-pull is one command. |
| 3 | Confirm the KEEP/CLEAR path sets in §11 and the reconciliation recipe stay in sync (a single canonical list of scaffolding paths, referenced by both — P1/D10). |

This ADR is a *posture* decision; the bringup-SOP edit (step 1) is the only immediate change. The reconciliation helper (step 2) is a small follow-on, filable after ratification.

## DECISIONS

- **Path-scoped checkout as the reconciliation mechanism (not git-subtree, not a vendored copy) — justifies via P1** — a path-scoped `checkout`/`read-tree` of the KEEP set is the minimal mechanism that exploits the existing path-disjointness; git-subtree adds a split/graft layer with no benefit when the boundary is already a clean path partition, and a vendored copy forfeits upstream-as-source-of-truth (D10).
- **Pull-only / never-push-upstream as the default direction — justifies via P6 + P4** — keeps the two audit chains uncontaminated and avoids re-importing the intra-instance write-coordination gotchas into the cross-operator case; the rare "contribute scaffolding upstream" path stays a deliberate human-curated exception, not the model.
- **`bugs/`+`lessons/` handled per-file, not whole-subtree — justifies via P6** — they are genuinely mixed (generic vs instance-specific); forcing them into KEEP or CLEAR would either pull instance-noise or drop reusable knowledge. Named as the one place the clean path-disjointness is imperfect, with a future-split refinement flagged rather than forced now.

## Addendum — fork conventions adopted (2026-07-27, [[T-2073]])

The first outside fork (wish-factory) independently invented three conventions that operationalize this model from the *fork's* side. Adopted as shipped defaults (below-goal methodology, principles-as-ratification — P1/P6 justification + ledger; not a re-ratification of the posture above, a filling-in of it). Documented in `sop/standard/pantheon-bringup.md` §11 "Fork conventions"; template shipped as `FORK.md.template` at the repo root.

- **`FORK.md` — the fork's delta-index, deliberately OUTSIDE the KEEP set.** The KEEP/CLEAR partition tells a fork what upstream owns; it did not give the fork a durable place to record *its own* delta (new files, customized KEEP-path files, rebrand record, fork-ADR index). Editing `README.md` for that fails — `README.md` is upstream-owned scaffolding carrying upstream's own specifics (names, URLs), so a fork that reconciles it (root docs are pulled by *explicit name*, not by the dir-scoped recipe) re-imports upstream content over its notes, and one that doesn't reconcile it drifts. `FORK.md` at the repo root is in neither the KEEP directories nor any CLEAR subtree path, and is deliberately never named in the reconcile pathspec, so a path-scoped reconcile never touches it: it is the fork-owned map that survives every pull. Classed with the MANIFEST-CLASS set (upstream ships the template shape via `FORK.md.template`; the filled-in file stays local). NB: the dir-scoped reconcile recipe (DECISION 1) reaches KEEP *directories* only — root-level upstream files (`README.md`, `BACKLOG.md`, `FORK.md.template`) are pulled by explicit name, per Implementation step 3's KEEP-set/recipe-sync note.
- **A fork-private ADR namespace (`<prefix>-NNN`, e.g. `COY-NNN`).** Upstream owns `ORG-NNN`; a fork authoring `ORG-050` collides the day upstream does. A fork prefixes its own ADRs from its name/initials and files them in `adr/` (a KEEP path) — safe because the checkout-based reconcile (DECISION 1) only overwrites files that exist upstream, so fork-prefixed ADRs are additive and never deleted. This extends the existing project-ADR namespacing rule in `adr/README.md` (echo-prismatic/pantheon-pwa ranges) to the cross-operator-fork axis.
- **"README.md is upstream scaffolding" as an explicit rule.** Makes the KEEP-membership of `README.md` (and the directory spines) an operator-facing instruction rather than a thing each fork rediscovers by losing notes to a pull.
