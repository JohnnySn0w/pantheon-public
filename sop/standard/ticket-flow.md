# Ticket flow — lifecycle, queues, and target/assignment discipline

Authored 2026-06-02 (T-0727 thread). Canonical reference for how tickets move through the system and — the load-bearing part — **who touches a ticket at each step**. Surfaced when HO `claim`ed a `target=operator` ratify ticket (wrong verb: `claim` is worker-takes-work; it misattributed the claim and pre-empted the operator's own step).

## States (ORG-019)

`inbox/ → active/ → done/`. Three directories; the directory IS the state. No `blocked/` dir — blocks are frontmatter only (`not_before:` calendar gate, or `depends_on:` dep gate; ORG-019). The lane shown in the PWA is derived from the directory.

## Verbs (`tools/ticket.py`)

`file` (→inbox) · `claim` (inbox→active) · `unclaim` (active→inbox) · `block` (set `not_before`/`depends_on` in place) · `resolve` (→done) · `retarget` (change `target`) · `close-noodle` / `close-action` (the operator's noodle/action close verbs) · `list`. All commit + push by default (`--no-push` to skip).

## `target` = assignment

`target` names who actions the ticket. The PWA surfaces **all** tickets sorted by status; a ticket is "assigned to" whoever its `target` is. `requester` is who filed it (audit only). Role IDs (`orchestrator`/`specialist`) normalize to persona names (`orchestrator`/`specialist`/`recovery`) on write; `operator` is the operator.

## The load-bearing rule: who claims what

- **Agent-targeted tickets (`target: orchestrator|specialist|recovery`)** — the per-agent inbox-consumer (`pantheon-inbox-consumer@<agent>.timer`) auto-delivers; the agent `claim`s → works → `resolve`s. **One active ticket per agent** is enforced structurally: T-0717's mutex stops *auto-dispatch* while an active ticket is held, and the T-0877 cap in `ticket.py claim` refuses a *manual* claim on the same condition (overridable via `PANTHEON_MAX_ACTIVE_TICKETS` for deliberate exceptions; `operator` is exempt). To take new work, first `resolve`, `retarget`, or `unclaim` the active one. A ticket blocked on the operator's decision should be `retarget`ed to `operator` (his lane), not parked in your active.

- **operator-targeted tickets (`target: operator`)** — HO/agents **file** them and **leave them assigned**. They land in `inbox/`, surface in the operator's queue (assigned via `target=operator`), and **the operator claims them himself** when he actions/ratifies — his claim records `[operator] claimed; moving to active`.
  - **HO MUST NOT `claim` a `target=operator` ticket.** `claim` is the worker-takes-this-to-work verb; HO claiming one misattributes the claim (`[claude] claimed`) and pre-empts the operator's step. There is no "assign-to-operator" verb because none is needed — `file` with `target: operator` already assigns it. After filing, HO's hands are off until the operator resolves it (or retargets it back).
  - If HO mistakenly claimed one: `unclaim` it back to inbox (target stays `operator`), then leave it.

## `intent` (target=operator only; T-0465)

`action` (operator do-X; default) · `ratify` (decision approval; **requires `ratifiable_files`** pointing at a committed file diff per ORG-015 — the PWA ratifies a diff, not prose) · `noodle` (chat-anchor; closed via `close-noodle`, optionally `--spawn-to` a child action ticket). Intent is not set for non-operator targets.

## Fan-out (D9 / ORG-019 / ORG-013)

Child tickets with non-empty `depends_on` file into `inbox/` with the dep gate set in frontmatter — they stay parked (grooming + consumer skip them) until the dep resolves. On parent `resolve`, ORG-013 cascade-unblock removes the resolved tid from each dependant's `depends_on` in place. `not_before` is reserved for **real calendar dependencies** only (release windows, scheduled events) — not HO-side pacing.

## Git-mutation discipline (the auto-commit owns the index)

*(Added 2026-07-14 by Orchestrator, T-1770 sweep S2 promotion — two failure modes, one root cause: `ticket.py`'s auto-commit assumes exclusive control of the git index/tree. Bugs: [`bugs/ticket-py-scope-guard-refuses-prestaged-index.md`](../../bugs/ticket-py-scope-guard-refuses-prestaged-index.md) + [`bugs/ticket-py-shared-checkout-interleaved-writer-race.md`](../../bugs/ticket-py-shared-checkout-interleaved-writer-race.md).)*

- **Never `git add` anything before calling `ticket.py`.** Its scope guard enforces a strict staged-set allowlist; pre-staged unrelated files make it abort mid-mutation (ticket file changed on disk, nothing committed, your staging lost). Sequence: ALL `ticket.py` ops first (each auto-commits), THEN stage + commit your own edits (ledger appends, ticket-body hand-edits) separately.
- **If the guard trips:** `git status` to see the true mixed state, explicitly stage-and-commit everything relevant in one commit, then verify `git log -1 --stat` matches the message's claims — a chained cleanup commit can silently claim more than it contains.
- **⚑ After ANY rebase on this repo, verify YOUR OWN commit is still in the log.** Do not
  trust `Successfully rebased and updated refs/heads/main` — it prints on a rebase that
  silently *dropped* your work. Mechanism (T-2147, live content loss 2026-07-28): when two
  commits each **create a file at the same path** (two `T-NNNN.md`, two ledger-shard
  appends, two autogen index writes), a `-X ours` rebase treats yours as already applied
  and emits only `dropping <sha> … patch contents already upstream` in the middle of
  otherwise-normal output. Nothing fails. Check:

  ```
  git log --oneline -3          # is my subject there?
  git show --stat HEAD          # does it contain the files I think it does?
  ```

  Cheap, and it is the step that would have caught the one real loss. **Never use `-X ours`
  to resolve an add/add on this repo** — for a same-path *creation* collision the correct
  response is to re-do your own write under a fresh identity (`ticket.py file` re-allocates
  automatically; see below), not to merge-strategy your way past it. *(`-X ours` is not
  actually a documented convention anywhere in this repo — T-2147 asserted it was, and a
  grep finds it only in that ticket. Treat it as a reflex to un-learn, not a rule to fix.)*

- **`ticket.py file` already self-heals an ID collision** — since `67dae6ef3` (2026-06-05,
  T-0846 follow-up) the FILE path passes `rebase_on_nonff=False`, so a racing same-ID push
  surfaces as a non-ff failure and `cmd_file`'s loop **re-allocates a fresh ID**. Allocation
  is already origin-aware too (`git fetch` + `max(local_max, origin_max)`, fail-loud on
  fetch error). So on a collision: **let the tool retry — do not hand-recover.** The 2026-07-28
  loss happened because the tool correctly surfaced the non-ff and the *hand* recovery
  discarded the commit.

- **Concurrent writers race on the shared checkout** — two agents' interleaved `ticket.py` add/commit/push sections can commit each other's staged files. Until the lockfile lands (T-1770 build), treat `ticket.py` bursts as a critical section: re-check `git status` before and after, and on a suspected interleave reconcile via reset-to-origin and re-run as the ORIGINAL actor (`feedback_shared_checkout_reconcile_pattern`).

## Closure discipline — closing against an ADR element

*(Drafted 2026-07-28 by Orchestrator, T-2167. **Ratified 2026-08-08 by the operator (T-2169).** Surfaced
by T-2163: an ADR element specified two changes, its child ticket implemented one, closed
green, and the gap stayed invisible for two months until the defect re-presented in
production.)*

The failure is granularity mismatch: we **verify** at ticket granularity ("did T-0684 pass
review and smoke? yes") but we **specify** at ADR-element granularity. When one ticket
implements one element, a green ticket reads as a satisfied element. Multi-clause elements
are what leak, and the tell is a conjunction — *plus*, *and also*, *as well as*.

- **Quote the element, don't name it.** A resolve note closing against an ADR element must
  quote the element text **verbatim** and state coverage **clause by clause**. T-0685's note
  recorded `E1 [resubscribe, T-0684]` — abbreviating a two-clause element by its first
  clause — and then asserted the ADR fully implemented. The second clause ("*plus the
  missing audio-source backoff*") did not get dropped in the code; it was dropped from the
  **accounting, at the moment of closure, by a shorthand**. An element referred to by a
  one-word label cannot be checked against its own text.

- **Report every named observable, including the clean ones.** When an ADR names the
  observables for its own acceptance gate, the gate's resolve note must report **each** of
  them explicitly. ADR-211 E5 required the soak to capture "*any hot-spin (CPU/log-rate)?*";
  the soak ran, really did bounce the broker, and recorded only recovery timing — so
  ACCEPTANCE MET was true against the wrong observable, since a busy-looping adapter
  reconnects fine. An **unreported** observable is indistinguishable from an **unmeasured**
  one. Note the defining surface here was written down *in advance* and simply not read at
  execution time.

Both are documentation requirements at closure time, not tooling — deliberately, because a
corpus-wide sweep for conjunction-bearing element headings found only **two** in the whole
agent-authored ADR set (one leaked, one clean), so the expected yield of an audit is low
while the cost of the discipline is a sentence. The discipline also subsumes the audit: it
catches body-level obligations a heading-level grep cannot see, because it forces reading
the element rather than its title.

## Acting on a watch-derived ask — re-read the cited source first

*(Drafted 2026-08-07 by Orchestrator, T-2524. Surfaced by Specialist: watch-derived asks are two paraphrase
hops from the primary source — source → report → ask — they are durable, and they are what the
next agent actually acts on. Nobody re-reads the originating report by default, so ordinary
summarisation-compression loss becomes the thing you act on.)*

A ticket ask carrying a **`(from watch report: <path>)`** provenance link is not the primary
source — it is a paraphrase of a paraphrase, and it drifts **measurably**. The provenance link
already exists in every such ask; it is simply not load-bearing. **Make it load-bearing: before
acting on a watch-derived ask, re-read the cited source section and verify the ask's load-bearing
claims — severity bands, premises, status, numbers — against it.**

The drift is not carelessness; it is the compression loss a summarisation hop always produces.
Measured in one batch (2026-08-03):

- **T-2470** called CVE-2026-59726 "high-severity"; NVD records CVSS 3.1 base **10.0 CRITICAL**
  (7.0–8.9 is HIGH, 9.0–10.0 is CRITICAL) — the ask understated a maximum-score CVE by a full band.
- **T-2482** said to "track final category rankings once Phase 5 ships" — encoding a premise that
  nothing is published yet. The OWASP MCP Top 10 has had all ten categories published since Phase 3
  beta; Phase 5 is a refinement. Acting on the ask as written would have parked a doable assessment
  until October.

The check is a single re-read of a section that is *already cited* — cheap, and it is the step
that turns the provenance link from decoration into a defence. This is the ticket-side sibling of
plan-first-dispatch's §Evidence map (T-2528): map the load-bearing claim back to its source before
you build on it.

## Cross-references

- `adr/ORG-019-blocked-state-retirement.md` — frontmatter blocks, no `blocked/` dir
- ORG-013 (cascade-unblock) · ORG-015 (ratifiable_files) · T-0465 (intent) · T-0717 (one-active-ticket mutex)
- `rulebook/directives.md` D9 (orchestrator response contract + ticket discipline)

## Authoring discipline

Hand-curated per D2. HO drafts; ratified by the operator. Updates ride alongside the lifecycle lessons that surface them.
