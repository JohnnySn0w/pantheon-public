---
symptom: "Piping a mutating or build command through tail/head (or any pipe) for output-trimming makes the shell's exit status reflect the LAST pipeline stage (tail/head, which almost always succeeds) — NOT the command itself. A failed cargo build, a git worktree-add that hit a mid-operation rollback, any nonzero-exiting mutator gets reported as success. The failure is masked; downstream logic proceeds as if it worked. Observed 2x same-day 2026-07-06: (a) implementer piping `cargo` through `tail` (build failures hidden until the reviewer countermeasured); (b) HO staging piped `git worktree add ... | tail -1`, swallowing git's atomic-rollback failure — the worktree dir vanished minutes later looking like a mystery (2026-07-06-worktree-vanish incident)."
fix: "Never pipe a mutating/build command through tail/head for trimming. Three correct shapes: (1) capture to a tmpfile then read/trim separately — `cmd > /tmp/out 2>&1; rc=$?; tail -20 /tmp/out; [ $rc -eq 0 ]`; (2) if you must pipe, gate on `${PIPESTATUS[0]}` (bash) — it holds the FIRST stage's exit code — e.g. `cmd | tail; [ ${PIPESTATUS[0]} -eq 0 ] || die`; (3) `set -o pipefail` at script top makes the pipeline exit nonzero if ANY stage fails. For `git worktree add` specifically: NEVER pipe it — its atomic rollback on partial failure is exactly the exit code you must see (see feedback_echo_worktree_vendor_symlink memory + the vanish incident). tee is safe for LIVE viewing (doesn't swallow — the piped-TO command's exit is what tee reports, but PIPESTATUS[0] still holds the real one)."
domain: bash, pipestatus, exit-code, pipefail, git-worktree, cargo, ci, mutating-commands
---

# Piping a mutating/build command through tail/head swallows its exit code

The single most reusable form of this: **a pipe's `$?` is the last stage's exit code.** `mutator | tail` reports whether `tail` succeeded, which it essentially always does — so the mutator's failure is invisible to any `$?`/`&&`/`|| die` check after it.

## Two instances that established the class (both 2026-07-06)

- **(a) build context — `cargo | tail` (T-1563).** An implementer piped `cargo` output through `tail` to trim the log; a build/test failure exited nonzero but the pipeline reported success. The reviewer caught it and left the standing note in T-1563's transition log: "piping cargo through tail swallows exit codes — check PIPESTATUS."
- **(b) mutating context — `git worktree add | tail -1` (worktree-vanish incident).** HO's staging piped `git worktree add` through `tail -1`. `git worktree add` does an atomic rollback if it fails partway (e.g. lock contention) — that rollback *is* a nonzero exit, and it got swallowed. The command looked like it succeeded; git then removed the half-created dir seconds-to-minutes later, presenting as a "mystery vanish." See `incidents/2026-07-06-worktree-vanish/README.md` (the ATTRIBUTION names this same PIPESTATUS class).

Two independent same-day instances of the identical shape → catalogued here per ORG-012's 2-instance bar.

## The fix, three shapes (pick by context)

1. **Capture-then-read (preferred for mutators):**
   ```bash
   cmd > /tmp/out 2>&1; rc=$?
   tail -20 /tmp/out          # trim for display
   [ $rc -eq 0 ] || { echo "FAILED"; exit 1; }
   ```
2. **`${PIPESTATUS[0]}` gate (bash — if you must pipe):**
   ```bash
   cmd | tail -20
   [ ${PIPESTATUS[0]} -eq 0 ] || die   # PIPESTATUS[0] = cmd's real exit
   ```
3. **`set -o pipefail`** at the top of a script — the whole pipeline exits nonzero if any stage does. Blanket safety for a script full of pipes.

## The rule of thumb

- **Never pipe `git worktree add`, `cargo` (when gating on success), or any mutator you check `$?`/`&&`/`||` on, through tail/head.** Capture to a file and trim the file.
- `tee` is fine for LIVE streaming (it doesn't change that PIPESTATUS[0] holds the truth), but the same PIPESTATUS discipline applies if you gate afterward.
- Smell test: "does anything after this pipe branch on whether the command succeeded?" If yes, the trimming pipe is lying to it.

## Cross-references
- `incidents/2026-07-06-worktree-vanish/README.md` — the mutating-context instance, full failure-shape.
- `tickets/done/T-1563.md` — the build-context instance (reviewer note).
- Memory: `feedback_echo_worktree_vendor_symlink` (never pipe worktree add), `feedback_watchdogs_must_be_harness_tracked` (adjacent "swallowed-signal" class).
