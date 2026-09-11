---
symptom: "a cross-process 'race' test passes stably but never actually races: each subprocess arm is driven with a sequential subprocess.communicate() call, so arm B only starts after arm A has fully finished — the test exercises cross-process-ness but the interleaving it claims to prove never occurs (a serialization regression would still pass it)"
fix: "start ALL contending subprocesses first with stdin gates held open, release every gate, THEN collect results (communicate/wait per process after the release step); additionally pin each deterministic ordering with its own separate test, and run the true-concurrency test several consecutive times for stability before trusting it"
domain: "testing / concurrency"
---

# Sequential `communicate()` in a race test silently serializes the arms

**Shape.** You want to prove two contenders (e.g. bind vs revoke on one SQLite row) serialize to exactly one winner. The natural-looking test spawns subprocess A, calls `a.communicate()`, spawns B, calls `b.communicate()`. It passes — but `communicate()` blocks until the process EXITS, so B never overlaps A. The test proves sequential behavior it was never doubting, and the actual claim (concurrent contention resolves to one winner) goes unverified. It cannot fail for the reason it exists.

**Caught live** 2026-08-12 (T-2910 AB-2 build, implementer self-review): the revoke-vs-bind serialization test had exactly this defect. Fix applied: both subprocesses started with stdin-gate protocol (each blocks reading stdin at its contention point), both gates released, then both collected — one arm blocks on SQLite `busy_timeout` while the other holds `BEGIN IMMEDIATE`, and the loser's classification is asserted. Stable across 3 consecutive runs; each deterministic ordering additionally pinned by its own sequential test (those are legitimate AS sequential tests — the trap is only claiming concurrency from a sequential driver).

**Review heuristic.** In any test whose name claims "concurrent"/"race"/"serialize": find the point where BOTH contenders are simultaneously started-and-unblocked. If every `communicate()`/`wait()` on contender N precedes the spawn or release of contender N+1, REBOUND it — the arms are serialized and the test is vacuous for its stated purpose.
