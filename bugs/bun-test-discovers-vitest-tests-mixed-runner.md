---
symptom: "`bun test` from repo root fails with `vi is not defined` errors when web/ subproject uses vitest; smoke loop reports unrelated failures hiding real regressions"
fix: "add `bunfig.toml` at repo root with `[test]\\nroot = \"tests\"` to scope bun test discovery; web/ vitest runs separately via its own `bun run test` script"
domain: bun, vitest, sveltekit, monorepo
---

## Symptom

A repo with a Bun-backend top-level (`tests/*.test.ts` using `bun:test`) AND a SvelteKit/Vite frontend subproject (`web/tests/*.test.ts` using vitest) — running `bun test` from the repo root produces output like:

```
108 pass
 24 fail
  1 error
```

The 24 failures all come from `web/tests/*.test.ts` files. Errors look like:

```
ReferenceError: Can't find variable: vi
  at file:///.../web/tests/api.test.ts:5:0
```

or:

```
TypeError: vi.stubGlobal is not a function
```

— bun:test discovers the vitest-style tests + tries to run them as bun:test, but vitest's globals (`vi.fn`, `vi.spyOn`, `vi.stubGlobal`, `vi.unstubAllGlobals`, `vi.useFakeTimers`, etc) don't exist in bun:test's runtime.

The 24 failures are **pre-existing** — every commit that touched the repo will report them. The smoke loop's regression-detection signal is structurally broken: a real new failure introduced by a code change is invisible in the noise, and the surface message can mistakenly claim "0 fail" if the operator runs a filtered variant (`bun test | grep -v web` or similar) instead of the literal command.

## Cause

`bun test` walks the file tree from CWD looking for `*.test.{ts,js,tsx,jsx}` files (and a few other patterns). It doesn't know the difference between bun:test files and vitest files — they look identical at the filesystem level. With no scope-constraint, both get discovered, and the vitest ones get loaded into bun:test's runtime where vitest's API surface doesn't exist.

vitest's own `bun run test` (which delegates to `bunx vitest run` or similar via `package.json` script) runs vitest's own runner against `web/tests/` — that path works correctly, just not when bun:test is the top-level invoker.

## Fix

Add `bunfig.toml` at repo root scoping bun:test discovery:

```toml
[test]
root = "tests"
```

That's it. `bun test` from the repo root now discovers ONLY `tests/*.test.ts` (the bun:test-shaped files) and exits clean. `web/tests/` is unaffected — vitest still runs from `cd web && bun run test` (or `cd web && bunx vitest run`).

Verification:

```sh
cd /repo
bun test 2>&1 | tail -5
# expected: <N> pass, 0 fail, no errors
cd web
bun run test 2>&1 | tail -5
# expected: vitest passes, separate runner, separate scope
```

Operator discipline: when smoke-loop output is reported up the chain, paste the **literal tail of the exact brief-specified command** — never a filtered variant. Pre-existing failures hide in deltas.

## Discovered

- T-0111 r0 (kino Phase 2 Cycle 5) — pre-existing 24 failures had been in place since Cycle 3 (T-0107 introduced `web/tests/`); reviewer caught at deploy-cycle pre-flight when the brief's "0 fail" criterion was checked literally for the first time. Implementer + orchestrator surface messages had reported "108 pass, 0 fail" via filtered invocations rather than the literal command. Reviewer running the brief's exact command exposed the gap.
- pantheon-pwa commit 7076664, 2026-05-07 (the 2-line bunfig.toml fix).
- T-0096 retrospective folds: smoke-loop literal-output discipline → orchestrator-tier verification step ("run the brief's commands yourself before relaying upward") to be added to team-orchestrator brief template going forward.

## Keywords

bun test, bunfig.toml, vitest, mixed runner, monorepo, vi.stubGlobal, vi.fn, vi.spyOn, vi.useFakeTimers, can't find variable vi, ReferenceError vi, TypeError vi.stubGlobal, sveltekit, web subproject, test discovery scope, root = "tests", smoke loop signal integrity, regression detection
