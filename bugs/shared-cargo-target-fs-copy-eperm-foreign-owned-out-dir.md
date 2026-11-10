---
symptom: "cargo build/clippy fails with 'failed to run custom build command' + 'Operation not permitted' (EPERM os error 1) copying bindings/headers/resources into a shared CARGO_TARGET_DIR — but only for a seat that didn't create the cached OUT dir"
fix: "run echo cargo via ~/pantheon-config/tools/echo-cargo.sh (T-4822), which runs echo_target_adopt.py (T-4071) first; or run python3 ~/pantheon-config/tools/echo_target_adopt.py before the build: re-owns a peer seat's OUT files for the known copier crates by mtime-preserving copy+rename, so no build-script re-run or recompile. Fallback: rm -rf the stale foreign-owned build/<crate>-<hash> (forces a re-run + recompile)"
domain: cargo, rust, shared-target, cross-seat, echo, disk-pressure, concurrency
---

## Symptom

A `cargo` build/clippy/test against a **shared** `CARGO_TARGET_DIR` (e.g. echo's `/var/lib/pantheon/echo-target`, the T-3604 cross-seat pilot) fails when run by a seat that is NOT the one who first populated the cache. The build script of a C-FFI or codegen crate panics while copying generated files into its `OUT_DIR`:

```
error: failed to run custom build command for `aws-lc-sys v0.41.0`
  thread 'main' panicked at .../aws-lc-sys-0.41.0/builder/main.rs:1116:18:
  Failed to copy include file during build setup: Os { code: 1, kind: PermissionDenied, message: "Operation not permitted" }
```
```
error: failed to run custom build command for `libsqlite3-sys v0.30.1`
  thread 'main' panicked at .../libsqlite3-sys-0.30.1/build.rs:36:35:
  Could not copy bindings to output directory: Os { code: 1, kind: PermissionDenied, message: "Operation not permitted" }
```
```
error: failed to run custom build command for `echo-demo-app v0.0.0`
  cargo:PERMISSION_FILES_PATH=/var/lib/pantheon/echo-target/debug/build/echo-demo-app-<hash>/out/app-manifest/...
  Operation not permitted (os error 1)
```

Note **os error 1 (EPERM)**, not EACCES(13). The echo **pre-push hook** mislabels it `[pre-push] FAILED: cargo clippy — fix warnings above` even though nothing is wrong with your code. Observed 2026-09-28 across SIX crates in one push: `aws-lc-sys`, `libsqlite3-sys`, `openssl-sys`, `ring`, `x11-dl`, `echo-demo-app` (tauri app-manifest). `umask 002` + setgid group dir + group-write on the files does NOT prevent it.

## Cause

`std::fs::copy` copies the source's permission bits to the destination — it `chmod`s the dest after writing, and **chmod requires OWNERSHIP of the target file, not merely write permission.** When a C-FFI/codegen build script (`build.rs`) copies generated bindings/headers/resources into `OUT_DIR`, and that dest file **already exists owned by a different seat** (the seat that first built it, e.g. `claude`-owned mode 664), the copying seat can write the bytes via group-write but the trailing chmod fails with EPERM. So `umask 002` (which makes NEW files group-writable) is necessary but NOT sufficient — it can't fix a pre-existing foreign-owned file. Pure codegen crates that only *read* their cached OUT (serde, thiserror, rustversion, …) are unaffected — the hazard is specifically build scripts that `fs::copy`/overwrite into OUT_DIR AND re-run (a stale/partial cache entry, or a fingerprint the copying seat's invocation invalidated).

## Fix

**Durable (T-4071, Recovery): adopt the peer's OUT files before building.**

```bash
python3 ~/pantheon-config/tools/echo_target_adopt.py            # known copier crates; --dry-run to preview
python3 ~/pantheon-config/tools/echo_target_adopt.py --packages ring,openssl-sys   # or name the crate(s) that failed
```

Every OUT dir is group-writable with no sticky bit, so any seat may rename inside it. The tool copies each file you don't own (bytes, mode and mtime) and renames the copy over the original. You now own the file (fchmod works) with the same bytes and mtime, so cargo sees nothing stale: no build-script re-run and no recompile (the `rm -rf` below forces both). It skips files modified in the last 120s (a peer's build may be writing them), leaves alone any file that changes mid-copy, and never fails the build (exit 0). Ownership now sits with you, so the next seat adopts in turn. That's why it belongs at the start of every echo build entrypoint, the pre-push gate first. Measured 2026-09-28: one seat switch is ~205–213 MB across 6–18k files for the six copiers (`--all` would be ~1.9 GB). If that copy cost ever bites, the escalation is a shared build identity or a root-side chown-to-caller helper; both are access grants, so they need the operator.

Fallback / manual unblock — delete the stale foreign-owned build OUT dir so your build recreates it owned by you (the shared `build/` dir is group-writable + setgid + no sticky bit, so you can delete a peer's files without owning them):

```bash
bd=/var/lib/pantheon/echo-target/debug/build
# Clear only the C-FFI/codegen copiers that hold foreign-owned OUT files:
for pat in aws-lc-sys libsqlite3-sys openssl-sys ring x11-dl echo-demo-app; do
  for d in "$bd/$pat"-*; do
    [ -d "$d" ] || continue
    if find "$d/out" -type f ! -user "$USER" -print -quit 2>/dev/null | grep -q .; then
      rm -rf "$d" && echo "cleared $(basename "$d")"
    fi
  done
done
```

Do NOT blanket-`rm` every foreign-owned `build/*/out` (dozens exist — serde/thiserror/etc. are read-reused fine and deleting them forces a near-full rebuild). It's whack-a-mole: each re-running copier fails in turn. Instead, run `python3 ~/pantheon-config/tools/echo_target_adopt.py` (T-4071) first. It re-owns the foreign OUT files without a recompile, and then the full pre-push hook can pass. If the hook is slow, run the push in the background. **Never `git push --no-verify`.** `rulebook/policies/trust-posture.md` forbids any hook bypass, and `sop/standard/merging-an-agent-branch-pr.md` step 1 reads that as covering pushes; CI re-running the smoke is not a carve-out. (This paragraph advised `--no-verify` until T-4561.) If the hook still cannot pass after adopting, that is a substrate fault: report it to the HO rather than bypassing.

**Concurrent seats (T-4955).** One adopt at the start of a long gate is not enough when another seat builds on the same target at the same time. On 2026-10-13 orchestrator's pre-push failed twice on aws-lc-sys (os error 13, 644 include files owned by suborch2), because suborch2's own builds re-owned the OUT files in the middle of orchestrator's ~20-min gate. Two rules close it:
1. `echo_target_adopt.py` holds cargo's own lock (`<profile>/.cargo-lock`) while it works, waiting up to `--lock-timeout` (default 1800s). Cargo holds that lock for a whole invocation, so no adopt can land inside another seat's build. If the wait times out it skips that profile, says so, and exits 0.
2. A gate that runs several cargo commands (fmt, clippy, test) must adopt before EACH of them, because a peer can build in the gaps between them. Run every cargo step through `tools/echo-cargo.sh`, which adopts first.

**Durable fix is substrate-owner territory (Recovery, target-dir ownership T-3777):** a shared build identity for the target, per-seat OUT segregation, or a post-build `chown`-to-group + `g+w` sweep. Flagged to orchestrator 2026-09-28.

## Verification

Reproduce: from a seat that is NOT the cache's original builder, with a shared `CARGO_TARGET_DIR` whose `<crate>-<hash>/out` holds another seat's files, run `cargo clippy -p echo-adapter-ha-mcp --all-targets` — panics at `fs::copy` with `kind: PermissionDenied, message: "Operation not permitted"` (EPERM). Confirm the mechanism: `ls -l /var/lib/pantheon/echo-target/debug/build/aws-lc-sys-*/out/include/openssl/*.h` shows the blocking `.h` files owned by the *other* seat at mode 664 (group-writable yet un-chmod-able by you). After the `rm -rf` fix + re-run, the same scoped gate passes (measured green 2026-09-28 for echo-adapter-ha-mcp: fmt+clippy+test, T-3823).

## Discovered

- T-3823 (echo ha-mcp fix push), 2026-09-28, artisan. Complements the seat-side operating note in artisan memory `shared-cargo-target-dir-cross-seat` (umask-002-insufficient extension).


Sibling: `bugs/shared-cargo-target-cross-worktree-artifact-bleed.md` (cross-worktree artifact bleed on the same shared dir; the correctness face).

Contradicts: `notes/echo-cargo-target-sharing-eval.md` (T-3550), which treats `umask 002` + the setgid `pantheon-shared` dir as the permission answer. As this entry shows, it does not prevent EPERM on a foreign-owned OUT dir; the durable fix is the adopt step (T-4071, `tools/echo_target_adopt.py` via `tools/echo-cargo.sh`).

## Keywords

fs::copy, std::fs::copy, EPERM, Operation not permitted, os error 1, PermissionDenied, failed to run custom build command, build.rs, OUT_DIR, aws-lc-sys, libsqlite3-sys, openssl-sys, ring, x11-dl, tauri, echo-demo-app, PERMISSION_FILES_PATH, CARGO_TARGET_DIR, shared target, cross-seat, setgid, umask 002, pantheon-shared, echo-target, pre-push hook, chmod ownership, cargo-lock, flock, concurrent gates, T-3604, T-3777, T-4955
