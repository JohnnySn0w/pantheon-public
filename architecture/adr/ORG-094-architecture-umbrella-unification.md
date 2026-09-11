---
aliases: [ORG-094]
id: ORG-094
title: "Architecture umbrella — unify adr + rfc + engineering-practices + atlas under architecture/ (reversing the ADRs-stay-in-adr rule)"
status: Draft 2026-09-06 by Orchestrator (HO). Direction ratified by the operator via Telegram 9161 ("A") + 9164 ("semantically, /architecture would/should contain things like adrs/rfcs/methodology") + 9168 ("engineering-practices works") + 9166 ("the atlas can stay as well, it seems relevant"). Pending ratification against this ADR body (bundle includes the umbrella ADR + the physical moves + ORG-092/ORG-093 body edits + H/I/J extraction + tooling updates).
date: 2026-09-06
author: Orchestrator (HO)
appetite: 5
build_disposition: partly-built
scoping_ticket: T-3472
supersedes: []
extends: [ORG-053, ORG-092, ORG-093]
relates: [ORG-053, ORG-092, ORG-093, T-3246, T-3471, T-3472]
labels: [doc-taxonomy, akm, architecture-umbrella, standards-tier, ratification-tier]
ratifiable_files: [pantheon-config:architecture/adr/ORG-094-architecture-umbrella-unification.md, pantheon-config:architecture/README.md, pantheon-config:architecture/adr/ORG-092-rfc-tier-design-specification.md, pantheon-config:architecture/adr/ORG-093-methodology-tier.md, pantheon-config:architecture/engineering-practices/README.md, pantheon-config:architecture/engineering-practices/TEMPLATE.md]
---

# ORG-094 — Architecture umbrella: unify normative + descriptive under `architecture/`

**Status:** Draft 2026-09-06 (Orchestrator, HO) — pending the operator ratify against this ADR body. **Date:** 2026-09-06 **Author:** Orchestrator (HO) **HITL:** Ratify-required. **Related:** [[ORG-053]] (SSEED Grand Project north star — the object atlas indexes), [[ORG-092]] (RFC tier), [[ORG-093]] (engineering-practices tier — landed as "methodology tier," renamed by this arc), [[T-3246]] (original architecture/ + atlas landing), [[T-3472]] (this arc).

**Appetite:** 5/10

## Context

Two prior ADRs established the standards-tier vocabulary:

- **[[ORG-092]]** (2026-09-06) established `rfc/` — design specifications — as an operator-ratified surface sibling to `adr/`. Rejected a shared `architecture/{adr, rfc}/` umbrella at that time citing tooling cost + adr-tools convention.
- **[[ORG-093]]** (2026-09-06) established `notes/methodology/` — HO-adopted evolving practice — as the third standards-tier class. Explicitly rejected a top-level `methodology/` dir in DEC-2, keeping the subdir-under-notes/ shape.

Both ADRs settled on repo-root-siblings + `notes/methodology/`. A third dir was concurrently active: **`architecture/`** (landed by T-3246, extended by T-3251) — the D2-protected Grand Project architectural atlas surface, holding `atlas/` + a `README.md` that explicitly stated *"ADRs stay in `adr/`. `architecture/` describes and indexes; it does not decide."*

The trigger for this ADR: when ORG-093 landed and the three-tier stack (adr + rfc + methodology) became visible, the operator observed (TG 9156, 9164) that (a) `notes/methodology` reads as awkward, and (b) semantically `architecture/` should contain the normative surfaces — adrs, rfcs, and engineering practices — alongside the descriptive atlas. This inverts both the ORG-092 rejected-umbrella verdict AND the pre-existing `architecture/` README rule.

## Corpus grounding

Industry precedent for **one architectural umbrella carrying both normative and descriptive surfaces**:

- **arc42** (arc42.org) — a widely-used architecture documentation template that puts decisions, constraints, quality goals, and cross-cutting concepts under one architecture umbrella, distinguished by section not by physical separation.
- **Google's engineering-practices** (github.com/google/eng-practices) — the eng-practices umbrella carries multiple practice surfaces (code review, code health, etc.) under one top-level name.
- **ThoughtWorks Tech Radar** — one radar surface, four rings (adopt/trial/assess/hold), all under one umbrella.
- **Bass/Clements/Kazman *Software Architecture in Practice*** — treats decisions (ADRs), views (atlas), and practices as first-class artifacts under the architecture heading, not as separate top-level concerns.

The prior ORG-092 rejected-umbrella verdict was scoped to a 2-tier world (adr + rfc). With 3 tiers landing (adr + rfc + engineering-practices) and a 4th descriptive surface (atlas) already present, the cohesion signal + reduced-top-level-noise case flips: umbrella > siblings.

### Projections, not a pipeline (SCEpter slide 16 + web-receptor lens)

An umbrella that co-locates decisions + specs + practice + description invites a workflow reading: *"draft an ADR first, then the RFC, then the engineering-practice, then dispatch."* That reading is the exact shape the corpus warns against. Two sources checked on 2026-09-06 (per TG 9176–9178):

- **SCEpter / Claim-Driven Development slide 15 vs 16** (Way / wayspurrchen, 2026 — `knowledge/episodic/2026-09-06-scepter-claim-driven-development/`). Slide 15 proposes a Requirements → Specification → Architecture → Detailed Design → Code pipeline where *direction = derivation*. Slide 16 immediately rejects it: "real projects refused; the flow ran every direction — Code→R (implementation discovers), A-leads (aesthetic first), S-leads (contract first)." The lesson lands at *"nothing is the master copy — requirements, specification, architecture, design, code are peer expressions of the same understanding"*, echoing Parnas & Clements 1986 (*"A Rational Design Process: How and Why to Fake It"*).
- **"Shaping Systems with Desires and Agents"** (web receptor, 2026 — `notes/2026-09-06-web-receptor-shaping-systems-with-desires-and-agents.md`) adds a state axis: projects move from **plastic** (infolding — details discovered in implementation reshape desires) to **rigid** (infilling — optimize predefined structure). Different project states permit different flow shapes; there is no fixed order across projects or across the life of one project.

**Together these fix the umbrella's frame:** the four subdirs are **peer registers keyed by kind of statement** (decision / specification / practice / description), NOT stages of any workflow. Direction of information flow between them is per-arc: ORG-091 was ADR-only (no RFC); RFC-001 preceded its ratified ADR; engineering-practice pins are adopted without any ADR predecessor. That's healthy projection behavior. The failure mode this ADR is deliberately NOT ratifying is *"must pass through adr → rfc → engineering-practice → dispatch"* as a required flow — that would recreate slide-15's rigidity through convention. The reading order in `architecture/README.md` is scoped to newcomer orientation only, never to arc execution order.

## Decision

**Establish `architecture/` as the single normative-plus-descriptive standards-tier surface**, unifying four subdirs:

```
architecture/
├── README.md              — the taxonomy index (this ADR is the ratifiable body)
├── adr/                   — Architecture Decision Records (moved from repo-root adr/)
├── rfc/                   — RFCs / design specifications (moved from repo-root rfc/)
├── engineering-practices/ — HO-adopted evolving practice (moved from notes/methodology/,
│                            renamed from "methodology")
└── atlas/                 — D2-protected whole-system architectural atlas (in place)
```

### Reversing the "ADRs stay in `adr/`" rule from T-3246

The pre-existing `architecture/README.md` (landed via T-3246) declared "ADRs stay in `adr/`. `architecture/` describes and indexes; it does not decide." That rule was correct at its landing time — atlas was the only surface intended under `architecture/`, so the boundary "descriptive-only here, normative elsewhere" was accurate. With ORG-092/ORG-093 landing normative surfaces (rfc/ + engineering-practices/) that share the same architectural register as atlas, that boundary is now the wrong one. This ADR reverses it: `architecture/` carries both.

The re-adopt is at the same tier (D2 / HO-authored) as the original rule; per D2 + D7, HO reversing HO's own prior editorial choice is in-tier, not an operator-scope decision. operator-ratify applies to THIS ADR (which names the new taxonomy) rather than to the reversal per se.

### Renaming "methodology" → "engineering-practices"

ORG-093 landed the third tier as "methodology tier," folder `notes/methodology/`. During the T-3472 execution, the operator proposed "engineering-practices" as the folder name (TG 9168, "engineering-practices works"). Adopted:

- **Folder rename:** `notes/methodology/` → `architecture/engineering-practices/`.
- **Tier concept rename:** "methodology tier" → "engineering practices tier" (with existing D3/msg-3733 "methodology is HO-adopted" phrasing preserved as internal register).
- ORG-093 body updated in the same commit; title + §Location + DEC-2 rejected-alternative all reflect the new naming.

Rationale for the name: `engineering-practices` matches industry-recognized register (Google's eng-practices, ThoughtWorks Tech Radar); reads coherently in the three-way lineup `adr / rfc / engineering-practices`; better distinguishes *practice discipline* from the more formal *methodology* register.

### Physical execution

- `git mv adr/ architecture/adr/` — 93 files.
- `git mv rfc/ architecture/rfc/` — 3 files (README + TEMPLATE + RFC-001).
- `git mv notes/methodology/ architecture/engineering-practices/` — 2 files (README + TEMPLATE), which then gets 3 new pins from H/I/J extraction.
- `architecture/README.md` rewritten to name all four subdirs + reverse the T-3246 rule.
- `architecture/adr/ORG-092-*.md` §Location + §Decision updated (rfc/ → architecture/rfc/).
- `architecture/adr/ORG-093-*.md` §Location + §Decision + DEC-2 rewrite (methodology → engineering-practices; notes/methodology/ → architecture/engineering-practices/).
- Amendments H, I, J extract from `sop/standard/testing-depth-and-scope.md` into `architecture/engineering-practices/` pins verbatim; SOP-body sections retire to one-line pointers.
- Tooling audit (~30-40 files): `tools/policy_outflow_sync.py` glob (load-bearing); test fixture path updates; hygiene comments in adr_scoping_lint / decision_recall_poc / knowledge_sweep / spine_index_groom / watch_runner.
- CI: `.github/workflows/adr-scoping-lint.yml` path pattern.
- Configs: `instance.yaml`, `adr-namespaces.yaml`.
- Living-doc cross-links: `sop/standard/README.md`, `CLAUDE.md`, `notes/recent-policy-changes.md`.
- Historical audit chain (`tickets/done/`, `ledger/`, `knowledge/watch/reports/`, `knowledge/episodic/`, `archive/`) NOT rewritten (P4 — the historical link "was correct at close time; git log --follow resolves to the new location" is the correct read).

## Consequences

**Immediate:**

- Single architectural umbrella covering normative + descriptive surfaces; reduced top-level dir noise (from 3 standards-tier siblings + `notes/methodology/` to 1 architecture/).
- `notes/methodology/` awkwardness retired.
- ORG-092/ORG-093 body location decisions re-ratified in the same arc (bundle-ratify).
- Amendment quagmire on testing-depth-and-scope.md structurally resolved: H/I/J now live at `architecture/engineering-practices/` as first-class practice pins.

**Downstream:**

- Future ADRs/RFCs/practices/atlas pages all land under `architecture/`.
- Cross-references to older `adr/…` paths in historical audit chain (tickets/done, ledger/, watch reports) remain broken-but-resolvable-via-git-log-follow. Not touched, preserving audit chain.
- Any external references to Pantheon's `adr/README.md` etc. (if any) will need a one-time redirect notice. Internal fleet has none outside this repo.

## Rejected alternatives

- **Different umbrella name** (e.g., `standards/`) — rejected. `architecture/` already exists with the semantic register; renaming to `standards/` would either move the existing atlas surface unnecessarily or fragment the umbrella. Better to expand `architecture/` (as the operator reasoned in TG 9164) than to introduce a competing umbrella.
- **Keep `architecture/` atlas-only, rename existing atlas surface out of `architecture/`** (Option C' from the T-3471/T-3472 exchange) — rejected. Would move active T-3251 arc paths unnecessarily; atlas belongs under architecture/ semantically per its own T-3246 framing.
- **Umbrella as index-only, physical dirs stay at repo root** — rejected. Would provide discovery cohesion but leave the physical repo showing 3 dirs at root — doesn't address the "notes/methodology awkward" observation and doesn't reduce top-level noise.

## Prior art

- **[[ORG-053]]** — SSEED Grand Project north star. Names the whole-system architectural surface this umbrella houses.
- **[[T-3246]]** — original `architecture/` + `architecture/atlas/` landing. This ADR reverses its "ADRs stay in adr/" rule per the two-touch discipline.
- **[[ORG-092]]** + **[[ORG-093]]** — the two tier-establishment ADRs whose §Location decisions this ADR re-ratifies.

## Unresolved

- **When does `architecture/atlas/`'s `T-3251` series land its remaining views?** Independent of this arc; `atlas/` path is stable throughout.
- **Should `sop/` graduate under `architecture/` too (as an operational-tier surface)?** Not for this arc; `sop/` is procedural, not architectural. Left as a separate future consideration if the taxonomy pressure ever surfaces.
