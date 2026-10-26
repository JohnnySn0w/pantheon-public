---
symptom: "a cargo check/clippy/test in one git worktree passes (or fails) against ANOTHER worktree's source for the same workspace crate. Seen when two seats or worktrees of echo-prismatic build the same crate (e.g. echo-demo-app) on the shared /var/lib/pantheon/echo-target dev profile. The result is quietly wrong, with no error."
fix: "run any build whose RESULT is evidence (review smoke, merge gates) through `python3 ~/pantheon-config/tools/echo_evidence_build.py -- <cmd>` from inside the worktree: it gives each worktree its own isolated CARGO_TARGET_DIR (T-4122). Adding sccache to the shared target does NOT fix this — cargo's freshness check runs before RUSTC_WRAPPER."
domain: "rust, cargo, build-cache, worktrees, shared-target, review-integrity, echo"
---

## Symptom

Two worktrees of the same repo, e.g. a reviewer's `/tmp/echo-T-4109-review` and a peer seat's T-4075 worktree, both run `cargo test -p echo-demo-app` against `CARGO_TARGET_DIR=/var/lib/pantheon/echo-target` (the shared dev profile). Either run can compile, link and test the OTHER worktree's `echo-demo-app` build and report its result as its own. Nothing warns. The smoke numbers look normal; they just aren't about the code under review.

First hit by artisan on T-4091 (2026-09-28), and flagged again that day when an orchestrator reviewer and artisan's T-4075 gate built echo-demo-app concurrently.

## Cause

Two cargo design choices, each reasonable on its own:

1. **The artifact identity of a workspace member does not include its absolute path.** Cargo hashes path sources relative to the workspace root, so the build is relocatable. Two checkouts of the same repo therefore produce the SAME `-C metadata` hash and the same artifact filenames (`target/debug/deps/echo_demo_app-<hash>.rlib`, the same fingerprint dir).
2. **Freshness is judged by mtime.** The dep-info fingerprint compares the current worktree's source mtimes against the recorded artifact time. If the peer's build wrote the artifact AFTER your sources were last modified, cargo calls your unit fresh and reuses the peer's artifact.

Cargo's build-dir lock serializes the two builds but doesn't separate them: it prevents corruption, not cross-talk.

## Fix

- **Evidence builds (review smoke, merge-train gates) run through `tools/echo_evidence_build.py`** (T-4122): `cd <worktree> && python3 ~/pantheon-config/tools/echo_evidence_build.py -- <CI command>`. It places a per-worktree target under `/var/lib/pantheon/echo-evidence-targets/`, refuses setups that would bleed, reaps its own orphans, and supports `--gc` / `--list`. A cold build is about 15-20 min and 4.3G; a warm re-gate after a small edit is about 3 min.
- Why not "keep sharing and add sccache": cargo decides a unit is fresh (relocatable hash + mtime) *before* it ever calls `RUSTC_WRAPPER`, so sccache never sees the stale reuse. Isolation is required; sccache can only sit underneath it.
- **Dev loops on the shared target run through `tools/echo-cargo.sh`** (T-5152). It calls `tools/echo_worktree_switch.py`, which records the last workspace to build in `<target>/.echo-last-workspace`. When a different worktree builds, it touches that worktree's crate roots (never `build.rs`), so cargo rebuilds its workspace crates from its own source. Registry dependencies stay shared. Reproduced and fixed live 2026-10-20 with two copies of a one-crate workspace: under bare cargo the older copy printed the newer copy's string, and after the switch it printed its own. A bare `cargo` from another worktree does not update the record: if in doubt, run `python3 ~/pantheon-config/tools/echo_worktree_switch.py --target /var/lib/pantheon/echo-target --force` from the worktree. That is the same as the manual "touch the crate roots" workaround.
- The shared dir stays fine for builds whose result nobody relies on as evidence: release builds for deploy (a separate profile, a single builder) and scratch `cargo check` loops.

**General rule:** a shared build cache keyed on something other than the content of the inputs is a cross-talk channel. Relocatable artifact names plus mtime freshness means "same crate name" gets treated as "same crate".

Sibling: `bugs/` entry from c63768cec (shared CARGO_TARGET_DIR fs::copy EPERM on a foreign-owned OUT dir) covers the permissions face of the same shared dir.
