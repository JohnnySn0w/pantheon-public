# SOP — Codex planning-phase spec review (ORG-041 wiring)

**Status:** Adopted 2026-07-12 (HO, below-goal per msg-3733 principles-as-ratification; prompted by the operator TG 5757 after the T-1633 arc shipped without this step and a post-hoc review found a BLOCKS-PILOT defect).
**Backing ADR:** `architecture/adr/ORG-041-codex-planning-phase-spec-review.md` (ratified TG 5624) — the ADR names the capability; this SOP wires it into the dispatch flow.

## When it fires (dispatcher checklist item)

Before approving Unit-1 implementation of any arc that is ANY of:
- multi-unit / project-orchestrator tier,
- introduces a new long-running component (daemon, service, bridge, watchdog),
- implements against a third-party protocol or API surface (D11 territory),
- touches cross-account or supervised-first-fire infrastructure,

the dispatcher (HO for PO arcs; PO for its own unit plans when HO hasn't already run it) MUST run `codex_second_opinion` over the arc plan + key design facts.

Single bounded tickets (one file, no new component, no third-party surface) are exempt — judgment per D3, but note the exemption in the dispatch DECISIONS if the arc is borderline.

## How to run it

- Tool: `mcp__pantheon-mcp-codex__codex_second_opinion`, model per ORG-041's ratified review slug (Sol: `gpt-5.6-sol` since 2026-10-14, T-5007/T-5008; the interim `gpt-5.5` was retired upstream).
- Brief shape (learned T-1689): **terse, numbered, falsifiable questions** — wire shapes, ordering hazards, rollback/state gaps, OS/systemd semantics. Ask for DEFECT/OK per item + severity (BLOCKS / FIX-SOON / NOTE). Long discursive briefs time out (~120s wrapper limit); two focused questions beat ten broad ones. Pass the primary artifact via `context_path` (file, not dir).
- **Critiques anchor to a named layer of concern** (added 2026-08-26, HO-drafted below-goal per the operator TG 8590; established through the T-3272/T-3273 germination-engine ADR arc where v8-v9 review rounds surfaced kernel/POSIX-tier and systemd-substrate-tier provider-facts that ADR-prose iteration cannot mechanically discover-in-advance even after explicit scoping each round). The dispatch brief MUST name a working layer taxonomy — typically {kernel/POSIX-tier, systemd-substrate-tier, process-state-machine-tier, interaction-protocol-tier, architectural-prose-tier, principle-purpose-tier} for infra-adjacent reviews, adjust for other domains — and require each finding to declare which layer it belongs to. Findings that surface a layer the target artifact cannot mechanically carry (e.g. kernel/POSIX-tier provider-facts against an architectural-prose-tier ADR) get an explicit `process_disposition` of `push-to-code / implementation-deferred to <reference-implementation-path>` rather than a REBOUND the prose tier cannot fold. REBOUND is reserved for findings the target artifact's native tier can actually carry (for an ADR: MUST/MUST-NOT inconsistency, responsibility partition drawn wrong, reference-to-nonexistent-authority, principle-purpose-tier conflict). Without this discipline, review is unbounded — as the operator named it: "a rhetoric engine could poke holes forever, or an engineer could polish something forever." Naming the layers bounds the review to what the artifact tier can actually resolve. See `lessons/operator-theories/2026-08-26-intuition-anchor-plus-giants-methodology.md` for the layer-methodology framing (Marr/Parnas/VSM/Dennett/Ryle/Rasmussen giants-shortlist).
- Codex is a **detail-checker, not a decider** (see `reference_codex_second_opinion_fit`): its "could not verify" means unverified, not wrong; its DEFECT claims get verified against primary sources or empirically before acting.

## What happens with findings

- Triage at the dispatcher's tier: BLOCKS → fix before implementation proceeds (or before flip, if post-hoc); FIX-SOON → fold into the current round; NOTE → record in ticket.
- The review result (or the exemption decision) is recorded in the ticket's transition log or Context — the reviewer pre-flight checks it exists, same as the DECISIONS section.

## Why this exists

T-1633 (2026-07-12): a fully-disciplined Claude-side chain (spike → arc plan → per-unit plan reviews → adversarial code reviewer, one genuine REBOUND) still missed two defects a 5-minute codex pass caught post-merge: a tmux/cgroup rollback orphan (BLOCKS-PILOT) and a session/load wire-shape gap. Independent-model review catches failure modes same-model redundancy cannot — that's the multi-model orchestration premise (ORG-016) applied at plan tier.
