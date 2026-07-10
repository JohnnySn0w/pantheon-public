# `gh pr checks` 403 under the fine-grained PAT → CI-gate watcher hangs silently

**Symptom.** A CI-gate poll loop built on `gh pr checks <n>` (or REST `repos/<o>/<r>/commits/<sha>/check-runs`) emits nothing forever. Both surfaces return 403 `Resource not accessible by personal access token` under the claude-user fine-grained PAT — it lacks Checks read permission. If the loop swallows the error (`|| { sleep; continue; }`), a persistently-failing poll is indistinguishable from "checks still pending": no events, no exit, no merge. Cost >1h on T-1095 / echo PR 125 (2026-06-11) before the activity-monitor poke surfaced it.

**Surgical fix.** Gate CI via the Actions API, which the PAT CAN read:

```
gh run list --repo example-org/<repo> --branch agent/<task> --json status,conclusion,workflowName
```

Poll until `status == "completed"`, then check `conclusion == "success"`.

**Loop-design rule (the general half of the bug).** Never write a watcher whose poll command failure path is silent. Either emit the error line as an event (`cmd 2>&1 || echo "POLL-ERROR: ..."`) or fail fast after N consecutive errors. A watcher that can only speak on success reads as "still running" through every failure mode — same shape as the monitor-coverage rule (silence is not success).

**Cross-refs.** [`gh-run-watch-spurious-exit-zero-under-fine-grained-pat.md`](gh-run-watch-spurious-exit-zero-under-fine-grained-pat.md) — **same root cause** (one-PAT-permission-gap: no Checks read), surfacing on the `gh run watch` command path instead of `gh pr checks`/check-runs; both fixes poll the Actions/run-status API. Memory pin `reference_git_auth_fine_grained_pat.md` (CI-gate bullet, 2026-06-11); ledger 2026-W24 entry -178 DECISIONS(4). If the PAT ever gains Checks read, `gh pr checks` becomes viable again — re-test before deleting this entry.
