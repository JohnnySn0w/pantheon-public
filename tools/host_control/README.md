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

## Implementation authority

T-2383 contains the reviewed Unit-1 implementation plan, exact signatures,
twenty mandatory tests, smoke loop, pre-mortem, and decision record. Its
reviewed plan SHA-256 is
`4aa4c20335684e3966bf234aaaa732957c477869ebe2ca223375d376d334288f`.

Later increments require separate tickets, plans, reviews, and explicit gates
for journals/state machines, D-Bus services, MCP packaging, executors, the
privileged helper, Telegram human approval, whole-system acceptance, and
production enablement.

Passing Unit 1 does not create a capability, authorize production use, or allow
the broker to amend its own authority boundary.
