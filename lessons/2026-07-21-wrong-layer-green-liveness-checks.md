# Wrong-layer GREEN — liveness checks must verify the capability, not a proxy layer

**Date authored:** 2026-07-21 (T-1914, ORG-012 sweep promotion — criterion 1, five verified instances)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a sixth instance lands in a layer not enumerated below (extend the enumeration); or when a monitor-authoring SOP/checklist ships that structurally forces capability-probes (then this lesson demotes to a pointer at that SOP).

## The pattern

A health or liveness check verifies a **proxy layer** — the layer that is easy to check — while the **capability** the check exists to guarantee is dead. The monitored surface reads GREEN precisely because the check is aimed at the wrong stratum. The failure is invisible for hours-to-days and is usually detected by a human noticing silence, not by the monitor.

The general form: *"X is running" is not "X can do its job."* Every instance below had a technically-true GREEN and a dead capability underneath it.

## Verified instances (the layers)

1. **Process layer** — `incidents/2026-05-21-recovery-personal-daemon-silent-outage/`: `systemctl is-active` reported `active (exited)` (oneshot semantics) while the spawned tmux+claude process was gone. ~22h silent; detected by the operator, not a monitor. Check saw the unit; capability was the agent session.
2. **Channel layer** — `incidents/2026-06-06-orchestrator-telegram-plugin-drop/`: session and pane fully healthy while the telegram MCP plugin had dropped — operator comms dark for several turns. Check-equivalent (session liveness) saw the process; capability was the comms channel. The incident README already named it "sibling shape … same lesson, different layer."
3. **Auth layer** — `incidents/2026-07-20-orchestrator-oauth-refresh-rejection/`: process-GREEN and channel-GREEN while every model call failed auth for ~8h47m — a rejected OAuth refresh had blanked the credential file. The two layers above were healthy; the capability (make a model call) was dead.
4. **Session-registry layer (tool surface)** — T-1908 (Atlas, 2026-07-21): `claude mcp list` printed `Connected` while the running seat sessions held a permanent zero-tools state. The CLI check spawns a *fresh* process and proves the config resolves — it says nothing about the registry of a session that started before the MCP dependency existed (stdio MCP connects once at startup, no retry). GREEN from the diagnostic tool itself, dead capability in the live session.
5. **Wiring layer (installed-but-dead)** — T-1904/T-1928 (2026-07-21): liveness/alert units *installed* from the repo template on every account, but the `OnFailure=` hook was wired only on the `claude` account (T-1869 account-local fix) — 23 oneshot templates on the other accounts would fail without ever firing an alert. "The template is installed" was the GREEN; "a failure produces an alert" was the capability, and it existed on one account of five. (Closed fleet-wide same day: T-1928 wired 23 templates + reconciled installed copies on all 7 accounts, verified by a real transient-fail → alert-landed smoke — a capability-probe, the correct verification layer.)

## The rule

**A liveness check must probe the capability end-to-end, or explicitly name the layer it verifies and the layers it does not.**

- Prefer capability-probes: send a real (marked) message through the channel, make a real cheap model call, invoke a real tool in the live session, `systemctl kill --signal=` a canary unit and assert the alert arrives. If the probe is too expensive to run continuously, run it at arm-time and after every repair, and let the continuous check be the cheap proxy *with its layer named*.
- When only a proxy check is feasible, write the enumeration down next to it: "this verifies process, NOT channel/auth/tools/wiring." An unnamed proxy check *becomes* the capability check in every reader's head — that is the mechanism of this whole class.
- At repair/bringup time, verify at the layer that failed, not the layer that's convenient: after an MCP repair, list tools *inside the running session*; after an auth repair, make a model call; after wiring an alert path, fire it once. ("Restart the seat" fixed instance 4; no restart fixes instance 3 — the repair-verification layer differs per instance, which is itself evidence the layers are real.)
- Sibling memory pins, same discipline at report-time: "verified healthy" needs exhaustive enumeration, and negative claims need the success-path signature — a curated subset of GREENs is this pattern in report form.

## Layer enumeration (checklist form)

For any agent-fleet monitor, ask which of these the check actually exercises: **process** (unit/pid up) → **channel** (comms in/out flow) → **auth** (credentials accepted by the remote service) → **session-registry** (tools/MCP live in the *running* session, not a fresh probe process) → **wiring** (failure paths actually connected: OnFailure, hooks, timers) → **data** (the thing it writes is landing and fresh). GREEN at layer N proves nothing about N+1.
