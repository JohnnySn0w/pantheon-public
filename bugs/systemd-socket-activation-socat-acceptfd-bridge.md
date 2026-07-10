---
symptom: "systemd-socket-activated service crashes with start-limit-hit when activated binary speaks pipe-stdio (StandardInput=socket passes the listening fd as stdin)"
fix: "use socat ACCEPT-FD:3 EXEC: to bridge the systemd-passed listening fd to subprocess pipe-stdio; do NOT use StandardInput=socket"
domain: systemd, socat, ipc
---

> **Superseded as load-bearing by** [`sop/remediation/socket-activated-agent-daemon-gotchas.md`](../sop/remediation/socket-activated-agent-daemon-gotchas.md) — the live remediation guidance these symptoms taught now lives in that SOP (ORG-012 supersedure-trail, T-0832 PR-2 / T-0853). This entry remains the recorded symptom + diagnosis; act on the SOP for current guidance.

## Symptom

A systemd service configured with socket activation (`Sockets=foo@%i.socket` + `StandardInput=socket` + `StandardOutput=socket`) crashes immediately on every connection attempt. `systemctl status` shows `Active: failed (Result: start-limit-hit)`. Journal shows the service exiting with non-zero status almost instantly after start, often without any meaningful error from the binary.

Specific case: the activated `ExecStart` is a stdio-pipe-speaking binary like `claude mcp serve` — designed to read JSON-RPC over stdin, write over stdout, in normal pipe mode.

## Cause

`StandardInput=socket` (with `Accept=no` on the .socket unit, the default for long-lived single-connection patterns) passes the *listening* socket fd as the service's stdin — NOT a connected socket. The activated binary tries to `read(0, ...)` and gets either EINVAL (can't read from a listening socket) or it `accept()`s the fd manually (if it knows to) — but most stdio-pipe binaries don't. They just confused-crash.

`StandardInput=socket` only works correctly when:
- `Accept=yes` on the .socket unit (per-connection forking — systemd accepts each connection itself, passes the connected fd, spawns one service instance per connection); OR
- The binary explicitly uses `sd_listen_fds()` from libsystemd to retrieve the fd as a listening socket and call `accept()` itself.

`claude mcp serve` (and most CLI/MCP-style binaries) does neither.

## Fix

Use `socat` as the bridge: it consumes the systemd-passed listening fd, accepts the connection, and bridges the connected fd to the subprocess's pipe-stdio.

```ini
# /etc/systemd/system/foo@.service
[Unit]
Description=foo subprocess for %i
PartOf=parent.service

[Service]
Type=simple
User=%i
Group=%i
WorkingDirectory=/home/%i
Environment=HOME=/home/%i
ExecStart=/usr/bin/socat ACCEPT-FD:3 EXEC:"/path/to/binary args"
Restart=on-failure
RestartSec=5s
Sockets=foo@%i.socket
StandardError=journal
# do NOT set StandardInput=socket / StandardOutput=socket
```

Socket activation is retained — the .socket unit creates the socket file with the right ownership/permissions, systemd passes the listening fd as fd=3 (LISTEN_FDS=1 env). socat 1.8+'s `ACCEPT-FD:3` reads from that fd, accepts incoming connections, and pipes the connected stream to the EXEC subprocess's stdin/stdout. The binary sees normal pipe-stdio.

Verify socat is installed:

```sh
apt-get install -y socat
socat -hh | grep ACCEPT-FD   # version 1.8+ has this
```

Socket file ownership stays managed by the .socket unit (`SocketUser=`, `SocketGroup=`, `SocketMode=`) — security boundary preserved. Any client that wants to connect to the socket file goes through normal unix-socket connect; the fact that socat is bridging is invisible to the client.

## Discovered

- T-0104 (kino Phase 1b Cycle B IPC plumbing) — initial kino-agent@.service unit had `StandardInput=socket` per research notes; all 3 units crashed with start-limit-hit at first deploy. ~30min of bisect to identify the structural mismatch; socat ACCEPT-FD:3 fixed all 3 on first try.
- pantheon-pwa commit b920068, 2026-05-06.
- ORG-006 §Phase-1-implementation-addendum §Deviation-2 captures the architectural shape.
- T-0096 Kerfuffle 13.

## Keywords

systemd, socket activation, socat, ACCEPT-FD, StandardInput=socket, stdio, pipe, claude mcp serve, mcp, IPC, LISTEN_FDS, sd_listen_fds, start-limit-hit, kino-agent, listening socket, fd bridge
