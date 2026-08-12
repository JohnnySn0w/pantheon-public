---
symptom: "after a power loss / cold boot of a small (2-node) Proxmox cluster with only ONE node coming back, the surviving node has no quorum — the pmxcfs /etc/pve tree goes read-only and onboot guests do NOT auto-start; the node's UI works but you cannot start VMs/CTs ('cluster not ready - no quorum?')"
fix: "for a home/2-node cluster without a QDevice, either add a lightweight qdevice (corosync-qnetd) as a 3rd vote, or — if you don't need live-migration/shared-storage/single-pane — DE-CLUSTER to independent standalone nodes so each boots and runs its guests with zero quorum dependency. Emergency single-node override: `pvecm expected 1` (temporarily lets the lone node reach quorum so guests can start)."
domain: "pantheon, proxmox, corosync, quorum, cluster, cold-boot, home-infra"
---

## Symptom

A 2-node Proxmox cluster loses power (or one node stays down after a reboot). The
node that IS up:

- boots fine, the web UI loads, the host itself is healthy;
- but `/etc/pve` (the corosync-backed `pmxcfs`) is **read-only**, `pvecm status`
  shows `Quorate: No`, and **`onboot: 1` guests never start** — a `qm start` /
  `pct start` fails with `cluster not ready - no quorum? (500)`.

So a cold boot where both nodes don't come up together strands every guest on the
surviving node, exactly when you most need them (e.g. the one hosting your git
forge, DNS, or home-automation).

## Cause

Corosync quorum needs a **majority of votes**. A 2-node cluster has 2 votes, so the
majority is 2 — **both** nodes must be present. One node alone has 1/2 = not a
majority → no quorum → `pmxcfs` refuses writes and the guest-autostart logic won't
fire. (The `two_node: 1` / `wait_for_all` corosync options soften the *steady-state*
case but still bite on a cold boot where the other node never appears.)

This is inherent to an even-node cluster with no tie-breaker. It is a **cold-boot
availability gap**, not a bug — the cluster is doing exactly what quorum is for
(preventing split-brain), at the cost of single-node autostart.

## Fix

Pick per what the cluster is actually FOR:

- **You need live-migration / shared storage / single-pane management:** keep the
  cluster but add a **QDevice** — a lightweight 3rd vote from any always-on box
  (`corosync-qnetd` on a Pi / NAS / VPS; `pvecm qdevice setup <ip>`). Now the
  surviving node + qdevice = 2/3 = quorum, and a single node cold-boots fine.
- **You do NOT need those (independent single-box workloads):** **de-cluster** to
  standalone nodes. Each node then has no corosync, no quorum, no cold-boot
  dependency — it boots and starts its own guests unconditionally. Trade-off: no
  single-pane UI, no live-migration until you have shared storage. (This is the path
  the Pantheon home fleet took 2026-07-26 — two standalone nodes, corosync.conf
  backed up on each for a future re-cluster once a NAS exists.)
- **Emergency (get guests up NOW on the lone node):** `pvecm expected 1` lowers the
  expected vote count so the single node reaches quorum and `/etc/pve` goes
  writable; then start the guests. Re-raises itself when the other node rejoins.
  Temporary override, not a fix.

## Verify

- `pvecm status` → `Quorate: Yes` (or, de-clustered, `pvecm status` reports the node
  is not part of a cluster).
- A reboot of one node alone brings its `onboot` guests up with no manual step.

## Cross-references

- Same 2026-07 de-cluster arc: `bugs/tailscale-subnet-route-for-a-network-clients-sit-on-hijacks-traffic.md`,
  `bugs/lone-cloudflare-proxied-record-edge-fail-looks-like-origin-down.md`.
- `project_pantheon_proxmox_cluster` memory (the home Proxmox fleet).
- Out-of-band power/boot recovery for a hung node is the sibling class (IKEA smart
  plug in HASS + AMT — the "how do I even reach a wedged host" problem).
