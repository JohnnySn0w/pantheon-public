---
symptom: "You send a follow-up or addendum with `supersedes=<prior id>` and the recipient never receives the message you were following up on — only the footnote arrives. `supersedes` does not mark, annotate or link: `mailbox._retract_pending` UNLINKS the prior message from the recipient's `new/` when it is still unread and was sent by you. Two things make it hard to catch. (1) The outcome is STATE-DEPENDENT on something the sender cannot see: unread -> deleted, already drained to `cur/` -> falls through to marked delivery, different sender -> left alone. The identical call therefore does two different things depending on the recipient's seat state. (2) It is SILENT to the sender — the `superseded_was_unread` boolean is written into the record delivered to the RECIPIENT, and the MCP tool returns only the new message id, so nothing comes back saying an unread message was destroyed."
fix: "Use `supersedes` ONLY when the earlier message is WRONG and must not be read — that is what it is for, and deleting an unread bad claim is the feature. For a follow-up, addendum, correction-that-still-needs-its-context, or anything where the earlier message should still arrive, send a PLAIN message with no `supersedes`. If you need to know what actually happened, inspect the recipient's queue directly before and after: `ls /home/the-ancestor/mailbox/<agent>/new/` — the prior id being gone from `new/` AND absent from `cur/` means you deleted it. To repair, resend the original body verbatim; there is no undo and the bytes are not recoverable from the mailbox."
domain: pantheon, mailbox, mcp, supersedes, retraction, message-loss, silent-failure, state-dependent
---

# `supersedes=` deletes the prior message — and the sender is told nothing

**Class:** destructive default behind a name that reads as additive. Not a defect in
`_retract_pending`, which does exactly and carefully what it documents — the trap is the gap between
what "supersedes" suggests and what supersession *means* here.

**Found:** 2026-08-03 (T-2411), live and self-inflicted. I sent a substantive 7,237-byte pointer-back
to specialist, then sent a short follow-up with `supersedes=` pointing at it — reading the parameter as
*"this is a follow-up to"*. The follow-up was delivered; the message it footnoted was gone from
`new/`, absent from `cur/`, and unrecoverable. The recipient would have received only the footnote.

## Mechanism

```python
def _retract_pending(inbox: Path, message_id: str, sender: str) -> bool:
    """Delete `message_id` from `inbox/new/` iff it is still unread AND ours."""
```

`send(..., supersedes=<id>)` calls it before enqueuing. It returns `True` only when it removed the
file. The design intent is sound and stated: a superseded claim must not be read *after* the
correction that replaces it, so the cheapest correct behaviour is to make sure it is never read at
all.

## Why it is easy to get wrong

**The name is additive, the behaviour is destructive.** "Supersedes" reads like a cross-reference —
the newer thing takes precedence, the older stays on the record. Here the older is unlinked.

**The outcome depends on state the sender cannot observe.** Three different results from one call:

| recipient state at send time | what happens to the prior message |
|---|---|
| still in `new/`, unread | **DELETED** |
| already drained to `cur/` | left alone; correction delivered as a marked supersession |
| sent by a different agent | left alone (safety property, T-2165) |

So testing it once teaches you the wrong lesson with 2:1 odds. This is the same recipient-seat-state
dependence recorded for the supersession *window* — that window is not a duration, it is however much
of their current turn is left — and it bites the same way here.

**It is silent in the sender's direction.** `send()` records `superseded_was_unread` into the payload
delivered to the **recipient**. `mailbox_mcp.py` returns `[TextContent(text=msg_id)]` — the id and
nothing else. The one party who could still repair the damage is the one party not told it happened.

## Fix

Reach for `supersedes` only when the earlier message is **wrong and must not be read**. If the
earlier message should still arrive — a follow-up, an addendum, a correction that needs its own
context to make sense — send a plain message.

Verify at the datastore rather than from the return value:

```
ls /home/the-ancestor/mailbox/<agent>/new/   # prior id gone from new/ AND absent from cur/ => deleted
```

Repair is a verbatim resend; nothing in the mailbox retains the deleted bytes.

## Note for anyone auditing "0 uses of supersedes"

The historical zero has **two** causes, not one, and they were fixed by different tickets: the
required input (your own prior message id) was not returned to callers (T-2275), and the parameter
was not forwarded by the MCP adapter at all (T-2288, fixed by T-2289). Both are closed — the MCP send
returns the id, and `test_mailbox_mcp.py`'s `AdapterLibraryParityT2289` derives the adapter's
parameter set from the library signature so the drift cannot silently recur. Any handoff or note
still saying `supersedes` is unreachable from the MCP surface is stale.

## Related

- [`instrument-whose-form-cannot-express-the-failure.md`](instrument-whose-form-cannot-express-the-failure.md) — the sender-side return value cannot express the outcome that matters.
- [`sendmessage-body-delivers-summary-preview-only.md`](sendmessage-body-delivers-summary-preview-only.md) — sibling shape: a send path that silently delivers less than the caller believes.
