# pve2 hung-box power recovery (out-of-band via the HA smart plug)

Recovery can force a power-cycle of pve2 without a human at the machine, using the IKEA smart plug on pve2's mains, driven through Home Assistant. This is the out-of-band recovery path from T-2019.

> **STATUS (2026-08-03): AUTHORED, NOT YET LIVE-FIRED.** The reach/auth/read path is verified. The destructive step (an actual power-cycle) has NOT been exercised — the operator's standing instruction is "don't hard-cycle" outside a coordinated window. Per the T-2019 principle, *a recovery path never exercised is a claim, not a capability* — treat this as a claim until the first coordinated live-fire (tracked on T-2019).

## Failure shape

pve2 (Lenovo ThinkStation P3, the compute box carrying forgejo/git-primary, sharkey, ci-runner, memex, netdata) is **hung** — powered on but unresponsive: ssh dead, ping dead, guests unreachable, and it does not recover on its own. A hung box is already powered on, so Wake-on-LAN does nothing; the only lever is forced power-off/reset. Historically this required the operator physically at the box (2026-07-24 outage; 2026-08-02 uplink-saturation event where he powered it on at 23:07Z and off at 23:43Z because Recovery had the diagnosis but no power lever).

Also covers: pve2 needs a forced OFF during an incident (e.g. it is saturating the home uplink and the OS is unresponsive to a clean shutdown).

## The lever (verified 2026-08-03)

- **Plug entity:** `switch.bed_lamp_3`, friendly-name **"Pve2 switch"**, device_class `outlet`, model IKEA TRETAKT. ⚠️ The device-registry metadata is misleadingly stale — device name "Bed lamp", area "Bedroom" (a repurposed spare plug). **the operator explicitly confirmed 2026-08-03 that `switch.bed_lamp_3` is the pve2 plug.** Do NOT trust the device name; trust the entity_id + friendly-name. No HA device is named "pve".
- **Home Assistant:** `http://192.168.2.220:8123` (LAN). Token in `~/hass.pass` (`home_assistant_token=`, mode 600).
- **Reach path — via boxon, NOT directly:** swarm has no subnet route to 192.168.2.0/24 (T-2020 retired subnet routing). Reach HA by jumping through **boxon** (`ssh boxon`), a tailnet node ON the same LAN as HA and **independent of pve2** — which is essential: the box being recovered must not be in its own recovery path.

### Command primitives (token never in argv — piped via curl -K on stdin)

Read plug state:
```bash
TOKEN=$(grep '^home_assistant_token=' ~/hass.pass | cut -d= -f2-)
printf 'header = "Authorization: Bearer %s"\nurl = "http://192.168.2.220:8123/api/states/switch.bed_lamp_3"\n' "$TOKEN" \
  | ssh boxon 'curl -sS -m8 -K -'
```

Actuate (turn_off / turn_on) — **DESTRUCTIVE, gated, see below**:
```bash
# body: {"entity_id":"switch.bed_lamp_3"} ; SERVICE = turn_off | turn_on
B64=$(printf '{"entity_id":"switch.bed_lamp_3"}' | base64 -w0)
printf '%s\n%s\n' "$TOKEN" "$B64" | ssh boxon 'read T; read B; printf "%s" "$B" | base64 -d > /tmp/hb.json;
  curl -sS -m8 -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d @/tmp/hb.json \
  http://192.168.2.220:8123/api/services/switch/SERVICE; rm -f /tmp/hb.json'
```

## Recommended remediation (ranked)

1. **Confirm pve2 is genuinely HUNG, not booting/slow.** A booting box is briefly unreachable — cycling it mid-boot corrupts state. Require sustained failure: ssh + ICMP dead from **two** vantage points (swarm over the tailnet AND boxon on the LAN) for a bounded window (≥ ~3–5 min of consecutive failures). If HA/boxon shows the plug already `off`, the box is already down — do NOT cycle; investigate why it powered off.
2. **Log intent loudly.** A power-cycle takes down every pve2 guest — git-primary (forgejo), sharkey, ci-runner, memex, netdata. This is a last resort, not routine remediation.
3. **Cycle:** read state (expect `on`) → `switch.turn_off` → wait ~20–30s for full power drain → `switch.turn_on`.
4. **Verify recovery** (never by reading a status page — by actual reachability): pve2 ssh returns; gateway health clean; each guest back (audit `onboot` — netdata's onboot=0 was fixed to 1 under T-2035; check the others). Confirm the guests that should return actually did.
5. **Bounded attempts:** at most **one** cycle, then re-verify. If pve2 does not return, **escalate to the operator — do NOT loop-cycle.** State-gate so the remediation cannot re-fire on the same incident.

## Autonomous-vs-bubble-up (D3 placement)

Power-cycling pve2 is **high-blast-radius** (takes down git-primary + all guests). Placement:
- **Coordinated / operator-present:** the FIRST live-fire and any daytime cycle — surface, get his explicit go, then execute. (Standing instruction 2026-08-03: "don't hard-cycle" outside a coordinated window.)
- **Autonomous forced-cycle** is justified ONLY when pve2 is confirmed hung (step 1) AND the operator is unreachable across ~3 cycles (overnight-autonomy bar, [[feedback_autonomy_bar_higher_overnight]]) AND the hang is causing active harm (e.g. uplink saturation). Then: cycle once, verify, file a post-hoc-review ticket. Idle-while-a-box-burns is worse than act-with-adjustment — but only under all three conditions.
- Reading the plug state and running the hang-detection is always in-scope (non-destructive).

## Cross-references

- T-2019 (this capability; live-fire verification still owed), T-2035 (netdata onboot fix + tailnet node), T-2375/T-2435 (2026-08-02 uplink-saturation incident, the evidenced need).
- `recovery-service-recovery.md`, `persona-daemon-liveness-recovery.md` (sibling technical recoveries).
- Second out-of-band lever — **AMT (Intel vPro), PROVISIONED + LIVE (verified 2026-08-10):** pve2 has NO BMC/IPMI (workstation), but AMT gives OS-independent power/reset + serial-over-LAN + KVM at `https://192.168.2.216:16993` (TLS-only; 16992/623 closed), user `admin`, cred `~/mebx.pass` (dotenv `mebx_password=`), **reached via boxon** (swarm has no LAN subnet route — a direct swarm probe FALSE-reads AMT as "unprovisioned", which bit the earlier T-2019 note). AMT is *richer* than the plug (adds console/KVM/boot visibility the plug cannot) but rides the onboard I219-LM NIC — so an e1000e NIC-hang could take AMT down while the **plug (mains-level, NIC-independent) still works**. Guidance: use the **plug** as the NIC-hang-proof power lever; use **AMT** when you also need console/serial/boot visibility. Full path + the reach-via-boxon gotcha are in the auth index (`reference_credential_index`). CLI power-control tooling (meshcmd/wsmancli) is not yet installed — drive AMT via its web UI through boxon for now (follow-up: add AMT command primitives here). An AMT power action shares the same live-fire gate as the plug — verified reachable/authenticated, never exercised on a real wedge.
