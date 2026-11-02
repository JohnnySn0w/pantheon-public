---
id: T-9901
created: 2026-10-27T16:30:00Z
requester: artisan
target: role:engineering_orchestrator
state: active
project_id: null
nearest_intent_ref: maintenance:ticket-board
parent_ticket: null
depends_on: []
labels: [ticket-system, frontmatter]
serves: >
  keep filing working
  when a ticket carries

  a folded block scalar
security_gate: accepted
threat_foothold: untrusted-content
threat_impact: money-credentials
threat_path: |
  first line: with a colon
  state: done
  labels: [bogus]
threat_justification: |-
  strip chomping, no trailing newline
not_security_reason: |+
  keep chomping

security_rank: primary
delivered_to: artisan
---

# T-5689 synthetic fixture: folded, literal, strip and keep block scalars

## Ask

Fixture only. The `  state: done` and `  labels: [bogus]` lines inside threat_path are
adversarial continuation lines: no reader may turn them into keys.

## Transition log

- 2026-10-27T16:30:00Z [artisan] filed in inbox
