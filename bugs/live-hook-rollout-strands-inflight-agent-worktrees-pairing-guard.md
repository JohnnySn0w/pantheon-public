---
symptom: "An implementer on an agent/<task> branch suddenly cannot commit at all — the commit-msg hook refuses with 'hook-allowlist pairing drift (T-1102): <hook>.py wired in ~/.claude/settings.json but not in HOOK_ALLOWLIST[seat]', and full sweeps in the worktree sprout collateral reds in test fixtures that invoke the real repo hook (plus systemd drift-lint if the rollout touched a live unit). The worker's own diff is clean — the pairing guard compares the seat's LIVE settings.json (rollout applied) against the WORKTREE's checkout of tools/context_fill_monitor.py, which predates the rollout's allowlist commit."
fix: "Dispatcher authorizes cherry-picking the rollout's allowlist commit alone onto the agent branch (content-identical to main, dedupes at squash-merge); land any tools/systemd/ canonical sync on main separately (that red exists on main too). Never --no-verify, never hand-edit the worktree's allowlist. Prevention: the rollout owner cherry-picks the allowlist commit onto every live agent/* branch on that seat immediately after landing it on main."
domain: "hooks, allowlist, pairing-guard, agent-worktree, commit-refused, rollout, T-1102, pantheon"
---

# Live hook rollout strands in-flight agent worktrees (T-1102 pairing guard refuses ALL their commits)

## Symptom

An implementer on an `agent/<task>` branch suddenly cannot commit at all — the commit-msg hook refuses with `hook-allowlist pairing drift (T-1102): <hook>.py wired in ~/.claude/settings.json but not in HOOK_ALLOWLIST['<seat>']`. Full sweeps in the worktree also sprout collateral reds in `test_hermes_hook.py` / `test_forward_ref_commit_gate.py` (fixtures that invoke the real repo hook end-to-end) plus `test_systemd_drift_lint.py` if the rollout touched a live unit. The worker's own diff is clean; `git diff --stat` shows none of the failing surfaces.

## Cause

The pairing guard compares the seat's **live** `~/.claude/settings.json` (rollout already applied) against `HOOK_ALLOWLIST` in the **worktree's checkout** of `tools/context_fill_monitor.py` — which predates the rollout's allowlist commit. Any agent branch cut before the rollout is structurally unable to commit, no matter what it changes. Same mechanics as the metis 2026-07-28 incident (seat-side), but hitting every in-flight agent worktree on the seat that rolled out. First observed on `agent/T-2912` during the T-2692 urgent-mail rollout, 2026-08-12.

## Fix (surgical)

1. Dispatcher authorizes the worker to cherry-pick the rollout's allowlist commit alone onto the agent branch (content-identical to main; dedupes at squash-merge). This unblocks commits and clears the hook-fixture reds.
2. If the rollout added live systemd `Environment=` lines, land the canonical `tools/systemd/` sync on main (that red exists on main too, not just the branch).
3. Merge `origin/main` into the agent branch before review as usual; the cherry-pick vanishes into the merge surface.

Do NOT let the worker `--no-verify` (defeats the guard everywhere) or widen scope into `tools/context_fill_monitor.py` by hand (divergent-content risk vs the ratified entry).

## Prevention (dispatcher-side)

When rolling out a hook on a seat with in-flight agent worktrees: immediately after landing the allowlist commit on main, cherry-pick it onto every live `agent/*` branch on that seat (or brief the workers to). The rollout owner creates the stranding; the rollout owner clears it.

## Verification

On a worktree whose branch predates the allowlist commit, with the hook live in the seat's settings.json: `git -C /tmp/<worktree> commit --allow-empty -m test` → refused with the T-1102 pairing-drift message; after cherry-picking the allowlist commit, the same command succeeds (drop the empty commit afterwards). Observed exactly this shape on `/tmp/pantheon-config-T-2912` 2026-08-12 (branch at `14ebae4be2`, live settings wired with `urgent-mail-inject.py`, allowlist commit `f154db5c70` only on main).

## See also
[`lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md`](../lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md) — this bug is one of three that ground the pattern-tier lesson (with the test-suite-real-git-pull and ticket-cli-in-agent-worktree sibling bugs).
