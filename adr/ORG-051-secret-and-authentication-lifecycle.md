---
aliases: [ORG-051]
title: Secret and authentication lifecycle for Pantheon hosts
status: Ratified 2026-07-30 by the operator (Telegram 275)
date: 2026-07-30
author: Codex (Cairn)
relates: ORG-039, ORG-046, ORG-047, ORG-050
build_disposition: needs-build
scoping_ticket: T-2828
---
# ORG-051 — Secret and authentication lifecycle for Pantheon hosts

**Status:** **Ratified 2026-07-30 by the operator (Telegram 275).** Migration from
legacy home-directory password files remains a staged implementation task.

## Problem

Pantheon currently uses informal `.pass` files in home directories for some
passwords and keys. This provides no common naming, ownership, audit, rotation,
revocation, or installer interface. It also makes a generic persona bringup
unsafe: a template may accidentally read, copy, log, or overwrite a credential.

## Decision

Define a credential reference, not a credential value, as the only portable
interface:

```yaml
credential:
  id: telegram/orchestrator/bot-token
  kind: bearer-token
  provider: host-file
  location: /run/user/UID/pantheon/credentials/telegram-orchestrator
  owner: claude
  mode: "0600"
  expires: null
```

The exact provider may evolve (host file, systemd credential, OS keyring,
secret service, or external vault), but installers and jobs consume the stable
reference contract. Values never appear in manifests, templates, tickets,
ADRs, logs, command-line arguments, or Telegram messages.

### Required properties

- **Owner and mode:** every material has an explicit Unix owner/group and
  restrictive mode; cross-account reads require a named, narrow ACL or helper.
- **Purpose and audience:** references identify token, password, deploy key,
  signing key, or another kind; a credential is not interchangeable merely
  because both are strings.
- **Use path:** consumers receive credentials through an environment/file
  descriptor/provider injection path, never by shell interpolation.
- **Expiry and rotation:** each reference records expiry or an explicit
  non-expiring justification; rotation installs the replacement, verifies the
  consumer, then retires the old material.
- **Revocation:** revocation disables consumers and removes grants before
  deleting material; the result is recorded in the audit ledger.
- **No value echo:** diagnostics redact values and stable prefixes where even a
  prefix could identify a secret.

### Brokered service identity

One PAT per user daemon is not the default design. A host-side credential
broker owns upstream credentials and issues short-lived, purpose-scoped leases
to an authenticated local caller. A request is bound to Unix uid, role,
instance, service, action, and requested lifetime; policy evaluates that tuple
before release. The broker records issuance and revocation without recording
the secret value. Consumers receive the lease through a file descriptor or
provider injection, not a command-line argument.

The broker may mint a derived token, sign a narrowly-scoped request, or proxy a
service call, depending on provider capabilities. Distinct upstream identities
remain distinct when required by the service. A direct credential reference is
the fallback for providers that cannot broker, and must carry an explicit
justification, owner, scope, and rotation/revocation plan.

### Installer interface

Generic installers expose `credential check`, `credential install`,
`credential rotate`, and `credential revoke` operations. `install` accepts
interactive or provider-backed input but never accepts a secret as a positional
argument. `plan` reports reference ids and intended ownership only. `verify`
checks provider reachability, owner/mode, expiry metadata, and consumer health
without printing the value. `rollback` removes only installer-owned material.

### Migration from `.pass`

Existing `.pass` files are legacy inputs, not the target interface. Migration
must be explicit and per-account:

1. inventory paths and consumers without reading values into logs;
2. classify each file by purpose and owner;
3. import into the selected provider with restrictive permissions;
4. switch one consumer at a time and run its health check;
5. revoke/delete the legacy file only after successful verification;
6. record path, reference id, actor, and result, never the value.

Until migrated, legacy files must be treated as sensitive, excluded from
repositories/backups that are not approved for secrets, and blocked from
generic template expansion.

## Scope

This ADR defines the credential lifecycle and installer contract. It does not
select a single vault product, redesign ORG-047 federation, or weaken ORG-040's
authority provenance. Provider selection, broker implementation, and migration
order are follow-up implementation decisions.

## DECISIONS

- **Reference contract over provider lock-in** — justifies-via D10 and ORG-039.
- **Purpose/audience/owner are first-class fields** — justifies-via P1, P5, and
  ORG-033's UID isolation.
- **Explicit rotation and revoke before deletion** — justifies-via P4 and
  ORG-050's lifecycle evidence requirement.
- **Treat `.pass` files as migration inputs, not a permanent API** —
  justifies-via P1 and the least-surprise security boundary requested by this
  gap report.
- **Broker shared service credentials and issue short-lived leases** —
  justifies-via P1 (central structural control), P5 (caller roles do not hop),
  and P4 (issuance remains attributable).
