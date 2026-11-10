---
aliases: [ORG-108]
id: ORG-108
title: "Cross-host extension of the inter-agent Maildir substrate"
status: Proposed 2026-11-06 by Orchestrator (head_engineering_orchestrator); pending the operator ratification.
date: 2026-11-06
author: Orchestrator (head_engineering_orchestrator)
steward: orchestrator
appetite: 5
riskiest_assumption: "That a narrowly scoped host transport, RFC-005's recoverable journal, and a transport-quiescence placement fence can retain or explicitly block every delivery across remote rename, lost acknowledgement, restart, consumer claim, and placement change without broad host access or duplicate admission."
build_disposition: needs-build
scoping_ticket: T-5497
amends: [ORG-026]
relates: [ORG-105, ORG-068, ORG-080]
registers_components: [mailbox]
ratifiable_files: [pantheon-config:architecture/adr/ORG-108-cross-host-maildir-relay-extension.md]
---

# ORG-108: Cross-host extension of the inter-agent Maildir substrate

**Status:** Proposed 2026-11-06 by Orchestrator — pending the operator ratification.

## Decision

Pantheon extends [[ORG-026]] so a persona may exchange inter-agent mailbox messages with a persona whose hub is on the other approved host.

The extension remains a bounded, store-and-forward transport between the VPS and pve2. Local sends, local durable queues, local consumer ownership, and acknowledgement remain governed by ORG-026.

For a remote destination, the chosen direction is a narrowly scoped host-to-host transport capability carrying a durable relay envelope. The capability authenticates a host transport endpoint, not a persona. Persona identity remains message metadata and is not an access credential.

The extension preserves these decisions:

- A remote send is durably recorded before it is reported accepted.
- An envelope is immutable and bound to exactly one destination host and placement revision. An unresolved, refused, or uncertain old-revision outbox is never redirected, rewritten, or re-enveloped to a new host.
- Remote admission is placement-gated and duplicate-safe through a recoverable destination journal. Retry, restart, delayed acknowledgement, or consumer movement after publication must not re-admit the same identity and content digest; identity reuse with different content is a conflict.
- A seat placement revision may change only through a transport-quiescence and one-active-host fence: stop new remote sends, reconcile source outboxes and destination journal records on the old placement, prove no unresolved delivery, durably deactivate the old host and obtain its exact acknowledgement, then publish and activate the next placement. If proof or acknowledgement is unavailable, cutover blocks for reconciliation or rollback.
- The receiver's authoritative work list remains its directory scan of `new/`. Deprecated path-watch units are neither restored nor represented as the delivery guarantee.
- Rollback preserves unsent relay records, journal evidence, and uncertain-delivery evidence before restoring prior placement.
- The transport is limited to the approved two-host, one-hop migration path. It does not create general federation, multi-hop routing, or exactly-once consumer execution.
- MQTT QoS1 remains an alternative only if the combined journal-recovery and placement-fence disposable-host experiment falsifies the narrow host-capability direction.

## Why

ORG-026 chose a daemonless file mailbox for one host and one shared filesystem. T-5423 changes that premise: shared rename cannot cross hosts, while the recovery and audit properties of ORG-026 remain load-bearing.

A transport that permits automatic redirection after placement changes would create cross-host duplicate-admission risk. The design therefore sacrifices availability at an unresolved placement boundary before it permits duplicate delivery.

## Consequences

- T-5497 remains the carrier and scoping ticket.
- [[RFC-005]] specifies the journal, immutable-envelope, local-host identity, quiescence, safety, and acceptance mechanics intentionally omitted here.
- Build tickets are pre-filed and linked before ratification; they remain dependency-gated until the operator ratifies this ADR and RFC-005. Dark relay work additionally waits on T-5498's static host-resolution contract.
- T-6259 owns the non-persona-writable placement revision and one-active-host transition protocol. T-5498 remains its static host-resolution and remote-execution consumer; it does not own activation authority.
- Installer and health integration follows dark relay implementation, T-5498, and T-6259.
- T-6259's cutover fence must land before any later seat-cutover ticket.
- A live VPS-to-pve2 proof additionally needs T-5499 and a separately approved narrow host capability.
- T-5423 seat moves remain individually staged, rollbackable, soaked, and operator-supervised.

## Alternatives considered

- **Retain a single shared hub.** Rejected: it retains the VPS-local filesystem assumption.
- **General broker or federation.** Rejected: it broadens trust and operational surface beyond the migration need.
- **MQTT QoS1 relay.** Retained as a conditional fallback. T-6220 is Echo-specific and not a reusable fleet transport.

## Appetite

5/10 for the decision: migration requires a cross-host transport, but it expands a previously single-host durability boundary. This does not authorize access, deployment, or a seat move.

## Riskiest assumption

That a narrowly scoped host transport, recoverable journal, and placement-quiescence fence can retain or explicitly block every delivery across acknowledgement loss and placement transition without broad access or duplicate admission. RFC-005's disposable-host experiment is the cheap falsifier.

## Build disposition

`needs-build`; T-5497 is the scoping carrier. The linked implementation tickets remain gated on ratification and their named substrate dependencies.

## DECISIONS

- **Intent:** preserve ORG-026's durable, auditable mailbox guarantees while enabling T-5423's staged migration.
- **P4 / D4:** make journal and placement-transition evidence explicit and reviewable.
- **P6 / D1:** preserve the constrained-transport versus MQTT tension until falsified.
- **P7 / D5:** favour blocked availability over unproven duplicate safety.
- **D8 / D9:** keep ratification categorical and distinct from access, deployment, and first-fire authority.
