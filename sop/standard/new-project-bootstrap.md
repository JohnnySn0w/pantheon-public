# New-project bootstrap — repo + tests as base work requirement

**Status:** Adopted 2026-06-24 by Orchestrator (HO) — the operator proposed it directly (Telegram msg 4070, "new project sop where repo and tests are setup as a base work requirement?") right after the auto-karaoke bootstrap proved the pattern. Below-goal-level org-process SOP codifying his own suggestion; adopted per msg-3733 (principles-as-ratification). The operator override-available.

## When to use this

At the **start of any new project intended to persist or ship** — a tool, a service, a multi-file script that the operator or another agent will run again. Bootstrap the repo and a Tier-1 test harness *before* the project is considered real work-in-progress, and have both in place before the first deliverable is declared done.

Applies to personal projects (auto-karaoke, vibecamp-*) and Pantheon projects alike. The discipline is the same; only the remote/merge conventions differ per project.

**Skip** for genuine one-shots: a throwaway diagnostic script, a one-render scratch job, anything that runs once and is discarded. If you find yourself running it a second time or handing it to someone, it has crossed into this SOP's scope — bootstrap it then.

## Procedure

1. **`git init`** in the project root.
2. **`.gitignore` first** — exclude heavy/generated artifacts before the first `git add`, so they never enter history: virtualenvs, build/work/output dirs, caches, and large media (`*.mp3 *.mp4 *.wav *.npz` etc.). Source only is tracked.
3. **Test harness** — install the runner (pytest / cargo test / etc.) and a `conftest`/path shim so `src.*` imports resolve from anywhere.
4. **Tier-1 tests** — cover the **pure, deterministic logic** the project's correctness rests on (parsers, transforms, run-detection, formatting/escape helpers). Test on synthetic input — do NOT require network, audio decode, model loads, or other heavy deps in the unit layer. Aim for the load-bearing functions, not 100% coverage.
5. **Green before commit** — run the suite; it must pass before the initial commit.
6. **Commit** source + tests (heavy excluded by step 2).
7. **Create + push the remote** per the project's convention:
   - Personal project: `gh repo create <owner>/<name> --private --source=. --push`. Note `gh` defaults the new remote to **SSH**; if the box has no SSH key, the repo is still created but the push 403s — re-point to HTTPS and push with the PAT (`https://x-access-token:<PAT>@github.com/...`), then scrub the token back out of `.git/config` (push with the token URL inline rather than `set-url`-persisting it).
   - Pantheon code repo: per-repo deploy key + SSH alias (see `feedback_github_per_repo_deploy_keys`), `agent/<task>` branch flow, no self-merge unless the repo is on the self-merge default list.
8. **Verify** the remote ref exists (`git ls-remote --heads origin`).

## Common failure modes

- **`.gitignore` after first add** — heavy artifacts already staged enter history and bloat the repo. Write it *first*.
- **Testing the wrong layer** — unit tests that need librosa/torch/network are slow and flaky; they belong in an integration tier, not Tier-1. Keep the pure logic separately testable (e.g. `runs_from_f0(midi, times)` split from `detect(path)` so the run-detection tests on arrays, not audio).
- **`gh repo create --push` SSH 403** — looks like a permissions failure but the repo was created fine; it's just the SSH-default remote. Switch to HTTPS+PAT (above).
- **Persisting the PAT in `.git/config`** — never leave the token in the remote URL; use it inline on the push command or scrub it after.

## Boundary

This SOP covers *bootstrapping* — the one-time repo + Tier-1 baseline. It does NOT prescribe the project's ongoing test strategy, CI, or branch protection; those are per-project decisions made once the project has shape. It is a floor (no persisting project starts without a repo + a passing baseline), not a ceiling.

## Cross-references

- `feedback_github_per_repo_deploy_keys` — Pantheon code-repo remote convention.
- `sop/standard/plan-first-dispatch.md` — when the bootstrap is large enough to dispatch an implementer.
- `feedback_project_digestion_loop_candidate_sop` (memory) — the infra→actualize→ticket-next-phase loop this bootstrap is the first beat of.
- D5 (smoke verification) — the green-before-done discipline this extends to project birth.
