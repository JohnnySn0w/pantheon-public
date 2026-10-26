---
symptom: "an auto-delivered prompt sits typed but unsubmitted in an idle seat's input box while send_keys_submit.sh reported success (exit 0); the seat then reads busy for hours (mailbox_wake defers, inbox_consumer stays dispatch_locked) until someone presses Enter or the 2h mail_staleness backstop fires. The pane's input box starts mid-prompt."
fix: "fixed in T-5380: submit_verify also matches the TAIL of the injected text in the composer region, both retry loops re-check pending before every retry Enter, and an empty pre-retry capture is no vote; inbox_consumer.deliver() refuses to paste over its own still-pending prompt (SkipCycle inject_pending_paste, alerts after ~30 min). For an already-stuck pane: one Enter via tools/send_keys_submit.sh after confirming the pane is idle (T-5408 automates this)."
domain: "pantheon, submit_verify, send_keys_submit, inbox_consumer, tmux, composer, wake-path"
---

# Auto-delivered prompt left unsubmitted: submit-verify head needle goes blind when a long paste scrolls the composer (T-5380)

Keywords: composer, scroll, needle, head, tail, unsubmitted, stuck prompt, submit_verify, inbox_consumer, T-4319, busy-stale

## Symptom
A ticket prompt delivered by inbox_consumer sits typed but unsubmitted in an idle
seat's input box. send_keys_submit.sh exits 0 ("confirmed"). The seat reads BUSY for
hours (mailbox_wake: "deferred, pane busy"; inbox_consumer: skip:dispatch_locked), until
mail_staleness fires its 2h busy-stale backstop (T-4323) or someone presses Enter.
Seen twice on recovery, 2026-10-22: T-5148 pulled 15:15:45Z, Enter by hand ~17:24Z (2.1h,
pane had just "Compacted while idle"); T-5221 pulled 19:49:17Z, Enter by hand ~23:41Z
(~3.9h, pane at a plain turn end, "Sauteed for 37s").

## Cause
submit_verify._composer_holds_text matched only the FIRST 48 characters of the injected
text inside the composer region. A ~700-char single-line paste wraps to ~11 rows; the
composer shows about 7-8 and keeps the cursor (the tail) in view, so the head scrolls
out. Both real captures begin mid-prompt ("auto-approve scope, ..." / "anything outside
auto-approve ..."). After one Enter that the TUI swallowed, the needle was absent, the
pane read "not pending", and the retry loop (2 more Enters) never ran. A single-line
paste is not collapsed, so there is no "[Pasted text" sentinel to catch it either.
Why the first Enter was swallowed is not established. Compaction is not required (case 2
had none); an Enter arriving ~0.7s after a long paste is the likely candidate.
The 2h busy read is not a misread: pane_idle_state calls a pane idle only over a visibly
EMPTY composer, so unsent text under the mode footer is busy by design, and it
self-sustains (the wake defers, the ticket stays in flight).

## Fix (T-5380)
- submit_verify: also match the TAIL of the injected text in the composer region
  (history above the composer and anything below the border stay excluded).
- inbox_consumer.deliver(): check_no_pending_delivery refuses to paste while our own
  prompt is still in the composer (SkipCycle inject_pending_paste, alerts after 6
  skips). Needed because a failed delivery leaves no stamp, the next cycle re-picks the
  same ticket, and main() has no pane check.
- inbox_consumer.inject(): log the helper's stderr tail (it was discarded, so the
  journal had no trace).

## Not fixed (follow-up)
A pane already stuck still waits for the backstop or a manual Enter. A bounded
single-Enter recovery for ticket prompts (extending mailbox_wake.recover_stuck_notice)
is the open follow-up.

## How to recognise it again
Journal shows "pulled T-xxxx", then pane=busy with in_flight=1 and unread climbing for
hours, no submit_verify output. The pane's input box starts mid-prompt.

## Verification
`python3 -m pytest tools/test_submit_verify.py -k HeadScrolledComposer -q`: 6 of these 11 tests fail on the pre-T-5380 predicate (head-only needle) and pass after it. The fixtures are the two real recovery pane captures from 2026-10-22, where the composer starts mid-prompt. Live symptom: `send_keys_submit.sh` exits 0, while `tmux capture-pane` on the seat still shows the prompt's tail in the input box.
