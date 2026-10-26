---
title: SCHEDULED — time-driven components
status: Redesigned 2026-06-11 by Orchestrator (T-1076); content lifted from Recovery's family files (T-0999).
purpose: Components whose work signal is the clock itself — the tick is the reason to act. Subtypes by effect; Monitor / Filer / Reconciler / Digester / Runner.
---

# SCHEDULED — the tick is the job

**Membership test:** with every queue empty and no upstream handoff state, the fire
still does its whole job (scan, watch, summarize, regenerate). A `.timer`/cron fires it
AND the tick itself is the work signal. (A timer that merely *transports* a check on
someone else's handoff artifact is [REACTIVE](reactive.md) — name-the-producer test.)

**Subtype litmus:** predicate-gated ([bail-and-poll](cross-cutting-patterns.md#bail-and-poll-detect--act))
→ Monitor/Filer/Reconciler/Digester by effect; **unconditional-every-fire → Runner.**
Writers follow [auto-commit discipline](cross-cutting-patterns.md#auto-commit--ledger-discipline);
filers are [idempotent + debounced](cross-cutting-patterns.md#idempotent--debounced-filing).
Shared libs: `_pane_predicates.py` (monitors), `_git_utils.py` (writers), `ledger_shard.py`.

---

## Subtype: Monitor — watch a world signal → alert/poke/restart-request

The signal is *exhaust* (context %, pane state, channel liveness, usage), not a handoff.
A new "is X alive?" check belongs in the liveness-probe trio (session/channel/api) and
reuses the matching shape. Alerts route via
[`route_failure_alert`](cross-cutting-patterns.md#failure-alert-routing-othersrecovery-recovery-selfthe-operator),
never direct-to-operator for a non-Recovery agent.

### context_fill_monitor — session liveness → restart request
- `tools/context_fill_monitor.py` · `pantheon-context-fill-monitor@.{service,timer}` · 5 min, per-agent.
- Context-fill threshold → writes a restart request; the repair executes in the **Restart loop** chain (→ [`restart_drain`](reactive.md), README §chains).
- Restart of the inner claude is verified by **PID change**, never exit-zero/is-active/sentinel (memory `reference_handoff_restart_systemctl_no_op`).

### activity_monitor — idle/wedge detection → structural poke
- `tools/activity_monitor.py` · `pantheon-activity-monitor@.{service,timer}` · 15 min, per-agent.
- Pane idle/parked/wedged signatures → structural poke into the pane.
- capture-pane comparison needs `-e` (ghost-text vs real input, memory `feedback_tui_ghost_text_vs_pending_input`); never blind-send keys into your *own* session (memory `feedback_self_health_alert_is_self_contradictory`).

### keep_going — anti-idle nudge
- `tools/keep_going.py` · `pantheon-keep-going@.{service,timer}` · `OnBootSec=5min` + 15 min, per-agent. LIVE on all 3 agents (T-0772).
- Watches the *idle-duration* signal; injects a "pick up your next item" nudge into the live session. Explains otherwise-unexplained "Please keep going" turns.
- *Placement note:* previously filed as a "Waker"; the idle signal is exhaust, not a handoff — Monitor whose poke is the nudge. Distinct from `mailbox_wake` ([Reactive/Deliverer](reactive.md)), which delivers actual work.

### channel_liveness_monitor — comms-dark detection → alert
- `tools/channel_liveness_monitor.py` · `pantheon-channel-liveness.{service,timer}` · 5 min.
- Detects **comms-dark but session-healthy** (bridge poller down, loop fine — T-0894) → alert via failure routing.
- NEVER diagnose a live bot with `claude mcp list` (single-poller 409; enforced by the [`mcp-list-guard`](hooked.md)); probe via ps + `curl getMe` (memory `reference_telegram_bridge_recovery_playbook`). This probe was the original spark for this whole map.

### usage_monitor — api/budget surface
- `tools/usage_monitor.py` · (timer per deployment).
- Surfaces usage/rate-limit state for humans; the **Resume loop** chain handles the repair side (`ho_stop_failure` → limit-hit file → `ho_resumer`; README §chains).

## Subtype: Filer — discover that work became due → file the work item

Discovery only; **filing is the action, never fixing inline**
(`sop/standard/auto-file-side-observations.md`, ratified). Sweep output format is
contractual: parent index + per-item sub-tickets, decision items `target=operator`
(`throughline-sweep-result-filing.md`) — never a rolling decision-menu.

### knowledge_sweep — daily knowledge/throughline sweep
- `tools/knowledge_sweep.py` · `pantheon-knowledge-sweep.{service,timer}` · daily 13:00 UTC. ORG-012 Phase C (T-0781).

### soak_filer — suite-health soak filer
- `tools/soak_filer.py` · `pantheon-soak-filer.{service,timer}` · monthly, 1st @ 06:00 UTC.
- Files the soak-verification ticket when a capability's unattended-soak window elapses (T-0991/T-0992; [ship-dark + supervised-first-fire](cross-cutting-patterns.md#ship-dark--supervised-first-fire)).

### ticket_grooming — staleness scanner (+ embedded proof-of-life digest)
- `tools/ticket_grooming.py` · `pantheon-grooming.{service,timer}` · daily 12:00 UTC. **Recovery's cron responsibility.**
- Flags `stale_after`-exceeded tickets idempotently; auto-commits + pushes; every fire also sends the plain-text Telegram digest **regardless of count** (the Digester-grade proof-of-life half — silence must mean broken, not idle).
- Scope strictly `tickets/active|blocked/`; refuses on HERMES-guard/scope failure; `git pull --ff-only` failure exits non-zero for HO to reconcile. Never exercise the reopen path against `cwd=pantheon-config` — tmpdir `--repo-root` (memory `reference_grooming_offschedule_fire_resolved`).

### promote_lessons — weekly lessons promoter *(census addition — postdates first draft)*
- `tools/promote_lessons_cron.py` · `promote-lessons.timer` · weekly. **Recovery.**
- Scans `lessons/` for promotion candidates; files/promotes per its SOP. (Filer with a digest tail.)

## Subtype: Reconciler — discover inconsistency → fix it in place

The fix-in-place half that the old "Filers" family wrongly bundled with filing: these
WRITE, so they carry the full
[auto-commit discipline](cross-cutting-patterns.md#auto-commit--ledger-discipline)
(HERMES guard, narrow scope, refuse-on-doubt) — a materially heavier contract than filing.

### ticket_lint — frontmatter fixer + cycle detector
- `tools/ticket_lint.py` · `pantheon-ticket-lint.{service,timer}` · hourly @ :20.
- Auto-fixes malformed frontmatter; flags dependency cycles + stale-done-deps (T-0975). Run `--dry-run` after batch gating (memory `feedback_check_parent_and_cycles_before_gating`). Can false-flag via a cross-repo resolution bug — verify the checker before retargeting (memory `feedback_improper_tickets_retarget_to_originator`).

### spine_index_groom — spine-README index regenerator
- `tools/spine_index_groom.py` · `pantheon-spine-index-groom.{service,timer}` · daily 12:30 UTC.
- Regenerates sentinel-region index blocks in spine READMEs ([marker/sentinel-debounce](cross-cutting-patterns.md#marker--sentinel-debounce) reference implementation); reconciles INDEX/SPINE against the ledger shards. Manual invocation now **requires a mode flag** (`--commit-prod` / `--dry-run`).
- *Placement note:* previously filed as a "Runner"; it is predicate-gated (no-ops when regions match) and its job is fixing drift between derived indexes and source — Reconciler.

## Subtype: Digester — aggregate activity → human-facing summary

Output is *informational* (a pulse, a rollup), not a work item or control action. Sent
via the Telegram reply tool (the human reads a phone, not a transcript). Digests are the
legitimate deliberate human-facing rollup — don't smuggle alerts into them (alerts route
via `route_failure_alert`). **Emit proof-of-life even on an empty cycle** so silence is
diagnostic.

### activity_digest — RETIRED (2026-09-29, T-4289)
- Was the 5-hourly status pulse to the operator; removed as status-only under T-4200 (action-only Telegram).

### watch_digest — daily watch-loop digest
- `tools/watch_digest.py` · `pantheon-watch-digest.{service,timer}` · daily 18:00 UTC. **Specialist.**
- Human-facing readout of `watch_runner`'s reports; deliverable shape per `sop/standard/research-dispatch-deliverable-standards.md`.

### weekly_ticket_summary — weekly ticket rollup
- `tools/weekly_ticket_summary.py` · `pantheon-weekly-ticket-summary.{service,timer}` · Mon 09:00 UTC.
- *Placement note:* previously misfiled as a Filer; its output is informational — Digester (per `sop/standard/weekly-ticket-summary-generation.md`).

## Subtype: Runner — unconditional defined job, every fire

The **only** members are genuinely gate-free: the fire IS the job, no predicate bails.
(The old Runners family was a residual catch-all; after the bail-and-poll litmus its
members redistributed — see placement notes below.)

### watch_runner — continuous-watch dispatch driver
- `tools/watch_runner.py` · `pantheon-watch.{service,timer}` · daily 08:00 UTC. **Specialist.**
- Every fire dispatches the day's standing-topic researchers; reports land in `knowledge/watch/reports/<date>/`; auto-commits + pushes (`research(watch): land <date> reports`, scope-limited — the **expected** claude-research auto-commit, signature in CLAUDE.md).

### kino_pwa_autodeploy — PWA redeploy poller
- `tools/kino_pwa_autodeploy.py` (T-0316) · `pantheon-kino-pwa-autodeploy.{service,timer}` · 5 min.
- Pull + redeploy the kino PWA when source changed. *Borderline by the litmus* (it gates on "source changed") — kept Runner because the pull half runs unconditionally and the gate is an idempotence optimization, not a work-discovery predicate; flagged honestly rather than apologized.
- PWA-down probes must NOT use bare `tailscale serve status` (memory `feedback_tailscale_serve_status_trap`); tagged-device serves inject no user-login header → kino 401 (memory `reference_tailscale_tagged_device_no_user_login`).

### pantheon_apt_update — weekly system package update *(census addition)*
- `pantheon-apt-update.{service,timer}` · weekly, **root** system timer (the one non-user unit in the census).

---

## Retired (kept for reference, not standing)

- **pantheon_config_sync** (T-0211 Phase B → RETIRED 2026-06-09, T-0365): every-minute bidirectional repo sync; replaced by kino read-path pull-on-demand (`syncGate`). Consequence: a `ticket.py --no-push` local commit no longer auto-propagates within 60s (help-text drift tracked in T-1039).

## Placement notes

- New scheduled tool: pick the subtype by **effect** (alert? file? fix? summarize? unconditional-job?); follow that subtype's contract above. If it writes: auto-commit discipline, narrow scope, expect (don't hide) the [branch-protection bypass success line](cross-cutting-patterns.md#branch-protection-bypass-is-a-success-not-a-rejection).
- The old families map here as: Monitors → Monitor; Due-detectors+Filers → split Filer (file) vs Reconciler (fix-in-place); Digesters → Digester; Runners → redistributed (watch_runner stays; spine_index_groom → Reconciler; kino_pwa_autodeploy stays-with-flag; config_sync retired).
