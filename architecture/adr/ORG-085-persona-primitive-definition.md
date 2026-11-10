---
aliases: [ORG-085]
id: ORG-085
title: "Persona primitive: harness, functional role, named continuity, and binding"
status: "Ratified 2026-10-07 by the operator (Telegram 11340, with the 2026-10-07 model/effort amendment; he co-authored it). Previously: Adopted 2026-09-01 by Consultant under D3 after independent exact-candidate APPROVE of f981f8e4e1f81c5802248007dbdc069672fd7c03; no goal-level or reserved trigger fired. Conceptual contract only: no schema implementation, registry row, installer change, routing change, or runtime migration lands here."
date: 2026-09-01
author: Consultant
steward: consultant
hitl: Autonomous
relates: [ORG-027, ORG-031, ORG-057, ORG-058, ORG-059, ORG-075, ORG-079, ORG-080, ORG-081, ORG-084, ORG-088, ORG-100]
build_disposition: no-build-needed
scoping_ticket: T-3029
no_registry_row_waiver: "Conceptual identity-and-composition decision only. It registers no component and leaves component contracts and registry coverage to ORG-080/081, while installed and runtime observations remain non-authoritative evidence governed by their adopted component/runtime contracts."
---

# ORG-085 — Persona primitive: harness, functional role, named continuity, and binding

## Intent

Give routing, germination, continuity, and harness composition one falsifiable
definition of a persona without making identity synonymous with a Unix account,
a harness, or the kind of work the persona may perform.

This decision defines three layers and the references between them. It does not
create a fourth layer, a new registry, or an implementation schema.

## Status and authority

This decision is **Ratified 2026-10-07 by the operator** (Telegram 11340), with the 2026-10-07 amendment (§Amendment). The operator co-authored it. It was earlier **Adopted 2026-09-01 by Consultant under D3**, during the TG 1890 one-week autonomy experiment. Independent review of
exact candidate `f981f8e4e1f81c5802248007dbdc069672fd7c03` returned APPROVE with
no findings, after which Consultant authorized the status transition. No goal-level
or reserved trigger fired: the ADR creates no account or access grant, secret or
spend, public/external act, operator-owned north-star change, or reserved
[[ORG-042]] topology flip. None of those acts is authorized by this ADR.

The older [[ORG-041]] “ticket for ratification” wording remains useful for the
adversarial review loop, but its below-goal ratify step is superseded by current
D3. D2 still controls the pen: workers may propose this contract, while an
authorized orchestrator writes the curated ADR bytes and a distinct reviewer
checks the exact candidate.

## Context

Pantheon already has persona names, OS accounts, harness adapters, instance
role-keys, audit identities, pinned persona rules, and runtime bindings. Those
namespaces serve different purposes, but no ADR says which one is identity,
which one describes work authority, or how they compose. The absence is now a
hard dependency for [[T-3176]], which must build continuity bundles without
inventing a competing persona schema.

Two current facts must not be generalized into the wrong architecture:

1. [[T-1922]] and `instance.yaml` currently bind mostly one persona to one
   instance role-key. An **instance role-key is a binding key**, not the
   conceptual functional Role defined here. Several personas may share one
   functional Role without sharing an account or instance role-key.
2. [[ORG-057]] makes a session, model, harness, Unix account, Telegram bot, and
   device replaceable embodiments. The operator's T-3029 ruling nevertheless assigns
   a persona its **specific Unix account** at L3. Both are true when L3 owns the
   declared binding reference and the binding alone owns the account details:
   the persona selects its present embodiment without making that embodiment
   identity canon.

[[T-3028]] may use this vocabulary, but its dependency on T-3029 was explicitly
cleared. The T-3027 MCP-provisioning concern was folded into [[T-3030]]. Neither
is directly unblocked here; T-3176 is the sole binding downstream dependency.

## Decision

Adopt the following composition:

```text
L1 HarnessDefinition -> L2 RoleDefinition -> L3 PersonaDefinition
                                      \
                                       -> PersonaBinding -> ResolvedPersona
```

- **L1 Harness** is the generic installed and controlled substrate common to
  persona instantiations: Harness Controller, adapters, systemd/tmux, storage,
  periodics and triggers, MCP and plugin mechanisms, install machinery, and the
  memory mechanism.
- **L2 Role** is a shareable functional work and authority envelope. It owns
  authentication/permission policy references, capabilities and work shape,
  autonomy and decision authority, trust posture, ratifiable-document scope and
  access, and model/effort **requirements**: a harness-neutral capability tier and effort
  intent. The binding resolves them to a concrete model (amended 2026-10-07, see §Amendment).
- **L3 Persona** is the stable named continuity: identity and memory-content
  references, accumulated history and relationships, role membership, a
  declared default embodiment binding, and pinned communication style,
  dialect, and tone.

Definitions state intent. Bindings attach definitions to a concrete account and
harness. Runtime observations say what is currently installed or happening.
No plane may silently become authority for another. [[ORG-084-executable-runtime-contract-projection|ORG-084]]
is a Proposed, unbuilt projection design that may later consume this separation;
it is not current runtime authority and is not adopted or promoted here.

## Conceptual serializable signatures

These signatures name required field families. They are conceptual contracts,
not a new file format or registry.

The conceptual `HarnessDefinition` and `RoleDefinition` signatures are relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §HarnessDefinition and §RoleDefinition.

`role_id` is the functional Role. There is no separate `functional_role_class`.

The conceptual `PersonaDefinition` signature is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §PersonaDefinition.

`default_role_id` MUST be a member of `role_ids` and `default_binding_id` MUST
be a member of `binding_refs`. The default binding declares the persona's
present/default embodiment, including its specific account by reference. It is
not a claim that the binding is healthy or the sole runtime authority holder.

The conceptual `PersonaBinding` signature is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §PersonaBinding.

The binding does not own functional-role membership. `instance_role_key_ref`
preserves the current `instance.yaml`/`agent_role_map` binding namespace without
turning it into a second Role authority. Communication endpoints live here;
communication style remains in PersonaDefinition.

The conceptual `OverrideRecord`, `ResolvedPersona` and `ResolutionReceipt` signatures are relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §OverrideRecord, §ResolvedPersona and §ResolutionReceipt.

## Field ownership and authority

| Field family | Owner | Plane | Cardinality | Override rule | Sole authority / duplicate prohibition |
|---|---|---|---|---|---|
| stable id, label, source revision/digest | each L1/L2/L3 definition | definition | exactly one id; one label | ids never override; labels only at owning definition | governed definition source; registry/runtime may reference, never rewrite |
| generic adapters, controllers, systemd/tmux/storage/triggers/plugins/MCP mechanisms | L1 | definition | one adapter contract plus sets | L2 may fill declared specialization slots only | harness/component contracts; installed observations are evidence only |
| memory mechanism | L1 | definition | set of refs | no L3 mechanism replacement without typed adapter override | [[ORG-058]] mechanism authority; no memory payload in L1 |
| functional Role membership and default | L3 | definition | non-empty set; exactly one default | explicit L3 source amendment only | PersonaDefinition; PersonaBinding and instance role-key MUST NOT duplicate membership |
| work envelope, capabilities, MCP/skill allowance | L2 | definition | sets | L3 may narrow only; never widen privilege | RoleDefinition plus [[ORG-059]] native authority; discovery is not authorization |
| model/effort requirement (harness-neutral) | L2 | definition | exactly one each | L3 persona-narrow override with OverrideRecord | RoleDefinition (amended 2026-10-07) |
| model/effort resolution (concrete, harness-specific) | binding | binding | exactly one each per binding | none (re-bind instead) | PersonaBinding; must satisfy the L2 requirement (amended 2026-10-07) |
| autonomy, decision authority, trust posture, ratifiable scope/class access | L2 | definition | non-empty authority refs where required | L3 may narrow only when the named policy permits; never widen | rulebook/RoleDefinition; no style or target name grants authority |
| identity canon, memory content, relationship history | L3 | definition | reference sets | governed source correction/supersession only | [[ORG-057]]/[[ORG-058]] sources; hidden/native/session state never becomes canon |
| persona/style/dialect/tone source | L3 | definition | exactly one pinned persona and style source | explicit provenance-bearing persona amendment | pinned persona source; Role constraints may bound conduct but do not own style |
| binding references and default embodiment selection | L3 | definition | non-empty set; exactly one default | explicit L3 source amendment | PersonaDefinition selects; binding details remain outside identity canon |
| account, instance role-key, mailbox, Telegram, install manifest | binding | desired binding | one account/mailbox/install ref; optional endpoint/key | binding replacement or typed binding amendment | PersonaBinding/`instance.yaml`; PersonaDefinition references but does not copy details |
| harness adapter attachment | L1 contract + binding | definition/binding | one selected adapter per binding | binding change after compatibility checks | adapter contract owns behavior; binding owns selection |
| installed state, active process, health, current runtime binding | runtime observation | runtime | observations may be many; one active authority receipt | never overrides definition or desired binding | adopted component/runtime contracts plus [[ORG-057]] active-authority receipt; proposed ORG-084 is an informed future projection only |
| lifecycle/status | owning object | its own plane | exactly one closed-enum state per object | only its owning lifecycle transition | runtime health cannot rewrite definition or desired-binding lifecycle |
| override applicability, waiver, provenance, expiry | OverrideRecord | definition/binding | one typed record per override | no implicit precedence; stale/unknown/conflicting records refuse | named authority owns waiver; document order is never authority |

## Composition and override contract

Composition is purpose- and Role-specific. A persona with several Roles never
receives the union of their authority merely because the same identity holds
them.

The conceptual `compose(persona_id, requested_role_id?, requested_binding_id?, purpose)` procedure is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §compose.

Normative invariants:

1. L1 → L2 → L3 is the only layer precedence. Binding and runtime are planes,
   not later layers.
2. A higher layer may set only a lower layer's declared typed slot. Arbitrary
   shadowing is invalid.
3. L3 overrides are persona-narrow. They cannot mint a capability, credential,
   approval, trust class, decision authority, or ratifiable-document access.
4. Every effective field has one authoritative writer. Mirrors are
   digest-bound and mismatches refuse; document order never chooses a winner.
5. Missing source revision/digest, unknown applicability, expired waiver,
   conflicting records, or an undeclared field is a typed refusal.
6. A multi-role persona resolves exactly one Role for one purpose. No implicit
   authority union exists.

## Interaction contracts

### Persona-target routing

A persona target pins the named logical identity. It does not mean “any account
with the same label.”

The conceptual `resolve_persona_target(persona_id, purpose)` procedure is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §resolve_persona_target.

### Functional-Role-target routing

A Role target identifies an eligible set, not an actor and not an authority
grant.

The conceptual `resolve_role_target(role_id, work_ref, purpose)` procedure is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §resolve_role_target.

[[T-3028]] owns ticket target syntax, WIP and availability rules, selector
policy, atomic claim mechanics, and [[ORG-003]]/[[ORG-078]] integration. This
ADR requires deterministic, idempotent, receipt-bearing, fail-closed behavior;
it does not implement or pre-decide those mechanics. Naming a stronger Role or
persona never launders authority: [[ORG-059]] delegation remains a durable
request accepted and owned by the receiving Role under its native authority.

### Germination and installation

The conceptual `project_install(binding_id, purpose)` procedure is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §project_install.

[[ORG-079-l1-install-completeness-contract|ORG-079]] supplies persona-stack L1 install-completeness; role/persona values
remain L2/L3 inputs. [[ORG-080]] owns germination process and component
contracts, and [[ORG-081]] owns registry coverage/adoption. Installed and
runtime observations remain non-authoritative evidence governed by the adopted
contract for the observed component. Proposed ORG-084 may later project those
facts, but this ADR neither adopts it nor treats its vocabulary as load-bearing.
Drift creates evidence or a repair plan; it never rewrites a definition or
desired binding.

### Mailbox and Telegram delivery

Every ordinary delivery resolves to one PersonaBinding and uses its
`mailbox_ref` as the durable per-persona destination. `telegram_ref` is an
optional bound endpoint, not identity or authority.

The conceptual `deliver(target, message_id)` procedure is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §deliver.

Explicit fan-out, if ever needed, requires a separate downstream contract. A
Role target does not cause one message to be copied to every persona holding the
Role.

### Continuity and handoff

Persona identity survives binding replacement; action authority does not exist
twice.

The conceptual `handoff(persona_id, old_binding_id, candidate_binding_id, checkpoint)` procedure is relocated verbatim to `notes/2026-11-03-org-085-conceptual-contracts-relocated.md` §handoff.

`default_binding_id` is desired/default selection; the active-authority receipt
is observed continuity state. [[T-3176]] may serialize references, checkpoints,
and receipts implementing this interface, but it must not copy identity or
memory payloads into a second canon.

## Lifecycle and failure semantics

- Definition lifecycle: `declared -> deprecated -> retired`. Retired
  definitions never resolve. Reactivation requires a new governed transition;
  runtime observation cannot do it.
- Binding lifecycle: `planned -> enabled -> disabled -> retired`. Verification
  may overlap enabled and planned bindings, but only one may hold action
  authority.
- Runtime observation states such as `unknown`, `starting`, `ready`,
  `degraded`, and `stopped` belong to the applicable adopted component/runtime
  contract. They are evidence used under a freshness rule, not definition
  lifecycle. Proposed ORG-084 may later project them without becoming their
  authority.
- Unknown ids, stale or missing digests, no eligible binding, ambiguous Role
  resolution, missing selector authority, duplicate delivery, unauthorized
  override, and split brain all fail closed with a typed receipt.

## Compatibility and migration

This decision is additive and initially descriptive:

- Current persona ids and audit ids remain unchanged.
- Current `instance.yaml` instance role-keys and the fallback mirror in
  `tools/agent_role_map.py` remain valid 1:1 binding keys. No consumer must
  reinterpret them as functional Roles until separately migrated.
- Multiple personas sharing a functional Role is a conceptual cardinality, not
  a claim that current routing already implements it.
- The string `codex` currently names a persona/account and appears in harness
  contexts. Typed fields disambiguate `persona_id`, `account_ref`, and
  `harness_id`; this ADR does not rename historical data.
- T-3176 consumes these definition and binding references as its hard schema
  dependency. T-3028 and T-3030 are informed consumers only.
- No account, permission, model configuration, manifest, mailbox, service,
  runtime controller, or ticket is changed by adopting this ADR.

## Alternatives considered

### Collapse to two layers

Rejected. Harness + Role mixes mechanism with authority; Role + Persona mixes a
shareable work envelope with named identity. Either collapse makes install or
authorization ambiguous.

### Define Persona as its seat or Unix account

Rejected. It would make replacement and recovery an identity change, contrary
to [[ORG-057]]. L3 instead selects a concrete binding while canon remains
portable.

### Treat Role as a tag only

Rejected. [[ORG-027]] and [[ORG-075-seat-ability-model|ORG-075]] make work envelope, autonomy, access, and
authority operational. A label without these references cannot govern routing
or composition.

### Add a fourth primitive axis

Rejected absent evidence. Instance role-key, binding, and runtime observation
are real namespaces, but they are relations/planes rather than another layer.
Evidence that cannot be expressed without duplicate authority or arbitrary
precedence is the trigger to reconsider this ruling.

### Create a persona registry beside the System Registry

Rejected. It would duplicate component, binding, installed, or runtime facts
already governed by [[ORG-080]], [[ORG-081]], `instance.yaml`, and adopted
component/runtime contracts. Proposed ORG-084 is an informed projection seam,
not an authority this ADR may promote. This ADR defines concepts and ownership
only.

## Amendment 2026-10-07: model and effort split between L2 and the binding

**the operator, Telegram 11338** (on Orchestrator's walk review: "point 2 is very good actually, keep that"). Concrete model names are harness-specific (Opus on Claude Code, gpt-5.5 on Codex), so holding a `ModelRef` in the harness-neutral Role leaked L1 detail into L2, and a harness swap would have needed a role edit. Now L2 states a harness-neutral **requirement** (`model_requirement: CapabilityTierRef`, `effort_intent: EffortIntentRef`), and the **binding**, which already names the harness, resolves it to the concrete `ModelRef`/`EffortRef`. A binding whose model doesn't satisfy its role's requirement is a refusal, not a silent downgrade. `ResolvedPersona.effective_model` and `effective_effort` are unchanged: they read the binding's resolution.

Also from that review, considered and kept as written: trust posture stays L2. The general model is role-seated trust; Pantheon's higher trust is an instance-level setting of its role definitions, because Pantheon is the R&D instance (the operator TG 11336).

## Consequences

Positive:

- one vocabulary can be consumed by routing, germination, continuity, and
  harness adapters;
- functional work authority no longer hides inside persona or account names;
- identity remains continuous across replaceable embodiments;
- every override, routing choice, delivery, and handoff can carry provenance.

Costs and tensions:

- downstream implementations must translate existing role-key and account
  bindings into typed references without breaking current 1:1 behavior;
- explicit receipts and refusal states add machinery, but prevent silent
  precedence and split-brain behavior;
- L3 owns a specific binding selection while identity remains embodiment
  independent. This is a preserved P6 tension, not permission to erase either
  side.

## Security and refusal properties

1. Persona or Role naming is never authentication, authorization, credential
   selection, or approval.
2. Capability discovery never grants invocation; native authority revalidates
   at the provider boundary per [[ORG-059]].
3. L3 cannot widen L2 capability, trust, or decision authority through an
   override.
4. Binding/runtime evidence cannot modify identity canon, role membership, or
   desired state.
5. Hidden session/model memory and ungoverned home-directory cruft are not
   identity or memory authority.
6. One persona has at most one action-authoritative binding at a time.
7. Zero-candidate, ambiguous, stale, duplicate-writer, and split-brain states
   refuse rather than guess.

## Source and falsifier map

The source and falsifier map (claim, primary authority, and the observation that falsifies this ADR's use of it) is relocated verbatim to `notes/2026-11-03-org-085-evidence-basis-relocated.md` §Source and falsifier map.

No version-sensitive external claim is load-bearing. No web source is required.

## Re-evaluate

Revisit this decision when any of the following occurs:

- two personas share one functional Role in production and the resolution
  contract cannot choose or refuse deterministically;
- [[T-3176]] finds that a continuity bundle cannot reference this primitive
  without duplicating identity or binding authority;
- a fourth-axis candidate carries evidence that definition/binding/runtime
  planes cannot express without arbitrary precedence;
- an ORG-080/081 change, adopted runtime-contract change, or future ORG-084
  adoption creates overlapping field authority;
- a persona needs concurrent action-authoritative interfaces; or
- a routing, override, or handoff incident succeeds through an ambiguous state
  this ADR says must refuse.

## Non-goals

- Do not relitigate [[ORG-027]] or redefine tickets ([[ORG-003]]) or ledger
  ([[ORG-001]]).
- Do not absorb [[ORG-042]] topology or [[ORG-057]] continuity implementation.
- Do not define ticket syntax, routing selectors, claim races, or priority
  ordering; those belong to T-3028 and existing ticket authorities.
- Do not define an installer, manifest migration, component contract, registry
  row, runtime collector, health probe, service, or schema file.
- Do not grant accounts, credentials, permissions, capabilities, or external
  communication authority.

## operator-skeleton rulings 1–15

| # | Ruling | Status |
|---|---|---|
| 1 | L1 Harness is generic install/control substrate | built |
| 2 | L2 Role owns functional work and authority envelope | built |
| 3 | L3 Persona owns named continuity, content/history, style, and binding selection | built |
| 4 | composition is L1 → L2 → L3 | built |
| 5 | downward overrides are rare, typed, bounded, and provenance-bearing | built |
| 6 | model/effort defaults are L2 with persona-narrow override | built; amended 2026-10-07: L2 holds the harness-neutral requirement, and the binding holds the concrete model |
| 7 | autonomy, authority, and trust posture are L2 | built |
| 8 | memory mechanism is L1; memory/identity content is L3 | built |
| 9 | communication style/dialect/tone is L3 | built |
| 10 | no fourth axis or over-taxonomy | built |
| 11 | routing/install/mailbox/continuity interaction contract | built conceptually; implementation deferred-with-disclosure |
| 12 | explicit conceptual serializable fields without parallel registry | built |
| 13 | definition/binding/runtime planes preserve ORG-081 and adopted runtime-contract authority without promoting Proposed ORG-084 | built |
| 14 | D2/D3 status and handoff legality | built; Adopted 2026-09-01 by Consultant after independent exact-candidate approval |
| 15 | full report enumerates rulings | built in this and the reviewer-ruling tables |

## Independent-reviewer rulings 1–15

The independent-reviewer ruling table (rulings 1–15 and their status) is relocated verbatim to `notes/2026-11-03-org-085-review-history.md` §Independent-reviewer rulings 1–15.

No ruling is infeasible-stopped. No implementation claim is made.

## DECISIONS

- **Use three minimal layers plus separate binding/runtime planes** — serves the
  intent of one falsifiable primitive without identity/work-capability
  conflation — **justified-via P1, P2, P3, P6, D7.**
- **Let PersonaDefinition select a binding while PersonaBinding alone owns the
  account and harness details** — serves both the operator's L3 specific-account
  assignment and harness-independent identity — **justified-via P1, P6 and
  [[ORG-057]].**
- **Make Role resolution single-context, deterministic, atomic, and
  receipt-bearing rather than unioning all roles** — serves collaboration
  without privilege accumulation — **justified-via P1, P4, D7 and
  [[ORG-059]].**
- **Keep component and registry authority with ORG-080/081, leave observations
  with adopted component/runtime contracts, and treat Proposed ORG-084 only as
  an informed projection seam** — serves one authority per field without
  silently ratifying a draft — **justified-via P1, P4, D10.**
- **Preserve current instance role-keys as binding compatibility rather than
  treating their 1:1 shape as the Role primitive** — serves migration without
  freezing an implementation accident into architecture — **justified-via P1,
  P4, P6, D10.**
- **Treat only T-3176 as hard-unblocked and name T-3028/T-3030 as informed
  consumers** — serves audit-chain truth over the stale ticket Ask —
  **justified-via P4, D4.**
- **Adopt only after independent exact-byte approval, while retaining merge to
  Consultant** — serves D2 pen ownership, reviewer independence, and the current D3
  below-goal closure path; candidate `f981f8e4e1f81c5802248007dbdc069672fd7c03`
  was APPROVED with no findings before the status transition, and no reserved
  trigger fired — **justified-via P5, D2, D3, D6, D7.**

## Amendment 2026-09-27 — role naming convention (T-4003)

Owed by [[ORG-100]] §Pending downstream ADR amendments (operator-ratified 2026-09-19). Appended; the ratified body above is unchanged. New L2 RoleDefinition entries (the §Decision table's L2 rows) follow ORG-100's ratified naming pattern:
- `head_<domain>_orchestrator` for a domain's key seat;
- `<domain>_orchestrator` for its regular seats;
- `head_auditor` for the auditor class.

A role class that doesn't parse under that pattern is an ORG-100 amendment, not a local exception.
