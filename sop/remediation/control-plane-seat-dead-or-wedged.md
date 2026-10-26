# Control-plane seat dead or wedged — the controller can't bring the session back

**Status:** **Ratified 2026-10-22 by the operator (TG 8740, "Looks good u may add it"; ratify ticket T-5370).** Drafted 2026-10-22 (T-5111; Author: Recovery), block_reason table checked against runtime_controller.py by orchestrator. Gap found while revising `persona-daemon-liveness-recovery.md` (T-5061). Paths A-C are now autonomous for Recovery and Watchman per the D3 placement below (report each use); Path D stays bubble-up.

## Failure shape

A seat whose session is owned by `pantheon-runtime-controller@<p>` stays dark, and the controller's own relaunch loop does not fix it. It applies to every control-plane seat: Specialist, Recovery and the mailbox-first seats Mason (`suborch1`), Artisan (`suborch2`) and Watchman (`codex-fallback`). Orchestrator and Archivist join after their cutovers. For a Telegram persona whose bespoke unit still owns the session, use `persona-daemon-liveness-recovery.md` instead.

Shapes, each with its tell:

| # | Shape | Tell |
|---|---|---|
| S1 | **Viability block** | Repeated `runtime_viability_blocked` lines in `controller-evidence.jsonl`, with a `block_reason` other than the seat's declared `expected_policy_skip` (instance.yaml). |
| S2 | **Start-limit** | `pantheon-runtime-controller@<p>` is `failed` with `Result=start-limit-hit` (the unit allows 20 starts in 600 s). |
| S3 | **Held runtime selected** | The seat comes up on (or keeps reaching for) a runtime you did not expect. `overrides/holds.json` or a cooldown in `runtimes/<ref>/last-attempt.json` explains it. |
| S4 | **Mailbox-first seat dark** | Mail piles up (`mail_staleness`, `wake-stuck`), the pane is missing or reads UNKNOWN, and there is no Telegram path to notice it by. |
| S5 | **Churn** | A new tmux scope every consume cycle, torn down each time (`runtime_viability_candidate_teardown`). Usually a stale or empty `runtime.json`. |

**First, rule out a deliberate hold.** A seat in seat-maintenance (`python3 tools/notify_policy.py list-maintenance` or `in-maintenance <p>`), or one HANDOFF.md records as held (mason since T-5222), is down **on purpose**. Do nothing except confirm the hold is still wanted. Since T-5277, mailbox_wake skips held seats, so a held seat should not raise wake-stuck any more.

## Diagnose (read-only, always first)

All paths are under the seat's account, `~/.local/state/pantheon/personas/<p>/`.

```bash
A=<account>; P=<persona>
# 1. What the controller last did, and why. This is the teardown record; trust it over pane inference.
sudo -u $A tail -n 20 ~$A/.local/state/pantheon/personas/$P/runtime-controller/controller-evidence.jsonl
# 2. Per-attempt failure class / cause / readiness proof:
sudo -u $A cat ~$A/.local/state/pantheon/personas/$P/runtime-controller/initial-viability.json
# 3. The record the controller validates the session against:
sudo -u $A cat ~$A/.local/state/pantheon/personas/$P/runtime-controller/runtime.json
# 4. Holds, cooldowns and the live pane (run AS the seat, so it reads that seat's state):
sudo -u $A bash -c 'cd ~/pantheon-config && python3 tools/harness.py '$P' status'
# 5. Unit state, through the seat's own manager:
sudo -u $A bash -c 'export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user show -p ActiveState,Result pantheon-runtime-controller@'$P' pantheon-persona-consume@'$P
# 6. Who launched what, and when. Two tmux-spawn panes seconds apart = two launchers (T-4101):
sg systemd-journal -c "journalctl _UID=$(id -u $A) --since -1h" | grep -E 'tmux-spawn|runtime-controller|persona-consume'
```

Map the newest `block_reason` (S1) to its fix. These are the reasons `runtime_controller.py` raises through `_raise_runtime_viability_block` and `_block_if_live_target`; orchestrator checked the table against main (T-5111 review):

| `block_reason` | Meaning | Fix |
|---|---|---|
| `recorded-source-key-mismatch`, `recorded-source-hash-mismatch`, `recorded-source-manifest-mismatch` | The live session was launched from different controller source or manifest than the record says | Path B (reconcile), or Path C once the seat's source is current |
| `current-source-provenance-unavailable` | The controller can't hash its own source (clone missing or unreadable) | Fix the clone: `git status` / ff-pull the seat's `~/pantheon-config`, then let the next cycle retry |
| *free-text reason next to a source-hash failure* | Two call sites raise the exception text itself, so the reason is not a fixed string | Same class as `current-source-provenance-unavailable`: fix the clone. Not a new fault |
| `recorded-scope-still-active`, `runtime-server-not-in-recorded-scope` | The recorded tmux scope and the live server disagree | Path B; if reconcile refuses, `reconciling-a-stale-runtime-state-record.md` §Resolving a refusal |
| `initial-viability-state-not-selected`, `runtime-state-missing-or-mismatched` | The live target doesn't match the selected runtime's recorded state | Path B (reconcile) |
| `selected-runtime-not-manifest-authorized` | The selected runtime isn't authorized by the manifest | Check the seat's `runtimes` in instance.yaml against the broker/runtime manifest; if they disagree and the fix isn't obvious, ticket orchestrator |
| `recorded-receiver-unknown`, `recorded-receiver-not-present`, `predecessor-receiver-present` | Telegram receiver state does not match the runtime (wrong bridge up, or none) | Check `active-runtime.json` and the poller unit, then the receiver handoff for the seat (`persona-daemon-liveness-recovery.md` §Telegram receiver) |
| `recorded-runtime-rehydrate-failed` | `runtime.json` is empty or unparseable | Path B; never hand-edit the file |

**Other evidence fields (not `block_reason`).** These appear in other fields of `controller-evidence.jsonl` and selection records, so grep for the field, not `block_reason`:

| Value (field) | Meaning | Fix |
|---|---|---|
| `runtime-held` (block `reason`) with `policy_skip_reason: runtime_held`; `operator-hold` (`cause`) | A hold is doing its job | S3: confirm the hold is wanted; lift with Path A only if it isn't |
| `receiver_handoff_failed` (teardown `reason` / `cause`) | The Telegram receiver handoff to the new runtime failed | As for the receiver rows above |
| `forced-cap` (swap `trigger`) | A provider cap forced a runtime swap | `agent-spend-limit-wedge.md` |
| `runtime_record_not_persisted` (`event`; `outcome` says why) | The controller launched a runtime but left `runtime.json` on the old record, so monitors read a stale harness (T-5371). Usual cause: the seat's clone is not clean, and untracked files count | `sudo -u $A git -C ~$A/pantheon-config status --short`; move strays to `~/.local/state/pantheon/stray-clone-files/` (never delete). The record heals on the next launch |

A reason not in this table is a **new** fault. Stop at diagnosis and file a ticket for orchestrator (the runtime_controller owner) with the evidence lines.

## Recommended remediation (ranked; stop at the first that confirms health)

**Path A — clear what is blocking selection (hold, cooldown, start-limit).** Reversible, no session touched.
- Hold that is no longer wanted: `sudo -u $A bash -c 'cd ~/pantheon-config && python3 tools/harness.py '$P' clear-hold <ref>'`. Watch the path trap: holds live under `~/.local/state/pantheon/personas/<p>/overrides/`, and only the controller honours them (the consume path and the context-fill relaunch ignore them).
- Cooldown blocking an immediate retry: `harness.py <p> clear-cooldown <ref>`.
- S2 start-limit: `systemctl --user reset-failed pantheon-runtime-controller@<p>`, then `systemctl --user start` it, after fixing the cause the evidence names. A reset without a fix just burns 20 more starts.

**Path B — reconcile a stale record.** Reversible (it writes a timestamped backup). Run `tools/runtime_controller.py reconcile --persona <p>` as the seat: dry-run first, `--apply` only on `status: dry-run` with the expected `matched_ref`. Full procedure: `sop/standard/reconciling-a-stale-runtime-state-record.md`. It fixes S5 and the `recorded-*` reasons above.

**Path C — the safe cycle: let the controller relaunch.** Use `harness.py <p> restart`, or for Specialist only, `tmux kill-session -t <session>` (verified 2026-09-25). The controller sees the session die, records the outcome, exits 0, and respawns through `_select_viable_runtime`, so exactly one runtime is ever alive.
- **Never** `systemctl restart pantheon-runtime-controller@<p>` as the cycle. The tmux runs in a separate `pantheon-runtime-tmux.scope`, so a bare controller restart can leave the old runtime alive next to the new one (the T-3995 split-brain).
- **Never** start, restart or re-enable a seat's retired bespoke session unit (e.g. `claude-telegram-fallback.service`). It brings back a second supervisor next to the controller.
- **Mason and Artisan: do not kill the session.** On those seats it takes the seat down instead of re-seating it (T-3819), until the difference from Specialist is understood. Use Path B first, and Path C only via `harness.py restart` with the evidence open.
- **Held seats: never** `pantheon-seat-relaunch.sh --recover`. It relaunches outside the controller and ignores holds.

**Path D — hand-move or re-onboard.** The record and the pane can't be reconciled, or the seat has never been cut over cleanly. Follow `sop/standard/hand-moving-an-established-seat-onto-the-control-plane.md` (Telegram seats) or `sop/standard/onboarding-a-mailbox-first-seat-onto-the-control-plane.md` (Mason, Artisan, Watchman). This is a planned maneuver with a don't-touch window: arm seat-maintenance first.

**After any path, confirm health** per `sop/standard/confirm-health-after-touching-a-live-session.md` (`verify_startup.sh --agent <p> --no-dewedge` where supported). The terminating action is proven by the **successor**: a fresh `runtime_started` evidence line, a pane at an idle prompt, and one mail delivered and consumed. Silence is not proof.

## Autonomous-vs-bubble-up D3 placement

- **Diagnose:** always autonomous.
- **Path A, Path B, Path C via `harness.py restart`:** autonomous for Recovery and Watchman (ratified 2026-10-22; reversible, single-seat, controller-mediated). Report each use, with the evidence line, to the seat's owner and on the ticket.
- **Path C via kill-session:** autonomous on Specialist only.
- **Path D, and any fix to runtime_controller itself:** bubble-up. Hand-moves go to the operator (they are planned maneuvers). Controller code goes to orchestrator.
- **A seat the operator put on hold:** never lifted autonomously, whatever the evidence says.
- **Unknown `block_reason`:** diagnose and ticket only.

## Precedent cases

- **T-3819 (2026-09-20):** killing and replacing mason's session wiped `runtime.json` to `{}`. The consume path fell into the create path and churned every cycle (S5), taking mason down. Lesson: Mason and Artisan are controller-validated; don't kill their sessions.
- **T-3995 / T-4022 (2026-09-25):** a bare controller restart left a codex runtime alive next to the new claude one (split-brain). Fixed by a hand-reconcile, now `runtime_controller reconcile` (T-4023).
- **2026-09-25 (specialist):** recovered from a stuck codex fallback with kill-session (Path C). Claude was re-seated once its cooldown expired.
- **T-4024 (2026-09-25..28):** an specialist "crash" was really the controller's own `provider-completion-not-proven` teardowns, visible in `controller-evidence.jsonl` all along. Lesson: read the evidence before inferring from the pane.
- **T-4786 / T-4788 / T-4789:** a hold on mason was ignored by the consume path and the context-fill relaunch. Hence the hold rules in Path A and Path C.
- **T-5277 (2026-10-22):** held mason raised wake-stuck every time mail arrived. mailbox_wake now skips seats in maintenance.

## Cross-references

- `sop/remediation/persona-daemon-liveness-recovery.md`: Telegram personas and bespoke seats. It points here for control-plane seats.
- `sop/standard/reconciling-a-stale-runtime-state-record.md` (Path B), `hand-moving-an-established-seat-onto-the-control-plane.md` and `onboarding-a-mailbox-first-seat-onto-the-control-plane.md` (Path D), `confirm-health-after-touching-a-live-session.md`.
- `tools/runtime_controller.py` (`_raise_runtime_viability_block` names every block reason), `tools/harness.py` (manual-override CLI), `tools/systemd/pantheon-runtime-controller@.service` (`Restart=always`, 20 starts per 600 s).
- ORG-068 (lifecycle legs), T-2953 (stateless harness controller), T-5061, T-5111.
