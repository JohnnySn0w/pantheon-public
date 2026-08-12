---
symptom: "A test fixture spawned as bash -c 'sleep 600' <marker-args> is invisible to pgrep -f: the marker args vanish from /proc/<pid>/cmdline, so a regression test asserting pgrep finds (or does not find) the marked process goes vacuous — the fixture 'runs' but never carries its marker."
fix: "Make the -c script a COMPOUND command: bash -c 'sleep 600; :' <marker-args>. A single simple command triggers bash's tail-call exec optimization — bash replaces its own process image (and argv) with the command's; a compound script forces bash to fork the child and stay resident with its original argv, keeping the marker visible in cmdline for the fixture's lifetime. Pair with start_new_session=True + os.killpg in teardown so killing the bash parent doesn't orphan the forked sleep."
domain: pantheon, bash, exec, pgrep, test-fixture, cmdline, proc
---

# bash -c tail-call exec silently replaces the argv of fake-process fixtures

## Mechanism

The pattern for a "fake process with custom argv" fixture is to exploit `bash -c`'s trailing
args: `bash -c '<script>' <arg0> <arg1> …` assigns the trailing args to `$0`/`$1`, so they sit in
the process's cmdline while the script ignores them. But when `<script>` is a **single simple
command** (`sleep 600`), bash applies its tail-call optimization: instead of forking, it `exec`s
the command directly, replacing its own process image — and `/proc/<pid>/cmdline` becomes
`sleep 600`, the marker args gone. Any `pgrep -f <marker>` assertion then tests nothing: a
positive test can never match; a negative test passes vacuously.

## Why it hides

The fixture process IS alive (as `sleep`), so liveness checks pass; only the argv is different
from what the test author believes. If the test's failure mode is "no match expected", the bug is
invisible — the suite is green for the wrong reason. It only surfaces if someone asserts the
POSITIVE case (marker must be found) or probes `/proc/<pid>/cmdline` directly.

## The fix shape

`bash -c 'sleep 600; :' claude --resume-fixture-<unique>` — the `; :` makes the script a compound
command, so bash forks `sleep` and stays alive as the argv-carrying parent. Teardown must kill the
process GROUP (`start_new_session=True` at spawn, `os.killpg(pgid, SIGKILL)` at teardown) —
terminating only the bash parent orphans the forked `sleep` child for its full duration.

## Provenance

Found by the T-1956 implementer (2026-07-22) building the live cross-account regression fixture
for the ho_resumer uid-scoped pgrep predicate; verified empirically with a standalone probe before
the test was written. The first-layer trap (GNU sleep rejects `--resume` as an unknown option, so
naive `sleep 600 --marker` dies instantly) was caught at plan review; the exec-optimization layer
beneath it only showed up against the real surface — mocks would never have found either.

## Cross-refs

- `lessons/2026-07-21-cmdline-substring-process-predicates-unsound.md` — the sibling class:
  cmdline is a lie-prone surface on BOTH sides (predicates that scan it, fixtures that fake it).
- `tools/test_ho_resumer.py` — the living example of the correct fixture shape.
