---
symptom: "intermittent device flapping / connectivity loss across a tailnet after a subnet router advertises a route (e.g. 192.168.2.0/24) for a LAN that some tailnet clients are ALSO physically on; a single subnet-router container becomes a SPOF whose magicsock can get stuck relaying over DERP, cutting VPS→LAN visibility for hours while the LAN itself is fine locally"
fix: "do NOT advertise a subnet route for a network your tailnet clients physically sit on — it hijacks their already-working local path through the router. Make the machines you need to reach their OWN direct tailnet nodes (each its own MagicDNS name) instead of riding a subnet route; keep subnet routes only for devices that CANNOT run tailscale (printers, IoT, VM/CT guests). Disable the bad route's approval in the Tailscale admin console."
domain: "pantheon, tailscale, networking, subnet-route, spof, home-infra, proxmox"
---

## Symptom

After a tailscale **subnet router** (here a Proxmox CT advertising `192.168.2.0/24`
+ acting as exit node) came up, the fleet saw:

- **Device flapping** — tailnet peers intermittently losing/regaining each other.
- A **~4.5h VPS→LAN blackout** (2026-07-24→25): the subnet router's magicsock got
  stuck relaying over a **DERP relay** instead of a direct path, so the remote VPS
  lost all visibility to the home LAN — while the LAN and the boxes on it were
  **fine locally the whole time** (healed only when another tailnet device rejoined
  and kicked path re-discovery).
- A **single point of failure**: that one CT was the *only* path from the VPS to the
  Proxmox hosts and everything on the LAN. If it (or its host) died, all remote
  diagnosis/recovery of a downed host died with it.

## Cause

Two compounding mistakes, one root:

1. **Advertising a subnet route for a network tailnet clients are ALSO physically
   on.** When a client already has a working local path to `192.168.2.x` AND a
   tailnet subnet route claims `192.168.2.0/24`, tailscale can prefer the tailnet
   path — routing traffic that should stay on the local wire *through the subnet
   router*. That is a routing hijack: the router inserts itself into a path that
   didn't need it, and now every one of those flows depends on that one node's
   health and its (fragile) direct-vs-DERP path selection.

2. **A single subnet router as the sole tailnet↔LAN bridge** turns a
   convenience into a SPOF, and subnet-router magicsock dropping to a DERP relay
   (NAT/UDP-41641 path loss) silently degrades every routed flow to relay latency
   or a full stall.

The root is treating a subnet route as "how I reach the LAN" when the machines that
matter can just be tailnet nodes themselves.

## Fix

- **Make the machines you need to reach their OWN direct tailnet nodes.** Install
  tailscale on each host (each gets its own tailnet IP + MagicDNS name). Reaching
  them no longer rides the subnet route at all — the single biggest resilience win.
  (Done here: both Proxmox hosts became direct nodes; the subnet-router CT was
  retired. The SSH config then prefers the direct node with `-lan`/`-via-boxon`
  fallbacks.)
- **Keep subnet routes ONLY for devices that cannot run tailscale** (printers, IoT,
  VM/CT guests that aren't their own nodes) — and never for a subnet your tailnet
  clients physically sit on.
- **Disable the offending route's approval** in the Tailscale admin console (routes
  must be approved to take effect; un-approving instantly stops the hijack).
- If a subnet router is genuinely required, reduce DERP fallback: forward **UDP
  41641** to it so direct paths stick, and advertise the subnet from a **second
  always-on node** too (HA failover subnet routers) so it isn't a SPOF.

## Verify

- The hosts resolve to their **own** tailnet IPs (not the subnet router's), and SSH
  reaches them with the router stopped.
- `tailscale status` shows **direct** connections (not `relay "<derp>"`) to the
  hosts you care about.
- Flapping stops once the hijacking route is un-approved.

## Cross-references

- Sibling incident: `bugs/lone-cloudflare-proxied-record-edge-fail-looks-like-origin-down.md`
  (the same 2026-07 de-cluster arc; a different "the network looks broken but the
  origin/host is fine" misattribution).
- `project_pantheon_proxmox_cluster` memory (the home Proxmox fleet + de-cluster).
