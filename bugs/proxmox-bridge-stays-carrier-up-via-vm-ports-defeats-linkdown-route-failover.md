---
symptom: "A Proxmox host with a wired-primary + wifi-backup default route (wlo1 metric 1000 behind vmbr0's metric-0 default) does NOT fail over when the wired uplink is unplugged: the box goes fully unreachable and stays down until the cable is re-plugged, even though the wifi interface is associated, has DHCP, and its network reaches the internet. Setting net.ipv4.conf.*.ignore_routes_with_linkdown=1 does NOT fix it. Measured on pve2 2026-09-03: a physical unplug left pve2 dark over tailscale for 158s; recovery came only from re-plugging wired, not wifi."
fix: "The default route lives on the BRIDGE (vmbr0), and a Linux bridge reports carrier-UP as long as ANY enslaved port has carrier — running VM tap (tapNi0) and CT veth (vethNi0) ports keep it up even when the physical uplink (the bridge's only real NIC, e.g. nic0) is unplugged. So the kernel never marks the vmbr0 default route link-down and never falls through to the wlo1 backup; ignore_routes_with_linkdown is inert because vmbr0 never goes NO-CARRIER. Fix: watch the PHYSICAL uplink's carrier directly, not the bridge's. Deploy a tiny systemd service polling nic0's LOWER_UP flag (`ip -o link show nic0 | grep -q LOWER_UP`) that, on link-down, `ip route del`s the dead vmbr0 default (letting the wlo1 metric-1000 default take over) and on link-up `ip route replace`s it back. Keep the ignore_routes_with_linkdown sysctl too as belt-and-suspenders (harmless, correct for the non-bridge case). On pve2 this is /usr/local/sbin/pve2-wired-failover-watch.sh + pve2-wired-failover-watch.service (enabled). Note LOWER_UP is absent for both a cable unplug AND `ip link set nic0 down`, so a software drill faithfully exercises the same path as a physical unplug."
domain: "proxmox, pve2, linux-bridge, vmbr0, networking, default-route, route-failover, ignore_routes_with_linkdown, carrier, LOWER_UP, wifi-backup, tailscale, systemd, multi-homing, pantheon"
---

# A Proxmox bridge stays carrier-UP via VM/CT ports, so ignore_routes_with_linkdown can't drive a wired→wifi route failover

**Class:** routing failover defeated by a false liveness signal — the bridge's carrier is not the uplink's carrier.

**Found:** 2026-09-03 (Recovery, T-2032 pve2 wifi-backup failover drill, operator-coordinated with the operator physically unplugging the cable).

## The trap

You add a wifi backup default route so tailscale (or any traffic) fails over when the wired NIC hangs or the switch drops:

```
default via 192.168.2.1 dev vmbr0 proto kernel onlink        # wired primary (metric 0)
default via 192.168.9.1 dev wlo1  metric 1000                # wifi backup
```

This looks correct and Part-A tests pass (wlo1 associates, gets DHCP on its own subnet, its net reaches the internet — verified `ping -I wlo1 8.8.8.8` = 3/3). But on a real unplug, nothing fails over. The box is dark until the cable returns.

The reason: **the primary default route is on `vmbr0` (a bridge), and the bridge does not lose carrier when its physical uplink does.** A Linux bridge is carrier-UP if *any* enslaved port has carrier. On a Proxmox host running guests, the bridge's ports include the physical NIC **and** every running VM's tap + every running CT's veth:

```
/sys/class/net/vmbr0/brif/  ->  nic0  tap210i0  veth200i0 veth201i0 veth202i0 veth203i0  fwpr105p0
```

Unplug `nic0` and it goes carrier=0 — but `tap210i0`, the `veth*` ports, etc. stay carrier=1 (the guests are still running), so **vmbr0 stays carrier-UP**. The kernel therefore keeps the dead metric-0 vmbr0 default route as valid and keeps sending traffic down the dead wire; it never falls through to the wlo1 metric-1000 backup. `ignore_routes_with_linkdown=1` cannot help — it only withdraws routes on a NO-CARRIER interface, and vmbr0 never reaches NO-CARRIER.

## The fix

Watch the **physical uplink's** carrier, not the bridge's. A ~40-line systemd service polling `nic0`'s `LOWER_UP` flag:

- link-down → `ip route del default via <gw> dev vmbr0` (the wlo1 metric-1000 default becomes the sole/active default → traffic + tailscale re-path over wifi)
- link-up → `ip route replace default via <gw> dev vmbr0 onlink` (restore wired primary)

Start-safe (no-op when the route is already present + wired is up), `Restart=always`, enabled for boot. Keep the `ignore_routes_with_linkdown` sysctl as a correct-but-inert-here belt-and-suspenders. Live implementation on pve2: `/usr/local/sbin/pve2-wired-failover-watch.sh` + `/etc/systemd/system/pve2-wired-failover-watch.service`.

Why `LOWER_UP` (not operstate or reading `/sys/.../carrier`): it is present iff physical carrier is up, and absent for BOTH a cable unplug and `ip link set nic0 down` — so an auto-restoring software drill (`ip link set nic0 down` + a `systemd-run --on-active=45 ... up` guaranteed restore) exercises exactly the real-unplug path. Reading `/sys/class/net/nic0/carrier` can instead error ("Invalid argument") when the iface is admin-down, making the two cases diverge.

## Verification

Reproduce the trap (pre-fix): with both defaults present and the watcher stopped, drop the physical uplink and watch reachability from an off-box tailnet node (`ping <pve2-tailscale-ip>` in a loop). It stays DOWN for the whole outage; `ip route show default` on the box (if reachable) still lists the vmbr0 default, and `cat /sys/class/net/vmbr0/carrier` = 1 despite `cat /sys/class/net/nic0/carrier` = 0.

Confirm the fix: with the watcher enabled, `ip link set nic0 down` (arm a `systemd-run --on-active=45` restore first). Off-box ping recovers in **~6s** (tailscale re-path), far below the auto-restore — proving wifi caught it, not the restore. During the down, on-box `ip route show default` shows **only** the wlo1 route and `journalctl -t pve2-wired-failover` logs "removed dead vmbr0 default; failing over to wlo1"; on restore it logs "restored vmbr0 default (primary)" and both routes return. (Measured 2026-09-03, T-2032.)
