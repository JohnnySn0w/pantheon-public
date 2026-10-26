# Adding a new Tailscale Serve service-scope service

## When to use this

You want a new backend (bound to `127.0.0.1:<port>`) reachable from the tailnet at a clean subdomain like `https://<name>.tail26cadd.ts.net` — separate FQDN, separate certs, coexists with the existing services (kino, dashboard, echo-recorder).

Pantheon's pattern is **service-scope serves** (`tailscale serve --service=svc:<name>`), NOT default-scope. Service-scope gives each service its own subdomain via Tailscale Services + `tag:swarm` node tagging. Default-scope would clobber root path on the swarm node (kino owns it).

Companion SOP: `sop/standard/operating-tailscale-services.md` — operating + restoring existing services (status checks, recovery, the `--json` quirk).

## End-to-end recipe

### 1. Bind the backend to loopback

The new service backs onto a process listening on `127.0.0.1:<port>`. **Security invariant: backend MUST NEVER bind 0.0.0.0** — the auth model (Tailscale-User-Login header injection by `tailscale serve`) is bypassable if the backend accepts non-loopback connections. Audit the bind address on every backend deploy.

Pick an unused port. Check with `ss -tlnp | grep LISTEN`. Currently in use: 8443 (kino), 4820 (dashboard), 8447 (echo-recorder), 8765 (telegram-bridge).

### 2. Register the Service in the Tailscale admin console (one-time, the operator)

Tailscale admin console → Services → "Create service":
- **Name:** `<name>` (no `svc:` prefix in the UI)
- **Backend host:** the VPS, tagged `tag:swarm`
- **Port:** `443` (Tailscale Services proxy terminate TLS at 443)

ACL must permit the tag. Already configured for `tag:swarm` per kino's setup.

### 3. Add the node-side service-scope serve

Run as root on the VPS (claude user has the sudo path):

```bash
sudo tailscale serve --service=svc:<name> --bg --https=443 http://127.0.0.1:<port>
```

The `--bg` flag is implicit when `--service` is set, but explicit is clearer. The serve config is persisted by `tailscaled` immediately.

### 4. Add the service to the persistence list (so it survives reboots)

Edit `/etc/tailscale/services.conf` and add a line `<name>:<backend-port>`:

```bash
echo "<name>:<port>" | sudo tee -a /etc/tailscale/services.conf
```

The companion systemd unit `tailscale-serve-restore.service` runs `/usr/local/sbin/tailscale-serve-restore` After=`tailscaled.service` on boot, which re-applies every line in `services.conf`. Without this, serve config survives reboots in normal operation BUT a tailscaled state-loss event (T-0336 pattern, root cause under investigation in T-0337) would silently drop the mappings — the unit is the defensive backstop.

### 5. Verify

```bash
# Full config dump (the canonical "what's registered"):
tailscale serve get-config --all

# Web-handler shape (the canonical "what's routing"):
tailscale serve status --json | jq '.Services'

# Smoke from the VPS itself (kino + dashboard work direct; new services may take ~1–2 min for MagicDNS propagation):
curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://<name>.tail26cadd.ts.net/

# End-to-end smoke from the operator's phone:
# - Tailscale active on iPhone
# - Open https://<name>.tail26cadd.ts.net/ in Safari (NOT http:// — see gotcha below)
```

**Operator gotcha — HTTPS only, no HTTP redirect.** Tailscale Serve at `--https=443` listens on HTTPS only; there is no automatic `http://` → `https://` redirect. A phone hitting `http://<name>.tail26cadd.ts.net/` will hang on a connection timeout (looks like a server problem; it's actually wrong-scheme). Always include the explicit `https://` scheme in any URL you share or bookmark. Surfaced 2026-05-16 during T-0363 echo-recorder first-fire.

### 6. Test the boot-restore path (recommended for new services)

```bash
sudo systemctl restart tailscale-serve-restore.service
journalctl --no-pager -u tailscale-serve-restore.service -n 20
```

Should show one `applying svc:<name> → http://127.0.0.1:<port>` line per entry in `services.conf` + final `Finished` line. All entries should re-apply cleanly (the underlying `tailscale serve --service=...` command is idempotent — re-running an existing mapping is a no-op).

## The persistence trio (file/script/unit reference)

### `/etc/tailscale/services.conf` — source of truth

```
# Pantheon Tailscale service-scope serves. One line per service: <name>:<backend-port>
# Re-applied at boot via tailscale-serve-restore.service.
# See: pantheon-config/sop/standard/adding-a-new-tailscale-service.md
kino:8443
dashboard:4820
echo-recorder:8447
```

Comments start with `#`; blank lines OK. Adding a new service = appending one line + running `tailscale serve --service=...` once (the unit re-applies on every boot from then on).

### `/usr/local/sbin/tailscale-serve-restore` — re-apply script

```bash
#!/usr/bin/env bash
# Re-apply Pantheon's Tailscale Serve service-scope mappings on boot.
# Source-of-truth list lives in /etc/tailscale/services.conf (one line per
# service: <name>:<backend-port>). Each invocation is idempotent — tailscale
# treats re-applying an existing mapping as a no-op.

set -euo pipefail

CONF="${1:-/etc/tailscale/services.conf}"

if [[ ! -r "$CONF" ]]; then
  echo "missing config: $CONF" >&2
  exit 1
fi

# T-0902: wait for tailscaled to reach Running (not NoState) before applying.
# After=/Requires=tailscaled only waits for the unit to be ACTIVE, not for the
# backend to be ready; on the 2026-05-29 boot this ran while tailscaled was in
# NoState and the apply loop failed ('unexpected state: NoState'). Poll up to
# 60s for BackendState=Running, then proceed best-effort. pipefail-safe via
# '|| true' so a transient status failure during NoState doesn't abort the script.
for _ in $(seq 1 30); do
  state="$(/usr/bin/tailscale status --json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null || true)"
  [[ "$state" == "Running" ]] && break
  sleep 2
done

while IFS=: read -r name port; do
  [[ -z "$name" || "$name" =~ ^# ]] && continue
  echo "applying svc:$name → http://127.0.0.1:$port"
  /usr/bin/tailscale serve --service="svc:$name" --bg --https=443 "http://127.0.0.1:$port"
done < "$CONF"
```

This is the copy installed on the VPS (T-0902) and, since 2026-10-22, CT201 (T-5003). CT201 had been built from an older copy of this block that lacked the wait loop, and its unit failed at every boot with `unexpected state: NoState`.

Install: `sudo install -m 755 -o root -g root <source> /usr/local/sbin/tailscale-serve-restore`.

### `/etc/systemd/system/tailscale-serve-restore.service` — systemd unit

```ini
[Unit]
Description=Restore Tailscale Serve service-scope mappings from /etc/tailscale/services.conf
Documentation=https://github.com/example-org/pantheon-config/blob/main/sop/standard/adding-a-new-tailscale-service.md
After=tailscaled.service
Requires=tailscaled.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tailscale-serve-restore
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Install + enable: `sudo install -m 644 ... /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now tailscale-serve-restore.service`.

### `/etc/systemd/system/tailscale-serve-restore.service.d/restart.conf` — retry backstop

```ini
[Unit]
StartLimitIntervalSec=120
StartLimitBurst=4

[Service]
Restart=on-failure
RestartSec=10
```

The wait loop is best-effort: after 60s it applies anyway. If tailscaled is still not Running, the apply fails and this drop-in retries it. The VPS has carried this file since T-0902, and T-5003 added it to CT201. Install it alongside the unit, then `systemctl daemon-reload`.

## Why this shape (decision rationale)

- **Service-scope over default-scope** — per-FQDN isolation, multiple services coexist on one node cleanly. Default-scope on `tag:swarm` would clobber root since kino already owns it.
- **`services.conf` list + shell script + systemd unit, NOT `get-config`/`set-config` JSON round-trip** — `tailscale serve get-config --all` emits a simplified `tcp:<port>` endpoint shape; `set-config` rejects re-applying it onto the live config with `"want to serve http but port 443 is already serving https"` (config-format mismatch on round-trip; confirmed 2026-05-16 T-0363). The shell-script-iterating-explicit-commands path is simpler, debuggable, and idempotent.
- **`RemainAfterExit=yes`** — `Type=oneshot` units report `inactive (dead)` after success, which reads like a failure in `systemctl status`. `RemainAfterExit=yes` keeps it as `active (exited)` so the operator sees "this ran" not "this is dead."
- **No auto-snapshot on serve changes** — out of scope; deferred. If services drift in/out frequently, revisit and add a `tailscale serve` wrapper that updates `services.conf` on success. Current cadence (a few services, rare additions) doesn't justify it.

## Cross-references

- `sop/standard/operating-tailscale-services.md` — operating + restoring existing services (the post-add operational surface).
- `tickets/done/T-0363.md` — landing of this SOP + the persistence trio (2026-05-16).
- `tickets/active/T-0337.md` — open root-cause investigation for the serve-config-disappearance pattern. This SOP's persistence trio is the operational defense; T-0337 may render it moot.
- `tickets/done/T-0336.md` — first production disappearance incident (2026-05-14) that motivated the defense.
- `architecture/adr/ORG-006-kino-pwa.md` §Decision/Auth-model — backend `127.0.0.1`-only invariant rationale.
