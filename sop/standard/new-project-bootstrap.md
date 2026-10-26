# New-project bootstrap — repo + tests as base work requirement

**Status:** Adopted 2026-06-24 by Orchestrator (HO) — the operator proposed it directly (Telegram msg 4070, "new project sop where repo and tests are setup as a base work requirement?") right after the auto-karaoke bootstrap proved the pattern. Below-goal-level org-process SOP codifying his own suggestion; adopted per msg-3733 (principles-as-ratification). The operator override-available.

## When to use this

At the **start of any new project intended to persist or ship** — a tool, a service, a multi-file script that the operator or another agent will run again. Bootstrap the repo and a Tier-1 test harness *before* the project is considered real work-in-progress, and have both in place before the first deliverable is declared done.

Applies to personal projects (auto-karaoke, vibecamp-*) and Pantheon projects alike. The discipline is the same; only the remote/merge conventions differ per project.

**Project formation precedes bootstrap** (per ORG-054, ratified 2026-08-24; operationalized in `sop/standard/project-lifecycle.md`). A project's identity — `project_id`, telos, parent, lifecycle — is established BEFORE any repo work, per that SOP's Formation section. Bootstrap does not create a project; it realizes an already-formed project as a fresh code artifact.

Three cases + the routing this SOP owns:

1. **New project + new dedicated repo** → THIS SOP. Bootstrap the repo + Tier-1 baseline as below.
2. **New project embedded in an existing repo (no dedicated repo)** → SKIP THIS SOP. An "embedded project" (e.g. a script realized as a directory inside a shared repo, no independent build/release/support cadence, no distinct authority boundary) legitimately has no repo of its own. ORG-055's boundary tests explicitly do NOT force a repo per project. Consumers reach the code via the parent repo's remote; there's nothing for this SOP to bootstrap.
3. **Existing code being carved into its own dedicated repo** → `sop/standard/repository-extraction.md`, NOT this SOP. Extraction is a distinct transaction with its own inventory, migration, and cut discipline.

**Skip** for genuine one-shots: a throwaway diagnostic script, a one-render scratch job, anything that runs once and is discarded. If you find yourself running it a second time or handing it to someone, it has crossed into this SOP's scope — bootstrap it then.

## Procedure

> Before repository initialization, run the project-formation section of the
> project-lifecycle SOP. A persistent project must already have a canonical
> project record, stable `project_id`, telos, classification, boundary,
> stewardship, developmental mode, operational obligations, and an explicit
> nullable `parent_goal_ref`. Creating a new telos is goal-level. Joining a
> composed program requires the parent decision. A standalone project records
> `parent_goal_ref: null`; no framework default supplies a root.
>
> A bounded exploratory probe may precede formation when it is labeled as a
> probe, has no persistent deployment or independent obligations, and names
> the ticket intent that bounds it. Reuse, handoff, persistence, or a new telos
> triggers formation before repository bootstrap.

The numbered steps below are the **repository** procedure — they realize an already-formed project as a fresh code artifact. `git init` (step 1) is the first *repository* action, not the first *project* action.

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

This SOP covers *bootstrapping the one-time repo + Tier-1 baseline for a newly-formed project*. It does NOT:

- **Form the project itself** — that's ORG-054 project identity, upstream of any repo work. Repository bootstrap does not create project identity, choose its parent, change developmental mode, or close the project — see `sop/standard/project-lifecycle.md` for all of those.
- **Cover embedded projects** — projects that legitimately don't need their own repo (see When to use this §case 2).
- **Cover extractions** — carving existing code into its own repo has its own SOP (`repository-extraction.md`).
- **Prescribe the project's ongoing test strategy, CI, or branch protection** — those are per-project decisions made once the project has shape.

It is a floor (no persisting project that gets a dedicated repo starts without a repo + a passing baseline), not a ceiling.

## Cross-references

- `architecture/adr/ORG-054-project-ontology-and-upward-justification.md` — project identity/formation authority upstream of this SOP.
- `sop/standard/project-lifecycle.md` — formation, developmental-mode transition, reorientation, replacement, split, merge, and retirement/closure procedure this SOP's precondition points at.
- `sop/standard/repository-extraction.md` — the SOP for carving existing code into its own dedicated repo (case 3 above).
- `feedback_github_per_repo_deploy_keys` — Pantheon code-repo remote convention.
- `sop/standard/plan-first-dispatch.md` — when the bootstrap is large enough to dispatch an implementer.
- `feedback_project_digestion_loop_candidate_sop` (memory) — the infra→actualize→ticket-next-phase loop this bootstrap is the first beat of.
- D5 (smoke verification) — the green-before-done discipline this extends to project birth.
