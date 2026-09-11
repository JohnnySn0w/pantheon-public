---
title: INVOKED — will-driven components
status: Redesigned 2026-06-11 by Orchestrator (T-1076); content lifted from Recovery's remediators.md + runners.md (T-0999).
purpose: Components a human or agent deliberately runs — no standing wiring fires them. Subtypes: Front-door / One-off / On-demand tool. This is also the fence that keeps the standing census honest.
---

# INVOKED — a decision is the work signal

**Membership test:** `grep` the unit files, crontabs, and hook wiring — **nothing fires
it.** It runs when someone (operator or agent) decides it should. This general exists
partly as a *census fence*: the first draft's "~40 standing components" inflated to that
number by counting these as standing; they are real, load-bearing, and **not standing**.

The operational line this preserves (why Invoked is not collapsed into Reactive): a
Reactive component **fires unattended** off a handoff; an Invoked one **cannot run
without a decision**. What can break silently at 3am is exactly the standing set —
Scheduled + Hooked + Reactive — and never these.

---

## Subtype: Front-door — the clean entry point humans/agents call

### restart_request — request a graceful restart
- `tools/restart_request.py`.
- Writes the restart **tombstone** that `restart_drain` ([Reactive/Repairer](reactive.md)) picks up — the producer end of the **Restart loop** chain, and the clean alternative to touching units directly.

### handoff_and_restart.sh — the graceful-restart executor
- `tools/handoff_and_restart.sh` (T-0008) · driven on demand (by `restart_drain`, or directly by an operator).
- Flow: flock → HANDOFF-mtime witness → canned handoff-write prompt → mtime-change poll → `systemctl restart` → is-active poll → capture-pane sentinel → SessionStart hook re-injects HANDOFF.
- **CRITICAL verification footgun:** `systemctl restart` is a **NO-OP for the inner claude** (supervisor wrapper is idempotent; inner claude lives outside the unit cgroup) — the canonical restart "succeeds" with the *same PID*. True restart = `tmux kill-session` + supervisor respawn; **verify by PID change on `.npm-global/bin/claude`**, never exit-zero/is-active/sentinel (memory `reference_handoff_restart_systemctl_no_op`).
- **Stale-flock wedge:** repeating `lock … held by another instance` = a wedged child holds the flock; `lsof` → SIGKILL holder+child → rm lockfile → re-fire (memory `reference_stale_flock_restart_pipeline`). **Self-restart** leaves the fresh session silent unless the kick comes from a detached transient unit — use `tools/self_restart.sh` / `tools/recovery_self_restart.sh` (SOP `agent-self-restart-with-kick.md`).

### veneration — resurrect-a-dead-agent harness
- `tools/veneration.py` (T-0766); HANDOFF archive in `tools/chronicle.py`.
- The heavyweight stand-a-fresh-daemon-back-up harness: session selection from the durable HANDOFF Chronicle → fresh daemon. CODE substrate complete + tested (88 tests, 2026-06-07); the **supervised live stand-up is the remaining step — do NOT rebuild** (memory `project_veneration_build_state`). Lends its group name to the ORG-026 hub.

## Subtype: One-off — migrations kept for repeatability

- `tools/migrate_ledger_to_weekly_shards.py` — the T-0148 ledger→weekly-shards migration (shares `ledger_shard.py`).
- `tools/retro_metrics.py` — one-time retro-extraction of the cycle-stats field.

## Subtype: On-demand tool — no trigger by design

### ticket_state_sweep — state↔ledger consistency check
- `tools/ticket_state_sweep.py` · **no timer/cron/hook anywhere** (the tightness review's "orphan-trigger" finding — previously misfiled as a scheduled scanner).
- Run it when state↔ledger divergence is suspected; reconciles/flags. If it earns a schedule someday it moves to [Scheduled/Reconciler](scheduled.md) — the placement algorithm re-runs on rewiring, by design.

### Retired-but-standing-by-shape
- **pantheon-bridge.service** — the decommissioned Telegram bridge daemon (T-0767); unit shape survives in the graveyard/archive for reference. Listed so its absence from the standing census reads as deliberate.

---

## Placement notes

- New operator-facing tool with no trigger: it goes here, and it does NOT count toward the standing census. If you later wire a timer/hook to it, re-run the placement algorithm — components move between generals when their *wiring* changes meaning, and that movement is informative, not embarrassing.
- Remediation-class front-doors carry the repair-verification discipline (structural witness, PID-change, independent reads) regardless of being invoked by hand — *especially* then.
