---
surface: routine
headline: No Flipper One change: $20 reservation coupon (379 SKU) still the only available variant; Kickstarter still undated.
---

# Flipper One launch watch, 2026-11-03

## Change since the last dispatch

Nothing material changed since 2026-11-02. The official Shopify feed still lists Batch 1 (`FLP-O-COUPON-349`), Batch 2 (`-399`) and Batch 3 (`-449`) as unavailable. The generic `Reservation Coupon` (`FLP-O-COUPON-379`) is the only available variant. Each variant shows a feed price of 2000 (cents, i.e. $20). No remaining quantity is published. [Primary: https://flipper.net/products/flipper-one-preorder.js]

## Campaign, shipping, specs

- No live or dated Kickstarter found. Press still says "closer to the end of the year" at roughly $350 (Hackmag, via web search) [secondary, press, no firm date].
- No early-bird tier, ship estimate, regional restriction or new batch published.
- Blog RSS (last built 2026-11-03) still tops out at the 2026-08-12 "Flipper OS" post. No new announcement. [Primary: https://blog.flipper.net/rss/]
- No spec change against `sseed-presence/notes/2026-10-20-flipper-one-analysis.md`.

## Instruments and limits

Tried: Shopify `.js` feed (parsed), blog RSS, WebFetch of the blog index, web search, WebFetch of `flipper.net/pages/flipper-one` (returned only the generic store page, unusable), and a curl to Kickstarter search (the HTTP status is in the tool output but not recorded here; Kickstarter absence rests on the feed, blog and search results, not a direct Kickstarter read). Coupon inventory is unpublished. The tech-specs page was not re-read today.

## Sources

- https://flipper.net/products/flipper-one-preorder.js: primary, variant availability and prices.
- https://blog.flipper.net/rss/: primary, blog chronology.
- https://blog.flipper.net/: primary, blog index.
- https://flipper.net/pages/flipper-one: primary, fetch returned store page only.
- Web search "Flipper One Kickstarter launch date" (Hackmag, WinFuture, TBreak, The Register): secondary press.
