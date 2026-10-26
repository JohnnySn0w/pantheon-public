# `systemctl is-active` misreads Type=oneshot units, in every direction

**Date authored:** 2026-09-27
**Author:** Orchestrator (Orchestrator), from Specialist's 2026-09-25 knowledge sweep (T-4010)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a fourth instance surfaces, or a shared probe helper lands that makes the raw call unnecessary.

## Observation

`is-active` answers one question: is ActiveState exactly `active`? A `Type=oneshot` unit reports `activating` for the whole of its run, then either `active (exited)` (with `RemainAfterExit=yes`) or `inactive`. So `is-active` gives the wrong answer for a oneshot in whichever direction the caller didn't think about:

- **False green.** `active (exited)` reads as healthy after the oneshot has returned, even when the thing it spawned has since died.
- **False red.** A oneshot without `RemainAfterExit` is never `is-active` once it exits, so a liveness probe FAILs a healthy seat.
- **False idle.** A oneshot mid-run is `activating`, not `active`, so a drain or fence that asks "is it running?" misses an in-flight run and races it.

Three authors, three tools, three directions, none cross-cited until the sweep.

**Rule:** probe a oneshot with `systemctl [--user] show <unit> -p ActiveState,SubState,ExecMainPID`. Treat `activating` as running. Treat `active (exited)` as "the start command returned", which says nothing about anything it spawned. For persistent liveness of a timer-driven oneshot, probe the TIMER (which stays `active` while armed), not the service.

## Grounding

1. **False green:** `incidents/2026-05-21-recovery-personal-daemon-silent-outage/`. `is-active` reported `active (exited)` while the tmux+claude process the unit had spawned was gone. Also listed as the process-layer example in `lessons/2026-07-21-wrong-layer-green-liveness-checks.md`.
2. **False red:** T-2658 / T-2685. `tools/restart_drain.py` (~187: "a Type=oneshot consume unit is never is-active after it exits") and `tools/fleet_health.py`'s probe-unit selection (~76-86), which switched control-plane seats to probing the consume TIMER.
3. **False idle:** T-3846, `bugs/systemctl-is-active-misses-oneshot-in-activating-state.md`. The relaunch fence missed an in-flight consume and raced the respawn.

Related mechanism: `bugs/systemd-oneshot-kills-daemon-it-spawns-cgroup-teardown.md` (why a oneshot's spawned child is fragile in the first place).

Current callers checked by the sweep (channel_liveness, parity_remediate, fleet_snapshot, kino): no live misuse.

## Design implications

- New liveness or fence code that touches a oneshot uses `show -p ActiveState,SubState,...`, never bare `is-active`. Reviewers flag a bare `is-active` against a unit whose `Type=` they haven't checked.
- "Is the unit OK?" and "is the work it started OK?" are different questions for a oneshot. The second needs a probe at the spawned process's own layer (the wrong-layer-green lesson).

## Open questions

- Whether a small shared helper (e.g. in `tools/_systemd_utils` or similar) should own this, so the rule is structural rather than remembered. Three hand-rolled fixes so far argue yes; no fourth caller is pending.

## Cross-references

- `lessons/2026-07-21-wrong-layer-green-liveness-checks.md`: the broader liveness-at-the-wrong-layer pattern this is one face of.
- `bugs/systemctl-is-active-misses-oneshot-in-activating-state.md`, `bugs/systemd-oneshot-kills-daemon-it-spawns-cgroup-teardown.md`.
- T-2658, T-2685, T-3846; sweep T-3993 / T-3996 / T-4010.
