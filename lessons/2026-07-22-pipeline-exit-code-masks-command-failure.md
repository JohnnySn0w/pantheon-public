# Pipeline exit status masks command failure — a failing command reads GREEN behind a succeeding pipe

**Date authored:** 2026-07-22 (T-1948, ORG-012 sweep promotion — criterion 1, three catalogued instances + a fourth live-caught, all same mechanism)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a repo-script lint flags `... | tail`/`| grep`/`| head` used as a success gate (structural enforcement retires the prose rule); or a fifth instance appears in a stage this enumeration doesn't cover.

## The mechanism

A shell pipeline's exit status is the status of its **last stage only**. So when the failure-bearing command is not last — `some_command | tail`, `some_command | grep`, `some_command &`, `cmd | consumer` — a non-zero exit from the command you actually care about is **discarded**, replaced by the (usually zero) status of the tail/grep/consumer. A command that FAILED reads GREEN to any `$?` / exit-code check downstream. The proxy (the pipeline's aggregate status) and the defining signal (the command's own status) agree right up until the moment the command fails and a later stage succeeds — which is exactly the moment you needed them to disagree.

## Catalogued instances (one mechanism, four surfaces)

1. **Mutating / build command through `tail`/`head`** — `bugs/pipe-through-tail-head-swallows-exit-code.md`: a build or state-changing command piped to `tail` reports success while it failed; `head` additionally SIGPIPEs the producer.
2. **Backgrounded pipe** — `bugs/pipes-on-background-commands-eat-exit-codes-and-evidence.md`: a `&`-backgrounded pipeline loses both the exit code and (often) the evidence.
3. **Test suite through a filter (2026-07-21, ×2)** — `bugs/pipeline-exit-code-masks-suite-failure.md`: `python3 -m unittest … | tail` returns `tail`'s 0 while the suite is FAILING. Hit independently by two agents the same day (Atlas's T-1922 baseline; the BT-5 implementer's first smoke) — both caught it by re-running unpiped.
4. **`PIPESTATUS[0]` of the wrong stage (live-caught, was uncatalogued)** — measuring a guard with `echo | hook | grep`, `PIPESTATUS[0]` is the *echo*, not the hook; briefly recorded a BLOCKING guard as PASSING. Recovered only because the author had read instance 1 closely hours earlier while diagnosing an unrelated defect in that same file (T-1929) — an entry another agent had catalogued months before under T-1613. The discovery surface worked *across authors and across months*, consumed by someone who was a stranger to the entry — which is the argument *for* it, and the load-bearing thing `bugs/README` and T-1912 exist to protect.

(Counted by reading each symptom line, not by grep: `bugs/sudo-S-heredoc-stdin-collision-false-auth-failure.md` matches a naive `pipe` grep but is a DIFFERENT mechanism — a heredoc replacing sudo's stdin — so it is three instances of this class, not four.)

## The rule

**Never gate on the exit code of a pipeline whose failure-bearing stage is not last.** In preference order:

1. **Don't pipe the thing you're checking.** Run it unpiped, capture to a file, check `$?`, then filter the file: `cmd > out 2>&1; rc=$?; grep … out`.
2. **`set -o pipefail`** (bash) — makes the pipeline return the first non-zero stage's status. Cheap and correct for most cases; know that it's not the default.
3. **`PIPESTATUS[N]` for the RIGHT N** — and confirm N is the failure-bearing stage, not reflexively `[0]` (instance 4's exact trap).
4. **For test suites, don't gate on an exit code at all** — grep ALL result lines and enumerate every FAILED (the smoke-discipline rule); a delta or an exit code both hide pre-existing failures.

## Why this is the proxy-for-the-defining-surface pattern

This is the shell-exit-status face of the same pattern as `lessons/2026-07-21-wrong-layer-green-liveness-checks.md`: a decision reads a **proxy signal** (the pipeline's aggregate status) in place of the **defining one** (the command's own status), and they diverge precisely on failure. The liveness lesson is the process/channel/auth-*layer* version; this is the exit-*status* version. Same discipline both times: **identify the defining signal and read THAT; if you must read a proxy, name it as a proxy and name the failure it cannot see.** The tell is identical — "green because correct" vs "green because the check couldn't see the failure." (These two, plus the same-day control-vs-convention cluster — T-1937/1939/1944/1945/1946, controls that read as mechanisms but aren't — are candidates for a single umbrella synthesis once those decisions land.)

## Cross-references

- `bugs/pipe-through-tail-head-swallows-exit-code.md`, `bugs/pipes-on-background-commands-eat-exit-codes-and-evidence.md`, `bugs/pipeline-exit-code-masks-suite-failure.md` — the three catalogued instances.
- `lessons/2026-07-21-wrong-layer-green-liveness-checks.md` — the proxy-vs-defining-surface sibling (liveness layer).
- T-1948 (this promotion); the smoke-discipline "grep ALL result lines" rule (`feedback_smoke_grep_all_test_result_lines`).
