# Test-substrate engine-coverage gap — Chromium-only Playwright shipped WebKit bugs

**Date authored:** 2026-05-13
**Author:** Orchestrator (Orchestrator)
**Status:** Authored 2026-05-13 (lessons/ is an Authored, non-ratifiable class per ORG-015; the older "awaiting ratification" header predated it).
**Re-evaluate when:** A second-instance bug ships through a substrate-divergence gap (any axis: engine, OS, viewport, network conditions, device-pixel-ratio, OR future axes); OR when the visual-verification SOP gets amended to require substrate-matching as a REQUIRED tier per the testing-depth-and-scope matrix. Either signal sharpens the lesson's preconditions.

## Observation

When the test substrate diverges from the production substrate, bugs that exercise the divergence axis ship to production. The cost compounds: routine candidate-evaluation becomes a real-device round-trip loop, slowing iteration by minutes per cycle and burning operator attention.

T-0288 (2026-05-12) established `playwright mobile-chrome` as the visual-verification surface for mobile UI tickets, with the explicit DECISIONS comment:

> Chromium-based to avoid the webkit install dependency in CI; viewport dimensions are within 1% of iPhone 13 (390x844) so layout-bug surface is equivalent for the gutter-class issues we care about here.

That second clause — "layout-bug surface is equivalent" — was the load-bearing assumption. It was wrong. T-0310 (2026-05-13) cascade surfaced four distinct WebKit-specific bugs through the operator's iPhone, none of which reproduced in mobile-chrome:

1. `<pre>` overflow past parent `.message` max-width (msg_1432).
2. `.pane-grid` columns growing past viewport (msg_1437).
3. InputBar Send button cut off (msg_1442 — exposed AFTER fix #2).
4. Initial-paint viewport-fit=cover quirk + `100dvh` recalc-on-scroll (msg_1456 → msg_1464).

All four are the **same root pattern** (CSS intrinsic-content-min-by-default — captured in [`bugs/css-intrinsic-content-min-by-default-grid-flex-pre.md`](../bugs/css-intrinsic-content-min-by-default-grid-flex-pre.md)) hitting WebKit's different propagation behavior. Chromium's grid/flex/pre engines clamp the child more aggressively when ancestors have `min-width: 0` chains; WebKit respects the child's intrinsic min and pushes the parent wider.

The pattern is generalizable: **substrate divergence ships substrate-specific bugs**. The engine is one axis; viewport class, OS, network conditions, device pixel ratio, scrollbar behavior, font rendering, and color profile are all potential axes. Whenever the test substrate doesn't match production on a given axis, bugs along that axis ship.

## Implications

1. **Test-substrate axes must match production-substrate axes on every dimension production actually exercises.** "Equivalent enough" / "close enough on viewport" is a sufficient-condition claim that needs evidence, not assertion. T-0288's "within 1% of iPhone 13" was the assertion; the four T-0310 bugs were the counter-evidence.

2. **The cost of substrate-matching is bounded; the cost of substrate-divergence is unbounded.** mobile-webkit Playwright project added 2026-05-13 (pantheon-pwa@cf3047b): one project entry in playwright.config + `playwright install webkit` + system-deps install. ~10min one-time setup. The 9-iteration operator-iPhone-bounce loop that T-0310 actually took: ~3h of the operator's attention. The substrate-matching cost would have caught all four bugs in the first iteration.

3. **"Engine coverage" is a discoverable axis only after the first miss.** T-0288 reviewer + author both signed off on the Chromium-equivalent claim. Neither caught it as a gap. The gap surfaced only when a WebKit-specific bug shipped. This is a recurring pattern in test-substrate design: **the gaps you don't know about are the ones that ship bugs**. Mitigation: prefer substrate-matching as the *default* — only deviate with explicit substrate-axis-by-axis justification.

4. **Real-device verification SHOULD remain in the loop as final acceptance, but NOT as routine candidate-evaluation.** Playwright with substrate-matching catches the deterministic + known bugs; real-device acceptance catches the new + unknown bugs. The cost gradient: deterministic test loop is cheap-and-frequent; real-device acceptance is expensive-and-occasional. Routine iteration via real-device loops is the anti-pattern.

## Design implications

- **Visual-verification SOP amendment**: `sop/standard/visual-verification-for-ui-tickets.md` should explicitly require Playwright project for each production engine — not just one viewport, but each substrate-axis production actually targets. For pantheon-pwa: chromium (desktop) + mobile-webkit (iPhone/Safari). For pages targeting Android Chrome too, add a chromium-mobile project.
- **Testing-depth-and-scope SOP amendment**: the matrix's UI row's "Visual / Manual smoke" cell (REQUIRED for UI tickets) should specify substrate-coverage: every production-targeted engine must be in the test substrate, not just viewport-equivalent stand-ins.
- **Substrate-discovery routine**: when adopting a new production substrate axis (new browser, new device class), audit the existing test substrate for coverage of that axis. New axes uncovered = file a test-substrate gap ticket before the first real-device-only-bug ships.

## Re-evaluate when

A second-instance bug ships through a substrate-divergence gap (any axis), OR when the visual-verification SOP is amended to require substrate-matching as a REQUIRED tier per testing-depth-and-scope. Either signal sharpens this lesson's preconditions.

## Related

- [`bugs/css-intrinsic-content-min-by-default-grid-flex-pre.md`](../bugs/css-intrinsic-content-min-by-default-grid-flex-pre.md) — the technical pattern this lesson abstracts from.
- [`sop/standard/visual-verification-for-ui-tickets.md`](../sop/standard/visual-verification-for-ui-tickets.md) — needs substrate-coverage amendment per Design implications.
- [`sop/standard/testing-depth-and-scope.md`](../sop/standard/testing-depth-and-scope.md) — UI row's REQUIRED cell needs substrate-coverage qualifier.
- T-0288 (mobile-chrome project added; chromium-equivalent claim) → T-0310 (four WebKit-specific bugs shipped, mobile-webkit project added).
- Telegram refs: msg_1432 / msg_1437 / msg_1442 / msg_1454 / msg_1456 / msg_1461 / msg_1464 / msg_1470 (T-0310 iteration thread).
- Principles: P1 (architecture over policy — substrate-matching as structural rather than discipline-tier), P4 (traceability — substrate-divergence-bug audit trail), D6 (plan-first — substrate-axis specification at brief-construction tier).
