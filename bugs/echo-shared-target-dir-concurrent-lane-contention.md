---
symptom: "Echo lanes building concurrently into the shared CARGO_TARGET_DIR (T-1668) corrupt each other's incremental artifacts, producing test-run failures with zero test-level FAILED lines: random-point infrastructure failures (missing rlib/module/binary), deterministic-looking phantom compile errors (e.g. E0432 on a symbol that verifiably exists), and lock-serialization slowdowns."
fix: "Distinguish real failures (a repeating 'test result: FAILED' or 'panicked at' line) from this bug (missing rlib/module/binary with zero FAILED lines): for phantom compile-error/doctest corruption run a targeted cargo clean -p <crate> (retry alone does not clear it; never a bare cargo clean); for a vanishing binary mid-run wait for sibling-lane quiescence (pgrep -af 'cargo|rustc' empty) then retry; for authoritative APPROVE-gating smokes, quiescence-gate plus targeted clean plus a single uninterrupted run with at most one active builder."
domain: pantheon, echo, cargo, rust, build-system, concurrency, ci
---

# Echo shared CARGO_TARGET_DIR: concurrent lane builds corrupt each other's incremental artifacts

**First observed:** 2026-07-13, three independent reproductions in one evening (T-1742 / T-1744 / T-1748 lanes building concurrently into `/home/claude/.cache/echo-lane-target`).
**Status:** STRUCTURAL FIX LANDED (T-1932, 2026-07-23) — the HYBRID: the shared cache stays the default for dev loops (T-1668 disk floor preserved); APPROVE-gating smokes run through the per-worktree `auth-smoke` wrapper that `stage-echo-worktree.sh` now generates (`/tmp/echo-T-NNNN/auth-smoke <cmd>` → isolated `CARGO_TARGET_DIR=<worktree>/target-auth`, env-over-config precedence; disk-floor-checked; sibling-activity warned; GC'd for free with the worktree at arc close). The wrapper supplies ISOLATION ONLY — the authoritative command still comes from the CI workflow file. sccache was rejected: adds a dependency for a benefit the problem doesn't need. Triage rules below stay valid for dev-loop (shared-cache) runs.

## Failure signatures (all with ZERO test-level `FAILED` lines — that's the tell)

1. **Random-point infrastructure failure** (T-1742, 3 consecutive full-workspace `cargo test` runs): each attempt died at a *different* point — missing doctest rlib, missing module for a doctest, unittest binary vanishing mid-run. Every test that actually executed passed. Attempt 4, with sibling lanes quiet, was fully clean.
2. **Deterministic-looking phantom compile error** (T-1744, reproduced 3x on the same symbol): `E0432: no ProfilesChangedNotifier in ports` despite the source being verifiably correct (isolated `-p echo-core` builds passed). Stale/corrupted incremental artifacts. `cargo clean -p echo-core` resolved it permanently. This variant is nastier: it *looks* like a real code defect and reproduces, so retry-once heuristics don't clear it.
3. **Extreme slowdown / lock serialization** (expected, benign): `Blocking waiting for file lock on build directory` — cargo's lock serializes whole builds; a lane can sit behind a sibling's 6-minute clippy. Not corruption, just latency.

## Triage rule

A REAL test failure repeats at the same test with a `test result: FAILED` or `panicked at` line. If a full-workspace run dies with none of those — missing rlib/module/binary, exit≠0 with all executed tests green — it's this bug:

- **Doctest-class corruption ("extern location for X does not exist", phantom `E0432` on a symbol that verifiably exists) → go straight to `cargo clean -p <the crate whose symbol/rlib is "missing">`.** Retry does NOT clear it — the stale rlib persists across runs (PO-verified 2026-07-13: two consecutive Lane 1 D5 retries hit the identical corruption until the targeted clean). Do NOT `cargo clean` bare from a lane (empties the shared dir for every lane — T-1668 rule).
- Vanishing test *binary* mid-run with siblings actively building → wait for quiescence (`pgrep -af "cargo|rustc"` empty), then retry.
- Best practice for authoritative (APPROVE-gating) smokes: quiescence-gate + targeted `cargo clean -p` of the diff's crates + single uninterrupted run, ≤1 builder across all lanes.

## Why it happens

`stage-echo-worktree.sh` (T-1668a) points every lane's `[build] target-dir` at one shared cache to hold the disk floor. Cargo's directory lock serializes *whole cargo invocations*, but fingerprint/incremental state is keyed by workspace path+toolchain — concurrent lanes with *different source trees* writing interleaved artifacts for the *same crate names* can leave a lane reading a sibling's fresher-timestamped but source-mismatched artifacts.

## Structural fix candidates (unticketed; weigh against T-1668's disk floor)

- Per-lane `CARGO_TARGET_DIR` with aggressive arc-close GC (pre-T-1668 shape; costs 5–25G per live lane).
- Shared dir + per-lane `--target-dir` override only for *authoritative* smoke runs (hybrid: warm cache for dev loops, isolation for the runs that gate APPROVE).
- `sccache`-style shared compilation cache with per-lane target dirs (cache hits without artifact aliasing).

## Cross-refs

T-1668 (shared lane target-dir decision) · T-1753 (same script, different regression) · 2026-07-13 lane reports for T-1742/T-1744 (verbatim failure logs in the session task outputs).

## Keywords

CARGO_TARGET_DIR, cargo clean, cargo clean -p, E0432, phantom compile error, doctest
corruption, missing rlib, missing module, vanishing test binary, lock serialization,
Blocking waiting for file lock, incremental artifacts, stage-echo-worktree.sh, T-1668,
concurrent lanes, quiescence, pgrep -af cargo|rustc, sccache, per-lane target dir,
disk floor, cargo test, fingerprint corruption, echo-core, ProfilesChangedNotifier

## 2026-07-15 — contention can present as SPURIOUS COMPILE ERRORS (not just slow builds)

During the T-1762 review, two consecutive clippy/test runs against the shared lane target-dir
returned reproducible-looking `E0599`/`E0609` ("method/field not found") for symbols that were
verifiably present and correctly feature-gated in source — while other lanes were building
concurrently. A from-scratch run with an ISOLATED `CARGO_TARGET_DIR=/tmp/<lane>-isolated-target`
was fully green (91/91 result lines).

**The rule:** a red compile result from the shared target-dir under active lane contention is
UNTRUSTWORTHY in both directions — before rebounding on it (or trusting a suspicious green),
re-run the failing stage with a private `CARGO_TARGET_DIR` and believe THAT result. Direct
source-read contradicting the compiler error is the tell that you're in this mode.

## Keywords addendum

E0599, E0609, method not found, field not found, spurious compile error, CARGO_TARGET_DIR,
isolated target dir, stale incremental artifacts, concurrent cargo, isolate-and-verify
