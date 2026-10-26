# Persona daemon liveness — diagnose + recover

**Status:** Ratified. Drafted 2026-06-06 (T-0873; generalizes `recovery-service-recovery.md` to the Telegram-facing personas per the 2026-05-21 incident's stated recurrence vector). Revised 2026-10-17 (T-5061) for the runtime-controller topology: control-plane seats recover through the controller, never their retired bespoke unit. Author: Recovery. **Ratified 2026-10-17 by the operator (TG 8525)** per the sop/remediation per-SOP ratification rule (T-0157); simplify once every seat is on the controller (T-5116).

How to tell whether a persona's Telegram-facing session is actually alive, and bring it back when it isn't. Audience: whoever is recovering a persona — Recovery (the monitoring/recovery host), the recovery seat (Watchman), HO/Orchestrator via sudo, or the operator directly. Applies to **Orchestrator, Specialist, Recovery and Archivist**. Each runs a harness TUI in a named tmux session, so any of them can die the same silent way, and "is the persona up?" is non-obvious from a process list.

## Substrate (re-verified 2026-10-17, T-5061)

What owns each session differs by seat, and it is changing as seats cut over to the runtime controller (T-2953/ORG-068). **Don't hardcode the owner: resolve it live** with `python3 tools/agent_role_map.py restart-target <persona>`, which prints `SUPERVISOR`, `SERVICE`, `PANE` and `SESSION_UNIT` from instance.yaml.

| Persona | Account | Supervisor (2026-10-17) | Session unit | tmux session | Runtime | jsonl dir |
|---|---|---|---|---|---|---|
| Orchestrator (HO) | `claude` | bespoke, until T-2953 | `claude-telegram.service` | `claude-telegram-bot` | claude | `/home/claude/.claude/projects/-home-claude` |
| Specialist | `claude-research` | control-plane | `pantheon-runtime-controller@specialist.service` (consume: `pantheon-persona-consume@specialist`) | `claude-research-bot` | claude/interactive-subscription | `/home/claude-research/.claude/projects/-home-claude-research-pantheon-config` |
| Recovery | `claude-fallback` | control-plane (T-3026, 2026-09-29) | `pantheon-runtime-controller@recovery.service` (consume: `pantheon-persona-consume@recovery`) | `claude-telegram-bot-fallback` (window `harness`) | claude/interactive-subscription | `/home/claude-fallback/.claude/projects/-home-claude-fallback-pantheon-config` |
| Archivist | `exocortex` | bespoke, until T-5063 | `claude-telegram-exocortex.service` | `claude-telegram-bot-exocortex` | claude | `/home/exocortex/.claude/projects/-home-exocortex` |

**Telegram receiver.** instance.yaml `codex_telegram_bridge.personas.<p>.claude_receiver_topology` is `runtime-owned` for orchestrator, specialist and recovery. On a claude runtime the `--channels` plugin inside the TUI is the receiver; on a codex runtime the `codex-telegram-poller.service` bridge is. So "is the bot up?" depends on which runtime is live. Read `~/.local/state/pantheon/personas/<p>/active-runtime.json` (a cache; tmux plus pid liveness is authoritative) before choosing a recovery path.

**Bespoke seats** (Orchestrator, Archivist today): the session unit is a `systemctl --user` unit with a `Restart=always` supervisor script that keeps the named tmux session alive and exits when it dies so systemd respawns it (crash-loop guard `StartLimitIntervalSec`/`StartLimitBurst`).

**Control-plane seats** (Specialist, Recovery; Orchestrator and Archivist after cutover): `pantheon-runtime-controller@<p>` owns the session and relaunches a dead pane through runtime selection (honouring holds and overrides). The old bespoke unit is retired and disabled: **never start or restart it**, because that resurrects a second supervisor next to the controller.

The auto-restart and health layer on top (context_fill_monitor api-health, wedge and fill restarts, `restart_drain`, `handoff_and_restart.sh`, the recovery watcher) reduces incidence but does not replace this runbook for the residual cases.

### Seats this SOP does NOT cover

Mason (`suborch1`), Artisan (`suborch2`) and Watchman (`codex-fallback`) have no Telegram daemon; they are mailbox-first. Their liveness is the runtime controller plus the persona-consume timer, and recovery goes through the controller (`pantheon-runtime-controller@<p>`, the consume timer, context_fill_monitor's control-plane route). Watchman's own health is the recovery watcher's `self_state` (`/run/recovery-seat-health/watcher.json`); its only Telegram path is a send-only alert bot (no poller). For those seats, use `sop/standard/onboarding-a-mailbox-first-seat-onto-the-control-plane.md` (topology and gates) and `sop/standard/confirm-health-after-touching-a-live-session.md`. For a control-plane seat the controller can't bring back, use `control-plane-seat-dead-or-wedged.md` (T-5111, Ratified).

## Failure shape

Three distinct modes — **`systemctl is-active` does NOT distinguish them; `tmux capture-pane` is the source of truth.**

1. **Inert daemon** — the tmux session and its harness process exited; nothing is running. Bespoke seats auto-restart, and the mode persists only if the crash-loop guard tripped (unit `failed`). Control-plane seats are relaunched by the controller unless runtime selection is blocked (look for `runtime_viability_blocked` in `~/.local/state/pantheon/personas/<p>/runtime-controller/controller-evidence.jsonl`).
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

### Control-plane seats (`SUPERVISOR=control-plane`)

0. **First, `harness <seat> status`** (ORG-106, T-5526). A seat in `deep-sleep` (`asleep` or `draining`) is down ON PURPOSE. It is not a fault. Do not restart it, kill-and-relaunch it, or start any unit for it: a launch while asleep is a `launch_breach` and pages. If it must come back, run `harness <seat> wake --reason "..."` and let the next consume fire relaunch it. A `paused-intake` seat's session is alive, so recover it normally (the pause stays in force).

1. **Cycle the session through the controller** (the cure for modes 2 and 3, and a fresh `--channels` bridge for mode 4):
   ```bash
   # what recovery_self_restart.sh and context_fill_monitor resolve to:
   sudo -u <ACCT> bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) ~/pantheon-config/tools/handoff_and_restart.sh \
     --service pantheon-persona-consume@<p>.service --mode user --target-pane <PANE> --supervisor control-plane'
   ```
   It asks the seat to write a handoff, ends the pane, and lets the controller re-run selection.
2. **Pane wedged so hard the handoff can't land** (mode 3 with no prompt): end the tmux session (`sudo -u <ACCT> tmux kill-session -t <SESSION>`); the controller relaunches it. For an open spend-limit or picker modal, a lease-held Escape through the recovery broker (`codex-recovery unstick <target> spend-modal-dismiss`, recovery seat only) is the audited path. Don't restart `pantheon-runtime-controller@<p>` itself for a stuck pane.
3. **Controller unit `failed`** (start-limit): `systemctl --user reset-failed pantheon-runtime-controller@<p>.service`, then `start` it, then read controller-evidence for the cause before trying again.
4. **Never** start or restart the retired bespoke unit (`claude-telegram-<x>.service`) on a control-plane seat.

### Bespoke seats (`SUPERVISOR=bespoke`)

1. **Restart the service (preferred — clean lifecycle; the cure for mode 2).**
   ```bash
   systemctl --user restart <SESSION_UNIT>                                            # same-account
   sudo -u <ACCT> XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>) systemctl --user restart <SESSION_UNIT>   # cross-account
   ```
   ⚠️ **`systemctl restart` can be a NO-OP for the inner harness.** The supervisor wrapper is idempotent: it only (re)creates the tmux session if it's missing, so a restart that finds the session alive cycles only the supervisor and leaves the SAME inner-claude PID. **Verify by inner-process PID change** (`pgrep -af '.npm-global/bin/claude'`), NEVER by `is-active` or exit-zero alone (caught on Orchestrator 2026-05-25).
2. **True restart when the inner process must actually cycle** (mode 2/3, or a no-op restart):
   ```bash
   sudo -u <ACCT> tmux kill-session -t <SESSION>     # supervisor (Restart=always) respawns a FRESH session
   ```
   A fresh session resets accumulated session and request state (clears the mode-2 poison), gives a fresh `--channels` bridge (clears mode-4 fork-severance) and dismisses a modal (mode 3).
3. **Unit `failed`** (crash-loop guard tripped): `systemctl --user reset-failed <SESSION_UNIT> && systemctl --user restart <SESSION_UNIT>`.

### Either supervisor

- **Mode 3 on malformed settings:** restore `~/.claude/settings.json` from a known-good backup (snapshot hooks / `~/.claude/backups/`), then cycle as above. Only hand-edit config if the failure points there — every edit is a chance to make recovery harder.

### Cross-account gotcha

`sudo -u` strips `XDG_RUNTIME_DIR`, which breaks `systemctl --user` (but not `tmux`). Always restore it dynamically: `XDG_RUNTIME_DIR=/run/user/$(id -u <ACCT>)`. Recovery has NOPASSWD sudo to every pantheon seat account (not to root). Watchman has mutual direct sudo with Recovery's account, and so transitive reach to every seat Recovery can reach (the operator TG 11705); the `codex-recovery` broker (leased, audited) is its preferred path. The operator/root can reach any.

## Verification after recovery

```bash
systemctl --user is-active <SESSION_UNIT>              # active
pgrep -af '.npm-global/bin/claude'                     # NEW inner PID vs pre-restart
tmux capture-pane -t <PANE> -p | tail -5               # prompt frame, no API errors / modal
```
On a control-plane seat, also confirm the last `controller_start_*` event in controller-evidence is after your action. Then **round-trip the bridge**: send a Telegram message and confirm a reply lands (a healthy-looking idle pane can still have a dead bridge — mode 4). Canonical self-check: `bash ~/pantheon-config/tools/verify_startup.sh --agent <persona>` (exit 0 = healthy; 7 = pane wedged; it names the session supervisor). Full discipline: `sop/standard/confirm-health-after-touching-a-live-session.md`.

## Autonomous-vs-bubble-up (D3) placement

- **Autonomous** (Recovery or any recoverer applies directly — in-scope, reversible): a controller cycle through `handoff_and_restart.sh --supervisor control-plane`, a bespoke restart / reset-failed / tmux-kill-session, restoring settings from backup, and the round-trip verification. Confirm health (is-active + capture-pane + PID change + round-trip) and state "confirmed healthy" rather than leaving it implied.
- **Bubble up to the operator:** a crash-loop or start-limit guard repeatedly exhausting after restarts (genuinely broken, not transient); any need to *edit* (not restore) config or change the model; a recurrence of the mode-2 400 outage (signals an unresolved upstream harness or model cause); a fresh session that boots silent and a re-kick doesn't recover; runtime selection blocked for a reason no ticket explains.

## Precedent cases

- **2026-05-21 Recovery** — mode 2 (alive-but-400ing), 162 failures over ~14h; cause = a harness/model edge on a giant long-lived session, duration = missing self-recovery (`incidents/2026-05-21-recovery-personal-daemon-silent-outage`).
- **2026-05-25 Orchestrator** — a `systemctl restart` no-op left the same inner-claude PID; a true restart needed `tmux kill-session`.
- **2026-06-06 Orchestrator** — mode 4 (bridge healthy, outbound replies composed in the transcript without the reply tool, so the operator received nothing); recovered by a send-keys nudge to re-send via the tool.
- **2026-10-15 Recovery (control-plane)** — a `/model` picker wedge (mode 3) was cleared by the recovery seat's lease-held `codex-recovery unstick … spend-modal-dismiss` (T-4884 B13), and a context-fill cycle went through the controller cleanly.

## Cross-references

- `recovery-service-recovery.md` — the original Recovery-scoped runbook this generalizes (kept for the 2026-05-21 RCA detail; its bespoke-unit steps no longer apply to Recovery).
- `sop/standard/confirm-health-after-touching-a-live-session.md` — the confirm-health discipline. `sop/standard/hand-moving-an-established-seat-onto-the-control-plane.md` and `sop/standard/reconciling-a-stale-runtime-state-record.md` — the controller cutover and state-record paths.
- `tools/agent_role_map.py restart-target` (the live supervisor resolver, T-4159), `tools/handoff_and_restart.sh`, `tools/recovery_self_restart.sh`, `tools/context_fill_monitor.py`, `tools/restart_drain.py`, `tools/codex_recovery.py` (the recovery broker, T-4884).
- `tools/verify_startup.sh` (T-0728) — the self-verifying boot/health gate.
