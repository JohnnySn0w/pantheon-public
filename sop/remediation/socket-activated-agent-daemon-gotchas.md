# Socket-activated / socat-bridged / Bun-spawned agent daemon gotchas

**Status:** **Ratified 2026-06-07 by the operator.** Drafted 2026-06-05 by Specialist (T-0832, consolidating PR-2 from T-0780 sweep);

**Purpose:** Remediation reference for the five-bug arc discovered during kino Phase 1b–2.7 (T-0096 through T-0114). Covers socket-activated systemd services that bridge IPC via socat and spawn subprocesses from a Bun daemon — a distinct operational pattern from the tmux-supervise-script daemons documented in [`../standard/pantheon-bringup.md`](../standard/pantheon-bringup.md) §4. If your failure is in a `Type=simple` + supervise-script + `Restart=always` unit (the standard agent-daemon shape), the bringup SOP §4 + §4.2 is the right surface; this SOP does not apply there.

**Scope:** applies to units of the shape:
- A `.socket` unit that activates a `.service` on first connection.
- The `.service` runs `socat ACCEPT-FD:3 EXEC:"/path/to/binary …"` to bridge the systemd-passed listening fd to a subprocess's pipe-stdio.
- The parent daemon is a Bun process (`ExecStart=… bun run main.ts` or equivalent) that may need to spawn privileged subprocesses.

**Source bugs (consolidated):**
- [`../../bugs/systemd-socket-activation-socat-acceptfd-bridge.md`](../../bugs/systemd-socket-activation-socat-acceptfd-bridge.md)
- [`../../bugs/systemd-environment-whitespace-split.md`](../../bugs/systemd-environment-whitespace-split.md)
- [`../../bugs/systemd-execstart-env-var-indirection-vs-socat.md`](../../bugs/systemd-execstart-env-var-indirection-vs-socat.md)
- [`../../bugs/systemd-socket-activated-service-bounces-back-after-stop.md`](../../bugs/systemd-socket-activated-service-bounces-back-after-stop.md)
- [`../../bugs/bun-spawn-systemd-service-eacces.md`](../../bugs/bun-spawn-systemd-service-eacces.md)

---

## Common thread

All five bugs share the same root shape: **systemd's abstractions hide a layer boundary that the downstream component (socat, the subprocess, the kernel, the Bun runtime) operates at a lower level than you think.** `StandardInput=socket` hides the listen-vs-connected fd distinction. `Environment=` hides its whitespace-split parser. `${VAR}` in a double-quoted `EXEC:` arg hides socat's internal re-tokenization. Socket activation hides that the socket, not the service, is the respawn trigger. Systemd's process context hides the Bun runtime restriction on post-import-graph spawning. Each fix works by removing the abstraction leak: use socat as an explicit bridge, inline the value, clear-and-reset the env-var directive, stop the socket before the service, and pre-spawn from a launcher before exec-ing Bun.

The arc tells you: when a socket-activated socat-bridged unit misbehaves, distrust every intermediate layer and verify each boundary independently.

---

## Gotcha 1 — `StandardInput=socket` crashes the service immediately (start-limit-hit)

**Symptom:** The socket-activated service exits almost instantly on every connection attempt. `systemctl status` shows `Active: failed (Result: start-limit-hit)`. The activated binary (`claude mcp serve` or any other stdio-pipe-speaking binary) emits no useful error; the crash is too fast to log.

**Root cause:** `StandardInput=socket` (with the default `Accept=no` on the `.socket` unit) passes the *listening* socket fd as stdin — not a connected socket. A stdio-pipe binary tries `read(0, …)` against a listening fd and immediately fails (EINVAL or confused-crash). `StandardInput=socket` only works for `Accept=yes` (per-connection fork, systemd accepts each connection itself) or for binaries that call `sd_listen_fds()` and call `accept()` themselves. Neither applies to MCP/CLI-style binaries.

**Fix:** Remove `StandardInput=socket` / `StandardOutput=socket` from the unit entirely. Use socat as the explicit bridge instead:

```ini
[Service]
Type=simple
ExecStart=/usr/bin/socat ACCEPT-FD:3 EXEC:"/path/to/binary args"
Sockets=foo@%i.socket
StandardError=journal
# StandardInput= and StandardOutput= left at defaults — socat owns the fd
```

socat's `ACCEPT-FD:3` reads the systemd-passed listening fd (fd 3 = `LISTEN_FDS=1`), calls `accept()`, and pipes the connected stream to the subprocess's stdin/stdout. The binary sees normal pipe-stdio; the socat bridge is invisible to the connecting client.

Verify socat version supports `ACCEPT-FD`:
```sh
apt-get install -y socat
socat -hh | grep ACCEPT-FD   # requires socat 1.8+
```

Socket file ownership and permissions remain managed by the `.socket` unit (`SocketUser=`, `SocketGroup=`, `SocketMode=`).

**Source bug:** [`../../bugs/systemd-socket-activation-socat-acceptfd-bridge.md`](../../bugs/systemd-socket-activation-socat-acceptfd-bridge.md) (T-0104, T-0096 Kerfuffle 13).

---

## Gotcha 2 — `Environment=` value with whitespace silently truncates

**Symptom:** A unit sets `Environment=KEY=value with spaces`. At process exec time, only `KEY=value` is set; the tokens after the first space become orphan entries (silently dropped). The downstream binary receives a truncated flag value and errors — e.g. `claude -p` exits with `option '--session-id <uuid>' argument missing` despite the UUID being visible in `systemctl show -p Environment`.

**Root cause:** systemd's `Environment=` parser splits on whitespace at the directive level. Each space-separated token must be a complete `KEY=value` pair. A value containing whitespace is therefore interpreted as multiple env entries; the second token becomes an orphan key with no `=` and is dropped. The `man systemd.exec` "may be used" language about quoting is misleading — quoting is *required* for any whitespace-containing value, not optional.

**Fix:** Wrap the entire `KEY=value` span in double quotes:

```ini
[Service]
Environment="KINO_SESSION_FLAG=--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a"
```

After reload, `systemctl show <unit> -p Environment` will show the quoted form, which signals atomic-value treatment. The value is preserved in full at exec time.

**Diagnostic:** if `systemctl show -p Environment` shows the full string but the process still misbehaves, see Gotcha 3 — the env-var itself may be set correctly but the socat EXEC: indirection is breaking downstream.

**Source bug:** [`../../bugs/systemd-environment-whitespace-split.md`](../../bugs/systemd-environment-whitespace-split.md) (T-0114, pantheon-pwa c54b0bd).

---

## Gotcha 3 — `${VAR}` inside socat `EXEC:"…"` produces malformed subprocess argv

**Symptom:** `Environment=` is correctly set (Gotcha 2 fixed, `systemctl show -p Environment` confirms the value). `systemctl show -p ExecStart` shows `${KINO_SESSION_FLAG}` unexpanded (expected — expansion happens at exec time, not at show-time). At exec time the subprocess still receives malformed argv — e.g. `--session-id` with the UUID stripped. Direct invocation of the same command as the unix user with the env-var manually exported works fine. Iterating quoting and escape patterns inside the `EXEC:` arg does not resolve it.

**Root cause:** Not fully root-caused. Hypothesis: within a double-quoted `EXEC:"…"` string, systemd expands `${VAR}` but the quoted outer span causes the expanded result to be delivered to socat as a single argv token. socat's own `EXEC:` parser must then re-split the string to build the subprocess argv. The interaction of "systemd expanded one chunk" + "socat re-tokenizes the whole chunk" produces incorrect token boundaries that confuse the CLI flag parser.

**Fix:** Drop the env-var indirection entirely. Write the per-instance value directly into the `ExecStart` line via a per-instance drop-in that overrides `ExecStart`:

```ini
# /etc/systemd/system/kino-agent@claude.service.d/session-id.conf
[Service]
ExecStart=
ExecStart=/usr/bin/socat ACCEPT-FD:3 EXEC:"/path/to/binary -p ... --session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a"
```

The empty `ExecStart=` clears the inherited base value; the second `ExecStart=` sets the override. The per-instance value is now a literal in the unit definition — no env-var expansion, no socat re-tokenization edge case. For systems where a setup script already writes per-instance drop-ins (e.g. via `ExecStartPre=+`), this is no extra work.

**Cost:** the drop-in must be regenerated whenever the per-instance value changes. Within-daemon dynamic substitution via env-vars is not viable with this socat EXEC: shape.

**Together with Gotcha 2:** after resolving both, the correct settled pattern for per-instance values is per-instance `ExecStart=` override (inlined literal), NOT `Environment=` + `${VAR}` substitution.

**Source bug:** [`../../bugs/systemd-execstart-env-var-indirection-vs-socat.md`](../../bugs/systemd-execstart-env-var-indirection-vs-socat.md) (T-0114, pantheon-pwa 65f6a77).

---

## Gotcha 4 — `systemctl stop <service>` bounces back active immediately

**Symptom:** `systemctl stop kino-agent@claude.service` emits:

```
Stopping 'kino-agent@claude.service', but its triggering units are still active:
kino-agent@claude.socket
```

After the stop command returns, `systemctl is-active kino-agent@claude.service` still reports `active`. A queued or inflight connection retriggered the socket between the service-stop and the socket-stop, respawning the service.

If the wrong-order pass already ran and the service was killed mid-run (non-zero exit), `systemctl is-active` may report `failed` rather than `active`.

**Root cause:** In socket activation, the `.socket` unit is the respawn source of truth. Stopping the `.service` while the `.socket` is still listening simply hands the work back to the socket on the next `accept()`. The service is never truly stopped as long as its triggering socket is live.

**Fix:** Stop and disable the `.socket` first, then stop the `.service`:

```sh
for u in claude claude-research claude-fallback; do
  sudo systemctl stop    kino-agent@$u.socket
  sudo systemctl disable kino-agent@$u.socket
  sudo systemctl stop    kino-agent@$u.service
done
```

If the first (wrong-order) attempt already tripped the service into `failed` state:

```sh
sudo systemctl reset-failed kino-agent@$u.service
```

`reset-failed` moves the unit from `failed` to `inactive (dead)` without restarting it — standard acceptance criterion for a clean teardown.

**General rule:** for any socket-activated unit, the teardown order is always: disable socket → stop socket → stop service.

**Source bug:** [`../../bugs/systemd-socket-activated-service-bounces-back-after-stop.md`](../../bugs/systemd-socket-activated-service-bounces-back-after-stop.md) (T-0353, pantheon-config f07d47c).

---

## Gotcha 5 — Bun.spawn returns EACCES from inside the systemd service

**Symptom:** A Bun daemon running under systemd (`ExecStart=… bun run main.ts`) calls `Bun.spawn(["systemctl", "start", …])` or `Bun.spawn(["sudo", …])` and gets `EACCES`. The identical `Bun.spawn` call works from an interactive bash shell as the same user. Strace shows the EACCES at `posix_spawn`. The failure occurs inside handlers (HTTP, IPC) — i.e., after the module import graph has fully loaded; inline spawns early in `main.ts` before full graph load may work.

Ruled out: sudoers correct, AppArmor inactive, `NoNewPrivileges=0`, clean mount namespace, `bash -c` wrap (fails identically).

**Root cause:** Unidentified as of Bun 1.3.13. The pattern bisect (Tests A–J, kino Phase 1b) proved the loaded-module-graph state is the trigger. Likely a Bun runtime restriction activated by something in the loaded module graph (SQLite native module, crypto, or similar). Not fixed upstream.

**Fix:** Move the privileged spawn out of the Bun daemon's runtime path. Use a bash launcher script that performs the privileged operations before `exec`-ing the Bun daemon:

```bash
#!/bin/bash
# /usr/local/bin/<thing>-launcher.sh
set -e
systemctl start <subservice-1>.service
systemctl start <subservice-2>.service
exec /path/to/.bun/bin/bun run /path/to/main.ts
```

Point the systemd unit's `ExecStart` at the launcher, not directly at bun:

```ini
[Service]
ExecStart=/usr/local/bin/<thing>-launcher.sh
```

The launcher runs under bash (no Bun spawn restriction). The `exec` replaces the launcher process with bun, so systemd's MainPID tracking is correct. Privileged setup is separated from the runtime path — a cleaner architectural shape regardless of the Bun restriction.

**Runtime spawns:** if you need to trigger privileged operations at runtime (not just at startup), `Bun.spawn` is not viable. Options: restart the parent service, or use an out-of-band IPC pattern (write a request file; have a separate dispatcher process consume it). Within-daemon `Bun.spawn` for privileged operations is structurally blocked in this Bun version.

**Source bug:** [`../../bugs/bun-spawn-systemd-service-eacces.md`](../../bugs/bun-spawn-systemd-service-eacces.md) (T-0100, T-0104, pantheon-pwa 0c0f071; ORG-006 §Phase-1-implementation-addendum §Deviation-1).

---

## Recommended remediation order (fresh deploy or bringup)

When standing up a new socket-activated socat-bridged daemon from scratch, address these in order to avoid the full arc:

1. Wire the socat bridge first (Gotcha 1) — validate a basic connection before adding per-instance configuration.
2. If per-instance values are needed (session IDs, UUIDs), write them as inlined drop-in `ExecStart=` literals (Gotchas 2 + 3 resolved by design — never reach them).
3. For Bun parent daemons, use a bash launcher (Gotcha 5) from the start — don't rely on `Bun.spawn` for any privileged ops.
4. When tearing down for redeploy, always stop socket before service (Gotcha 4) — the warning systemd emits is an early signal you have the order wrong.

## Autonomous-vs-bubble-up (D3) placement

- **Autonomous:** applying the socat bridge fix, rewriting per-instance drop-ins as inlined values, adding a bash launcher, and using the correct socket-first teardown order — all are reversible, well-precedented, and directly documented here.
- **Bubble up to the operator:** if `Bun.spawn` EACCES appears on a Bun version newer than 1.3.13 (upstream fix may have landed — verify first; the cause is unidentified so a fix could be silent), or if the socat `ACCEPT-FD:3` approach fails on a socat version earlier than 1.8 (pre-dates the flag — upgrade socat before investigating further). Any deviation from these patterns that requires editing `principles.md`, `directives.md`, or unit files outside the per-instance drop-in layer needs the operator sign-off.

## Cross-references

- Contrasting tmux-daemon pattern: [`../standard/pantheon-bringup.md`](../standard/pantheon-bringup.md) §4 (the supervise-script + `Type=simple` + `Restart=always` shape; not socket-activated).
- Source bugs:
  - [`../../bugs/systemd-socket-activation-socat-acceptfd-bridge.md`](../../bugs/systemd-socket-activation-socat-acceptfd-bridge.md)
  - [`../../bugs/systemd-environment-whitespace-split.md`](../../bugs/systemd-environment-whitespace-split.md)
  - [`../../bugs/systemd-execstart-env-var-indirection-vs-socat.md`](../../bugs/systemd-execstart-env-var-indirection-vs-socat.md)
  - [`../../bugs/systemd-socket-activated-service-bounces-back-after-stop.md`](../../bugs/systemd-socket-activated-service-bounces-back-after-stop.md)
  - [`../../bugs/bun-spawn-systemd-service-eacces.md`](../../bugs/bun-spawn-systemd-service-eacces.md)
- ORG-006 §Phase-1-implementation-addendum §Deviation-1 (Bun launcher pivot), §Deviation-2 (socat ACCEPT-FD shape).
- T-0096 Kerfuffles 12 + 13 (original discovery sessions).
- T-0104 (kino Phase 1b cross-UID architecture), T-0114 (kino Phase 2.7 deploy-fix arc), T-0353 (socket teardown order).

---

— Specialist (specialist), drafted 2026-06-05 for T-0832 (PR-2).
