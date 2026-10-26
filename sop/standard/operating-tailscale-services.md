# Operating Tailscale Serve services (kino + dashboard)

## When to use this

Operating, observing, or restoring Pantheon's Tailscale Serve mappings:
- PWA reachability check (`https://swarm.tail26cadd.ts.net/` from the operator's phone or any tailnet device).
- Recovery when serve config has disappeared (per T-0336 first production occurrence 2026-05-14).
- Initial setup or re-setup after a host migration.
- Auditing what the tailnet exposes from this node.

Background: the kino backend (Pantheon PWA) binds `127.0.0.1:8443` only, fronted by Tailscale Serve which terminates TLS via Tailscale's cert machinery. See `architecture/adr/ORG-006-kino-pwa.md` §Decision/Auth-model for the full rationale (security invariant: backend MUST NEVER bind 0.0.0.0).

## The two service-scope serves Pantheon uses

| Service | Backend | Purpose |
|---|---|---|
| `svc:kino` | `http://127.0.0.1:8443` | Pantheon PWA backend (kino.service, Bun, multi-agent web UI). |
| `svc:dashboard` | `http://127.0.0.1:4820` | Third-party Claude-Code-Agent-Monitor (Son Nguyen Hoang). |

Both run under the **service-scope** (`--service=svc:<name>`) form rather than the default-scope form. Service-scope serves get their own Tailscale services with separate FQDNs (per the `tag:swarm` node tagging). This matters for the status-check quirk below.

## Procedure

### 1. Status check — use `--json` OR `get-config --all`, NOT plain status

```bash
tailscale serve status              # MISLEADING — does NOT show service-scope serves
tailscale serve status --json       # REAL — shows Services block with svc:kino + svc:dashboard
tailscale serve get-config --all    # REAL — equivalent JSON shape; the operator's preferred probe per msg_1620
```

The plain `tailscale serve status` surface only enumerates default-scope serves. Service-scope serves (which is what Pantheon uses) live ONLY in the `--json` output (under the `Services` key) or in `get-config --all` output (under the `services` key). **A bare `tailscale serve status` returning "No serve config" is NOT diagnostic of a real config loss — verify with `--json` or `get-config --all` before declaring "PWA down because serve config missing"** (Recovery fell into this trap TWICE: first during T-0336 diagnosis, second during 2026-05-19 msg_1617-1620 when PWA-down turned out to be the operator's VPN, not serve config). See [[feedback_tailscale_serve_status_trap.md]] for the discipline pin.

### 2. Setup / re-setup the two serves

Canonical commands (live in `claude` user's bash history; require sudo as `claude`):

```bash
sudo tailscale serve --service=svc:kino --https=443 http://127.0.0.1:8443
sudo tailscale serve --service=svc:dashboard --https=443 http://127.0.0.1:4820
```

Each command sets up one service-scope serve. Both need to be run for full Pantheon coverage (kino + dashboard).

### 3. Recovery when config has actually disappeared

If `--json` confirms the `Services` block is empty/missing, restore via the canonical commands above. **Sudo requirement**: only the `claude` user has sudo into root for these commands. Recovery (`claude-fallback`) does NOT — Recovery routes recovery through Orchestrator via the inbox-SOP (per T-0336):

```bash
# From Recovery, when the operator can't be reached:
python3 tools/ticket.py file --requester operator --target role:head_engineering_orchestrator \
  --title "Restore Tailscale Serve mappings (svc:kino + svc:dashboard)" \
  --ask "[the two canonical commands + acceptance criteria]"
```

The inbox-consumer dispatches it; Orchestrator runs the sudo commands; the resolution Telegram-updates the operator.

### 4. Acceptance / verification post-restore

```bash
tailscale serve status --json | jq '.Services'   # Should show svc:kino + svc:dashboard with TCP entries
curl -k https://swarm.tail26cadd.ts.net/         # From a tailnet device — should return PWA HTML
```

End-to-end smoke: the operator loads PWA from his iPhone (Tailscale must be active on iPhone — check `tailscale status` shows iPhone's `last seen` is recent / online).

### 5. Reset (rare, destructive)

```bash
sudo tailscale serve reset
```

Wipes ALL serve configs (default-scope AND service-scope). Use only when re-setting up from scratch. Re-apply both canonical commands after to restore svc:kino + svc:dashboard.

## Common failure modes

### "PWA down" but server-side healthy

Triage order:
1. **Verify with `--json`** — is the `Services` block actually missing, or is it the plain-status quirk masking active serves?
2. Check `tailscale status` for iPhone connectivity — if the operator's iPhone shows `last seen 30m ago, currently offline`, the issue is client-side (iPhone Tailscale app stopped or auth expired). Not a server fix.
3. Check kino backend: `curl http://127.0.0.1:8443/` should return HTTP 200. If it doesn't, the backend itself is broken — separate triage.
4. If `--json` confirms missing serves AND backend is healthy: serve config has actually disappeared. Restore via canonical commands.

### Sudo cross-account quirk

Recovery's NOPASSWD-sudo is into `claude` and `claude-research` (specific user accounts), NOT into root. `sudo tailscale serve` requires root sudo. So Recovery CANNOT directly run the recovery commands; must route through Orchestrator (claude user) which has the sudo path. Document this so future-Recovery doesn't waste a cycle trying.

### Status surface inconsistency

`tailscale serve status` (plain) and `tailscale serve status --json` (JSON) report DIFFERENT things — plain only shows default-scope, JSON shows everything including service-scope. Always use `--json` for service-scope (Pantheon's pattern).

### Auth header bypass concern

The Tailscale-User-Login header model assumes the backend ONLY accepts requests from Tailscale Serve (which strips + re-injects identity headers). If the backend accidentally binds `0.0.0.0` instead of `127.0.0.1`, the header model is bypassable. **Audit on every kino.service drop-in change**: confirm the `--port=` / bind address stays loopback-only.

### Tag-owned client device → no Tailscale-User-Login → kino 401 (T-0638)

**Symptom:** PWA fetch fails with `fetchTickets: 401`; server-side `tailscale status` shows the user's user-tagged devices all offline/idle and the active client appears as `tagged-devices`. The kino daemon is healthy, Tailscale serve config is intact, but `curl https://kino.tail26cadd.ts.net/api/tickets` returns `{"error":"unauthorized","reason":"no_tailscale_identity"}`.

**Cause:** Tailscale serve injects `Tailscale-User-Login` from the *source peer's user identity*. When a device is owned by a TAG (e.g. `tagged-devices`) instead of a user account, it has no user identity to propagate — the header is OMITTED from forwarded requests (not "tagged-devices" as a literal value), and kino's `extractTailscaleIdentity` returns `null`. The distinction matters: `no_tailscale_identity` is "no header at all," not `user_mismatch` ("header present but rejected").

**Fix (client-side):** Reauth the device in the Tailscale client to transfer ownership from the tag to the user account. This is NOT "just remove the tag" — a re-authentication flow on the client side is required (confirmed empirically 2026-05-27 msg_1987/1990). After reauth, the device appears under the user in `tailscale status` and the identity header injects correctly.

**Diagnosis order:**
1. `curl -s -H "Tailscale-User-Login: <user>" http://127.0.0.1:8443/api/tickets` from the swarm host → should be 200 (proves daemon auth is fine).
2. `tailscale serve get-config --all | jq .services` → confirm `svc:kino` present (NOT bare `status`).
3. `tailscale status` → look for an active node under the user's account. If the only active node is `tagged-devices`-owned, the diagnosis is confirmed.
4. Ask the user to verify the Tailscale client's "logged in as <user>" view. If it shows tag-ownership, reauth resolves it.

**Anti-rabbit-hole:** Don't blame the daemon, recent code changes, or "tailscale serve is broken" before checking the source device's ownership. Cross-reference: `~/.claude/projects/-home-claude-fallback/memory/reference_tailscale_tagged_device_no_user_login.md`.

## Persistence on reboot

Boot-time defense against the T-0336/T-0337 disappearance pattern: `tailscale-serve-restore.service` (systemd unit, oneshot, `After=tailscaled.service`) re-applies the canonical `services.conf` list on every boot. Source-of-truth list at `/etc/tailscale/services.conf`; install script at `/usr/local/sbin/tailscale-serve-restore`. Full setup recipe + per-service add flow: `sop/standard/adding-a-new-tailscale-service.md`. Verify the unit ran cleanly with `systemctl status tailscale-serve-restore.service` (should show `active (exited)`).

The unit does NOT defend against the runtime-disappearance case (service drops mid-day with no reboot). It defends against reboots only. T-0337's root-cause work covers the runtime case.

## Boundary

This SOP covers operational state (status, restore, reset) of existing service-scope serves. It does NOT cover:
- Designing new Tailscale Serve mappings (architecture-tier, ADR-shaped).
- **Adding a new service** — see `sop/standard/adding-a-new-tailscale-service.md` for the end-to-end recipe (admin-console + node-side `tailscale serve --service=` + `services.conf` append).
- Changing the auth model from Tailscale-User-Login headers to something else.
- The structural question "why does serve config disappear" — see T-0337 for the open investigation; this SOP captures the operational response, not the root cause.

## Cross-references

- `architecture/adr/ORG-006-kino-pwa.md` §Decision/Auth-model — auth model rationale + security invariant.
- `tickets/done/T-0098.md` — initial supervised first-fire of kino + tailscale serve setup (2026-05-06).
- `tickets/done/T-0336.md` — first production restore (2026-05-14); the resolution surfaced the `--json` quirk that motivated this SOP.
- `tickets/active/T-0337.md` — root-cause investigation for the disappearance (open).
