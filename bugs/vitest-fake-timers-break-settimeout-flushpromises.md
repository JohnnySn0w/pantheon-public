---
symptom: "vitest test hangs forever (no error, no output) after vi.useFakeTimers() is called and a flushPromises helper runs"
fix: "use Promise.resolve() chaining for flushPromises, NOT setTimeout-based — fake timers intercept setTimeout"
domain: vitest, fake-timers
---

## Symptom

A vitest test that uses `vi.useFakeTimers()` and any `flushPromises` helper based on `setTimeout` hangs indefinitely. No error is thrown. No assertion runs. The test eventually times out (or the process gets killed), but the failure mode is silent — `vitest` reports nothing useful while the hang is in progress.

Common helper that breaks:

```ts
const flushPromises = () => new Promise(resolve => setTimeout(resolve, 0));
```

## Cause

`vi.useFakeTimers()` intercepts `setTimeout` (along with `setInterval`, `setImmediate`, etc) and queues callbacks against vitest's mock clock instead of the real event loop. The `setTimeout(resolve, 0)` inside `flushPromises` never fires because the mock clock isn't being advanced — the Promise stays pending forever, the `await flushPromises()` never resolves, the test execution stops at that line.

Real timers don't have this problem (the 0ms timeout fires on the next macrotask). The interaction is silent because `vi.useFakeTimers()` doesn't warn when intercepting setTimeout; the timeout just sits in the mock-timer queue.

## Fix

Use microtask-only flush — `Promise.resolve()` chaining, which is fake-timer-safe:

```ts
const flushPromises = () => Promise.resolve().then(() => Promise.resolve());
```

Two `.then()` calls handles two-deep microtask queues (most cases). For deeper async chains, add more `.then()` calls or use:

```ts
const flushPromises = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};
```

If you genuinely need to advance fake timers within a flushPromises-style helper, use `vi.advanceTimersByTime(n)` or `vi.runAllTimersAsync()` explicitly — but that's a different intent than "let pending microtasks settle."

## Discovered

- T-0106 (Phase 2 Cycle 2, kino PWA) — implementer hit the hang on the WS reconnect-backoff test, ~5min to identify. Surfaced in implementer reflection.
- pantheon-pwa commit 0f2a715, 2026-05-06.

## Keywords

vitest, fake timers, useFakeTimers, flushPromises, setTimeout, microtask, Promise.resolve, async flush, test hang, infinite hang, advanceTimersByTime, runAllMicrotasksAsync, vi.useFakeTimers, mock clock
