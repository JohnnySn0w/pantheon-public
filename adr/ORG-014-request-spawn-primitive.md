---
aliases: [ORG-014]
title: RequestSpawn primitive (Shape 2 / b33 followup)
status: Superseded 2026-08-10 by ORG-037 (per the operator, Telegram) — ORG-037 (team-coordination liveness, built) automates the same silent-dispatchee failure; the M2 manual RequestSpawn primitive was never built and is redundant. Was Ratified 2026-05-17.
date: 2026-05-17
---
# ORG-014 — RequestSpawn primitive (Shape 2 / b33 followup)

**Status:** **SUPERSEDED 2026-08-10 by ORG-037** (per the operator, Telegram — on the supersede recommendation from the 2026-08-10 ratified-ADR audit). ORG-037 (team-coordination liveness, Ratified 2026-06-26, fully built) handles the same silent-dispatchee failure with a strictly-better AUTOMATIC mechanism (self-wakeup watchdog + bilateral self-checks); ORG-014's manual `tools/request_spawn.py` primitive was never built and is redundant (no consumer exists). Original ratification preserved for the record below.

**Originally Authored + Ratified 2026-05-17** by the operator (Telegram msg_1615 "I do approve of the reasoning, convert and ratify"). Promoted from `notes/2026-05-17-request-spawn-primitive-design.md` (drafted earlier same day) per msg_1614 verdict — "notes should not be submitted as ratifiable documents; this should be an adr tbh." Documented the M2 mechanism choice as binding architectural direction; implementation was gated on a downstream implementer dispatch (HO-direct or sub-ticket) that was never filed (T-0398 resolve) — the gap the 2026-08-10 audit surfaced.

**Date authored:** 2026-05-17

**Author:** Orchestrator (Orchestrator) — drafted under T-0174 disposition retarget (claude-fallback msg_1422, 2026-05-17T00:39Z) per HO-drafts-the-ADR shape from T-0156 fanout.

**Related:** [[T-0174]] (HO drafting ticket, done), [[T-0398]] (ratification ticket, done), [[T-0156]] (parent fan-out: Shape 1 self-merge prevention sibling), b33 incident report (`incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/README.md` §92-94 Structural-prevention thinking — Shape 2).

**HITL:** Authored + Ratified

## Context

Lifted verbatim from the b33 incident postmortem (§Shape 2 candidate paths analysis). Shape 2 is **orchestrator-blocked-on-silent-implementer**: structurally distinct from Shape 1 (worker-bypassing-rules, which T-0156 Path 1+3 ratified addresses via pre-stage worker pool + heartbeat). Both shapes are addressable only by P1-architectural change — not by tightening the brief further.

## Problem

b33 (2026-05-09) was the first ~6-hour-class implementer stall in the org. `implementer-fresh` did not produce a deliverable between 22:36Z and 01:55Z (next morning). No errors, no narrative, no progress signal. The project-orchestrator had **no structural primitive to surface "my dispatchee has stopped responding"** — its only available channel was a narrative SendMessage to HO, which arrives in HO's regular inbox and competes with active work.

The b33 postmortem named this **Shape 2**: orchestrator-blocked-on-silent-implementer. Distinct from Shape 1 (worker-bypassing-rules), which the T-0156 Path 1+3 ratified fan-out (pre-stage worker pool + heartbeat) already addresses. Shape 2 is a separate architectural surface: structural-vs-narrative signal-routing for orchestrator-tier stalls.

Class of failure: **discipline-by-prose without architectural backing leaves multiple bypass surfaces** (b33 postmortem §17). Both shapes are addressable only by P1-architectural change — not by tightening the brief further.

## Goal

Give project-orchestrator (and any future orchestrator-tier persona) a tool call to signal *"I am blocked because my dispatchee stopped responding; I need a fresh worker."* That signal must:

1. **Be a structured tool call, not narrative prose.** A tool call surfaces in HO's tool-use history as a discrete event; narrative gets buried in the chat stream.
2. **Route to HO with priority.** Orchestrator stalls are time-sensitive — they're already-burning compute hours. They should not sit behind regular inbox work.
3. **Carry minimum sufficient context.** Reason, prior-implementer-status, suggested-disposition. Enough for HO to decide without re-reading the orchestrator's full transcript.
4. **Be cheap to invoke.** Orchestrators should not weigh "is this worth a RequestSpawn call?" — that's the P7 cost-asymmetry move; the deliberation tax is the thing we're trying to remove.

## Candidate mechanisms (M1 / M2 / M3)

These three were surfaced in T-0174's transition log (HO disposition note 2026-05-10T06:04Z) and have not changed since. Restating for ratification context:

### M1 — Custom MCP tool

Server-side process hosts the queue + priority routing. Project-orchestrator persona gets `request_spawn` MCP tool exposed via `tools:` frontmatter. Tool call lands at the MCP server, server routes to HO's inbox with priority flag.

**Pros**
- Cleanest persona-tier abstraction. Tool is first-class; orchestrator calls it like any other tool.
- Server-side queue lets HO see RequestSpawn calls as a separate stream from regular inbox.
- Naturally extends to future orchestrator-tier primitives (RequestPause, RequestEscalate, etc.).

**Cons**
- New infra to write + maintain (MCP server process, deployment, lifecycle).
- Adds a Telegram-bridge-class component to the substrate. Currently three: Telegram bridge, ticket queue, watch loop. A fourth costs maintenance attention.
- Failure modes (server down, queue stuck) become new ops surface.

### M2 — `tools/request_spawn.py` invoked via Bash tool

Project-orchestrator persona instructs `Bash tool: python3 /home/claude/pantheon-config/tools/request_spawn.py --reason "..." --prior-status "..." --suggested "..."`. The script emits to HO's tmux pane via `tmux send-keys` (mirrors the existing `inbox_consumer.py` pattern). HO receives the request as a delivered message in HO's input queue, prefixed with a recognizable marker (`[request-spawn]`).

**Pros**
- Lightest infra. One script, one CLI surface, one delivery mechanism — all mirroring well-tested existing patterns (`inbox_consumer.py`, `ticket.py`).
- Scriptable + testable. CLI flags = clear contract; can be smoke-tested with stubbed delivery.
- Survives all existing failure modes already handled by the inbox-consumer pattern (HO pane busy, tmux flaky, etc.). No new ops surface.
- Persona-tier change is minimal: one paragraph in `project-orchestrator.md` naming the script + when to call it.

**Cons**
- Bash-tool layer adds an indirection vs. a first-class tool. Orchestrator must invoke via shell rather than direct tool call.
- No structured queue: HO sees the message inline with other inbox traffic, distinguished only by the `[request-spawn]` marker.
- Priority routing is conventional, not structural. HO is trusted to recognize the marker + treat the call as high-priority.

### M3 — Hook + reserved subject-line marker

Project-orchestrator emits a SendMessage with reserved subject prefix (`REQUEST_SPAWN: …`). A SessionStart-style hook on HO's pane scans incoming SendMessage subjects + auto-prioritizes those matching the prefix (move to top of queue, optional Telegram-notify).

**Pros**
- Smallest persona-tier change. Project-orchestrator already uses SendMessage; this is just a subject-prefix convention.
- No new script / no new server. Existing SendMessage delivery handles transport.
- Hook is configurable on HO's side only (no cross-instance coordination).

**Cons**
- SendMessage is project-orchestrator → HO direct already; the only thing this adds is the priority routing on HO's side. The "structural" character is weakest of the three — it's still narrative-shape with a magic-string convention.
- Hook discipline: relies on the hook being correctly installed + maintained on HO's pane across session restarts. Fragile vs. server-side queue (M1) or script-CLI surface (M2).
- The convention is invisible to anyone reading the SendMessage transcript later (the subject prefix only matters at delivery time).

## Recommendation — M2

HO lean since 2026-05-10 disposition note, unchanged. Reasoning:

- **Mirrors a working pattern.** `inbox_consumer.py` already does priority routing into HO's pane via `tmux send-keys`; the substrate is proven. M2 is one more script in the same family.
- **Smallest new ops surface.** M1 adds an MCP server; M2 adds a Python file in `tools/`. Cost asymmetry favors M2 by an order of magnitude (P7 lean: cheap action over deliberation; this is a cheap mechanism vs. an expensive one).
- **Testable.** CLI flags + stdout/stderr + an integration test that captures the tmux send-keys call. No mocking-the-network or daemon-state-fakery needed.
- **Easy to retire / replace.** If M1 turns out to be needed later (e.g., for cross-instance orchestrator-tier signals once Echo or revenue-project resume), M2 doesn't paint us into a corner — the script can be deprecated and the persona-tier paragraph updated. M1 would lock infra commitments earlier.

**Trade accepted with M2:** priority routing is convention-via-marker, not structurally enforced. If the marker discipline drifts (HO ignores `[request-spawn]` for some reason), the architectural protection is weaker than M1 would provide. This is acceptable as a v0 because (a) Shape 2 has not yet recurred — frequency unknown until counterfactual data accumulates (b33 postmortem §71); (b) the marker is highly visible in HO's pane; (c) we can promote M2 → M1 later with negligible cost if frequency justifies it.

## Implementation sketch (M2)

### CLI contract

```
python3 tools/request_spawn.py \
  --reason "implementer-fresh silent ~3h post-APPROVE; no progress signal" \
  --prior-implementer-status "claimed T-0220; APPROVE'd plan 22:31Z; no Phase-2 commit since" \
  --suggested-disposition "kill silent dispatchee, re-dispatch fresh implementer with same plan + worker-isolation guard"
```

All three flags required. `--reason`, `--prior-implementer-status`, `--suggested-disposition`. Free-form strings; the script normalizes whitespace + caps at ~500 chars each.

### Delivery

Script invokes `tmux send-keys -t <ho-pane> '[request-spawn] <reason>\n<prior-implementer-status>\n<suggested-disposition>' Enter`. Pane target is configurable via env var or `~/.local/state/pantheon/ho_pane_target` (mirrors how `inbox_consumer.py` discovers HO's pane today).

Optional: also append a structured log line to `~/.local/log/pantheon/request_spawn.log` for audit-chain replay.

### Persona-tier change

One new paragraph in `claude-config/agents/project-orchestrator.md`, near the existing dispatch/escalation guidance:

> **When a dispatchee stops responding** (no progress signal beyond your role-threshold — implementer 15min, reviewer 5min), do NOT bubble up narratively. Run `python3 /home/claude/pantheon-config/tools/request_spawn.py --reason ... --prior-implementer-status ... --suggested-disposition ...`. This routes structurally to HO with priority. Continue your own work or hold; HO will respond.

### Test

Single integration test in `tools/test_request_spawn.py`:
1. Invoke with valid flags; assert script succeeds + writes expected log line.
2. Invoke with missing required flag; assert script exits non-zero with helpful message.
3. Invoke with mocked tmux binary; assert send-keys arguments match expected pattern.

No mocking of HO's pane state — the integration boundary is "send-keys was called correctly," not "HO actually saw it." That's the substrate guarantee.

### Estimated scope

~80-120 LoC for the script (CLI parsing, normalize-clamp, tmux invocation, log append) + ~40-60 LoC for the test + persona-brief paragraph. Single implementer dispatch, plan-first, 2-3 day calendar.

## Tensions / open questions

**T1 — How does HO actually treat `[request-spawn]` messages?**
Convention says "priority routing." Operationally that means HO interrupts current work to triage the RequestSpawn. Is that the right call always, or are there cases (mid-dispatch of unrelated work) where HO defers? The discipline lives in HO's pinned rules. Suggestion: add a §"RequestSpawn handling" section to `/home/claude/CLAUDE.md` once M2 ships, naming the interrupt-discipline explicitly.

**T2 — Does this open a noise-channel?**
If project-orchestrators over-fire RequestSpawn (e.g., on the 30-min implementer threshold when the implementer was just doing a long compile), HO's pane fills with low-value interrupts. Mitigation: persona-brief sets a higher bar than the role-threshold ("only fire when you've waited beyond 2x the threshold AND have a concrete reason to believe the worker is genuinely stalled, not just slow"). Tunable.

**T3 — Cross-instance scope?**
M2 only routes within the HO pane on the claude account. If a future orchestrator on Specialist (claude-research) or echo-orchestrator persona needs to RequestSpawn into HO, M2 doesn't handle it. Out of scope for v0 — addressable later via M1 promotion or a sidecar bridge.

## Decision (ratified)

**M2 ratified by the operator Telegram msg_1615 (2026-05-17, 15:38Z).** Ratification was on the reasoning + mechanism choice; the same message directed promotion from `notes/` to `adr/` (msg_1614 — "this should be an adr tbh; notes should not be submitted as ratifiable documents"), which this ADR captures.

Implementation path (next):
- HO dispatches implementer (plan-first, worktree pre-staged per T-0357, on `agent/<followup-ticket-id>` per T-0176 convention).
- Implementer ships `tools/request_spawn.py` + test + persona-brief paragraph in `claude-config/agents/project-orchestrator.md`.
- Reviewer APPROVE → HO opens PR + merges via admin bypass (or HO deploy key once T-0399 ratifies) → resolves followup ticket.
- T-0398 (ratification ticket) and T-0174 (draft-task ticket) both close on this ADR landing.

Open items T1 (HO interrupt-discipline on `[request-spawn]` messages), T2 (noise-channel mitigation thresholds), T3 (cross-instance scope deferral) carry forward as their own follow-up tickets if/when implementation surfaces concrete need.
