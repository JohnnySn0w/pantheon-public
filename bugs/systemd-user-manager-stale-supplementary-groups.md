---
symptom: "after adding a unix user to a new group (e.g. veneration), that user's running systemd --user manager — and every timer/service it spawns — still carries the OLD supplementary-group set; group-gated paths return Permission denied until the manager is restarted"
fix: "restart the user manager so it re-reads the group set: `sudo systemctl restart user@<uid>.service` (DESTRUCTIVE for an agent whose live session/poller is hosted there — see caveat). In-session workaround that needs no manager restart: `sg <group> -c '<cmd>'` (works without a password when the user is already in /etc/group for that group)."
domain: systemd
---

## Symptom

A unix user is added to a new group — e.g. `claude` joins `veneration` for the `/home/the-ancestor` mailbox hub (ORG-026):

```
$ id claude        # /etc/group now lists veneration
... groups=...,1004(veneration)
```

But a process spawned by that user's *running* `systemd --user` manager still can't traverse the group-gated path:

```
$ ls /home/the-ancestor/mailbox/orchestrator/new/
ls: cannot access '.../new/': Permission denied
$ id | grep -o veneration      # live process group set — empty
```

`id <user>` (which reads `/etc/group` fresh) shows the new group; the *live* process's group set (inherited from the manager at its start time) does not. Every timer/service the manager spawns inherits the same stale set.

## Cause

A process's supplementary-group set is fixed at process creation (from `initgroups()` / the credentials it was launched with). The `systemd --user` manager (`user@<uid>.service`) is long-lived — it started once (often at boot / first login-linger) and carries the group set from *that* moment. Adding the user to a new group in `/etc/group` updates the database but does **not** retroactively inject the group into already-running processes. The manager, and therefore every unit it forks, keeps the old set until the manager itself re-execs or restarts.

Verified 2026-06-09: `orchestrator`'s manager (uid 1000, pid from Apr 26) and `codex`'s (uid 1003) both lacked `veneration` in their live group set despite being in `/etc/group`; `specialist`'s was fixed by a root `systemctl restart user@1002.service` (T-1016).

## Fix

**Durable (when safe):** restart the user manager so it re-reads the group set:

```
sudo systemctl restart user@<uid>.service
```

This re-execs the manager with fresh credentials; subsequently-spawned timers/services carry the new group.

**CAVEAT — destructive for a session-hosting manager.** If `user@<uid>.service` hosts a live interactive agent session and/or the Telegram poller (true for `orchestrator` = uid 1000), restarting it kills that session → comms-dark. Do NOT do this mid-session for such an agent. This is the exact gap a *graceful self-restart* mechanism fills (drain → handoff-refresh → re-exec picking up new groups) — tracked in **T-0309** (the veneration-reload use-case is its named Phase-1 acceptance scenario).

**In-session workaround (no manager restart needed):** wrap the group-gated command in `sg`:

```
sg veneration -c '<command that touches the group-gated path>'
```

`sg` launches a new process with the target group active. It needs **no password** when the user is already in `/etc/group` for that group (the membership exists; only the *running process* lacks it). This is how an orchestrator session with a stale manager still consumes hub mailbox messages — every hub op just has to remember the wrap.

## Discovered

- T-1016 (ORG-026 hub rollout, 2026-06-09) — specialist's inbox-consumer + mailbox ops hit Permission denied on the veneration-gated hub; fixed via root `systemctl restart user@1002.service`.
- Re-confirmed same session for orchestrator + codex (managers predate the veneration grant). orchestrator's couldn't be manager-restarted (hosts the live session + poller) → `sg veneration` wrap adopted as the standing in-session workaround; durable self-restart tracked in T-0309.

## Keywords

systemd --user, user@.service, supplementary groups, initgroups, stale group set, Permission denied, veneration, sg command, newgrp, group membership not picked up, manager restart, ORG-026 hub mailbox, T-0309, T-1016, login linger
