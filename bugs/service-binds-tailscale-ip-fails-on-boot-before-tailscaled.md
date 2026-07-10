---
symptom: "a systemd service that binds/listens on the node's Tailscale IP crash-loops on boot (bind fails, exhausts StartLimit, systemd gives up) because it starts before tailscaled brings the interface up"
fix: "drop-in: After=/Wants=tailscaled + an ExecStartPre that polls for the Tailscale IP to appear (≤60s) before the service binds; add Restart=on-failure + a generous StartLimitBurst as backstop"
domain: systemd, tailscale, mqtt, boot-race
---

## Symptom

A service whose config binds a **specific Tailscale IP** (e.g. mosquitto `listener 1883 100.122.54.49`) fails on a fresh VPS boot and never recovers:

```
mosquitto.service: Main process exited, code=exited, status=1/FAILURE
mosquitto.service: Scheduled restart job, restart counter is at 5.
mosquitto.service: Start request repeated too quickly.
mosquitto.service: Failed with result 'exit-code'.
```

`systemctl --failed` shows it. The bind fails because the Tailscale IP isn't assigned yet — tailscaled is still coming up (NoState). With a default `StartLimitBurst`, the service crash-loops a few times in the first seconds of boot, hits the limit, and systemd **gives up** — so it stays down until manual intervention (could be hours).

**Downstream symptom:** anything that depends on the down service may itself fail loudly. Observed (T-1053): the **echo daemon** (`echo.service`, 2-min systemd `WatchdogSec`) couldn't reach the down MQTT broker, failed to send its watchdog keepalive, got SIGABRT-killed and restarted **every 2 minutes — 148 times over ~5h** — until the broker was restored, at which point it self-stabilized. So a single broker boot-failure can masquerade as a busy crash-loop in a *different* service.

## Cause

Same class as **T-0902** (tailscale-serve-restore NoState boot-race). `After=network-online.target` (or no ordering at all) only guarantees the network stack, **not** that tailscaled has reached `Running` and assigned the node's Tailscale IP. A service that hard-binds that IP races tailscaled and loses on a cold boot. The hardcoded-IP bind is the trigger; binding `0.0.0.0` or a loopback-only listener would not hit it (but loopback-only loses tailnet reachability, and `0.0.0.0` violates the loopback-bind security invariant for tailscale-serve backends).

## Fix

A systemd drop-in that waits for the IP before the service binds, plus retry-hardening as backstop. Mirrors T-0902's readiness-poll shape.

```ini
# /etc/systemd/system/<svc>.service.d/tailscale-ip-wait.conf
[Unit]
After=tailscaled.service
Wants=tailscaled.service
StartLimitIntervalSec=120
StartLimitBurst=20

[Service]
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do ip -4 addr show | grep -q "<TAILSCALE_IP>" && exit 0; sleep 2; done; exit 0'
Restart=on-failure
RestartSec=5
```

`sudo systemctl daemon-reload` after. The ExecStartPre exits 0 on timeout (best-effort, doesn't block forever); the `Restart=on-failure` + 20-burst-over-120s keeps retrying as a backstop if the poll's 60s isn't enough. Revert = `rm` the drop-in + daemon-reload.

**Detection habit:** a service-by-service "is it active?" check will NOT catch this — the failed service is one you didn't think to poll. Always run `sudo systemctl --failed` (system) + `systemctl --user --failed` (every agent account) as part of any post-reboot verification. T-1053 was found exactly this way, 5h after the reboot.

## Related

- `tickets/inbox/T-1053.md` (mosquitto instance + the echo-daemon downstream crash-loop), `tickets/done/T-0902.md` (tailscale-serve-restore instance), `sop/standard/adding-a-new-tailscale-service.md` (the tailscale-serve service pattern).
