---
symptom: "agent-monitor.service (Claude-Code-Agent-Monitor, node server/index.js) wedges after multi-day uptime — port 4820 stays open but HTTP requests stall or time out; accept-queue accumulates unaccepted connections; dashboard.tail26cadd.ts.net effectively down with no crash signal in journal"
fix: "restart the user-mode systemd unit: `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart agent-monitor.service` (run as the `claude` user; Restart=on-failure does NOT trip because the process didn't exit)"
domain: "pantheon, agent-monitor, node, third-party, systemd-user"
---

## Symptom

`tailscale serve` config intact (`svc:dashboard` → `dashboard.tail26cadd.ts.net` → `http://127.0.0.1:4820`), but the dashboard is unreachable:

- `curl -m 10 http://127.0.0.1:4820/` returns HTTP 000 (timeout) or a long-stalled 200 (50s+).
- `ss -tlnp` on port 4820 shows the listener with Recv-Q > 0 — unaccepted connections piling up.
- `systemctl --user is-active agent-monitor.service` returns `active` (the process didn't exit, so Restart=on-failure never triggers).
- Journal shows no crash / no error — only the unrelated periodic "Agent Dashboard: upstream update available" nag.
- Process uptime (`ps -o etime`) is multi-day (5d+ in the first occurrence).

## Cause

Third-party node server's event loop wedges after extended uptime — root mechanism (memory leak, FD leak, stuck WebSocket handler, GC pathology) not pinned per first occurrence. Whatever the upstream defect, the symptom signature is "process running, port listening, no error logs, requests stall." Because the process is technically alive, systemd's `Restart=on-failure` is a no-op — only an explicit restart kicks it.

## Fix

```bash
sudo -u claude bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart agent-monitor.service'
```

(Recovery's path; Orchestrator and Specialist run as `claude` so they skip the `sudo -u claude` prefix.)

Verify post-restart:

```bash
sudo -u claude bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user is-active agent-monitor.service'   # active
ps -ef | grep 'node server/index.js' | grep -v grep                                                            # fresh PID
curl -fsS -o /dev/null -w "HTTP %{http_code} (%{time_total}s)\n" -m 10 http://127.0.0.1:4820/                  # HTTP 200 in <100ms
```

If the wedge recurs frequently, consider a periodic restart timer or pin the root cause in the upstream code (Claude-Code-Agent-Monitor by Son Nguyen Hoang, `/home/claude/GitHub/Claude-Code-Agent-Monitor`, fork upstream `origin/master`).

## Discovered

- 2026-05-28 the operator msg_2051 ("is the agent dashboard down?") — first observed occurrence. Recovery triaged: port open, accept-queue=2, requests timing out, 5d 15h uptime. Restart cleared it; fresh PID, HTTP 200 in 41ms.

## Keywords

agent-monitor, agent-dashboard, agent-monitor.service, Claude-Code-Agent-Monitor, dashboard.tail26cadd.ts.net, svc:dashboard, port 4820, node server/index.js, event loop wedge, listener stalled, accept-queue accumulating, Recv-Q nonzero, no crash signal, restart-on-failure no-op, multi-day uptime, third-party node app, Son Nguyen Hoang, pantheon dashboard down
