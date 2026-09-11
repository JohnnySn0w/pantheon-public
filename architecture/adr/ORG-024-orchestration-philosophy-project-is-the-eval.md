---
aliases: [ORG-024]
title: "ORG-024 — Orchestration philosophy: the project is the eval"
status: Ratified 2026-05-29
date: 2026-05-28
build_disposition: no-build-needed
---

# ORG-024 — Orchestration philosophy: the project is the eval

**Status:** **Ratified 2026-05-29 by the operator** (T-0657 resolve "Ratified", 2026-05-29T21:12:47Z). Authored in notes/ as a draft; moved to adr/ on ratification.

> **Renumber note (2026-05-29):** originally proposed as ORG-023, but that number was concurrently taken by `adr/ORG-023-watch-runner-kanban-port-decision.md` (authored 2026-05-29, indexed in adr/README). Per the operator's call, the watch_runner ADR keeps ORG-023; this proposal is renumbered to **ORG-024** (next free).

**Date authored:** 2026-05-28 (revised 2026-05-29 per the operator's T-0657 request-edits)

**Author:** Recovery — per the operator msg 2021 ("a holistic overview of the plan somewhere, adr worthy"), prompted by the 2026-05-28 external orchestration-systems dump (`notes/2026-05-28-orchestration-systems-external-dump.md`).

**Related:**
- ORG-002 (principles + directives split — this ADR documents the calibration shape they describe)
- ORG-011 (activity monitor / poke predicates — concrete instance of "feedback at multiple rates")
- ORG-019 (blocked-state retirement — concrete instance of "compaction is a diagnostic, the taxonomy was too coarse")
- ORG-020 (self-triggered restart for all Pantheon agents — concrete instance of "handoffs as mid-term memory")
- ORG-022 (ratifiable-pairing enforcement at file-tier — concrete instance of "filing-time gates beat post-hoc cleanup")

**HITL:** Ratified by the operator 2026-05-29 (Autonomous thereafter — gap tickets stay under the operator's discretion to schedule or park).

## Context

External dump received 2026-05-28 from an orchestration-systems practitioner
("The Project Is The Eval"; verbatim captured in
`notes/2026-05-28-orchestration-systems-external-dump.md`). Cross-comparing
its bullets against Pantheon's current shape surfaced a load-bearing
observation: **most of what the author prescribes, we already do — and we
arrived at it independently, ad-hoc, through repeated failures.** What we
DON'T have is the explicit umbrella thesis that ties our individual
mechanisms together.

This ADR ratifies the thesis as the framework our existing mechanisms
already operate under, and names the active gaps so they can be picked off
one at a time.

The reason this is ADR-tier (not SOP or lessons): every load-bearing
Pantheon mechanism (handoffs, tickets-as-contracts, ledger, hooks,
plan-first dispatch, worktrees, byte-sequence guards, agent-health
monitors, restart pipelines) is a feedback loop. We've been adding loops
reactively as failures surface. Naming the meta-pattern lets future
decisions evaluate against "does this make the eval-loop tighter or
looser?" instead of re-deriving from first principles each time.

## Decision

**Pantheon's orchestration philosophy: the project is the eval.**

The thesis (verbatim from the external dump, ratified as the framework name):

> All you can do is design the project's environment so that it evaluates
> itself at each step and self-optimises... the system has multiple feedback
> loops to keep it in check that develop as a byproduct of normal work.

**What this means in Pantheon-specific terms:**

1. **Every load-bearing mechanism is a feedback loop, deliberately.** Adding
   a new mechanism asks: what loop does this close? at what cadence?
   what's the structural witness it watches?

2. **Bottlenecks are movable; constraints aren't.** When a failure surfaces,
   the first triage question is "constraint or bottleneck?" Context window
   is a constraint (design within it); slow tests or stale docs are
   bottlenecks (fix structurally). The two get different responses.

3. **External state, fresh boot, structural witnesses.** Runtime state lives
   outside the agent — ledger, tickets, HANDOFF.md, ~/.local/state/pantheon.
   Each model invocation boots fresh from disk. The structural witness
   (mtime, refspec, frontmatter field) is the load-bearing signal, NOT the
   agent's recall.

4. **Filing-time gates beat post-hoc cleanup.** Validation at the moment of
   creation (ticket.py, HERMES byte-sequence guard, ratifiable-pairing
   enforcement, lint scope-asserts) compounds — every gate that fires
   structurally is a class of bug that can't enter the corpus.

5. **Compounding via hooks + lessons.** Recurring failure patterns get
   promoted to structural enforcement (hooks, pre-commit, lint rules) —
   not just lessons that future agents might re-read. The lesson is the
   first observation; the hook is the structural lock.

## What Pantheon already has aligned with the dump

The dump's bullets map cleanly to existing Pantheon mechanisms:

| Dump bullet | Pantheon mechanism |
|---|---|
| Handoffs for large tasks | T-0008 `tools/handoff_and_restart.sh` + HANDOFF.md contract |
| Runtime state external (boots fresh from disk) | ledger jsonl + tickets/ + HANDOFF.md + ~/.local/state/pantheon |
| Plans-as-contracts | Tickets (frontmatter + transition log + body); survive compaction |
| Phase + reviewer-first dispatch | Plan-first + reviewer-before-implementer per `feedback_reviewer_step_valuable_even_for_one_offs` |
| Hooks as smallest feedback loops | HERMES byte-sequence guard, SessionStart auto-read, scope/sudo guards |
| Worktrees for parallel non-dependent work | `tools/_git_utils.with_worktree` (ticket_lint, ticket_grooming, etc.) |
| Orchestrators at top + phase level | HO at top; implementer/reviewer subagents per phase |
| Filing-time gates | ratifiable-pairing (ORG-022), HERMES guard, ticket.py field validation, lint scope-asserts |
| Bug-bot → structural rules | ticket_lint, ticket_grooming, context_fill_monitor, activity_monitor — each a "watch + flag/auto-fix" loop |

## Active gaps (each filed as a follow-up ticket)

The dump surfaces eight gaps where Pantheon hasn't yet adopted the
prescribed shape (gaps 1-7 from the original dump; gap-8 added 2026-05-29
per the operator's T-0657 edits). Each has been filed as a follow-up ticket —
read this ADR, then drop into the named tickets for the per-gap detail:

1. **File/function size discipline** — **T-0646** (target=operator, noodle).
   ~500 LoC/file, ~50-75 LoC/function as soft norms. Current state:
   routinely violated (inbox_consumer.py ~2300, ticket.py ~1815,
   context_fill_monitor.py ~1100). Worth deciding whether to adopt as a
   soft norm + apply selectively in next refactor.

   **Canonical offender — the decision ledger.** `ledger/2026.jsonl` is a
   single ~990KB monolithic file (GitHub renders it poorly — the exact
   failure ORG-001 §Format and D10 were written to prevent). This was
   already ticketed AND greenlit as **T-0148** ("Ledger weekly-shard +
   INDEX consolidation per D10", 2026-05-09) with a full 4-scope plan
   (per-ISO-week shards + index consolidation + tool updates + ORG-001
   amendment). But T-0148 was mis-closed: it sits in `done/` while its
   "resolved" log line (stamped 2s after claim) is only the dispatch
   greenlight — no migration script ever existed in git history, no shard
   was ever committed, and both `INDEX.md` and `2026-INDEX.md` still
   coexist. Per the operator (2026-05-29), T-0148 is re-opened (done → inbox)
   as the concrete work item for this gap; ledger-segmentation is the
   highest-value instance of the size norm, not a separate retention
   question (see the "Less is always more" note below — segmenting ≠
   pruning; no entries are lost).

2. **Pre-mortem + round-up before plan-first dispatch** — **T-0647**
   (target=recovery, action). Today we dispatch reviewer subagent on plans
   but don't systematically (a) run pre-mortem on a fresh design, (b)
   "round up" library/external changes since the model's training
   cutoff. Extension to the existing plan-first ceremony.

3. **Test-tier ladder (existence → property → behavioural → adversarial)**
   — **T-0648** (target=recovery, action). We mostly write behavioural +
   some adversarial. The ladder codified would catch missed dimensions;
   can become a reviewer-checklist item.

4. **Bug-bot → linter → pre-commit compounding pipeline** — **T-0649**
   (target=operator, noodle). Today we file lessons + feedback memories
   (soft enforcement) but don't systematically promote findings into
   committed hooks. ticket_lint, HERMES guard, ratifiable-pairing are
   the closest examples — generalize, or keep deliberate soft-enforcement?

5. **Restart-clustering-as-diagnostic** — **T-0650** (target=recovery, action).
   Pantheon does NOT use in-session compaction — we restart-with-handoff
   (T-0008) at the 60% context-fill threshold instead. The author's
   "compaction is a diagnostic" framing translates: when restarts cluster
   (N restarts in M time), treat as a scope-overshoot signal, not just
   recovery. Adjacent research on whether to ADOPT compaction as a
   complement filed separately as **T-0656** (target=specialist).

6. **Per-file intent-injection hook** — **T-0651** (target=operator, noodle).
   Hook fires before edit on files with non-obvious invariants
   (HANDOFF.md, handoff_and_restart.sh, restart-pipeline cluster) —
   injects the intent/why before the agent edits.

7. **6-question per-phase checklist** — **T-0652** (target=recovery, action).
   "What do I want / ways to achieve / what slows me / prevent recurrence
   / stay on track / 2nd-3rd order effects." Prompt-prepend on
   implementer/reviewer dispatch.

8. **Portable memory store + ripgrep (off MEMORY.md + grep)** — **T-0692**
   (target=recovery, action; added 2026-05-29 per the operator's T-0657 edits).
   Current state is a default we backed into, not a choice: MEMORY.md is
   not portable, and `grep` is orders of magnitude weaker than `ripgrep`.
   Baseline (grep → rg) is independently safe; the portable-store design is
   the open piece. See "Current state we are NOT happy with" below — this
   gap is the prerequisite to any heavier BM25/embeddings layer, which
   stays a deliberate "not yet."

## Adjacent: noodle/action lifecycle (filed 2026-05-28 from msg 2024)

Surfaced in parallel by the operator flagging four improperly-filed tickets
(T-0554, T-0563, T-0618, T-0619) where the PWA shows "needs
classification" despite proper intent frontmatter. The structural fix
(noodle/action close lifecycle + PWA recognition + lint) is a sibling
bundle to the ORG-024 gaps:

- **T-0653 (recovery, action):** `ticket.py close-noodle` + `close-action`
  CLI subcommands; close-action auto-spawns review ticket
- **T-0654 (recovery, action, depends T-0653):** PWA noodle/action close
  buttons + drop the false needs-classification warning
- **T-0655 (recovery, action, depends T-0653):** ticket_lint extension
  to flag malformed intent combinations at file-tier

This bundle is the filing-time gate that ORG-024's principle #4
("filing-time gates beat post-hoc cleanup") prescribes — without it,
the noodle/action vocabulary T-0465 added is invisible to the consumer
PWA + the lint, so authors who use it correctly still get false
warnings.

## What we explicitly do NOT adopt

One area where the dump's prescription diverges from a Pantheon *deliberate*
choice:

- **"Less is always more" memory pruning (as a retention rule).** Our
  MEMORY.md, ledger, and ticket queue all accumulate. We have deliberate
  retention (memory pins earn their place across multiple incidents; ledger
  is append-only; resolved tickets stay in done/). We re-evaluate per the
  SOP/lessons/notes retention policies, not by a hard "less is more" rule.
  Periodic prune sweeps (T-0644-shape) keep the active queue manageable
  without forcing retention loss. **Important distinction:** *segmenting*
  the decision ledger into smaller files (gap-1 / T-0148 above) is NOT
  pruning — it's the size-discipline norm applied to an append-only store,
  with zero retention loss. We want the segmentation; we decline the
  retention-loss rule. The two were previously conflated.

## Current state we are NOT happy with (migration-worthy, not a deliberate choice)

The dump prescribes a BM25/embeddings memory tool. Our current posture here
is a *default we backed into*, not a deliberate divergence — and the operator has
flagged it as something he'd prefer we move off of:

- **File-based MEMORY.md + `grep` is what we use now, not what we chose.**
  MEMORY.md is **not portable**, and `grep` is inferior by orders of
  magnitude to `ripgrep`. The near-term migration (portable memory store +
  `rg` over `grep`) is filed as gap-8 below; it is independent of, and a
  prerequisite-of-sorts to, any heavier retrieval layer.

- **Full BM25 + embeddings retrieval *is* a deliberate "not yet."** Once the
  baseline above is on a portable store + ripgrep, adding a BM25/embeddings
  layer is a cost/value call we make when ripgrep stops scaling — not
  before. Adopting the heavyweight layer too early adds tooling cost without
  proportional value; that judgment is unchanged.

## Forces

**For ratification:**
- The thesis is already implicitly how Pantheon operates; ratification
  makes it referenceable in future ADRs ("this widens the eval loop", "this
  is a filing-time gate per ORG-024").
- Gives a coherent answer to "why does Pantheon have 50+ tools?" — each
  is a feedback loop that closed a recurring failure.
- Sets the criterion for new orchestration mechanisms: does this make
  the eval-loop tighter or looser?

**Against ratification:**
- Adopting external framing risks anchoring on someone else's vocabulary
  when our own is already serviceable.
- Some gaps (file-size discipline, BM25) may not be net-positive for
  Pantheon's specific shape; ratifying the framework doesn't commit us
  to its full prescription, but does set the direction.

**Open questions for the operator:**
- Which gaps (1-8 above) are worth scheduling vs. parking?
- Is the meta-thesis worth the umbrella ADR, or is a notes/-tier
  consolidation enough?
- Should the "explicitly do not adopt" section grow into its own
  policy doc that future external-frame proposals get compared against?

## Boundary

**This ADR does NOT:**
- Commit Pantheon to adopting every dump bullet — it ratifies the
  framework, not the line items
- Replace existing ADRs (ORG-002 principles/directives, ORG-019 blocked-
  state retirement, etc.) — they remain load-bearing; this provides the
  umbrella context they fit under
- Auto-promote the gap-tickets to scheduled work — each gap stays under
  the operator's discretion to schedule or park

**This ADR DOES:**
- Name the meta-pattern Pantheon already operates under
- Surface the gap list with structural reasoning for each
- Give future orchestration decisions a referenceable criterion ("does
  this tighten the eval loop?")

## Re-evaluate when

- A new gap surfaces from operational experience (similar to how T-0644
  surfaced the P1.a unblocked-count gap)
- An external framing materially contradicts the thesis (we'd need to
  argue against the meta-pattern, not just the specifics)
- Pantheon's scale changes substantially (the "less is always more"
  retention call would re-open; BM25 adoption threshold would re-open)
