---
title: Migrating an existing persona onto the control plane
status: Drafted 2026-08-07 by T-2633 (implementer, dispatched by Orchestrator / HO); landed by Orchestrator (HO) — the worker-guard (T-1345) structurally denies subagent writes to sop/, so HO wrote the file from the implementer's draft. Adopted 2026-08-07 by Orchestrator (HO), the same below-goal-level-methodology-SOP pattern `onboarding-a-new-harness.md` (T-1031) was adopted under (autonomous per msg-3733; the operator flagged the need for this SOP directly, TG 7170). The MIGRATION ACTION on any specific persona's live production seat remains a goal-level, operator-supervised call; this SOP is the how, not a decision to fire.
---

# Migrating an existing persona onto the control plane

**Rationale:** `sop/standard/onboarding-a-new-harness.md` covers bringing up a **new, non-claude harness** as a Pantheon daemon from scratch. `sop/standard/adding-restart-capability-to-a-new-agent.md` covers giving a **new claude-code agent** the full ORG-020 restart stack. Neither covers the case this SOP fills: an **EXISTING claude persona**, already running its own bespoke telegram-bot supervisor (a hand-built systemd unit + supervise script + settings.json, predating the control plane), that needs to be **moved onto** `pantheon-persona-consume@<persona>` — the ORG-031/ORG-050 role-keyed control plane — WITHOUT a fresh bringup. Recovery (T-2590) was the first migration, hand-hardened in place. Specialist (T-2633) is the second, and the moment the flow was extracted into a generic, persona-parameterized tool: `tools/persona_cutover.sh` / `tools/persona_rollback.sh` / `tools/persona_cutover_resolve.py`.

**Architecture note:** this is a deliberately SEPARATE transitional migration tool, not a `persona_installer` verb. Install/apply/verify/rollback of the control-plane units themselves runs THROUGH `persona_installer` (via `tools/t2611_recovery_persona_apply.py`, ORG-050's plan/apply/verify contract) — this SOP's tooling only handles the legacy-bespoke-teardown half, which is a one-time, persona-specific transitional concern that does not belong inside the steady-state installer.

---

## When to use this

- An existing claude-code persona is running its own bespoke telegram-bot supervisor (a hand-built `systemd --user` service + supervise script + `~/.claude/settings.json`), predating `pantheon-persona-consume@<persona>`, and needs to move onto the control plane.
- The persona's `instance.yaml` `harness_runtime.roles.<role>` stanza has ALREADY been edited to carry the control-plane launch shape (model, channels, tmux session, post_start hooks) — see Prerequisites below. This SOP does not itself edit the manifest.

Do NOT use for:
- Bringing up a **brand-new** harness/persona that never had a bespoke supervisor — use `onboarding-a-new-harness.md` (non-claude) or `adding-restart-capability-to-a-new-agent.md` (claude) instead. There is nothing to migrate FROM.
- A wedged/crashed daemon that is already on the control plane — that's ordinary recovery, see `handoff-restart-runbook.md` / `../remediation/`.

---

## Prerequisites

Before running `persona_cutover.sh` for any persona:

- [ ] The persona's `instance.yaml` `harness_runtime.roles.<role>` stanza is edited AND that edit is present on the **target persona's own `pantheon-config` clone**, not merely HO's authoring clone. `resolve_persona()` (in `tools/persona_cutover_resolve.py`) verifies this — the manifest-precondition step — but the target clone must be `git pull`ed to a commit carrying the edit before firing for real.
- [ ] The stanza carries: `claude.native.channels` == `plugin:telegram@claude-plugins-official`, `claude.model` set (non-null), `tmux.session` set (and — current design invariant, see DECISIONS in the T-2633 implementer report — matching the persona's bespoke pre-cutover session name), `post_start` containing both `arm_pane_log.sh` and `recovery_boot_kick.sh` entries.
- [ ] `pantheon-persona-consume@<persona>.timer`/`.service` are NOT yet installed/enabled for this persona (the `--execute` run's `t2611 apply` step installs them).
- [ ] You (or the operator) can run as the target persona's own unix account — the whole flow runs there, not from HO's account. Both scripts enforce this via a uid guard sourced from `tools/persona_cutover_resolve.py`.
- [ ] A `--stamp <TS>` value chosen up front — the same stamp must be passed to `persona_cutover.sh` and, if ever needed, `persona_rollback.sh`.

---

## The fire flow

Always **dry-run first** (the default mode — `--execute` is required to mutate anything):

```bash
# as the target account, from a path that account can read (its own clone or a /tmp copy):
python3 tools/persona_cutover.sh --persona <name> --stamp <TS>
```

This resolves identity (bespoke facts + the manifest-precondition check), then the install-done precondition (a read-only `persona_installer` preflight — catches a PATH/harness-not-found failure BEFORE anything is torn down), then prints the exact sequence of backup/disable/apply/verify steps it would run. Read it. If anything looks wrong, stop here — nothing has been touched yet.

Then, BEFORE `--execute`, stage the disabled control-plane units into a durable checkpoint (T-2953 R5, T-3026 witness 2026-09-11):

```bash
# as the target account (same account persona_cutover.sh runs as):
PATH="<target-account-npm-global-bin>:$PATH" python3 tools/t2611_recovery_persona_apply.py stage-disabled \
    --manifest <target-account>/pantheon-config/instance.yaml \
    --persona <name> --harness claude \
    --repo-root <target-account>/pantheon-config \
    --stamp <TS>
```

**Why this step is separate:** the persona-installer applies + validates the target consume units in a *disabled* state and writes a signed checkpoint at `<target-account>/.local/state/pantheon/personas/<name>/staged-cutover-<TS>.json`. `persona_cutover.sh --execute`'s first action is `check-staged` — it refuses to touch anything if the checkpoint is absent. The dry-run above does NOT create it; skipping this step aborts the fire cleanly with `staged checkpoint precondition failed` before any state mutation (no rollback needed, but a wasted fire cycle). **Expected output:** exit 0 + `STAGE_STATUS=applied` (or `noop` on re-run) + the checkpoint path.

The stage-disabled step is safe to run repeatedly: it does NOT touch the still-live bespoke supervisor. It only writes the target-service units + checkpoint file.

Then fire for real:

```bash
python3 tools/persona_cutover.sh --persona <name> --stamp <TS> --execute
```

Sequence: verifies the staged checkpoint (aborts if absent — see above) → backs up the three bespoke files (`<file>.pre-cutover-<TS>`) → disables the bespoke service → kills the bespoke tmux session (aborts if it survives — never activates staged units with a live bespoke claude still bound to the telegram bot) → runs `t2611_recovery_persona_apply.py activate-staged` (activates from the checkpoint: enables + starts the consume timer/service, T-2953 R5) → runs the full soak-crit-4 verify block: tmux session up, cgroup scope ownership, exact `--channels`/`--model`/`--permission-mode` launch flags, composer ready with no channels-startup dialog, telegram inbound poller present (bounded 90s retry, with a comms-dark diagnostic capture on failure).

Any verify FAIL, or an `apply` failure, prints an explicit pointer at the rollback command (see below) — the script never auto-rolls-back.

### Cross-account invocation shape

Both scripts must run as the target account (uid enforced). The standard HO fire-time pattern, mirroring `t2590_recovery_cutover.sh`'s own header:

```bash
PW=$(tr -d '\n' < /home/claude/root.env)
echo "$PW" | sudo -S -k -u <account> bash <readable-path>/persona_cutover.sh --persona <name> --stamp <TS>            # dry-run
echo "$PW" | sudo -S -k -u <account> bash <readable-path>/persona_cutover.sh --persona <name> --stamp <TS> --execute  # real cutover
```

`<readable-path>` must be a path the target account can read (its own `pantheon-config` clone, or a `/tmp` copy) — `/home/claude/...` (HO's home) is not cross-account readable.

### Optional flags

- `--manifest <path>` — override which manifest is verified (defaults to the target account's own clone, computed dynamically from its resolved home directory).
- `--unit-file <path>` — operator escape hatch: resolve identity (including the supervise-wrapper parse) against a LOCAL copy of the bespoke unit file instead of the real cross-account read. Useful to dry-run against a draft/backed-up unit before it's live, or from a seat that cannot read the real cross-account file. Also the seam `tools/test_persona_cutover_verify.sh` uses for hermetic testing.
- `--with-urgent-env` — installs a `PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED` drop-in on the consume service (Recovery's original T-2590 use of the cutover as a supervised-first-fire activation vehicle for that generic mailbox-severity kill switch). Default OFF; most migrations should omit this flag entirely — it is unrelated to the migration itself.

---

## Rollback flow

`persona_rollback.sh` is **direction-independent**: it always means "restore the three captured bespoke files and restart," regardless of how far the cutover got. It has **zero manifest dependency** (mirrors `t2590_recovery_rollback.sh`'s own precedent) — it works even if the target's `instance.yaml` is missing, unreadable, or mid-incident reverted.

```bash
python3 tools/persona_rollback.sh --persona <name> --stamp <TS>            # dry-run
python3 tools/persona_rollback.sh --persona <name> --stamp <TS> --execute  # real restore
```

Preflight: every `<file>.pre-cutover-<TS>` backup must exist before ANYTHING is touched — a partial restore is refused outright.

Execute sequence: restores the three files → re-enables the bespoke service (the cutover's `disable --now` removed the enable symlink; skipping this step is the exact bug both live Recovery attempts hit) → restarts it → the Shape-1 inverse: disables `pantheon-persona-consume@<persona>.timer` and removes the urgent-env drop-in if one was installed (no-op if it wasn't) → verifies: service active + enabled, bespoke tmux session up, claude child present, bun poller child present (WARN only, not a hard FAIL).

---

## Verification checklist

Run after a real `--execute` cutover, before declaring the migration complete:

- [ ] `CUTOVER COMPLETE` printed with all soak-crit-4 verify lines OK (no FAIL lines).
- [ ] `systemctl --user list-timers pantheon-persona-consume@<persona>.timer` shows it armed with a finite next-elapse.
- [ ] The persona responds to a real inbound Telegram message within the normal turn-latency window (soak-crit-4's poller-present check confirms the plugin process tree is alive, not that a message round-trips).
- [ ] `journalctl --user -u pantheon-persona-consume@<persona>.service` shows no repeated failures across at least one more scheduled fire.
- [ ] The three `.pre-cutover-<TS>` backups are retained (do not delete) until the migration has soaked — they are the only rollback path.

If the first fire is not clean (verify FAIL, comms-dark, or apply failure): do not leave the persona half-migrated. Roll back immediately per the flow above, diagnose from the printed diagnostic (comms-dark capture path, or the apply error), and re-attempt only after the root cause is understood — same disarm discipline as `onboarding-a-new-harness.md`'s "if the first-fire is not clean" section.

---

## Boundary

Covers: migrating an EXISTING claude persona from a bespoke telegram-bot supervisor onto the control plane (`pantheon-persona-consume@<persona>`) — the dry-run/execute/verify/rollback flow, cross-account invocation shape, and prerequisites.

Out of scope: editing `instance.yaml`'s `harness_runtime.roles.<role>` stanza itself (a separate, prior step — see Prerequisites); bringing up a persona that never had a bespoke supervisor (`onboarding-a-new-harness.md` / `adding-restart-capability-to-a-new-agent.md`); non-claude harness migrations (`--harness claude` is the current, deliberate scope of `persona_cutover.sh` — see the script's own header and the T-2633 implementer report's DECISIONS for the relax-note if a future non-claude seat ever needs migrating); ordinary recovery of an already-migrated, wedged daemon.

---

## Cross-references

- `tools/persona_cutover.sh` / `tools/persona_rollback.sh` / `tools/persona_cutover_resolve.py` — the tooling this SOP documents.
- `tools/t2590_recovery_cutover.sh` / `tools/t2590_recovery_rollback.sh` — the Recovery-specific, hand-hardened originals this generic tool was extracted from (T-2590). Superseded for any FUTURE migration by the generic tool; retained on disk as the historical record until a follow-up ticket (T-2634) retires them post-Specialist-soak.
- `tools/t2611_recovery_persona_apply.py` — the persona-general install-done-precondition + real-apply driver both scripts call into (ORG-050).
- `tools/t2590_cutover_verify_lib.sh` — the shared, persona-general verify predicates (pid selection, urgent-env confirm, telegram-poller tree-walk) both scripts source.
- `sop/standard/onboarding-a-new-harness.md` — the companion SOP for a brand-new, non-claude harness.
- `sop/standard/adding-restart-capability-to-a-new-agent.md` — the companion SOP for a brand-new claude-code agent's full ORG-020 restart stack.
- `architecture/adr/ORG-031-harness-abstraction-and-daemon-onboarding.md` §iii — the ADR this SOP is authored per.
- `architecture/adr/ORG-050-security-and-identity-contract.md` — the installer plan/apply/verify/identity contract `t2611_recovery_persona_apply.py` implements against.
