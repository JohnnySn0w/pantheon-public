---
symptom: "a self-hosted GitHub Actions runner flips to status=OFFLINE while busy=true — it claimed a job then lost its GitHub heartbeat mid-execution — and STRANDS that required check, even though a healthy identical-labelled runner sits idle right next to it (the scheduler already committed the job to the now-offline runner and won't fail it over)"
fix: "never run a heavy build (or any CPU/RAM hog) on the same container/host as a LIVE runner — it starves the runner's heartbeat thread (CPU) or OOM-kills it. Run verification builds in a SEPARATE guest, or nice/cgroup-cap them. Recover a stranded runner: kill the hog; if the runner service is then stuck `deactivating`, `systemctl kill -s KILL <svc> && systemctl reset-failed <svc> && systemctl start <svc>` for a fresh listener. Cancel+rerun the stranded job so a healthy runner takes it."
domain: "pantheon, ci, github-actions, self-hosted-runner, proxmox, capacity, resource-starvation, self-inflicted"
---

## Symptom

The GitHub runners API reports (mid-CI):

```
beefbox-echo-prismatic   status=OFFLINE  busy=TRUE
vps-runner-1             status=online   busy=false
```

**Offline-WHILE-busy** is the tell: a runner that shut down cleanly goes `offline/busy=false`; a runner that CLAIMED a job and then lost its connection during execution goes `offline/busy=true`. The required check it grabbed is now stranded — queued with no runner executing it — and it does **not** fail over to the idle sibling, because both advertise the same `[self-hosted, linux, x64]` label set and the scheduler had already committed the job to the one that died.

## Cause

Something heavy ran on the **same container/host as the live runner** and starved it. In the case that produced this entry: a manual `cargo check --workspace` verify-build (echo's workspace pulls a Tauri/webkit dependency tree — a big CUDA-free-but-CPU-heavy compile) was launched *as the runner's service account, inside the 4-core/4GB container (CT202) that also hosts the live runner*. The compile pegged all 4 cores; the runner's heartbeat thread couldn't get CPU time; GitHub stopped hearing from it and marked it offline. **It was NOT an OOM** (kernel log clean) — pure CPU/heartbeat starvation. (An OOM-kill produces the same offline-while-busy signature, so check `journalctl -k | grep -i oom` to tell them apart.)

The container's small size is the multiplier: 4 cores means one build saturates everything. The physical host was a 32-core box at ~50% load the whole time — **the host had plenty of headroom; the *container's* cgroup limit was the wall.** A build that's harmless on the host can still kill a runner pinned inside a small container.

## Fix

**Recovery (get the runner back):**

```bash
# 1. kill the hog (the manual build), as whatever user ran it
pkill -9 -u <user> -f cargo ; pkill -9 -f <scratch-dir>
# 2. if the runner service is now wedged in "deactivating" (graceful stop hung):
S=actions.runner.<owner>-<repo>.<name>.service
systemctl kill -s KILL "$S" && systemctl reset-failed "$S" && systemctl start "$S"
# 3. confirm a FRESH listener + reconnect:
journalctl -u "$S" --since "40 seconds ago"   # want: "√ Connected to GitHub" + "Listening for Jobs"
```

Then cancel + re-run the stranded check so a healthy runner claims it (unblocks the merge regardless of the dead runner's state).

**Prevention:**

- **Never co-locate a heavy build with a live runner.** To verify a runner's toolchain, either (a) trust a pkg-config/`command -v` probe sweep run *as the runner user* (proves visibility without load), or (b) run the actual build on a **separate** guest, or (c) `nice -n19` + a `systemd-run --scope -p CPUQuota=` / cgroup cap so it can't monopolize the container.
- **Size the container for its peak job, not its idle.** A 4c/4GB container that must build a webkit-inclusive Rust workspace is under-provisioned; a real CI job reproduces the same pressure a manual build does. Grow it (`pct resize` for disk, `qm/pct set` for cores/RAM) or pin heavy jobs elsewhere.
- **Distinguishing labels are a second, independent reason to pin.** Because identical label sets let the scheduler commit a job to a runner that then dies — with no failover to a healthy idle sibling — give each runner a distinguishing label and pin heavy jobs deterministically. Installing packages does not fix this failure mode.

## Cross-references

- Sibling capacity/starvation lesson on a *dedicated build VM*: an unbounded `cmake --build -j` (no job cap) on a 12-vCPU VM pegs all its vCPUs and makes the VM's own sshd unreachable (banner-exchange timeout), even while the 32-core host is fine — cap parallelism (`-j <cores-2>`) or `qm set <id> --cpulimit N`.
- `feedback_confirm_health_after_touching_live_sessions` — the general "verify after touching a live thing" discipline this violated.
