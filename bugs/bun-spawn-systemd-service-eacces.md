---
symptom: "Bun.spawn returns EACCES when called from a bun-on-systemd service after the daemon's module-import graph loads"
fix: "pre-spawn privileged ops from a bash launcher script, then `exec` the bun daemon — takes Bun.spawn out of the runtime hot path"
domain: bun, systemd
---

> **Superseded as load-bearing by** [`sop/remediation/socket-activated-agent-daemon-gotchas.md`](../sop/remediation/socket-activated-agent-daemon-gotchas.md) — the live remediation guidance these symptoms taught now lives in that SOP (ORG-012 supersedure-trail, T-0832 PR-2 / T-0853). This entry remains the recorded symptom + diagnosis; act on the SOP for current guidance.

## Symptom

A bun service running under systemd (e.g. `ExecStart=/path/to/bun run /path/to/main.ts` with `User=<some-user>`) tries to call `Bun.spawn(...)` to invoke `systemctl`, `sudo`, or any other privileged binary. The spawn call fails with `EACCES`. The same `Bun.spawn` call works fine from an interactive bash shell as the same user. Strace shows the EACCES coming from `posix_spawn`.

Triggers AFTER the daemon's module-import graph has fully loaded — single inline `Bun.spawn` ops at the top of `main.ts` may work, but the same call deeper in the daemon (inside an HTTP handler, IPC handler, etc) fails.

## Cause

Unidentified after exhaustive triage:
- sudoers config is correct (verified — same user can sudo from bash).
- AppArmor inactive.
- `NoNewPrivileges=0` on the systemd unit.
- Mount namespace clean (no read-only `/usr/bin`).
- Bun.spawn `uid` / `gid` options silently no-op (separate verified pattern — they don't even attempt the syscall).
- bash-wrap inline (`bun -e 'Bun.spawn(["bash", "-c", "systemctl start ..."])'`) works.
- bash-wrap from production daemon fails identically.

The pattern bisect (Tests A-J at kino Phase 1b deploy) proved the daemon's loaded-module-graph state is the trigger. Likely a Bun runtime restriction triggered by something in the module graph (loaded SQLite native module? Loaded crypto? unclear). Not yet fixed upstream as of Bun 1.3.13.

## Fix

Don't put privileged spawns in the bun daemon's runtime path. Pre-spawn from a bash launcher script that runs the privileged ops *before* `exec`'ing the bun daemon:

```bash
#!/bin/bash
# /usr/local/bin/<thing>-launcher.sh
set -e
systemctl start <subservice-1>.service
systemctl start <subservice-2>.service
exec /path/to/.bun/bin/bun run /path/to/main.ts
```

systemd unit's `ExecStart` points to the launcher, not directly to bun:

```ini
[Service]
ExecStart=/usr/local/bin/<thing>-launcher.sh
```

The launcher runs in bash (no Bun.spawn restriction), the bun daemon runs after `exec` replaces the process. Side-effect: the daemon never needs spawn capability for the privileged ops — they happen at startup, before bun loads. This is also a cleaner architectural shape (privileged setup separated from runtime).

If you need to re-fire privileged ops at runtime (e.g. spawn a new subservice on demand), you have to either restart the parent service or use a different IPC pattern (write a request file, have a separate dispatcher process consume it, etc). Within-daemon Bun.spawn for privileged ops is structurally not viable in this Bun version.

## Discovered

- T-0100 + T-0104 (kino Phase 1b cross-UID architecture) — initially had kino-daemon issuing `systemctl start kino-agent@<user>.service` at runtime via Bun.spawn; broke at deploy with persistent EACCES. ~3 hours of bisect-and-pivot before reaching the launcher pattern.
- pantheon-pwa commit 0c0f071 (kino-launcher.sh pre-spawn pivot), 2026-05-06.
- ORG-006 §Phase-1-implementation-addendum §Deviation-1 captures the architectural shape.
- T-0096 Kerfuffle 12.

## Keywords

bun, Bun.spawn, posix_spawn, EACCES, systemd, ExecStart, sudo, systemctl, launcher, exec, daemon, kino, cross-uid, privileged spawn, module graph, can't spawn, permission denied bun
