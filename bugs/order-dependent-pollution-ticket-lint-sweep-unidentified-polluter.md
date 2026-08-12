---
symptom: "test_ticket_lint.retarget and test_ticket_state_sweep.t0141 fail under full `unittest discover -s tools` but pass in isolation and in their own modules — order-dependent cross-module pollution with an UNIDENTIFIED mechanism, currently MASKED on main (T-1936's two new test modules reordered discovery and the failures stopped manifesting)."
fix: "None yet — and do NOT apply the T-1943 idiom (_git_common_dir cache reset in setUp): that mechanism is DISPROVEN for these two. Atlas's falsification (2026-07-23, T-1939 arc): both tests shell out to ticket.py as a SUBPROCESS, which builds its own fresh in-process cache, so the parent test-runner's poisoned/warmed cache is irrelevant — he set the cache to the bogus linked-worktree FILE shape and both tests still passed. If the pair resurfaces (any change that reorders discovery can unmask it), file a ticket WITH the failing reproduction — that is the only honest basis for a fix; a setUp cache-reset would be a no-op that reads as handled."
domain: pantheon, tests, unittest, discover, order-dependence, pollution, subprocess
---

# Order-dependent pollution in ticket_lint/ticket_state_sweep — polluter unidentified, currently masked

## What is known

- Pre-2026-07-23 (before the T-1936 merge), `test_ticket_lint.retarget` and
  `test_ticket_state_sweep.t0141` failed deterministically in full-discover and passed in
  isolation — classic cross-module pollution (Atlas, T-1936 baseline differentials).
- Post-T-1936, full-discover is green (3595 OK) — but this is **incidental discovery
  reordering** from two new test modules, not a fix. The pollution is latent, not gone.
- The obvious suspect is DISPROVEN: it is NOT the `_git_common_dir` module-global cache
  (the T-1943 mechanism). Both tests invoke `ticket.py` as a subprocess with its own fresh
  cache; poisoning the parent's cache with the worst-case bogus-file shape does not touch them.

## What is NOT known

The actual polluter and its channel (candidates untested: cwd leakage, env-var leakage,
shared-tmp collision, git global state — all unverified speculation; listed only so the next
investigator doesn't mistake this section for findings).

## The rule this entry encodes

A latent, mechanism-unknown, currently-unreproducible defect gets a **label, not a fix**: any
fix shipped without a reproduction is unfalsifiable and reads as "handled" while protecting
against nothing (the no-op-fix trap — sibling of the false-red family). When a discovery-order
change unmasks the pair, capture the failing order verbatim and file a ticket with it; bisect
from a real repro only.

## Cross-refs

- `tickets/done/T-1943.md` — the sibling pollution that WAS root-caused (different mechanism,
  in-process cache; its idiom does not transfer here — proven, not assumed).
- T-1936 / T-1939 transition logs — the discovery and falsification provenance.
- `lessons/2026-07-22-false-red-verifier-must-call-prod-parser.md` — the check-your-own-check
  discipline that produced the falsification.
