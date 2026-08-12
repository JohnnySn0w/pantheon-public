---
symptom: "A guard that filters `git diff --cached --name-only` output by prefix (`p.startswith('tools/')`) or feeds it to `git show :<path>` SILENTLY SKIPS files whose names git decides to quote. With `core.quotePath` at its default, a non-UTF-8 (or otherwise unusual) filename comes back ESCAPED AND DOUBLE-QUOTED — `tools/bad_<bytes>_name.py` renders as a string whose FIRST CHARACTER IS A LITERAL DOUBLE-QUOTE. So the prefix test is false and the file is never linted, and `git show` on the bogus path fails, so a blob reader returns None. Both failures are silent: no error, no warning, the guard reports success having read nothing."
fix: "Fix at INTAKE, not downstream: pass `-z` to `git diff --cached --name-only` and split on NUL. `-z` disables the quoting entirely, so every consumer of the list is repaired at once and none has to know `core.quotePath` exists. Capture BYTES (drop `text=True`) and decode with `errors='surrogateescape'` — subprocess re-encodes those strings back to the original bytes on POSIX, so `git show :<path>` still resolves for a non-UTF-8 name. TESTING THIS NEEDS REAL GIT: a unit test over a synthetic list of path strings exercises neither git's quoting nor its binary detection and PASSES AGAINST THE BUG. Stage a real file with a name git quotes and observe the guard missing it before and catching it after, and pair every case with an ordinary-named CONTROL — without one, a MISS cannot be told from a broken harness."
domain: "git, quotePath, pre-commit-hooks, guards, silent-skip, byte-sequence-guard, encoding, test-design, pantheon"
---

# `core.quotePath` escapes a path out of its own prefix test, and the guard reports success

**Class:** silent-skip in a guard. Not a git bug — documented `core.quotePath` behaviour meeting code
that treats git's path output as a plain string.

**Found:** 2026-07-29 by the T-2314 reviewer, who verified it against a real repo with a real staged
file rather than reasoning about the code, and correctly kept it OUT of that ticket's diff and flagged
it up instead. Fixed under T-2325.

## The shape

```
$ git diff --cached --name-only        # core.quotePath default
"tools/bad_\377\376_name.py"           # <- leading " is part of the STRING

p.startswith("tools/")   -> False      # never linted
git show :"tools/...     -> fails      # blob reader returns None -> skipped
```

Two consumers, two different silent skips, one cause. Neither logs anything.

## Why this one is worth cataloguing

The affected guard was the local half of the `HERMES\.md` byte-sequence discipline — the layer that
exists because that sequence in git-log content routes requests to extra-usage billing via Anthropic's
anti-abuse classifier (a real user ate a $200.98 overage; per the T-1216 re-eval the classifier is
active by design, not a lapsed bug). So the hole was a path by which contaminated content reaches a
commit **with the guard never having read it**, while the guard reports clean.

It was never a live incident: reaching it needs a deliberately odd filename, no such file is tracked,
and nothing suggests it has ever happened. A hole in a guard, not a breach.

## The part worth stealing: complementary blind spots are LUCK, not architecture

Probing both layers of the same guard found the mirror image of this bug on the other side:

| case | local pre-commit hook | server-side audit |
|---|---|---|
| quoted path, trigger in text | **blind** (this bug) | catches |
| binary file, trigger in bytes | catches | **blind** |

The local hook reads staged **blobs** at byte level — sees binary, blind to oddly-named paths. The
audit consumes **unified diff text** — the mangled name only appears in the `+++ b/<path>` header
while the added content lines are emitted and scanned normally, so it sees odd names and is blind to
binary (git emits `Binary files ... differ` with no content lines).

Neither layer alone covers both. That they cover each other **was not designed** — record it as luck,
because crediting it as architecture invites someone to remove a layer on the grounds that the other
one has it covered. Different intake shapes fail in different directions; only probing each shape
against each threat tells you which.

## Corollary: a severity downgrade can expire without anyone touching it

This ticket's severity was downgraded on the reasoning "real for the local layer, closed at origin."
That was true when written and became false when an unrelated thing — CI queue throughput — put the
server-side layer hours behind every push. The mitigation never changed and never failed; it just
quietly stopped being prompt, so citing it stayed superficially true.

Ask not only *is the hazard this compensates for still real?* but *is this mitigation still doing the
thing I credited it for?* The second is harder to see, because the control still functions.
