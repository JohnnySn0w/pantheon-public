---
symptom: "agent-monitor.service (Claude-Code-Agent-Monitor, node server/index.js) wedges after multi-day uptime — port 4820 stays open but HTTP requests stall or time out; accept-queue accumulates unaccepted connections; dashboard.tail26cadd.ts.net effectively down with no crash signal in journal"
fix: "restart the user-mode systemd unit: `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart agent-monitor.service` (run as the `claude` user; Restart=on-failure does NOT trip because the process didn't exit)"
domain: "pantheon, agent-monitor, node, third-party, systemd-user"
---

> ⛔ **DORMANT SINCE 2026-08-02 — this bug is not currently reachable, and the Fix below is not a live
> procedure.** The operator retired the dashboard (TG 6746/6747: *"are we pulling any stats from it? if not,
> just shut it off"* → *"dormant, leave it around just off"*), closing the TG 6507 fix-vs-retire steer.
> Both `agent-monitor.service` and `pantheon-agent-monitor-healthcheck.timer` are **stopped and
> disabled**; port 4820 is closed. Nothing consumed it — there is no `tailscale serve` config any more
> (so the §Symptom URL below is also stale), and the only repo references were the watchdog's liveness
> probe and documentation.
> **Do not run the Fix to "clear a wedge" — nothing is running to wedge.** Kept because it is the
> reference if the dashboard is ever re-armed (`systemctl --user enable --now agent-monitor.service`,
> plus the timer if the watchdog is wanted), and because T-2346 reattributed the failure history it
> feeds. **Delete this block on re-arm.**

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

## Structural guard (T-1885, 2026-07-27)

A timer-driven watchdog now detects this automatically: `tools/agent_monitor_healthcheck.py`, fired every 2 min by `pantheon-agent-monitor-healthcheck.timer`. On 3 consecutive stalled HTTP probes it logs a confirmed wedge. Auto-restart is SHIP-DARK (`PANTHEON_AGENT_MONITOR_REMEDIATE_ENABLED`, default OFF) pending a supervised first-fire — until armed, the manual restart above remains the only remediation path; the watchdog only detects + logs. Once armed, it self-heals (cooldown-gated, escalates to the operator only if a restart doesn't clear it) and this manual procedure becomes the fallback for between-poll gaps.

## Discovered

- 2026-05-28 the operator msg_2051 ("is the agent dashboard down?") — first observed occurrence. Recovery triaged: port open, accept-queue=2, requests timing out, 5d 15h uptime. Restart cleared it; fresh PID, HTTP 200 in 41ms.

## Keywords

agent-monitor, agent-dashboard, agent-monitor.service, Claude-Code-Agent-Monitor, dashboard.tail26cadd.ts.net, svc:dashboard, port 4820, node server/index.js, event loop wedge, listener stalled, accept-queue accumulating, Recv-Q nonzero, no crash signal, restart-on-failure no-op, multi-day uptime, third-party node app, Son Nguyen Hoang, pantheon dashboard down
