---
symptom: "Adding a new mutating verb to ticket.py (a new cmd_<verb> + argparse wiring) that ALSO writes a commit through _commit_and_push() will hard-fail at first commit time with TicketError('unknown verb: <verb>') if the corresponding branch wasn't added to _build_commit_message's closed elif chain. The failure is at COMMIT time, not at PARSE/DISPATCH time — so happy-path testing passes until the first real write."
fix: "When adding any mutating verb to ticket.py, also add a matching branch to _build_commit_message that formats the commit subject line for that verb. Absence-of-branch is caught by the else-raise, not by parse-time validation — the whole test suite can be green + the verb visibly wires through argparse + the frontmatter mutation lands + then _commit_and_push falls into the raise on the last step."
domain: pantheon, ticket-py, commit-message, verb-addition, silent-hard-fail
---

# ticket.py new-verb addition: _build_commit_message closed-elif silently hard-fails at commit time

## Symptom

You add a new mutating verb to ticket.py — say `cmd_label_add` — and wire it through argparse via a new subparser (`label add <T-NNNN> <label>`). You write tests for the new command. Everything green. The verb accepts input, mutates the frontmatter correctly, appends the transition-log entry — all as intended.

Then you actually run it against a real ticket:

```
$ python3 tools/ticket.py label add T-1234 my-new-label
# ... frontmatter written to disk, transition log appended ...
Traceback (most recent call last):
  File "tools/ticket.py", line 4523, in _commit_and_push
    subject = _build_commit_message(verb, tid, ...)
  File "tools/ticket.py", line 4468, in _build_commit_message
    raise TicketError(f"unknown verb: {verb!r}")
TicketError: unknown verb: 'label-add'
```

The verb landed the ON-DISK mutation but couldn't produce the commit subject. Depending on where `_commit_and_push` catches this vs re-raises, you may end up with a modified ticket on disk that can't be committed, requiring manual git-restore + fix + re-run.

## Verification

```
grep -n 'def _build_commit_message' tools/ticket.py
# See the function is a closed elif chain of known verbs
# ending in `else: raise TicketError(f"unknown verb: {verb!r}")`
grep -n 'if verb ==' tools/ticket.py
# See the branches — one per known verb (file, annotate, resolve, retarget,
# block, unclaim, claim, receipt, etc.)
```

Add a new verb without a matching branch, exercise it, watch it raise.

## Fix

When adding ANY new mutating verb to ticket.py:

1. Add `cmd_<verb>` function.
2. Add argparse subparser wiring.
3. **Add matching branch to `_build_commit_message`** — before the closed-elif else-raise:
   ```python
   elif verb == "label-add":
       labels_added = ", ".join(context.get("labels", []))
       subject = f"tickets: annotate {tid} — label add: {labels_added}"
   elif verb == "label-remove":
       labels_removed = ", ".join(context.get("labels", []))
       subject = f"tickets: annotate {tid} — label remove: {labels_removed}"
   ```
4. Test the new verb end-to-end with a REAL commit (not just mocked _commit_and_push) — the branch-guard trap only surfaces on real writes.

## Why the failure is late

The verb-dispatch layer (`main()` → argparse subparsers → `cmd_<verb>`) doesn't validate against `_build_commit_message`'s known-verb set — no compile-time check binds the two. Only when `cmd_<verb>` reaches `_commit_and_push` → `_build_commit_message(verb, ...)` does the else-raise fire.

Mocked-write tests (`patch.object(am, '_commit_and_push')`) don't reach the branch either, so a test suite that mocks the commit will stay green forever.

## Related

- T-3020 BT5 (`label` verb + backfill-audit tool) — surfaced this trap when the impl added `cmd_label_add`/`cmd_label_remove`. Fixed by adding the two `elif` branches. Impl flagged as a "surprise worth naming" in the DECISIONS section.
- ticket.py:_build_commit_message — the closed-elif source.
- Test coverage guidance: whenever adding a new mutating verb, ensure at least one integration test exercises the real `_commit_and_push` path (not just its mock) so the branch-guard trap surfaces during test.
