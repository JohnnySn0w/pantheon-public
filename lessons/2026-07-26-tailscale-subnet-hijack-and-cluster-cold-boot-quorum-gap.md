# Lesson — two home-cluster failure classes: a Tailscale subnet route that HIJACKS LAN routing, and a Proxmox cluster's cold-boot quorum gap

**Surfaced:** 2026-07-26, during the pve2/network incidents that ended in de-clustering boxon+pve2 into two standalone nodes (T-2000/T-2020/T-2021 arc). Filed by Recovery (`claude-fallback`). Operator context: two correlated outages in one arc — a pve2 NIC "Hardware Unit Hang" (T-2021, separate root cause) and device flapping that made the home LAN intermittently unroutable.

## Pattern 1 — never advertise a Tailscale subnet route for a subnet your clients physically sit on

CT-101 (a container on the home LAN) advertised a Tailscale **subnet route + exit node** for the very subnet that physical LAN clients already sit on. Tailscale then had two ways to reach those LAN IPs — the physical link and the advertised route — and preferred/injected the tailnet route, **hijacking LAN routing**. Symptom: intermittent device flapping / hosts becoming unreachable, which *looked* like a hardware/link problem but was a routing conflict.

### The tell

A single node advertising `--advertise-routes` for a CIDR that overlaps the subnet its consumers are physically on. The route is redundant (the clients can already reach that subnet directly) and actively harmful (it competes with / overrides the physical path). Also a **single point of failure**: every off-LAN path to the hosts depended on that one container's route staying up.

### Mitigation (applied + live)

- **Make each host a DIRECT tailnet node** instead of routing through one container: boxon `100.115.155.50`, pve2 `100.76.170.91`. Reaching either host no longer depends on any subnet router. `~/.ssh/config` prefers the direct nodes, with `-lan` (subnet) and `-via-boxon` (ProxyJump) fallbacks.
- **Retire the overlapping subnet route** — the operator disabled the CT-101 route approval in the Tailscale admin console; CT-101 retired. This removed both the SPOF and the routing hijack.
- Rule of thumb: advertise a subnet route ONLY for a network your tailnet clients **cannot otherwise reach** (a truly isolated segment). Never for the LAN your clients already live on.

## Pattern 2 — a 2-node Proxmox cluster has a cold-boot quorum gap (and buys little without shared storage)

The boxon+pve2 Proxmox **cluster** (corosind/quorum) added a cold-boot failure mode: if the nodes don't both come up, quorum is lost and guests can be blocked from starting — a fragility that bit during the incident. And without shared storage, the cluster's headline benefits (single-pane UI, live migration) aren't even available.

### The tell

A small (2-node) cluster where you're paying the quorum/cold-boot **cost** (a node can't act alone if it loses quorum) without the **benefit** (live migration / shared-storage HA) because there's no shared storage backing it.

### Mitigation (applied)

- **De-clustered into two STANDALONE nodes.** No corosync, no quorum, no cold-boot quorum failure mode; each node acts independently and all guests kept running. `corosync.conf` backed up on each node (`/root/corosync.conf.bak-decluster-2026-07-26`) so a future re-cluster is one restore away **once a NAS / shared storage exists** to make clustering actually pay for itself.
- Accepted tradeoff (the operator's call): no single-pane UI, no live-migration until shared storage lands. Cosmetic leftover: `/etc/pve/nodes/` on each standalone node still lists the other — harmless; clean if a stale node shows in the UI.

## Meta-lesson

Both failures *presented* as hardware/link problems (flapping devices, an unreachable host) but were **configuration/topology** choices adding fragility: a redundant-and-competing route, and a quorum dependency with no offsetting benefit. When a "hardware" symptom is intermittent and correlates with a topology you added for resilience, suspect the resilience mechanism itself — the falsifying check is to look at what the mechanism actually routes/depends on, not just the symptom (sibling of `feedback_verify_root_cause_before_asserting`).

## Cross-references

- T-2020 (SPOF eliminated → direct tailnet nodes), T-2021 (pve2 e1000e NIC-hang offload fix), T-2000 (cluster arc)
- `project_pantheon_proxmox_cluster` (fleet-infra state), `reference_tailscale_tagged_device_no_user_login` (adjacent tailscale gotcha)
