# rulebook/

The goal-anchored rule framework: goals + principles + directives + policies. ORG-002 introduced the principles/directives split; ORG-010 added the policies tier and gathered them under `rulebook/`; the goals registry landed 2026-06-11 (msg-3733 posture) and ORG-034 (2026-06-12) made goals the head of the justification chain via arc-intents.

## What lives here

| File / Folder | Tier | Owns | Amendment authority |
|---|---|---|---|
| `goals.md` | Tier 0 (north stars) | What the org is FOR — terminal goals, few, slow-moving. Layer-2 arc-intents live on work artifacts, not here (ORG-034). | North-star lines: the operator's verbatim. Structure/phase lines: HO-maintained operational fact. |
| `principles.md` | Tier 1 (durable) | Org-level identity rules — what we *are*; rare amendment. | Amendment via ADR; post-msg-3733: Adopted under justification, the operator digested (goal-level changes stay his). |
| `directives.md` | Tier 2 (scoped) | Project-calibrated rules — what we *do*; amend when scope shifts (D-N entries dated + scoped). | Post-msg-3733: HO adopts amendments under P/D justification with digest; pre-3733 entries were operator-ratified. |
| `policies/` | Tier 3 (operational) | Operational calibrations of directives — concrete enforceable ops-tier rules. | HO-authored under D2; Adopted-with-digest per msg-3733. |

## Cross-tier discipline

- **Intent → methodology** is the chain (ORG-034): work cites the arc-intent it serves (nested under a north star); P/Ds are cited as the how. Within methodology, **principles → directives → policies** still holds: a policy must `justifies via` the principles + directives it operationalizes; a directive must reference principles; principles stand alone. Goal-neutral maintenance is a legitimate, honest intent-cite for hygiene work.
- **Don't blur tiers.** A new operational rule = policy file under `policies/`, not a directive amendment. A new project-calibrated rule with multi-month scope = directive entry, not a policy. Org-identity changes = principle amendment, rare.
- **Rare ≠ never.** When a tier doesn't fit the new shape, the right move is to surface to the operator, not stretch a misaligned tier.

## How to add

| You want to add… | Where it lives | Authoring |
|---|---|---|
| A new durable rule defining what Pantheon *is* | `principles.md` (new `P<N>` entry) | Drafted; the operator ratification required. |
| A new project-calibrated rule with scope/date | `directives.md` (new `D<N>` entry) | Drafted; the operator ratification required. |
| A new operational calibration of an existing directive | `policies/<name>.md` (new file per policy) | HO drafts under D2; surface for ratification when ready. |

## Per-policy file shape

See `adr/ORG-010-three-tier-policies-extension.md` §Implementation §2 for the canonical front-matter + section template. Copy-shape:

```
**Status:** Drafted / Ratified <date> / Superseded by <ID>
**Author:** Orchestrator (Orchestrator) [or attribution]
**Date:** YYYY-MM-DD
**Re-evaluate when:** <substrate / event / date trigger>
**Justifies via:** <principles + directives this policy operationalizes>
```

Sections: Context → Policy → Boundary → Re-evaluate when → Related.

## Founding policies (migrated from `notes/` per T-0191)

- `policies/autonomy.md` — inbox auto-consumer gating + activity-digest cadence.
- `policies/trust-posture.md` — HO direct-action calibration of D3.

## Auto-index

<!-- AUTOGEN-START spine-index -->
| [`directives.md`](directives.md) | **Status:** **Ratified 2026-05-26 by the operator.** Established 2026-04-28 by ORG-002. **Origin:** First three directives are demotions from the original P1/P7/P9 (the "operationally true now, obviously change later" subset of the initial 9-principle set). |
| [`goals.md`](goals.md) | **Status:** Drafted 2026-06-11 by Orchestrator (HO) per the operator msg 3737/3738; **the three core north stars are the operator's verbatim words (msg 3740, 2026-06-11)** — canonical, not draft. Remaining entries carry DRAFT north stars until he blesses or rewrites them. Phase lines and deferred-items are HO-maintained operational state. |
| [`principles.md`](principles.md) | **Status:** Ratified 2026-04-28 by the operator (Telegram). Restructured 2026-04-28 per ORG-002 (two-tier framework: Principles + Directives). **Origin:** Drafted in Telegram 2026-04-26 / 2026-04-27. Initial 9-principle ratification 2026-04-28 (`ledger 2026-04-28T00:40:20Z-001`). Restructured the same day after research grounding (`ledger 2026-04-28T14:08:00Z-001`). |
<!-- AUTOGEN-END spine-index -->

## Related

- `adr/ORG-002-principles-directives-split.md` — the original two-tier split.
- `adr/ORG-010-three-tier-policies-extension.md` — the policies-tier extension + migration ratification.
- T-0173 (parent ticket: ORG-010 + rulebook authoring) — T-0191 (this migration).
