---
symptom: "A backgrounded compound command shaped `(cmd; ec=$?; ...; exit $ec) > log 2>&1; echo COMPLETE ec=$?` correctly captures the inner exit inside the subshell — but the outer shell's trailing echo becomes the LAST command, and its exit 0 is what the harness observes. The notification reports exit 0 while the test suite (or whatever ran inside) actually failed."
fix: "The very last thing the background command executes must be an `exit $ec` (or equivalent), NOT a trailing echo/log line. Either drop the trailing echo entirely and rely on the subshell's own exit propagation, or end the compound with `; exit $ec` at the outermost level after any trailing prose. `subshell > log 2>&1 && echo ok || echo fail` is fine (short-circuit preserves the failure); `subshell > log 2>&1; echo done` silently overrides it."
domain: pantheon, bash, background-tasks, exit-code, subshell, harness, smoke, false-green
---

# Trailing echo after a background subshell masks the inner exit code

**Surfaced:** 2026-09-02, orchestrator T-3391 soak leg. Background command shape:

```bash
(cmd; ec=$?; echo "---EXIT-CODE:$ec---"; exit $ec) > /tmp/log 2>&1; echo "COMPLETE ec=$?"
```

The subshell correctly did `exit $ec` (test suite ec=1). But the outer shell then ran `echo "COMPLETE ec=$?"` and its own exit was 0. The harness's task-notification reported `exit code 0` — inspecting `/tmp/log` after showed `---EXIT-CODE:1---` and `FAILED (failures=2, errors=89)`, meaning the harness's "success" reading was wrong for the actual suite state.

## The gotcha

Shell exit-code semantics: the exit status of a sequence of commands `A; B; C` is the exit status of C. A backgrounded command is one script from the harness's view; its exit is the exit of its LAST statement, not the "most substantive" one.

Common shapes that trip this:
- `subshell > log 2>&1; echo COMPLETE` — echo overrides
- `smoke_cmd; date >> log` — date overrides
- `runner; touch /tmp/done` — touch overrides

## The rules

1. **Do NOT append trailing commands to a background-executed script whose exit code the harness will read.** Either omit them entirely, or bracket everything under a subshell that itself ends with `exit $ec`.

2. **Safe shapes** (harness sees the intended exit):
   ```bash
   # A: subshell exit is the outer exit
   (cmd; ec=$?; echo ---; exit $ec) > log 2>&1
   
   # B: short-circuit preserves failure through outer echo
   (cmd) > log 2>&1 && echo OK || { echo FAIL; exit 1; }
   
   # C: no trailing anything
   cmd > log 2>&1
   ```

3. **Unsafe shape** (echo overrides):
   ```bash
   (cmd; ec=$?; exit $ec) > log 2>&1; echo COMPLETE
   #                                    ^^^^^^^^^^^^ this becomes the exit
   ```

4. **After any smoke via this pattern, do NOT trust the notification's exit-code.** Grep the log for `Ran N tests` + `OK` / `FAILED` lines per `feedback_smoke_grep_all_test_result_lines`; the exit code is a proxy layer that this bug demonstrates is unreliable.

## Related

- `bugs/pipes-on-background-commands-eat-exit-codes-and-evidence.md` — sibling failure mode (pipe through tail masks exit)
- `bugs/pipeline-exit-code-masks-suite-failure.md` — same class via pipeline
- Memory pin `feedback_background_task_exit_code_is_last_command` — the fleet-wide discipline pin (this bug is the first ledgered instance under the pin)

## Keywords

background, run_in_background, exit code, subshell, trailing echo, ec=$?,
COMPLETE ec=, notification says exit 0, harness reports 0, actual failure,
task-notification, false success, silent failure, exit propagation, wrapper
overrides inner, last command wins, sequence exit semantics, smoke false-green,
T-3391 soak leg
