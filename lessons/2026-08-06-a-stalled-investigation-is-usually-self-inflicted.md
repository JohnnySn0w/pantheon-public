# A stalled investigation is usually self-inflicted: theory-first, proxy-verified, or self-confounded

**Date:** 2026-08-06 · **Author:** Orchestrator (HO) · **Status:** Authored (`lessons/` is a
non-ratifiable class per ORG-015)
**Origin:** The T-2590 Recovery cutover — six fires, an induced comms incident, and a multi-hour
drag, against a root cause that turned out to be one line in a log. The operator's two questions
("is the race for-sure or a theory?" and "comms shouldn't be fragile — we're missing something")
are what broke each stall.
**Re-evaluate when:** an investigation drags ≥3 attempts and none of these three modes was the
cause — then the checklist is missing a fourth.

## The rule

> **When an investigation drags across many attempts, the stall is usually one of three
> self-inflicted things, not the system being subtle:**
> 1. you are **fixing a theory, not a mechanism**;
> 2. you are **verifying a proxy, not the capability**;
> 3. **your own actions are the confounder.**
> Check these before concluding the problem is hard.

Each cost the cutover a fire or more. All three were visible early and cheap to check.

## The three modes, from the cutover

**1. Fixing a theory, not a mechanism.** We carried an inherited model — an "MCP-init race
against a ~5s startup probe" — and shipped fixes *against the theory* (a longer `MCP_TIMEOUT`, a
direct-spawn to shrink time-to-connect, even a "3/3 determinism proof"). Every one failed,
because the actual mechanism was `bun`-not-in-PATH under `systemd-run --scope` — a **deterministic
ENOENT in 13 ms**, no race, no 5-second probe anywhere. The tell was in hand the whole time: the
dark boots produced **no MCP log at all**, which means *never spawned*, not *spawned-and-raced-out*
— a different mechanism with a different fix. The fix arrived only when Recovery stopped fixing the
theory and **read the actual server log**. → *A theory you can't point at a log line for is a
guess. Grounding a guess in a fresh "proof" (our 3/3) makes it more dangerous, not less.* (Reinforces
`feedback_isolating_test_before_root_cause_claim` and `lessons/2026-07-28-name-the-other-hypothesis`.)

**2. Verifying a proxy, not the capability.** Two separate proxy-checks lied:
- The "3/3 determinism proof" checked for a **sustained `bun` child process**, never the reply
  *tool*. A process is a proxy for a capability.
- The cutover's own comms-check walked the process tree for the poller's *argv shape* — and
  **false-negatived a genuinely-green cutover twice** (first an argv mismatch, then a tree-walk
  miss under `--scope`), reporting "comms-dark" while the tool was live. I nearly rolled back a
  *working* cutover on it, twice.
The authoritative signals were the **capability** itself: the MCP log's `Connection established …
hasTools:true`, an `ss` **ESTABLISHED** long-poll to Telegram's DC, and an end-to-end test message
that actually round-tripped. → *Verify the defining surface (the capability), not a proxy for it
(a process, an argv, a PID file). A proxy check can fail both ways — false-negative a working
system and false-positive a broken one.* (Reinforces
`lessons/2026-07-23-proxy-vs-defining-surface-synthesis` and the T-2528 evidence-map SOP amendment.)

**3. Your own actions are the confounder.** This is the one worth pinning, because it hid in
plain sight:
- The "load-sensitive bespoke dark" that drove a whole incident — Recovery going dark under load,
  self-healing when the box calmed — was **my own runaway process**: a 3.75-hour busy-loop
  load-test I'd left orphaned (its `trap … EXIT` cleanup bypassed by SIGKILL), pinning three of
  four cores. The "mysterious load-sensitive race" was me, generating the load.
- The `getUpdates` probe I ran to *check* whether inbound was live **stole the poll slot**, made
  my own probe read "inbound down," and briefly knocked the healthy poller offline. The
  measurement changed the thing measured.
→ *Before concluding the system is flaky, rule yourself out: what have I spawned, probed, or left
running that could produce exactly this symptom? An investigator's instruments and leftovers are
part of the system under test.*

## The meta-tell: an operator's one-line question broke each stall

The operator didn't have the logs. He asked **"is the race for-sure or a theory?"** — which is mode 1
stated as a question — and **"comms shouldn't be fragile like that; it would indicate we're
missing something"** — which is the refusal to accept a proxy-level "it's just flaky" as an answer.
Both were right, and both were *cheaper than the fire they'd have prevented*. → *When you're deep
in an investigation, the person who isn't can often see the stall you can't, precisely because
they aren't invested in the current theory. Surface the theory-vs-fact distinction to them
explicitly (I eventually did: "it's a theory, not confirmed") — it invites exactly this catch.*

## The self-check

Cheap, and it fires exactly when the drag starts:

> After the **second** failed attempt: "Can I point at the log line for the mechanism? Am I
> checking the capability or a proxy for it? What have *I* got running or probing that could be
> causing this?"

If any answer is uncomfortable, that's the next move — not another attempt against the same
theory.

## Related
- T-2590 / T-2624 (the arc), T-2628 (cgroup-scope background commands, so a runaway can't
  confound again), T-2630 (the proxy-check that false-negatived).
- `lessons/2026-07-28-name-the-other-hypothesis-before-asserting-a-state.md` — mode 1's prior form.
- `lessons/2026-07-23-proxy-vs-defining-surface-synthesis.md` — mode 2's prior form.
- `sop/standard/plan-first-dispatch.md` §Evidence map (T-2528) — the up-front discipline that
  forces a claim→source→falsification map, which is mode 1 caught before execution.
- `feedback_isolating_test_before_root_cause_claim` — the falsifying-test memory this generalises.
