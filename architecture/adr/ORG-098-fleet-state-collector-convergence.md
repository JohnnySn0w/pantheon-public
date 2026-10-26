---
aliases: [ORG-098]
id: ORG-098
title: "Fleet-state collector convergence — presence emitter reads Recovery's canonical JSON for STATE; rich content stays on its narrow path"
status: "Ratified 2026-09-16 by the operator (T-3718; TG 9981 'Fire at will' after TG 9979-9980 priority-sort exchange). Shape F: STATE signal converges (presence emitter reads Recovery's canonical JSON); rich per-persona activity content (activity_strip.recent_text) stays on its current narrow presence-hooks path. Transport (d) picked at ratify: Recovery's push script tees the canonical JSON to a local file (/var/lib/pantheon-metrics/data.json) alongside the existing your-domain push — internal consumers read the local file, external consumers keep reading your-domain.example. Not a schema change to pantheon-metrics/1. HITL: **Ratified** — implementation is autonomous (BT1 orchestrator, plus Recovery tee-line ticket)."
date: 2026-09-14
author: Orchestrator (orchestrator / presence-hooks + kino operator plane steward)
steward: orchestrator
appetite: 3
riskiest_assumption: "That the STATE signal Recovery's collector already produces (`state ∈ {active, idle, down}`) is sufficient for the Car Thing presence app + kino Health Plane's needs without the finer per-persona activity strip that presence-hooks writes. If either consumer surface actually needs strip content merged with state atomically — e.g. kino gains a per-persona history view that needs state+strip pairs from the same instant — F's two-read pattern (state from Recovery, strip from .recent_text) either accepts drift between them or the design has to revisit the merge question. Mitigation: BT1 implements explicit stale/missing handling + timestamp semantics on the two-read path (per codex's push-to-code disposition); if kino later needs strip, it can add its own .recent_text reader on the same narrow path (small-audience preserved), OR the merge question re-opens under a fresh ADR with the actual use case as evidence."
build_disposition: needs-build
scoping_ticket: T-3716
relates: ORG-076, ORG-077, ORG-097, ORG-050, ORG-003, ORG-012
---

# ORG-098 — Fleet-state collector convergence: STATE-only, rich content stays narrow

**Status:** **Ratified 2026-09-16 by the operator** (T-3718; TG 9981 "Fire at will" after TG 9979-9980 priority-sort exchange). Shape F + transport (d).
**Date:** 2026-09-14 (rewrite 2026-09-15).
**Author:** Orchestrator (HO / presence-hooks + kino operator plane steward).
**Steward:** orchestrator.
**HITL:** ratify-hold.
**Related:** [[ORG-076]] §5.5 (collector seam — terminal target for STATE), [[ORG-076]] §7.3 (one-collector-N-consumers pattern this satisfies for STATE), [[ORG-077]] (folded into ORG-076 §7 — health plane), [[ORG-097]] (external-authority signal-writer, would apply if a public-egress problem arose — F avoids creating one), [[T-3715]] (specialist relay surfacing the operator's direction), [[T-3716]] (scoping ticket), [[T-2941]] (ORG-076 build tranche — terminal-phase dep for the STATE surface).

## Intent

Retire the **duplicate fleet-state collection** on the STATE signal only. The Car Thing presence emitter's own mtime scan of `/home/pantheon-shared/presence/*.mtime` duplicates the coarse working/thinking/idle signal Recovery's `pantheon_metrics_gen.py` already derives from `.claude/` jsonl freshness. That duplication goes away: presence emitter reads Recovery's canonical JSON for STATE. The Car Thing presence app joins the "one collector, N consumers" seam ORG-076 §7.3 already establishes.

**Deliberate non-goal — the rich per-persona activity strip does NOT converge.** The redacted `.recent_text` file each persona writes stays exactly where it is: a per-persona file under `/home/pantheon-shared/presence/`, readable only by processes on that VPS with group access, filtered once at emit-source by `presence-hooks/activity_strip.py`'s 10 INLINE + 4 FULL_LINE regex pass. Presence app keeps its direct read of that file. It is not health-plane-scoped content and does not enter Recovery's public-safe feed.

Serves **D1** (the operator's bandwidth via reduced duplicate collection surface for STATE) + ORG-076 §7.3's premise (the health-plane's data lives at one collector subscription — for STATE, which IS the health-plane surface; the strip is a per-persona presence-app concern that lives on its own narrow mechanism).

## Context — why F (this shape) not A/B/C/D

Design-shape review 2026-09-15 (the operator TG 9953-9958 exchange, codex second-opinion gpt-5.5) considered five candidate shapes:

- **A (two-file emit — internal-rich + public-safe).** Recovery's collector produces both files, filter code lives in the projection layer.
- **B (one-file + read-gate).** Collector produces one rich file; a filter gate wraps each external egress.
- **C (ORG-097 as egress-authority).** Reuse the ratified external-authority signal-writer.
- **D (two separate collector processes).** No shared code, no projection; runs twice.
- **F (this — don't merge in the first place).** Recovery's collector unchanged; only STATE signal converges; rich content stays on its narrow path.

F wins because it never creates the rich-content-in-shared-feed problem the other shapes have to solve. A/B/C/D each add machinery (projection layer, filter gate, ORG-097 wiring, or duplicate aggregation) to defend against a leak-path or filter-audience problem that F avoids by scope-drawing. Codex's independent design-shape review (gpt-5.5, 2026-09-15) confirms: no near-term use case justifies widening `activity_strip` into a shared feed; §7.3's "one collector, N consumers" is satisfied for STATE alone; the two-read cost on the presence-app side is smaller than A/B/C's privacy machinery; C is the strongest alternative but only wins if public egress becomes the main problem, which F avoids entirely.

If a future consumer (kino history view, cross-persona reporting) actually needs state+strip merged from one source, that's a new consumer contract that re-opens the merge question with evidence in hand. Not a reason to solve it speculatively today.

## Current two-collector shape (unchanged since T-3715 investigation)

- **Recovery-stewarded `tools/pantheon_metrics_gen.py`.** `SCHEMA=pantheon-metrics/1`, coarse allowlisted per-persona fields (`label`, `cpu_pct`, `mem_pct`, `state ∈ {active, idle, down}`) + top-level `cluster` rollup + top-level `hosts` array (added T-3721). Roster: 6 claude-harness personas + consultant + echo. Signals: `/proc` CPU-delta, `ps %mem`, `.claude` jsonl freshness. Emits local JSON on VPS; `pantheon_metrics_push.sh` copies it to CT103 caddy for public `pantheon-metrics.your-domain.example`. `leak_scan()` refuses non-allowlisted content on every write.

- **Orchestrator-stewarded `sseed-presence/emitter/state_source.py`.** Signals: orchestrator's own transcript mtime (self-account scan) + shared `/home/pantheon-shared/presence/<persona>.{mtime,recent_text}` files (cross-account, written per-turn by `presence-stop-hook.sh` + `activity_strip.py` per persona). Derives coarse activity state (working/thinking/idle) at 60s/300s thresholds. Presence app reads state_source's derived state + `.recent_text` directly for the activity strip display.

- **Overlap.** ~60s freshness threshold on the STATE signal. Same derivation semantically, from different signal shapes (Recovery: `.claude/*.jsonl` mtime; presence: `.mtime` file mtime).

## Consumer landscape (per specialist's 2026-09-15 inventory, T-3730)

For scope-drawing, the health-plane consumer set spans two tiers under F:

**Coarse-tier consumers** (read Recovery's `pantheon-metrics/1` canonical JSON):
- Car Thing presence app (via presence emitter, post-BT1 rewrite) — STATE per persona.
- Personal-site PANTHEON // CLUSTER panel — `agents[]` + `cluster.load_pct`, already aligned.
- Personal-site SYSTEMS // UPLINK panel — `hosts[]`, already aligned (T-3721, landed today).
- Future kino Health Plane section (T-2979) — STATE per persona, on the same Recovery feed today, later on §5.5 subscription.

**Rich-tier consumers** (read presence-hooks' `.recent_text` directly):
- Car Thing presence app (unchanged read path) — per-persona activity strip.
- Personal-site PARTNER DAEMON // SSEED panel (latent, hardcoded placeholder values today; specialist-side wire-up when the converged surface lands) — rich state (voice/context/link levels).

**Explicit Non-goals** (called out so a scope-question doesn't recur):
- Personal-site OPERATOR // usr.dat panel — static identity card, not a consumer.
- Personal-site SYSTEM DIAGNOSTIC panel — client-side browser telemetry (rAF fps, DOM count, navigator.*), reads the viewer's own device, not any Pantheon feed. Deliberately out-of-scope.

The two-tier split is F's deliberate shape — coarse-safe stays public-safe (leak_scan-refused), rich stays narrow (small-audience presence-hooks path). Consumers pick their tier by what they need to display; no consumer needs to read across the tier boundary.

## Design — F, in one phase

BT1 (orchestrator) rewrites `sseed-presence/emitter/state_source.py`:

1. **State source flips to Recovery's canonical JSON.** Read the local file `pantheon_metrics_gen.py` writes; project the per-persona `state` field into the presence app's working/thinking/idle expected shape. 60s/300s thresholds move into the projection layer (or drop if Recovery's state field's semantics carry them, TBD at build).
2. **Duplicate `.mtime` scan is deleted.** No more per-persona mtime file reads for state derivation; the presence-hooks `.mtime` file writes still happen (presence-stop-hook.sh unchanged) but state_source stops reading them.
3. **Rich content read path unchanged.** `.recent_text` per-persona reads stay exactly as-is. Same narrow audience, same emit-side filter, no schema shift.
4. **Explicit stale/missing handling on the two-read path.** Per codex's FIX-SOON push-to-code disposition (design-shape review 2026-09-15): the two reads (state from Recovery's JSON, strip from `.recent_text`) can drift in time or one can be missing. BT1 must implement: (a) if state is missing/stale, presence app shows "unknown" for that persona rather than empty; (b) if strip is missing/stale, presence app shows state alone rather than a blank card; (c) `state`'s timestamp is the Recovery-canonical-JSON's `generated_at` field; `strip`'s timestamp is the per-persona `.recent_text` mtime; both surface to the display layer so the app can render age indication or hide stale content.

**Recovery's collector is unchanged.** No schema addition. No leak_scan allowlist change. No filter-audience question. No new consumer of Recovery's canonical JSON externally-facing.

**Presence-hooks are unchanged.** `presence-stop-hook.sh` + `activity_strip.py` keep writing `.mtime` + `.recent_text` per persona. Their audience stays as-is (small, VPS-account-bounded).

**No Phase 2 in this shape.** The original two-phase design existed because I was trying to build a bridge to T-2941's §5.5 collector. Under F, if/when T-2941 lands with §5.5 carrying per-persona state, state_source's read source flips again (from Recovery's JSON to the §5.5 subscription) — that's a re-projection, not a schema migration. Rich content still stays narrow. Presence-hooks still stay untouched.

## Steward-split

- **Orchestrator** owns the entire scope of this ADR's build: `sseed-presence/emitter/state_source.py` rewrite (BT1), presence app's projection changes (none needed unless the state → display mapping changes shape).
- **Recovery** touches nothing for this ADR. No schema change, no code change to `pantheon_metrics_gen.py`. Cross-steward handoff surface = zero (BT1 is entirely orchestrator-domain).
- Presence-hooks (`/home/pantheon-shared/presence-hooks/`) stay owned by orchestrator as today; no changes to their emit path.

Neither steward touches the other's surface. The convergence happens by orchestrator's reader changing what it reads from — Recovery's canonical JSON is a **read surface** orchestrator was not previously using, but Recovery already publishes it for other reasons (personal-site panel). No new obligation on Recovery.

## Build tranche

**BT1 (orchestrator, appetite 2):** `state_source.py` rewrite per §Design. Includes:
- New reader of Recovery's canonical JSON at its on-VPS local path (path resolution: read `tools/pantheon_metrics_gen.py`'s output path config or environment).
- Delete the `.mtime` scan loop.
- Add stale/missing handling per codex FIX-SOON.
- Add timestamp exposure to the display layer (state's `generated_at`, strip's file mtime).
- Test: presence app rendered view is equivalent to today's output for at least 10 minutes of live measurement, across state transitions. Divergence budget: zero on strip content; state may lag or lead by up to one 60s window at threshold boundaries.

**Post-T-2941 (orchestrator, later):** state_source's read source flips from Recovery's JSON to the §5.5 collector subscription. Re-projection only; presence-hooks unchanged. Not filed as its own ticket yet; will be when T-2941's build tranche moves.

**Retired scope from the earlier draft:**
- BT2 (Recovery schema addition) — not needed under F.
- BT3 (presence-hooks retirement/migration) — presence-hooks stay; no retirement.
- BT4 (Recovery collector cutover to §5.5 consumer) — Recovery's separate arc, not scoped by this ADR.
- BT5 (kino parity check) — not affected under F; kino reads §5.5 for state on its own timeline.

## Consequences

**Positive:**
- The two codex DEFECTs from the earlier draft evaporate. No leak-path issue (Recovery's feed content unchanged); no filter-audience widening (`.recent_text` audience unchanged).
- Recovery has no work. Cross-steward coordination surface = zero.
- Presence-hooks stay untouched. No mid-migration lifecycle risk on the emit side.
- The convergence achieved is exactly the ORG-076 §7.3 scope: STATE signal, one collector, N consumers.
- BT1 is a simple single-file rewrite bounded to `state_source.py`.

**Negative:**
- Rich content stays on a separate mechanism from state. Not fully "one canonical fleet-state source" if you view rich content as part of fleet state (this ADR argues it isn't).
- Two-read pattern on the presence-app path (state + strip from different sources); requires explicit stale/missing handling in code.

**Neutral:**
- Recovery's `pantheon-metrics/1` schema stays as today. Any Recovery-side extension (like T-3721's `hosts` field addition) is orthogonal to this ADR.
- Presence app display shape unchanged in the immediate term. Could gain finer-grained state semantics later if Recovery's collector's state field grows richer (unrelated to this ADR).

## Ratify-surface for the operator

The one call:

**Does shape F match the "converge the two fleet-state collectors + tie into the health plane" direction from 2026-09-14, given that F narrows the convergence to the STATE signal and explicitly excludes rich content from the shared surface?** If yes, BT1 spins off as a buildable-now ticket (T-3719 already exists with the older draft's shape; it will need a re-brief to match F). If no, the residual is which of A/B/C/D is preferred and why (F's argument against each is in §Context).

## Changelog

- 2026-09-14 — Authored (Orchestrator) as the T-3716 scoping ticket's deliverable. Two-phase schema-widening shape (rich content merged into Recovery's feed).
- 2026-09-15 morning — §3 filled with primary-source values from `activity_strip.py`; elaborated then reverted per the operator TG 9930 clarification ("elaborate to me in channel, not the doc").
- 2026-09-15 afternoon — codex second-opinion surfaced two spec-tier DEFECTs (leak-path, filter's widened audience); initially disposed as push-to-code, then per the operator TG 9949 caught as spec-tier that the ADR prose should carry. The operator TG 9953-9958 exchange reframed the design as "common consumable surface that emits either two ways, or one way with a filter layer for external accesses" — five candidate shapes considered; F picked as simplest that avoids the DEFECTs by scope-drawing. Codex design-shape review (gpt-5.5) confirmed F over A/B/C/D. Full rewrite to F.
