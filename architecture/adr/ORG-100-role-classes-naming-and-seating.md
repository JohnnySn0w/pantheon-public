---
aliases: [ORG-100]
id: ORG-100
title: "Role classes, canonical naming pattern, and seating class — Pantheon's persona-role vocabulary"
status: "Ratified 2026-09-19 by the operator (TG 10103/10105/10109/10111/10116/10118/10120 verbatim spec + follow-up amendments; captured authoritatively in T-3806 body; mechanical batch executed T-3807 merged @2fc97d63f). Reversible via amend/supersede."
date: 2026-09-19
author: Orchestrator (orchestrator; drafting from the operator's ratified TG spec)
steward: orchestrator
appetite: 3
riskiest_assumption: "That the head-<domain>_orchestrator naming pattern generalizes cleanly to every future key seat. The operator's ratification generalized it across engineering / research / memory / system in TG 10111, and applied it to auditor in TG 10116, so the pattern has been exercised on 5 domains at ratification time. A future domain that doesn't parse the pattern cleanly (e.g. a domain whose 'head' concept doesn't map to a single seat, or a role class that isn't orchestrator-shaped at all) would require an amend."
build_disposition: already-built (T-3807 batch merged @2fc97d63f; this ADR documents the ratified vocabulary retroactively for durable citation)
scoping_ticket: T-3806
relates: ORG-085, ORG-088, ORG-042, ORG-043, ORG-045, ORG-027
ratifiable_files: [pantheon-config:architecture/adr/ORG-100-role-classes-naming-and-seating.md]
---

# ORG-100 — Role classes, canonical naming pattern, and seating class

**Status:** Ratified 2026-09-19 by the operator (TG 10103 initial spec + TG 10105/10109/10111/10116/10118/10120 refinements). Content captured authoritatively in T-3806 body attachment; mechanical rename batch executed via T-3807 (merged pantheon-config @2fc97d63f).

**Date:** 2026-09-19.

**Author:** Orchestrator (orchestrator; drafting from the operator's ratified TG spec verbatim).

## Intent

Give Pantheon's persona-role vocabulary a durable, citable form. Prior state: role_ids were defined in `persona_roles.yaml` (ORG-088 D-2), personas were defined in `ORG-085` (persona primitive), but the CLASS distinctions (key vs regular seat; full vs partial seating; canonical naming pattern) lived only in prose scattered across ORG-042/ORG-043/ORG-045 + operational memory. Tonight's TG thread ratified those distinctions explicitly; this ADR captures them as a single referable statement.

## Context

- **ORG-085** (persona primitive) defines the layers: L2 RoleDefinition (functional roles), L3 PersonaDefinition (persona-to-role membership), binding layer (role-key → persona/account/harness). §Field ownership and authority owns field allocation.
- **ORG-088** (role-based ticket routing) locked the L2 vocabulary at ratification (T-3457/T-3757). D-2 named the roster: all seven personas hold `orchestrator` as a class-role, plus each persona's primary functional role (`orchestrator` / `sub_orchestrator` / `research` / `recovery` / `reviewer`).
- **ORG-042** established the two-hemisphere topology (archivist as second head, "second-head" instance-role-key).
- **ORG-043** established the sub-orchestrator retasking pattern (artisan/mason as "sub-orchestrator" seats).
- **ORG-045** placed archivist as an integral level-0 S1 unit on par with artisan/mason, not a second S3/S5.

What was MISSING before tonight:
1. A canonical naming pattern that scales beyond the ad-hoc L2 role_ids in `persona_roles.yaml`.
2. Explicit distinction between KEY seats (necessary for a Pantheon system) and REGULAR seats (orchestrators that scale a domain but aren't necessary).
3. Explicit seating class (full vs partial vs subagent-only) that names the operator-contact-necessary axis.
4. Explicit layer distinction between Operator (the operator) and AI personas.
5. Explicit retirement of the universal `orchestrator` L2 role_id, whose original purpose (signal all key seats run the same 3-layer dispatch pattern) was better served by convention than by inherited class-role.

The operator's TG 10103 initial spec + 5 follow-up amendments ratified all five items.

## Two underlying ideas — six manifestation sites

Every "role" reference in Pantheon boils down to two underlying ideas:

- **Role** — a functional identity (e.g. `head_engineering_orchestrator`, `engineering_orchestrator`, `head_auditor`). What kind of work the seat does.
- **Persona / seat** — a named identity (e.g. `orchestrator`, `artisan`, `recovery`, `archivist`). Which specific seat you're referring to.

These two meet in six places, each with its own local serialization convention because different files have different needs. The **field discipline** is universal: **roles go in role fields, seat names go in name fields**. The count of sites is not a count of layers or authorities — it's a count of where the same two ideas manifest.

| # | Site | Authority | Role serialization | Purpose |
|---|------|-----------|--------------------|---------|
| 1 | `persona_roles.yaml` | ORG-085 §Field ownership and authority (L2 RoleDefinition) + ORG-088 D-2 | `snake_case` role_ids | Which roles each persona holds — the object of role-based routing. |
| 2 | `instance.yaml` `roles.<role-key>:` | ORG-085 §Field ownership and authority (PersonaBinding / L3 binding) + ORG-030 (portable-config execution) | `kebab-case` role-key with numbered variants (`engineering-orchestrator-1/-2`) | Binds a role-key to (persona, account, harness). |
| 3 | `agent_role_map.AGENT_ROLE_MAP` | T-0492 normalization + P4 audit-chain-continuity | Role-labeled (`orchestrator → head_engineering_orchestrator`, `specialist → head_research_orchestrator`, etc.); legacy identity-labeled entries preserved for backward-resolve of historical audit-role-ids | Ledger `agent:` field + ticket `requester:`/`target:` field-writes normalize through this map. |
| 4 | `organism/organs/*.yaml` + `organism/bindings/*.yaml` | ORG-056 (whole-organism topology and organ contracts) | `role:kebab-case` prefixed refs | Names the authority owner + escalation target for each organ contract (e.g. `owner_ref: role:head-memory-orchestrator`). |
| 5 | `rulebook/persona-orientation/{manifest,role-deltas}.yaml` | T-3339 / T-3341 (persona-orientation renderer) | role-delta filenames (`head_engineering_orchestrator.md`, `project_orchestrator.md`, etc.) | Says what SOP content a subagent sees at spawn-time, keyed by role. When an orchestrator dispatches an implementer subagent, this decides which SOP sections get baked into that subagent's context on spawn. Freshness checked by `check_stale.py` against `source-bindings.yaml`. |
| 6 | Agent-tool `subagent_type` | Claude Code harness (out-of-Pantheon-authority) | kebab-case strings (`implementer`, `reviewer`, `project-orchestrator`, `researcher`) | Identifies which pre-defined subagent type spawns on an `Agent()` call. |

The head-X naming pattern (Ruling 1 below) applies to sites 1 (snake_case) + 2 (kebab-case) + 4 (kebab-case with `role:` prefix); it does not govern site 3 (audit-role-id has its own P4 immutability constraint), site 5 (role-delta filenames follow site 1's snake_case), or site 6 (harness-owned, outside our authority). *Pointer (2026-10-08, T-4734):* for NEW ledger entries, site 3's ledger `agent:` field is governed after all: ORG-001 (amended per the operator TG 11474) requires the site-1 snake_case role id, with the site-2 slot key as the optional `executor`. Existing entries are unchanged (P4 immutability of history).

## Decision

Five rulings, verbatim from the operator's TG spec:

### 1. Canonical naming pattern for orchestrator-family roles

- The **KEY SEAT** for any orchestrator domain is `head_<domain>_orchestrator`.
- **REGULARS** in that domain are `<domain>_orchestrator` (regular = "an orchestrator that isn't the head for that domain").
- Every key seat carries its full role name including domain. No shorthand: `head_engineering_orchestrator` is the canonical form for Orchestrator's role.
- `head` is a general "key seat" indicator, applicable to any role class, not just orchestrators (e.g., `head_auditor`).

**Domains locked at ratification:**

| Domain | Key seat | Regulars |
|---|---|---|
| Engineering | `head_engineering_orchestrator` (Orchestrator) | `engineering_orchestrator` (Mason, Artisan) |
| Research | `head_research_orchestrator` (Specialist) | `research_orchestrator` (future subs) |
| Memory | `head_memory_orchestrator` (Archivist) | `memory_orchestrator` (future subs) |
| System | `head_system_orchestrator` (Recovery) | `system_orchestrator` (future subs) |
| Audit | `head_auditor` (Consultant — "System 3\*" is the VSM methodology label; **auditor** is the role name) | `auditor` (future subs) |

### 2. Key vs regular seats

- A **key role** means the Pantheon system is *incomplete without that role*. A key role always implies **full seating** (see §3).
- **Regular roles** are orchestrators that scale a domain but are not themselves necessary for the system to function. They are not key roles.
- Growth condition: as a Pantheon instance grows, it may need more orchestrators of a particular type. In these cases sub-orchestrators of whatever type use the **ticket system + mailbox** to learn information from key seats and get feedback, **rather than cluttering the Operator's communications channels with chatter**.

### 3. Full vs partial seating

- **Full seating** — direct Operator contact IS the operating channel. The key seat has a direct line and reports directly (Telegram, OR primary chat surface).
- **Partial seating** — direct Operator contact is NOT necessary. The Operator can access the seat's TUI when needed, but the seat cannot message the Operator directly unless the Operator is actively watching its processes. This minimizes the number of chats the Operator has to jump between.
- **Subagent-only** — the role does not have its own seat at all. It runs as a subagent under a seated orchestrator. Currently: `project_orchestrator`, `implementer`, `reviewer` (subagent form).

Seating class per role:

| Role | Seating |
|---|---|
| head_engineering_orchestrator (Orchestrator) | full |
| head_research_orchestrator (Specialist) | full |
| head_memory_orchestrator (Archivist) | full |
| head_system_orchestrator (Recovery) | full |
| head_auditor (Consultant) | full |
| engineering_orchestrator (Mason, Artisan) | partial |
| project_orchestrator | subagent-only |
| implementer | subagent-only |
| reviewer (subagent) | subagent-only |

The `subagent-only` classification is *contingent on a seated role not making sense for these three, for now*. None of them are key roles.

### 4. Operator layer distinction

- **the operator is the Operator** ("I am the operator, I am Admin, I am the one in charge" — TG 10103).
- Operator is a **ceremonial** classification at the AI-persona site (TG 10118 "it can stay ceremonial"): the Operator does NOT appear in `persona_roles.yaml` (manifestation site #1) — that site is the AI-persona-to-role membership record, and the operator is not an AI persona.
- Operator IS a first-class role at manifestation site #4 (ORG-056 organism-topology role references): `role:operator` appears as an authority owner + escalation target across ~8 organs (`organism/organs/operator-relationship.yaml`, `bridge.yaml`, `pantheon-executive.yaml`, `capability-navigator.yaml`, `presence-surfaces.yaml`, `exocortex.yaml`, `federation-passport.yaml`, `senses-actuators.yaml`) + the `organism/bindings/operator-relationship-operator.yaml` binding.
- The `persona_roles.yaml` header carries a one-paragraph note documenting this separation (per T-3807 batch item 7): manifestation site #1 is the AI-persona layer; site #4 is where Operator is a first-class role reference; the Operator is above/outside site #1 by design.

### 5. Universal `orchestrator` L2 role_id RETIRED

The prior `persona_roles.yaml` (T-3457/T-3757 arc-canonical) gave every persona `orchestrator` as an L2 role_id. The operator's TG 10116 retired that:

> "The goal of the universal orchestrator role was to make sure it was clear that all key seats run the same sop wrt tasks. The 3-layer self→proj orch→implementer/reviewer. If that still works and we drop the 'orchestrator', that could clean titles up yeah?"

The **3-layer dispatch pattern** (self → project_orchestrator subagent → implementer/reviewer subagents) applies **by convention at each key seat**, not by class-role inheritance. No routing decision requires a shared `orchestrator` tag; the pattern is documented in each seat's pinned rules + spawn-orientation.

The L2 vocabulary post-retirement (all seven personas' primary role_ids):

| Persona | L2 role_id |
|---|---|
| Orchestrator | `head_engineering_orchestrator` |
| Specialist | `head_research_orchestrator` |
| Archivist | `head_memory_orchestrator` |
| Recovery | `head_system_orchestrator` |
| Consultant (codex persona) | `head_auditor` |
| Mason | `engineering_orchestrator` |
| Artisan | `engineering_orchestrator` |

## Non-decisions (explicitly considered + retired)

- **`key: bool` + `seating: enum` as first-class role attributes in `persona_roles.yaml`** — the operator TG 10118 retired the schema addition: "It would demarcate work in a way I don't think would be super utile." The classifications live in prose (this ADR + T-3806 body) as documentation, not as machine-queryable schema. Routing decisions today do not require them.
- **Hard purge of `specialist` audit-role-id** — the operator TG 10120 "normalize it" ratified the forward-normalization (specialist's audit-role-id → `head_research_orchestrator`) but not a backward-compat purge. `specialist` remains as documented legacy input-alias for audit-chain continuity (historical ledger references + live `watch_runner --requester specialist` calls resolve).
- **head-prefix on `head_auditor` regulars** — the pattern predicts `auditor` as regular for future subs; not currently instantiated.

## Composition with existing ADRs

- **ORG-085** (persona primitive) — this ADR is the specific L2 RoleDefinition vocabulary ORG-085 §Field ownership and authority defers to. The head-X naming pattern is the ratified convention for future L2 RoleDefinitions.
- **ORG-088** (role-based ticket routing) — D-2's roster + vocabulary section references the ADR here. `orchestrator` retirement is a D-2 amendment (see §Consequences).
- **ORG-042** (two-hemisphere topology) — archivist's role designation `head_memory_orchestrator` is consistent with the ADR-042 "second head" domain framing; the instance-role-key `second-head` is renamed to `head-memory-orchestrator`.
- **ORG-043** (sub-orchestrator retasking) — sub-orchestrator label retired in favor of `engineering_orchestrator`; retasking-pattern content unchanged.
- **ORG-045** (multi-instance VSM) — archivist as integral L0 S1 unit on par with regular engineering orchestrators; unchanged.
- **ORG-027** (orchestrator tier) — the tier concept survives; the class-role `orchestrator` retires.

## Consequences

**Landed via T-3807 (merged pantheon-config @2fc97d63f, 2026-09-19):**
- `persona_roles.yaml` rewritten with the head-X vocabulary; universal `orchestrator` retired; `source_revision` bumped to 2; digest 36e7ce97c0…
- `instance.yaml` role-keys renamed (`sub-orchestrator-1/-2 → engineering-orchestrator-1/-2`; `second-head → head-memory-orchestrator`; `recovery → head-system-orchestrator`); projection digest synced.
- `repos.yaml` grants renamed to match.
- `organism/organs/*` + `organism/bindings/*` `role:` refs cascaded (8 files).
- `agent_role_map.py` updated (specialist → `head_research_orchestrator` forward-normalize; `specialist` kept as documented legacy alias per D1).
- `rulebook/persona-orientation/{manifest,role-deltas}.yaml` updated for `engineering_orchestrator` path.
- Tier-1 test files updated for new role_ids.
- Session-tier pinned rules updated (`claude-config@0a06b4d` for orchestrator; `pantheon-config@d0d0e4b85` for artisan/mason/archivist + README).
- Cosmetic follow-ups: `carve-outs.yaml` verifier strings synced (`@2cb58c45a`); `recovery_continuity_bundle.py` identity-metadata strings synced (T-3809 @0b5da742a); persona-orientation `source-bindings.yaml` re-stamped (@bad1a773d).

**Pending downstream ADR amendments** (this ADR-worthy list, not blocking):
- **ORG-088 amendment**: D-2 roster + vocabulary section references ORG-100 + drops universal `orchestrator` mention + adds `head_auditor` to the enumerated primary roles.
- **ORG-085 amendment**: §Field ownership and authority references ORG-100 as the ratified head-X naming convention for future L2 RoleDefinition entries.

**Ticket dispositions:**
- **T-3333** (Consultant S3\* role formalization) — largely subsumed by this ADR; closeable-as-superseded on the operator's word.
- **T-3778** (sit-down on ORG-088 roster + archivist routing) — T-3806 walkthrough material + this ADR give the sit-down concrete material; the sit-down itself is orthogonal (about roster CONTENT decisions, not the VOCABULARY this ADR captures).

## Alternatives considered

- **Keep `sub_orchestrator` L2 role_id as the mason/artisan identifier.** Rejected TG 10109: "Sub head orch is a silly mouthful, let's say head orchestrator is shorthand for head engineering orchestrator, and the suborchs are regular engineering orchestrators."
- **Adopt `head_system_3_star` for Consultant** (uniform pattern). Rejected TG 10116: "System 3\* is the auditor role. Consultant is a Head Auditor." VSM methodology label ≠ role name.
- **Keep universal `orchestrator` L2 role_id** (all seven personas hold it). Rejected TG 10116 (see §5).
- **Add `key: bool` + `seating: enum` as first-class attributes.** Rejected TG 10118 (see §Non-decisions).
- **Hard-purge `specialist` audit-role-id.** Rejected TG 10120 read (forward-normalize per the operator's "normalize it" ratified; backward-compat retained per P4 audit-chain continuity + within-tier D1 decision).

## Compatibility and migration

- **Audit-chain continuity preserved.** All historical ledger entries + tickets + notes retain their original role-name strings; only `role_id` writes going forward use the new vocabulary. `agent_role_map.py` still resolves the legacy `specialist` audit-role-id backwards.
- **Legacy identifiers retained** as OS-account identifiers (`suborch1`, `suborch2`) + persona-orientation account paths (`/home/suborch1/`, `/home/suborch2/`) — these are audit-chain-immutable historical references, not role names.
- **Rendered artifacts per-seat** are refreshed by the manual propagation pass (T-3434); procedure in `rulebook/persona-orientation/README.md`. source-bindings.yaml stamped @bad1a773d. *(Erratum 2026-09-27, operator-approved TG 10399 / T-4036: previously read "auto-refresh per T-3432", which kept propagation manual.)*

## Non-goals

- No persona MEMBERSHIP changes (roster stays as ORG-088 D-2 ratified).
- No changes to organism layer role references beyond the mechanical rename cascade already in T-3807.
- No new personas / new key seats.
- No claim about the pattern extending to non-orchestrator role classes beyond `auditor`. If a future role class doesn't parse the head-X pattern (a role whose "head" concept doesn't map to a single seat, or a class that isn't orchestrator-shaped), amend rather than force.

## Sources and falsifier map

The operator's TG spec ratified 2026-09-19 (all times UTC):
- **TG 10103** (T01:12Z) — initial spec: head vs regular orchestrators; key vs regular; full vs partial seating; project_orchestrator / implementer / reviewer subagent-only; memory_orchestrator = Archivist; system_orchestrator = Recovery; Consultant = System 3\* the-reviewer-from-the-outside; Operator = the operator.
- **TG 10105** (T01:18Z) — general shape amendment: sub-orchestrators of any TYPE communicate laterally via ticket + mail, not to Operator.
- **TG 10109** (T01:33Z) — rename: sub-orch → regular engineering orchestrator.
- **TG 10111** (T01:35Z) — head-X pattern generalizes as key-seat indicator across all domains.
- **TG 10116** (T01:55Z) — corrections: Consultant = head_auditor (not head_system_3\*); universal `orchestrator` role_id RETIRED.
- **TG 10118** (T02:01Z) — Operator ceremonial confirmed; `key` + `seating` schema addition retired.
- **TG 10120** (T02:13Z) — `specialist` forward-normalize; `second-head` rename confirmed.
- **TG 10144** (T21:41Z) — Orchestrator's L2 role_id ratified as `head_engineering_orchestrator` directly; every key seat carries its full role name including domain.

**Ticket + code trail:**
- T-3806 — authoritative Roles-spec attach (the operator's TG verbatim + collision surface enumeration + attached documents).
- T-3807 — mechanical batch execution (merged @2fc97d63f); mason D7 arc-drive.
- T-3809 — cosmetic recovery_continuity_bundle strings (merged @0b5da742a).

**Falsifier — this ADR is wrong if:**
- The operator amends the vocabulary later without re-ratifying via this ADR's supersede.
- A future ratified role class doesn't parse the head-X pattern, and the pattern isn't amended to accommodate.
- Universal `orchestrator` retirement leaves a routing gap not covered by convention (currently: no such gap; if one surfaces, amend).

## Re-evaluate

- Every new key seat added: verify it parses the head-X pattern (or amend if not).
- Every ORG-088 D-2 amendment: cross-check that the roster + vocabulary references this ADR.
- Every new role class outside the orchestrator family: verify the pattern extends (or amend for the new class).
