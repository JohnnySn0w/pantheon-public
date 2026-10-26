---
symptom: "`systemctl is-active --quiet <unit>` returns NON-zero (reads 'not active') for a Type=oneshot service that is actually RUNNING — because a oneshot mid-ExecStart is ActiveState=activating, and never reaches 'active'. Any guard that uses is-active to detect an in-flight oneshot (e.g. a fence checking 'is a consume run in flight before I kill the session?') silently never fires, and the in-flight run races whatever the guard was protecting."
fix: "Detect an in-flight run via `systemctl --user show <unit> -p ActiveState --value` and treat BOTH 'active' AND 'activating' as in-flight — never `is-active` alone. For a drain/fence, poll that until it is neither."
domain: systemd
---

# `systemctl is-active` misses a Type=oneshot service in the `activating` state

## Symptom

A guard meant to detect "is this service currently running?" uses
`systemctl --user is-active --quiet <unit>.service` and treats exit-0 as
"running". For a **Type=oneshot** service it NEVER sees the running instance:
`is-active` on a oneshot mid-`ExecStart` reports `activating` (exit non-zero for
`--quiet`), not `active`. The guard reads "not running", proceeds, and the
still-executing run races whatever came next.

Live case (T-3846, 2026-09-21): `pantheon-seat-relaunch.sh`'s fence checked
`is-active --quiet pantheon-persona-consume@artisan.service` to abort if a consume
run was in flight before kill+respawn. `persona-consume@` is `Type=oneshot`. The
consume timer fired the service the same instant the fence masked the timer; the
in-flight oneshot was `activating` (not `active`), so the `is-active` check passed
("not active"), the fence proceeded, and the consume run's controller executed
~4s later — AFTER the respawn — saw the new scope active, hit
`runtime_controller._prepare_runtime_scope`'s active-scope refusal, and blanked
`runtime.json` (the T-3819 teardown mode; see
`feedback_mason_artisan_runtime_controller_managed_not_bare`).

## Cause

A `Type=oneshot` unit's ActiveState lifecycle is `inactive → activating (while
ExecStart runs) → inactive (on success)` — it passes through **`activating`** and
lands back on **`inactive`**, and (unless `RemainAfterExit=yes`) is **never**
`active`. `systemctl is-active` matches only the literal `active` state; `--quiet`
exits 0 only for `active`. So `is-active` cannot observe a running oneshot at all.
(A long-running `Type=simple`/`notify` service DOES reach `active`, which is why
this trap is specific to oneshot — and easy to miss when the same guard pattern
worked for a simple service elsewhere.)

## Fix

Read the state explicitly and accept both transient-running states:

```bash
astate="$(systemctl --user show "$svc" -p ActiveState --value 2>/dev/null)"
if [[ "$astate" == "active" || "$astate" == "activating" ]]; then
  # the unit is in flight (covers a oneshot mid-ExecStart, which is 'activating')
  ...
fi
```

For a fence that must not race an in-flight run, DRAIN rather than abort-blind:
mask the unit (blocks NEW starts — masking does NOT stop a running instance), then
poll `ActiveState` until it is neither `active` nor `activating` (the in-flight run
finished on its own — no need to SIGTERM it, which would risk a mid-write partial
state). Bound the wait; abort+unmask if it will not drain. This is exactly what
`pantheon-seat-relaunch.sh:fence_consume_timer` does post-T-3846.

## Verification

The state a running oneshot actually reports (run against any Type=oneshot unit
whose ExecStart is briefly slow, on its own manager):

```bash
# while the oneshot's ExecStart is executing:
systemctl --user is-active --quiet foo.service; echo "is-active rc=$?"   # -> rc=3 (reads NOT active) — the trap
systemctl --user show foo.service -p ActiveState --value                 # -> activating — the truth
```

Covered hermetically in `tools/test_pantheon_seat_relaunch.sh` (T-3846): the fake
`systemctl` models a consume that is `activating` for N polls then drains; the
"fence DRAINS an in-flight consume then proceeds" test asserts the drain path runs
(non-vacuous — shown to fail when the drain loop is neutered), and the
"fence-abort when in-flight consume won't drain" test covers the bounded-timeout
abort.
