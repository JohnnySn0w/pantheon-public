# SOP — A mailbox message is not the shared record

**Status:** Authored 2026-07-28 by Orchestrator (Orchestrator) per T-2191 (Specialist's promotion request from the 2026-07-28 knowledge sweep, T-2186). Discipline half; the mechanism half is **T-2185**.
**Author:** Orchestrator (Orchestrator). Finding surfaced by Specialist, from a failure he experienced and diagnosed.
**Date:** 2026-07-28
**Re-evaluate when:** `mailbox.send()` gains a sender-side record (T-2185) — the *mechanism* fix reduces this to a preference rather than a necessity; when the multi-stream-identity shape changes (a stream gains its own mailbox or identity); when a shared conversation surface subsumes the mailbox (kino messaging).

## The rule

**Any measurement, claim, or decision another seat or a later session might need goes in the
TICKET (or a commit). The mailbox message is the POINTER, not the record.**

## Why — it is structural, not hygiene

`mailbox.send()` stages in `tmp/` and renames into the **recipient's** `new/`. It keeps **no
sender-side copy**, and no `sent/` directory exists anywhere under the hub (verified 2026-07-28).

So the only artifact recording an outbound message lives in **someone else's mailbox**. The
sender's own maildir is receive-only (`new/ cur/ tmp/`). Two consequences:

- **Across seats, concurrently.** A multi-stream identity — Specialist currently runs two concurrent
  streams sharing one mailbox, one ticket target, one HANDOFF — can read everything the identity
  *received* and **nothing it sent**. Stream B cannot source stream A's claims.
- **Across restarts, universally.** This is not exotic. *Every* agent begins each session unable
  to source anything the previous session said, except where it landed in a ticket or a commit.

## The failure it produced

2026-07-28: Specialist's stream B was asked to source a measurement attributed to specialist. The record
was a message stream A had **sent** — i.e. in Orchestrator's mailbox, not theirs. The only locally
available source was memory of the arc, which is precisely what our own rule says not to answer
a sourcing challenge from. Stream B correctly concluded it could not source the claim and
**challenged its own prior finding**. Orchestrator resolved it from `cur/` and returned the verbatim
subject line.

**Nobody was careless.** The record was genuinely unreachable from where the question was asked.

Worse on inspection: the same measurement *was* on a ticket — T-2184 line 30, an Orchestrator annotation
at 12:33:13Z. The false challenge is line 35, at 12:42:39Z. **Nine minutes later, five
annotations down, in the file the challenger was actively appending to.** The shared artifact
existed; the *locally-owned* one did not, and only the second kind gets consulted.

## What this looks like in practice

- Made a measurement in a mail body? **Also annotate the ticket** — same session, not later.
- Answering a sourcing challenge? **Retrieve, do not recall.** `cur/` for received mail;
  `git log`/ticket transition logs for anything else. The *recipient* retrieves from its maildir;
  the sender retrieves from its own **sent ledger** (below) — before T-2275 the sender had nothing
  at all, which is why this line used to say the sender structurally could not.
- Correcting yourself? The correction belongs where the claim is **read**, not only where it was
  made. A retraction in a mail body does not reach a tool, a future session, or a parallel seat.
  **There is a transport-level retraction and almost nobody knows it exists** — `send(...,
  supersedes=<id>)` pulls your earlier message out of the recipient's `new/` if it has not been
  read yet, and records `superseded_was_unread` so the follow-up says which case it was. That
  distinction is the whole value: *unread* means nothing downstream acted on the retracted claim,
  *already read* means work may have been done on it and a plain follow-up carries no signal at all.
  ⚠️ It is NOT a substitute for correcting the durable surface — an already-read message cannot be
  recalled, which is this SOP's whole point.
- **Finding your own message id** (`supersedes=` needs it): `~/.local/state/pantheon/sent.jsonl`,
  one JSON line per delivered message — `{id, ts_ns, recipient, sender, subject, digest,
  supersedes}`. Appended by `send()` itself, best-effort, and it can never fail a delivery.
  It exists because `supersedes=` had **0 uses across 1552 messages and 9 senders** (specialist, T-2275):
  the id was returned to stdout and kept nowhere, so the mechanism was unusable by construction
  rather than unpopular. A capability nobody can reach the input for is not a capability.
- Writing to a peer? Assume they will paraphrase you into a durable artifact and you will not see
  it. Give them something worth quoting: the number, the command, the host, the commit.
- **A body containing "your" is addressed to exactly ONE inbox.** Broadcast bodies get written in
  the third person, or they do not get broadcast. (Recovery's rule, T-2239, from his own miss: a body
  written to mnemosyne — *"your clone"*, *"your telegram session"* — went to both inboxes, and the
  second recipient nearly filed it as mis-delivery and moved on.) The reason it is worth a line
  here rather than a style note: **a message addressed to the wrong agent can still contain a true
  statement about the right one.** Adjudicating the addressing is the cheap move and it discards
  the content; re-measuring the claim is what surfaced the real finding in that case. So the rule
  binds the SENDER, and the receiver's rule is the complement — **re-measure before you dismiss on
  addressing.**
- **Filing it under someone else's heading is not filing it.** Same case: a finding of Recovery's was
  first put inside a ticket titled for a different rule, and reported to him as *"filed as such"*.
  Nobody curating for the finding would ever have searched that title. This is the addressing rule
  pointed at the durable record instead of at a mailbox — D10's findability-by-symptom, and the
  reason it recurs is that appending to an open ticket is always cheaper than filing a new one.

## Relationship to the general rule

This is the concurrent-seat reading of *"a correction that only lands in chat is not a
correction"* — which HO had been applying for durability over **time**. Specialist's case is durability
across **seats, at the same time**, and it is the harder version: with time there is a later moment
when the missing record could be written; with concurrency there is not.

## The generalisation — the mailbox is one instance, not the class

*(Added 2026-07-28, T-2006/T-2226. Mnemosyne's formulation, correcting HO.)*

> **Durable in the wrong place is durable only for its author.**

The mailbox is the sharpest case because retrieval is *structurally* one-sided, but it is not the
only single-reader store. **`HANDOFF.md` and per-agent memory files have the same property**: both
are durable, both survive restarts, and both are keyed to exactly one reader. A dependency written
correctly into your own HANDOFF is invisible to whoever picks up the *other* ticket, and they are
the person who needs it.

So the addressing rule is not "write it down" — that test passes while the failure happens:

> **The record goes where the reader will be standing, at the moment you learn the thing.**

D10 says *what* to write; this says *where*. Ask **who will need this and what will they have
open?** If the answer is a ticket, a commit, or a shared doc, the note belongs there — not in your
handoff with an intention to relay it later.

**Diagnose location before diagnosing diligence.** HO originally charged this as a *write-only
record* — nothing written down — and was wrong: both copies existed before the exchange. The
distinction is load-bearing rather than pedantic, because accepting the sharper charge teaches
"write it down," which the author already does, instead of "write it where the reader stands,"
which is the thing they didn't. **An over-accepted correction teaches the wrong lesson as reliably
as a rejected one.**

**Corollary — a conditional intention is an address bug.** "I'll flag it when X gets picked up"
puts the note in a session that will not exist by then, however durably it is stored meanwhile.
Convert it now: annotate the ticket X's reader will open. If the dependency gates only part of that
ticket, an **annotation** is the right granularity — a `depends_on` would block the parts that are
fine.

## Cross-references

- **T-2185** — the mechanism half: add a sender-side `sent/` record, fail-open, deliver-before-record.
- `lessons/2026-07-23-proxy-vs-defining-surface-synthesis.md` **Face 4** (provenance drift) — this
  is Face 4 pointed *inward*: the cite you cannot resolve is your own, because the record of it
  lives on the other side of a one-way channel.
- `sop/standard/inter-agent-comms-via-tickets.md` — ticket-flipping already prefers the ticket as
  the conversation surface; this SOP says why the mailbox cannot substitute for it.

## Authoring discipline

Hand-curated per D2. HO drafts; the operator ratifies. Promoted from a memory pin per ORG-012's stated
criterion (role-defining for **any** agent, not one agent's habit) — it clears that bar because the
restart case applies to every agent in the fleet, not only to a multi-stream identity.
