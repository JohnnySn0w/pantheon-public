---
symptom: "test_ticket_lint.retarget and test_ticket_state_sweep.t0141 fail under full `unittest discover -s tools` but pass in isolation and in their own modules — order-dependent cross-module pollution with an UNIDENTIFIED mechanism. Currently latent, not fixed: masked-on-main since T-1936 (2026-07-23) has RESURFACED — the t0141 half unmasked twice on 2026-08-25 (T-3240 arc, T-3241 arc); the retarget half stayed green those runs. Surfacing is stochastic under concurrent host load — a clean serial re-run of the identical failing tree passed."
fix: "None yet — and do NOT apply the T-1943 idiom (_git_common_dir cache reset in setUp): that mechanism is DISPROVEN for these two. Atlas's falsification (2026-07-23, T-1939 arc): both tests shell out to ticket.py as a SUBPROCESS, which builds its own fresh in-process cache, so the parent test-runner's poisoned/warmed cache is irrelevant — he set the cache to the bogus linked-worktree FILE shape and both tests still passed. If the pair resurfaces (any change that reorders discovery can unmask it), file a ticket WITH the failing reproduction — that is the only honest basis for a fix; a setUp cache-reset would be a no-op that reads as handled."
domain: pantheon, tests, unittest, discover, order-dependence, pollution, subprocess
---

# Order-dependent pollution in ticket_lint/ticket_state_sweep — polluter unidentified, currently masked

## What is known

- Pre-2026-07-23 (before the T-1936 merge), `test_ticket_lint.retarget` and
  `test_ticket_state_sweep.t0141` failed deterministically in full-discover and passed in
  isolation — classic cross-module pollution (Atlas, T-1936 baseline differentials).
- Post-T-1936, full-discover green (3595 OK) was **incidental discovery reordering** from two
  new test modules, not a fix. Pollution latent.
- The obvious suspect is DISPROVEN: it is NOT the `_git_common_dir` module-global cache
  (the T-1943 mechanism). Both tests invoke `ticket.py` as a subprocess with its own fresh
  cache; poisoning the parent's cache with the worst-case bogus-file shape does not touch them.
- **2026-08-25 resurfacings (T-3257 fold, T-3240 + T-3241 arcs):** the t0141 half unmasked twice
  in one day, retarget stayed green each run.
  - **T-3240 (agent/T-3240 @ db5805b6f8fd99511ba79cdb9b5ade561f16e0be):** adding
    `tools/test_harness_adapter_injection.py` (+590 lines, +51 discoverable tests) reordered
    discovery and unmasked t0141. Repro: `python3 -m unittest discover -s tools -p 'test_*.py'`
    → `Ran 8589 tests / FAILED (failures=1, errors=89, skipped=86)`, the single failure being
    t0141. Isolation of t0141 alone: `Ran 1 test / OK`. Verbatim smoke log preserved at
    `/tmp/claude-1000/-home-claude/193a6f06-5a95-4996-a8f3-eebb5c1d8a57/scratchpad/t3240-smoke.log`
    (session-scoped, not durable long-term). Error-set diff vs baseline: 89 errors byte-identical
    name-by-name; only the 1 failure is the pollution. Confirmed by re-inspection at T-3257 fold time.
  - **T-3241 (agent/T-3241 @ 2aaa9a5a73c0b0a1d913057d6fe1f2253f2a8fbe):** same failure signature under
    concurrent host load (~10 observed-behaviour demo subprocesses, ~200KB payload each): `Ran 8538 /
    FAILED (failures=1, errors=89)`, t0141 the failure. Clean sequential re-run of the identical
    tree (no concurrent load) passed t0141: `Ran 8572 / FAILED (errors=89)` — failures=0. Verbatim
    smoke logs preserved (session-scoped) at `t3241-full-smoke.log` and `t3241-full-smoke-run2.log`
    in the same scratchpad dir as T-3240. Re-verified at T-3257 fold time.
- **Concurrent host load is a real surfacing channel** — not just discovery order. T-3241's serial
  re-run of the identical failing tree passed, isolating concurrent-load as the differential.
  Candidate mechanisms this admits (still speculation): shared-tmp namespace collision, CPU-time-
  budget in subprocess ticket.py, git-object contention, filesystem-mtime resolution races.
- **Test-count drift within the same tree is real too**: `Ran 8538` (T-3241 concurrent, fails),
  `Ran 8572` (T-3241 serial re-run, passes) — same worktree, same discover invocation, +34 test
  delta with the failure absent in the higher-count run. Source of the +34 unattributed at the
  T-3257 fold — it is NOT the T-3241-added tests (tools/hooks/ lacks `__init__.py` so discover
  cannot reach them). Worth a separate look; may or may not be the same pollution class.

## What is NOT known

The actual polluter and its channel (candidates untested: cwd leakage, env-var leakage,
shared-tmp collision, git global state, subprocess time budget, mtime-resolution races,
CPU contention — all unverified speculation; listed only so the next investigator doesn't
mistake this section for findings). The +34-test discover-count drift between two apparently
identical runs is a separate open thread — mechanism unknown, may be an independent defect.

## Investigation plan (open)

- **Bisect** the ~40 tools/ test modules discover picks up before `test_ticket_state_sweep` to
  isolate the polluter — but the bisect is UNRELIABLE while the surfacing is stochastic under
  concurrent load (T-3241 evidence). Prerequisite: a deterministic repro. Path forward is
  either (a) find a load-independent unmask (a new test module added to the tree, per the T-3240
  path), then bisect from that green→red boundary; or (b) synthesize the concurrent-load channel
  reproducibly (e.g. background CPU pinning during the discover run).
- **Root-cause the +34-test discover-count drift** first — it may be the same class as the
  t0141 pollution (both surface under concurrent-load differentials), and identifying its channel
  might shortcut the polluter search.

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
- `tickets/done/T-3257.md` — the 2026-08-25 resurfacing fold (T-3240 + T-3241 arc incidents);
  entry above updated from that ticket's repro data. Bisect step recorded as open, not attempted
  — the stochastic-under-concurrent-load surface makes bisect unreliable without a deterministic
  repro first.
