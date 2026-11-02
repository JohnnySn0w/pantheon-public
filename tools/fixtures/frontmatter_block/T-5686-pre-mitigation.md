---
id: T-5686
created: 2026-10-27T16:06:32Z
requester: orchestrator
target: role:head_engineering_orchestrator
state: active
project_id: null
nearest_intent_ref: maintenance:worker-guard
parent_ticket: null
depends_on: []
labels: [worker-guard, org-052]
serves: Keep the real worker-guard catches at a fraction of the current guard's size, cost and churn (the operator TG 12378, go on TG 12375).
appetite: 5
security_gate: accepted
threat_foothold: untrusted-content
threat_impact: money-credentials
threat_path: |
  a subagent steered by injected content runs sudo or pantheon-sudo, reads the sudo password file or a token/credential file, edits a protected doc, or pushes/opens a PR from its shell
security_rank: primary
delivered_to: orchestrator
---

# worker-guard v2: small literal guard, run in shadow, then swap

## Ask

Build a small literal v2 worker-guard (about 1-3k lines) covering only: sudo / pantheon-sudo / the sudo password file; credential and token files; Edit-tool protected docs; push and PR at command position; a gh allowlist; and denying subagents ticket.py security-accept / security-reject (the T-5678 identity gap). Run it in SHADOW beside the current guard (log-only). Validate it against the recorded field commands (none newly blocked) and the ~15 real catches (all still caught), then swap and retire the current guard and its tests. No obfuscation-form chasing: ORG-052 is the structural answer.

## Context

Ruling: the operator TG 12378 '1. Yes' on the T-5682 proposal. Evidence: notes/2026-10-27-worker-guard-utility-audit.md (@cf4b6bccf): ~15 real catches of 457 denials, all plain forms; T-5224 OOMs; 0.5s/command. Seed corpus: ~/.local/share/pantheon/guard-v2-seed/ (T-5661 rig drv.py, cmd lists, all review reports). Kept open: T-5642, T-5574 (usability) plus the structural T-1347/T-4458/T-5393. Dropped: T-5661, T-4508, T-4534, T-4851, T-5127, T-5346, T-5375, T-5391, T-5392, T-5420, T-5431.

## Transition log

- 2026-10-27T16:06:32Z [claude] filed in inbox
- 2026-10-27T16:06:59Z [claude] security-accept @7ef5fd2e79; rank=primary; orchestrator: chain is real. Injected content steering a subagent toward the sudo password file, token files or a push is the class behind the ~15 real catches in the audit.
- 2026-10-27T16:11:57Z [claude] claimed @cdd96b1d5f; moving to active
