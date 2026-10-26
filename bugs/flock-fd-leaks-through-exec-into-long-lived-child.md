---
symptom: "A relaunch/respawn pattern that holds a flock lock for its whole run deadlocks on the SECOND fire: the first respawn's LONG-LIVED child (a new tmux server / daemon / claude session started via systemd-run/setsid/exec) inherited the parent's open flock fd, so the advisory lock stays held by that child for its entire life — long after the parent that took the lock has exited. The next run blocks on flock forever (or times out) against a lock nothing appears to hold."
fix: "Close the lock fd on the exec into the long-lived child: `9>&-` on the launcher command (bash), or open the lock fd O_CLOEXEC / set FD_CLOEXEC before exec. Short-lived children (systemctl, tmux send-keys, kill) can inherit it harmlessly — they exit and release it — so only the one long-lived respawn needs the close."
domain: flock
---

# flock fd leaks through `exec` into a long-lived child and holds the lock for that child's life

## Symptom

A script takes an flock on a lock fd (`exec 9>/tmp/foo.lock; flock 9`) to
serialize a relaunch, then `exec`s / `systemd-run`s / `setsid`s a **long-lived**
process (a new tmux server, a daemon, a fresh `claude` session). The first run
succeeds. The **second** run blocks on `flock` indefinitely — even though the
process that took the lock (the first run's script) has long since exited, and
nothing that looks like a lock holder is running.

Live case (T-3846, 2026-09-21): `pantheon-seat-relaunch.sh` holds
`flock` on `/tmp/pantheon-seat-relaunch_<persona>.lock` for the whole relaunch,
then respawns the seat's tmux server + `claude` via
`systemd-run --user --scope ...`. fd 9 (the lock) was inherited by the respawned
long-lived session. A seat's *first* auto-restart worked; its *second* deadlocked
on a lock held by the very session the second relaunch was trying to replace —
i.e. the seat could never auto-restart twice. Latent for **every** armed bespoke
seat (mason + artisan); surfaced on artisan's re-arm by timing, not because artisan was
special.

## Cause

Open file descriptors are inherited across `fork`+`exec` unless marked
close-on-exec (`FD_CLOEXEC`). A bash redirect like `exec 9>file` opens fd 9
**without** `O_CLOEXEC`, so every child — including one launched into a new
cgroup scope via `systemd-run`, or detached via `setsid` — starts with fd 9 open
on the lock file. An advisory `flock` is associated with the **open file
description**, not the process: as long as *any* process holds that fd open, the
lock is held. A long-lived child therefore holds the parent's lock for its own
entire lifetime, invisibly (the child never called `flock` and has no idea it is
pinning the lock). Short-lived children (`systemctl`, `tmux send-keys`,
`kill-server`) also inherit it, but harmlessly — they exit promptly and release
their copy.

## Fix

Close the fd on the exec into the long-lived child so it does not inherit the
lock. In bash, append `N>&-` to the launcher command:

```bash
# fd 9 is the flock lock; the respawned session must NOT inherit it
systemd-run --user --scope --unit "$scope" \
  tmux new-session -d -s "$sess" ... 9>&-
#                                     ^^^^ close fd 9 for THIS child only;
#                                          parent keeps the flock for the whole relaunch
```

`9>&-` closes fd 9 **for the child only** — the parent script keeps the lock for
the rest of the relaunch, exactly as intended. Only the one long-lived respawn
needs it; you do not have to (and should not) close it for the short-lived helper
calls. Equivalent fixes in other languages: open the lock fd with `O_CLOEXEC`, or
`fcntl(fd, F_SETFD, FD_CLOEXEC)` before the exec. Landed as `5fd9ffb22`.

## Verification

Reproduce the inheritance directly (a long-lived child keeps the lock; the second
acquirer blocks):

```bash
# holder keeps fd 3 open on the lock, backgrounds a long-lived child, then EXITS
( exec 3>/tmp/leak.lock; flock 3; sleep 300 & ) &   # child inherits fd 3
sleep 0.2
flock -w 2 /tmp/leak.lock -c 'echo GOT LOCK'         # -> times out: the sleeping child still holds it
# with the fix, background the child with 3>&- and this returns GOT LOCK immediately
```

Covered hermetically in `tools/test_pantheon_seat_relaunch.sh` (T-3846): the fake
`systemd-run` probes whether the lock fd is open in the child it receives and logs
`FD9_OPEN` / `FD9_CLOSED`; the happy-path test asserts `FD9_CLOSED`, and it was
shown to fail (`FD9_OPEN`) with the `9>&-` removed. 38 passed.

## See also

- `bugs/systemctl-is-active-misses-oneshot-in-activating-state.md` — the other
  relaunch-fence seam found in the same arc (a fence race, different mechanism).
- `feedback_mason_artisan_runtime_controller_managed_not_bare` (auto-memory) — why
  a broken seat relaunch is especially costly on these controller-managed seats.
