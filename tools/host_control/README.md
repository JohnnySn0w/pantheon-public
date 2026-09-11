# Host control

This package implements the harness-neutral host-control boundary ratified in
ORG-052. It is intentionally separate from:

- `tools/harness_adapter/`, which presents harness-specific configuration;
- `tools/persona_installer.py`, which generates and verifies typed installation
  plans;
- `tools/credential_broker.py`, which issues purpose-scoped credential leases;
  and
- mailbox/ticket delivery, which carries opaque handles and work context but is
  never identity, approval, actuation, or durable operation state.

## Authority boundary

The public host surface accepts fixed logical identifiers and opaque handles.
It never accepts a caller-supplied shell, command, executable, path, content,
environment, uid, group, unit or policy text, sudo target, credential value,
serialized plan, or claimed persona authority.

The eventual system D-Bus service derives the caller uid from the live bus
sender. Root-owned registries resolve the seat, target, manifest, harness,
deployment root, policy, templates, and operation families. MCP remains a
privilege-free harness adapter over that service.

The initial package increment does not expose D-Bus or MCP and cannot mutate a
host. It establishes only server-owned planning truth:

- frozen root-owned registry, policy, and manifest input;
- a closed actor-neutral effect digest and actor-bound plan digest;
- structured operation-family and approval-class metadata;
- subject-bound opaque plan handles;
- durable idempotent SQLite plan storage; and
- bounded, canonical, redacted plan summaries.

## Landed increments beyond Unit 1

**Broker operation state machine (Unit 2a, T-2909, PR 294).** `journal.py` v2 +
`operations.py`: the PREPARED → AUTHORIZED → DISPATCH_INTENT → DISPATCHED →
terminal lifecycle with subject-bound random operation handles, exact
plan/receipt binding, strict idempotency, and staged receipts plus write-ahead
rollback intents durably synced before any dispatch step. Execution evidence
and terminal state are appended by the broker only; the executor seam is a
value-returning Protocol that structurally cannot reach the journal.
Operations deliberately never expire.

**Approval journal (Unit 2b, T-2910, PR 297).** `approval/` is a physically
separate authority: its own SQLite file (application_id `PHCA` vs the broker's
`PHCB`; each journal loudly refuses the other's file), its own review
(OPEN → GRANTED/DENIED/EXPIRED) and grant (ISSUED → BOUND/REVOKED/EXPIRED)
state machines, single-use BOUND enforced by SQL constraints, and
ValidateAndBind semantics where an identical-tuple retry replays the original
binding and any differing element denies with a closed reason code. The broker
cannot mint or edit approval records; the approval service cannot edit plans,
operations, or receipts. Shipped defaults deny everything: a deny-all
authorizer and an empty consumer-uid set. Grant payloads (single-use nonce,
authority record id) never cross a public return; an approver-supplied audit
anchor is context and can never populate the provider-authenticated
`source_event` field.

## Implementation authority

T-2383 contains the reviewed Unit-1 implementation plan, exact signatures,
twenty mandatory tests, smoke loop, pre-mortem, and decision record. Its
reviewed plan SHA-256 is
`4aa4c20335684e3966bf234aaaa732957c477869ebe2ca223375d376d334288f`.
Units 2a/2b landed under the same discipline via T-2909 and T-2910 (plans,
dispositions, reviews, and gap registers on those tickets' transition logs).

Later increments require separate tickets, plans, reviews, and explicit gates
for journals/state machines, D-Bus services, MCP packaging, executors, the
privileged helper, Telegram human approval, whole-system acceptance, and
production enablement.

Passing Unit 1 does not create a capability, authorize production use, or allow
the broker to amend its own authority boundary.
