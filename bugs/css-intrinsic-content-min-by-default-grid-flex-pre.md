---
symptom: "Grid columns / flex items / `<pre>` blocks expand their containers past viewport on narrow screens. Visible as: agent-pane right border off-screen; chat bubble's pre block wider than bubble's max-width; Send button cut off by container edge."
fix: "Explicit minimum-zero on each layout context: grid → `grid-template-columns: minmax(0, 1fr)` (not bare `1fr` which is `minmax(auto, 1fr)`); flex item → `min-width: 0` on the item; `<pre>` → `max-width: 100%; min-width: 0; box-sizing: border-box`. WebKit needs the explicit constraints; Chromium clamps via different min-content propagation."
domain: css, pantheon-pwa, mobile, ios, webkit, layout, grid, flex, pre, overflow
---

## Symptom

CSS containers with intrinsic-content-sized minimums (bare `1fr` in grid; default `min-width: auto` on flex items; `<pre>` blocks with long content) expand their parent past viewport width on narrow screens, especially on WebKit/Safari.

Visible failure modes encountered in pantheon-pwa T-0310 cascade (2026-05-13, the operator's iPhone):

1. `.pane-grid` had `grid-template-columns: 1fr` — a `<pre>` block inside an agent-pane bubble with a long file-listing pushed the grid column wider than viewport; main's `overflow-x: hidden` clipped the right side, hiding the agent-pane's right border.
2. `.input-bar__input` had `flex: 1` (no `min-width: 0`) — input refused to shrink below intrinsic min (placeholder text + padding), pushing the sibling Send button off the right edge of the container.
3. `.message__content--markdown :global(pre)` had no width constraint — pre's intrinsic content width pushed the parent `.message` bubble past its `max-width: 75%`.

Chromium handles all three via different min-content propagation, so the symptom doesn't reproduce in `playwright mobile-chrome` (Pixel 5 / Blink engine). WebKit / iOS Safari needs the explicit constraints.

## Cause

CSS layout defaults use intrinsic content as the minimum size:

- **Grid columns**: bare `1fr` is shorthand for `minmax(auto, 1fr)`. The `auto` minimum means the column grows to fit its largest child's min-content.
- **Flex items**: default `min-width: auto` (NOT `0`). Flex items refuse to shrink below their intrinsic content size.
- **`<pre>` blocks**: with `white-space: pre` / `word-break: normal`, content sets the element's natural width; parent's `max-width` doesn't override.

On wide viewports the intrinsic min is rarely binding (container has plenty of space). On narrow viewports (mobile, especially iPhone-class 390px) the intrinsic min exceeds available space and the layout overflows.

WebKit vs Chromium divergence: both engines compute intrinsic min identically, but their PROPAGATION of `min-width: 0` cascading from ancestors differs. Chromium tends to clamp the child to the parent's contracted bounds; WebKit respects the child's intrinsic min more aggressively, letting it push the parent wider.

## Fix

Add explicit `0` minimums at every layout boundary:

```css
/* Grid: replace `1fr` with `minmax(0, 1fr)` */
.pane-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr); /* if vertical fill matters too */
}

/* Flex item: explicit min-width: 0 */
.input-bar__input {
  flex: 1;
  min-width: 0;  /* lets the input shrink below placeholder intrinsic width */
}

/* <pre> inside a constrained bubble */
.message__content--markdown :global(pre) {
  max-width: 100%;
  min-width: 0;
  box-sizing: border-box;
  overflow-x: auto;  /* scroll long lines instead of expanding parent */
}
```

The pattern: every time a child can have intrinsic content larger than parent's available space, add `min-width: 0` (and possibly `max-width: 100%`) on the child. For grid tracks specifically, use `minmax(0, 1fr)` instead of bare `1fr`.

## Discovered

2026-05-13 across T-0310 cascade (the operator's iPhone iterating through):
- msg_1437 — agent-pane's right border off-screen (pane-grid columns).
- msg_1442 — Send button cut off (input-bar input flex).
- (Implicit in screenshot from msg_1432) — pre block content wider than bubble (Message.svelte pre).

Three different layout contexts, same root pattern. Caught only after WebKit/iPhone testing because Chromium-only Playwright (`mobile-chrome` project) didn't reproduce.

Commits: pantheon-pwa@598f0e6 (pre), d54a0d2 (grid columns), 6e25381 (grid rows), 81d0ef2 (flex input).

## Pairs with

- [[reference_tailscale_service_subdomain_pattern]] — adjacent CSS-on-narrow-viewport class.
- `lessons/test-substrate-engine-coverage-gap.md` (forthcoming) — meta-insight on why Chromium-only Playwright missed these.
- `sop/standard/testing-depth-and-scope.md` — UI ticket testing tier; relates to the WebKit engine coverage gap.

## Keywords

css, layout, grid, grid-template-columns, grid-template-rows, minmax, 1fr, flex, flex-item, min-width, intrinsic, content-min, overflow, pre, max-width, webkit, safari, ios, iphone, pantheon-pwa, mobile, narrow-viewport, agent-pane, message-bubble, input-bar, send-button
