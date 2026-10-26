# Mocking the seam hides the seam break: a green suite shipped a wrapper/worker kwarg mismatch

**Date authored:** 2026-10-13
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015).
**Re-evaluate when:** another outage ships through a mocked seam, or the T-4888 parity lint catches a real drift before merge. Either outcome sharpens the lesson.

## Observation

#894 (T-4862) added a keyword argument at one call site: `persona_bringup.main()` called `wake_persistent_runtime(gate_on_usability=True)`. Only the locked worker `_wake_persistent_runtime_locked` accepted that keyword; the public wrapper did not. 16009 tests passed. In production every persona-consume fire raised `TypeError`, and the consume units failed for recovery, artisan and specialist from 2026-10-12 21:55Z until #895 at about 22:05Z.

Every test that drove `main()` through that branch mocked `wake_persistent_runtime` itself. The mock accepted any keyword, so the one place the mismatch lived, the wrapper's signature, was never executed.

## Implications

1. **Fake at the IO edges, not at the seam you're changing.** A test that mocks the function whose signature is in question cannot see a signature break. Drive-through tests should run the real wrapper and worker, and fake only subprocess, systemd, tmux and the filesystem.
2. **Reviewers: when a diff adds a keyword argument, check it against every caller and every wrapper on the path to the function that consumes it.** It isn't enough that the final worker accepts it.
3. **Structural backstop (T-4888):** an AST lint asserts that every public wrapper of a `*_locked` worker accepts and forwards each of the worker's keyword-only parameters. There is also an end-to-end `main()` consume test with edge-only fakes.

## References

- #894 (T-4862), #895 @7601294ff (fix plus a single-wrapper parity test), T-4888 (general lint plus the end-to-end test).
- Related: [`verification-tier-blind-spots-each-covers-what-the-previous-cant-see.md`](verification-tier-blind-spots-each-covers-what-the-previous-cant-see.md).
