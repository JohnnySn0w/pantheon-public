---
symptom: "test module 'hangs forever' — exits 124/143 with no summary line, sometimes after printing one test name"
fix: "re-run with a timeout well above the suite's real runtime (or none) before calling it a hang; time it"
domain: "python-unittest"
---
## Symptom

A test module appears to hang indefinitely:

```
$ timeout 8 python3 -m unittest tools.test_inbox_consumer -v
... test_c_auto_resolve_does_not_call_recovery_pane
Terminated                                   # exit 124 (timeout) or 143 (SIGTERM)
```

No `Ran N tests` line, no `OK`, no traceback. Under `-v` it looks like one specific
test "prints its name then never completes" — which reads as a precise, actionable
localization of the hang. It is not.

## Cause

`timeout N` kills the process at N seconds and the suite simply needed longer. From
outside, a slow suite and a hung suite are **byte-identical**: both give you a signal
death, no summary, and truncated output. The exit code tells you the timeout fired,
not why.

The `-v` detail is actively misleading: unittest prints each test's name **before**
running it, so whichever test happens to be executing when the axe falls looks like
the culprit. In a 262-test module, that is an arbitrary test.

Real numbers from the T-1962 case — every one of these passes clean:

| module | tests | runtime | probe used | verdict reported |
|---|---|---|---|---|
| `tools.test_inbox_consumer` | 262 | 38.9s | `timeout 8`, later `timeout 20` | "hangs indefinitely" |
| `tools.test_mailbox_mcp` | 15 | 22.6s | `timeout 20` | "hangs with NO output" |
| `unittest discover tools` (seed) | 3081 | 96.8s | never run to completion | "unusable" |

Three separate agents across two sessions reported these as hangs. Each inherited the
prior probe's timeout value and re-confirmed the "hang" without ever letting the suite
finish. The misdiagnosis then hardened into a ticket, an investigation, and a proposed
fix (module-level `SkipTest` injection into the seed generator) for a defect that did
not exist.

## Fix

Before recording "hangs" anywhere, let it run to completion at least once:

```bash
# Measure. No timeout, or one far above any plausible runtime.
/usr/bin/time -f "REAL=%es" python3 -m unittest tools.test_inbox_consumer
```

Then distinguish the two cases:

- **Terminates given enough wall clock** → it is SLOW. Not a hang. Fix is performance
  work, or nothing at all.
- **Still alive after minutes with no progress** → genuine hang. Localize it properly
  with `faulthandler`, which dumps where the process actually sits:

```bash
python3 -X faulthandler -c "
import faulthandler, unittest
faulthandler.dump_traceback_later(30, exit=True)   # real stack at 30s, not a guess
unittest.main(module='tools.test_inbox_consumer', argv=['x'])"
```

Two collection rules that make this failure mode cheap to avoid:

- **Never read `$?` through a pipe.** `cmd | tail -4; echo $?` reports `tail`'s status
  (always 0) and silently launders a timeout into an apparent pass — the inverse error,
  and it fired in this same investigation. Redirect to a file, capture the exit code,
  then inspect. See `pipeline-exit-code-masks-suite-failure.md`.
- **Grep the summary line, don't eyeball the tail.** `grep -E "^Ran |^OK|^FAILED"`.
  A run with no summary line did not finish, whatever the tail looks like.

## Discovered

- T-1960 (original report by implementer), T-1962 (investigation + resolution),
  2026-07-23. Resolved as invalid-premise: no defect, no code change.
- Sibling of the standing lesson "an adjacent signal is not the fact — verify at the
  defining surface." The defining surface for "does this hang" is a run that is
  allowed to terminate.

## Keywords

hang, hangs, hangs indefinitely, timeout 124, exit 143, SIGTERM, Terminated, slow test
suite, unittest discover, -v prints name then stops, faulthandler, dump_traceback_later,
misdiagnosis, false hang, probe timeout too short
