---
aliases: [ORG-049]
id: ORG-049
title: "Manifest-driven Pantheon installation and convergent bringup"
status: Ratified 2026-07-30 by the operator (Telegram 275). SUPERSEDED 2026-08-26 by [[ORG-080]] germination engine (operator-ratified via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle; ORG-049's layers become the op taxonomy, its four modes become the engine verbs).
date: 2026-07-30
build_disposition: built
---

# ORG-049 — Manifest-driven Pantheon installation and convergent bringup

**Status:** **Ratified 2026-07-30 by the operator (Telegram 275). SUPERSEDED 2026-08-26 by [[ORG-080]] germination engine** (operator-ratified via Telegram 8612 as the ORG-080 + ORG-081 + ORG-082 bundle; ORG-049's layers become the op taxonomy, its four modes become the engine verbs). This record originally scoped the installer contract; the engine is now the authoritative composer.

**Related:** [[ORG-028]] (portable config topology), [[ORG-029]] (Pantheon
distribution), [[ORG-031]] (harness abstraction and daemon onboarding),
[[ORG-039]] (harness-config distribution), `instance.yaml`,
`sop/standard/pantheon-bringup.md`, and
`sop/standard/onboarding-a-new-harness.md`.

## Context

Pantheon bringup is currently correct in pieces but fragmented across account-
specific recipes. A seed instance can therefore contain the right files while
still having a wrong user, mailbox ACL, harness home, systemd unit, MCP path, or
persona overlay. The failure is convergence: repeated bringup should produce one
known state, and verification should prove that state at every boundary.

The existing architecture records define the ingredients but not one installer
contract. ORG-028 defines the manifest seam, ORG-031 separates harness-specific
runtime from orthogonal Pantheon wiring, and ORG-039 defines framework/instance
distribution plus host provisioning. ORG-049 makes the composition and lifecycle
explicit.

## Decision proposal

Adopt one manifest-driven installer with explicit, composable layers. A persona
is substitutable data; a seat is the runtime composition of a role, harness,
shared substrate, and persona overlay.

### Layer 0 — host substrate

The installer provisions or verifies Unix users and groups, package/runtime
prerequisites, linger and user-systemd availability, filesystem ownership,
runtime directories, mailbox roots, and secret/deploy-key placeholders. It must
support a documented single-user mode without silently weakening the multi-user
trust boundary.

### Layer 1 — connective Pantheon substrate

The installer provisions the harness-independent control plane: mailbox and
acknowledgement paths, ticket and ledger access, comms/MCP registrations,
timers/cron jobs, monitors, failure routing, and shared tool configuration.
Jobs are generated from role and manifest data; account-specific copies are
never hand-edited as the source of truth.

### Layer 2 — harness runtime

The installer selects a `PantheonHarnessAdapter` and provisions its command,
session, event, inbound, diagnostic, approval, and harness-home surfaces. A
harness override is a deployment choice; it does not change persona identity or
role. Unsupported or unverified harness features fail closed with a namespaced
diagnostic rather than being treated as equivalent.

### Layer 3 — persona and user-daemon overlay

The overlay contains the persona brief, memory locations, daemon-local identity,
role description, and other user-owned state. It is resolved from the manifest
and installed with explicit ownership/symlink checks. Persona content and
rotating credentials are instance data, never framework templates.

### Layer 4 — policy and governance

ADRs, SOPs, the rulebook, and ratification/authority gates remain the written
source of policy. The installer may install, reference, and verify policy
artifacts; it must not silently synthesize or weaken policy from runtime state.
Policy changes remain subject to their existing ratification class.

### Cross-cutting umbrellas

Three concerns cut across all layers and therefore get first-class installer
contracts rather than being buried in a layer:

1. **Security and identity:** auth references, deploy keys, ACLs, ownership,
   trust boundaries, and secret non-copying. Secrets are referenced, never
   embedded in manifests or templates.
2. **Lifecycle and observability:** dry-run, apply, verify, upgrade, rollback,
   health evidence, correlated results, and failure routing. Every apply emits a
   machine-readable report and a per-role boot-smoke result.
3. **Distribution and provenance:** framework versus instance content, manifest
   schema/version, source revision, and release provenance. A seed or fork must
   be able to identify exactly which framework and harness templates produced a
   running seat.

## Installer contract

The installer consumes a versioned manifest and exposes four idempotent modes:

```text
plan   -> resolve layers, paths, ownership, jobs, and required human inputs
apply  -> materialize only the approved plan; refuse unexpected existing files
verify -> diagnose every layer and run one bounded per-role boot smoke
rollback -> restore the last recorded installation plan, never delete unknown data
```

The plan is the reversible boundary. `apply` must not copy rotating credentials,
overwrite a non-symlink unexpectedly, enable a timer before its service first
fires successfully, or claim that durable mailbox delivery means a model turn
ran. Generated jobs carry role/persona/harness provenance and use the adapter's
typed lifecycle contract.

The manifest is the only hand-authored binding seam. At minimum it names the
framework revision, harness selection, role, persona, account, mailbox, runtime
and state roots, comms/MCP references, policy bundle, and secret references. No
absolute host path or inline credential is valid unless explicitly declared by
the manifest schema.

## Scope boundaries

In scope: the composition model, manifest schema contract, installer modes,
layer ownership, generated-job provenance, idempotency/refusal rules, rollback
boundary, and boot-smoke evidence.

Out of scope: choosing a new harness, changing ORG-031's generalized/bridge
partition, rewriting existing policy, selecting a subscription/model policy, or
performing a fleet-wide migration. Those remain separate decisions or tickets.

## Consequences

Positive: a fresh seed and a repaired existing seat use the same path; persona
substitution does not require duplicating substrate recipes; harness changes are
localized to the adapter; and a broken account is diagnosable by layer rather
than by a long, account-specific checklist.

Cost: the installer becomes a privileged control-plane component and must carry
schema/version, ownership, rollback, and evidence discipline. Existing hand-
installed copies require an inventory/import step before they can be managed.

## Implementation sequence (proposed)

1. Freeze the manifest schema and layer ownership matrix.
2. Extract the current Recovery Codex activation into a generic planner/apply/verify
   interface; retain it as the first harness-neutral fixture.
3. Add clean-home and existing-seat convergence tests for one Claude and one
   Codex role.
4. Add generated-job provenance and rollback state.
5. Extend to the remaining personas and only then consider seed publication.

## DECISIONS

- **Use a layered installer rather than one persona-specific recipe — justifies-via P1, ORG-028, ORG-039.** The layer boundaries make substitution and portability structural.
- **Keep policy as a written, ratified layer — justifies-via P3, D3.** Runtime convenience must not become an unratified policy generator.
- **Make plan/apply/verify/rollback first-class — justifies-via P4, P6.** Privileged bringup needs reversible execution and evidence, not a best-effort shell sequence.
- **Treat security, lifecycle, and provenance as cross-cutting contracts — justifies-via P1, P4.** Each affects every layer and is unsafe when hidden inside one implementation.
