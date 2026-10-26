# When code under review has real side effects, live-repro beats static reading

Filed 2026-09-16 (orchestrator) after T-3782 subagent-reviewer round-4 review found the
fence-self-heal-destroys-worktree HIGH bug by literally letting the destructive
side effect fire against their own review worktree — a discovery a strict
static-reading pass would have missed.

## The pattern

Static code review + smoke loop passes are the default review shape. Both
prove the code is INTERNALLY consistent. Neither proves the code is safe to
INVOKE from the invocations you haven't thought of.

When the code under review calls out to real, destructive side effects —
`git reset --hard`, `os.remove`, `subprocess.run` against real paths, network
POSTs to production — a review pass that only READS the code + runs the
happy-path test suite can miss a whole class of failure: the destructive path
firing under an invocation shape the tests don't cover.

The catch is that TEST INVOCATION IS ITSELF an invocation shape. Every
`python -m unittest` and `pytest -n auto` run is a real process with a real
cwd hitting real code. If the code under review has a side effect that fires
based on the process's environment (cwd, env vars, module-relative paths),
running the tests can EXECUTE the side effect against whatever real state
the test process happens to sit on.

**The pattern is: when the code under review is destructive on real state,
add a live-repro axis to the review shape.** Run the test suite in a
controlled environment where the destructive side effect would matter, and
watch whether it fires. Instrument via a subprocess-interceptor if needed
(blocks-and-tracebacks rather than executes). Reflog / audit-log evidence
before/after asserts non-invocation.

## Example (T-3782 round 4)

The fence-lifecycle code calls `git reset --hard origin/main` when it
detects a divergent HEAD. The review agenda listed axes A-H covering the
predicate's correctness, atomic persistence, test coverage, autouse
fixture, etc. All 8 axes passed on static + smoke.

Axis H (autouse fixture verification) was where the reviewer went beyond
"read the fixture, check what it excludes." They asked: "does the fixture
actually PREVENT the destructive path from firing during real test
invocations, or does it just claim to?" — and validated by running the tests
against a real worktree with reflog watching.

The reflog caught the reset firing at 22:09:03Z under plain unittest and
AGAIN at 22:32:50Z under xdist-parallel pytest. Neither the static reading
nor the fixture-code-inspection would have surfaced the pytest-xdist
nondeterminism — it required live-repro to see the timing-race.

## When to reach for live-repro

- Code under review calls destructive shell operations (`git reset --hard`,
  `rm -rf`, `truncate`, force-pushes, DELETE SQL) with a cwd or path argument
  that isn't a tempfile literal.
- Test isolation depends on runtime-scoped patches (pytest autouse, unittest
  setUp) rather than module-level constants or constructor injection.
- The failure mode is silent (recoverable via reflog / undo, but LOOKS LIKE
  data loss to a naive observer).
- The invocation shape that would fire the side effect is a completely
  standard command a human developer or an automation would run without
  thinking.

## When it's overkill

- Pure functions with no I/O.
- Code that only writes to explicit-path arguments (tempfile or otherwise
  passed-in).
- Small deltas to well-covered code where the smoke loop's failure paths
  cover the invocation shape.

## Discipline for the dispatcher

When writing a reviewer brief for code with destructive side effects:

- Name the destructive call site explicitly in the "failure surface" section.
- Include an axis specifically for "does invoking the tests fire the
  destructive side effect against unintended state?" — with permission to
  spend budget on live-repro if reasonable-doubt exists.
- Provide a fixture worktree the reviewer can spend on (a dedicated
  worktree they can nuke without impact — /tmp is fine, `agent/*` branch is
  fine).
- Do not treat "smoke loop green" as sufficient evidence of safety when the
  smoke loop itself might BE the failure vector.

## What NOT to do

Do not conclude "no test fires the side effect" from a grep of test files
alone. Mason's rigorous test-lane grep on T-3782 concluded "no test resets
the worktree" — but the grep was scoped narrowly and missed 2 tests plus
the pytest-xdist timing-race. The traceback from live-repro was
authoritative where the grep was not.

## Related patterns

- **`feedback_isolating_test_before_root_cause_claim`** — the reviewer's
  live-repro IS the isolating test the fix-implementer would need. Pair the
  two: live-repro during review surfaces the bug + gives the fix-implementer
  a working repro to reason from.
- **`feedback_verify_claims_in_own_durable_artifacts`** — a fixture's
  documented claim ("this fixture protects tests X-Y") is not the same as a
  fixture's demonstrated behavior. Verify by running.
