---
title: REACTIVE — handoff-driven components
status: Redesigned 2026-06-11 by Orchestrator (T-1076); content lifted from Recovery's pollers-and-wakers.md + remediators.md (T-0999).
purpose: Components whose work signal is an artifact a nameable upstream component produced for them — queue items, mailbox files, tombstones, state files. Subtypes: Deliverer / Repairer.
---

# REACTIVE — a handoff artifact is the work signal

**Membership test:** name the **producer component** and the **artifact**. If you can
(agents file tickets → inbox; agents send mail → hub `new/`; `restart_request` writes
tombstones; `ho_stop_failure` writes the limit-hit file), the component is Reactive —
**even though its wiring is almost always a polling timer.** The timer is *transport*;
the artifact is the signal: an empty-queue fire does nothing, by design. (Contrast a
[Scheduled](scheduled.md) component, whose tick does its whole job with all queues
empty; a watched *world* signal with no producer — context %, idle time — is exhaust,
and its watcher is a Scheduled/Monitor.)

Every member here is the consumer end of a
[handoff chain](README.md#handoff-chains--composition-is-expected-not-an-overlap-apology) —
the chains table in the README is the composition view; this file is the per-component view.

**Shared contracts:** consumers are
[idempotent + debounced](cross-cutting-patterns.md#idempotent--debounced-filing)
(a timer re-fire never double-delivers); cross-account/group-gated consumers respect the
[user-systemd group constraint](cross-cutting-patterns.md#user-systemd-carries-only-the-primary-group)
(the hub's inotify `.path` units are structurally DEAD — clock-timer transport is the
mechanism, `sg veneration`-wrapped ExecStart) and the
[timer-anchor discipline](cross-cutting-patterns.md#timer-anchor-discipline-survive-a-manager-restart)
(`OnBootSec` + `OnUnitActiveSec`, survives a manager restart). Repairs are **verified
structurally** (PID change, file written, ref updated) — never trust exit-zero/is-active
(memories `feedback_tooling_can_fabricate_output`, `reference_handoff_restart_systemctl_no_op`).

---

## Subtype: Deliverer — hand the artifact's work to an agent

### inbox_consumer — ticket-queue delivery
- `tools/inbox_consumer.py` · `pantheon-inbox-consumer@.{service,timer}` · `OnBootSec=5min` + 10 min, per-agent.
- **Chain:** agents/Filers → `tickets/inbox/` `target=<agent>` items (+ `not_before`-elapsed) → delivered into the owning agent's session. Also home of the `route_failure_alert` helper.
- A **bare** `target=recovery` ticket auto-fires immediately — gate future work with `--depends-on`/`--not-before` at file time (memory `feedback_gate_forward_tickets_at_file_time`). Specialist self-consumes (`@specialist`, ORG-025). Its `OnBootSec` exists because of the timer-anchor discipline (it once stalled silently after a manager restart).

### mailbox_wake — hub-mail delivery
- `tools/mailbox_wake.py` (drives `mailbox.py`) · `pantheon-mailbox-wake@.{service,timer}` · `OnBootSec=2min` + 5 min, per-agent.
- **Chain:** agents (`mailbox.send`) → `/home/the-ancestor/mailbox/<agent>/new/` → wake the agent to consume. Marker-debounced. The load-bearing autonomous-intercomms path (T-1015); restart-durable.
- **codex caveat (T-1041):** codex participates but is not a Claude agent — `bwrap` sandbox blocks `sg` (needs **native** `veneration` membership via user-manager restart, not daemon restart); the hub must be in `writable_roots` with group-writable entries (the `0710` hub parent breaks bwrap's git-prep → every codex shell command fails); codex cwd is its home so kicks need **absolute** paths. Runbook: `codex-config:MAILBOX.md`.

### ancestor_consume — the-ancestor dispatch loop
- `tools/ancestor_consume.py` · runs *inside* the `pantheon-ancestor-daemon.service` session (the unit hosts the daemon; no ExecStart references the script — the old map conflated the two).
- **Chain:** mail/work addressed to the shared-state host → the-ancestor's own dispatch loop (T-0765 glue; ORG-026 Phase 2 / T-1017 wires the remaining consume sides).

## Subtype: Repairer — execute the repair the artifact requests

The artifact *is* a repair request. Distinction from a Monitor's poke: a Repairer
restores a **failed** surface (dead session, rate-limited agent, requested restart); the
nudging of *healthy-but-idle* agents is [Scheduled/Monitor](scheduled.md) (`keep_going`).

### restart_drain — tombstone processor
- `tools/restart_drain.py` · `pantheon-restart-drain.{service,timer}` · every 30 s.
- **Chain (Restart loop):** `restart_request` (Invoked) or `context_fill_monitor` (Scheduled/Monitor) → tombstone files → drain fires the graceful restart (executes `handoff_and_restart.sh`, [Invoked/Front-door](invoked.md)).
- A drain cgroup-kill was once misdiagnosed as the XDG footgun — verify the journal/unit before naming a cause (memory `feedback_verify_root_cause_before_asserting`).

### ho_resumer — limit-hit resume
- `tools/ho_resumer.py` · `pantheon-ho-resumer.{service,timer}` · every 5 min.
- **Chain (Resume loop):** `ho_stop_failure` (Hooked/Recorder) → limit-hit state file → fires `claude --resume`. The resume-from-*failure* counterpart of `keep_going`'s healthy-idle nudge; `usage_monitor` surfaces the same condition for humans.

### kick_verify — post-kick verification + bounded re-fire
- `tools/kick_verify.py` (T-0899).
- **Chain:** any waker/poke delivery → the kick record → verify it actually *dispatched* (a kick can be delivered-but-never-dispatched in a post-restart boot window — clean jsonl gap, memory `reference_silent_kick_drop_boot_window`) and re-fire bounded times. The verify-after-act discipline that pairs with every delivery mechanism.

---

## Placement notes

- New consumer: add the chain row in the README first (producer → artifact → consumer), then the component entry here. Deliverer vs Repairer by what the artifact asks for (work vs repair).
- After ANY tooling against another agent's live session (even read-only), verify health and STATE it (memory `feedback_confirm_health_after_touching_live_sessions`). Account for auto-remediation when diagnosing — a healthy snapshot ≠ false alarm (memory `feedback_account_for_auto_remediation_in_incident_diagnosis`).
- The old families map here as: Pollers → Deliverer; Wakers → split (`keep_going` → Scheduled/Monitor; `mailbox_wake` stays here as Deliverer — it delivers work); Remediators' *standing* members → Repairer (`handoff_and_restart.sh` + `restart_request` + `veneration` → [Invoked](invoked.md), they have no standing trigger).
