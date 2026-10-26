# Seat parity — SOP (guidelines + operational procedures)

**Landed:** 2026-09-16 (T-3747, ORG-099 Ratified).
**Owner:** orchestrator (spec + manifests); recovery (audit operator + per-seat parity restoration).
**Authority:** ORG-099 (definition + axis taxonomy + carve-out shape + upgrade paths).
**Manifests:** `axes.yaml` (parity axes) + `carve-outs.yaml` (declared non-parity).

This SOP is the operational-guideline half of ORG-099. Rationale + reasoning lives in the ADR; step-by-step how-to-do-it lives here.

---

## Overview

**Seat parity** = every seat's capability surface matches the fleet baseline OR carries a declared carve-out. A seat's parity is checked against `axes.yaml`; legitimate exceptions live in `carve-outs.yaml`.

**Three parity states** (per ORG-099 §2):
- **FULL** — every HARD axis passes, every declared carve-out verified, no undeclared deviation.
- **PARTIAL** — HARD axes pass, some SOFT axes lag with no carve-out, no HARD gap. Operational; soft-lag > 30d triggers REBASELINE ticket.
- **BROKEN** — a HARD axis fails without a declared carve-out. Seat cannot do its persona's work.

**Directional bias:** alarm-biased, always. Silent-green is the failure mode this SOP exists to prevent (gaps G1/G2/G4/G9/G13/G14 in the audit corpus). If a check can't distinguish "axis absent" from "I could not measure," report as ALARM, not pass.

---

## When a seat is BROUGHT UP

Bring-to-parity checklist (procedural). Runs against a specific seat; produces per-axis outcome record.

1. **Confirm the seat's declaration.** Grep `instance.yaml` for role → persona → account binding. Confirm `harness`, `domain`, `session`, `restart_service` fields present. If missing, this is a provisioning-declaration gap, not a parity gap — resolve in the provisioning arc first.

2. **Run the current axis manifest against the seat.** For every axis in `axes.yaml` where `applies_to` is `all-seats` OR includes this seat's role-id:
   - If a carve-out exists in `carve-outs.yaml` for this (seat, axis) — verify the carve-out's `verifier` check passes. Record `CARVE_OUT_VERIFIED` or `CARVE_OUT_BROKEN`.
   - Otherwise — run the axis's `check_method` + `check_ref`. Record `PASS`, `FAIL`, or `UNMEASURABLE` (never conflate `FAIL` with `UNMEASURABLE`).

3. **Report outcome by tier.** HARD FAIL / UNMEASURABLE = seat is BROKEN, cannot go live. SOFT FAIL / UNMEASURABLE = seat can go live but files a per-axis remediation ticket. BILLING mismatch = declaration bug, block until reconciled. DOMAIN mismatch = declaration bug, ditto.

4. **First-live-turn canary** — a real turn that exercises the axes the persona will use. Not a bare "hello" — actually invoke the chat surface, the mailbox send, a test tool call. Verify no unattended-permission prompt reaches the operator (gap G6 field record: archivist 2026-07-20 caught by the operator TG 3833).

5. **Record baseline.** On successful FULL-parity bringup, record the seat's baseline in the ledger as `action: seat_bringup_to_parity` with the full axis outcome table + carve-out verifications + first-live-turn evidence.

Full-parity failure ≠ retry-in-a-loop. Every HARD FAIL is a seat-substrate ticket; the bring-up-operator (usually recovery) files them + reports to orchestrator.

---

## How to DETECT DRIFT (fleet audit)

Fleet audit = re-run every check against every live seat, produce diff vs last-known-good baseline.

1. **Run the current manifest.** For each seat in `instance.yaml`, for each axis in `axes.yaml`, execute per §When-a-seat-is-brought-up step 2. The canonical invocation (post-T-3755, 2026-09-16) is `parity_render.py --fleet` run from Recovery's account: `sudo -u claude-fallback -H bash -c 'cd ~/pantheon-config && python3 tools/parity_render.py --fleet'`. Recovery has the cross-account sudo grants + the operator-role authority for the audit; running from other accounts yields UNMEASURABLE on peer-seat axes (the audit-account can't reach peer XDG_RUNTIME_DIRs / user-buses via the tool's own sudo-wrap). Confirmed in T-3755 verification: orchestrator-side run yields U-everywhere for peers; Recovery-side run yields real per-cell measurements.

2. **Emit a fleet audit table.** Rows = seats, columns = axes, cells = PASS / FAIL / UNMEASURABLE / CARVE_OUT_VERIFIED / CARVE_OUT_BROKEN. Also emit JSON envelope per T-2372 shape (`{axis_id, seat, verdict, timestamp, evidence}`) for machine-readability.

3. **Diff vs last baseline.** Any NEW FAIL or NEW UNMEASURABLE = drift. Any CARVE_OUT_BROKEN = a legitimate carve-out has silently degraded (the carve-out's `verifier` failed; the exemption may no longer be in effect and the axis is now applicable again). File remediation tickets per gap; seat-owner drives fix; orchestrator reviews spec-tier questions.

4. **Cadence.** Recovery runs the audit on schedule (T-3747 BT3 wires this up per `tools/delivery_reachability.py`'s armed 6-hourly pattern, or looser if the load doesn't justify). On any operator-flagged concern, run ad-hoc.

5. **The drift-detection rule that matters most (per audit corpus §4 methodology):** **substrate check ≠ declaration check.** Never trust the classification-registry (`instance.yaml`'s declaration) alone; verify the substrate (systemd unit actually exists on the seat's own user-manager, MCP tool actually callable, mailbox send actually reaches). Every axis's `check_method` in `axes.yaml` is chosen to hit substrate, not declaration.

---

## How to CLOSE A SEAT DOWN

Per ORG-068 gap 4 (T-2889 open — full-seat decommission is not yet a proven mechanic in the persona_installer CLI). Interim procedure until T-2889 lands:

1. **File a decommission ticket** target=role:head_system_orchestrator, note the ORG-068 gap-4 hand-run status. Recovery drives.
2. **Retire the seat's carve-outs** — set every carve-out for the seat to `verdict: retired` (don't delete; audit-chain preservation) or move to a `retired/` block in `carve-outs.yaml`.
3. **Retire the seat from `instance.yaml`** — move under a `retired_roles:` section (don't hard-delete; ledger-checkers may need to resolve historical persona references).
4. **Substrate teardown per ORG-068 remove-stage** — systemd units stopped + disabled + unit files removed, mailbox dirs archived, home-dir preserved-or-archived per the operator's call, tmux session killed.
5. **Ledger the decommission** with `action: seat_decommission` + full pre-teardown state snapshot.
6. **Fleet audit re-run confirms the seat is off the roster** — no phantom rows, no green-forever cells.

---

## Adding a new axis (upgrade path — expand)

1. **Draft the axis** — fill the schema fields in `axes.yaml` (axis_id, tier, description, applies_to, source_of_truth, check_method, check_ref, gap_history).
2. **If tier is HARD:** file a ticket target=operator for ratify — HARD tier defines what a seat IS, warrants explicit y/n. If SOFT/BILLING/DOMAIN: orchestrator-autonomous per `feedback_infra_design_autonomous_with_corpus_check_msg10007`.
3. **Version bump `manifest_version` in `axes.yaml`.**
4. **Render check scripts** (once BT2 exists) — new axis gets its check script generated.
5. **Run fleet audit** with the new axis — every seat gets a first measurement; seats failing the new axis file remediation tickets (or declare carve-outs).
6. **Surface a lean** to the operator summarizing: axis added + fleet-audit result + who's failing + remediation plan.

---

## Retiring an axis (upgrade path — contract)

1. **Confirm no seat depends on the axis.** Grep `carve-outs.yaml` for any carve-out referencing the axis; grep `instance.yaml` + tickets for pending remediations against the axis.
2. **File the retirement ticket** target=role:head_engineering_orchestrator (SOFT/BILLING/DOMAIN) or target=operator (HARD, ratifiable).
3. **Move the axis to a `retired_axes:` block in `axes.yaml`** — don't hard-delete; audit history preservation. Bump `manifest_version`.
4. **Render check scripts re-run** — retired axis no longer appears in per-seat check output.
5. **Fleet audit re-runs** — the axis's column disappears from the audit table. Gap G20 (green-forever cell after retirement) is prevented by generating audit output from current manifest, not accumulating.
6. **Surface a lean** to the operator: axis retired + why + downstream impact.

---

## Propagating a fleet-standard change (upgrade path — shift)

Example: XDG portal mask (T-3722, three silent-failure recurrences before fleet-wide adoption). Pattern:

1. **Land the change on the fleet-authoritative source** (SOP file, hook script, config template) — same-day.
2. **Update the axis's `source_of_truth` + `check_ref`** in `axes.yaml` to reflect the new baseline. Bump `manifest_version`.
3. **Fleet audit re-runs** — seats not-yet-updated show as drifted.
4. **File per-seat remediation tickets** — recovery's cross-account substrate authority drives the propagation. Cadence: within 7 days for SOFT, immediate for HARD.
5. **Baseline refresh** — once all seats caught up, re-record the baseline.

---

## Ownership + escalation

- **Spec (ORG-099 + `axes.yaml` + `carve-outs.yaml`):** orchestrator. Amendments per §Adding-an-axis / §Retiring-an-axis.
- **Per-seat parity restoration:** recovery (recovery holds cross-account substrate authority). Recovery acts on remediation tickets audit surfaces.
- **Fleet audit operator:** recovery (runs the scheduled audit; hosts the report emission). Specialist may run supplementary domain-specific audits.
- **Ratify (HARD-axis additions, tier-taxonomy changes, carve-out-shape changes):** operator.
- **Escalate to orchestrator:** any drift-detection UNMEASURABLE that doesn't fit an existing carve-out verdict — spec-tier question about how to classify.
- **Escalate to operator:** cred/security envelope shifts, public-flip-adjacent axes, financial-tier axes (per `feedback_infra_design_autonomous_with_corpus_check_msg10007` carve-out classes).

---

## Cross-references

- **ORG-099** — parity ADR (definition + tier taxonomy + carve-out shape + upgrade paths).
- **ORG-068** — universal system-lifecycle contract (install/configure/monitor/update/remove). Parity SITS ALONGSIDE lifecycle.
- **ORG-042** — two-hemisphere topology (archivist = second-head = full-parity seat with hemisphere-domain partition).
- **ORG-031** — harness abstraction (KEEP-SPECIFIC for permissions/sandbox/doctor per T-2372 harness-parity-audit).
- **T-2372** — 2026-07-29 harness-parity-audit (Codex/Consultant). Canonical prior parity work; methodology + envelope shape adopted here.
- **T-2232** — 2026-07-28 fleet delivery-reachability audit (Recovery). Canonical substrate-check example.
- **T-3747** — this SOP's scoping ticket.
- **T-3748** — implicit-corpus-awareness structural build (upstream of the pre-flight discipline this SOP invokes).
- **`sop/standard/pantheon-bringup.md`** — from-scratch daemon bringup SOP. This parity SOP is the parity-restoration counterpart; bringup is the parity-establishment counterpart.
- **`tools/delivery_reachability.py`** — the model for BT2's `tools/parity_render.py`: read manifest, emit per-seat check output, JSON envelope + human table.
