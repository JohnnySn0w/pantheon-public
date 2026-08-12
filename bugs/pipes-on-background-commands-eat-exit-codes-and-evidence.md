---
symptom: "Backgrounding a command piped through tail/head (cmd 2>&1 | tail -20) reports the pipeline's exit code as tail's (0) rather than the underlying command's, so a failing smoke run silently reports success, AND the output file only retains the last N lines, destroying the evidence needed to enumerate all 'test result:' lines or find where a hang started."
fix: "Never pipe a background command through head/tail/grep at launch — redirect raw output (> file 2>&1, e.g. via the task's own output capture) and filter at READ time instead of at launch time; if piping is unavoidable use set -o pipefail; after any smoke enumerate every 'test result:' line from the full log rather than trusting a bare exit-0."
domain: pantheon, bash, pipestatus, exit-code, background-tasks, smoke, testing
---

# `cmd | tail -N` in background tasks eats the exit code AND the evidence

**Surfaced:** 2026-07-12, T-1686 (echo smoke run) — bit the same session twice.

## The gotcha

Backgrounding `long_running_thing 2>&1 | tail -20`:
1. The task's exit code is TAIL's (0), not the command's — a failing smoke reports success.
2. The output file holds only the last N lines — when you later need to enumerate all
   `test result:` lines (per feedback_smoke_grep_all_test_result_lines) or find where a hang
   started, the evidence is gone.

Related same-session trap: `pkill -f <pattern>` where the pattern text also appears in your
OWN command line (e.g. killing tunnels by `-L 3232:` while the same command opens one) —
self-match kills your own shell with exit 144. Bracket-trick the pattern AND keep the
kill in a separate command from anything embedding the literal.

## The rules

1. Background commands: redirect RAW (`> file 2>&1` implicit via task output) — never pipe
   through head/tail/grep at launch. Filter at READ time.
2. If you must pipe, `set -o pipefail` — but for background evidence capture, just don't.
3. After any smoke: enumerate every `test result:` line from the FULL log; "exit 0" through
   a pipe proves nothing.

## Keywords

pipe, tail, head, grep, exit code, pipestatus, pipefail, set -o pipefail, background
task, smoke run, test result:, pkill -f, self-match, kill own shell, exit 144, tunnel,
-L 3232, echo smoke, T-1686, evidence destroyed, log truncation, silent success

**Pattern lifted to lesson** (ORG-012 criterion 1, T-1948): `lessons/2026-07-22-pipeline-exit-code-masks-command-failure.md` — three-instance promotion of the pipeline-exit-status-masks-command-failure class.
