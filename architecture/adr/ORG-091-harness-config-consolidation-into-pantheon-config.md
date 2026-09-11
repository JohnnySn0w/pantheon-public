---
aliases: [ORG-091]
id: ORG-091
title: "Harness-config consolidation — FRAMEWORK slices fold into pantheon-config/harness/{claude,codex}/, separate claude-config + codex-config repos retire"
status: Ratified 2026-09-06 by the operator via Telegram 6728 ("sounds like it's fine, just gave it a glance. lgtm"), captured in a recovery-facilitated ticket walk. Direction ratified TG 9105 (2026-09-06T06:11:43Z, "yes, go with option B for 3104"); body ratified against 2ea33260bb per the two-touch flow named on T-3104 (TG 9103).
date: 2026-09-06
author: Orchestrator (HO)
appetite: 8
build_disposition: needs-build
scoping_ticket: T-3104
supersedes: []
extends: [ORG-028, ORG-039]
relates: [ORG-028, ORG-039, ORG-049, ORG-063, ORG-068, ORG-080, T-3104, T-3102, T-3096, T-3109, T-0493, T-0495, T-0496, T-1475]
labels: [harness-controller, t-2953-arc, config-topology, fleet-substrate, org-039-follow-up]
---

# ORG-091 — Harness-config consolidation into pantheon-config

**Status:** Ratified 2026-09-06 by the operator via Telegram 6728 ("sounds like it's fine, just gave it a glance. lgtm"), captured in a recovery-facilitated ticket walk. **Date:** 2026-09-06 **Author:** Orchestrator (HO) **HITL:** Ratify-required **Related:** [[ORG-028]], [[ORG-039]], [[ORG-049]], [[ORG-063]], [[ORG-068]], [[T-3104]], [[T-3102]]

**Appetite:** 8/10

## Context

Today Pantheon has three source-of-truth repos on the VPS: `pantheon-config` (org substrate) plus two per-harness config repos, `claude-config` (in `/home/claude`) and `codex-config` (in `/home/codex`). The two harness-config repos live in per-user homes with `chmod 700`, so cross-account tooling that needs to read one from the other's UID fails. This is the concrete cause of the 87 CI-red baseline documented at T-3096 (tests walk `/home/codex/codex-config` from the claude UID and get `Permission denied`) and the T-3109 fresh-install E2E gap.

Beyond the permission issue, the topology has three secondary drift shapes measured at T-3102's audit:

1. **Repo drift.** claude-config + codex-config are edited independently and drift both from each other and from what `persona_installer` would install today. T-3102's audit found live-state divergence at material.
2. **INSTANCE-content leak into FRAMEWORK repos.** claude-config carries `pinned-rules/orchestrator.md` (persona brief content), asymmetric with specialist.md + recovery.md which live in `pantheon-config/pinned-rules/`. codex-config hardcodes `/home/codex` paths + the operator's chat_id `456474197` — INSTANCE-class values baked into FRAMEWORK templates in violation of [[ORG-039]] §1.
3. **De-facto pinned-rules-home drift.** The pantheon-config `pinned-rules/README.md` still names `claude-config/pinned-rules/` as canonical home per [[T-0493]], but five of seven pinned-rules files (Recovery / Specialist / Metis / Atlas / Mnemosyne + orchestrator-codex) already live in `pantheon-config/pinned-rules/`. Only `orchestrator.md` sits in claude-config. The canonical-home claim is de-facto obsolete.

T-3104 surfaced three candidate shapes for the fix: A (partition-in-place, keep separate repos + distribute FRAMEWORK slice per [[ORG-028]] par 4), B (consolidate FRAMEWORK slices into `pantheon-config/harness/{claude,codex}/` subdirs, retire the separate repos, public mirror becomes a generated artifact), C (rename + partition, keep separate repos, cheapest incremental improvement). Cairn's audit recommendation was B. The operator ratified the direction (Telegram 9105).

## Riskiest assumption

**The independent-clocks benefit that [[ORG-028]] §1/§4 named as the reason for one-repo-per-harness is preserved at the RELEASE-artifact layer, not the source-of-truth layer.** Under this ADR, source of truth for the FRAMEWORK slice consolidates into `pantheon-config/harness/{claude,codex}/`, but the release cadence for the public mirror (`claude-code-config`, [[T-1475]]) is decoupled: the mirror-generation pipeline runs on its own schedule against whatever `pantheon-config` sha the operator chooses to release from. If this decoupling fails to produce a genuinely-independent release cadence (e.g. because operators end up releasing on every pantheon-config commit for convenience), we have added coupling without keeping the benefit.

Falsifier: at three months post-migration, is the mirror release cadence measurably decoupled from pantheon-config commit cadence (release count < pantheon-config commit count for that window, and release timing not lockstep with pantheon-config commit timing)? If NO, the coupling collapsed and this ADR's supersession of ORG-028's independent-clocks claim was overreach — the reversal path is to re-extract the harness subdirs into standalone repos via reverse-subtree.

## Decision

Adopt **shape B** — consolidate the FRAMEWORK slices of both harness-config repos into `pantheon-config` as subdirectories, retire the separate `claude-config` + `codex-config` repos post-migration, and generate the public self-hoster mirror as a downstream artifact.

### 1. Repo topology

- `pantheon-config/harness/claude/` — the FRAMEWORK slice of what was `claude-config`. Includes `agents/`, generic `pinned-rules/` templates, `settings.json.template`, hook wiring, MCP scaffolding — everything harness-generic per [[ORG-039]] §1.
- `pantheon-config/harness/codex/` — the FRAMEWORK slice of what was `codex-config`. Same partition principle. Absolute paths and instance-specific values (chat_id, `/home/codex`) route through `instance.yaml` per ORG-039 §2.
- `pantheon-config/pinned-rules/orchestrator.md` — INSTANCE-slice persona brief. Joins the existing `specialist.md` / `recovery.md` / `metis.md` / `atlas.md` / `mnemosyne.md` / `orchestrator-codex.md` per the pattern the majority already follows. Retires the [[T-0493]] canonical-home-in-claude-config claim as superseded by this ADR.
- Separate top-level `claude-config` + `codex-config` repos: **archived read-only** post-migration + before deploy-key retirement. Old repos remain fetchable for provenance/audit; nothing writes to them.

### 2. Mirror as generated artifact

The public self-hoster mirror (`example-org/claude-code-config`, [[T-1475]]) becomes a **generated artifact** derived from `pantheon-config/harness/claude/` (allowlisted subtree + explicitly-shared generic assets), not a source-of-truth repo. A reproducible pipeline in `pantheon-config/tools/` generates the mirror payload from a pinned `pantheon-config` sha; the mirror repo receives the generated payload as an atomic ordinary non-force push. Same shape available for a future `codex-code-config` mirror off `pantheon-config/harness/codex/`.

### 3. Distribution: every seat's pantheon-config clone already carries it

Under this topology, every seat that clones `pantheon-config` (all clone-bearing personas do) automatically has access to both harness FRAMEWORK slices. `persona_installer` reads from `pantheon-config/harness/<name>/` at the seat's own clone. No cross-account read is needed; the 87 CI-red baseline retires structurally.

### 4. INSTANCE content stays in `instance.yaml` + `pinned-rules/`

Everything operator-specific (chat_id, absolute paths, persona name bindings, secrets by reference) stays in `instance.yaml`. Everything persona-authored (per-persona operating rules) stays in `pantheon-config/pinned-rules/<persona>.md`. FRAMEWORK templates reference INSTANCE values through `instance.yaml` — the ORG-039 §2 seam is preserved and now uniform across both harnesses.

## Supersession — precise clauses

### From ORG-028

- **§1 "one config repo per harness runtime"** — REVISED. The harness-keying principle (config schema boundary = repo boundary) is preserved as (subdir boundary within pantheon-config). Adding a new harness = adding a new `pantheon-config/harness/<name>/` subdir. What retires is the standing "one TOP-LEVEL repo per harness" claim.
- **§4 "composition happens at agent-HOME level, not at repo level"** — PRESERVED. Every seat still has all harness content in its home; the composition just consolidates the SOURCE layer. Each seat gets the harness content by cloning `pantheon-config`, which drops the subdirs into the seat home naturally.
- **§4 "if org tooling ever needs a unified view, use git subtree"** — INVOKED, not superseded. B is the sanctioned §4 escape hatch. The subtree import gives the `pantheon-config/harness/*` subdirs their history.

### From ORG-039

- **§1 (FRAMEWORK/INSTANCE per-harness partition)** — PRESERVED entirely. The partition principle stands; only the STORAGE of the FRAMEWORK slice moves from a separate repo to a pantheon-config subdirectory.
- **§2 (`instance.yaml` as single binding seam)** — PRESERVED. INSTANCE content stays out of FRAMEWORK; codex-config's hardcoded `/home/codex` + chat_id parameterize via instance.yaml as part of the phase-2 partition.
- **§3 (Host/runtime provisioning contract)** — PRESERVED. The mirror pipeline emits the generic template payload; the provisioning contract still ships with it.
- **§4 (Decoupling contract — fork without the harness repo must not half-break)** — PRESERVED. The `directive_skill_drift_check.py` still skips cleanly when a harness subdir is absent/configured-off; a fork that pulls `pantheon-config` without configuring a harness gets a clean skip.
- **§D10 (references-not-transclusion, no vendored frozen copies)** — PRESERVED, directionally. The concern was fork-side vendoring (drift from upstream). Under this ADR, `pantheon-config/harness/*` IS upstream; mirrors are generated artifacts (references) pinned to a pantheon-config sha. No frozen-copy drift is possible because there is no upstream to drift from.

### From T-0493

- **"canonical pinned-rules home is `claude-config/pinned-rules/`"** — RETIRED. The canonical home is `pantheon-config/pinned-rules/` for every persona, uniform. Orchestrator's `orchestrator.md` migrates as part of the phase-2 partition.
- **Edit protocol ("no PRs needed, self-commit, git history is audit trail")** — PRESERVED. Applies uniformly to `pantheon-config/pinned-rules/*.md` post-migration.

## Migration plan (Cairn's 7-phase path, adopted)

Each phase is reversible per phase and standalone-landable.

1. **Ratify this ADR.** The operator ratifies against this body. T-3104 resolves as executed-via-ORG-091. This phase is the ratification event, not a code landing.
2. **Genericize + partition both live repos in place.** In claude-config: strip persona-brief content from `pinned-rules/orchestrator.md` (INSTANCE) into a target that will move to `pantheon-config/pinned-rules/orchestrator.md`; keep the role-keyed skeleton. In codex-config: parameterize hardcoded `/home/codex` paths and the allowlist template's hardcoded chat_id (`456474197`) via `instance.yaml`-style references. Add leakage-scan tests + clean-home smoke. Reversible (revert commit). Both repos remain their own source of truth this phase.
3. **Import FRAMEWORK history into `pantheon-config/harness/{claude,codex}/`** via `git subtree add` (preserves the imported repo's commit history under the subdirectory prefix). At this phase both source-of-truth locations exist simultaneously — old repos still writable, new subdirs additive. Reversible by removing the subdirs.
4. **Path-scoped owners + CI.** `pantheon-config/harness/claude/` changes trigger the claude harness's unit / clean-home / leakage / packaging / installer tests; `harness/codex/` similarly. Unrelated ticket / ledger changes to pantheon-config don't publish a harness release. Reversible by removing the CI gates.
5. **Reproducible versioned bundles / release mirrors.** Land the mirror-generation pipeline that produces `claude-code-config` releases from `pantheon-config/harness/claude/` + allowlisted shared assets. A deployed seat binds to a pantheon-config commit + subtree/bundle digest. Reversible by unpublishing the release; the source stays.
6. **Update `persona_installer` / bridge / all executable old-path consumers.** Every code path that reads `claude-config` or `codex-config` retargets to `pantheon-config/harness/{claude,codex}/`. Canary on one seat + prove rollback before fleet rollout. Reversible by reverting the persona_installer changes; old repos still exist as fallback until phase 7.
7. **Archive old repos read-only + retire deploy keys.** Only after all seats + the self-hoster release consume the new source. This phase is the point-of-no-return; before this phase, everything is reversible on the same-day.

Between phase 5 and phase 7, both source locations exist and are consumed. This is the transient overlap Cairn's audit called out — needs ordering discipline so nothing reads from both simultaneously (a per-executable one-of-two-locations config binding, not both).

## Consequences

### Preserved from prior topology

- FRAMEWORK/INSTANCE partition (ORG-039 §1).
- `instance.yaml` as single binding seam (ORG-039 §2).
- Harness schema boundary (ORG-028 §1 principle).
- Composition-at-agent-HOME (ORG-028 §4).
- Reference-not-transclusion for downstream forks (ORG-039 §D10).
- Edit protocol for pinned-rules (T-0493 §Edit protocol).
- Decoupling contract for forks without a configured harness (ORG-039 §4).

### Changed

- Source of truth for FRAMEWORK slices: pantheon-config subdirs, not separate top-level repos.
- Distribution mechanism: one pantheon-config clone per seat carries all FRAMEWORK content; retires two separate deploy keys + cross-account read requirements.
- Public mirror: generated artifact from pantheon-config, not source-of-truth repo.
- Pinned-rules canonical home: pantheon-config/pinned-rules/, uniform for every persona.
- Number of live source-of-truth repos on the VPS: 3 → 1 (pantheon-config); 2 old repos archive read-only.

### Downstream unblocks

- **T-3096 (Cat C CI-red baseline).** Cross-account codex-config read requirement retires structurally; the 87 CI errors clear.
- **T-3109 (fresh-install E2E on CI seat).** No longer gated on cross-account codex-config access; the seat's own pantheon-config clone carries what the test needs.
- **T-3108 (HC update propagation).** Simpler substrate to propagate updates to (one repo per seat instead of three).
- **New-seat provisioning under SSEED / germination engine** ([[ORG-080]]). New seats get all harness FRAMEWORK content with one clone; no deploy-key provisioning per harness.

### Risks (per riskiest assumption above)

- Coupling collapse — if mirror release cadence effectively locks to pantheon-config commit cadence, the independent-clocks benefit is lost. Reversible via reverse-subtree; falsifier at three months.
- Ordering fragility during phases 5-7 — a per-executable "read from harness/ subdir OR from claude-config" config binding is required; getting this wrong risks half-migrated seats reading from both. Mitigated by phase-6's canary + prove-rollback discipline.
- Deploy-key retirement is irreversible in effect (revoking a deploy key can be redone but the key material is regenerated); order it last (phase 7) and only after every consumer is verified on the new source.

### Cost break-even

Cairn's audit put A's ongoing cost as "6+ clones added to seats + T-1475 pipeline update + ongoing per-repo maintenance." B has a one-time migration cost + retires ongoing cost. Break-even is fast as the fleet grows — SSEED will spawn seats programmatically, and each new seat under A adds two clones + two deploy-key provisioning steps that B eliminates.

## Boundary — what this ADR does NOT decide

- **Renaming.** No repo is renamed. The mirror stays `claude-code-config` for continuity with existing self-hosters; the subdir is just called `pantheon-config/harness/claude/`. A future rename can be a separate arc.
- **Codex-config-mirror publication.** Whether a public `codex-code-config` mirror ships (parallel to `claude-code-config`) is a separate decision. This ADR names the shape (generated artifact from `pantheon-config/harness/codex/`), not the publication policy.
- **The T-2953 HC cutover arc as a whole.** This ADR resolves one substrate-shape concern within the HC cutover; other T-2953 arc items (T-3096/T-3108/T-3109 unblock as consequences, but T-2953's own scope remains).
- **Cross-persona pinned-rules edit-protocol.** The T-0493 self-commit protocol carries over to `pantheon-config/pinned-rules/` unchanged for every persona; nothing new decided here.
- **The germination engine architecture.** ORG-080 owns SSEED's own shape; this ADR just observes that new-seat provisioning simplifies under B.

## Related work

- [[ORG-028]] — the original harness-repo topology this ADR extends.
- [[ORG-039]] — the FRAMEWORK/INSTANCE partition this ADR consolidates the storage of.
- [[ORG-049]] — manifest-driven installation; `persona_installer`'s apply mode reads from `pantheon-config/harness/<name>/` post-migration.
- [[ORG-063]] — harness-manager / action-broker; simpler substrate to expose broker over.
- [[ORG-068]] — universal system-lifecycle contract; harness-config-repo lifecycle (was: two extra repos to manage) simplifies to one.
- [[ORG-079]] — L1 install-completeness contract; V<n> invariants continue to check the same content, just resolved through pantheon-config/harness/*/ path.
- [[ORG-080]] — germination engine (SSEED); new-seat provisioning under B simplifies from 3-clone to 1-clone shape.
- [[T-3104]] — the scoping ticket + the operator's B ratify direction (TG 9105).
- [[T-3102]] — the audit that identified the topology drift + Cairn's cost-compared A/B/C.
- [[T-1475]] — the public mirror history that becomes a generated-artifact pipeline under phase 5.
- [[T-0493]] — pinned-rules edit-protocol + canonical-home claim being retired.
- [[T-0495]] / [[T-0496]] — Recovery + Specialist pinned-rules-in-pantheon-config accommodation; retires as "the accommodation IS the canonical home now."

## Authoring discipline

Hand-curated per D2 (HO authors). Draft-only until the operator ratifies against this body per the two-touch flow named on the T-3104 talk-out. REBOUND rounds and review clutter belong in `notes/2026-09-XX-org091-review-history.md` when they occur, not in the ratifiable body. Amendments post-ratify follow the standard ratify surface flow (operator-ratified via a ratify ticket against this file).
