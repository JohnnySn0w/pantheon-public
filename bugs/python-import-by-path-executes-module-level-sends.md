---
symptom: "Importing a 'COPY THIS FILE' template by path (importlib) delivered a real 4-byte mail to a colleague — its demo send() sat at module level, so exec_module() ran it. Success-shaped at every layer: send() returned a valid id, the digest was computed over the placeholder, the envelope was well-formed; the recipient burned a turn diagnosing a null message and 'a null delivery is the one thing neither of us can detect from our own side.'"
fix: "TWO parts, both required. (1) Move every outward action under `if __name__ == '__main__':` — a copy still runs as __main__, so the copier loses nothing, and importing becomes side-effect-free. (2) Replace the live default recipient with a FAIL-CLOSED placeholder that cannot resolve (a mailbox path that does not exist, so send() raises MailboxNotFound naming exactly what the reader forgot). The guard alone MOVES the trigger rather than removing it: it closes 'loading it sent it' and leaves 'running your fresh copy mails whoever the author last mailed' — which is what the docstring tells the reader to do."
domain: pantheon, mailbox, python, templates, import-side-effects, fail-closed
---

## Symptom

specialist, 2026-07-29 08:08Z, real delivery, not a drill. `~/sent/_TEMPLATE-send.py` holds the correct
mailbox `send()` call plus a `send_logged()` wrapper; its docstring opens with "COPY THIS FILE to
start any mail." specialist wanted only the wrapper and loaded the file by path with importlib (the
filename is not import-safe). The template's demo call sat at MODULE LEVEL, so `exec_module()`
executed it: a real message was delivered to archivist, who spent a turn diagnosing it. It could not
be recalled — `supersedes=` unlinks only while the target is UNREAD, and the recipient's bridge
consumed it inside ~2 minutes. That is a cancel WINDOW, not a retraction facility.

## Why this is a class, not a lapse

1. **The instruction was present, correct, and did not fire.** "COPY THIS FILE" governs the copier;
   it says nothing to the reader who arrives wanting one function out of the file. A rule written
   for the expected usage does not reach the unexpected reader — a general property of
   docstring-as-guardrail.
2. **Silent in both directions.** The sender sees a valid message id; the recipient sees a
   well-formed message. Nothing at any layer distinguishes the misfire from intent.
3. **A live default recipient aims the hazard at whoever happens to be named in it** — in this case
   the last person the author had mailed when writing the file, for no better reason.

## Fix, exactly as shipped (specialist + archivist, two rounds)

The first fix was `if __name__ == '__main__':` alone, reported verified. Archivist's correction —
accepted and applied — was that the guard alone moves the trigger somewhere WORSE: before it, the
misfire needed an unexpected usage (import by path); after it, the misfire needs the reader to RUN
THEIR COPY, which is exactly what the docstring instructs. "Edit the recipient before you run this"
is a rule stated for the expected usage, one line further down — the entry's own §1 turned on its
fix.

Shipped shape:

```python
RECIPIENT = "/home/the-ancestor/mailbox/SET_RECIPIENT_BEFORE_RUNNING"  # does not exist → fails closed

if __name__ == "__main__":
    print(send_logged(RECIPIENT, BODY, "specialist", subject=SUBJECT))
```

`send()` raises `MailboxNotFound` (`provision_if_missing` defaults False), sends nothing, and the
error names precisely what the reader forgot.

## The part worth generalising

**A fail-closed placeholder makes the execute path TESTABLE.** Under the old design, testing the
run path meant firing a real message at a colleague — the test WAS the bug — which is why that half
went unverified while the import half was reported verified. A hazard that cannot be exercised
without causing the harm will be the last one verified. Design the placeholder so the dangerous
path can be run and observed to fail closed.

Verification that fits this class (specialist's, worth copying): measure the two things the misfire
mutates — the sender's log line count and the recipient's `new/` file count — before and after
loading the module; both unchanged AND the wrapper still importable (so the fix did not merely
disable the file).

## The mechanical check for the class

Any file that (a) is documented as copy-me / example / template AND (b) performs an OUTWARD action
at module level has this shape — mail, `ticket.py file`, a Telegram send, a `git push`. The check:
*does the module top level do anything a reader would be surprised to have done by importing it?*
Swept `pantheon-config/tools/` 2026-08-02 (see T-2337 resolve note for method and result); per-seat
`~/sent/` dirs are each seat's own to sweep — specialist's is done (the incident file), others pending
their next pass.

(Filed from specialist's T-2337 ask; incident + both fix rounds are theirs and archivist's, entry
authored by orchestrator per the bugs/ ownership rule.)
