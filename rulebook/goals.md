# Goals registry — the north stars

**Status:** Drafted 2026-06-11 by Orchestrator (HO) per the operator msg 3737/3738; **the three core north stars are the operator's verbatim words (msg 3740, 2026-06-11)** — canonical, not draft. Remaining entries carry DRAFT north stars until he blesses or rewrites them. Phase lines and deferred-items are HO-maintained operational state.

**What this file is.** The tier ABOVE principles. The amended D3 bar says "goal-level and above is the operator's; everything below is the orchestrator's" — this registry is what makes that test mechanical. Work maps to a registered goal → below the bar, adopt under P/D justification. Work maps to nothing here → goal-shaped by definition → see the unmapped-work rule in `policies/goal-mapping.md`.

**Two goal layers (ORG-034, 2026-06-12).** This file holds **layer 1: north stars** — terminal, few, slow-moving, the operator's. **Layer 2: arc-intents** — what a given arc/ticket/dispatch is for, in outcome language — live on the work artifacts themselves (brief opening lines, ticket Asks, `serves:` fields), nested under a north star, agent-ownable at every tier. They are NOT registered here; this registry stays small and his. The justification chain runs ticket → arc-intent → north star, with P/Ds as the how (see `adr/ORG-034-two-layer-goal-architecture-intent-first-justification.md`).

**Maintenance contract.** The operator owns the **North star** lines (content). HO owns structure, **Phase** lines, and **Deferred** lines, updated as state changes (no ratification needed — they're operational facts, P4-traceable to ledger/tickets). New entries are drafted by HO when unmapped work surfaces, marked DRAFT, and surface in the operator's next digest.

---

## echo-prismatic — the voice interface

- **North star (the operator, msg 3740):** "Have a voice interface to plug anything into." — Generality is the point: the demux/intent/action pipeline is the universal plug surface; the demo app is the current proof, not the ceiling.
- **Phase:** Post-ADR-217/218/219 dashboard+tool-policy+FP arc merged and live. ADR-221 (partials correlation) + ADR-222 (typed classifier errors) adopted 2026-06-11, implementations in flight. Daemon-side merges batch into the next supervised restart.
- **Deferred / reserved:** Vault ADRs 001–199 (operator-authored). Chain-classifier flip to `adapter="chain"` in echo.toml is the operator's call. `*-working-<date>` tags wait on his live confirm.
- **Registered arc-intent — phone calls (the operator, msg 3770, 2026-06-12):** echo makes/takes phone calls as a conversation surface. Vault-anticipated at two levels (capture-context abstraction names "a phone session" as a non-acoustic source; Twilio/VoIP outbound calls = Technology Stack "Future idea" row) but never phased. **Gated on the CSM state-machine arc** (vault phase 4+: PROMPTED/CONVERSING transitions + timers + barge-in — unbuilt, itself an operator roadmap call per BACKLOG 2026-06-09 actualization; a phone call is pure multi-turn conversation, so the gate is structural, not scheduling). Groundwork survey of existing systems ticketed at registration.

## revenue-project — the income engine

- **North star (the operator, msg 3740):** "Have a set of recurring revenue streams that solve my income problem." — Plural streams; the target is his income problem, not merely Pantheon's burn (the ~$300–500/mo from the scaffold README is the floor milestone, not the goal). Automation-first, platform-risk-aware (MX-1..3).
- **Phase:** Research V1–V5 delivered + lifted; synthesis decision-gated on the operator reviewing the veins. **Actuation DEFERRED** (the operator msg 3510, 2026-06-08) — build tickets parked with `interactive-required`.
- **Deferred / reserved:** The un-defer ruling itself (goal-level). Real-money actuation, accounts, spend — all his. Orchestration shape pre-decided in ORG-032 (STATE 2 executes at his trigger; don't re-open).

## pantheon (org system) — the orchestration system

- **North star (the operator, msg 3740):** "Create a system of orchestration complex enough to pull off any project." — Capability ceiling is the goal: any project. msg 3733 describes the operating relationship (ideas → delegated creation → digests); the registry you are reading is part of this system.
- **Phase:** principles-as-ratification posture adopted + propagated (D3 amendment, 2026-06-11). Live surfaces: ticket system + ledger + PWA (ticket-UI/stats; chat sunset shipped — the 2026-06-13 headless-API billing cutoff passed, Telegram owns conversation), Telegram as chat surface. Harness-abstraction arc ORG-031 ratified 2026-06-09 (Option-A; codex onboarded as a dispatched worker) + ORG-032 revenue-project shape adopted 2026-06-11; harness-config distribution ORG-039 ratified 2026-06-30 (the operator, via T-1474; genericized reference template published private, T-1475 done — public flip stays the operator-held T-1473 gate).
- **Deferred / reserved:** New always-on agents/accounts (goal-level: access + cost). Veneration substrate (T-0765, his lane today).

## pantheon-pwa (kino) — the org's web surface

- **North star (DRAFT):** The browser surface for org state: ticket queue + ratification UI + stats, boot-durable, zero-chat (Telegram owns conversation).
- **Phase:** Chat sunset arc shipped (code-strip → redeploy → unit teardown complete; T-0910 done) ahead of the 2026-06-13 headless-API billing cutoff — Telegram now owns conversation, the PWA serves ticket-UI/stats only. Live + boot-durable.
- **Deferred / reserved:** Supervised production deploys coordinate with the operator (P1.b of his stated preference, not a ratify gate).

## vibecamp-scheduler + vibecamp-trials-tracker — the operator's personal sites

- **North star (DRAFT):** Small personal tools that just work: the scheduler (one-file Astro site at vibecamp.example.com) and the trials tracker (stdlib-Python server on the tailnet), maintained low-touch.
- **Phase:** Both live. Scheduler: push-without-confirms posture (msg 3187). Tracker: boot-durable via services.conf.
- **Deferred / reserved:** Nothing — lowest-ceremony projects.

## personal-site (resume/portfolio) — NOT YET STARTED

- **North star (DRAFT):** The operator's resume/portfolio site tracking big projects + their executions (T-0705, his framing).
- **Phase:** Unstarted; ticket in inbox, target operator.
- **Deferred / reserved:** Kickoff is his (new-project goal-level).
