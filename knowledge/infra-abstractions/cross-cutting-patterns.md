---
title: Cross-cutting infra patterns
status: Drafted 2026-06-09 by Recovery (T-0999). Part of the infra-abstractions reference map.
purpose: The contracts that span component families. A new standing tool should follow the relevant ones here rather than inventing its own.
---

# Cross-cutting infra patterns

These are the contracts that recur across families. They are *descriptive* (this is how
our standing tools behave) and *prescriptive-by-convention* (a new tool should match,
because deviating surprises operators and breaks the fleet's safety posture). Where a
pattern is ratified as binding, the ADR is linked.

---

## Trigger & control-flow

### Bail-and-poll (detect → act)
The dominant Monitor/Filer shape: on each fire, **cheaply check a predicate; if it does
not hold, exit 0 immediately** ("bail"); only when it holds do the (more expensive)
action. Driven by a periodic `.timer` (the "poll"), so the loop is the timer's cadence,
not an in-process `while True`. Keeps each fire fast and crash-restartable, and makes the
cadence a unit-file knob rather than code.
- *Examples:* every Monitor; `ticket_grooming`, `ticket_lint`, `soak_filer`.
- *Edge-trigger alternative (`.path` inotify) is unusable under group-gated paths* — see [user-systemd group constraint](#user-systemd-carries-only-the-primary-group) below; use a timer + idempotent action instead.

### Idempotent + debounced filing
A poll-driven action that writes (a ticket line, a flag, a commit) **must be safe to run
repeatedly** because the timer re-fires. The convention: before appending, check whether
the latest entry is already the one you'd write, and skip if so (marker/sentinel
debounce). The digest may still re-surface the item every fire; the *write* does not
duplicate.
- *Examples:* `ticket_grooming` skips a repeat `[grooming] stale` line; `mailbox_wake` is marker-debounced so periodic re-runs never double-consume; `spine_index_groom` is a sentinel-region updater.

### Marker / sentinel debounce
The general mechanism behind idempotent filing and "block once" guards: hash or
sentinel-string the unit of work, persist the marker, and no-op if the marker is present.
- *Examples:* `act-vs-ask-gate` blocks once per distinct message via a hash marker; grooming's `[grooming] stale` line is its own sentinel; spine-index groom updates between sentinel comments.

---

## Safety & failure posture

### Default-safe contract
When a guard or detector cannot reach a confident verdict, it resolves to the **safe**
side, and which side is "safe" is decided per-tool by what failure is worse:
- **Fail-open** where bricking is the worse outcome: a Guard that errors must not block the action it guards. *Example:* `act-vs-ask-gate` is fail-open by design — it can never brick comms; a genuine decision re-send passes.
- **Fail-closed** where acting-wrongly is the worse outcome: a de-wedge / dismissal step that can't positively confirm the safe target refuses to act. *Example:* `verify_startup.sh` de-wedges the boot dialog fail-closed (exact-sentinel + denylist).
The rule is not "always fail-open" — it's "name the worse failure and resolve toward avoiding it."

### Refuse-on-doubt for writers (guard before commit)
Auto-commit tools refuse the commit if a precondition fails, with loud stderr and a
non-zero exit — they do **not** force, auto-rebase, or auto-recover unattended.
- *Examples:* `ticket_grooming` refuses on HERMES-guard failure or out-of-scope staged changes; the grooming `git pull --ff-only` failure exits non-zero rather than auto-rebasing real divergence.

### Connectivity / soft-vs-hard warnings
A boot/health check distinguishes **hard** failures (brick the thing being checked) from
**soft** warnings (note and continue). Never let a soft signal brick a boot.
- *Example:* `verify_startup.sh` treats pane-wedge as hard (exit 7) but connectivity / service / stale-handoff as soft warnings.

---

## Cross-account & substrate constraints

### user-systemd carries ONLY the primary group
A `systemctl --user` unit — and the `user@<uid>.service` manager itself — runs with only
the account's **primary** group. `SupplementaryGroups=` hard-fails (216) on `--user`
units. Consequences a new component on the user-systemd substrate must respect:
- A user service touching a **group-owned** resource must wrap its ExecStart in `sg <group> -c '<cmd>'`. (The mailbox wake `.service` died `PermissionError` on the `2770 veneration` hub until wrapped.)
- **`sudo -u <user>` runs `initgroups`** → the target's FULL group set, so a *manual* run works while the *systemd service* fails the same op. Don't conclude "works" from a manual/sudo run; test via the actual unit.
- The manager's group set is **fixed at login**; `usermod -aG` doesn't reach a running manager until a `user@<uid>.service` restart (heavy — cycles the agent).
- **`.path` (inotify) units are unusable for a group-gated path** under user systemd (the manager can't traverse the gated dir, so the watch silently never fires). Use a clock `.timer` + idempotent action.
- *Refs:* memory `feedback_user_mode_systemd_cannot_set_supplementary_groups`; ADR `ORG-026`; `notes/2026-06-09-infra-patterns-observed.md`.

### Neutral-shared-home (cross-account shared state)
Per-account homes are `750` (private, untraversable cross-account). For state N agents
must share, co-locate it in a **neutral group-accessible account** (Pantheon: `the-ancestor`):
home `0710 <group>` (peers traverse through to a known path, can't enumerate), shared
dirs `2770 <group>` (setgid + group-writable so children inherit the group). Writers
whose live process lacks the group still need `sg`. Generalizes to any cross-account
shared resource (mailbox, lease, cache). This is ORG-026 Path A.

### Timer-anchor discipline (survive a manager restart)
A user `.timer` that must survive a manager restart needs a **restart-relative anchor**
(`OnBootSec` / `OnActiveSec` / `OnCalendar`), NOT `OnUnitActiveSec` alone —
`OnUnitActiveSec` computes next-fire from the service's last activation, and a
freshly-restarted manager has no post-restart history, so it schedules nothing
(`list-timers` → `Trigger: n/a`, never fires again). Bit `pantheon-inbox-consumer@.timer`
after a manager restart (fixed with `OnBootSec=5min`). After any manager restart, sweep
all timers for a missing realtime next-fire. (`OnCalendar` showing
`NextElapseUSecMonotonic=infinity` is the monotonic-display quirk, NOT a stall.)
- *Credit:* Specialist (T-1020); *ref:* `notes/2026-06-09-infra-patterns-observed.md`.

---

## Auto-commit & ledger discipline

### HERMES byte-sequence-guarded auto-commit
Any tool that auto-commits runs the HERMES byte-sequence guard over the file content +
constructed commit message and refuses (loud, non-zero) on a violation. The trigger
phrase must not appear contiguously in any committed source byte; prose references use
the discipline-escape form.
- *Examples:* `ticket_grooming`, `ticket.py` lifecycle ops, the watch auto-commit; scanners `hermes_diff_scan` / `hermes_history_audit`. Shared primitives in `_git_utils.py`.

### Branch-protection bypass is a SUCCESS, not a rejection
A push printing `remote: Bypassed rule violations …` / `Changes must be made through a
pull request` but that **still updates the ref** succeeded (bypass-allowlisted identity).
A *rejected* push with no ref update is the real failure. Don't use `git push -q` — it
hides the bypass-success line. (Deploy-key **write** and **main-bypass** are separate grants.)
- *Ref:* memory `reference_direct_git_push_works_bypass`; `notes/2026-06-09-infra-patterns-observed.md`.

### Narrow-scope commit guard
Auto-committers narrow their staging to a declared path scope and refuse to commit
unrelated staged changes — prevents a cron from sweeping up a human's in-flight edit.
- *Examples:* `ticket_grooming` (scopes to `tickets/active/`+`tickets/blocked/`); `ticket.py` lifecycle ops refuse with unrelated staged changes.

### Index + records (the spine pattern, D10)
Durable knowledge that compounds is shaped **index + records**: a thin index (one line per entry — id / ts / agent / tag / summary) is what loads in agent context; the full record opens on demand. Every directory carries a `README` spine; append-only logs (the ledger) split into an `INDEX` plus per-segment record files. References (path/commit pointers) replace transclusion — one canonical source, N pointers, so the same fact doesn't drift across five copies.
- *Examples:* `ledger/INDEX.md` + per-week `<seg>/SPINE.md` + `<seg>.jsonl`; every spine `README` regenerated by `spine_index_groom`. *Ref:* directive D10.

### Ledger pipeline ordering (append → commit → push → groom → pull)
The ledger's INDEX + per-segment SPINE are **groomer-generated, not hand-edited**. The correct sequence for an HO ledger append: append the entry to the current-week shard `.jsonl` ONLY → commit + **push** the jsonl → run `spine_index_groom` (it regenerates INDEX + SPINE from origin and commits/pushes off a fresh origin worktree) → `pull`. Two failure modes this ordering avoids:
- **Push the jsonl before grooming** — the groomer reads a fresh *origin* worktree and bypasses unpushed local commits; an un-pushed entry is omitted from the regenerated INDEX and the local branch diverges.
- **Don't hand-edit INDEX/SPINE** — the groomer has a **both-or-neither scope guard** (INDEX and SPINE regenerate together): hand-editing only one (e.g. INDEX) leaves the change set asymmetric and the groomer aborts (`scope leak … reset and aborting`). The per-segment SPINE header also carries an entry-count a hand-appended row won't update. Let the groomer own both files.
- *Ref:* memory `reference_ledger_append_push_before_groom`; D10.

---

## Rollout & autonomy

### Ship-dark + supervised-first-fire
A new standing capability is built and tested **dark** (committed, inert — no live fire)
and then exercised once **under supervision** before being trusted autonomous. Soak
confidence afterward comes from a `not_before:`-gated ticket the inbox-consumer surfaces,
not from babysitting.
- *Refs:* `sop/standard/soak-verification-via-time-gated-ticket.md`; memory `feedback_dont_conflate_budget_with_work_risk` (ship-dark is safe to START regardless of budget).

### Failure-alert routing (others→Recovery, Recovery-self→the operator)
Per-agent failure alerts route via `inbox_consumer.route_failure_alert`: an alert about
another agent goes to Recovery; a Recovery-self alert goes to the operator. Pantheon-internal alerts
must NOT relay through the operator. Auto-resolve canned remediations; surface only novel cases.
- *Refs:* memories `feedback_failure_alert_routing_recovery_vs_self`, `feedback_auto_resolve_orchestrator_wedge_alerts`.

### Re-provision ALL agents after a templated-unit fix
Installed units are COPIES; a source/repo fix doesn't reach them until re-provisioned
per-agent. After fixing a templated unit, re-provision every agent and run a
`systemctl --user --failed` fleet sweep — the sweep is what catches the one you missed.
- *Ref:* memory `feedback_reprovision_all_agents_after_unit_fix`.
