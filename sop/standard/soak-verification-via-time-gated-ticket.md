# Soak verification via a time-gated ticket

**Status:** Drafted 2026-06-03 by Recovery per the operator Telegram msg 2320 ("soaks should just be time gated tickets worth an sop"). **Awaiting ratification.**

## When to use

After a **supervised first-fire passes** but you want **soak confidence** before declaring a capability trustworthy unattended — i.e. you've proven the mechanism works once with an operator watching, and now you want to confirm it survives **real, unattended operation** over time (e.g. a restart-stack capability surviving an unattended context-fill/api-health/natural restart; a new daemon running clean for N hours; a migration holding under live traffic).

The anti-pattern this replaces: **babysitting** — keeping a ticket open and manually polling/watching for the soak window, or resolving prematurely on the supervised fire alone.

## The rule

**A soak is a time-gated ticket, not a watch loop.** Don't sit on it; schedule it.

1. Do the supervised first-fire and record it as done in the parent/work ticket.
2. File (or convert) a **separate soak ticket** with a `not_before:` set to the end of the soak window (e.g. `not_before: 2026-06-05T19:00:00Z` for ~48h). Label it `soak, time-gated`.
3. The inbox-consumer's not_before filter (T-0312) keeps it parked until the window elapses, then surfaces it automatically — no polling, no babysitting.
4. **On fire**, run the soak acceptance check:
   - Confirm the capability actually exercised unattended during the window (e.g. an unattended restart landed clean — check the drain/monitor journal). If nothing exercised it naturally, **synthesize one** fire now and verify it lands clean.
   - Confirm no regression signature (e.g. no exit-7 / restart-loop in the journal, no wedge alerts during the window).
   - If clean → resolve the soak ticket (and close the parent epic if it gates on it). If not → disarm/fix per the capability's own remediation protocol, re-soak.

## Picking the window

- Match the window to how often the thing-under-soak naturally exercises. A restart capability that fires on context-fill (~daily under normal load) wants ~24–48h so at least one unattended cycle is likely. A daemon-stability soak might want longer.
- State the window in the ticket; it's a default, not a law — adjust per the capability.

## Why a ticket (not a /loop or a wakeup)

The work survives session restarts, is visible in the queue, carries its own acceptance criteria + transition log, and fires deterministically via the existing not_before machinery — no new polling infra, no dependence on a single session staying alive through the window.

## Worked example

T-0756 (T-0517 parity epic, P3): Specialist's supervisor + drain passed supervised first-fires 2026-06-03. Rather than babysit, converted P3 to `not_before: 2026-06-05T19:00:00Z` to observe an unattended restart land clean before closing the epic.

## Related

- `adding-restart-capability-to-a-new-agent.md` — the supervised-first-fire + disarm-on-unclean-fire protocol this soak follows on from.
- not_before / inbox-consumer default-fire machinery (T-0312); the defer-with-ping-date pattern.
