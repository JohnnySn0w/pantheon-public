---
symptom: "'gh run watch --exit-status' returns exit 0 while the run is still in_progress, falsely signalling CI success"
fix: "The PAT's 403 on the annotations endpoint is swallowed - poll 'gh run view --json status,conclusion' until completed instead of trusting watch exit codes"
domain: github, gh-cli, pat, ci
---
# Bug — `gh run watch` exits 0 spuriously under the fleet fine-grained PAT

**Surfaced:** 2026-06-24, Echo T-1296 merge (Orchestrator). Reusable across any agent using `gh` with the fleet fine-grained PAT (`gh_token.env`).

## Symptom

`gh run watch <run-id> --exit-status` returns **exit 0 while the run is still `in_progress`**, falsely signalling CI success. Acting on that exit code (e.g. proceeding to merge) fails downstream because the required status check hasn't actually completed.

## Cause

The fine-grained PAT lacks the permission `gh run watch` needs to read check-run **annotations**. The watch hits `HTTP 403: Resource not accessible by personal access token` on the annotations endpoint and **bails early with exit 0** instead of erroring — the 403 is swallowed, not surfaced as failure. The run itself keeps running; `gh` just stopped watching.

Observed alongside: `gh pr checks`, `commits/<sha>/check-runs`, and `commits/<sha>/status` all return `403 Resource not accessible by personal access token` under the same PAT. The PAT can read **run-level** metadata but not check/annotation detail.

**Exit code varies; neither value is the run's result.** 2026-06-24 it exited 0 (reads as a false pass). 2026-09-27 (pantheon-config PR #474, Orchestrator) it exited **1** with `failed to get annotations: HTTP 403`, while the run was still `in_progress` and later concluded `success`. That reads as a false failure. Treat any `gh run watch` exit under this PAT as "watch aborted", never as a CI verdict.

## Fix — poll run status instead of watching

Don't trust `gh run watch` exit codes under this PAT. Poll the run's top-level status (which the PAT *can* read) until `completed`, then check `conclusion`:

```bash
for i in $(seq 1 30); do
  st=$(gh run view <run-id> --repo <owner/repo> --json status,conclusion \
        -q '.status+"/"+(.conclusion//"pending")' 2>/dev/null)
  case "$st" in completed/*) echo "DONE: $st"; break;; esac
  sleep 30
done
```

`completed/success` → green; `completed/failure` → red. Run in `run_in_background:true` (foreground `sleep` is blocked in-harness) so it re-invokes on completion. For per-job detail use `gh run view <id> --json jobs` (also PAT-readable) to see which job is the long pole.

## Related

- [`gh-pr-checks-403-fine-grained-pat-silent-ci-watch.md`](gh-pr-checks-403-fine-grained-pat-silent-ci-watch.md) — **same root cause** (the fleet fine-grained PAT returns 403 on the GitHub Checks surface → silent CI-watch failure). That sibling is the `gh pr checks` / check-runs command path; this one is the `gh run watch` path. Both fixes prescribe polling the Actions/run-status API the PAT *can* read. Kept distinct because the commands differ; the shared root is the one-PAT-permission-gap (no Checks read).
- [[reference_git_auth_fine_grained_pat]] (memory) — the PAT's scope limits.
- `reference_echo_agent_branch_merge_flow` (memory) — the echo merge flow this gotcha surfaced inside; echo CI runs ~11-12 min, so a spurious early exit is easy to mistake for a fast pass.
