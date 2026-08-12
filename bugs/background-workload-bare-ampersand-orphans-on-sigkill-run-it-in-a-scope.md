---
symptom: "A background/load/test workload an agent spawns with bare `&` (plus a `trap ... EXIT` to clean it up) SURVIVES the command that launched it and keeps running for hours — pinning cores, generating the exact sustained load that then reads as a mysterious 'flaky/load-sensitive' failure elsewhere. The parent shell shows PPID=systemd --user (it reparented — it detached from the agent's Bash-tool process), and `trap ... EXIT` never fired."
fix: "Run any ephemeral background/load/test workload inside a systemd TRANSIENT UNIT so it lives in its own cgroup and is reaped UNCONDITIONALLY on teardown — SIGKILL and orphaning cannot bypass a cgroup kill the way they bypass a shell trap. `systemd-run --user --scope --unit=<uniq> -- <cmd>` then `systemctl --user stop <uniq>.scope` (or let the scope's main process exit) kills the WHOLE tree. Never rely on `trap 'kill $PIDS' EXIT` for background children: traps do not run on SIGKILL, and children `&`-backgrounded from a shell that then dies get reparented to the user manager and outlive it. If you must use `&`, at minimum `setsid` + kill the process GROUP — but the cgroup/scope is the only unconditional reaper."
domain: "bash, background-jobs, ampersand, trap, sigkill, orphan, reparent, systemd-run, scope, cgroup, load, race-test, runaway, pdeathsig, pantheon"
---

# A background workload spawned with bare `&` orphans on SIGKILL — run it in a systemd scope

**Root cause of the 2026-08-06 T-2624 comms incident** (T-2628). A `--scope` stop-race test on
the `claude`/Orchestrator account generated CPU load the textbook way:

```bash
for i in $(seq 1 $(nproc)); do (while true; do :; done) & done
LOADPIDS=$(jobs -p)
trap "kill $LOADPIDS 2>/dev/null" EXIT       # <-- the load-bearing mistake
# ... run the actual race test ...
```

Three of these were still running **3h45m later** (PIDs 3416529/30/31), 94% CPU each, `loadavg
7.19` on a 4-core box. Their `trap … EXIT` never fired, so the `while true` loops survived and
pinned three of four cores — which is what made a *sibling* agent's boots go dark under load and
sent a multi-hour investigation chasing a "load-sensitive race" that was, in fact, self-inflicted
load.

## Cause

Two independent facts kill the trap-based cleanup:

1. **Traps do not run on `SIGKILL`.** When the launching process is `kill -9`'d (or the harness
   reaps a finished foreground command hard), the `EXIT` trap is skipped entirely.
2. **`&`-backgrounded children outlive their shell and reparent.** A subshell `(…) &` is not
   tied to the parent's lifetime; when the parent exits, the child reparents to the user
   manager (PPID = `systemd --user`) and keeps running as an orphan — invisible to any
   parent-scoped cleanup.

`trap 'kill $PIDS' EXIT` needs (a) a graceful exit and (b) the parent still owning the children.
A runaway defeats both.

## Fix

Put the workload in a **cgroup** — a systemd transient unit — so teardown is unconditional:

```bash
# scope: reaped when the scope stops OR its main process exits — whole tree, no exceptions
systemd-run --user --scope --unit=myload-$$ -- bash -c 'for i in $(seq 1 $(nproc)); do (while true; do :; done) & done; wait'
# ... run your test ...
systemctl --user stop myload-$$.scope      # SIGKILLs the entire cgroup, orphans included
```

- A **cgroup kill** cannot be bypassed by SIGKILL or by reparenting — every process in the
  cgroup dies, regardless of PPID.
- For the harness's own tracked background work, use `run_in_background: true` (harness-reaped on
  session end) and do **not** spawn bare `&` children *inside* that command — those escape the
  harness's process, exactly as above.
- Lesser tools, for reference: `PR_SET_PDEATHSIG` (a child asks the kernel to signal it when its
  parent dies — per-child, Linux-only) and `setsid` + `kill -<pgid>` (process-group kill, but an
  orphaned group still persists). The scope/cgroup is the only unconditional reaper.

## Verification

Reproduced 2026-08-06: three `while true` load parents (3416529/30/31) survived 3h45m past the
race-test that spawned them, PPID=1195 (`systemd --user`), `trap … EXIT` unfired; reaped by hand
(`kill -9`), `loadavg` fell from 7.19 → ~2 within a minute. Mechanism confirmed by construction:
`systemctl --user stop <unit>.scope` sends the cgroup a SIGKILL, so no member — orphan or not —
can survive it, whereas a `trap … EXIT` is skipped on the SIGKILL path that produced the runaway.

## Discovered

- T-2628 (this entry), from the T-2590/T-2624 cutover incident. The operator flagged the class
  (TG 7149) and the systemd-scope fix (TG 7150/7151). Related: `lessons/2026-08-06-a-stalled-investigation-is-usually-self-inflicted.md`
  (mode 3: your own leftovers are the confounder), `feedback_watchdogs_must_be_harness_tracked`.

## Keywords

bare ampersand background job, `while true` busy loop, `trap EXIT` didn't fire, SIGKILL bypasses
trap, orphaned process reparented to systemd --user, runaway load, loadavg pinned, systemd-run
--user --scope, transient unit cgroup teardown, reap whole process tree, PR_SET_PDEATHSIG, setsid
process group kill, self-inflicted load-sensitive failure.
