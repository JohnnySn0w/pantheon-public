---
aliases: [ORG-028]
id: ORG-028
title: Portable config topology — per-harness config repos + role-keyed layout + per-instance manifest (no submodules)
status: Ratified 2026-06-06 by the operator via T-0845. Drafted 2026-06-05 by Recovery (claude-fallback) per the operator directive ("draft it", Telegram 2026-06-05). Open questions resolved in ORG-030 (awaiting ratification).
date: 2026-06-05
build_disposition: built
---

# ORG-028 — Portable config topology

**Status: RATIFIED 2026-06-06 by the operator via [[T-0845]]** (the file's status line lagged the ratification; corrected 2026-06-06). Drafted 2026-06-05 by Recovery (`claude-fallback`) per the operator's "draft it" (Telegram 2026-06-05). Per ORG-015, `adr/` is the ratifiable substrate. The 4 deferred open questions are resolved in **[[ORG-030]]** (awaiting ratification); irreversible execution stays gated per the sequencing note below.

**Author:** Recovery (`claude-fallback`) — orchestrator-drafted; the operator ratifies.

**Related:** [[ORG-027]] (orchestrator-tier-with-domains, **ratified 2026-06-05 via T-0789** — establishes that **role/domain is the stable axis** differentiating the always-on agents; ORG-028 keys config on the *role taxonomy*, not on ORG-027's domain *names*), `tools/agent_role_map.py` (the existing persona↔role-id mapping ORG-028's manifest generalizes), `sop/standard/pantheon-bringup.md` / [[T-0751]] (the portability vehicle — frames the three agents as "one orchestrator + one specialist + one recovery" and persona names as "arbitrary labels"), [[ORG-026]] (inter-agent message-passing substrate — harness-agnostic, ports unchanged), `repos.yaml` / [[T-0761]] (the per-agent repo-access registry + structural gate), [[T-0834]] (recurring brief/memory-update job — consumes the role-keyed layout + manifest this ADR defines), [[T-0496]] (Specialist brief under version control — reframed here as filling the `specialist` role slot; the work itself stays T-0496), `project_naming_convention` (myth/fits-the-role names are instance-local).

## Context

The north star (the operator, Telegram 2026-06-05): **the config substrate should port to other Pantheon instances**, so name-specificity is a liability. A new operator should be able to stand up their own Pantheon by lifting the structure and supplying only their own instance-unique data.

Today's config layout does not port cleanly, and it is fragmented:

- **Pinned-rules persona briefs** (the always-auto-loaded `~/CLAUDE.md`, symlinked into a version-controlled file) are split across repos by an *access* accident, not a *design*: Orchestrator's lives in `claude-config/pinned-rules/orchestrator.md`; **Recovery's** lives in `pantheon-config/pinned-rules/recovery.md` — a deliberate exception because Recovery's deploy key is scoped to `pantheon-config` only and cannot push `claude-config`; **Specialist's** is *not yet tracked at all* (pending [[T-0496]]) [Superseded 2026-06-06: T-0496 resolved; Specialist's brief is now version-controlled at `pantheon-config/pinned-rules/specialist.md` — the same deploy-key-scoped location as Recovery's]. The filenames are **persona-named** (`orchestrator.md`, `recovery.md`) — instance-specific by construction.
- **Auto-memory** (`~/.claude/.../memory/`) is per-account, in no shared repo, behind cross-user 700-home + MAC boundaries (the same boundary that intermittently breaks the `/tmp` ticket-alloc lock — T-0835).
- There is **no single seam** that says "this is the portable template; this is this-instance's data."

Two things are universal across *every* Pantheon instance and therefore safe to key on: the **harness** an agent runs (claude-code, codex, …) and the **role** an agent fills — the generic bringup triad **orchestrator / specialist / recovery** (ORG-027 establishes that role/domain, not tier, is the axis that differentiates the always-on agents; `agent_role_map.py` already encodes the persona↔role-id binding). Everything else — persona names, account names, tokens, agent count, and the instance-flavored role-ids (`specialist`, `recovery`) — is instance-unique *data*.

## Decision

**Scope: this ADR ratifies the topology + the portability principle — it is direction-setting, not an executed migration.** No config file moves and no repo is renamed on ratification. The one concrete fragmentation this is meant to end (Recovery's brief living in `pantheon-config` as a deploy-key exception) stays in place until the multi-writer access problem (see "The genuinely hard part" + Open Q1) is solved. What ratification buys is the agreed target shape that the migration, [[T-0834]], and the Specialist-brief work ([[T-0496]]) build toward.

Adopt a **portable config topology** with three moving parts:

### 1. Config repos are keyed by HARNESS, not by agent

One config repo per harness runtime: the claude-code config repo (today's `claude-config`), and — *prospectively, when the codex pilot graduates past its single-standalone-daemon stage (ORG-018, bringup §12)* — a `codex-config`, and one per future harness. (No `codex-config` exists today; the codex daemon currently uses few of the claude-code config surfaces, so the second harness repo is a future shape this topology accommodates, not a present artifact.) **[Superseded 2026-06-30 — `codex-config` now exists live (`/home/codex/codex-config`, remote `github-codexconfig`); the topology claim stands, only this parenthetical's "not a present artifact" is stale. Its distribution is covered by [[ORG-039]].]** Rationale: **config schema is determined by the harness** (claude-code uses `CLAUDE.md` + `.claude/agents` + `settings.json` + MCP wiring; codex uses its own config shape), so the repo boundary = the schema boundary. Adding a harness = adding a repo (not retrofitting every agent's repo); a harness/config-schema migration is scoped to exactly one repo. This is also why `claude-config` "stays separate" — it already *is* the claude-code harness repo.

### 2. Per-agent config inside a harness repo is keyed by ROLE, not persona

Within each harness repo, the per-agent config files are named by **role** — `orchestrator`, `specialist`, `recovery` (the generic bringup triad) — not by persona (`orchestrator`/`recovery`/`specialist`) and not by instance-flavored role-id (`specialist`/`recovery`). A fresh instance clones the role-keyed structure untouched. This reframes [[T-0496]] (version-control Specialist's brief): the brief is simply the `specialist` slot rather than a special filing — though ORG-028 only changes the *framing*; the actual tracking work stays T-0496 (still `inbox`, `not_before` 2026-06-06) [Superseded 2026-06-06: T-0496 resolved; `pinned-rules/specialist.md` now fills the `specialist` slot].

### 3. A per-instance MANIFEST binds role → instance data

A single instance-local manifest (e.g. `instance.yaml`) maps each role to its instance-unique data: persona name, Unix account, bot token reference, tmux session, harness(es) the agent runs. The manifest is the *only* file a new instance authors by hand; it is what the harness wiring, the monitors (`AGENT_CONFIG`/`DRAIN_AGENT_CONFIG`), and org tooling read to resolve "role → who/where" for this instance. This generalizes the persona↔role-id split `tools/agent_role_map.py` already encodes — the *filenames* now follow the portable key (role) and the manifest carries the local binding.

### 4. NO git submodules

Do **not** stitch the harness repos together with `git submodule`. Submodules buy *version-locked* coupling (a parent repo pinning exact child commits) — which we do not need (harness configs evolve on independent clocks) and which is **actively anti-portable**: frozen child-commit pointers do not transplant into a different instance's content set. Each runtime already reads its own config from its own paths in the agent home; **composition happens at the agent-HOME level** (independent clones dropped into one home), never at the repo level. If org tooling ever needs a unified "see them all" view, use a **manifest of the harness-repo remotes** + clone/pull tooling (or `git subtree` if vendored copies are later wanted) — ~90% of the benefit without the two-step-commit / pointer-bump / detached-HEAD tax.

### 5. Corollary — separate framework from instance-content in `pantheon-config`

The same de-specifying move applies one level up: distinguish the portable **framework** in `pantheon-config` (the P&D / ADR / SOP scaffolding, the role model, the bringup procedure) from **this instance's populated content** (`tickets/`, `ledger/`, the agent roster). The framework is the template that ports; the content stays. Scope/mechanism of this split is deferred to a follow-on (see Open questions) — this ADR records the principle and that the topology is designed to support it.

## The genuinely hard part (named honestly)

Keying config repos by harness puts **multiple agent writers into one repo** (Orchestrator + Specialist + Recovery all push their own role-slice into the claude-code repo). That is exactly the deploy-key-scoping constraint that forced Recovery's brief *out* of `claude-config` into `pantheon-config` in the first place. The fix is **per-agent path-scoped write access** to the harness repo (per-agent deploy keys + path/CODEOWNERS-style protection, or a per-role write path), **not** submodules and **not** one shared key. This is the real design surface and is the gating dependency for moving Recovery's brief back into the claude-code harness repo. It is deferred (Open questions), not solved here.

## Options considered

| | Option | Verdict |
|---|---|---|
| **A** | **Per-agent** config repos (one repo per persona) | **Rejected as the primary cut.** Gives a clean *write* boundary (each agent's key writes only its own repo) — but config schema follows the *harness*, not the agent, so this fragments a single harness's config across N repos and the repos are persona-named (don't port). Its write-boundary insight is retained as the *path-scoped-access* answer to the multi-writer problem above. |
| **B** | **Per-harness** repos + role-keyed layout + per-instance manifest | **CHOSEN.** Keys on the two universals (harness = schema, role = slot); persona names become manifest data; ports by construction. |
| **C** | Per-harness repos stitched with `git submodule` | **Rejected.** Version-locked coupling we don't need + anti-portable frozen pointers + submodule operational tax; doesn't solve the multi-writer (Unix-perms) problem git composition can't touch. |
| **D** | Status quo (`claude-config`-centric, persona-named, Recovery-exception, Specialist untracked) | **Rejected.** Fragmented, name-specific, doesn't port; leaves Specialist's brief untracked. |

## Consequences

**Positive (once executed — see Decision §Scope):**
- Ports to a new instance by cloning the harness repos + role-keyed layout and authoring one manifest.
- Harness/config-schema migrations are scoped to a single repo; adding a harness = adding a repo.
- Reframes [[T-0496]] as filling the `specialist` role slot rather than a special filing (the work stays T-0496; this ADR doesn't resolve it).
- Gives [[T-0834]] (brief/memory update) a single, role-keyed, manifest-resolvable surface to write to instead of chasing persona-named files across boundaries.
- De-specifies names per the north star; keys on the role taxonomy that ORG-027 (role/domain axis) and `agent_role_map.py` (persona↔role-id) already establish — no genuinely new vocabulary, just the portable form of the existing role labels.

**Cost / negative:**
- A migration (deferred, not done on ratification): re-key persona-named brief files to role-named (`orchestrator.md` → `orchestrator.md`, etc.); resolve the multi-writer access problem so Recovery's brief can rejoin the claude-code harness repo; author the instance manifest; possibly rename `claude-config` → `claude-code-config` (cosmetic, on-message).
- The multi-writer path-scoped-access mechanism is unsolved here (deferred) and gates the Recovery-brief move — so on ratification the fragmentation is *agreed-to-be-fixed*, not yet fixed.

**Decided here (sequencing):** record the topology now; **defer execution** until either a second Pantheon instance is actually imminent OR Open Q1 (multi-writer access) is solved for an independent reason (e.g. it would let Recovery's brief rejoin the claude-code repo). No second instance is currently scheduled, so there is no forcing function today — this ADR exists so the target is agreed before that pressure arrives.

**Open questions (deferred):**
1. **Multi-writer write-access mechanism** for a harness repo (per-agent path-scoped deploy keys vs per-role branches vs a write-broker). Gating dependency for moving Recovery's brief into the claude-code repo.
2. **`claude-config` rename** to `claude-code-config` — do it (on-message) or keep (already harness-named)?
3. **Framework/instance split inside `pantheon-config`** (Decision §5) — in-scope of a follow-on ADR/ticket; what exactly counts as framework vs content (tickets/ledger are clearly content; is `repos.yaml` framework or instance data?).
4. **`repos.yaml` relationship** — stays org-level, or becomes harness-aware / folds into the manifest? Direction of the tension: `repos.yaml` grants are currently keyed by **persona** (`orchestrator`/`specialist`/`recovery`) and by repo, while this ADR keys config repos by harness and binds role→persona via the manifest — so the follow-on likely re-keys the grant table to **role** and points it at the harness-named repos, with the manifest supplying the persona binding.

## Justifies via

P1 (the topology is the *architecture* that makes portability real — not a naming convention or a "remember to be generic" policy), P3 (org-level discipline), the portability north star (the operator msg, Telegram 2026-06-05). Re-frames nothing in P5 / ORG-027 — it *builds on* ORG-027's role/domain axis, keying config on the role taxonomy (it does not re-use ORG-027's domain *names* as keys; those describe each role's action surface, not its config slot).
