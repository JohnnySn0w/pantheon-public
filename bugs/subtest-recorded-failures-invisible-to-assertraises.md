---
symptom: "A meta-test that wraps another test method in assertRaises to prove the inner test CAN fail never sees the failure: unittest's subTest RECORDS failures on the running TestResult instead of raising, so assertRaises catches nothing — and the recorded failure additionally leaks out as a spurious failure of the OUTER test run."
fix: "Refactor detection out of assertion: put the scan in a plain helper that RETURNS offenders (e.g. _resolved_trackers() -> list), have the real test assert the list is empty, and have the meta-test call the helper directly on a synthetic fixture and assert on its return value. Never invoke a test method to observe its failure when the method uses subTest — the failure never propagates as an exception."
domain: pantheon, python, unittest, subTest, assertRaises, meta-test, test-of-test
---

# subTest-recorded failures are invisible to assertRaises (and leak into the outer run)

## Mechanism

`unittest`'s `self.subTest()` context manager implements soft-fail: on an assertion failure inside
the block it catches the exception, records it on the active `TestResult`, and CONTINUES — by
design, so one parametrized case's failure doesn't hide the rest. Consequence: the failure is never
re-raised to the caller. A meta-test doing `with self.assertRaises(AssertionError): self.test_x()`
around a subTest-using method gets nothing to catch (the assertRaises itself then fails with
"AssertionError not raised"), while the recorded sub-failure still lands on the shared TestResult —
surfacing as a confusing extra failure attributed to the meta-test.

## Why it hides

Both failure messages are individually plausible ("not raised" reads like the guard didn't detect;
the leaked record reads like the guard mis-fired), so the natural first response is to debug the
DETECTOR, which is actually working. The real defect is the observation channel.

## The fix shape

Scan-vs-assert split, same shape the pgrep lint itself uses: a pure function returns the offender
list; the guard test asserts it is empty; testability of the guard comes from calling the function
on fixtures, not from invoking the guard test and trying to observe its failure.

## Provenance

Mason, T-1958 (2026-07-22), building the meta-test that fails when a KNOWN-VIOLATION lint entry's
tracking ticket is already resolved — their first attempt wrapped the guard test in assertRaises and
hit both symptoms; the refactor made the check testable at all.

## Cross-refs

- `tools/test_pgrep_predicate_lint.py` — the living example post-T-1958.
- `bugs/bash-c-tail-call-exec-replaces-argv-in-fake-process-fixtures.md` — sibling T-1956-arc find;
  both are "the test scaffolding lies, not the code under test" class.
