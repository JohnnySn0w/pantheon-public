---
symptom: "A multi-binary / workspace test run was reported green, but failures existed — the orchestrator stopped reading at the FIRST `test result:` summary and inferred 'the rest passed'. Later binaries carried FAILED lines that were missed (5 speechmatics failures slipped past; the reviewer gate caught it)."
fix: "Enumerate EVERY `test result:` line across ALL binaries and verify each says `ok` with `0 failed`; never infer pass from the first summary. A cargo workspace (and similar multi-suite runners) emit one `test result:` line per test binary."
domain: rust, cargo, testing, smoke, echo, workspace
---

## Symptom

A workspace `cargo test` (or any multi-binary/multi-suite runner) prints multiple `test result: ...` lines — one per binary. Reading only the first summary (e.g. the `silero` binary's `test result: ok`) and reporting "smoke green" misses `FAILED` lines emitted by later binaries. In the T-0557 echo deploy this hid 5 `speechmatics` failures; the smoke was declared green and only the reviewer's independent re-run caught it.

## Cause

A workspace run reports per-binary, not as a single aggregate. The first `test result:` line is NOT the whole-suite verdict — it's just the first binary's. The failure mode is pattern-matching the first `test result: ok` and stopping, conflating "first binary passed" with "the suite passed."

## Fix

Grep ALL the result lines and count them against the number of test binaries; check each one:

```
cargo test --workspace 2>&1 | grep -E '^test result:'
```

Every line must read `ok` with `0 failed`. If a binary errors before printing its summary, that's also a fail — don't treat a missing line as a pass. When reporting a smoke upward, quote the literal command-tail with every `test result:` line, never a delta-claim (D5).

## Discovered

- T-0557 echo deploy (2026-05-22). Postmortem: `notes/2026-05-22-echo-demo-integration-postmortem.md` §Reusable disciplines. The orchestrator's first pass stopped at the silero summary and missed 5 speechmatics failures; the reviewer gate caught it.
- Filed from ORG-012 sweep T-0874 (parent T-0869).

## Pairs with

- HO memory `feedback_smoke_grep_all_test_result_lines.md` (behavioral pin) + `feedback_echo_smoke_full_command.md` (echo smoke = fmt + clippy -D warnings + test, all three).
- `rulebook/directives.md` §D5 (smoke-verification discipline — literal command-tail, no delta-claims).

## Keywords

cargo test, workspace, test result, per-binary, multi-suite, smoke, the rest passed, false green, speechmatics, silero, T-0557, echo, reviewer gate, D5
