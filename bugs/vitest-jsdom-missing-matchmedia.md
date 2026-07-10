---
symptom: "any vitest test that transitively renders a Svelte component calling `window.matchMedia(...)` throws `TypeError: window.matchMedia is not a function` — jsdom doesn't implement matchMedia"
fix: "stub `window.matchMedia` in a `web/tests/setup.ts` (or equivalent), wire it via vitest's `setupFiles` config, return a minimal MediaQueryList shape from the stub"
domain: vitest, jsdom, sveltekit
---

## Symptom

A vitest run, with jsdom (or happy-dom) as the test environment, hits a Svelte component whose `onMount` (or top-of-component logic) calls `window.matchMedia(...)`:

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  let isStandalone = false;
  onMount(() => {
    isStandalone = window.navigator.standalone === true ||
                   window.matchMedia('(display-mode: standalone)').matches;
  });
</script>
```

The test throws:

```
TypeError: window.matchMedia is not a function
  at <component>.onMount
  at runOnMount
  at hydrate
  at render
```

The trap: this fires from any test that mounts the component OR any parent that mounts it. So a route-level test (e.g. rendering `+page.svelte`) that includes the component as a child crashes too — even when the test isn't asserting anything about media queries. **Component test that doesn't touch media queries crashes because of the import graph, not because of what the test does.**

## Cause

jsdom does not implement `window.matchMedia` — the spec calls it part of the CSSOM View Module which jsdom hasn't shipped. happy-dom doesn't either by default. Browsers implement matchMedia natively; jsdom-as-test-environment doesn't.

References:
- jsdom issue (long-standing): https://github.com/jsdom/jsdom/issues/3324
- MDN matchMedia spec: https://developer.mozilla.org/en-US/docs/Web/API/Window/matchMedia (the API itself is universally implemented in real browsers)

## Fix

Add a `setupFiles` entry to your vitest config that runs before each test file:

```typescript
// web/tests/setup.ts
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},      // legacy MediaQueryList API
    removeListener: () => {},   // legacy
    addEventListener: () => {}, // modern
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
```

Wire via `vitest.config.ts`:

```typescript
export default defineConfig({
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
  },
});
```

The stub returns a `MediaQueryList`-shaped object with `matches: false` as the safe default — most "is this in standalone mode / dark mode / reduced motion" checks return false, which matches the typical test-environment intent. Override per-test if needed:

```typescript
import { vi, beforeEach } from 'vitest';

beforeEach(() => {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query === '(display-mode: standalone)',  // simulate installed PWA
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
});
```

## Discovered

- T-0115 (kino Phase 3 — Web Push) — `PushSubscribe.svelte` checks `window.matchMedia('(display-mode: standalone)').matches` in `onMount` to gate the "Enable notifications" CTA behind iOS-installed-standalone state. First test that rendered the component crashed; transitively, all tests rendering `+page.svelte` (which includes the component) crashed too.
- pantheon-pwa commit e073170 — fix landed in `web/tests/setup.ts` + `web/vitest.config.ts` (setupFiles entry).

## Pairs with

- `bugs/vitest-virtual-module-vite-pwa.md` — same class (test-environment doesn't ship a runtime API the component depends on); fix shape is also a setup-file stub.
- `bugs/vitest-fake-timers-break-settimeout-flushpromises.md` — different cause but same "jsdom test environment surprise" flavor.

## Keywords

vitest, jsdom, happy-dom, matchMedia, window.matchMedia, MediaQueryList, setupFiles, sveltekit, svelte component test, onMount crash, display-mode standalone, prefers-color-scheme, prefers-reduced-motion, iOS PWA install detection, TypeError is not a function, test environment polyfill
