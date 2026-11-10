---
surface: routine
headline: Glop blog has no new posts in the past week; still the same two posts from 2026-09-18.
---

## Finding

No new public Glop blog posts between 2026-10-26 and 2026-11-02 UTC. Post set and `updated` timestamps are identical to the prior baseline.

## Baseline Snapshot (diff anchor for next cycle)

`https://glop.it/api/blog` returns two posts, `hasMore: false`, both `version: 2`. `?page=2` returns an empty array.

| Published (UTC) | Title | Gist | Link |
| --- | --- | --- | --- |
| 2026-09-18 18:02:38 | New features | Glops can now browse the web, see location, and use the microphone, with permission prompts. | https://glop.it/blog/4a4984f1-0c37-4463-a7ff-7edc99a3fd15 |
| 2026-09-18 18:01:51 | hello world | Minimal launch post; body is just "hello". | https://glop.it/blog/301dfe34-cdbb-42c0-97ce-c4506347b01f |

## Instruments

curl of `/blog` (SPA shell only), `/blog.js` (to confirm the API path), then `/api/blog` and `?page=2` (public JSON the SPA calls). Control: the listing returned the known two posts, so the empty page 2 is real absence.

## Sources

- https://glop.it/blog - target page, JS shell only.
- https://glop.it/api/blog - public listing JSON (primary).
- /home/claude-research/pantheon-config/knowledge/watch/reports/2026-10-26/glop-it-blog.md - prior baseline.
