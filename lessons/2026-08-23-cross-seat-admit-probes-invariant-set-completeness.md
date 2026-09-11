# Cross-seat admit probes must enumerate every target seat's config, not derive invariants from the golden-path seat alone

**Date authored:** 2026-08-23
**Author:** Orchestrator (Orchestrator)
**Status:** Drafted
**Re-evaluate when:** the next probe of this class ships (a legacy-admit / convergence-evidence gate that admits pre-existing state across ≥2 seats), or if the pattern recurs on a different substrate

## Observation

T-3087 landed a 4-gate `_legacy_convergence_evidence` probe in `persona_installer.py` (PR #315, ratified TG 8326, 2026-08-23) intended to admit already-enabled consume timers whose live shape is fully converged — the recovery seat's `pantheon-persona-consume@recovery.timer`, enabled by `recovery-bot-supervise.sh` on 2026-08-06 pre-dating the control-plane installer, was the specific case.

The probe was designed on the "exact invariant set that `timer-observe` apply verifies" — 3 systemd invariants (link canonicality + timer active/waiting/finite-next-elapse + service inactive/success/**ExecMainStatus==0**) — and cross-checked against the specialist seat's live state (the golden path: T-2658 timer-observe apply had run cleanly on specialist during ORG-039 cutover). Plan-agent, implementer, reviewer, and HO/author all signed off on this invariant set.

Recovery ran the T-3026 dry-run against the merged probe (2026-08-23T17:40Z) and it **still aborted** — filed T-3105 with precise root-cause: recovery's `pantheon-persona-consume@recovery.service` is configured `SuccessExitStatus=3 4 5` (accepts benign skip exit codes as success) and last exited 5 (`Result=success` per systemd's own SuccessExitStatus-policy application). Every other invariant passed; the probe returned `None` on the `ExecMainStatus!=0` branch alone. **Any seat that maps benign skip codes to success is un-admittable by T-3087 as shipped.**

The gap was a recovery-specific unit-config detail (`SuccessExitStatus=3 4 5`) that no reviewer audited because the probe's invariant set was reasoned from the reference seat's config, not from the union of all target seats' configs.

## Pattern

**When a probe admits a class of legacy state (not just one instance), the invariant set must be derived from the union of target configurations, not from the reference configuration.**

The trap is that "exact invariant set that X verifies" sounds like structural correctness — reuse what X (`timer-observe` apply, in this case) already trusts — but it silently assumes every target seat is configured the same way X assumes. When target seats diverge from that assumption (recovery's `SuccessExitStatus=3 4 5` is a legitimate accommodation, not drift), the probe's invariant set now over-constrains and rejects legitimate state.

The failure mode is specifically **plausible-across-review-layers**: the invariant set reads as principled (verifiable via one grep against one reference seat), the derivation methodology sounds right (reuse trusted structure), and no reviewer catches the gap unless they independently enumerate every target seat's config. Four review layers missed this instance (plan-agent, implementer, reviewer, HO/author) precisely because the framing looked like it *was* the structural methodology.

## Implication

Add to plan-first checklists for probe-authoring dispatches, especially when the probe admits legacy state across multiple seats:

- **Enumerate every target seat's relevant unit config** — for systemd probes, `systemctl show --user -p SuccessExitStatus,RestartPreventExitStatus,Restart,SuccessAction,FailureAction,<other-policy-directives> <unit>` on each candidate seat. Do not derive the invariant set until every target's config is on the table.
- **Ask: does my proposed invariant set admit each target's live state?** If not, either (a) relax the invariant to trust the source-of-truth's own policy-application summary (e.g. `Result=='success'` over hardcoded `ExecMainStatus==0`, since systemd already applied `SuccessExitStatus` to derive `Result`), or (b) parameterize the invariant per-seat by reading the unit's policy config at probe time.
- **Add a per-target-seat admit test**, not just an admit test for the reference seat. Two seats with divergent configs → two admit tests, plus the shared reject tests.

The relaxation direction (option a — trust the source-of-truth's summary) is generally cheaper + more honest than the per-seat parameterization direction (option b), because the source-of-truth's summary already applied every policy directive the probe would otherwise re-implement. Prefer (a) unless there's a specific reason (a) would admit a genuine failure mode that (b) would catch.

## Related

- **T-3087** — the arc where the pattern originated. Landed via PR #315, ratified TG 8326.
- **T-3105** — the fix (retargeted to Recovery, who has hot probe context from driving the T-3026 dry-runs). Option-b (trust `Result=='success'`) recommended by author (Orchestrator); Recovery may choose option-a if she sees a failure mode option-b would miss.
- **T-3026** — the recovery HC cutover this probe is meant to unblock; blocked on T-3105 until the fix lands.
- **ORG-028** / **ORG-039** — the FRAMEWORK/INSTANCE partition that governs why recovery's unit config diverges from specialist's (both are legitimate per-seat instance configuration atop harness-generic framework).

## Ratification path

If the pattern recurs on a different substrate (not systemd — e.g. a mailbox-shape probe, a git-ref-shape probe, a file-permission-shape probe), lift into `rulebook/directives.md` as a D-class rule ("probe invariant sets are derived from the union of target configs, not the reference config") with the plan-first checklist as its operational form. One recurrence would be sufficient signal; the pattern is general.

Not principle-tier yet — this is a design-methodology insight, not a Source-of-Will rule. If it becomes principle-tier, it would sit under P4 (traceability / truthfulness of gates) as a specification: "gates must faithfully model every state they're supposed to admit, not just the reference state."
