---
symptom: "In pantheon-pwa (kino), a vitest/component test that needs to observe a child component's dispatched custom events (createEventDispatcher events) cannot use `component.$on('event', handler)` — Svelte 5 removed the runtime `component.$on` instance method. The test either throws `component.$on is not a function` or silently never fires the handler."
fix: "Wrap the component-under-test in a tiny `*Harness.svelte` that binds the handler at COMPILE time with `on:event={...}` and exposes the captured payload (a bound prop, a spy, or a store), then mount the harness in the test instead of the raw component. Compile-time `on:` directives still work in Svelte 5; only the runtime `$on` instance method was removed."
domain: "svelte, svelte5, vitest, component-testing, createEventDispatcher, pantheon-pwa, kino, ui"
---

# Svelte 5 removed runtime `component.$on` — event-dispatching components need a compile-time test harness

**Class:** framework API removal, test-only. Not a bug in the component — the runtime event-subscription API the test reaches for no longer exists.

## Cause

Svelte 5 removed the runtime `component.$on(name, handler)` instance method (part of the components-are-not-classes change). A component that emits via `createEventDispatcher()` still dispatches fine at runtime through a **compile-time** `on:event` directive on its parent; what's gone is the imperative `instance.$on(...)` a test would call after mounting the bare component.

## Fix (surgical, established precedent)

Add a `web/tests/<Component>Harness.svelte` that mounts the component with the handler bound the compile-time way and surfaces the payload for assertion:

```svelte
<!-- captures the dispatched event's detail at compile time -->
<script>
  export let onCaptured = () => {};
  import Component from '../src/components/Component.svelte';
</script>
<Component on:theEvent={(e) => onCaptured(e.detail)} {...$$restProps} />
```

Mount the harness (not the raw component) in the test; pass a spy as `onCaptured`.

## Prevention / recurrence note

This is the **second** instance in pantheon-pwa: `TicketActionsHarness.svelte` (T-0245) was the first; `TicketGraphHarness.svelte` (T-2676, 2026-08-12) rediscovered the same wall independently. Any third event-dispatching component that needs testing will hit it again — grep `web/tests/*Harness.svelte` for the pattern before hand-rolling a `$on` test.

## Verification

`grep -l 'on:' web/tests/TicketActionsHarness.svelte web/tests/TicketGraphHarness.svelte` — both use the compile-time directive. Re-check on any Svelte major bump: if a future Svelte reintroduces a runtime subscription API, the harness indirection may become optional (banner this entry then, don't silently leave it).
