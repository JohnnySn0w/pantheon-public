---
aliases: [ORG-063]
title: Full-perms harness posture — the UID/OS boundary is the security cordon
status: "Ratified 2026-08-12 by the operator (T-2657 retarget comment 03:11 UTC: 'ratified with the above comments/changes') — both OPEN items dispositioned at ratify: invariants+health rehoused to ORG-068 with pointers; the future control seat = the Bridge persona (head seat), down the line. Rewrite of 2026-08-11 (posture-only)."
date: 2026-08-07
rewritten: 2026-08-11
author: Orchestrator (Orchestrator)
relates: ORG-052, ORG-031, ORG-050, ORG-051, ORG-064, ORG-049
build_disposition: needs-build
scoping_ticket: T-2653
# elements: — ORG-065 machine index of the "Implementation ticket chain"
# section (T-2946 Tranche A, pair 1/8). E<n> == chain position n (the
# section's bulleted ticket order, 1:1); text opens with the native ticket
# id verbatim — the ADR body's ticket-chain bullets remain authoritative.
# Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "chain-1 (T-2652): build the Action Broker — modular host-capability broker, single deterministic UID; head of the chain"}
  - {id: E2, text: "chain-2 (T-2653): unblock all harnesses (full-perms) + standardize user perms; depends_on T-2652"}
  - {id: E3, text: "chain-3 (T-2654): UID-cordon audit + tightening — specialist pilot; near-term stopgap track, gates specialist's bypassPermissions flip"}
---
# ORG-063 — Full-perms harness posture: the UID/OS boundary is the security cordon

**Status:** **RATIFIED 2026-08-12 by the operator** (T-2657 retarget comment, 03:11 UTC: *"ratified with the above comments/changes"*) — his two comments applied: the §4b invariants + §7 health interface are **rehoused in ORG-068** (see §OPEN items below, now resolved), and the future control seat is answered as **the Bridge persona (head seat), down the line**. This is the 2026-08-11 rewrite of the prior bundled draft, cut to its one genuinely-new decision — the security posture — per the operator's steer (TG 7608–7618). Direction: the operator TG 7226–7255 (the posture + rulings) + TG 7608–7618 (de-bundling + repair-tier and codex-sandbox refinements).

## Intent

Canonicalize **one** security posture for running persona seats: stop gating power at the harness permission-prompt, make the UNIX **UID/OS** the real boundary, and route brokered sensitive host actions through the Action Broker — so seats never wedge on a permission prompt, and the security boundary is explicit, deterministic, and auditable. This serves P1 (the boundary is a deliberate OS artifact, not a per-harness allowlist kept in sync by hand) and the D1-adjacent operational goal of unwedging the fleet.

## Context

Surfaced by the T-2633 specialist control-plane cutover: specialist reached full bringup but **wedged on a Telegram-reply permission prompt** — the harness launched under `--permission-mode default` and its allow-list path diverged from what the bespoke launch loaded. The operator's framing (TG 7226): each harness handles perms differently (config file vs CLI flags), and a harness wedges on any perm not granted via its **pre-daemon-start** method. The operator then named the cleaner path (TG 7227/7230 + the loginasroot.net root-login FAQ he circulated): **flip full-perms flags on all units and rely on user-level (UID) restrictions to cordon actual power.** The load-bearing idea: in-harness permission prompts are the cargo-culted layer; the real boundary is the UNIX user — what powerful resource sits behind a UID — and it must be built deliberately.

## The decision — one posture, four layers

**1. Harness runs unblocked (per-harness).** Every seat launches with its harness's no-prompt/full-auto setting so it never wedges. This is a **per-harness mapping, not a single flag** — a thin adapter (the ORG-031 `harness_adapter`) maps a neutral "full-auto" intent onto each harness's actual controls:
  - **Claude:** `--permission-mode bypassPermissions`.
  - **Codex:** `approval_policy: never` — **and note codex additionally runs its own OS sandbox** (`sandbox_mode: workspace-write`, bubblewrap-enforced) that restricts writes to enumerated `writable_roots` and gates network **regardless of the approval setting**. *(Amended 2026-10-12, the operator TG 11733/11737: codex runs `sandbox_mode: danger-full-access` by design. The harness sandbox is NOT a layer we keep, because permissions operate at the unix-user level, not the harness level; see §2.)* Originally: that sandbox was described as an added layer we keep, not something the unblock removes; codex separates approval (when it prompts) from sandbox (what the OS allows), plus per-MCP-tool approval and hook-trust — so "unblock codex" is several settings, not one switch. (Verified vs `tools/harness_adapter/codex.py` + `knowledge/watch/reports/2026-07-07/codex-cli-sandbox-profiles-spec.md` + `codex doctor`.)

  **Load-bearing distinction (the operator, TG 7614–7615):** unblocking the harness drops the harness **prompt** layer ONLY. It does **not** grant OS power. A fully-unblocked seat can still only do what its UID is granted — the flag stops the prompting; the UID is still the wall.

**2. The UID/OS grant is the real cordon.** Once the harness stops gating, the UNIX user is the boundary: files, group membership, sudo, reachable sockets. This must be built deliberately and audited (see §UID-cordon audit) — it is the ONLY boundary once the prompts are gone.

**3. The broker gates brokered sensitive ops (normal seats).** Normal seats hold minimal OS power and route real host actions through the **Action Broker (ORG-052)** — a single deterministic privileged UID behind a privilege-free MCP adapter over system D-Bus (no model session, no per-seat bearer secret, no network listener). Sensitive host capability lives in the broker's auditable code, not spread across daemon-user grants.

**4. The watch/repair tier is the exception — standalone, broker-independent privilege (the operator, TG 7614).** Repair personas (Recovery) hold **asymmetric direct sudo that does NOT route through the broker.** Rationale: **the recovery tier cannot depend on the thing it recovers** — a broker-as-repair-dependency makes the broker a single point of failure for recovery. The repair tier sits **above** the broker, not behind it. This is the concrete content of the posture's "few exceptions": the exception set is the watch/repair tier, deliberately broker-independent, not a vague carve-out. (ORG-052 already leans this way — it calls host-broker ops an "exceptional recovery surface" — but the sharper point is that the *meta*-recovery tier can't sit behind the broker at all.)

## The UID-cordon audit — the load-bearing prerequisite

Once the harness stops gating, the UID cordon is the ONLY boundary, so it must actually be tight. Today it is **not audited**: daemon users share groups (`pantheon-shared` / `pantheon-mailbox` / `veneration`) and some hold sudo. Flipping full-perms without this audit = a full-auto agent on a loose boundary. The work: audit each daemon user's actual power, define the intended per-seat blast radius, tighten each UID to match. **This is where "cordon off actual power" actually happens, and it is a HARD prerequisite** — not a follow-up — before full-perms becomes fleet-wide.

Sequenced (the operator, TG 7237): **specialist is the pilot** cordon-audit (the seat mid-cutover). Flag home tracks cordon maturity — **per-role flag NOW** (conservative; only flip seats already cordoned) → **harness-manager global-default** (full-auto everywhere, per-seat opt-out) once the cordon is comprehensive.

## What this ADR does NOT specify — the already-homed components

This posture is *implemented by* subsystems that each already have their own ADR. This ADR **references** them; it does not re-specify them (that re-bundling was the prior draft's flaw):

- **Action Broker** → **ORG-052** (harness-neutral host-capability broker with MCP adapters over system D-Bus). Layer 3's "brokers gate sensitive stuff."
- **Harness Manager** — the "how a seat runs" umbrella (`persona_installer`, `runtime_controller`, `persona_bringup`, `harness_adapter`, `instance.yaml`) — is itself an umbrella over subsystems already covered by **ORG-031** (harness-abstraction adapter + generalized daemon-onboarding), **ORG-050** (security/identity contract for installer + bringup), and **ORG-051** (secret & authentication lifecycle). Layer 1's full-auto-flag mapping is a thin addition to the ORG-031 `harness_adapter`; it is not a new subsystem.
- **Germination engine** → **[[ORG-080]]** (Ratified 2026-08-26). The apex lifecycle composer that installs, configures, updates and removes every component, including the Harness Manager, which it composes as the `harness-manager` component. The engine holds no capability of its own ([[ORG-080]] D-B), so ruling 2's isolates stand: substrate ops run on the bootstrap in-process root path and runtime ops through the Action Broker ([[ORG-082]]).
- **Scheduler** — the "when things run" isolate — → **ORG-064**. Peeled out of the prior draft's §Scheduler; unaffected by this rewrite.

## the operator's rulings (carried forward — TG 7234 + 7255 + 7614)

1. **specialist: HOLD** the `bypassPermissions` flip until the UID-cordon (specialist pilot) lands. The reply perm currently goes through via interactive approval, which is not durable across a session restart; the durable fix is the cordon-gated flip.
2. **Modular isolates, confirmed** (TG 7255): the Action Broker (ORG-052) and the Harness Manager subsystems are independently-supervised isolates, neither enmeshed in the other — enmeshing = one subsystem holding both seat-lifecycle power and host-capability power = the blast radius ORG-052 exists to collapse.
3. **Full-perms flag home: sequenced** — per-role now → harness-manager global-default once the cordon is comprehensive (ruling 3 above).
4. **Repair tier is broker-independent** (TG 7614): the watch/repair tier keeps standalone asymmetric sudo so recovery never depends on the broker.
5. **Codex sandbox confirmed** (TG 7616–7618): codex's OS sandbox holds regardless of the approval flags; "remove the harness blocks" is the prompt layer only, and is per-harness.

## Consequences

- **Positive:** no harness wedges; one boundary model regardless of harness; directionally the ORG-052 end-state, not a throwaway.
- **Cost/risk:** full-auto agents on the current (un-audited) UID boundary until the cordon audit lands — so the **UID-cordon audit is a hard prerequisite**, not a follow-up, before this becomes fleet-wide policy.
- Recovery gains a runtime **wedge-monitor** backstop (detect a seat stuck on a perm prompt, surface/handle) for anything the pre-start flag misses.

## Alternatives considered

- **Harness-neutral GRANTS rendering** (a manifest allowlist the harness manager renders into each harness's config with parity): retired — it rebuilds the per-harness parity machinery that full-perms + UID-cordon makes unnecessary.
- **Enmeshed broker** (host authority inside the harness manager): rejected (ruling 2) — collapses the two power classes into one blast radius.
- **Keeping this as a 2–3-component bundle** (the prior draft): retired per the operator TG 7608–7618 — the components already have homes; only the posture is new.

## OPEN items — RESOLVED at ratify (the operator, T-2657 comment, 2026-08-12)

1. **Modular-subsystem invariants + health interface → REHOUSED in ORG-068** per the operator's ruling ("rehouse them under the rest of the lifecycle/health adr work instead of here, but point to them"): the §4b invariants (health-legible / restart-fixable / stateless-across-sessions) and the §7 uniform D-Bus health-property surface now live in **ORG-068 §Modular-subsystem invariants + uniform health interface**, recovered verbatim from this ADR's pre-rewrite revision (`1f9c2d43ac^`). This ADR *points* there: any subsystem this posture governs must meet that contract.
2. **Forward-looking control component → ANSWERED**: the future higher-tier "who orchestrates the orchestrators" seat **will be the head seat — the Bridge persona — down the line** (the operator, same comment). Stays deferred as build; no longer an open design question. Connects to the ORG-061 Bridge-then-Echo sequencing and the T-2025 Bridge-persona groundwork.

## Implementation ticket chain (the operator TG 7237)

- **T-2652** — build the Action Broker (ORG-052 end-state). Head of the chain.
- **T-2653** — unblock all harnesses (full-perms) + standardize user perms. `depends_on T-2652`. *(This ADR's `scoping_ticket`.)*
- **T-2654** — UID-cordon audit + tightening, **specialist pilot** — the near-term stopgap track (not broker-gated); gates specialist's `bypassPermissions` flip.

## Re-evaluate when

- The UID-cordon audit completes — its findings may reshape the posture's stopgap→end-state sequencing.
- The Action Broker (ORG-052) reaches implementation — this posture's stopgap retires into it.
- The operator dispositions the §OPEN-1 modular-invariants/health-interface relocation.

## Amendment — 2026-10-09 (re-eval of ORG-063:S2, T-4741)

**Approved by the operator via Telegram 11611 ("1. ok"), in reply to Orchestrator's amendment request.** This updates the text to a decision that was already ratified. It adds nothing new, and the trigger text above (ORG-063:S2) is left as ratified.

The second re-evaluate trigger says the posture's stopgap "retires into" the Action Broker. That was true when written, but [[ORG-082]] (ratified 2026-08-26, TG 8612) has since split where the stopgap retires to:

- **Substrate operations** (account create/retire, allowlisted-group membership, sudoers fragments, system units, mailbox ACLs) retire into the **bootstrap in-process root path**. That is a supervised, operator-run leg. These operations are structurally absent from the broker daemon's method table ([[ORG-082]] §1, D-B).
- **Runtime brokered host actions** still retire into the Action Broker ([[ORG-052]]), which exposes only the reviewed runtime subset ([[ORG-082]] gate 32a).

Read S2 as firing when **both** destinations reach implementation. The posture itself does not change: full-perms harnesses, with the UID/OS boundary as the cordon.
