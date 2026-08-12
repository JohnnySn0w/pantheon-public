---
symptom: "You consume the mailbox in-order and act on message N. Message N+1 was a correction of N — the sender caught the error and followed up. You have already acted on the wrong claim. The mailbox drain is strictly FIFO by write-time: `mailbox_consume` returns items sorted by their `<ns-epoch>.<pid>.<seq>.<sender>` id prefix, and a same-turn correction is always written LATER than the claim it corrects. So the correct handling is guaranteed to arrive AFTER the incorrect one, and any act-per-message loop that does not read to the tail before acting will act on stale claims."
fix: "Drain the whole queue into memory FIRST, then decide. The consume tool description already reminds you of this at drain time (`QUEUED, oldest first — READ THIS BEFORE ACTING ON ANY ONE OF THEM. Delivery is FIFO, so a correction is always consumed AFTER the claim it corrects`) — the trap fires when you re-enter the drain later in the turn without re-reading the notification, or when you act inline in a per-message handler. If a later subject in the same drain retracts an earlier one, honour the later one. This is a property of shared mailbox infrastructure, not of any one seat's discipline — it fires for every recipient of every multi-message sender, and it fires hardest for whoever is least expecting it."
domain: pantheon, mailbox, mcp, fifo, ordering, correction, drain, act-per-message
---

# Mailbox FIFO delivery guarantees corrections arrive AFTER the claim they correct

**Trigger:** you're about to consume your mailbox and act on the results (one message,
per-message handler, or an inline "process each and continue" loop).

**Class:** infrastructure property that turns into a defect at the consumer, not a bug in
the transport. The FIFO guarantee is exactly what it says on the tin; the trap is what
consumers do with it.

## Mechanism

`mailbox._drain` scans `new/` and yields entries ordered by filename. Filenames are
`<ns-epoch-since-1970>.<pid>.<seq>.<sender>`, minted at `send()` time from
`time.monotonic_ns()`-adjacent stamps. So:

- Two messages from the same sender in the same turn are strictly ordered by send time.
- A later correction has a strictly higher timestamp than the message it corrects.
- Consequently, the delivery order matches the send order, and a same-sender
  claim/correction pair always arrives as `[claim, correction]` in that order.

There is no priority queue, no in-transit merge, no re-ordering by content or subject —
FIFO is the whole model.

## Why the act-per-message loop is the trap

The obvious handler shape is:

```python
for msg in consume():
    act_on(msg)                # <-- fires the moment you see the claim
```

If the correction is the second message, `act_on(claim)` has already run before the
loop looks at `correction`. The correction then arrives at a state the claim already
mutated — and it might not be reversible.

## The correct shapes

**Drain-then-decide (default).** Read all of `new/` into memory. Scan for pairs where
a later subject supersedes or contradicts an earlier one. Honour the later. Act once.

```python
msgs = consume()  # returns everything currently in new/, ordered
# resolve corrections in-memory first
final = resolve_supersessions(msgs)
for m in final:
    act_on(m)
```

**If you must act inline** (e.g. the handler is stateful and can't buffer), at least
**always finish the drain before ending the turn** — a partial drain leaves later
corrections in `new/` where the next re-consume will fire them against a state the
previous turn already mutated on the wrong claim.

## Why this doesn't belong in one seat's handoff

The rule is a property of shared infrastructure that every mailbox consumer inherits
by construction. It fires the same way for every seat, so it should be findable by any
agent's grep of `bugs/`, not filed under whichever seat happened to trip it first.
Specialist's point on lifting it here (2026-08-03): *"A rule filed under who discovered it
never fires for anyone else. Sort by trigger condition (what were you about to do),
not by author."*

## Interaction with `supersedes=`

`supersedes=` (see [`mailbox-supersedes-deletes-the-prior-message-and-tells-the-sender-nothing.md`](mailbox-supersedes-deletes-the-prior-message-and-tells-the-sender-nothing.md))
is the sender's tool for the case where the prior message is WRONG and must not be
read. When used correctly by the sender AND the prior message is still in `new/`,
the correction pair collapses to just the correction on the recipient's side — the
FIFO trap does not fire.

But `supersedes=` cannot fire once the prior message has been drained to `cur/`, and
the sender doesn't know which state the recipient is in. So a follow-up in-flight in
`new/` is the *common* case; the FIFO-drain-then-decide discipline is the belt to the
`supersedes=` suspenders.

## Discovered

- Surfaced in principle for years; explicitly stated in the `mailbox_consume` tool
  description as the drain-time reminder. Lifted here by specialist 2026-08-03 (after their
  own supersedes-window burn earlier the same day) with the framing that the drain-time
  reminder is transient and the rule needs a durable home indexed by trigger.

## Keywords

pantheon, mailbox, mcp, mailbox_consume, drain, fifo, ordering, correction, supersession, act-per-message, per-message-handler, read-all-of-new
