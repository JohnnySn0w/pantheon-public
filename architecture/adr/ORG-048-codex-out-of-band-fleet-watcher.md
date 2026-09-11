---
aliases: [ORG-048]
id: ORG-048
title: "Codex as out-of-band fleet watcher/fixer: a separate-subscription dead-man switch for correlated Claude-cap failures"
status: "RATIFIED 2026-08-10 by the operator (ADR ratify walk, TG 7598; via ratify-ticket T-2041). Both gates ratified: (a) ROLE CHANGE — Codex from dispatched-worker + spec-reviewer to ALSO a standing self-directed watcher/fixer (amends ORG-031); (b) PRIVILEGE GRANT — scoped cross-account repair access, ratified WITH the disposition that RECOVERY is the supervisor for the supervised first-fire (the operator need not be present), that Codex's ORG-041 review of the §5 access model precedes the grant, and that codex being a sandboxed process (operator-confirmed, discussed directly with Codex) is part of the safety basis and the reason the Action-Broker convergence is the right end-state. The interim sudoers grant MUST converge to route through the Action Broker (ORG-052/063), not persist as a parallel grant. Build arc cascade-unblocked: T-2804 (role scaffold, steps 1-3) + T-2805 (privilege grant + scope-out, steps 4-5). HO-drafted 2026-07-26 from the operator-directed design capture (TG 6479), grounded on a five-leg substrate sweep."
date: 2026-07-26
# elements: — ORG-065 machine index of the "Decision" (D1-D5), "The
# privilege surface" (§5), and "Build sequence" (steps 1-5) sections
# (T-2946 Tranche A, pair 3/8). Three native enumerations collide on
# 1-5, so ids run sequentially in document order: E1-E5 == D1-D5;
# E6 == §5 privilege surface; E7-E11 == build steps 1-5. Each entry's
# text opens with its native id verbatim (D<n>/§5/step N) — that text
# is the audit anchor, not the E-number. Added by HO; no obligation
# content changed.
elements:
  - {id: E1, text: "D1: standing schedule — a systemd --user timer on the codex account firing a headless codex exec watcher run every 5 min"}
  - {id: E2, text: "D2: detection — reuse existing monitor logic hosted Codex-side (pane-scrape cap/wedge signatures, credential/disk monitors) plus a new fleet-heartbeat file as the positive correlated-cap tell"}
  - {id: E3, text: "D3: repair — a scoped allowlist of already-below-the-bar recovery actions (session/service restart, wake-drop unstick, settings restore, scoped model-pin); anything off-allowlist escalates only"}
  - {id: E4, text: "D4: escalation channel — Codex's own Telegram bot (Claude-independent); confirm live and wire as the watcher's escalation sink"}
  - {id: E5, text: "D5: scope — prove the four legs against Recovery first, then extend the same watcher to the rest of the standing fleet"}
  - {id: E6, text: "§5 (the privilege surface — the gating security design): narrow sudoers.d allowlisted command shapes, no credential-material reads, every repair action audited, Codex ORG-041 review before build"}
  - {id: E7, text: "step 1: D2 heartbeat file — non-privileged hook + poll; the positive correlated-cap detector; buildable ahead of ratify"}
  - {id: E8, text: "step 2: D4 channel confirm — verify Codex's Telegram bot is live and wired as escalation sink"}
  - {id: E9, text: "step 3: D1 timer — Codex-local systemd schedule firing the headless watcher"}
  - {id: E10, text: "step 4: [RATIFY-GATED] D3 allowlist + §5 sudoers — the privilege grant; Codex ORG-041 review first; supervised first-fire"}
  - {id: E11, text: "step 5: D5 scope-out — prove against Recovery, then extend to the fleet"}
---

# ORG-048 — Codex as out-of-band fleet watcher/fixer

**Status:** RATIFIED 2026-08-10 by the operator (ADR ratify walk, TG 7598; ratify-ticket T-2041). **Author of record:** Orchestrator (HO). **Dispositions:** both gates ratified — **(a) role change** approved; **(b) privilege grant** approved, with **RECOVERY as the supervised-first-fire supervisor** (the operator need not be present), Codex's **ORG-041 review of §5 first**, and **codex-is-sandboxed** (operator-confirmed, discussed directly with Codex) as part of the safety basis and the Action-Broker rationale. The interim sudoers grant **converges to the Action Broker** (ORG-052/063), not a parallel grant. Cascade-unblocks **T-2804** (role scaffold) + **T-2805** (privilege grant + scope-out).
**HITL:** Autonomous (agent-authored, unratified) → seeks **Ratified**. Two distinct things need the operator's sign-off: **(1) the role change** — Codex from dispatched-worker to also standing self-directed watcher/fixer (an autonomy-posture change); **(2) the privilege grant** — scoped cross-account repair access for the `codex` unix user (an access-posture change with an S3* precedent, §5). Neither is HO's to self-adopt.
**Source of authority:** The operator's directive TG 6479 (2026-07-26) — "add monitoring Recovery + being the critical-moment watcher/fixer to Codex's responsibilities," motivated by the 2026-07-26 correlated-cap incidents (Fable caps, Recovery budget-critical, the Mnemosyne-bridge recovery — ledger `2026-W30`), where the repairer was itself at risk of being capped.
**Grounded on:** `notes/2026-07-26-codex-out-of-band-fleet-watcher.md` (the design capture); a substrate sweep (2026-07-26) that verified the recovery-action inventory, detection surfaces, Codex's schedule substrate + escalation channel, and the S3* privilege lineage against live files. **Ticket:** T-2039 (requester=operator, ADR-worthy, interactive-required); absorbs T-2040 (telegram wake-drop, resolved into this).
**Amends:** [[ORG-031]] (harness-abstraction + daemon-onboarding — the home for Codex's role posture). **Relates to:** [[ORG-041]] (Codex planning-phase spec reviewer — the second existing role), T-1980 (Recovery's Claude-side wedge auto-step — the in-band complement), T-1734 / T-1598 (the S3* credential-reachability finding + its sandbox-profile remediation — the privilege guardrail).

## Why this ADR exists

Every standing Claude agent — **Orchestrator** (HO), **Recovery** (recovery operator), **Specialist**, **Mnemosyne**, the seats — runs on the **same Anthropic subscription**. A usage cap is therefore a **correlated failure**: when the shared sub caps (e.g. late in the weekly window), the *whole Claude fleet* goes down together — **including Recovery, the recovery operator, and Orchestrator, the orchestrator.** Nothing self-repairs until the sub week rolls over.

This is the structural flaw under *every* Claude-side recovery mechanism: **the repairer lives inside the thing that fails.** Recovery's recovery domain, the T-1980 model-stepper, HO-drives-recovery — all assume at least one Claude agent has headroom. A whole-sub cap violates that assumption exactly when recovery is most needed. The 2026-07-26 session made it concrete (three near-misses in one day).

**Codex runs on a separate subscription** — the `codex` unix user (uid 1003, home `/home/codex`), authenticated via ChatGPT OAuth (`/home/codex/.codex/auth.json`), a different provider and billing window from the Claude fleet's shared Anthropic sub. That separation makes Codex the one actor **still alive when the entire Claude fleet is capped** — a genuine **out-of-band dead-man switch**. This ADR turns the operator's directive into a concrete, security-first design.

## Context — what is already fixed (do not design over it)

1. **Codex today has two roles:** a **dispatched worker-harness** (ORG-031 Option A, via the MCP tool + the ACP-bridge mailbox drain) and a **planning-phase spec reviewer** (ORG-041). Its AGENTS.md contract explicitly states it is *"NOT a peer orchestrator — you do not run your own standing arcs; you execute dispatched tasks and return."* This ADR's role change is precisely an amendment to that clause.
2. **The account separation is real and is the whole basis of the design.** `/home/codex` is permission-isolated from `/home/claude`. The `codex` account holds its own OAuth token, SSH key, and — load-bearing for leg 4 — **its own Telegram bot** (`/home/codex/.config/codex-telegram/.env`), separate from the Claude agents' plugin bridges.
3. **The repair procedures already exist and are D3-classified.** Recovery's `sop/remediation/` set defines every recovery action at command level, each already partitioned into autonomous-vs-bubble-up. Codex does not invent new recovery; it inherits a scoped subset of the *existing, proven* procedures.
4. **The privilege surface has a precedent warning.** The S3* audit (S3STAR-2026-07-13-003 / T-1734) found that a cross-account worker could **read** Codex's own credential-bearing files; remediated by the T-1598 Phase-2 sandbox profile (deny-reads, production 2026-07-14). Cross-account credential reachability is a *High-severity* finding in this org — the access model here is designed against that precedent, not in ignorance of it.
5. **This complements, does not replace, T-1980.** The Claude-side auto-step handles individual/idle wedges *while the fleet has headroom*. Codex handles the **whole-sub-cap** moment the Claude-side mechanisms structurally cannot. Two tiers, different failure domains.

## The load-bearing constraint (from the operator's directive)

**Codex's watcher must depend on ZERO Claude agents** — or it re-inherits the correlation and dies exactly when needed. All four legs must be Claude-independent: **(1)** own standing schedule, **(2)** own detection, **(3)** own repair capability + access, **(4)** own channel to the operator. The Decision section resolves each leg; the sweep found legs 2 and 4 largely already satisfied and legs 1 and 3 the genuine build.

## Decision

### D1 — Standing schedule: a systemd `--user` timer on the `codex` account (MUST-BUILD)

Codex today runs a `Restart=always` supervised tmux session (`codex-supervise.sh`) — a **liveness supervisor, not a scheduler**. The watcher gets its own cadence from a **systemd `--user` timer on the `codex` account** — the same shape the Claude fleet's monitors use (`tools/systemd/*.timer`), but hosted on Codex's substrate so no capped Claude dispatches it. The timer fires a **headless `codex exec` watcher run** on a fixed interval (lean: **5 min**, matching `context_fill_monitor`'s cadence).

*Justifies-via:* the load-bearing constraint (leg 1) — a dispatcher that might be capped cannot fire the watcher, so the schedule must live on the surviving substrate. *Build caveat (grounded):* `bugs/codex-exec-blocks-on-open-nontty-stdin.md` — the watcher's `codex exec` invocation must close/redirect stdin (`</dev/null`) or it blocks. *Decides autonomously at build:* exact interval, timer vs cron. *Bubbles up:* nothing at this leg — it's Codex-local infrastructure.

### D2 — Detection: reuse the existing monitor logic, hosted Codex-side, plus one new fleet-heartbeat file (MOSTLY-EXISTS + one build)

The sweep found a **rich detection substrate already exists** — but almost all of it runs on **Recovery's** account and/or routes its *remediation* through a Claude agent, so it dies in the correlated-cap case. The detection *logic* is sound and portable; the fix is **where it runs**, not **what it computes**. Codex's watcher detects fleet-state from primitives that survive a Claude cap:

- **Pane-scrape for the cap/wedge signatures** — the spend-limit modal (`monthly spend limit`), the wake-drop stuck-input signature (non-empty input box + idle footer `? for shortcuts`, not `esc to interrupt`), the 429 capability-cap, sustained 401 auth-outage. These are the exact signatures `context_fill_monitor.py` / `activity_monitor.py` already encode; Codex reads them via `sudo -n -u <agent> tmux capture-pane` (a **read-only** pane scrape — see §5 for why this is the scoped-narrow surface, not credential read).
- **Credential + resource signals that already survive an outage** — `credential_expiry_monitor.py` and `disk_usage_monitor.py` are pure file/proc reads that alert via the **Telegram bot token, independent of Claude OAuth**. Their *outputs* are Codex-consumable as-is.
- **NEW (MUST-BUILD): a fleet-heartbeat file.** No purpose-built "agent-alive heartbeat that goes stale on cap" exists today (confirmed — the design note flagged it, the sweep verified the gap). Each standing agent writes a timestamped heartbeat (lean: a Stop-hook touch of `~/.local/state/pantheon/heartbeat/<agent>.json`) while it is taking turns; a whole-sub cap freezes *all* of them at once. Codex polling that directory sees the correlated-freeze signature directly — the cleanest positive detector of the exact state this ADR exists for. This is a small, **reversible, non-privileged** addition (a hook + a poll), buildable ahead of the ratify-gated legs.

*Justifies-via:* leg 2 — Codex must detect the fleet-down state itself, not via a Claude-side signal. The heartbeat-freeze is the one unambiguous correlated-cap tell; pane-scrape covers the individual-wedge and wake-drop cases folded in from T-2040. *Decides autonomously:* heartbeat file schema, poll interval. *Bubbles up:* nothing — detection is read-only.

### D3 — Repair: a scoped allowlist of the existing autonomous-tier recovery actions (MUST-BUILD the access; procedures EXIST)

Codex executes **only** the recovery actions the existing SOPs already classify as *below the autonomy bar* — the proven, reversible set. Concretely, the allowlist is:

- **Session restart** — `sudo -n -u <acct> tmux kill-session -t <session>` → the `Restart=always` supervisor respawns a fresh session (new PID, fresh bridge, dismissed modal). The true cure for alive-but-400ing / wedged-TUI / dead-bridge.
- **Service restart + crash-loop clear** — `systemctl --user restart <unit>`, `reset-failed` then restart. *(Encodes the load-bearing gotcha: `restart` is often a NO-OP on the inner `claude` due to the supervisor's idempotent `tmux has-session` guard — proof-of-restart is a changed inner PID via `pgrep`, never `is-active`.)*
- **Wake-drop / spend-modal send-keys unstick** — the T-2040 fix: `tmux send-keys C-c` → re-type the stuck message as fresh keystrokes → `Enter` (the re-type is load-bearing; a plain Enter does not submit injected text). The single highest-value action, because the moment it's most needed is exactly when the fleet is capped and cannot unstick itself.
- **Settings restore-from-backup** — restore `<file>.bak.<TS>` (atomic jq-validate-then-`mv`), or `/etc/claude-recovery/restore.sh` for the known-good.
- **Scoped model-pin** — the `model_stepper.py` semantics (durable supervisor `--model` pin edited *before* the live `/model` inject, context-fill paused across the switch). **Gated:** autonomous only for the *cap-recovery* direction (capped-flag → Opus); a model *choice* as a goal call stays the operator's.

**Explicitly escalate-only (above the bar — Codex alerts the operator via its own Telegram bot, never acts):** OAuth `/login` re-auth (browser auth is the operator's; copying credentials between accounts is forbidden), spend-limit *raise* (billing = the operator's), config *edits* (vs restore-from-known-good), and anything not on the allowlist above.

*Justifies-via:* leg 3 + the D3-per-SOP autonomy classification already ratified for Recovery — Codex inherits the *same* below-the-bar set Recovery runs, no broader. *Bubbles up:* any repair not on the allowlist; any action that fails twice (don't loop).

### D4 — Escalation channel: Codex's own Telegram bot (EXISTS — confirm live, do not build)

Codex already has a **Claude-independent channel to the operator**: its own Telegram bot on the `codex` account (`/home/codex/.config/codex-telegram/.env`), on the separate ChatGPT/OpenAI sub. It survives a whole-Claude-sub cap by construction. This leg is **effectively already satisfied** — the build task is to **confirm the bot is live and monitored** and wire it as the watcher's escalation sink, not to stand up a new channel.

*Justifies-via:* leg 4 — Codex can always reach the operator out-of-band to escalate what it can't (or won't, per D3) fix.

### D5 — Scope: start at Recovery, extend to the fleet (as the operator framed it)

Begin exactly where the operator pointed: **watch Recovery.** Recovery is the recovery canary — if Recovery is capped, that *is* the no-Claude-recovery moment. Prove the four legs against Recovery first (detection freeze → allowlisted repair → escalate-if-stuck), then extend the same watcher to the rest of the standing fleet and to the shared-sub-cap state directly. This keeps the first live exposure narrow (one target, one supervised first-fire) before generalizing.

*Justifies-via:* the supervised-first-fire discipline (narrow blast radius on a new autonomous actor) + the operator's explicit framing.

## The privilege surface — the gating security design (§5)

Standing repair access means the `codex` user can restart services, kill sessions, and send keystrokes **as other agents' unix users**. That is a real privilege surface, and the S3* precedent (T-1734) is the specific warning: cross-account credential *reachability* is a High-severity finding here. The access model is therefore **least-privilege by construction**:

- **Narrow `sudoers.d` entries, not blanket sudo.** Codex gets `NOPASSWD` for *exactly* the allowlisted command shapes in D3 (`tmux kill-session`/`send-keys`/`capture-pane` for the named panes, `systemctl --user restart/reset-failed` for the named units, the restore script) — enumerated commands, not `ALL`.
- **No read access to other agents' credential material.** The scope is process/session control + read-only pane scrape, **not** `.credentials.json` / `auth.json` / SSH-key readability. The T-1598 Phase-2 posture (deny-reads on credential paths) is the model to preserve, inverted: Codex's grant must not re-open the exact reachability that audit closed.
- **Audited.** Every Codex repair action logs to the ledger (or a dedicated `codex-watcher` audit trail) — the same auditability every recovery action carries today.
- **Codex ORG-041 spec review before build.** Per the ORG-041 lane, Codex reviews this ADR's plan (the access model especially) at the planning phase, before any privilege is granted.

The security posture is the gating design work, not an afterthought — this section is normative, not advisory.

## Relationship to existing work

- **ORG-031** — the home for the role amendment; this ADR adds a *third* Codex role (dispatched worker + spec reviewer + **standing autonomous watcher/fixer**).
- **T-1980** (Recovery, Claude-side wedge auto-step) — the in-band complement; Codex is the strictly out-of-band tier for the correlated-cap domain T-1980 cannot cover.
- **T-1734 / T-1598** — the S3* credential-reachability finding + its sandbox remediation; the privilege guardrail §5 designs against.
- **T-2040** (telegram wake-drop) — resolved into this ADR; its detection+auto-unstick is D2's pane-scrape + D3's send-keys action.
- The 2026-07-26 correlated-cap incidents (ledger `2026-W30`) — the motivating evidence.
- **ORG-052 / ORG-063 (Action Broker) + ORG-051 (Auth/credential broker)** — cross-referenced per the operator (TG 2026-08-10): the repair capability and the broker roadmap must not diverge. This ADR's privilege grant (§5 + the D3 `sudoers.d` allowlist) is a **bespoke, interim instance of the Action Broker's host-capability authority** — ORG-063 defines the Action Broker as the single deterministic UID to which seats route real host actions, and Codex's cross-account repair access is exactly such an action-set. It **MUST converge to route *through* the Action Broker once that is built**, not persist as a permanent parallel `sudoers.d` grant: this ADR is the stepping-stone, and the D3 allowlist is the concrete action-set the broker later mediates. Symmetrically, §5's **no-credential-reads** boundary is the **Auth/credential broker's (ORG-051) domain** — Codex's grant governs process/session control, never credential material, preserving the broker's custody separation. When the Action Broker lands, ORG-048's step-4 grant is superseded by (not duplicated alongside) the brokered path.

## Build sequence (post-ratify)

Ordered so the reversible, non-privileged pieces land first and the ratify-gated grants last:

1. **D2 heartbeat file** (non-privileged; hook + poll) — buildable ahead of ratify; the positive correlated-cap detector.
2. **D4 channel confirm** — verify Codex's Telegram bot live + wired as escalation sink.
3. **D1 timer** — Codex-local schedule firing the headless watcher.
4. **[RATIFY-GATED] D3 allowlist + §5 sudoers** — the privilege grant; Codex ORG-041 review of the access model first; supervised first-fire.
5. **D5 scope-out** — prove against Recovery, then extend to the fleet.

Steps 1–3 are Codex-local and reversible and may proceed on ratify of the *role* direction; step 4 waits on the explicit *privilege* grant.

**Build tickets (filed 2026-08-10, gated on ratification T-2041 so they don't auto-fire pre-ratify):** steps 1–3 → **T-2804** (role-gated watcher scaffold); steps 4–5 → **T-2805** (privilege-gated grant + scope-out — Codex ORG-041 review first, then supervised first-fire). Ratifying ORG-048 cascade-unblocks these, so the ratification triggers tracked work.
