# Goals registry — the north stars

**Status:** Drafted 2026-06-11 by Orchestrator (HO) per the operator msg 3737/3738; **the three core north stars are the operator's verbatim words (msg 3740, 2026-06-11)** — canonical, not draft. Remaining entries carry DRAFT north stars until he blesses or rewrites them. Phase lines and deferred-items are HO-maintained operational state.

**What this file is.** The tier ABOVE principles. The amended D3 bar says "goal-level and above is the operator's; everything below is the orchestrator's" — this registry is what makes that test mechanical. Work maps to a registered goal → below the bar, adopt under P/D justification. Work maps to nothing here → goal-shaped by definition → see the unmapped-work rule in `policies/goal-mapping.md`.

**Two goal layers (ORG-034, 2026-06-12).** This file holds **layer 1: north stars** — terminal, few, slow-moving, the operator's. **Layer 2: arc-intents** — what a given arc/ticket/dispatch is for, in outcome language — live on the work artifacts themselves (brief opening lines, ticket Asks, `serves:` fields), nested under a north star, agent-ownable at every tier. They are NOT registered here; this registry stays small and his. The justification chain runs ticket → arc-intent → north star, with P/Ds as the how (see `adr/ORG-034-two-layer-goal-architecture-intent-first-justification.md`).

**Maintenance contract.** The operator owns the **North star** lines (content). HO owns structure, **Phase** lines, and **Deferred** lines, updated as state changes (no ratification needed — they're operational facts, P4-traceable to ledger/tickets). New entries are drafted by HO when unmapped work surfaces, marked DRAFT, and surface in the operator's next digest.

---

## ⭐ THE GRAND PROJECT — this instance's ROOT north star

*Added 2026-08-02 per ratified ORG-053 §1 + its Consequences ("this instance's goals registry gains one Grand Project root entry after ratification"), T-2390. The text below is the operator's ratified wording from ORG-053, transcribed — not HO-authored.*

- **Root north star (ORG-053, ratified by the operator 2026-08-02, TG 665):**
  > Bootstrap a living digital companion and assistant: one coherent, per-user whole that remembers, learns, communicates, and acts through replaceable organs while preserving identity, trust, and human authority.
- **What it is:** for the operator's instance, a **project-of-projects** and the architectural identity of the whole. **Developmental, not terminal** — a stage can complete, but the whole keeps growing, reorienting, maintaining itself, and deepening its relationship. It is a *relational* north star being attempted, not a completed claim.
- **How the project entries below relate to it.** They become **nested organ-project teloi where they serve this root**. ORG-034's two artifact types are unchanged: north stars may nest recursively; arc-intents remain the operational layer beneath a project north star. **This adds a root, not a third kind of work artifact** — so the D3 goal-level bar and the goal-mapping policy work exactly as before, one level deeper. Each participating project declares its parent/telos relationship.
- **Scope boundary, load-bearing and easy to lose (ORG-053, narrowed before the operator's verdict per his TG 556):** this is an **instance-level goal decision, NOT a framework installation requirement**. Pantheon must equally support an instance with **no** shared root north star and one with **several independent** top-level projects. The generic project and installer substrates model an **optional** root; they do not hard-code the Grand Project. Do not let this entry harden into an assumption that every deployment composes into one whole.
- **Stage ordering:** ORG-061 §1 carries the ten-stage developmental sequence with falsifiable exit evidence per stage. The sequence is dependency-oriented, not a ban on early probes — a later stage may run bounded research or prototypes to reduce uncertainty for an earlier one; **production reliance** is what waits on real exit evidence.
- **Re-evaluate:** ORG-053:R7 — the operator changes the relational north star.

## echo-prismatic — the voice interface

- **North star (the operator, msg 3740):** "Have a voice interface to plug anything into." — Generality is the point: the demux/intent/action pipeline is the universal plug surface; the demo app is the current proof, not the ceiling.
- **North-star sharpening — SUPERSEDED 2026-08-02 by ratified ORG-061 (T-2390).** The TG-6330 sharpening (2026-07-22) read: *the voice interface's first-class target is the Pantheon agents … Echo is the primary human↔agent interface INTO the orchestration system; Telegram/text stay as fallback,* with the voice round-trip marked north-star-tier. **the operator retired that framing at TG 534:** it was Orchestrator's wording which he assented to at the time, and the current Grand Project decisions supersede it.
- **North-star sharpening (current — ORG-061 §1, ratified by the operator 2026-08-02):** the ordering is **Bridge then Echo**. Prove the Bridge's answer/delegate/synthesize contract **text-first** (Grand Project stage 4), *then* attach Echo as its **first-class voice substrate** (stage 5), whose exit evidence is a static-geometry microphone deployment sustaining multi-turn conversation, barge-in, identity/consent boundaries and spoken results. **What did NOT change, and ORG-061 requires it be said:** Echo's project north star above — a general voice interface to plug anything into — is **undiminished**. Echo is still the intended first-class voice line to the agents; what moved is the *sequencing and the proving surface*, not the destination. Read this as "the voice line is earned through a working text contract", not as a demotion of voice. The sequence is dependency-oriented and does **not** forbid earlier bounded Echo probes — only production reliance waits on the prerequisite's exit evidence. Superseded memory pin `project_echo_voice_line_to_pantheon` updated to match.
- **Phase:** Post-ADR-217/218/219 dashboard+tool-policy+FP arc merged and live. ADR-221 (partials correlation) + ADR-222 (typed classifier errors) adopted 2026-06-11, implementations in flight. Daemon-side merges batch into the next supervised restart.
- **Deferred / reserved:** Vault ADRs 001–199 (operator-authored). Chain-classifier flip to `adapter="chain"` in echo.toml is the operator's call. `*-working-<date>` tags wait on his live confirm.
- **Registered arc-intent — phone calls (the operator, msg 3770, 2026-06-12):** echo makes/takes phone calls as a conversation surface. Vault-anticipated at two levels (capture-context abstraction names "a phone session" as a non-acoustic source; Twilio/VoIP outbound calls = Technology Stack "Future idea" row) but never phased. **Gated on the CSM state-machine arc** (vault phase 4+: PROMPTED/CONVERSING transitions + timers + barge-in — unbuilt, itself an operator roadmap call per BACKLOG 2026-06-09 actualization; a phone call is pure multi-turn conversation, so the gate is structural, not scheduling). Groundwork survey of existing systems ticketed at registration.

## revenue-project — the income engine

- **North star (the operator, msg 3740):** "Have a set of recurring revenue streams that solve my income problem." — Plural streams; the target is his income problem, not merely Pantheon's burn (the ~$300–500/mo from the scaffold README is the floor milestone, not the goal). Automation-first, platform-risk-aware (MX-1..3).
- **Phase:** Research V1–V5 delivered + lifted; synthesis decision-gated on the operator reviewing the veins. **Actuation DEFERRED** (the operator msg 3510, 2026-06-08) — build tickets parked with `interactive-required`.
- **Deferred / reserved:** The un-defer ruling itself (goal-level). Real-money actuation, accounts, spend — all his. Orchestration shape pre-decided in ORG-032 (STATE 2 executes at his trigger; don't re-open).

## pantheon (org system) — the orchestration system

- **North star (the operator, msg 3740):** "Create a system of orchestration complex enough to pull off any project." — Capability ceiling is the goal: any project. msg 3733 describes the operating relationship (ideas → delegated creation → digests); the registry you are reading is part of this system.
- **Phase:** principles-as-ratification posture adopted + propagated (D3 amendment, 2026-06-11). Live surfaces: ticket system + ledger + PWA (ticket-UI/stats; chat sunset shipped — the 2026-06-13 headless-API billing cutoff passed, Telegram owns conversation), Telegram as chat surface. Harness-abstraction arc ORG-031 ratified 2026-06-09 (Option-A; codex onboarded as a dispatched worker) + ORG-032 revenue-project shape adopted 2026-06-11; harness-config distribution ORG-039 ratified 2026-06-30 (the operator, via T-1474; genericized reference template published private, T-1475 done — public flip stays the operator-held T-1473 gate); authority-provenance model ORG-040 adopted 2026-07-06 (HO, under amended D3); codex planning-phase spec review ORG-041 ratified 2026-07-11 (the operator TG 5624 — mandatory pre-Unit-1 gate for qualifying arcs); two-hemisphere topology ORG-042 ratified 2026-07-19 (the operator TG 6193 — Mnemosyne, second head, personal+creative domain) + sub-orchestrator seats ORG-043 ratified 2026-07-20 (the operator TG 6245 — Metis + Atlas, retask lane live; both first arcs complete 2026-07-21).
- **Deferred / reserved:** New always-on agents/accounts (goal-level: access + cost — and exercised as such: this trigger fired twice in one week, both operator-ratified: ORG-042 Mnemosyne, ORG-043 Metis+Atlas; the reservation held). Veneration substrate (T-0765, his lane today).

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
