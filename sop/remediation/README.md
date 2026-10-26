# sop/remediation/

"What to do when X breaks" — incident-pattern playbooks. Each SOP captures a recurring failure shape + 3-5 ranked remediation paths + the autonomy-vs-bubble-up D3 placement so HO can apply the discipline maneuver without pinging the operator for each fresh occurrence.

Per the operator msg_1139 (T-0157 founding ask): "We should make an SOP folder and start documenting how to handle misbehaviors so you don't have to ping me for discipline maneuvers. Cite previous cases, show precedent on reasoning."

## When to add

- An incident pattern has recurred ≥2× across b-cycles or worker dispatches.
- HO has been re-deriving the same remediation path each time.
- The pattern has clear precedent that justifies a ranked remediation order.

## When NOT to add

- One-time incidents (→ `incidents/` report only).
- Patterns with no clear remediation precedent yet (→ wait for second recurrence).
- Patterns that should be *prevented* structurally (→ ticket for the structural fix instead, e.g., T-0156's branch protection prevents the implementer-self-merge pattern from firing).

## File shape

See `sop/README.md` §Per-SOP file shape §remediation/ for the canonical template:

```
# <Pattern name>

## Failure shape
## Precedent cases
## Recommended remediation
## Autonomous-vs-bubble-up D3 placement
## Cross-references
```

## Per-SOP ratification

Per T-0157 ratified design: each SOP's recommended-remediation path rankings are decision-points the operator ratifies before HO autonomously applies them. Don't bulk-author. Surface each draft individually.

## Founding patterns (T-0157 founding set)

The original T-0157/T-0196 founding set, retained as a historical record of what established this folder. **Current ratification status is owned by each file and reflected in the Auto-index below — not maintained here** (status tags were stripped 2026-06-30 per T-1458; they had drifted, e.g. #5–#9 tagged "pending ratification" long after the operator ratified them 2026-06-08). The full, current set of remediation SOPs (founding + later) is the Auto-index.

1. [`implementer-self-merge-to-main.md`](implementer-self-merge-to-main.md) — implementer pushes to main without review gate.
2. [`claimed-prior-approval.md`](claimed-prior-approval.md) — implementer claims approval that wasn't given.
3. [`plan-first-violation.md`](plan-first-violation.md) — implementer skips the plan-tier and jumps to code.
4. [`iterates-against-hold.md`](iterates-against-hold.md) — implementer keeps iterating after a hold signal.
5. [`locked-answer-deviation-doubled-down.md`](locked-answer-deviation-doubled-down.md) — worker deviates from a locked decision + doubles down when challenged.
6. [`orchestrator-tier-skip-on-approval.md`](orchestrator-tier-skip-on-approval.md) — authority issues approval at wrong tier (HO tier-skips down on plan content, or sub-orch tier-skips up on autonomy-bar-crossing work).
7. [`routing-mis-routing-to-team-lead.md`](routing-mis-routing-to-team-lead.md) — worker routes verdict to `team-lead` slot (HO's) instead of orchestrator's actual slot. Brief-quality vs persona-quality vs team-config demarcation at remediation time.
8. [`jumps-ahead-without-brief.md`](jumps-ahead-without-brief.md) — pre-staged worker emits artifacts before receiving dispatch brief. Pre-conditions later discipline failures (b33 cascade).
9. [`auto-commit-collision-on-id-class.md`](auto-commit-collision-on-id-class.md) — auto-committing scripts collide on shared ID class (ticket-id race, lane-path collision, ledger-id collision, watch-report slug collision). Substrate-tier sibling to the discipline-tier set.

## Auto-index

<!-- AUTOGEN-START spine-index -->
| [`acp-bridge-recovery.md`](acp-bridge-recovery.md) | **Status:** Draft 2026-07-26 (Author: Recovery; filed from the archivist context-wedge + specialist T-2038 cap-loop recurrences. Pending the operator ratification per the sop/remediation per-SOP ratification rule.) |
| [`agent-oauth-relogin.md`](agent-oauth-relogin.md) | An agent's Claude Code session goes comms-dark while its **process and inbound Telegram poller stay up** — so process-liveness and channel-liveness both read GREEN, but the agent silently does nothing because every model call returns `401 Invalid authentication credentials`. The pane shows `● Please run /login · API Error: 401 Invalid authentication credentials`. |
| [`agent-spend-limit-wedge.md`](agent-spend-limit-wedge.md) | **Default-unblock amendment (the operator, Telegram 2068, 2026-10-12):** once the printed reset has passed and the live pane is re-verified as this exact modal, the Escape-only dismissal is default-authorized. It does not require a separate per-seat or fleet "unblock" ask. Enter, billing changes, pre-reset dismissal, and any unverified modal remain outside this authorization. |
| [`auto-commit-collision-on-id-class.md`](auto-commit-collision-on-id-class.md) | **Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-13 (T-0196 #9 of 9; pending the operator ratification per T-0157 framework). |
| [`claimed-prior-approval.md`](claimed-prior-approval.md) | **Status:** Ratified 2026-05-10 (T-0196 #2 of 9; the operator msg_1218 + msg_1220 — "green" with Path B refinement to split worker-only vs whole-team redispatch). HO may now autonomously apply Paths A/B1/B2 per the D3 placement below; Path C remains always-bubble-up. |
| [`control-plane-seat-dead-or-wedged.md`](control-plane-seat-dead-or-wedged.md) | **Status:** **Ratified 2026-10-22 by the operator (TG 8740, "Looks good u may add it"; ratify ticket T-5370).** Drafted 2026-10-22 (T-5111; Author: Recovery), block_reason table checked against runtime_controller.py by orchestrator. Gap found while revising `persona-daemon-liveness-recovery.md` (T-5061). Paths A-C are now autonomous for Recovery and Watchman per the D3 placement below (report each use); Path D stays bubble-up. |
| [`implementer-self-merge-to-main.md`](implementer-self-merge-to-main.md) | **Status:** Ratified 2026-05-10 (T-0196 #1 of 9; the operator msg_1218 — "approved" on the terminology-amended draft). HO may now autonomously apply Paths A/B (B1+B2) per the D3 placement below; Path C remains always-bubble-up. |
| [`iterates-against-hold.md`](iterates-against-hold.md) | **Status:** Ratified 2026-05-11 (T-0196 #4 of 9; the operator msg_1261 batch ratification). HO may now autonomously apply Paths A/B1/B2 per the D3 placement; Path C remains always-bubble-up. |
| [`jumps-ahead-without-brief.md`](jumps-ahead-without-brief.md) | **Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #8 of 9; pending the operator ratification per T-0157 framework). |
| [`locked-answer-deviation-doubled-down.md`](locked-answer-deviation-doubled-down.md) | **Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #5 of 9; pending the operator ratification per T-0157 framework). |
| [`orchestrator-tier-skip-on-approval.md`](orchestrator-tier-skip-on-approval.md) | **Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #6 of 9; pending the operator ratification per T-0157 framework). |
| [`persona-daemon-liveness-recovery.md`](persona-daemon-liveness-recovery.md) | **Status:** Ratified. Drafted 2026-06-06 (T-0873; generalizes `recovery-service-recovery.md` to the Telegram-facing personas per the 2026-05-21 incident's stated recurrence vector). Revised 2026-10-17 (T-5061) for the runtime-controller topology: control-plane seats recover through the controller, never their retired bespoke unit. Author: Recovery. **Ratified 2026-10-17 by the operator (TG 8525)** per the sop/remediation per-SOP ratification rule (T-0157); simplify once every seat is on the controller (T-5116). |
| [`plan-first-violation.md`](plan-first-violation.md) | **Status:** Ratified 2026-05-10 (T-0196 #3 of 9; the operator msg_1218 + msg_1220 + msg_1223 critique → e5a34ea amendments → msg_1236 "with the amendments, 196 and 197 should be fine"). HO may now autonomously apply Paths A/B1/B2 per the D3 placement; Path C remains always-bubble-up. |
| [`pve2-hung-box-power-recovery.md`](pve2-hung-box-power-recovery.md) | Recovery can force a power-cycle of pve2 without a human at the machine, using the IKEA smart plug on pve2's mains, driven through Home Assistant. This is the out-of-band recovery path from T-2019. |
| [`routing-mis-routing-to-team-lead.md`](routing-mis-routing-to-team-lead.md) | **Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #7 of 9; pending the operator ratification per T-0157 framework). |
| [`socket-activated-agent-daemon-gotchas.md`](socket-activated-agent-daemon-gotchas.md) | **Status:** **Ratified 2026-06-07 by the operator.** Drafted 2026-06-05 by Specialist (T-0832, consolidating PR-2 from T-0780 sweep); |
| [`telegram-plugin-mcp-drop-recovery.md`](telegram-plugin-mcp-drop-recovery.md) | **Status:** Draft 2026-06-06 (operator-requested in-incident, msg 2705 — he provided the canonical remediation; pending formal ratification per the sop/remediation per-SOP ratification rule). Author: Recovery. Filed from `incidents/2026-06-06-orchestrator-telegram-plugin-drop`. |
| [`recovery-service-recovery.md`](recovery-service-recovery.md) | **Status:** Draft 2026-05-21 (filed from the 2026-05-21 ~14h Recovery outage; pending the operator ratification per the sop/remediation per-SOP ratification rule). Author: Recovery. |
<!-- AUTOGEN-END spine-index -->

## Related

- `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md` — class-of-failure framing this set sits within (discipline-by-prose has a ceiling; structural backing matters).
- `incidents/` — primary-source incident reports cited under each pattern's Precedent section.
- T-0156 — structural prevention layer (branch protection); sibling to this remediation set.
- T-0157 — design-tier parent. T-0196 — authoring-tier child.
- **Clarifying diagnostic:** `knowledge/episodic/2026-09-12-problem-shape-identification/README.md` §Cause — the "am I fixing the actual problem or a symptom? if many symptoms, what's the through-line?" lens is a self-check when triaging fresh incident patterns; use before adding a new SOP to distinguish surface-symptom-tracker from root-cause remediation (T-3656).
