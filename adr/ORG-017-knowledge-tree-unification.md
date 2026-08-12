---
aliases: [ORG-017]
title: Unify `watch/` + `research/` under `knowledge/` (Option C)
status: Ratified 2026-05-17
date: 2026-05-17
build_disposition: built
---
# ORG-017 — Unify `watch/` + `research/` under `knowledge/` (Option C)

**Status:** Ratified 2026-05-17 by the operator — picked Option C explicitly in Telegram msg_1443 ("knowledge/episodic + knowledge/watch under single knowledge/ root"), framing T-0372's "investigate unification options" as a pre-narrowed decision rather than fresh A-vs-B-vs-C-vs-D. ADR documents the decision + migration plan for the durable record.

**Date authored:** 2026-05-17

**Author:** Orchestrator (Orchestrator) — drafted post-ratify per T-0372 ASK ("HO drafts; Specialist consults; the operator ratifies"). The operator narrowed the decision space ahead of ADR draft, so this ADR captures the ratified shape + migration plan rather than a fresh decision.

**Related:** [[T-0372]] (the unification ticket; resolves on this ADR landing + migration sub-ticket filing), [[T-0378]] (Specialist's prior multi-arc dispatch under the research/ tree; informs operational pattern), [[ORG-012]] (information-inflow methodology — Specialist + HO author research/watch material; this ADR doesn't change authorship, only filesystem topology), [[ORG-015]] (ratifiable-class allow-list — ADR is the right substrate for this layout decision), Specialist's `watch/topics.md` (the watch spine; path changes under migration).

**HITL:** Authored

## Context

Per the operator msg_1440 (2026-05-17): *"I think pantheon-config/watch and /research should be combined in some way. There's a conceptual overlap."*

Today's split:
- `research/<YYYY-MM-DD-topic>/` — episodic: multi-subagent commissioned arcs, ad-hoc dispatch, deep deliverables. 24 directories accumulated.
- `watch/reports/<YYYY-MM-DD>/<slug>.md` — continuous-S4: standing topics scanned per-cadence, shallow, single-topic per report. 16 date-bucket directories.

Both are research outputs from Specialist / researcher-tier subagents. Both live under "things we learned about the environment." Reader-side query ("what do we know about X?") today requires searching two trees independently. The conceptual overlap is real; the operational distinction (cadence-vs-arc semantics) is also real.

T-0372's ASK presented four shapes (A physical merge, B sibling-with-shared-index, C hierarchy under one root, D tag-based unification) for HO to investigate. The operator msg_1443 explicitly picked Option C ahead of HO's draft. This ADR captures C as the ratified topology + migration plan.

## Decision

### Topology — `knowledge/` umbrella with episodic + watch children

```
knowledge/
├── README.md                           # umbrella: cadence-vs-arc semantics
├── episodic/                           # was research/
│   ├── README.md                       # episodic-arc purpose + index
│   └── 2026-MM-DD-<topic>/             # per-arc directories
└── watch/                              # was watch/
    ├── README.md                       # watch-loop purpose
    ├── topics.md                       # the standing watchlist (the spine)
    └── reports/
        ├── README.md
        └── <YYYY-MM-DD>/<slug>.md      # per-date buckets, per-slug reports
```

The cadence-vs-arc distinction is preserved structurally (one child per flavor); the umbrella signals the shared conceptual role (env-knowledge artifacts). Reader query path becomes single-rooted (`knowledge/`); search by topic, then drill into episodic or watch as needed.

## Alternatives considered (kept for ADR record)

| Option | Shape | Why not (vs C) |
|---|---|---|
| **A** | Physical merge into one tree with episodic-vs-continuous flag (filename or frontmatter) | Muddies cadence-loop discipline: watch_runner.py + grooming logic depends on the per-date bucket convention; flag-based separation requires loop logic to filter, adds operational fragility. The operator's intuition (msg_1443) prioritized clean separation. |
| **B** | Sibling-with-shared-index (`research/by-topic/<topic>.md` cross-linking both trees) | Adds an index to maintain (grooming burden). Cadence-vs-arc semantics stay clean BUT the umbrella signal (the operator's pattern-match) doesn't land — readers still see "two trees with an extra index" not "one knowledge surface with two child shapes." Index without a parent is weaker than C's parent-with-children. |
| **C** | Hierarchy under `knowledge/` with `episodic/` + `watch/` children | **Ratified.** Single search root; structural cadence-vs-arc preservation; explicit umbrella signal in path layout itself. Migration churn one-time; survives subsequent grep audits cleanly. |
| **D** | Tag-based unification (frontmatter tags + tag-index tool) | Zero physical migration BUT requires frontmatter migration on all existing docs + a new tag-index tool to maintain + the umbrella signal lives in tooling not paths. Higher long-term maintenance; the structural-via-path-layout choice (C) is more durable. |

## Migration plan

### Atomic shape

The migration MUST be atomic for Specialist's `watch_runner.py` — it reads `watch/topics.md` on each cron fire; a half-migrated repo (paths moved but code unchanged) would fail. All filesystem renames + code-reference updates land in ONE commit. Specialist's claude-research user auto-pulls pantheon-config; the next cron fire after the merge sees the post-migration state cleanly.

### Renames (`git mv` preserves history)

```bash
git mv research/ knowledge/episodic/
git mv watch/    knowledge/watch/
```

### Code refs to update (forward-looking; live)

Confirmed via grep — ~260 references across the repo. Forward-looking refs (active code + docs) MUST be updated atomically with the rename. Audit covers (non-exhaustive):

- `tools/watch_runner.py` — primary watch-loop driver; reads `watch/topics.md` + writes `watch/reports/<date>/`.
- `tools/watch_digest.py` — end-of-day digest walks `watch/reports/<date>/`.
- `tools/_git_utils.py` — HERMES guard scope-assertion includes watch paths; narrow-scope regex needs update.
- `tools/context_fill_monitor.py` — may reference research/watch paths for self-monitoring (verify in implementer plan).
- `tools/test_watch_runner.py`, `tools/test_*` — fixtures + path constants.
- `tools/README.md` — index/explainer references.
- `tools/systemd/README.md`, `tools/systemd/claude-telegram-research.service` — operational references.
- `CLAUDE.md` (pantheon-config root + `/home/claude/CLAUDE.md`) — the auto-commit heads-up text currently says `watch/reports/<date>/`; update to `knowledge/watch/reports/<date>/`.
- `rulebook/directives.md`, `rulebook/policies/cost-monitoring-thresholds.md` — text references.
- `sop/standard/*.md` — multiple references in operational SOPs (auto-resume runbook, cross-account ops, etc.).
- `adr/README.md` — index references.
- Specialist's persona brief (lives in `claude-config/agents/researcher.md`) — write-scope discipline names `watch/reports/<date>/` as Specialist's narrow surface; needs update to `knowledge/watch/reports/<date>/`. **Cross-repo coordination.**

### Historical refs — DO NOT update (preserve audit chain)

Refs in the following classes are **historical** and stay as-written:
- `ledger/2026.jsonl` (and earlier ledger files) — append-only audit chain; ledger entries reference paths "as-of-when-written." Rewriting them would erode the audit chain's authenticity.
- `tickets/done/T-*.md` — closed tickets are immutable history. Path refs in resolved tickets are "as-of-closure."
- The CONTENT of moved reports themselves (e.g., the prose inside `research/2026-04-29-cybernetics-org-theory/notes.md` — soon to be `knowledge/episodic/2026-04-29-cybernetics-org-theory/notes.md` — that references `../watch/topics.md`). After the git mv, the relative-path link `../watch/topics.md` from `knowledge/episodic/<dir>/notes.md` resolves correctly to `knowledge/watch/topics.md` because the relative depth shifts identically for both source + target. Absolute path mentions in prose ("see watch/topics.md") become stale; leave as-is in historical documents (reader can resolve via git log if needed) UNLESS the document is also a live SOP / ADR / README where forward-looking update applies.

Rationale: historical immutability vs forward-looking accuracy is the standard ADR/ledger discipline (per ORG-001 ledger-format + ORG-003 ticket-system). This ADR explicitly inherits it for the migration.

### README inheritance

- `knowledge/README.md` (new) — umbrella explainer: cadence-vs-arc semantics, when to look in episodic/ vs watch/, cross-link to ORG-017.
- `knowledge/episodic/README.md` — short purpose-specific (currently at `research/README.md`); restructured to drop the cross-reference-to-watch verbatim now that umbrella explains it.
- `knowledge/watch/README.md` — short purpose-specific (currently at `watch/README.md`); same shape.
- `knowledge/watch/reports/README.md` — date-bucket index (was `watch/reports/README.md`); unchanged content except header.

### State file (out-of-repo)

`~/.local/state/pantheon/watch/state.json` lives outside the repo + is Specialist-owned. **Out of migration scope** unless implementer notices a code reference that hard-codes the state path — in which case keep the state-file path unchanged (it's not a "watch tree" semantic; it's the watch_runner's runtime state) but flag for HO decision if it surfaces as confusing.

### Cross-actor coordination

Specialist (claude-research user) auto-pulls pantheon-config on the inbox-consumer cadence. After the merge commit lands on origin/main, Specialist's next pull picks up both the rename + the code updates simultaneously. **No window where Specialist is on stale code.**

Specialist's persona brief lives in `claude-config/agents/researcher.md` — separate repo. The persona-brief update (write-scope path change from `watch/reports/<date>/` to `knowledge/watch/reports/<date>/`) lands in a coordinated PR on `claude-config`. Sequencing: pantheon-config migration commit lands first; claude-config persona update follows within the same dispatch window; Specialist's NEXT cron fire after both lands.

### Migration sub-ticket

This ADR captures the decision + plan. The implementation lands in a separate sub-ticket dispatched as a single-implementer plan-first cycle:
- Worktree pre-staged per T-0357 on `agent/<sub-ticket-id>` per T-0176 convention.
- Atomic single-commit migration; all path renames + code-ref updates in one PR.
- Reviewer verifies: smoke loop green (all `python3 -m unittest discover tools` tests pass); no stale `watch/` or `research/` refs in forward-looking files; historical refs preserved per the rule above.
- Followed by claude-config persona-brief PR.

Filed as **T-XXXX** post-this-ADR-landing.

## Consequences

**Positive:**
- Single reader query root (`knowledge/`); searching for "what do we know about X" lands in one place.
- Cadence-vs-arc semantics structurally preserved in path layout.
- Umbrella signal lives in the filesystem, not in tooling or convention prose — durable + grep-discoverable.
- Future additions (lessons-tier env-knowledge? agent-health? prior-art catalogs?) have a natural home under `knowledge/`.

**Negative:**
- One-time migration churn across ~260 references. Atomic single-commit migration mitigates operational risk but the PR is large by Pantheon standards.
- Historical-vs-forward-looking distinction must be applied case-by-case during the migration. Implementer needs the rule (this ADR §"Historical refs" subsection) in their brief.
- Cross-repo coordination (claude-config persona-brief in same dispatch window) — implementer brief must call this out.

**Deferred:**
- Whether to add a `knowledge/lessons/` child for env-lessons-tier observations: not in T-0372 scope, not addressed here. File a follow-up if a third child class becomes needed.
- Whether the umbrella naming evolves (`knowledge/` vs `env-knowledge/` vs `research/` retained as umbrella with `episodic/` + `watch/` children): the operator picked `knowledge/` in msg_1443; ratified as-is.

## DECISIONS

- `knowledge/` umbrella over `research/` retained-as-umbrella — justifies via the operator's msg_1443 explicit pick (`knowledge/episodic/` + `knowledge/watch/`); HO does not relitigate.
- Atomic single-commit migration (rename + code updates land together) — justifies via P1 (architectural over policy: structural atomicity prevents the half-migrated-state failure mode for Specialist's watch_runner) — alternative (rename first, code updates follow) would leave Specialist's cron broken between commits.
- Historical refs preserved (ledger + done tickets + moved-report content) vs forward-looking refs updated (live code + ADRs + SOPs + READMEs) — justifies via ORG-001 + ORG-003 (audit-chain immutability) — established discipline; this ADR explicitly inherits it for the migration scope.
- Migration filed as a separate sub-ticket rather than landed in this ADR's dispatch — justifies via P5 + D6 (roles don't hop; ADRs are decisions, implementer dispatches are code; separating keeps each piece clean) + P7 (cheap-action ADR-only dispatch lets the operator ratify the topology before implementer burns cycles on a large rename).
- Cross-repo coordination (claude-config persona-brief) named explicitly — justifies via D9 (orchestrator response contract; surface cross-actor coupling at design time, not implementer-discovery time).
- README inheritance (umbrella + child-purpose docs) — justifies via D10 (minimum-traversal: umbrella is one click; children disambiguate when needed; no redundant prose) — same shape as `sop/standard/README.md` vs `sop/remediation/README.md` precedent.

## Ratify trigger

Already ratified per the operator msg_1443. This ADR is the durable record of the decision + migration plan. No separate ratification ticket needed; T-0372 (the parent unification ticket) resolves on this ADR landing + migration sub-ticket filing.

If migration sub-ticket surfaces decisions not anticipated here (naming variant on the umbrella, additional child class needed, state-file path question), implementer escalates via DECISIONS section; HO triages.
