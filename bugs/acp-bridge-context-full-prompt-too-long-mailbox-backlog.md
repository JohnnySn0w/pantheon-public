---
symptom: "a long-running ACP mailbox bridge loops on 'ACP error -32603: Prompt is too long' every poll and stops consuming — mailbox messages pile up unconsumed (backlog age + iteration count climbing, pane-visible); a plain restart does NOT fix it (it resumes the full session)"
fix: "systemctl --user stop pantheon-acp-bridge@<agent>, rm the persisted state file (~/.local/state/pantheon/acp-bridge-<agent>.json, backup first), then start — forces session/new (fresh empty context). NOT a raw tmux kill (systemd respawns it, resuming the full session)"
domain: "acp-bridge / mailbox-substrate"
---

## Symptom

The ACP bridge for an agent (the `acp-bridge-<agent>` tmux session, driven by `acp_bridge.py`)
stops delivering mailbox messages. The bridge **process is alive and polling**, but every
iteration logs:

```
{"agent": "<agent>", "errors": [{"error": "handler: ACP error -32603: Internal error: Prompt is too long", "id": "<msg-id>"}], "iteration": <climbing>, "processed": [], "session_id": "<full-session>", ...}
```

Messages accumulate in the agent's `inbox/new/`; the backlog-age monitor fires
(`acp-bridge-backlog` alerts — "oldest N min old, warn >30min", age growing between alerts).
The agent can still work via its **other** sessions (e.g. its Telegram-bot session) — this
breaks the **mailbox-consuming path specifically**, which is why the agent looks "alive" while
its mailbox goes dark for hours.

## Cause

The ACP wrapper session (`claude-code-acp`, one `session_id`) accumulates context across every
delivered message over its lifetime (100k+ poll iterations over days). Eventually the session
context exceeds the model limit → the handler returns **-32603 "Prompt is too long"** on the next
delivery → the message can't be consumed → it is retried every poll forever → backlog. **The
stuck message is usually NOT the problem** (a 1.5KB message triggers it) — the *accumulated
session context* is.

Two traps make the naive fix fail:

1. **A plain restart RESUMES the full session.** `acp_bridge.py _resolve_session()` persists the
   `sessionId` to a state file (`~/.local/state/pantheon/acp-bridge-<agent>.json`). On restart it
   reads that file and, if a `sessionId` is present, does **`session/load` (RESUME)** → resumes
   the same full-context session → "Prompt is too long" persists. It only takes the
   **`session/new` (FRESH)** path when there is **no persisted `sessionId`** (or the load throws).
   So a restart alone is a no-op against this failure.
2. **The bridge is systemd-managed, so a raw `tmux kill-session` just respawns it.**
   `pantheon-acp-bridge@<agent>.service` → `acp-bridge-supervise.sh` → the tmux session. Killing
   the tmux session triggers the supervisor to recreate it (resuming the full session again). The
   clean control is `systemctl`, not raw tmux.

## Fix (the state-file delete is the load-bearing step)

As the agent's user, with `XDG_RUNTIME_DIR=/run/user/$(id -u)`:

1. `systemctl --user stop pantheon-acp-bridge@<agent>.service` — stops supervisor + bridge so it
   can't re-write the state file. Confirm with a **user-scoped** check —
   `pgrep -u <account> -af 'acp_bridge\.py --agent <agent>'` — which must come back empty. Do
   **not** use a bare `pgrep -fc '<pattern>'`: it matches its own command line and any wrapper
   shell carrying the pattern, and on a shared host it matches *other agents'* bridges too. See
   the VERIFY section for the measured numbers.
2. `cp <statefile> <statefile>.bak.pre-reset && rm -f ~/.local/state/pantheon/acp-bridge-<agent>.json`
   — removes the persisted `sessionId` so `_resolve_session` has nothing to resume → forces
   `session/new`.
3. `systemctl --user start pantheon-acp-bridge@<agent>.service` — comes up on a fresh empty session.

**VERIFY** (at the defining surface, not by assumption) — corrected 2026-07-28 from a live
execution (T-2132), because the original wording here could report a *failed stop that had
actually succeeded*:

- **Primary — the log:** `errors: []`, `iteration` reset toward 0, `session_id: null` until a
  session is actually established, backlog drains to **0**.
- **State file is SECONDARY and timing-dependent.** It is written only once the bridge
  establishes a session, and it only establishes one when it has something to deliver — so right
  after a clean restart with an empty inbox it is **absent**, then later present with a new
  `sessionId`. Read **absent-or-different** as healthy; only an *unchanged* `sessionId` means the
  reset failed. (Measured: absent at +0min, `d7459558…` at +14min, versus the wedged
  `f351a1dd…` preserved in the `.bak`.)
- **Unit state is the authoritative liveness check:**
  `systemctl --user show pantheon-acp-bridge@<agent> -p ActiveState -p SubState -p MainPID`
  → expect `active` / `running` / a **changed** MainPID.
- **Process check must be user-scoped, and expects a TREE:**
  `pgrep -u <account> -af 'acp_bridge\.py --agent <agent>'` → **exactly 2** (`sh -c` + `python3`;
  the `sg veneration` wrapper runs as **root**, so `-u <account>` correctly omits it). "Exactly
  one bridge process" is the wrong invariant. Measured trap: a bare
  `pgrep -fc 'acp_bridge.py --agent specialist'` returned **5** when the truthful answer was 2, and an
  unscoped pattern showed 3 procs for specialist *and* 3 for mnemosyne on the same host. `| grep -v
  pgrep` is not a dependable self-exclusion — it works only when the checking shell's argv happens
  to contain the string `pgrep`.
- **Do NOT use `pstree -p <MainPID>`.** MainPID is `acp-bridge-supervise.sh`; the bridge tree does
  not hang beneath it (measured: `bash---sleep` while the bridge ran healthily elsewhere).

Misreading any of these as "the stop failed" invites the raw `tmux kill-session` escalation that
the Cause section explicitly warns against.

## Cost dimension — this is not only a reliability bug (added 2026-07-28, T-2135)

Measured by Specialist over 249 local transcripts (1.95B tokens) while working T-2101. The wedged
session documented here — `f351a1dd-52ac-4331-97b2-18e4ed05adfe`, the one reset per the recovery
SOP at 02:26Z on 2026-07-28 — is **the single largest token consumer in the entire corpus: 323M
tokens, 16.6% of everything**, across 5,440 assistant turns with a peak single-turn prompt of
244,297 tokens.

Cost is extremely concentrated: **5 sessions = 62.9%** of all tokens, 25 sessions = 89.8%. And it
is **superlinear in turn count**, which is the mechanism this bug describes: accumulated context
means every further turn re-reads a prefix that has grown toward the ceiling, so cache-read per
turn climbs **8.3×** (17.9K at turns 0–9 → 149.5K at turn 100+), and cache-read is **90.7%** of all
tokens fleet-wide.

**Consequence for how to read this doc:** the session-recycling step in
`sop/remediation/acp-bridge-recovery.md` is a **cost control as well as a recovery step**. A bridge
that has not wedged *yet* is still accruing this curve, so recycling a long-lived bridge session is
defensible on cost grounds before the -32603 loop ever appears — the wedge is the visible end of a
gradient, not a discrete event.

Caveat, stated because it changes what the number means: the fleet is on a flat-rate Max
subscription, so dollar figures would be notional. Tokens are the accurate primitive; treat this as
consumption concentration, not spend. Method + full numbers:
`knowledge/episodic/2026-07-28-dynamic-turn-limit/report.md`.

## Detection note

An idle-but-full-context bridge is **pane-visible** (the -32603 loop in the tmux pane) but can be
**jsonl-silent** to a detector keyed on `isApiErrorMessage` from *active turns* — so a
model-cap-style auto-detector may not fire. The **backlog-age monitor** (`acp-bridge-backlog`,
"oldest N min unconsumed") is the reliable signal; it catches the pile-up regardless of turn
activity. (Same pane-visible/jsonl-silent shape as the idle spend-cap wedge.)

## Related

- **Work survives the reset** — only the ACP session's *conversational* context is lost; the
  agent's committed work + memory-dir captures are intact. Confirm work is committed before
  resetting (it should always be).
- **Credit:** Recovery diagnosed `_resolve_session` (the state-file resume) 2026-07-26; Orchestrator executed
  + verified under the operator-escalated multi-system incident. Full narrative: ledger `2026-W30`
  (`mnemosyne-bridge-incident-resolved`). The operational procedure (systemctl-not-tmux +
  state-file-delete) also belongs in Recovery's bridge runbook.
- Sibling "restart resumes the wrong state" shape:
  `supervisor-launch-flag-outranks-settings-json-on-model-pin.md`.
