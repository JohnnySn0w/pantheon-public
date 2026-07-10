---
symptom: "Counting running daemon instances with `pgrep -f '<cmdline>'` inflates the count by +1 — the pattern also matches the operator's own interactive shell (and the pgrep/pkill invocation itself). `pkill -f '<cmdline>'` then kills the operator's shell. Caused a phantom duplicate-daemon / respawn debugging saga."
fix: "Count and signal daemons by process NAME: `pgrep -x <name>` / `pkill -x <name>` (exact-name match), or use `systemctl` for managed units. Never use `-f <cmdline>` for liveness counts — `-f` matches the full command line of every process, including your own shell."
domain: linux, process-management, pgrep, pkill, systemd, echo, deploy
---

## Symptom

Checking "how many `echo` daemons are running" with `pgrep -f 'echo'` (or any cmdline substring) returns N+1. The phantom +1 is the operator's own interactive shell whose argv/history contains the search string, plus potentially the `pgrep`/`pkill` process itself. Worse: `pkill -f '<cmdline>'` to "clean up duplicates" kills the operator's own shell. During the T-0557 echo deploy this produced a duplicate-daemon / phantom-respawn debugging saga (chasing a duplicate that didn't exist, then killing the wrong process).

## Cause

`pgrep -f` / `pkill -f` match against the **full command line** of every process — not just the daemon's. The operator's interactive shell, the deploy script, and the matching command itself all carry the cmdline substring in their argv. `-x` matches the **exact process name** only, which is what a liveness count actually wants.

## Fix

```
pgrep -x echo            # exact process-name count — no shell false-positive
pkill -x echo            # signal by exact name
systemctl status <unit>  # preferred for systemd-managed daemons
```

For managed daemons, prefer `systemctl status/stop/restart <unit>` over signal-by-pattern entirely. Reserve `-f` for cases where you genuinely need to match on arguments, and then exclude self/shell explicitly.

## Discovered

- T-0557 echo deploy (2026-05-22). Postmortem: `notes/2026-05-22-echo-demo-integration-postmortem.md` §Reusable disciplines. Mentioned in passing in T-0562 but never cataloged until now.
- Filed from ORG-012 sweep T-0874 (parent T-0869).

## Keywords

pgrep, pkill, -f, -x, cmdline, process name, daemon count, +1 inflation, kill operator shell, phantom respawn, duplicate daemon, systemctl, T-0557, echo, deploy
