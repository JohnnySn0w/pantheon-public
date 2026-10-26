---
symptom: "a Type=oneshot service starts a tmux server (or any daemon) but it's gone the instant ExecStart returns; on-disk state (runtime.json) persists yet 'no server running'"
fix: "launch the daemon inside `systemd-run --user --scope` so it re-parents to the user manager, outside the oneshot's control-group"
domain: "systemd"
---
## Symptom

A `Type=oneshot` user service starts a long-lived daemon during `ExecStart` — e.g. `runtime_controller.ensure_runtime` doing `tmux new-session` / `respawn-pane`. The unit reports `Result=success`, `ExecMainStatus=0`, and the runtime state file (`runtime.json`) is written to disk — but there is **no live server**. A follow-up liveness probe (`swap_before`, `tmux has-session`) reports `REFUSE: no server running`. The daemon vanished the moment the oneshot's `ExecStart` returned.

## Cause

Default `KillMode=control-group`. A process spawned by `ExecStart` stays in the service's cgroup — **tmux's double-fork daemonization does NOT escape the cgroup** (cgroup membership is not shed by `fork`). When the oneshot's `ExecStart` returns, systemd considers the unit done and tears down its control-group with `SIGTERM`. Anything the daemon wrote to disk *before* exit survives (hence `runtime.json` persists), but the process itself is killed. The signature — "state persisted, server dead" — is the tell.

The same mechanism bites in reverse: stopping such a service (`systemctl --user stop`) also kills any tmux/daemon it had launched, because that daemon lives in the service cgroup (observed quieting Recovery: stopping `claude-telegram-fallback.service` dropped its `claude-telegram-bot-fallback` tmux).

## Fix

Launch the daemon inside a transient scope registered with the **user manager**, not the caller's cgroup:

```
systemd-run --user --scope --quiet --unit=pantheon-runtime-tmux.scope \
  tmux new-session -d -s <session> ...
```

The scope re-parents to `user@<uid>.service/app.slice/<name>.scope` and **survives the parent oneshot exit** (verified: server PID unchanged and still in the scope after `ExecStart` returns). Record the scope unit in `runtime.json` and anchor liveness on the **tmux SERVER pid's** cgroup — panes self-isolate into their own auto-created `tmux-spawn-<uuid>.scope`, so a pane's cgroup is NOT the runtime-liveness anchor.

Do NOT try to "fix" this with `KillMode=process` alone — it stops the oneshot from reaping the child, but leaves the daemon un-scoped and unmanaged. A dedicated `systemd-run --scope` (or a separate long-lived owner unit) is the clean separation: a wake/consume oneshot should not be the owner of a long-lived runtime.

## Also seen: a git hook's detached `( … ) &` push, killed mid-flight (T-4097, 2026-09-28)

Same mechanism, less visible because nothing is supposed to persist. A `Type=oneshot` ingest driver (`supernote-ingest.service` on LXC 203) commits to a store whose **post-commit hook pushes to an off-box backup in a detached background subshell** (`( git push backup --all … ) &`). The driver exits about 1s after committing, and the cgroup teardown kills the push. Keywords: post-commit hook, background push, backup never updated, git push killed, systemd oneshot.

- **Tell:** the hook's log has **no entry at all** for a driver commit, or a remote update line with no trailing "push ok" (killed mid-push). The backup catches up only when a later **manual** commit's hook push carries the older commits along. So the lag is invisible unless you compare `git ls-remote backup` with local HEAD after a driver-only commit.
- **Fix here (not a scope):** the unit does the push **synchronously** before exiting (`timeout 120 git push backup --all`, then `--tags` as a separate command, since git refuses `--all` with `--tags`). A failure is logged and fails the unit. The driver's own commits **skip the hook** (`git -c core.hooksPath=/dev/null commit`).
- **Why skip the hook:** a hook push and the driver push of the same ref overlap, and **the loser is rejected** (`cannot lock ref 'refs/heads/main': is at <new> but expected <old>`, measured with a 2s pre-receive). Keeping both would fail the unit spuriously on commits that did land.
- The hook stays for manual commits.

## Verification

- tmux case: see T-2382 refs below.
- git-hook case: on 203, `tail ~/.local/state/memex-backup-push.log` showed no entry for driver commit `0092452`, while `git ls-remote backup -h refs/heads/main` returned `ca203e3` with HEAD at `0092452`. A manual `git push backup --all` then went `ca203e3..0092452`. `systemctl --user show supernote-ingest.service -p KillMode` returned `KillMode=control-group`.

## Refs

T-2382 (three live-swap aborts on `swap_before REFUSE no server running`; fix commit `4aa5c58b` / landed `e175325ec4` "keep persona tmux server outside oneshot cgroup"). Off-prod scope-feasibility gate + rehearsal proof: ledger `20260804T033600Z`. The consume unit is `pantheon-persona-consume@.service` (`Type=oneshot`), driven by `persona_bringup.py consume` → `wake_persistent_runtime` → `runtime_controller.ensure_runtime`.
