# Structural identity beats routing discipline — the bridge-to-kino pivot

**Date authored:** 2026-05-10
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015; the older "awaiting ratification" convention predated it)
**Re-evaluate when:** kino's per-agent identity model meets a sustained scaling stress not yet observed (e.g. multi-org deployment where N×M agents fan out); or when a bridge-shaped fallback resurfaces under operational pressure (cost, latency, infra reliability) and the trade-off needs a fresh look against the no-longer-default discipline-tier alternative.

## Observation

When a single shared resource — a credential, a bot token, a connection identity — is routed across N agents via discipline-tier table lookup ("agent X gets routing key Y"), the **per-agent-identity-collapse** failure mode is structurally inescapable: any routing miss, any race condition, any code path that doesn't consult the routing table emits as the shared identity. No amount of test coverage, code review, or persona discipline catches this reliably under load — the fundamental architecture is one identity, multiplexed by policy.

The structural fix is **per-agent identity at the OS / process / subprocess level** — each agent runs as a distinct unix user / distinct process / distinct authenticated subprocess, so "which agent emitted this" is a property of *who is running the code*, not *what the code chose to put in a header*. The failure mode becomes impossible-by-construction rather than preventable-by-discipline.

This is the **architecture-over-policy principle (P1)** applied at the identity-multiplexing boundary. Same family as the instruction-fidelity-collapse pattern (`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`) at the dispatcher↔dispatchee boundary — both lessons argue that load-bearing rules need architectural backing if discipline alone has been observed to fail.

## Grounding

### The pantheon-bridge era (ORG-004 Phase 2 — superseded)

`pantheon-bridge` was a Bun/HTTP/SSE transport that held a single BOT_TOKEN and served all 3 Pantheon agents (Orchestrator, Specialist, Recovery) through one Telegram identity. Inbound Telegram messages got tagged with their `chat_id`; outbound messages from each agent went out through the same shared bot, with a `from-agent` header in the daemon's internal routing layer signaling which Pantheon agent originated the message.

**The failure mode** observed across the bridge era:

- Per-agent identity-collapse on outbound edge: if the bridge's routing layer mis-attributed (a cron-fired process bypassed the header convention; an integration seam's test harness emitted without setting from-agent; a developer's local test fired with default headers) — the message went out as "the shared bot," from Telegram's view. From the user's view, all three agents looked like one agent.
- Routing table drift: the bridge's `from-agent` → tmux pane mapping had to be maintained manually (or via cron-fired sync from a config source-of-truth). Drift produced "agent X received message intended for agent Y" — a more visible failure mode than identity-collapse but same family (single-shared-resource depending on routing-discipline).
- Test surface explosion: every code path that emitted needed test coverage proving correct from-agent header attribution; every new agent required updating the routing table; every operational change needed bridge-side sync. The structural cost compounded.

Even with disciplined routing-table maintenance, the pattern was leaky. Spot-failures across the bridge era surfaced repeatedly enough to make the structural critique unavoidable: the architecture itself was making the failure mode reachable.

### The kino pivot (ORG-006 — ratified)

Per ORG-006 §Per-agent identity model: kino runs each agent as a **distinct unix user** on the VPS (claude=Orchestrator, claude-fallback=Recovery, claude-research=Specialist), each with its own home directory, its own ~/.claude state, its own systemd unit, its own GitHub deploy key, and its own outbound channel (Tailscale-tagged-node identity headers per ORG-009; per-agent OAuth posture at the daemon).

**Structural consequences**:

- "Which agent emitted this" is now a property of *which unix user the process is running as*. A misattribution would require process-tier privilege escalation — far harder to do accidentally than routing-table drift.
- Each agent has its own credentials surface. A leaked BOT_TOKEN affects one agent's outbound; doesn't compromise the others.
- Authenticated routing at the kino-daemon tier is per-user — Tailscale identity headers + per-agent bind-port arrangement.
- New agents added by adding a unix user + a kino subscription, not by maintaining a routing table.
- Test surface shrinks: agent identity is enforced at the OS, so tests don't have to prove header-attribution correctness for every emission code path.

The failure mode pantheon-bridge fought (per-agent-identity-collapse) becomes impossible-by-construction in kino. You cannot accidentally emit as "all three agents at once" because each agent's outbound runs as a distinct OS user — there is no shared write-path at the routing layer to multiplex through.

### The pivot itself (T-0091 strategic-pivot)

T-0091 was the strategic-pivot meta-ticket that ratified the move from ORG-004 Phase 2 (bridge-shaped) to ORG-006 (kino-shaped). The decision:

- **Cost of staying with bridge**: continued routing-table maintenance + per-agent-identity-collapse risk + test-surface explosion + integration friction.
- **Cost of pivoting to kino**: ground-up daemon implementation + per-agent identity infra + Tailscale tagged-node setup + ORG-006 + ORG-007 + ORG-009 ADR sequence + Phase 1-4 build arc.
- **Decision**: structural fix worth the build cost; bridge stays operational as a transitional surface (per ORG-004 §Status: stays viable for cap-hit recovery + ad-hoc messaging) but Phase-4-Telegram-retirement is the cutover where the structural advantage activates.

The decision has been validated operationally: kino's Phase 1-3 ship work (T-0107, T-0115, T-0124, T-0125, T-0126, T-0127) all landed cleanly; T-0144 Phase 2.x fan-out (T-0169 ADR ratified, T-0170 in flight, T-0171 emoji shipped) compounds. The bridge era's recurring failure pattern hasn't recurred in kino's identity surface.

## Design implications

### Identify "single shared resource multiplexed by routing discipline" as a smell

When designing or reviewing architecture: if a load-bearing identity / credential / authentication surface is shared across N agents and disambiguated by policy (a header, a routing table, a config field that selects the right agent), that's the smell. The fix is to find the OS-level / process-level / subprocess-level structural primitive that gives each agent its own identity by construction.

Concrete shapes the smell takes:
- One bot token, N agents using it.
- One database user, N services authenticating as it.
- One API key, N callers tagging themselves via header.
- One systemd unit, N consumers it serves with content discriminated by request body.
- One git remote credential, N actors pushing as it.

Each is a per-agent-identity-collapse risk waiting for the routing-discipline to slip.

### When the structural fix is too expensive, accept the trade-off explicitly + add discipline-tier guards

Sometimes per-agent OS identity isn't worth it (overhead too high; deployment shape doesn't allow; the per-agent fan-out is small enough that policy works). When that's the case, **make the trade-off explicit in the ADR** — name the per-agent-identity-collapse risk; document the discipline-tier guards (lint rule, test coverage, code-review-gate) that mitigate; set a re-evaluation trigger for when the structural fix becomes affordable. Don't let the trade-off be unwritten and subject to drift.

### Apply this pattern at every identity-multiplexing boundary

The bridge-to-kino pivot is one instance. Other places this pattern surfaces (today + soon):

- **Per-agent GitHub credentials** — each agent has its own deploy key per repo (memory-pin `feedback_github_per_repo_deploy_keys.md`); same lesson applied at the GitHub authentication boundary.
- **Per-agent Telegram bot identity** (kino's outbound-edge analogue) — Specialist, Recovery, Orchestrator each have their own Telegram bot for inbound; Specialist's outbound went via shared digest bot pre-T-0102 + got scope-tightened to TUI-only post-T-0102 because the outbound shared-identity collapse was real.
- **Per-account auto-commit machinery** — claude-research's watch_runner.py auto-commits research reports as `claude-research` user (Specialist identity); pantheon-config commits land as the actor running the script. The cron-exemption path in T-0177 (T-0156 Sub-C) is the structural recognition of this — auto-commit actors have their own identity surface, distinct from human/worker push paths.

### Tie this lesson into T-0156's structural prevention work

T-0156 (Path 1+3 ratified — branch protection + agent/<task> convention + cron-exemption) is the structural fix at the *git push identity* boundary. Same family as this lesson: discipline-tier "no self-merge" was insufficient (b23-b33 corpus); structural fix moves the enforcement to GitHub-tier branch protection + per-agent branch convention. Cross-reference: T-0156 sub-tickets T-0175 / T-0176 / T-0177 are the operational implementation; this lesson is the corpus-level pattern that motivates them.

## Open questions

- **Where's the cost-benefit tipping point between OS-tier identity and policy-tier multiplexing?** Has been answered case-by-case (bridge → kino was clearly worth it; per-agent GitHub deploy keys clearly worth it); but no general heuristic yet. Candidate framing: number of expected agents × failure cost when identity-collapse fires. Below some product threshold, policy is fine; above it, structural is required.
- **Does the lesson generalize beyond identity to other-load-bearing-multiplexed-resources?** Connection pools, rate limits, log streams, observability surfaces — same family of resource-multiplexed-by-policy pattern. May be worth a sister lesson if the corpus shows it.
- **How does this interact with the "least privilege" principle?** OS-tier per-agent identity gives least-privilege almost for free; policy-tier multiplexing tends to give shared-privilege by default. The two principles point the same direction; the relationship deserves explicit framing in some future amendment.
- **Cross-account dispatch primitives** (the ticket-as-delayed-messaging-system per the operator msg_1169 — proven end-to-end this session via T-0184) are themselves an instance of structural primitive over discipline. Worth noting: the ticket queue + per-agent inbox-consumer is the *messaging-multiplexing* analogue — each agent has its own consumer (post-T-0102), which is structural rather than policy. Same family.

## Cross-references

- **Principles + directives**: P1 (architecture over policy — direct lens), P5 (roles don't hop — adjacent; per-agent identity is what makes role-discipline structurally enforceable), P6 (operator comprehension — structurally-enforced identity is more comprehensible than policy-multiplexed identity for operator audit).
- **CLAUDE.md sections**: §Identity (Orchestrator name + role distinction; downstream of this lesson at the persona-tier).
- **ADRs**: ORG-004 (pantheon-bridge — Phase 2 superseded by this pivot's structural conclusions), ORG-006 (kino PWA — the ratified destination; §Per-agent identity model is the load-bearing structural primitive), ORG-007 (kino-claude wire protocol — operationalized by the per-agent identity), ORG-009 (kino PWA ticket-API contract — Tailscale identity headers ride on the per-agent OS-user surface).
- **Lessons**: [`instruction-fidelity-collapse-discipline-vs-architecture.md`](instruction-fidelity-collapse-discipline-vs-architecture.md) (sister; same architecture-over-policy lens at the dispatcher↔dispatchee boundary). [`org-churn-is-constant-methodology-must-be-concise.md`](org-churn-is-constant-methodology-must-be-concise.md) (cousin; structural-over-policy applies to methodology shape too — concise + indexed beats sprawling-prose-with-discipline-rules).
- **Incidents**: `2026-05-09-b33-ticket-state-sweep-multi-violation/` (instruction-fidelity collapse; same family — architecture-over-policy at the dispatcher boundary). The original pantheon-bridge per-agent-identity-collapse incidents weren't filed as formal incident reports (they happened pre-T-0028 incident-folder framework); historical ledger entries in 2026-04 / 2026-05 cover the surfacing.
- **Tickets**: T-0091 (strategic-pivot meta — bridge → kino decision), T-0102 (Specialist inbox-consumer rollout — operationalized per-agent identity at the inbox-consumer surface), T-0156 (structural prevention of self-merge — sister structural-fix in the git-push-identity boundary), T-0144 (kino PWA Phase 2.x — current arc executing on the kino structural foundation), T-0184 (auto-restart-on-decay — uses Recovery's distinct identity to fire restart against Orchestrator, same per-agent-identity-as-structural-primitive).
- **Memory pins (Recovery's auto-memory + HO's)**: `feedback_github_per_repo_deploy_keys.md` (sister application of per-resource identity at the GitHub auth surface), `project_pantheon_account_topology.md` (the three-distinct-unix-user shape that operationalizes ORG-006).
- **Telegram refs**: msg_863 (T-0158 ratification + this lesson's filing context), msg_996/997 (ADR-206 ratification — Echo's analogous pivot to pluggable-EventStore-adapters; same structural pattern at the storage-identity boundary).
- **External**: kernel-tier process isolation primitives (unix users + cgroups + namespaces) as the substrate; OAuth's "service principal per identity" pattern; AWS IAM's "role per workload" recommendation; same structural lesson, different substrates.
