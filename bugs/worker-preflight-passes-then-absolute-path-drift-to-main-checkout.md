---
symptom: "A worker's cwd pre-flight passes (git rev-parse/branch verified IN the worktree) but every subsequent Read/Edit/Write/Bash uses an absolute path prefix pointing at the CANONICAL MAIN CHECKOUT (~/echo-prismatic) instead of the worktree (/tmp/echo-T-NNNN) — the entire implementation lands in the wrong tree. Pre-flight verifies the shell's location once; it does not bind later absolute-path tool calls."
fix: "Worker-side: after pre-flight, derive every path from the verified toplevel (relative paths from cwd, or a $WT variable), and re-check `git -C <dir-of-file-being-edited> rev-parse --show-toplevel` before the FIRST edit. Recovery if caught: diff+capture all changes, restore main (git checkout -- . + remove untracked additions ONLY — never touch gitignored operational files like .env), re-apply in the worktree, re-run ALL verification from the correct tree. Orchestrator/HO-side: after any such incident, independently verify the canonical checkout (status clean + gitignored operational files intact: .env, vendor/, target/release binary, live configs) before trusting the daemon/auto-consumer surfaces."
domain: pantheon, echo, worktrees, dispatch-pattern, claude-code-harness
---

# Worker pre-flight passes, then absolute-path drift into the main checkout

## Observed (T-1656 implementer, 2026-07-10)

Pre-flight was correct (`git rev-parse --show-toplevel` → `/tmp/echo-T-1656`).
The implementation phase then used `/home/claude/echo-prismatic/...` absolute
paths throughout — new crate, core edits, config edits all landed in the main
checkout on `main`. Caught mid-task by the worker itself (a suspiciously-clean
background check prompted a `git status`); recovery executed textbook: capture
diff + untracked additions → restore main to pristine → re-apply in worktree
(byte-verified) → re-run all verification from the worktree. Main was never
committed or pushed; HO and orchestrator independently verified the checkout
plus gitignored operational files (.env, vendor, release binary) intact.

## Why this is distinct from the dispatch-cwd pin

`reference_reviewer_cwd_tmp_worktree_dispatch` covers the SPAWN inheriting a
wrong cwd. Here the cwd was right and verified — the drift came from the
worker composing absolute paths from the repo's canonical location (its
training-data-shaped default) instead of its verified toplevel. Pre-flight is
a point-in-time check; path composition is per-call.

## Brief clause to add where it matters

> Your verified toplevel is `<worktree>`. Compose EVERY file path from it
> (relative to cwd or via that prefix). The canonical checkout path
> (`~/<repo>`) must not appear in any tool call — if you notice it, stop and
> re-verify state before continuing.

## Stakes reminder

The canonical echo checkout is LIVE OPERATIONAL SURFACE: run-echo.sh sources
its .env and execs its target/release/echo; the auto-consumer reads disk
state. Contamination there isn't just git hygiene — it can alter or strand
the running daemon. (This incident's recovery correctly left .env untouched;
verify that explicitly every time.)
