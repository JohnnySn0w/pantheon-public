# SOP: Staging a worker dispatch (worktree shape per dispatch type)

**Status:** Adopted 2026-09-30 by Orchestrator (HO) under msg-3733 principles-as-ratification (below-goal org-practice methodology; ORG-012: HO authors SOPs). Filed under T-4465 so it can render as an auto-triggering skill. It consolidates CLAUDE.md §"Agent branch convention — HO dispatch discipline" and the HO memory pins on dispatch cwd, isolation worktrees and the echo vendor symlink, corrected by a 2026-09-30 accuracy review. The shapes were verified by bail-outs and clean runs in 2026-05 to 2026-09 (T-0632, T-0658, T-1031, T-1792, T-2709, T-3074, T-3289/T-3290). It applies to any orchestrator tier that spawns workers. The operator override-available.

## When to use this

Right before any `Agent(...)` call that spawns an implementer, reviewer or project-orchestrator on a code repo. The staging differs by dispatch type. Worker personas refuse to run from a canonical main checkout (`/home/claude/<repo>` or your seat's equivalent): they check the session-start cwd, and a brief's "cd /tmp/…" cannot rescue a bad spawn cwd.

## Procedure

**Always, in every shape:**
- Name the branch `agent/<ticket>` in the brief.
- Open the brief with its intent line (what the work is FOR), and carry the chain-of-justification rule explicitly: a DECISIONS section citing intent first, then P/D ids; required recursively of anyone the worker dispatches; the bubble-up list partitioned.
- Brief the `HERMES\.md` byte-sequence rule when the output could touch it.

### 1. Implementer (code-writing): pantheon-config, pantheon-pwa, other non-echo repos

- `cd /home/claude/<repo>` in the Bash call right before the Agent call. The harness worktree follows the session cwd's repo; it has landed in the wrong repo when cwd drifted.
- Dispatch with `isolation: "worktree"`. The worktree appears at `<repo>/.claude/worktrees/agent-<id>` on branch `worktree-agent-<id>`.
- Brief the worker's first actions:
  1. `pwd` and `git rev-parse --show-toplevel` are not the main checkout;
  2. `git branch --show-current` is the harness default, then `git branch -m agent/<ticket>`, then re-check.
- **Plan-first:** the plan turn must `touch .plan-first-keepalive` (untracked, deleted before the final commit). Otherwise the harness auto-cleans an unchanged worktree at the turn boundary.
- Don't pre-stage a `/tmp` worktree on `agent/<ticket>` for this shape. The rename collides with it.
- Afterwards: `cd /home/claude` so the next dispatch doesn't inherit a repo cwd.
- This supersedes `pantheon-dispatch` skill §6 rules (a)-(c) ("HO pre-stages always; `isolation: "worktree"` not trusted"), which predate the per-dispatch-type shapes. Rule (e), cwd out of the main path before every spawn, still stands.

### 2. Implementer on echo-prismatic

- `isolation: "worktree"` is NOT usable. A fresh worktree misses the gitignored `vendor/` symlink, which gives 9/9 silero_vad failures (T-0658).
- Pre-stage with `/home/claude/pantheon-config/tools/stage-echo-worktree.sh` (absolute path) into `/tmp/echo-<ticket>` on `agent/<ticket>`. It also emits the worktree's `auth-smoke` wrapper. `cd /home/claude` in the same cell as the Agent call.
- Brief:
  - the first action is `cd /tmp/echo-<ticket>`, then verify toplevel and branch;
  - "your shell starts at /home/claude; that's expected, don't bail or ask for re-staging" (some workers otherwise bail);
  - dev loops run cargo through `~/pantheon-config/tools/echo-cargo.sh <cargo args>` (T-4822): it sets the shared target, `umask 002`, and re-owns peer-owned build-script OUT files first, which plain `cargo` skips (fs::copy EPERM, T-4071). Equivalent bare env, if the wrapper is unavailable: `export PATH="$HOME/.cargo/bin:$PATH"; umask 002; export CARGO_TARGET_DIR=/var/lib/pantheon/echo-target` plus `python3 ~/pantheon-config/tools/echo_target_adopt.py` before each build. Echo's smoke is fmt + clippy `-D warnings` + test;
  - the authoritative (APPROVE-gating) smoke runs ISOLATED: `./auth-smoke bash -c '<CI chain>'` in the worktree, or `python3 ~/pantheon-config/tools/echo_evidence_build.py -- <cmd>` (T-4122). A shared-target result is not evidence (echo `CLAUDE.md`).
  - **never hand-roll an isolated target** (`CARGO_TARGET_DIR=/var/lib/pantheon/echo-evidence-targets/...` or any per-worktree dir set by hand). Only `auth-smoke` or `echo_evidence_build.py` may create one, because they write the `.evidence-meta.json` that lets `--gc` reclaim the dir. A hand-rolled target has no meta and leaks disk: 16.6G of such orphans caused the 2026-10-09 91% disk warning (T-4795 is the GC backstop; T-4813 is this front door). Brief echo workers with this line.

### 3. Reviewer (read-only), any repo

- `pwd` then `cd /home/claude` IMMEDIATELY before the Agent call, in the same tool cell. A prior `cd /home/claude/<repo> && …` persists and the reviewer bails.
- Pre-stage: `git -C /home/claude/<repo> worktree add --detach /tmp/<repo>-<ticket>-review agent/<ticket>`. `--detach` is required when the branch is checked out in the implementer's worktree (git refuses otherwise); don't use `-b`.
- Dispatch WITHOUT `isolation: "worktree"`. That would create a fresh branch that fights the read-only shape.
- Brief:
  - the first action is `cd /tmp/<repo>-<ticket>-review`, then verify with `pwd` and `git rev-parse --show-toplevel`;
  - "the worktree is DETACHED at the tip of `agent/<ticket>`: `git branch --show-current` is empty by design. Verify `git rev-parse HEAD` equals `git rev-parse agent/<ticket>` instead";
  - read-only (no rename, no commit, no push);
  - diff against `origin/main` after a fetch;
  - name any destructive side effects the checks could have;
  - the reviewer RE-RUNS the smoke (P7; don't trust the implementer's numbers). For echo, through the isolated path in shape 2. On the loaded 4-core host, pin plain suite runs (`taskset`) with a bounded `-n`, as a courtesy.
- After the arc: `git worktree remove /tmp/<repo>-<ticket>-review --force`.

### 4. Project-orchestrator dispatch, and HO-direct work

- `git -C /home/claude/<repo> worktree add /tmp/<repo>-<ticket> -b agent/<ticket> origin/main`, and confirm with `git -C /tmp/<repo>-<ticket> branch --show-current`.
- Name the path in the brief. Tell the orchestrator that ITS reviewer spawns inherit its shell cwd, so shape 3 applies recursively (`pantheon-dispatch` skill §6 rule (e)).
- The orchestrator's session cwd is usually not a repo, so its own `isolation: "worktree"` spawns fail. Brief it to have its implementer work directly in the pre-staged `/tmp/<repo>-<ticket>` worktree.
- **Completion notices for the orchestrator's workers reach the HO, not the orchestrator** ([`bugs/nested-subagent-completion-notification-routes-to-main-not-spawner.md`](../../bugs/nested-subagent-completion-notification-routes-to-main-not-spawner.md); the full relay contract is in [`dispatch-and-wait-liveness.md`](dispatch-and-wait-liveness.md) §Self-spawning intermediate orchestrators). Both sides compensate:
  - In the brief, tell the orchestrator to POLL its workers' state when idle (commits on `agent/<ticket>`, worktree mtime, their transcripts) instead of waiting for a wake.
  - The HO forwards the substance of any worker completion that lands in its session to the orchestrator with SendMessage at once.
- Brief every waiting step to use `run_in_background` and its completion notice, or to poll an output file. Never `until ! pgrep -f <pattern>`: the pattern matches the waiting shell's own command line, so the loop never exits.

## Common failure modes

- A reviewer spawned from a main-checkout cwd bails with "main-path discipline violation" (T-3289/T-3290 v1).
- A reviewer bails on an empty `git branch --show-current` in a detached review worktree, because the brief didn't say it's detached by design.
- A plan-first worktree vanishes between the plan turn and approval (no keepalive).
- The harness worktree lands in the wrong repo because cwd was in another repo.
- Parallel Bash calls share one cwd. Use `git -C <abs>` for staging commands issued in parallel.
- A worker told to write a guarded canonical path (`rulebook/`, `sop/`, `architecture/adr/`, `ledger/`, `notes/`, README…) refuses, and the deliverable is lost. Brief "return it in your report" and the orchestrator commits it. The worker-guard can also block a Bash heredoc whose prose merely mentions guarded operations, so ask for long reports inline.
- Forgetting the orphan worktree: after the arc, remove any leftover `.claude/worktrees/agent-<id>/` dir.
- Tearing a worktree down before its commits are confirmed on the branch (`git log <branch> --oneline | grep <sha>`).

## Boundary

- PR and merge stay HO-exclusive, whatever the shape. A dispatched worker (subagent) never pushes, so the HO pushes its branch; a peer-orchestrator seat pushes its own (`merging-an-agent-branch-pr.md`, T-4549).
- Stress or load loops in any brief go through `tools/confined_load.sh` (T-4349).

## Cross-references

- CLAUDE.md §Agent branch convention (the pinned summary of this SOP).
- `plan-first-dispatch.md`, `cross-agent-dispatch-discipline.md`, `team-orchestrator-dispatch-checklist.md`.
- `pantheon-dispatch` skill §6 (rule (e) stands; (a)-(c) superseded as above).
- echo-prismatic `CLAUDE.md` (isolated authoritative smokes, T-4122).
