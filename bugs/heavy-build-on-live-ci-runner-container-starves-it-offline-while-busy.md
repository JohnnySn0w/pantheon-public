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

## Recurrence 2026-09-28 (T-4084): the heavy build was a real CI job

**Symptom variant: online + busy + NO job running (a "ghost busy").** The echo runner showed `online busy=true`, yet no echo run was in progress. GitHub had lost track of a `Runner.Worker` that was still executing a `cargo tarpaulin` coverage job with ~12 parallel rustc/rust-lld processes. That job filled CT202's RAM and swap (8 GiB / 1 GiB). The reclaim thrash pushed CT202 io.pressure to ~97% full, starved the whole pve2 disk (other CTs at 76-98%, host load ~100), and wedged the **co-located** pantheon-config runner too: its seconds-long audit job sat in D-state for 40+ min. `gh run cancel` and even the `force-cancel` API were accepted but never acked, because a starved runner can't process a cancel. There was no OOM kill, which is why it wedged instead of failing.

**Recovery:** the same kill-the-hog plus restart-service block as above (Recovery killed the 50-process worker tree, then restarted both runners).

**Prevention added:** echo PR #361 caps the coverage job at 4 jobs (`tarpaulin.toml` `jobs = 4`, which tarpaulin passes as `cargo --jobs`, plus `CARGO_BUILD_JOBS=4`), and runs the advisory coverage leg on push:main only. With a single runner, every job in a run serializes, so a ~1h advisory leg on every PR doubles per-PR runner time. At the time, two runners shared one 8 GiB CT, and even the non-coverage echo `test` job caused 60-180s subprocess timeouts in pantheon-config's suite the same night. Splitting the runners into separate CTs was named as the structural fix, but T-4084 closed without a split (memory 8 to 12 GiB plus a contingency ladder).

**Decision 2026-10-05 (T-4569): the CT split is DECLINED; size, cap and isolate in place.** Every pve2 guest sits on the host's one HDD (`sda`; the NVMe is off limits). Two of the three recurrences here were disk-bound: io pressure at ~97% full, then ENOSPC. A second CT on the same host and disk isolates CPU and RAM, which CT202's current sizing already covers, but not the disk. State on 2026-10-05: CT202 has 12 cores, 16 GiB and an 80G rootfs (20% used), plus the `/ci-cache` LV. It runs five runners: echo-prismatic `beefbox` + `pve2-…-light`, pantheon-config `pve2` + `pve2-…-light`, and revenue-project `beefbox`. The pantheon-config runners have their own users (`runner-pc`, `runner-pcl`; T-4436), and the other three share `runner`. Also in place: coverage capped at 4 jobs (echo #361), job `timeout-minutes` (T-4170), runner-junk reclaim (T-4174), a fresh HOME per job (T-4425), and the CT202 disk monitor (T-4484). **Re-open trigger:** a fourth offline-while-busy or ghost-busy strand with these in place. Then file the split, or a second physical disk, which pve2 lacks (T-4353).

**Post-recovery backlog drain (reusable).** A runner outage leaves a per-push backlog: pantheon-config's `hermes-audit.yml` and `cascade-on-merge.yml` run on every main push and scan only that push's `before..HEAD` diff. On 2026-09-28 that backlog was 186 queued runs, about 2h on one serialized runner, all ahead of the PR CIs. Clear it without losing coverage:
1. Pin `PIN=$(git rev-parse origin/main)`. Take `BASE` = the `headSha` of the last COMPLETED main run of each workflow.
2. Run the equivalent scans once over the whole range: `git diff $BASE $PIN | python3 tools/hermes_diff_scan.py` (exit 0 = clean), and `git log --diff-filter=R --name-status --format="" $BASE..$PIN | python3 -c '…extract_resolved_tids…'` to list the resolved tids. Then check that no open ticket still `depends_on` them. If some do, run `ticket.py cascade --tid` for each.
3. Falsify the scanner once (pipe it a synthetic diff containing the sequence built at runtime; expect exit 1), so "clean" means something.
4. Cancel only the queued main-push runs whose `headSha` is in `git rev-list $BASE..$PIN`. Pushes after `PIN` run normally.

## Recurrence 2026-09-28 18:26Z (T-4170 / T-4174): DISK, not CPU/RAM

**Symptom variant: the co-located pantheon-config runner is `online busy=true`, its job hangs, and cancels don't ack.** Here the runner was NOT starved: the listener renewed the job lease every 60s for the whole hour. The tell is in CT202's `/opt/actions-runner-pc/_diag`: the hung job's `Worker_<ts>.log` is **0 bytes**, and the listener later logs `System.IO.IOException: No space left on device`. The cause was that CT202's rootfs (then 42G, shared by every runner on it) filled up during echo's `Run coverage` (cargo-tarpaulin's instrumented build in `target/`). The echo worker hit ENOSPC 7s after the pantheon-config worker started into the full disk. Echo hit ENOSPC 3x that day. A 5-second HERMES scan held the only pantheon-config runner for 57 min, with ~20 runs queued behind it.

**Diagnose:** `pct exec 202 -- df -h /`. Guest df, not the thin LV's Data% (untrimmed blocks read ~100% and mean nothing). Then `grep -l "No space left" /opt/actions-runner-*/_diag/*<date>*`.

**Fixed (T-4174):** reclaimed ~5G of runner junk: `_diag` logs older than 3 days (never rotated; 2G), `_work/_update` leftovers from the 2.337.0 self-update (~2G), and a stale Jul `ct202-echo-verify` scratch. Also grew the rootfs online to 80G from the HDD `pve/data` pool (`pct resize 202 rootfs +38G`; never the NVMe), and made the idle 98G `/ci-cache` LV (mp0) runner-writable (host `chown 101000:101000 /mnt/ci-cache`, since the CT is unprivileged). T-4170 bounds every pantheon-config job with `timeout-minutes`: a cancel carries a 5-min grace, then the listener abandons the worker, so the worst-case hold is timeout + 5 min.

## Cross-references

- Sibling capacity/starvation lesson on a *dedicated build VM*: an unbounded `cmake --build -j` (no job cap) on a 12-vCPU VM pegs all its vCPUs and makes the VM's own sshd unreachable (banner-exchange timeout), even while the 32-core host is fine — cap parallelism (`-j <cores-2>`) or `qm set <id> --cpulimit N`.
- `sop/standard/confirm-health-after-touching-a-live-session.md` — the general "verify after touching a live thing" discipline this violated.
