---
aliases: [ORG-050]
title: Security and identity contract for installer and persona bringup
status: Ratified 2026-07-30 by the operator (Telegram 275)
date: 2026-07-30
author: Codex (Cairn)
relates: ORG-031, ORG-033, ORG-039, ORG-040, ORG-046, ORG-047
build_disposition: built
---
# ORG-050 — Security and identity contract for installer and persona bringup

**Status:** **Ratified 2026-07-30 by the operator (Telegram 275).**

## Context

Pantheon security controls currently exist, but are distributed by surface:
ORG-033 defines Unix-account isolation; ORG-039 defines framework/instance and
secret-reference boundaries; ORG-040 defines authority provenance; ORG-046
defines trusted manifest and remote binding; and ORG-047 defines cross-seed
identity. A bringup installer must compose these rules without inventing a
second, conflicting security model.

The missing artifact is a small, installer-facing contract that answers four
questions for every generated file, job, daemon, mailbox, and credential:
who owns it, what authority it carries, which trust boundary it crosses, and
how it can be revoked or rolled back.

## Decision

Treat security as a cross-cutting contract with five explicit domains:

1. **Host identity:** Unix user/group, filesystem owner, ACL, systemd user
   manager, and narrowly-scoped privileged helper.
2. **Persona identity:** role, persona, instance id, harness selection, and
   memory/persona files. Persona identity does not grant host privilege.
3. **Communication identity:** mailbox sender/consumer, ticket actor, ledger
   actor, and comms/MCP account. Each emitted action retains its originating
   actor; forwarding is not impersonation.
4. **Credential identity:** secret references and their provider/location.
   Installers may validate references and permissions, but never copy secret
   values into templates, ADRs, tickets, logs, or generated unit files.
5. **Authority identity:** human ratification, policy, installer action, and
   runtime action. A file marker is only a pointer to ratifiable substance,
   consistent with ORG-040; generated content is not thereby ratified.

### Authority and trust boundaries

The installer MUST classify inputs as one of: trusted manifest, framework
template, instance/persona data, written policy, runtime message, or secret
reference. Trust does not flow across those classes implicitly. In particular:

- a runtime message cannot alter installer policy;
- persona data cannot select a different host owner or privilege boundary;
- a template cannot embed account names, credentials, or absolute host paths;
- a manifest selects approved sources but does not override local ownership or
  policy gates;
- an external identity (ORG-047) does not become a local Unix identity.

### Required installer gates

Every installer implementation exposes `plan`, `apply`, `verify`, and
`rollback` modes. `apply` is permitted only after:

- owner/group and target-path checks pass;
- existing files are classified as managed, user-owned, or conflicting;
- secret references resolve without exposing values;
- generated units and timers are attributable to role/persona/instance;
- a clean-home/dry-run and leakage scan pass;
- an audit record names source, actor, host, harness, and result.

`verify` MUST be safe to run without mutating state. `rollback` MUST remove or
disable only installer-owned artifacts and MUST preserve user-owned state.
Failure routing goes to the owning role's queue; installers do not silently
repair across Unix-account boundaries.

### Minimum lifecycle controls

Bringup records creation, activation, verification, rotation, revoke, and
retirement. Revocation disables generated jobs and communication consumers,
removes ACL grants, and invalidates credential references where the provider
supports it. Rotation changes references atomically before old material is
retired. Every control has an observable result suitable for a later audit.

## Scope boundaries

This ADR composes existing security decisions; it does not replace ORG-033's
multi-instance architecture, ORG-040's authority model, ORG-046's manifest
trust rules, or ORG-047's federated identity protocol. It does not prescribe a
cryptographic signing scheme. If the trust model admits an untrusted writer,
ORG-040's accepted-risk boundary must be revisited before that writer is
allowed to install or ratify governance artifacts.

## Consequences

Installers can be generic across personas and harnesses while keeping the
security-bearing decisions in a stable contract. Diagnostics can report a
specific failed domain or gate instead of a vague bringup failure. The cost is
that each installer must emit ownership, provenance, and lifecycle evidence;
convenience scripts that bypass those gates are out of contract.

**Migration tooling (T-2633, 2026-08-07 — back-reference per the operator TG 7170):**
the generic persona control-plane cutover (`tools/persona_cutover.sh` /
`persona_rollback.sh`, documented in
`sop/standard/migrating-an-existing-persona-onto-the-control-plane.md`) is an
in-contract example, not a bypass: it runs its install/apply/verify/rollback
THROUGH `persona_installer` (via `tools/t2611_recovery_persona_apply.py`), exercising
this contract's gates. The bespoke-supervisor teardown it adds around that call
is the transitional, out-of-installer half of a one-time migration.

## DECISIONS

- **Compose existing ADRs into one installer contract rather than duplicate
  their rules** — justifies-via D10 (references prevent drift) and P1
  (structural controls must remain structural).
- **Separate host, persona, communication, credential, and authority identity**
  — justifies-via P5 (roles do not hop) and ORG-033's UID/write-scope model.
- **Require plan/apply/verify/rollback and lifecycle evidence** —
  justifies-via P4 (auditable authorship/results) and ORG-039's provisioning
  and clean-home release gates.
- **Keep the untrusted-writer case as an explicit re-evaluation trigger** —
  justifies-via ORG-040's documented accepted-risk boundary and P6 (preserve
  the real tradeoff instead of silently expanding trust).
