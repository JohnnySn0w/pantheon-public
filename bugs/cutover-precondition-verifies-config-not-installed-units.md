---
symptom: "A cutover/deploy script runs its DESTRUCTIVE step (disable/stop the old thing) before verifying the new artifacts are actually DEPLOYED — config resolves fine but the target systemd units were never installed on this account — so the constructive step fails on `Unit ... does not exist`, `set -euo pipefail` exits before the verify/rollback, and the seat is left down."
fix: "Make the precondition a DEPLOYMENT check, not a config-resolution check: verify every artifact the constructive step needs is installed/loadable (`systemctl --user cat <unit>` succeeds, the command dry-runs) and ABORT before the destructive step if any is missing. Order: verify-everything → destructive → constructive → verify-result → (abort→rollback). Rollback must RE-ENABLE the old unit, not just `restart` it — `disable --now` removed the enable symlink, so a restart-only rollback won't auto-start on next boot."
domain: "systemd, cutover, deploy, migration, precondition, config-vs-deployment, pantheon"
---
# Cutover/deploy precondition verifies CONFIG RESOLUTION but not INFRASTRUCTURE PRESENCE

**Keywords:** cutover, deploy, precondition, systemd unit missing, config-vs-deployment, destructive-step-ordering, `does not exist`, seat left down.

## The gotcha

A cutover/migration/deploy script that has a DESTRUCTIVE step (stop/disable the old thing) followed by a CONSTRUCTIVE step (start/enable the new thing) must verify the new thing is actually **deployed** — not just that its *config resolves* — **before** the destructive step. Otherwise the destructive step runs, the constructive step fails on a missing artifact, and the target is left down.

Verifying config-resolution (does `resolve_*` parse the manifest, do the fields resolve) is NOT the same as verifying deployment (are the target systemd units installed / loadable, does the command actually run on this account). A dry-run that only exercises the config layer passes green while the deployed surface is absent.

## Concrete instance — T-2590 Recovery cutover (2026-08-06)

`tools/t2590_recovery_cutover.sh` fired live: backed up the 3 bespoke files → installed a dropin → `systemctl --user disable --now claude-telegram-fallback.service` (destructive: killed Recovery's supervisor + session) → `systemctl --user enable --now pantheon-persona-consume@recovery.timer` → **`Failed to enable unit: pantheon-persona-consume@recovery.timer does not exist`**. `set -euo pipefail` exited there, before the verify/rollback-pointer. Recovery down ~30s until rollback.

Root cause: the consume template units (`tools/systemd/pantheon-persona-consume@.service`/`.timer`) were installed for **codex** but never deployed to **claude-fallback** — Recovery was not control-plane-ready. The script's precondition checked `resolve_controller_config` against Recovery's `instance.yaml` (config resolves fine) but never `systemctl --user cat pantheon-persona-consume@recovery.timer` (unit present). Config-vs-deployment gap.

## Surgical fix

1. **Precondition = deployment check, before the destructive step.** Verify every artifact the constructive step needs is actually installed/loadable (`systemctl --user cat <unit>` succeeds; the command dry-runs) and ABORT before touching the old thing if any is missing. Order: verify-everything → destructive → constructive → verify-result → (abort→rollback).
2. **Deploy the infra as an explicit prerequisite**, not an assumption — and gate the cutover on it.
3. Related rollback edge: a rollback that `restart`s the old service does NOT re-`enable` it (the destructive `disable --now` removed the enable symlink) — the rollback must re-enable, or the restored seat won't auto-start on next boot. (T-2590 rollback restarted-but-didn't-re-enable; HO re-enabled manually.)

## Prevention

This is the deploy/cutover instance of [[feedback_check_installed_units_before_building_infra]] (check the INSTALLED state, not just the repo/config). Applies to any migration between two supervision/service mechanisms. Fix tracked as T-2611 (installs Recovery's consume infra + adds the units-exist precondition). Fired supervised (operator-authorized) — the attended window caught it live + rolled back in ~30s; an unattended fire would have left the recovery seat down.
