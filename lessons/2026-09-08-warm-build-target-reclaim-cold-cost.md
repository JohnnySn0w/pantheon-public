# Reclaiming a warm build target has a cold-build cost the next dispatch inherits

Filed 2026-09-08 (orchestrator) after T-1671 (artisan) — a 3h+ wall-clock on a plan-first
implementer build that read as a stall to the watcher.

## The pattern

Freeing disk headroom by reclaiming a warm build cache (Rust `target/`, npm
`node_modules/`, cargo/pip caches, Docker layers, etc.) is often the right call
when the box is tight — artisan's echo `target/` was 18G on a shared box at 94%,
freeing it dropped free from 11G → ~22G. But the next build that reads from that
cache is now COLD, and for a large workspace the cold-build wall-clock can be
hours where a warm build was minutes.

If a watcher (peer orch, activity monitor, human) is measuring elapsed dispatch
time against a warm expectation, the cold build reads as a stall. It isn't — the
implementer is doing exactly what it was asked to do, just against a cache the
reclaimer nuked.

## What to do

**The reclaimer names the downside at reclaim time.** If you reclaim a warm build
target within reasonable horizon of a dispatch that will read from it, flag it in
whatever channel the dispatch lives in (ticket transition, mailbox to the peer,
operator-facing update if it's on the arc's critical path): *"expect a cold build
on the next X-workspace dispatch — I reclaimed target/ for citizenship at Y% and
the warm cache is gone."*

That single line converts an unpredictable wall-clock into a predictable one and
short-circuits the "is this a stall or a build" question that otherwise costs a
peer-courtesy status check + inflight distraction.

## When it's still the right call to reclaim

Almost always, when the box is tight. Disk citizenship on a shared VPS
(especially near 90-94%) is worth the cold-build wall-clock — the alternative is
the whole host degrading (systemd unit failures, git operations refusing,
container starts hanging), which hits every workload. Flag-and-eat is the right
posture; don't-reclaim-because-someone-might-dispatch is not.

## Related

- Watcher-side smell test: a build over 2× the warm expectation is still legit
  wall-clock IF a recent reclaim explains it. Check for a reclaim signal before
  reading silence as stall.
- Not the same as [[feedback_new_delay_is_never_local]] (which is about
  attributing a specific delay to a specific mechanism); this is about the
  substrate-level cache invalidation cost being invisible to non-participants.
- Artisan's disciplinary commitment: flag-at-reclaim-time when reclaim is within
  dispatch horizon.
