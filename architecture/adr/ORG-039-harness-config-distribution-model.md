---
aliases: [ORG-039]
id: ORG-039
title: Harness-config distribution model — per-harness FRAMEWORK/INSTANCE split + instance.yaml binding + host-provisioning contract
status: Ratified 2026-06-30 by the operator (Telegram 3608/3609, via T-1474) — distribution/product axis, the harness-config sibling of ORG-029
date: 2026-06-30
---

# ORG-039 — Harness-config distribution model

**Status:** **Ratified 2026-06-30 by the operator** (Telegram 3608 "It looks good to me" / 3609, via [[T-1474]]), drafted by Orchestrator (HO). Per ORG-015 `adr/` is the ratifiable substrate; this is the distribution/product axis (how Pantheon ships to a self-hoster), the same class as [[ORG-029]] which the operator ratified — so it carries the same ratification gate rather than principles-as-ratification self-adoption. **Publish (T-1475, done):** the genericized `claude-code-config` reference template is published as a PRIVATE release mirror (`example-org/claude-code-config`, HEAD `875c55e`); the public flip stays the separate [[T-1473]] gate, operator-held.

**Ticket:** [[T-1468]] (the S1 design step in `notes/harness-config-distribution-plan.md`; ADR drafting is HO's domain per ORG-012). **Related:** [[ORG-029]] (pantheon-config distribution model — this is its harness-config sibling), [[ORG-028]] (portable-config topology — per-harness repos, role-keyed layout, `instance.yaml` manifest; this ADR is its distribution leg), [[ORG-030]] (portable-config migration plan), [[ORG-031]] (harness abstraction + daemon onboarding), `notes/harness-config-distribution-plan.md` (Recovery's codex-reviewed two-track plan; the S-sequence), [[T-1469]] (→recovery: the build that consumes this design).

## Context

A runnable Pantheon instance needs **four layers**, not one:

1. **Framework / content** — `pantheon-config` scaffolding (rulebook/sop/adr/tools). Distribution covered by [[ORG-029]]. ✅
2. **Agent-runtime config** — the per-harness config repos (`claude-code-config`, `codex-config`): `CLAUDE.md` + `.claude/agents` + `settings.json` + MCP wiring + pinned-rules persona briefs. **Required to boot agents. NOT covered by any distribution model today** — the gap this ADR closes.
3. **Control surface** — `pantheon-pwa` / kino. ✅ on the release mirror.
4. **Host / runtime provisioning** — unix users/groups, mailbox dirs+perms, install/symlink locations, env/secret placeholders, service/launch wiring, deploy-key expectations. Implicit on the reference host; **must be made explicit** or the 3-repo bundle is source-complete but not bootable (codex review, adopted in the plan note).

ORG-029 distributes pantheon-config via a **KEEP/CLEAR path-disjoint** model (pull-only reference scaffolding upstream; the fork grows its own history; reconciliation is a path-scoped checkout). ORG-028 established the **topology** of layer 2 (config repos keyed by *harness*, per-agent files keyed by *role*, one `instance.yaml` binding role→instance-data) but explicitly **deferred the distribution mechanics** to a follow-on. This ADR is that follow-on: the **distribution model for the harness-config repos** — the layer-2 analog of ORG-029 — plus the **layer-4 provisioning contract** that makes a fork bootable.

**Stale-line correction:** ORG-028 §1 states "*No `codex-config` exists today … a future shape this topology accommodates, not a present artifact.*" As of 2026-06-30 that is **stale** — both `claude-config` (claude-code) and `codex-config` (`/home/codex/codex-config`, remote `github-codexconfig`) exist live. This ADR supersedes that parenthetical: codex-config is a present artifact and a covered harness, shipped as a fast-follow (Decision §5).

## Decision

Adopt a **per-harness FRAMEWORK/INSTANCE distribution model**, bound by `instance.yaml`, shipped with a host-provisioning contract.

### 1. Per-harness FRAMEWORK (ports, pull-only) vs INSTANCE (stays, fork-local)

For **each** harness-config repo (`claude-code-config`, `codex-config`), partition the contents:

- **FRAMEWORK (ports to a self-hoster; pull-only reference, like ORG-029's KEEP set):**
  - role-keyed `.claude/agents/` skeletons — `orchestrator` / `specialist` / `recovery` (the generic bringup triad; structure + role-generic instructions, no persona content),
  - `settings.json` **template** — hook wiring, permission structure, env-var *names* with placeholder values,
  - **MCP wiring placeholders** — server registration shape with `${...}`-style token/path placeholders,
  - the **`pantheon-org-discipline` skill** (org-wide worker conventions — generic by construction),
  - **pinned-rules STRUCTURE** — the `pinned-rules/` README + section skeleton/template, not the filled briefs.
  - > A fork records its harness-layer delta (which of these FRAMEWORK templates it customized) in the same root-level `FORK.md` that ORG-029 blesses for the framework layer — see ORG-029 Addendum "fork conventions" + `sop/standard/pantheon-bringup.md` §11. One fork-owned delta-index spans both layers.
- **INSTANCE (stays in the reference fork; never ships; like ORG-029's CLEAR set):**
  - persona-brief **content** (the actual `orchestrator.md`/`recovery.md`/`specialist.md` text — operational state, account specifics, history),
  - **tokens / secrets** (bot tokens, deploy keys, API credentials),
  - **account / unix names** (`claude`, `claude-research`, `codex`, `claude-fallback`), **bot refs**, tmux session names,
  - **absolute host paths** (`/home/codex`, `~/.claude/agents`, mailbox/hub dirs).

The boundary is **structural** wherever possible (ORG-029's P1 move): **role-keyed filenames are the template; persona-named brief content is the fill.** `pinned-rules/` is **MANIFEST-CLASS** (ORG-029's third category): its *structure* ports, its *content* stays — handled per-file, never wholesale KEEP/CLEAR.

### 2. `instance.yaml` is the single binding seam

A self-hoster authors **one** `instance.yaml` (ORG-028's manifest, extended) mapping each role → its instance data: persona name, unix account, bot-token reference, tmux session, **which harness(es) the agent runs**, and — critically — **every absolute path** the harness templates need (`~/.claude/agents`, personas dir, MCP server paths, mailbox/hub dirs, install locations). The FRAMEWORK templates reference these through the manifest (or a generated env file); **no operator-host path is hard-coded** (`/home/codex`, `/home/the-ancestor`, native `veneration`, `sg`). This makes codex's "parameterize all absolute paths" requirement **structural**, not a review checklist item: a path that isn't in `instance.yaml` is a portability bug the leakage scan (§3) catches. **Secrets are bound by reference only** — `instance.yaml` carries a *name/path* to each token or credential (e.g. `token_ref: <path-or-env-name>`, as `instance.yaml.example` shows), **never an inline secret value**. An inline credential in the manifest is a leak the §3 scan must reject.

### 3. Host/runtime provisioning contract (layer 4) + release gates

The distribution ships a **lightweight but explicit** provisioning contract so the bundle boots:
- unix users/groups (or a documented **single-user mode**), mailbox dir creation + perms, config symlink/install locations, env/secret **placeholders**, **systemd-user / tmux launch examples**, deploy-key/SSH expectations, and a **per-role boot smoke**.
- **Release gates (both required before a harness template ships):** (a) a **leakage scan** — no secrets, persona-brief content, account names, or absolute host paths present in the FRAMEWORK set; (b) a **clean-home dry-run** — agents boot from the template alone in a throwaway account.
- **Genericization convention — dangling upstream references (T-1987, non-blocking):** bare `T-NNNN` ticket refs and `ADR-NNN` design refs not resolvable within a mirror are upstream-internal provenance, not leaks. They are handled by a standing one-line README note (safe-to-ignore), NOT stripped and NOT added to the leak-scan fail-gate. See [[notes/2026-07-27-mirror-dangling-refs-convention]].

### 4. Decoupling contract — a fork without the harness repo must not half-break

`pantheon-config` is coupled to the claude-code harness repo (the `pantheon-org-discipline` skill lives there, validated by `pantheon-config/.githooks/pre-commit` + `tools/directive_skill_drift_check.py`; SOPs reference persona briefs at harness paths). The distribution **formalizes the decoupling**: the drift-check **degrades to an explicit SKIP with an actionable message** when the harness repo is absent/unconfigured (strict validation only when the harness path is configured/present — upgrading today's silent soft-fallback), and all harness-path references route through `instance.yaml`. A fork that pulls `pantheon-config` **without** a harness repo configured gets a clean skip, never a broken hook.

### 5. Reference-harness-first; build under the target name; live rename is a separate track

- **`claude-code-config` ships first-class** as the reference harness; **`codex-config` is a documented fast-follow** (experimental until it has a clean generic template + sandbox read/write smoke). Both are *covered* by this per-harness model; only the *ship order* is reference-first — shipping both first-class doubles the boot matrix (settings, sandbox roots, MCP/env, briefs, smokes); make one stack excellent first (codex review, adopted).
- **Build the template under the target name `claude-code-config` now.** The live `claude-config`→`claude-code-config` rename ([[T-0915]]) is a **separate compatibility/cleanup track** (GitHub auto-redirects + ssh-alias/deploy-key bridges handle the gap). **Do not block the distribution artifact on a live mutation** — it adds operational risk for zero artifact benefit (codex review, adopted).

## Why this works (justifies via)

- **P1 (architecture over policy).** Role-keyed filenames + the `instance.yaml` binding make the FRAMEWORK/INSTANCE boundary a property of the **structure** (a path/name partition + a manifest indirection), not a merge-discipline a human must apply — the same structural move ORG-029 makes with path-disjointness. The leakage scan turns "did we de-specify?" into a mechanical gate.
- **P4 (traceability) + P6 (tensions preserved).** Secrets, persona content, and the reference instance's account topology **never enter the shippable set** — the two operators' audit/identity surfaces stay disjoint by construction. The build-vs-live-mutation tension and the both-harnesses-vs-one-first tension are **named and sequenced**, not collapsed.
- **D10 (references-not-transclusion).** A fork binds to upstream's harness template by **reference** — either a **pull-only live reference** or a **pinned release-mirror artifact** (a tagged/versioned upstream release the self-hoster pulls from) — **not a vendored frozen copy** maintained in-tree; upstream stays the single source of the harness shape (mirrors ORG-028's no-submodules / composition-at-the-home-level stance). Both forms are references, not transclusion; the pinned-mirror option is offered because a versioned release artifact gives a self-hoster **reproducible installs** (relevant for the friends BETA), which a live pull-only reference does not guarantee.
- **D3 (below-goal methodology, HO-drafted; the operator ratifies the product axis).** The *methodology* is HO's to draft; the *decision to ship Pantheon this way to outsiders* is product-level and stays the operator's — hence Draft-pending-ratification, not self-adoption.

## Relationship to the existing ADRs and the intra-instance gotchas

- **ORG-028** gave the topology (harness-keyed repos, role-keyed files, manifest); **this ADR gives the distribution mechanics** for those repos and corrects ORG-028 §1's stale codex-config line.
- **ORG-029** is the pantheon-config sibling; this reuses its three vocabulary classes (FRAMEWORK≈KEEP, INSTANCE≈CLEAR, MANIFEST-CLASS for `pinned-rules`/`instance.yaml`) so the two models are learnable as one.
- **Intra-instance gotchas stay out of the distribution** (ORG-029's foreclosure, restated): branch-protection, multi-clone-divergence reconciliation, `sg pantheon-shared` write-coordination, and the **multi-writer harness-repo access problem** (per-agent deploy keys, [[T-0913]]) are all artifacts of *one operator's N agents sharing one repo*. A cross-operator fork has one operator and never pushes upstream — so the distribution model must not import that machinery. (The multi-writer access work is real, but it is a *reference-instance cleanup* track, not a distribution requirement.)

## Implementation / sequencing

This ADR is the **S1 design** in `notes/harness-config-distribution-plan.md`. On ratification it unblocks:

| Step | Owner | Work |
|---|---|---|
| S3a | recovery (build) → orchestrator (GitHub publish) | [[T-1469]] — genericize the `claude-code-config` reference template (target name, parameterized paths, drift-check SKIP contract, layer-4 provisioning artifacts), bundle with seed `pantheon-config` + kino, tied by `instance.yaml`. |
| S4 | recovery | clean-home dry-run bringup in a throwaway account; leakage scan + per-role boot smoke as release gates. |
| S2 | orchestrator (the operator uploads keys) | reference-instance cleanup, **non-blocking / parallel**: [[T-0913]] per-agent deploy keys → Recovery-brief rejoins the harness repo; [[T-0915]] live `claude-config`→`claude-code-config` rename. Gates nothing in the build track. |
| S3b/S4b | recovery | codex-config generic template + sandbox smoke — fast-follow after the reference harness passes S4. |

`sop/standard/pantheon-bringup.md` gains a "harness-config distribution posture" paragraph (the layer-2/4 analog of the §11 posture ORG-029 added), filable with S3a.

## DECISIONS

- **Per-harness partition keyed on role-filenames + `instance.yaml`, not a curated allowlist — justifies via P1.** The portable boundary is the role-keyed structure ORG-028 already established; making distribution ride that structure (template = role files, fill = persona content/manifest) means the leakage scan is a mechanical gate, not a judgment call — the harness-config analog of ORG-029's path-disjointness.
- **Layer-4 provisioning folded into THIS design, not deferred — justifies via P1 + P6.** Codex's review showed a source-complete 3-repo bundle still isn't bootable without host provisioning; binding it into the distribution model (vs a later ticket) keeps "shippable" honest and the tension visible.
- **claude-code-config first-class / codex-config fast-follow — justifies via P5 (cost) + P6.** Both are covered by the model; shipping both first-class doubles the boot matrix for no beta benefit. The codex coverage is named (not dropped) so the fast-follow is a sequencing decision, not a silent omission.
- **Build under target name now; live rename decoupled — justifies via P1.** The artifact depends on the template's *name*, not on mutating the live repo; coupling the build to a disruptive live rename would add operational risk with zero artifact benefit (codex correction, adopted).
- **Draft-pending-operator-ratification, not principles-as-ratification self-adoption — justifies via D3.** This decides how Pantheon ships to outside operators (product axis), the same class as the operator-ratified ORG-029; HO drafts, the operator ratifies.
