---
aliases: [ORG-033]
title: Multi-instance architecture — Pantheon's three-Claude-Code-instance discipline (consolidating)
status: Ratified 2026-06-10 by the operator (Telegram 3100)
date: 2026-06-10
---
# ORG-033 — Multi-instance architecture (consolidating ADR)

**Status:** **Ratified 2026-06-10 by the operator (Telegram 3100).** Authored 2026-06-10 by Recovery (`claude-fallback`) per T-0827 (greenlight PR-5 from the T-0780 ORG-012 sweep); revised to a cross-reference map per the operator's D10 steer (Telegram 3098). This ADR is a **single architecture map** that **cross-references** the already-ratified discipline scattered across P1/P3/P5, D1/D9, and ~6 ADRs — it introduces **no new rule** and (per D10) does **not restate** the source rules, only points at them. Its own content is the architecture facts + the verification recipe + alternatives.

**Author:** Recovery (recovery, `claude-fallback`).

**HITL:** Autonomous

**Related:** P1/P3/P5 (`rulebook/principles.md`); D1/D9 (`rulebook/directives.md`); ORG-001 (ledger per-agent `agent` field), ORG-002 (principles/directives split), ORG-010 (three-tier policies), ORG-011 (stays-working-under-silence), ORG-012 (inflow bubble-up), ORG-013 (relational triggers / cascade-unblock); source note `notes/2026-05-18-multi-instance-discipline.md` (T-0417 Shape C, ratified 2026-05-18); T-0780 (ORG-012 sweep), T-0827 (this filing).

## Context

Pantheon deliberately runs **three independent Claude Code instances** under separate Unix accounts on a single VPS, collectively forming one autonomous-agent system. The discipline that governs this design is real and ratified — but it lives **as record, scattered** across principles, directives, and ~6 ADRs. The source note (`notes/2026-05-18-multi-instance-discipline.md` §"Cross-links") states the gap explicitly: *"there is no single dedicated 'ORG-NNN multi-instance' ADR. The discipline is captured across the artifacts above. A consolidating ADR would be useful future work."* This ADR is that consolidation — presenting the discipline **as architecture, not as record**.

**Why an ADR and not an SOP/note:** the multi-instance design is an architectural decision among alternatives (three isolated accounts vs. one instance with internal role-switching vs. separate hosts) with named tradeoffs (OS-level UID isolation over network isolation; per-account git identity over a shared author; recovery as a distinct account over in-process self-repair). Architectural-decision-among-alternatives-with-tradeoffs is ADR-shape, not SOP-shape (per ORG-012 §Promotion #5, the same test that converted ORG-013 from a draft SOP).

## Decision

Record the three-instance architecture as a single consolidating ADR with the following load-bearing structure.

### The three instances

| Identity | Unix account | Role | Write-scope (structural) |
|---|---|---|---|
| **Orchestrator (HO)** | `claude` | Orchestrator: top-level dispatch, ticket lifecycle, ledger curation, the operator's Telegram interface, project-orchestrator launching | `pantheon-config/` (full), project repos as needed, `ledger/` as audit author |
| **Specialist (specialist)** | `claude-research` | Watch loops, research dispatches, B11 actuation (auto-file findings), daily digest, research memory authorship | `knowledge/watch/reports/<date>/` (auto-commit), `tickets/inbox/` (B11), own ledger entries (`agent: specialist`) |
| **Recovery (recovery)** | `claude-fallback` | Ticket grooming, blocked-ticket reassessment, session-reset auto-resume, repair-sudo path, recovery of a wedged HO | `tickets/` (grooming retargets/blocks), own ledger entries (`agent: recovery`/`claude-fallback`), recovery sudo into `claude` |

*(Roles reflect the architecture as of authoring; the ratified content is the three-account isolation + per-account authorship + the governing-rule map, not the exact task list, which evolves. Note the source-note's task lists predate later changes — e.g. Specialist now self-consumes its own ticket queue per ORG-025; the architecture facts + cross-reference map below are what this ADR records, not the task enumeration.)*

### The architecture this ADR records (ORG-033's own, not stated elsewhere)

These are the architecture facts unique to this record — the operational shape no principle/directive states:

- **Three Unix accounts on one VPS, not three hosts.** `claude` (Orchestrator/HO), `claude-research` (Specialist), `claude-fallback` (Recovery). Per-agent isolation is **UID separation** — separate `~/.claude/`, separate Telegram bots, separate systemd-user service stacks, cross-account reads via tightly-scoped sudoers. (Corrects the recurring "Specialist is on a separate host" misread — same host, separate accounts.)
- **Recovery is a *distinct account*, by design.** Recovery exists as `claude-fallback` precisely so a wedged/capped/self-broken Orchestrator can be repaired from *outside its own process and account* — in-process self-repair can't run when the instance is the thing that's down. (This is the architectural *application* of P1; the recovery-as-separate-account choice itself is what ORG-033 records.)

### Governing rules — cross-referenced, NOT restated (D10: references replace transclusion)

The discipline that makes the three-account architecture *correct* already lives in canonical principles/directives/ADRs. ORG-033 **points** at them rather than re-stating them (a second statement would drift). The architecture above instantiates:

| What | Canonical source (the rule lives there, not here) |
|---|---|
| Write-scope isolation — no instance authors another's domain, enforced by per-account fs-perms + git identity + sudoers | **P5** (roles don't hop) |
| Audit-chain authorship is mechanical + third-party-verifiable (distinct `agent`/`[actor]`/git-`Author` per write) | **P3 + P4**; **ORG-001** (ledger `agent` field), **ORG-003** (ticket `[actor]` token) |
| Bandwidth separation via three-tier dispatch (the operator ↔ Orchestrator ↔ orchestrators ↔ workers) | **D1** + **D7** |
| Structural-over-convention enforcement (isolation is permissions, not policy) | **P1** |
| Cross-domain action routes through the queue (lateral roles-don't-hop), recovery-domain excepted | **ORG-027** §Per-domain |

ORG-033's job is to be the **single map** that ties these together as one deliberate architecture — not to copy them. If any source rule changes, the map's *pointer* stays valid; there is no duplicated statement to drift.

### Verification (third-party-confirmable from a clone)

A reader cloning `github.com/example-org/pantheon-config` can independently confirm the discipline:
```
jq -s 'group_by(.agent) | map({agent: .[0].agent, count: length})' ledger/2026-W*/*.jsonl   # 3 authorship streams (weekly-sharded ledger, ORG-001)
grep -h "^- " tickets/done/*.md | grep -oE "\[[a-z-]+\]" | sort | uniq -c | sort -rn    # per-agent transitions
git log --format='%an' | sort | uniq -c | sort -rn                                       # per-agent commits
git log --oneline -- knowledge/watch/reports/ | head                                     # Specialist-side watch loop
```

## Alternatives considered

- **Single instance, internal role-switching.** Rejected: loses structural write-scope isolation (P5 becomes convention, not enforcement) and the recovery posture (a wedged single instance can't repair itself — see §The architecture this ADR records).
- **Separate hosts per instance.** Rejected as unnecessary: OS-level UID isolation on one VPS delivers the isolation invariants at far lower operational cost; network separation buys nothing the audit-chain + permission model doesn't already provide.
- **Leave the discipline scattered (status quo).** Rejected per T-0827 + the source note's own gap-flag: the discipline reads as record, not architecture; a consolidating ADR makes it discoverable as a single decision and gives later cross-references one canonical anchor.

## Consequences

- **Positive:** one canonical anchor for the multi-instance discipline; the scattered P/D/ADR references now resolve to a single architecture record; onboarding/audit of "why three accounts" is one Read, not a scavenger hunt.
- **Neutral/maintenance:** this ADR records architecture + pointers, not the evolving task lists — when roles shift (e.g. ORG-025 Specialist self-consumption), the task table is updated but the architecture facts + the cross-reference map hold. Keep the architecture/governing-rule map as the load-bearing content; treat the role table as illustrative. Because the governing rules are *pointers, not copies*, a source-rule change needs no edit here (D10).
- **Classifier-risk posture (carried from the source note):** this ADR is **present-as-architecture, not present-as-record** — discoverable to anyone who clones the repo, but NOT promoted (no public-claim/press-release framing). Per `knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md`, public-claim language about DIY-harness records carries asymmetric classifier-exposure risk. T-0417 Shape A+C is partially-ratified and explicitly does NOT include a public "record claim." If the operator later ratifies a public-claim posture, a `## Public claim` section can be added; until then the architecture stands on its own merits without advertisement.
