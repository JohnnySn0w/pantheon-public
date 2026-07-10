# Telegram plugin MCP drop — comms-dark while session healthy

**Status:** Draft 2026-06-06 (operator-requested in-incident, msg 2705 — he provided the canonical remediation; pending formal ratification per the sop/remediation per-SOP ratification rule). Author: Recovery. Filed from [`incidents/2026-06-06-orchestrator-telegram-plugin-drop`](../../incidents/2026-06-06-orchestrator-telegram-plugin-drop/README.md).

How to recover an agent (Orchestrator/Specialist/Recovery) whose **Telegram channel plugin dropped its MCP connection mid-session** — so its reply tool + inbound go dark — **while the session itself stays healthy.** Audience: Recovery (recovery domain), or whoever is recovering an agent.

## Failure shape

The agent is **comms-dark but not wedged**: the `claude --channels plugin:telegram@claude-plugins-official` session keeps processing turns, the pane is healthy/idle, `systemctl is-active` is `active` — but the Telegram MCP server (a child of the claude process, providing `mcp__plugin_telegram_telegram__reply` + inbound) has lost its connection. The operator can neither reach nor be reached by the agent.

**Why the standard detectors miss it:** wedge/staleness (T-0857) needs a *frozen pane*; verify_startup checks *pane* health. Both read session-liveness, which is green. This is a *channel-layer* failure — `is-active`/pane-healthy is **not** a comms-health signal (sibling to the 2026-05-21 alive-but-erroring mode). The agent's only intact comms path is the **ticket queue** — expect it to self-report by filing a target=recovery ticket.

**Key architecture fact:** Telegram is a Claude Code *plugin baked into the session*, not a separate service. There is **no separate bridge to bounce** — the MCP server lives inside the claude process. So the only fixes are in-session reconnect (not agent-drivable) or a client restart.

## Recommended remediation (ranked)

**0. Assess read-only first + bridge the comms gap.** `tmux capture-pane` (confirm session healthy/idle, not wedged) + `systemctl --user is-active`. Then **relay the agent's status to the operator on its behalf** ("X is up + working, telegram plugin dropped, recovering") — the operator may be waiting and can't hear the agent.

**1. (Did NOT work) In-session `/mcp` reconnect — context-preserving but not agent-drivable.** `/mcp` opens an interactive TUI modal that an autonomous agent cannot navigate. An *operator* could drive it via send-keys, but blind-driving a TUI on a large-context live session is high-risk — **do not** attempt on the prime unless you can see + drive each step and the downside (fumble → fall to restart) is acceptable.

**2. (Canonical fix — the operator, msg 2705) Restart the client, then `/resume`.** Restarting respawns the plugin with a fresh MCP connection; `/resume` reloads the prior session's full context — recovers MCP *without* losing context.
   - **Restart = `tmux kill-session -t <session>`**, NOT `systemctl restart`. The supervisor (`*-bot-supervise.sh`) blocks while the session lives and exits when it dies, so `Restart=always` respawns a fresh client. `systemctl restart` only cycles the *supervisor*; the inner claude survives (idempotent `tmux has-session` guard) → **no-op**. **Verify the inner-claude PID changed** (`pgrep -u <user> -f 'claude --channels'`) — that is the only proof of a real restart ([[reference_handoff_restart_systemctl_no_op]]).
   - **Then `/resume` → pick the most-recent (top) session** to restore context.
   - ⚠️ **`/resume` RACE CAVEAT (2026-06-06):** the fresh session does **not** sit idle waiting for `/resume`. The SessionStart hook auto-injects a HANDOFF read, and Telegram immediately delivers any **queued inbound** — so the fresh session engages in live work within the first beat. To use `/resume` cleanly you must catch it *before* that. If the session has already started handling live operator requests (as happened here — it ran a device-auth flow the operator asked for), **do NOT `/resume`** — switching sessions orphans the in-progress work. Cancel (`Esc`) and accept the cold-boot.

**3. (Acceptable fallback) HANDOFF cold-boot.** If `/resume` is preempted (per the caveat), the restart alone still recovers the MCP, and the fresh session cold-boots from HANDOFF (auto-read by the SessionStart hook). Provided HANDOFF is current (the agent should keep it so — and update it on noticing the drop), the agent recovers enough context to proceed correctly. This is what actually happened in the founding incident and it was sufficient.

## Hard don'ts (token-landmine guard)

- **Never** spawn a second `claude` / `claude --bare` / anything that grabs the Telegram token — it steals the token and kills the *live* bridge ([[feedback_no_second_claude_with_telegram_plugin]]).
- **Never** `systemctl restart` and trust exit-zero / `is-active` as proof — verify by PID change.
- After any send-keys to the agent's pane, **confirm its health** (`is-active` + capture-pane) and state it ([[feedback_confirm_health_after_touching_live_sessions]]).

## Autonomous-vs-bubble-up D3 placement

- **Autonomous (Recovery recovery domain):** read-only assessment, relaying the agent's status to the operator, and — once the in-session reconnect is shown non-viable — the client restart itself is pre-authorizable by the affected agent (it owns its context; if it says "HANDOFF current, restart OK as last resort," that is consent).
- **Bubble up:** when the only remaining fix is a **context-destroying restart of the prime** and it is **not urgent** (agent idle, comms bridged by relay) and the operator is reachable — surface the call (the irreversible-ish prong). In the founding incident Recovery surfaced; the operator both approved and supplied the `/resume` method.

## Cross-references

- Incident: [`incidents/2026-06-06-orchestrator-telegram-plugin-drop`](../../incidents/2026-06-06-orchestrator-telegram-plugin-drop/README.md).
- Sibling SOPs: [`recovery-service-recovery.md`](recovery-service-recovery.md), [`persona-daemon-liveness-recovery.md`](persona-daemon-liveness-recovery.md) (process-exit / inert-daemon modes — distinct from this channel-drop mode).
- Memory: [[reference_handoff_restart_systemctl_no_op]], [[reference_telegram_agent_setup]], [[feedback_no_second_claude_with_telegram_plugin]], [[feedback_confirm_health_after_touching_live_sessions]].
- Detection gap (no auto-detect for channel-down-while-session-healthy): follow-up ticket off T-0893.
