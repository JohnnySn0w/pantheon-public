---
aliases: [ORG-099]
id: ORG-099
title: "Seat parity — definition, guidelines, upgrade paths for the fleet's per-seat capability surface"
status: "Ratified 2026-09-16 by the operator (TG 10007-10011 trust-boost + T-3747; drafted autonomously per the amended infra-design autonomous-with-corpus-check policy). Reversible via amend/supersede."
date: 2026-09-16
author: Orchestrator (orchestrator / fleet-substrate steward)
steward: orchestrator
appetite: 4
riskiest_assumption: "That the axis taxonomy (§3) is the right cut of the parity surface — i.e. that HARD/SOFT/BILLING/DOMAIN tiering meaningfully separates 'the seat is broken' from 'the seat differs by design.' If a real parity gap arises that doesn't fit the four tiers, the tiering has to expand rather than shove the case into an ill-fitting tier."
build_disposition: needs-build
scoping_ticket: T-3747
relates: ORG-068, ORG-042, ORG-039, ORG-053, ORG-031, ORG-047, ORG-095
---

# ORG-099 — Seat parity: definition, guidelines, upgrade paths

**Status:** Ratified 2026-09-16 by the operator (T-3747; TG 10007-10011 trust-boost — the seat-parity spec was drafted + landed autonomously per the amended infra-design autonomous-with-corpus-check policy captured in `feedback_infra_design_autonomous_with_corpus_check_msg10007`).

**Date:** 2026-09-16.
**Author:** Orchestrator (HO / fleet-substrate steward).
**Related:** [[ORG-068]] (universal system-lifecycle contract — the stage matrix parity sits alongside), [[ORG-042]] (two-hemisphere topology; second-head is a full-parity seat), [[ORG-039]] (harness-config posture), [[ORG-053]] (HC controller — MIGRATION-in-progress axis), [[ORG-031]] (harness abstraction — cross-harness parity semantics), [[ORG-047]] (federated identity — per-seat credential minimization), [[ORG-095]] (credential + key lifecycle — the auth axis).

**Scoping ticket:** T-3747. **Data seed:** `knowledge/episodic/2026-09-16-t3747-seat-parity-audit-corpus/report.md` (10 sources, 13 axis families, 20 gaps caught by prior audits, 7 documented carve-outs, 9 open questions — method: data-first extraction, spec-second).

## Intent

Give **seat parity** a canonical spec so that:

1. Fleet operators (Recovery, Orchestrator, the operator) have one document to answer "what does it MEAN for a seat to be at parity?" rather than re-deriving from scratch each bringup.
2. Drift-detection has a target surface — every axis has a check method + a directional-bias rule (silent-green is the failure mode to prevent).
3. Legitimate non-parity carve-outs are DECLARED, not silent — a seat's exemption from an axis is a first-class artifact with rationale + verifier.
4. Upgrade paths — adding, propagating, retiring axes — are procedural, not ad-hoc, so the spec stays evergreen without a permanent full-fleet re-audit tax.

Serves **P1** (architecture over policy — parity is a substrate contract, not a compliance script) + **P4** (traceability — declared axes + declared carve-outs + verifiable checks) + **D1** (the operator's bandwidth — one spec, not per-bringup improvisation).

## §1 — What is a seat?

A **seat** is one **(persona × unix account × harness × substrate slice)** tuple. The four components together define what a seat CAN do:

- **Persona** — the role-id in `instance.yaml` (orchestrator, specialist, recovery, sub-orchestrator-1, sub-orchestrator-2, second-head, peer-daemon-codex, peer-daemon-the-ancestor).
- **Unix account** — the OS user the persona's process runs as (claude, claude-research, claude-fallback, suborch1, suborch2, exocortex, codex, the-ancestor).
- **Harness** — the LLM-driver frame (Claude Code | Codex CLI). Determines config file shape (`~/.claude/settings.json` vs `~/.codex/config.toml`), allowlist syntax, plugin surface, hook-set names.
- **Substrate slice** — the systemd-user manager + tmux session + assigned resources (billing model, API quota, cred store) the seat operates on.

Terminology note (§6.1 of the corpus flagged corpus uses "persona"/"account"/"role"/"seat" semi-interchangeably): **"seat" is the canonical noun in this spec.** "Persona" refers to the seat's role identity; "account" to the OS-user component; "role" to the `instance.yaml` role-id. When precision matters, use the compound.

## §2 — The parity contract

**A seat is at full parity if:** every HARD-parity axis check passes AND every declared carve-out for the seat is verified AND no undeclared deviation from the fleet baseline exists.

**A seat is at partial parity if:** all HARD-parity axes pass, some SOFT-parity axes lag, no undeclared HARD deviation. Partial-parity seats are OPERATIONAL but not fleet-baseline; a soft-lag over 30 days without carve-out declaration triggers a REBASELINE ticket.

**A seat is BROKEN if:** any HARD-parity axis fails AND no declared carve-out covers the failure.

The three tiers (broken / partial / full) are the check output shape; a seat's actual capability isn't binary.

## §3 — Axis taxonomy (four tiers)

Every parity axis is classified into ONE of four tiers. Data seed §2.1-§2.13 (13 axis families) provided the input; this section is the tiering pass. Sources cited per axis in the audit corpus report.

### §3.1 HARD parity (breaks the seat if absent)

If any of these fails without a declared carve-out, the seat cannot do the work its persona exists for. Drift-detection MUST alarm on failure.

- **Substrate baseline** — unix account exists + linger enabled + git identity set + sudoers scope declared (whatever the persona's authority requires) + home-dir traversal permissions respect `/home/pantheon-shared/` cross-account discipline.
- **Harness live** — CLI installed + on PATH + auth completed + config file present + `permissions.allow` populated AND covers MCP tools (gap G5 + G6 in the corpus — the MOST-MISSED class).
- **Mailbox two-directional** — own inbox at hub + can send to peer inboxes (gap G7 — one-direction grant is a silent seat-isolation).
- **`mcp` python package pinned** — `mcp<2` per-account (gap G8 — unpinned pulls 2.x, breaks MCP server at session start).
- **Ticket-system registration** — persona registered in `AGENT_ROLE_MAP`/`instance.yaml` (gap G12 — unregistered = hard-rejected by `ticket.py`, seat is invisible).
- **Chat-surface exercised end-to-end** — a first-live turn that actually uses the reply tool + confirms no permission prompt reaches the operator (canary discipline, archivist 2026-07-20 field record).
- **Delivery capability, not classification** — `pantheon-inbox-consumer@<persona>` present on the seat's own user-systemd manager (query the RIGHT manager per corpus methodology observation), timer enabled, delivery-mode declaration matches substrate reality (send-keys ⇒ live tmux pane exists; mailbox ⇒ hub grants exist).

### §3.2 SOFT parity (fleet baseline; degrades gracefully; carve-outs allowed)

If any of these lags, the seat still works for its persona's core function, but the fleet has drifted. A soft-lag over 30 days without declared carve-out triggers a REBASELINE ticket.

- **Standard monitors** — `pantheon-context-fill-monitor@`, `pantheon-activity-monitor@`, `pantheon-keep-going@`, `pantheon-mailbox-wake@` templated units enabled at the persona-tier's thresholds (orchestrator: activity-monitor 30min; implementer: 15min; reviewer: 5min per `feedback_active_team_worker_check`).
- **Persona-orientation rendered artifacts** — `~/.claude/skills/pantheon-*` present + source-bindings fresh (T-3339 manifest, rendered on orchestrator authoritative + peers `git pull`). Gap G19 — per-clone-render stamped source-bindings.yaml with nowhere to push; correct discipline is orchestrator-authoritative render + push.
- **Standard hook set** — `SessionStart` + `PreToolUse` + `PostToolUse` + `Stop` + `SessionEnd` hooks installed with sane timeouts (T-3172 bug#3 flagged 8/14 hooks default 60s; SOFT because a missing hook degrades rather than breaks the seat).
- **Substrate hygiene** — XDG portal masked (T-3722; three silent-failure recurrences before fleet-wide mask), CLAUDE.md under 40KB soft-threshold, `.claude/settings.json` merge-don't-overwrite discipline observed.
- **HC controller** — `pantheon-runtime-controller@<persona>.service` (T-2953). Currently MIGRATION-in-progress across the fleet — seats can be pre-HC OR HC; SOFT tier reflects this axis being in flux.

### §3.3 BILLING / QUOTA (per-seat variance is expected; must be DECLARED)

Not a parity target; explicit variance surface. Seats declare their model; a mismatch between declared + actual is the failure mode.

- **Billing model** — subscription-OAuth (claude-seat, kino config) vs headless API key (archivist per `project_kino_chat_sunset_headless_api_billing`). Declared in seat's instance.yaml annotation OR ledger.
- **Quota schedule** — subscription-OAuth resets follow the seat's substrate (claude Sun 12:00 Chicago, codex Sat 14:15 Chicago per `reference_quota_reset_schedule`). API-key seats billed per-token, no reset schedule.

### §3.4 DOMAIN / SCOPE (partition of work, not parity)

Not a parity target; scope partition by design. Named here so a seat's narrow scope doesn't get flagged as a parity gap.

- **Hemisphere domain** (ORG-042) — archivist handles memex/exocortex-side work; not a defect that she doesn't handle Echo.
- **Recovery-fallback distinctions** — Recovery has broader sudo scope (kino/journalctl asymmetric per corpus §2.1), cross-account status-check role most peers don't have. NOT a parity gap; parity requires recovery's authority to be authorized-different, not identical-to-peers.
- **Peer-orch vs implementer tier** — artisan/mason FULL orchestrators at HO's degree (CLAUDE.md pin); their WIP-cap posture, dispatch authority, and mid-arc autonomy differ from implementer-tier seats. Not a parity gap; tier is declared.
- **Codex harness variant** (per pantheon-bringup.md §12) — codex's config shape (`~/.codex/config.toml`), sudo scope (narrower), permission model differ from Claude Code. §12.6 verification disposition table is the worked example of per-harness carve-out documentation shape.

## §4 — Carve-out declaration shape

Any deviation from HARD or SOFT parity that is legitimate must be declared. Undeclared deviation = drift = REBASELINE ticket. Declaration shape:

```yaml
carve_outs:
  - seat: the-ancestor
    axis: pantheon-context-fill-monitor
    verdict: intentional-absence
    rationale: >
      the-ancestor's purpose is to be the one agent that runs unbounded context
      without forced resets; a context-fill restart defeats the persona.
    ratifier: operator-msg-2936
    ratified_date: 2026-06-09
    verifier: check that the timer unit is absent on the-ancestor
    review_trigger: any change to the context-fill monitor's fleet-standard behavior
```

Canonical fields: `seat`, `axis`, `verdict` (`intentional-absence` | `intentional-variant` | `migration-in-progress` | `deferred`), `rationale`, `ratifier` (operator-msg-ref, ADR-ref, or ticket-ref), `ratified_date`, `verifier` (grepped/scriptable check), `review_trigger` (event that would invalidate the carve-out).

Carve-outs live in a single fleet manifest (candidate location: `sop/standard/seat-parity/carve-outs.yaml` per the manifest-driven upgrade-path in §5). One-file source of truth beats scattered per-seat prose (T-1029's prose form doesn't scale to N seats × M axes).

## §5 — Evergreen mechanism

The parity spec itself must stay current. Two candidate patterns from the corpus:
- **(a) SOP-with-appended-field-records** — pantheon-bringup.md's shape: 14+ freshness passes appending "seat X 2026-Y gap Z + operator-signal." Ratified: works; scales; carries provenance; grows monotonically.
- **(b) Manifest-driven** — T-3339 persona-orientation source-bindings.yaml: axes declared in a manifest that renders check scripts + audit reports; drift-detection = re-render + diff.

**Decision: hybrid.** Adopt (b) for axis-DECLARATION + check-script generation (mechanical, drift-scannable); adopt (a) for spec-LEVEL commentary + field-record accretion (human judgment, methodology observations, gap-provenance).

- Axis manifest at `sop/standard/seat-parity/axes.yaml` — schema: `{axis_id, tier (HARD|SOFT|BILLING|DOMAIN), check_script_ref, applies_to (all-seats|list), source_of_truth (which SOP/ADR/repo file)}`. Render tools (candidate: `tools/parity_render.py`) generate per-seat check scripts + a fleet audit table from the manifest.
- Field-records + rationale + methodology in this ADR + successor amendments; carve-outs in `sop/standard/seat-parity/carve-outs.yaml`.

Upgrade paths:

- **Add-an-axis:** file a proposal ticket → amend axes.yaml → render check scripts → include in the next fleet audit → surface for lean → post-hoc ADR amendment or superseding ADR if the axis shifts the spec's shape.
- **Retire-an-axis:** file a retirement ticket → verify no seat depends on it → remove from axes.yaml + carve-outs.yaml → render check scripts → the axis stops appearing in fleet audit output. Gap G20-style green-forever cells prevented by re-render generating from current manifest, not accumulating.
- **Propagate-a-fleet-standard-change** (e.g. XDG portal mask, ORG-039 harness-config shift): the change lands on the fleet-authoritative source (whatever the SOP or hook script), the axis manifest gets a version bump, fleet audit re-runs, seats not-yet-updated show as drifted, remediation tickets file per-seat.
- **Migration-in-progress axes** (like HC controller today): axis carries a `migration` field naming the source + target state + carve-out shape (`{verdict: migration-in-progress, ratifier: T-2953, ...}`); seat isn't broken during migration.

## §6 — Ownership + change authority

- **SPEC owner** (this ADR + axes.yaml + carve-outs.yaml): orchestrator. Amendments file as new tickets targeting orchestrator; OPERATOR-ratifies structural changes (adding a new tier, changing carve-out shape); the operator does NOT need to ratify individual axis additions/retirements under the existing tier taxonomy (per `feedback_infra_design_autonomous_with_corpus_check_msg10007`).
- **SEAT owner** (per-seat parity restoration): the persona's recovery (recovery for most; recovery role covers her own seat too via cross-account discipline). SEAT owner acts on remediation tickets when audit surfaces drift.
- **AUDIT operator** (running the render + fleet-audit): recovery (recovery holds cross-account substrate authority + hosts the scheduled runs). Specialist (specialist) may run supplementary audits on knowledge/telemetry axes within her domain.
- **CARVE-OUT ratifier** (declaring a new legitimate non-parity): the operator for HARD-axis carve-outs (they encode a decision about what the fleet cares about); orchestrator-autonomous for SOFT/BILLING/DOMAIN carve-outs matching existing verdict types.

## §7 — Cross-harness parity semantics

Per ORG-031 harness-abstraction (T-2372 harness-parity-audit): KEEP-SPECIFIC for permissions / sandbox / doctor. This ADR inherits that discipline — parity SEMANTICS are cross-harness (e.g. "harness auth completed" applies to both Claude Code + Codex), but CHECK-SCRIPT implementation is per-harness (Claude Code checks `~/.claude/settings.json`; Codex checks `~/.codex/config.toml` + `approval_policy`). The axis manifest carries `applies_to` per-axis; where implementation differs, per-harness check-script variants are named.

## §8 — Directional bias for drift-detection

Per T-2232 methodology observation §230: alarm-biased failure preferable to silence-biased. When a check has ambiguous rc semantics (sudo-denied, container-restart-vs-fresh, wrapper-squashed exit codes), REPORT the ambiguity as ALARM, not as pass. Silent-green is the failure mode this spec exists to prevent (gaps G1, G2, G4, G9, G13, G14 are all silent-green variants). Every check script MUST distinguish "thing absent" from "I could not ask."

## §9 — Build tranche

- **BT1 (orchestrator, appetite 2):** materialize `sop/standard/seat-parity/axes.yaml` (starter manifest with the axes named in §3), `sop/standard/seat-parity/carve-outs.yaml` (starter with the-ancestor's context-fill carve-out + ORG-042 hemisphere carve-out + ORG-053 HC migration-in-progress + billing/quota declarations), and `sop/standard/seat-parity/README.md` (guideline SOP — bring-to-parity checklist + detect-drift audit + close-down process).
- **BT2 (orchestrator, appetite 3):** `tools/parity_render.py` — read the manifest + emit per-seat check scripts + fleet audit table (markdown + JSON envelope per T-2372 shape). Runnable locally by orchestrator; deployable to recovery's substrate-audit lane.
- **BT3 (recovery, appetite 2):** wire the fleet audit to a scheduled run under recovery's recovery substrate authority, comparable to `tools/delivery_reachability.py`'s armed 6-hourly cadence. Surface report to recovery's mailbox for triage; escalate structural gaps to orchestrator via ticket.
- **BT4 (orchestrator, appetite 3):** first fleet audit run against the current 8-seat topology; produce baseline report + file remediation tickets per seat per axis-gap. This IS the "empirical parity dataset" the operator's TG 10000 wanted — but auto-generated from the manifest.

BT3 gates on BT2 gates on BT1. BT4 gates on BT3.

## §10 — Open questions folded from the corpus §6

- **Seat scope** (codex daemon = seat? the-ancestor?): RESOLVED — the four-component tuple (§1) admits both. Codex is one seat (codex persona × codex account × Codex harness × its substrate). The-ancestor is one seat (its own persona + account + Claude Code harness + substrate with the one carve-out).
- **Ownership + change authority**: RESOLVED §6.
- **Retirement of an axis**: RESOLVED §5.
- **Cross-harness parity semantics**: RESOLVED §7.
- **Seat's canonical noun**: RESOLVED §1.
- **Hard-vs-soft tiering**: RESOLVED §3 (four tiers, per-axis assignment).
- **Drift-detection automatable?**: RESOLVED §5 (manifest-driven render + per-axis check scripts; scheduled run).
- **Evergreen mechanism**: RESOLVED §5 (hybrid — manifest for axes/checks + SOP for field-records/rationale).
- **Carve-out declaration shape**: RESOLVED §4.

## §11 — Reversibility + revision path

This ADR is reversible via amend or supersede. Ratified 2026-09-16 by the operator via TG 10007-10011 trust-boost; the amended infra-design autonomous-with-corpus-check policy means substantive amendments file as orchestrator-owned tickets landing directly under the same policy (draft + land + lean-surface). Structural changes to the tier taxonomy (§3) or the carve-out shape (§4) — operator-ratify because they'd shift what the fleet MEANS by parity, which is expressive-operator. All other amendments (adding axes, retiring axes, per-tier carve-out additions matching existing verdicts): orchestrator-autonomous per the new policy, with post-hoc surface.
