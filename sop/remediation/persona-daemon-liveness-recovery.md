# Persona daemon liveness — diagnose + recover

**Status:** Ratified. Drafted 2026-06-06 (T-0873; generalizes `recovery-service-recovery.md` to the Telegram-facing personas per the 2026-05-21 incident's stated recurrence vector). Revised 2026-10-17 (T-5061) for the runtime-controller topology and simplified 2026-11-02 (T-5116) after every covered seat moved to that topology. Author: Recovery. **Ratified 2026-10-17 by the operator (TG 8525)** per the sop/remediation per-SOP ratification rule (T-0157); that ruling explicitly authorized removing the obsolete bespoke branch once every seat was on the controller.

How to tell whether a persona's Telegram-facing session is actually alive, and bring it back when it isn't. Audience: whoever is recovering a persona — Recovery (the monitoring/recovery host), the recovery seat (Watchman), HO/Orchestrator via sudo, or the operator directly. Applies to **Orchestrator, Specialist, Recovery and Archivist**. Each runs a harness TUI in a named tmux session, so any of them can die the same silent way, and "is the persona up?" is non-obvious from a process list.

## Substrate (re-verified 2026-11-02, T-5116)

Every covered session is owned by the runtime controller (T-2953/ORG-068). **Don't hardcode the owner: resolve it live** with `python3 tools/agent_role_map.py restart-target <persona>`, which prints `SUPERVISOR`, `SERVICE`, `PANE` and `SESSION_UNIT` from instance.yaml.

| Persona | Account | Supervisor (2026-11-02) | Session unit | tmux session | Runtime | jsonl dir |
|---|---|---|---|---|---|---|
| Orchestrator (HO) | `claude` | control-plane | `pantheon-runtime-controller@orchestrator.service` (consume: `pantheon-persona-consume@orchestrator`) | `claude-telegram-bot` | claude or codex fallback | `/home/claude/.claude/projects/-home-claude` |
| Specialist | `claude-research` | control-plane | `pantheon-runtime-controller@specialist.service` (consume: `pantheon-persona-consume@specialist`) | `claude-research-bot` | claude/interactive-subscription | `/home/claude-research/.claude/projects/-home-claude-research-pantheon-config` |
| Recovery | `claude-fallback` | control-plane (T-3026, 2026-09-29) | `pantheon-runtime-controller@recovery.service` (consume: `pantheon-persona-consume@recovery`) | `claude-telegram-bot-fallback` (window `harness`) | claude/interactive-subscription | `/home/claude-fallback/.claude/projects/-home-claude-fallback-pantheon-config` |
| Archivist | `exocortex` | control-plane | `pantheon-runtime-controller@archivist.service` (consume: `pantheon-persona-consume@archivist`) | `claude-telegram-bot-exocortex` | claude or codex fallback | `/home/exocortex/.claude/projects/-home-exocortex` |

**Telegram receiver.** instance.yaml `codex_telegram_bridge.personas.<p>.claude_receiver_topology` is `runtime-owned` for orchestrator, specialist, recovery and archivist. On a claude runtime the `--channels` plugin inside the TUI is the receiver; on a codex runtime the `codex-telegram-poller.service` bridge is. So "is the bot up?" depends on which runtime is live. Read `~/.local/state/pantheon/personas/<p>/active-runtime.json` (a cache; tmux plus pid liveness is authoritative) before choosing a recovery path.

`pantheon-runtime-controller@<p>` owns each session and relaunches a dead pane through runtime selection (honouring holds and overrides). The old bespoke unit is retired and disabled: **never start or restart it**, because that resurrects a second supervisor next to the controller.

The auto-restart and health layer on top (context_fill_monitor api-health, wedge and fill restarts, `restart_drain`, `handoff_and_restart.sh`, the recovery watcher) reduces incidence but does not replace this runbook for the residual cases.

### Seats this SOP does NOT cover

Mason (`suborch1`), Artisan (`suborch2`) and Watchman (`codex-fallback`) have no Telegram daemon; they are mailbox-first. Their liveness is the runtime controller plus the persona-consume timer, and recovery goes through the controller (`pantheon-runtime-controller@<p>`, the consume timer, context_fill_monitor's control-plane route). Watchman's own health is the recovery watcher's `self_state` (`/run/recovery-seat-health/watcher.json`); its only Telegram path is a send-only alert bot (no poller). For those seats, use `sop/standard/onboarding-a-mailbox-first-seat-onto-the-control-plane.md` (topology and gates) and `sop/standard/confirm-health-after-touching-a-live-session.md`. For a control-plane seat the controller can't bring back, use `control-plane-seat-dead-or-wedged.md` (T-5111, Ratified).

## Failure shape

Three distinct modes — **`systemctl is-active` does NOT distinguish them; `tmux capture-pane` is the source of truth.**

1. **Inert daemon** — the tmux session and its harness process exited; nothing is running. The controller relaunches it unless runtime selection is blocked (look for `runtime_viability_blocked` in `~/.local/state/pantheon/personas/<p>/runtime-controller/controller-evidence.jsonl`).
2. **Alive-but-erroring** — process running, `is-active` = `active`, but every API request 400s (the 2026-05-21 Recovery trigger: `400 role 'system' is not supported on this model`, 162 failures over ~14h on a giant long-lived session). A bare auto-restart does NOT catch this (the process never exits); the `context_fill_monitor` api-health check force-restarts it.
3. **Wedged TUI** — blocked at a modal or dialog (e.g. a malformed `~/.claude/settings.json` gives "Settings Error"; a "Resume from summary" usage modal; the dev-channels boot dialog; a spend-limit or `/model` picker). capture-pane shows the dialog; the loop is frozen behind it.

A fourth, comms-only mode worth ruling out: **bridge-dead-but-session-healthy** — the pane is at a clean idle or working prompt and the session is fine, but outbound Telegram never reaches the operator. Causes: the persona composed a reply in its transcript without calling the reply tool, or a fork-on-resume severed the live `--channels` binding (see the recovery-service RCA cross-ref). Confirm with a round-trip, not just a healthy-looking pane.

## Diagnosis (always capture the pane first)

```bash
python3 tools/agent_role_map.py restart-target <persona>   # SUPERVISOR / SERVICE / PANE / SESSION_UNIT

# Same-account (recovering your own seat, or from the owning account):
tmux capture-pane -t <PANE> -p | tail -40                  # source of truth
systemctl --user status <SESSION_UNIT>
journalctl --user -u <SESSION_UNIT> -n 100 --no-pager

# Cross-account (Recovery or Watchman recovering another seat, or the operator recovering any):
sudo -u <ACCT> tmux capture-pane -t <PANE> -p | tail -40   # no XDG needed for tmux
sudo -u <ACCT> XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>) systemctl --user status <SESSION_UNIT>
```

- Mode 1 on a control-plane seat: read the last few `controller-evidence.jsonl` events. `controller_start_*` means it is relaunching; a run of `runtime_viability_blocked` with an unexpected `block_reason` means selection is refusing (compare instance.yaml `expected_policy_skip`).
- Mode 2 (alive-but-400ing): the pane shows repeated `API Error: 400 …`; confirm in `<jsonl dir>/*.jsonl` (grep `not supported on this model` or sustained 400s).
- Mode 4 (bridge dead): the pane looks healthy, so send a Telegram message and confirm a reply actually lands; check that the persona's bridge process (`bot.pid` under `~/.claude/channels/telegram/`) is the live session's child.

## Recommended remediation (ranked)

### Controller-owned seats (`SUPERVISOR=control-plane`)

0. **First, `harness <seat> status`** (ORG-106, T-5526). A seat in `deep-sleep` (`asleep` or `draining`) is down ON PURPOSE. It is not a fault. Do not restart it, kill-and-relaunch it, or start any unit for it: a launch while asleep is a `launch_breach` and pages. If it must come back, run `harness <seat> wake --reason "..."` and let the next consume fire relaunch it. A `paused-intake` seat's session is alive, so recover it normally (the pause stays in force).

1. **Cycle the session through the controller** (the cure for modes 2 and 3, and a fresh `--channels` bridge for mode 4):
   ```bash
   # what recovery_self_restart.sh and context_fill_monitor resolve to:
   sudo -u <ACCT> bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) ~/pantheon-config/tools/handoff_and_restart.sh \
     --handoff-file /home/<ACCT>/HANDOFF.md --service pantheon-persona-consume@<p>.service \
     --mode user --target-pane <PANE> --supervisor control-plane'
   ```
   It asks the seat to write a handoff, ends the pane, and lets the controller re-run selection. Treat this wrapper as the **sole owner of the cycle** and let it return through its built-in readiness verification. Do not separately restart `pantheon-persona-consume@<p>.service` before, during, or immediately after it: a second consume job can race the controller relaunch and respawn the selected runtime over the fresh pane. If another action is still necessary, re-capture the pane and controller evidence after this command returns, then diagnose that result before firing another controller/consume action.
2. **Pane wedged so hard the handoff can't land** (mode 3 with no prompt): end the tmux session (`sudo -u <ACCT> tmux kill-session -t <SESSION>`); the controller relaunches it. A spend-limit modal is governed by the specific [agent spend-limit wedge SOP](agent-spend-limit-wedge.md), not this generic liveness recipe: its lease-held broker Escape is permitted only when fresh detector evidence identifies a monthly-spend-limit modal, the parsed reset time has passed, and a live capture immediately before action proves the exact modal still occupies the pane bottom. The detector state is reread after capture; missing, changed, stale, ambiguous, pre-reset, unreadable, or nonmatching evidence refuses. This broker action does not authorize dismissing a generic picker modal. Don't restart `pantheon-runtime-controller@<p>` itself for a stuck pane.
3. **Controller unit `failed`** (start-limit): `systemctl --user reset-failed pantheon-runtime-controller@<p>.service`, then `start` it, then read controller-evidence for the cause before trying again.
4. **Never** start or restart the retired bespoke unit (`claude-telegram-<x>.service`) on a control-plane seat.

### Configuration recovery

- **Mode 3 on malformed settings:** restore `~/.claude/settings.json` from a known-good backup (snapshot hooks / `~/.claude/backups/`), then cycle as above. Only hand-edit config if the failure points there — every edit is a chance to make recovery harder.

### Cross-account gotcha

`sudo -u` strips `XDG_RUNTIME_DIR`, which breaks `systemctl --user` (but not `tmux`). Always restore it dynamically: `XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>)`. Recovery has NOPASSWD sudo to every pantheon seat account (not to root). Watchman has mutual direct sudo with Recovery's account, and so transitive reach to every seat Recovery can reach (the operator TG 11705); the `codex-recovery` broker (leased, audited) is its preferred path. The operator/root can reach any.

## Verification after recovery

```bash
systemctl --user is-active <SESSION_UNIT>              # active
pgrep -af '.npm-global/bin/claude'                     # NEW inner PID vs pre-restart
tmux capture-pane -t <PANE> -p | tail -5               # prompt frame, no API errors / modal
```
Also confirm the last `controller_start_*` event in controller-evidence is after your action. Then **round-trip the bridge**: send a Telegram message and confirm a reply lands (a healthy-looking idle pane can still have a dead bridge — mode 4). Canonical self-check: `bash ~/pantheon-config/tools/verify_startup.sh --agent <persona>` (exit 0 = healthy; 7 = pane wedged; it names the session supervisor). Full discipline: `sop/standard/confirm-health-after-touching-a-live-session.md`.

## Autonomous-vs-bubble-up (D3) placement

- **Autonomous** (Recovery or any recoverer applies directly — in-scope, reversible): a controller cycle through `handoff_and_restart.sh --supervisor control-plane`, the controller-owned reset-failed or tmux-kill-session paths above, restoring settings from backup, and the round-trip verification. Confirm health (is-active + capture-pane + PID change + round-trip) and state "confirmed healthy" rather than leaving it implied.
- **Bubble up to the operator:** a crash-loop or start-limit guard repeatedly exhausting after restarts (genuinely broken, not transient); any need to *edit* (not restore) config or change the model; a recurrence of the mode-2 400 outage (signals an unresolved upstream harness or model cause); a fresh session that boots silent and a re-kick doesn't recover; runtime selection blocked for a reason no ticket explains.

## Precedent cases

- **2026-05-21 Recovery** — mode 2 (alive-but-400ing), 162 failures over ~14h; cause = a harness/model edge on a giant long-lived session, duration = missing self-recovery (`incidents/2026-05-21-recovery-personal-daemon-silent-outage`).
- **2026-05-25 Orchestrator** — a `systemctl restart` no-op left the same inner-claude PID; a true restart needed `tmux kill-session`.
- **2026-06-06 Orchestrator** — mode 4 (bridge healthy, outbound replies composed in the transcript without the reply tool, so the operator received nothing); recovered by a send-keys nudge to re-send via the tool.
- **2026-10-15 Recovery (control-plane)** — a `/model` picker wedge (mode 3) was cleared by the recovery seat's then-current lease-held `codex-recovery unstick … spend-modal-dismiss` (T-4884 B13), and a context-fill cycle went through the controller cleanly. This is historical evidence, not current authorization; T-5754 narrowed that action to verified post-reset monthly-spend-limit modals.

## Cross-references

- `recovery-service-recovery.md` — the original Recovery-scoped runbook this generalizes (kept for the 2026-05-21 RCA detail; its bespoke-unit steps no longer apply to Recovery).
- `sop/standard/confirm-health-after-touching-a-live-session.md` — the confirm-health discipline. `sop/standard/hand-moving-an-established-seat-onto-the-control-plane.md` and `sop/standard/reconciling-a-stale-runtime-state-record.md` — the controller cutover and state-record paths.
- `tools/agent_role_map.py restart-target` (the live supervisor resolver, T-4159), `tools/handoff_and_restart.sh`, `tools/recovery_self_restart.sh`, `tools/context_fill_monitor.py`, `tools/restart_drain.py`, `tools/codex_recovery.py` (the recovery broker, T-4884).
- `tools/verify_startup.sh` (T-0728) — the self-verifying boot/health gate.
