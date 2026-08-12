---
symptom: "A test/smoke command piped through tail/grep/head reports exit 0 while the suite is FAILING - the pipeline returns the LAST command's status, so the runner's non-zero exit is swallowed and a red suite reads green to any exit-code check"
fix: "Never trust an exit code through a pipe. Run the suite unpiped and capture to a file (cmd > log 2>&1; echo $?), or set -o pipefail in bash contexts, or check the runner's own summary line - and grep ALL result lines rather than trusting exit codes at all for gating"
domain: bash, smoke, ci, pipeline, false-green
---

# Pipeline exit code masks suite failure — `unittest | tail` returns tail's 0

**Date:** 2026-07-21 (Atlas, T-1922 baseline run; second same-day sibling: Metis's push-loop success-message-on-failed-rebase in T-1912)

Atlas's first T-1922 baseline run reported exit 0 while the suite was failing 9F/5E — they had piped `python3 -m unittest ... | tail`, so the pipeline's status was tail's. Caught by re-running unpiped. Same family as the SIGPIPE/head gotcha in echo-merge (a pipe changing the observable outcome of the command in front of it) and the wrong-layer-GREEN lesson (the exit code is a proxy layer; the result lines are the capability).

Audit candidates: any fleet smoke/CI/watchdog check that gates on `$?` of a piped command.

**Pattern lifted to lesson** (ORG-012 criterion 1, T-1948): `lessons/2026-07-22-pipeline-exit-code-masks-command-failure.md` — three-instance promotion of the pipeline-exit-status-masks-command-failure class.
