# Agent self-restart-with-kick

**Status:** **Ratified 2026-05-22 by the operator (Telegram msg_1884).** Drafted 2026-05-21 by Recovery (T-0530, sibling of T-0518/T-0524 restart-resilience set).

How an always-on agent restarts its OWN long-lived Claude Code session **on demand** and lands the post-restart kick — so the fresh session takes its first turn (reads `HANDOFF.md`, announces on Telegram) **unattended**, with no operator `/resume`. Proved end-to-end on Recovery 2026-05-21 (`tools/recovery_self_restart.sh`, commit `8a41b30`); this very SOP's authoring session was brought up by it.

For *firing a restart of another session* (operator/monitor surface) see [`handoff-restart-runbook.md`](handoff-restart-runbook.md). For *installing* the 5-capability stack on a new agent see [`adding-restart-capability-to-a-new-agent.md`](adding-restart-capability-to-a-new-agent.md). This SOP is the third leg: an agent **deliberately restarting itself**.

## When to use this

The agent itself decides — mid-session, in-band — that it needs a fresh session right now, and wants the fresh session to come up working (not idle):

- Cleared a blocker / shipped a change whose effect needs a clean session to take hold (e.g. updated its own settings, hooks, or MCP config).
- Self-diagnosed context cruft or a soft wedge that a fresh session would think past, but is NOT capped and NOT 400ing (those have their own paths — see Boundary).
- Validating the restart stack itself (this SOP's reference fire).

**Do NOT use this for** the unattended/scheduled paths — those already exist and should NOT be hand-fired:
- **Context-decay or api-health restart** → the monitor writes a tombstone, the drain consumes it (`context_fill_monitor.py` → `restart_drain.py`). Scheduled, hands-off; don't race it with a manual self-restart.
- **Max-window cap-hit** → `tools/ho_resumer.py` + `pantheon-ho-resumer.timer` (separate trigger/state; see [`auto-resume-operator-runbook.md`](auto-resume-operator-runbook.md)).
- **Restarting a *different* agent** → fire `handoff_and_restart.sh` against its pane directly; no self-restart wrapper needed (you don't die when you restart someone else).

## The core problem this solves

`handoff_and_restart.sh` already lands the kick (step 8 send-keys a directive into the fresh pane to force its first turn — necessary because the SessionStart auto-read hook injects `HANDOFF.md` only as `additionalContext`, which seeds context but does **not** trigger a turn). But that wrapper **cannot drive a *self*-restart**: it runs inside the very session being restarted, so `systemctl restart` kills the wrapper mid-flight, before it can deliver the kick. The fresh session then comes up silent.

**Fix:** launch `handoff_and_restart.sh --skip-ack` from a **detached `systemd-run --user` transient unit**. The transient unit lives in its own cgroup, so it survives the restart of the agent's service and delivers the kick to the fresh session. `--skip-ack` bypasses steps 2–4 (the in-session handoff *prompt* + ack-wait — the dying session can't answer it), running `restart → is-active → steady-state → kick`. The kick rides the T-0519 `send_keys_submit()` fix (delayed Enter + submit-verify), so this path also live-validates that fix on every fire.

## Procedure

**Pre-condition (MANDATORY): refresh `HANDOFF.md` BEFORE firing.** The wrapper does NOT write the handoff — an agent cannot write its own handoff and then restart synchronously without deadlocking. The kick tells the fresh session to *read* `HANDOFF.md`, so a stale or missing file means the fresh session resumes on stale state. Write the handoff in the same turn, immediately before the fire.

Invocation (run as the agent's own user, in-session). Generic path (any agent — same flag contract as `handoff_and_restart.sh`):

```bash
# 1. Refresh the handoff first (Write tool / editor) — captures what's inflight.
# 2. Then fire the self-restart:
~/pantheon-config/tools/self_restart.sh \
  --service <unit> --target-pane <session>:0 [--mode user|system] \
  [--handoff-file <path>] [--steady-timeout 90]
# Recovery shorthand (back-compat wrapper pinning Recovery's substrate):
~/pantheon-config/tools/recovery_self_restart.sh
```

The transient unit name is derived from `--service` (`self-restart-<service>`) so it's unique per agent; override with `--unit`. `--mode` threads into both the transient-unit scope (`systemctl`/`systemd-run` `--user` vs system) and the wrapped `handoff_and_restart.sh`. Always `--dry-run` first when adapting to a new substrate — it prints the constructed `systemd-run` command without firing.

The wrapper:
1. **Preflights** — tmux session exists, `handoff_and_restart.sh` is executable, `HANDOFF.md` freshness (warn-only).
2. **`reset-failed`** the fixed transient unit name (a failed instance from a prior run would block re-launch).
3. **`systemd-run --user --collect --unit=recovery-self-restart`** a detached `bash -c`: `sleep <SELF_RESTART_DELAY> ; exec handoff_and_restart.sh --skip-ack …`. The `sleep` lets the launching turn flush before the restart kills the session; `exec` makes the restart script the unit's `MainPID`.
   *(Note 2026-10-16, Orchestrator, T-5060: `recovery-self-restart` is the name Recovery's wrapper pins. Through `self_restart.sh` the unit is `self-restart-<service>` unless `--unit` overrides it, as described above.)*
4. The transient unit (now in its own cgroup) survives `systemctl restart <service>`, waits for steady-state (`--steady-timeout 90` — claude + plugin + bun boot can exceed the script's 60s default), then send-keys the kick into the fresh pane.

**Validated per-agent invocations (dry-run-proven 2026-05-22, T-0531; substrate from `context_fill_monitor.py` AGENT_CONFIG):**

| Agent | User | Invocation (run in-session as that user) |
|---|---|---|
| Recovery | `claude-fallback` | `tools/recovery_self_restart.sh` (or the explicit flags below with `--service claude-telegram-fallback.service --target-pane claude-telegram-bot-fallback:0`) |
| Orchestrator | `claude` | `tools/self_restart.sh --service claude-telegram.service --target-pane claude-telegram-bot:0 --handoff-file /home/claude/HANDOFF.md` |
| Specialist | `claude-research` | `tools/self_restart.sh --service claude-telegram-research.service --target-pane claude-research-bot:0 --handoff-file /home/claude-research/HANDOFF.md` |

All three derive collision-free transient unit names (`recovery-self-restart`, `self-restart-claude-telegram`, `self-restart-claude-telegram-research`).

**Generalizing to other agents:** use `self_restart.sh` directly with that agent's `--service/--target-pane/--mode` — self-restart is always same-account (the agent fires it as its own user; no cross-account sudo). **Verify the tmux session name against the unit's `ExecStart` — do NOT guess it** (the same gotcha as the install SOP: `claude-fallback-bot` was wrong; the real session is `claude-telegram-bot-fallback`). `recovery_self_restart.sh` remains as a thin back-compat wrapper that pins Recovery's defaults (and still honors the legacy `RECOVERY_*` env overrides), but new call sites should prefer the explicit flags.

## Common failure modes

- **Fresh session comes up silent (kick didn't land).** Two culprits: (a) the kick text reached the pane but the Enter was swallowed — tmux paste-mode batches successive send-keys; this is exactly what the T-0519 `send_keys_submit()` delayed-Enter + submit-verify guards against, so confirm `handoff_and_restart.sh` is at `f302ecf`+; (b) **step-7 steady-state sentinel never matched** the fresh boot pane → the kick step was skipped. Diagnose with `tmux capture-pane`; the stale-`idle_sentinel` trap (install SOP Gotchas) bites here too — the live pane shows `❯` / `esc to interrupt`, not the old `? for shortcuts`.
- **`STEADY_TIMEOUT` too short.** claude + telegram plugin + bun bridge boot can exceed 60s; the wrapper defaults to 90s for this reason. If steady-state is declared-failed on a healthy-but-slow boot, raise `RECOVERY_STEADY_TIMEOUT`.
- **Transient unit name collision.** `systemd-run` reuses the fixed unit name `recovery-self-restart`; a *failed* prior instance blocks re-launch. The wrapper `reset-failed`s it first — if you bypass the wrapper, do the same.
- **Stale/missing `HANDOFF.md`.** Warn-only in the wrapper (it kicks regardless), but the fresh session resumes on stale state. Pre-condition exists for this reason — refresh first, every time.
- **Fired the wrong path.** If the agent is capped or 400ing, this on-demand wrapper is the wrong tool — see Boundary; the monitor/drain or `ho_resumer` own those.
- **Self-inflicted bridge kill while "testing."** Do NOT spawn a second `claude` (even `--bare`) to reproduce or stage this — ANY second claude takes the Telegram token and kills the live bridge. The wrapper is the only safe self-restart path. (Self-inflicted twice; see [[feedback_no_second_claude_with_telegram_plugin]].)

## Boundary

- **Scheduled/unattended self-heal** (context-decay, api-health-400) → monitor + drain, NOT this. This is the *on-demand, agent-decided* leg.
- **Cap-hit auto-resume** → `ho_resumer.py` + timer (separate trigger/state).
- **Restarting another agent** → fire `handoff_and_restart.sh` directly (no self-death to survive).
- This SOP supersedes the now-stale "HO self-shutdown: no such mechanism today" line in [`handoff-restart-runbook.md`](handoff-restart-runbook.md) §Boundary — an agent CAN now initiate its own restart.

## Cross-references

- **Source / scripts:** `tools/self_restart.sh` (the generalized self-restart wrapper — primary source; `--dry-run`/`--help`; smoke `tools/test_self_restart.sh` 9/0); `tools/recovery_self_restart.sh` (Recovery back-compat wrapper over it); `tools/handoff_and_restart.sh` (the wrapper it drives, with `--skip-ack`); commits `8a41b30` (original Recovery wrapper), `f302ecf` (T-0519 kick fix), `7f9df0b`/`970e50c` (per-UID lockfile + reset-failed hardening), T-0531 (generalization).
- **Sibling SOPs:** [`handoff-restart-runbook.md`](handoff-restart-runbook.md) (fire-a-restart operator surface), [`adding-restart-capability-to-a-new-agent.md`](adding-restart-capability-to-a-new-agent.md) (install the stack), [`auto-resume-operator-runbook.md`](auto-resume-operator-runbook.md) (cap-hit boundary).
- **Principles + directives:** P1 (architecture over policy — a structural self-heal beats hoping the operator notices a silent session); D1 (the operator's bandwidth — unattended self-restart is what makes "work indefinitely" hold without a human in the loop).
- **ADRs:** ORG-020 (context-fill monitor + restart drain — the unattended siblings this on-demand path complements).
- **Tickets:** T-0519 (send-keys Enter race fix the kick rides), T-0517 (parity epic), T-0518/T-0524 (install SOP + its ratification), T-0530 (this SOP's ratification), T-0531 (`self_restart.sh` generalization — shipped).
