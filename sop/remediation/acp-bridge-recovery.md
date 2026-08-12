# ACP mailbox-bridge wedge / loop recovery

**Status:** Draft 2026-07-26 (Author: Recovery; filed from the mnemosyne context-wedge + specialist T-2038 cap-loop recurrences. Pending the operator ratification per the sop/remediation per-SOP ratification rule.)

**Amendment adopted 2026-07-28 (Recovery, T-2132)** — verification-step corrections only; the remediation itself was executed verbatim on specialist and worked as written. Added the run-on-your-own-seat pre-flight (transcript mtime as the am-I-resetting-myself discriminator), and rewrote the verify step after measuring that its process check could report a *failed stop that had actually succeeded*. Amendment tier is below the goal-level bar per amended D3, so it is adopted rather than held for a stamp; the base SOP's ratification status above is unchanged by it.

The `pantheon-acp-bridge@<agent>.service` bridge (`tools/acp_bridge.py`) connects a
seat/keeper (specialist, mnemosyne, …) to its mailbox over a persistent ACP wrapper
session. Two distinct failure shapes recur; both look like "the bridge is alive but
messages aren't getting through."

## Failure shape

**Class A — session-context wedge ("Prompt is too long").** The persistent ACP
wrapper session context fills up, so every poll re-attempts the stuck message and
returns `ACP error -32603: Internal error: Prompt is too long`. The *message* isn't
too long — the *accumulated session context* is (mnemosyne hit this at wrapper
iteration ~101k, 2026-07-26). A plain restart does NOT clear it: `_resolve_session()`
persists a `sessionId` to a state file and does `session/load` (RESUME) on restart,
so it re-loads the same full-context session and re-wedges.

**Class B — environmental-failure loop (usage cap / rate limit).** During a usage
cap the wrapper returns `-32603 "You've hit your limit · resets <time>"` for *every*
`session/prompt`. Pre-T-2038 this dead-lettered innocent messages and each quarantine
spawned a quarantine-exempt `mailbox-dlq` alert that then retried forever (~50k
iterations observed on specialist). **Fixed in T-2038**: a cap is now classed transient
(`AcpCapError`, `mailbox_transient=True`) → left in `new/` uncounted, plus a circuit
breaker defers drains for `cap_cooldown_secs`. If you see Class B on a bridge, first
confirm it's running the post-T-2038 code; if so it self-recovers on cap reset.

**Where the evidence lives — NOT journald.** The bridge tees its per-drain status
JSON + stderr to `~/.local/state/pantheon/acp-bridge-<agent>.log` (on the agent's
account), never journald (`StandardOutput=journal` only captures the silent supervise
wrapper). `journalctl -u pantheon-acp-bridge@<agent>` looking empty means nothing —
read the log file. (This red herring cost a misdiagnosis in T-2038.)

## Precedent cases

- **mnemosyne, 2026-07-26** — Class A "Prompt too long" loop; recovered by deleting
  the state file before restart (the fresh-session path).
- **specialist, 2026-07-26 (T-2038)** — Class B usage-cap loop; root-caused + fixed
  structurally (transient classification + circuit breaker) and the bridge redeployed.
- **mnemosyne, T-1830** — earlier redelivery-loop residue in the same stack.

## Recommended remediation

Run as the agent's own user (`sudo -u <agent> bash -c '... XDG_RUNTIME_DIR=/run/user/$(id -u) ...'`).
Probe the substrate first (`feedback_verify_foreign_agent_substrate_before_live_mutation`):
confirm the unit name and the running invocation before acting.

0. **PRE-FLIGHT if you are running this on your own seat — confirm the wedged session is
   NOT your live one.** The bridge's session dir sits under the agent's own projects path
   and the log's `main_session_busy` reason both invite the misread that the wedged
   session *is* you; resetting your own live session is far more consequential than
   resetting a stale bridge one. **The discriminator is transcript mtime:** the wedged
   session's transcript is stale (hours), your live session's is being written right now.
   Compare the state file's `sessionId` transcript against your own before deleting
   anything. (Specialist hit exactly this on T-2132 — suspected self, checked mtime, and the
   wedged session had been idle 3.4h while its own was actively writing.)
0.5. **FALSIFY "wedged" FIRST — is the ACP session simply WORKING?** (Added 2026-07-28 by Recovery
   after resetting mnemosyne's bridge that was not broken.) A busy bridge and a wedged bridge
   look **identical** on every cheap signal, and all of them mislead:
   - `cputimes` near 0 — a blocked-on-`session/prompt` bridge and a healthy idle-polling one
     both consume ~1 CPU-second over an hour. Low CPU is NOT evidence of a wedge.
   - the log appears frozen — stdout is **block-buffered through `tee`**, so a fresh bridge
     writes nothing until ~4KB accumulates. A silent log means nothing on its own.
   - `cur/` directory mtime is hours old — correct and healthy if no mail *arrived* in those
     hours. Check when the queued messages were SENT before reading staleness into it.
   - a non-empty `new/` — each delivery costs the agent a **full working turn** (up to
     `--prompt-timeout`, default 1800s). A multi-message backlog draining at one per turn is
     the designed behaviour, not a fault.

   **THE DISCRIMINATOR — watch the ACP session's transcript grow:**
   ```
   S=$(cat ~/.local/state/pantheon/acp-bridge-<agent>.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["sessionId"])')
   F=~/.claude/projects/-home-<account>/$S.jsonl
   s1=$(stat -c %s $F); sleep 25; s2=$(stat -c %s $F)
   [ "$s2" -gt "$s1" ] && echo "WORKING — do not reset" || echo "static — continue to step 1"
   ```
   Growing ⇒ the prompt reached Claude and it is mid-turn. **Do not reset**; you would discard
   a live session to fix nothing. Static across two samples ⇒ proceed to step 1.
   A missing transcript file ⇒ the prompt never reached Claude — that IS a real fault.

1. **Read the log file** to classify: `tail -200 ~/.local/state/pantheon/acp-bridge-<agent>.log`.
   Class A → `Prompt is too long`; Class B → `You've hit your limit`.
2. **Class A (context wedge) — delete the state file, then restart:**
   ```
   systemctl --user stop  pantheon-acp-bridge@<agent>
   rm -f ~/.local/state/pantheon/acp-bridge-<agent>.json   # THE lever: forces session/new
   systemctl --user start pantheon-acp-bridge@<agent>
   ```
   Deleting the state file is load-bearing — without it the restart RESUMES the same
   full-context session and re-wedges. Work is committed to git before wedging, so a
   context reset loses no work.

   **This step is also a cost control, not only a recovery action (T-2135).** The session
   this SOP was written against was the single largest token consumer across 249 local
   transcripts — 16.6% of a 1.95B-token corpus on its own — because accumulated context
   makes per-turn cache-read climb ~8.3× from early turns to turn 100+. So recycling a
   long-lived bridge session is defensible *before* the -32603 loop appears: the wedge is
   the visible end of a gradient, not a discrete event. Numbers + caveats (flat-rate plan,
   so tokens not dollars) in `bugs/acp-bridge-context-full-prompt-too-long-mailbox-backlog.md`
   §Cost dimension.
3. **Class B (cap loop) — verify post-T-2038 code, don't fight the cap:** confirm the
   deployed `tools/acp_bridge.py` has `AcpCapError`/`_is_cap_error`; if so it
   self-recovers when the cap resets — no action beyond letting it ride. If the bridge
   predates T-2038, pull the clone to current `origin/main` + restart (a per-fire
   oneshot picks up new code on respawn; the bridge is `Type=simple`, so it needs a
   `systemctl --user restart`).
4. **Never** raw `tmux kill-session` alone — the supervisor just respawns it; and
   never `claude mcp list` a live bot.

## Verify after any restart — and do NOT count processes with a bare pattern

**Primary signal is the LOG, not the state file.** Recent lines show `iteration` reset
toward 0, `session_id: null` until a session is actually established, and `errors []`.

**The state file is timing-dependent, so it is a SECONDARY signal.** The bridge only
writes it once it establishes a session, and it only establishes one when it has
something to deliver — so immediately after a clean restart with an empty inbox the file
is **absent**, and it becomes present with a NEW `sessionId` later. Both "absent" and
"different `sessionId`" are healthy; only "unchanged `sessionId`" is a failed reset.
Measured on T-2132: absent at restart +0min, then `d7459558…` at +14min versus the
wedged `f351a1dd…` in the `.bak` — the reset was genuinely confirmed, just not yet
observable when the operator first looked.

**Unit state — this is the authoritative liveness check:**
```
systemctl --user show pantheon-acp-bridge@<agent> -p ActiveState -p SubState -p MainPID
```
Expect `ActiveState=active`, `SubState=running`, and a MainPID **different** from before.

**Process check — MUST be user-scoped. Three traps, all measured:**
```
pgrep -u <account> -af 'acp_bridge\.py --agent <agent>'     # expect exactly 2
```
- **A bare `pgrep -f <pattern>` matches its own command line**, and matches any shell
  wrapper whose argv contains the pattern. Measured: `pgrep -fc 'acp_bridge.py --agent
  specialist'` returned **5** when the truthful answer was 2. Read as "the stop failed" this
  invites the raw `tmux kill-session` escalation that step 4 explicitly forbids.
- **`| grep -v pgrep` is NOT a reliable self-exclusion.** It happened to work once only
  because the checking shell's argv contained the literal string `pgrep`. A wrapper
  carrying the pattern without that word still pollutes the count. Scope by `-u <account>`
  instead — it excludes your own shell (different user) *and* other agents' bridges.
- **An unscoped pattern sees OTHER agents' bridges.** Measured live: 3 procs for specialist
  and 3 for mnemosyne on the same host.

**Expect a TREE, not one process. "Exactly one bridge process" is the wrong invariant.**
The bridge is `sg veneration -c` → `sh -c` → `python3`; user-scoped that is **2** (the
`sg` wrapper runs as **root**, so `-u <account>` correctly omits it), and unscoped it is 3.

**Do NOT verify via `pstree -p <MainPID>`.** MainPID is `acp-bridge-supervise.sh`, and the
bridge tree does not hang beneath it — measured, `pstree -p` on the MainPID showed only
`bash---sleep` (the supervisor's poll sleep) while the bridge ran fine on another branch.

Then confirm backlog drains, and **state health explicitly**
(`feedback_confirm_health_after_touching_live_sessions`).

## Autonomous-vs-bubble-up D3 placement

Below the goal-level bar — a bridge restart is reversible, internal, and in the
recovery lane. Recovery (or the owning agent) executes autonomously and reports a digest;
no ratify-ask. Bubble up only if the wedge recurs immediately post-restart (a deeper
bug) or if recovery requires a privilege/scope Recovery doesn't hold.

## Cross-references

- `tools/acp_bridge.py` (engine), `tools/acp-bridge-supervise.sh` (supervisor)
- `recovery_runbook` (general fleet-recovery playbook)
- T-2038 (Class B root-cause + fix), T-1830/T-1690 (session/load lineage)
- T-2132 (verify-step amendment: pgrep self-match, state-file timing, own-seat pre-flight — filed by specialist from a live Class A execution)
- `sop/remediation/persona-daemon-liveness-recovery.md` (sibling daemon recovery)
