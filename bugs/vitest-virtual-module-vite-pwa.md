---
symptom: "vitest cannot mock virtual:pwa-register/svelte (or other vite-pwa virtual modules); test fails at import time"
fix: "stub file + alias in vitest.config.ts only (NOT in vite.config.ts) — production import path stays canonical"
domain: vitest, vite-pwa, sveltekit
---

## Symptom

When unit-testing a Svelte/SvelteKit component that imports `virtual:pwa-register/svelte` (or any vite-pwa virtual module — `virtual:pwa-register`, `virtual:pwa-info`, etc), vitest blows up at import-resolution time, before `vi.mock` can intercept. Error pattern is something like vite's import-analysis failing to resolve `virtual:*` because the vite-pwa plugin generates these modules at build time and they don't exist in the source tree.

## Cause

Vite's import-analysis transform runs during vitest's module-graph build, and it needs to resolve every import to a real file or a registered virtual-module provider. The vite-pwa plugin does provide the virtual modules — but only in the production build pipeline, not in vitest's test pipeline (which doesn't load the SvelteKitPWA plugin). So vitest sees an unresolvable import.

`vi.mock('virtual:pwa-register/svelte', ...)` doesn't help — `vi.mock` runs after import-analysis, and the failure happens before `vi.mock` gets a chance.

## Fix

Stub file + test-only alias.

1. Create a no-op stub matching the real virtual module's API shape:

   ```ts
   // web/src/lib/__pwa-register-stub.ts
   import { readonly, writable } from 'svelte/store';
   
   export function useRegisterSW() {
     return {
       needRefresh: readonly(writable(false)),
       offlineReady: readonly(writable(false)),
       updateServiceWorker: async () => {},
     };
   }
   ```

2. Alias the virtual-module path → stub in `vitest.config.ts` ONLY:

   ```ts
   // web/vitest.config.ts
   export default defineConfig({
     resolve: {
       alias: {
         'virtual:pwa-register/svelte': new URL('./src/lib/__pwa-register-stub.ts', import.meta.url).pathname,
       },
     },
     // ...
   });
   ```

3. Production `vite.config.ts` does NOT get this alias. Production imports still resolve to the real virtual module via SvelteKitPWA at build time. Component code imports from the canonical path:

   ```ts
   // web/src/routes/+page.svelte
   import { useRegisterSW } from 'virtual:pwa-register/svelte';
   ```

   This path resolves to the real module in prod, the stub in tests. Component code is identical.

## Discovered

- T-0107 (Phase 2 Cycle 3, kino PWA) — first SW registration test wouldn't run; ~10min triage. Implementer surfaced as plan-flag, reviewer verified scope-test-only.
- pantheon-pwa commit 6c7fe0d, 2026-05-06.

## Keywords

vitest, vite-pwa, virtual:pwa-register, virtual module, sveltekit, vi.mock, import-analysis, useRegisterSW, service worker, test alias, __pwa-register-stub, can't resolve virtual, module not found virtual
