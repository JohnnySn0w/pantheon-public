---
title: Hand-moving an established (install-done) seat onto the control plane
status: Adopted 2026-09-11 by Recovery (recovery) per the operator's direct steer (TG 2026-09-11, msgs 6931/6932/6934) that established seats be HAND-MOVED rather than have the installer/germination-engine taught to tolerate their pre-existing state. Below-goal-level methodology SOP (same adopt-and-proceed convention as `migrating-an-existing-persona-onto-the-control-plane.md`). The MOVE ACTION on any specific live seat remains a goal-level, operator-supervised, maintenance-windowed call driven by an external driver; this SOP is the how, not a decision to fire. AMENDED 2026-09-12 (Recovery + orchestrator converged after the T-3026 fire-3 diagnosis): added **step 3b — reset stale runtime-controller state** (mv the seat's stale `runtime.json` aside, after tmux-kill / before consume-enable) so an established seat's consume takeover bootstraps a fresh runtime instead of rehydrate-blocking on a recorded scope that no longer matches; + the matching rollback restore. Below-goal-level methodology amendment, adopt-and-proceed. AMENDED 2026-09-28 (T-3650; drafted on orchestrator's decisions, reviewed and APPROVED by Recovery @20a26b9ba after one REBOUND): the end-state is now the **specialist shape** (`pantheon-runtime-controller@<seat>` for swap-on-death, coexisting with the consume timer), not consume-only. The procedure is rewritten as a gated seat script (preflight, backups, teardown, quarantine of ALL control-plane residue, consume bootstrap Gate A, controller-attach Gate B, timer last), with a manual rollback that covers runtime-controller@. Phase Q supersedes step 3b. Below-goal-level methodology amendment, adopt-and-proceed. The MOVE on a live seat remains an operator-supervised, maintenance-windowed, externally driven event. AMENDED 2026-09-29 (T-4330, Recovery): the seat script holds `codex-telegram-poller.service` down from phase 1 until it exits (`tools/codex_poller_hold.sh`), and the rollback releases the hold. Below-goal-level methodology amendment, adopt-and-proceed. AMENDED 2026-09-30 (T-4377, orchestrator; from the T-4362 review): phase 0 refuses unless the seat is in maintenance and the clone carries T-4362 and T-4371, and §3 and C3 note that maintenance now pauses the seat's inbox consumer, so C3's pull is manual. Below-goal-level methodology amendment, adopt-and-proceed. AMENDED 2026-10-24 (T-2953, Recovery; from the failed orchestrator attempt 20261024T000344Z): since T-5282 (#1080) consume skips the whole cycle while the seat is in maintenance, so the phase-4 bootstrap fire launched nothing and Gate A rolled back. Phase 4 now writes a one-shot, persona-scoped, <=15-min bypass token (`persona_bringup.consume_maintenance_bypass`) right before that single fire; consume deletes it on read, so the timer's fires stay held. Phase 0 refuses on a clone without it; the EXIT trap and the rollback remove any leftover token. Below-goal-level methodology amendment, adopt-and-proceed.
---

# Hand-moving an established (install-done) seat onto the control plane

**Rationale.** `migrating-an-existing-persona-onto-the-control-plane.md` documents the *automated* cutover via `tools/persona_cutover.sh --execute`. That tool assumes a **from-scratch** seat: its `activate-staged` step (`persona_installer.py` `_classify_stale_unit_failure_reset_snapshot`, ~L1016) hard-aborts with `OperationFailed("pre-existing loaded service")` on any consume unit whose `LoadState=loaded`, and `persona_rollback.sh` (~L178-186) demands `LoadState=not-found`. On an **established / install-done** seat the `pantheon-persona-consume@.{service,timer}` template files are already present, so `@<persona>` always reports `loaded` — both the activation and the rollback abort. Recovery hit this twice on 2026-09-11 (T-3026), with a ~2-min comms-dark window when the auto-rollback also aborted.

The operator's ruling (TG 2026-09-11): **do NOT teach the installer/germination-engine to tolerate established-seat state** — it stays clean-seat-only for germinating NEW seats. The two legacy established seats (**recovery**, **orchestrator**) are moved **by hand** as a one-time, supervised transitional operation. This SOP is that manual procedure. It is deliberately NOT installer machinery.

**What the move produces (2026-09-28 amendment).** The seat ends in the **specialist shape**: `pantheon-runtime-controller@<persona>.service` supervises the runtime and swaps Claude to Codex (and back, with `fallback_return`) when a runtime dies. The consume timer keeps its wake and mailbox duties. The original 2026-09-11 version of this SOP expected the consume timer alone and "no runtime-controller@ unit". That was the stale half: `tools/systemd/README.md` (Runtime controller section) says the two units coexist, and swap-on-death, the reason for T-2953, lives only in the controller.

**Where the reasoning lives.** Section numbers below match the approved draft `notes/2026-09-28-t3650-handmove-hc-variant-draft.md`. References to §9 (code-level answers: launcher race, controller first cycle, T-4101 attach, drivers, the gaps the old SOP missed) and to DECISIONS D-1..D-15 point into that note.

---

## 0. What changed on 2026-09-28 (vs the 2026-09-11 version)

| 2026-09-11 version | Now | Why (details in §9 and the DECISIONS of the notes file) |
|---|---|---|
| End state: the consume timer only, with "no runtime-controller@ unit expected" (old §The runtime model) | End state: `pantheon-runtime-controller@<seat>.service` (swap-on-death) plus `pantheon-persona-consume@<seat>.timer`. §1 is the model. | HO decision 1; `tools/systemd/README.md:478-488` |
| Step 3b moves only `runtime.json` and leaves `initial-viability.json` "[status=selected]" (old step 3b) | Step Q quarantines **all** control-plane residue on both seats. Only the append-only evidence log and the lock anchor stay. | Orchestrator's record is `exhausted`, not `selected`. A failed bootstrap re-arms the orphan block (§9f-3). |
| Order: enable the timer, then kick consume | Order: one consume fire (Gate A), then enable the controller, which must attach and not relaunch (Gate B), then enable the timer | §9a |
| `tmux kill-session` ends the teardown | Also stop the old pane's `tmux-spawn-*.scope`, then prove the old Telegram poller is gone | The pane and the plugin live in a separate scope that can outlive the server (§9f-2) |
| No memory handling | Pin `autoMemoryDirectory` before the move | The launch cwd moves to `~/pantheon-config`, which would fork the memory directory (§9f-1) |
| Rollback: disable the timer, restore, re-enable the bespoke unit | Rollback: stop both launchers, stop the Codex poller and prove it absent, take the control-plane pane down, restore state and backups, re-enable the bespoke unit | HO decision 2; §6 |
| Maintenance: `enter-maintenance` with no account named | Arm it in the shared registry (T-4150) from an up-to-date clone, and keep it armed until commit B is live on claude-fallback's clone | HO decision 6; §9f-6 |

**2026-09-29 amendment (T-4330).** If the bootstrap falls back to Codex, its receiver handoff starts `codex-telegram-poller.service` on the seat's bot token. That happened on recovery's attempt 1 (20:01:15-20Z), and the rollback tore it down seconds later. On a seat with no `~/.config/codex-telegram/poller.offset` (recovery, today), the poller's first start runs codex-config's first-boot drain-and-skip. It confirms every queued update and injects none, and the dark window from phase 2 on is exactly when the operator's messages queue up.
- The script now holds the poller from phase 1 until exit. `tools/codex_poller_hold.sh hold` adds a runtime drop-in with a failing `ExecStartPre` and `Restart=no`.
- While held, a Codex fallback's start fails, and the controller records `receiver_handoff_failed` and compensates. Gate A then rolls back, and the queue waits for the restored Claude plugin. Messages are delayed, never lost.
- Don't use `systemctl --user mask --runtime`: the unit file in `~/.config/systemd/user` outranks the runtime directory, so that mask is a silent no-op (measured: `start` rc 0, unit active).
- An `EXIT` trap releases the hold on every exit, and the rollback releases it inline.
- SIGKILL skips the trap. A hold left that way is on tmpfs, so a reboot clears it; until then the next run's preflight refuses and names it (T-4335).

---

## 1. The runtime model you are moving to (read this first)

The end state is the **specialist shape**: two units with two separate duties. They coexist; neither replaces the other (`tools/systemd/README.md:480`).

- **`pantheon-runtime-controller@<persona>.service`** is the runtime supervisor.
  - It is `Type=simple`, `Restart=always`, `RestartSec=5`, and `WantedBy=default.target`.
  - Each process runs one `controller_main` cycle: select, then start or attach, then block until the tmux session dies, then record the outcome and exit (`tools/runtime_controller.py:8644-8907`).
  - On pane death, systemd respawns it into a fresh selection. That fresh selection is **swap-on-death**: Claude comes back, or Codex if Claude is cooling down or held.
  - With `fallback_return: true`, the cycle returns from Codex to Claude once Claude is viable again. There is a 300 s dwell before the return, and it waits for an idle pane (`:8015-8082`, `:8386-8394`).
- **`pantheon-persona-consume@<persona>.timer`** fires the oneshot `persona_bringup.py consume` every ~2 min.
  - With a live, recorded runtime, it is a wake/no-op.
  - When the record doesn't match the pane, it policy-skips (exit 5, `runtime_viability_blocked`), as on specialist and artisan today (`instance.yaml:699`, `:825`).
  - It can also *launch*, but only when no pane exists and no `selected` viability record says otherwise. §9a covers how the two launchers are kept from colliding.
- **The pane** is `<tmux.session>:harness.0`.
  - The session name is the bespoke one, but the window is named `harness`, not the bespoke auto-name `claude`. The cwd is `~/pantheon-config`, not `~`.
  - The tmux **server** runs in `pantheon-runtime-tmux.scope`. The pane process runs in its own `tmux-spawn-*.scope` (tmux 3.4).
  - Window index 0 is unchanged (base-index 0), so `restart_pane: <session>:0`, `mailbox_wake.py:71`, and `keep_going.py:102` keep resolving. Leave them as they are (same note as `instance.yaml:760-767`).
- **Launch argv** is derived from the manifest, plus `--settings <state>/runtime-controller/quota/main-session-settings.json` (T-4024, `:1265-1296`). For both seats today:
  `/home/<acct>/.npm-global/bin/claude --channels plugin:telegram@claude-plugins-official --model claude-opus-5-5 --effort xhigh --permission-mode bypassPermissions --settings /home/<acct>/.local/state/pantheon/personas/<p>/runtime-controller/quota/main-session-settings.json`
- **post_start commands** (`arm_pane_log.sh`, the 409 re-assert, `recovery_boot_kick.sh`) run only when the **consume** path launches (`ensure_runtime`, `:2745-2760`). A controller relaunch after a death does not run them. This is follow-up F1, not a blocker; §9f-5 covers it.
- **Telegram** runs inside the Claude process (`claude_receiver_topology: runtime-owned`, `instance.yaml:233-258`). The codex flip is fleet policy (T-5446): the planned 95% quota transition, the mid-session cap swap and the return to the default runtime are on for every seat that has a manifest fallback, comms seats and recovery included. None of them can be set by hand; `instance.yaml` keys for them are rejected. On a comms seat the controller swaps the Telegram receiver with the runtime (claude plugin <-> codex-telegram poller), so a Codex stint keeps Telegram. A comms seat's mid-session swap additionally requires a ready codex receiver (verify-seat comms section). The planned-quota path is not readiness-gated (T-5456). (Supersedes the old "HO decision 5", which said a codex stint has no Telegram.)

---

## 2. Per-seat variables

| var | recovery | orchestrator |
|---|---|---|
| `PERSONA` | recovery | orchestrator |
| account (uid) | claude-fallback (1001) | claude (1000) |
| `BESPOKE` | claude-telegram-fallback.service | claude-telegram.service |
| `SUPERVISE` | ~/.local/bin/recovery-bot-supervise.sh | ~/.local/bin/claude-bot-supervise.sh |
| `TMUX_SESSION` | claude-telegram-bot-fallback | claude-telegram-bot |
| post-move pane | claude-telegram-bot-fallback:harness.0 | claude-telegram-bot:harness.0 |
| memory dir to pin | /home/claude-fallback/.claude/projects/-home-claude-fallback/memory | /home/claude/.claude/projects/-home-claude/memory |
| `WATCHDOG_TIMER` | recovery-telegram-child-watchdog.timer | (none) |
| `harness_runtime.roles.<role>` | head-system-orchestrator (`instance.yaml:483`) | head-engineering-orchestrator (`instance.yaml:387`) |
| agents entry | `roles.head-system-orchestrator` (`instance.yaml:729`) | `roles.head-engineering-orchestrator` (`instance.yaml:605`) |
| driver | orchestrator, from `claude` via `sudo -S` with the root.env password | recovery, from `claude-fallback` via its NOPASSWD `(claude)` rule (`RECOVERY.md:29`). Fallback: a self-launched transient unit (§5). |
| maintenance armed on | claude-fallback | claude-fallback |

---

## 3. Staging (T-minus): reversible and inert on the bespoke seat

Do S1-S4 days or hours ahead. None of them changes the live bespoke seat's behaviour.

**S1. Commit A: manifest additions, merged to `main` and pulled on the target seat's clone.** Nothing reads the `harness_runtime.roles` stanza while the seat's consume timer is disabled. `persona_bringup.py` is the only runtime reader (`instance.yaml:436-442` says the same for specialist).

Orchestrator (`instance.yaml:387-395`, HO decision 3, plus the 409 carry-over from §9f-5):
```diff
     head-engineering-orchestrator:
       fallbacks:
         - codex/interactive-chatgpt
+      # T-3650 (hand-move, specialist shape): return orchestrator to claude -- and so to
+      # Telegram -- once claude is viable again after a failover to codex.
+      # Mirrors roles.specialist (T-4056). Rollback: set false, restart the
+      # orchestrator controller. mid_session_cap_swap stays OFF (comms seat, T-4037).
+      fallback_return: true
       tmux:
         session: claude-telegram-bot
       post_start:
         - command: ["{account_home}/pantheon-config/tools/arm_pane_log.sh", "{tmux_target}", "{persona}"]
+        # T-3650: carries claude-bot-supervise.sh's pre-launch 409 re-assert
+        # (T-2629/T-4092) onto the control plane, as roles.specialist does.
+        # Idempotent, fail-open; protects the NEXT restart.
+        - command: ["{account_home}/pantheon-config/tools/telegram_poller_409_patch.sh", "{account_home}"]
         - command: ["{account_home}/pantheon-config/tools/recovery_boot_kick.sh", "{tmux_target}", "{persona}"]
           background: true
```
Recovery (`instance.yaml:483-497`): add the same 409 `post_start` line and `fallback_return: true` (DECISION D-10, ratified by Recovery). Without it, a Recovery that fails over to Codex stays on Codex and stays Telegram-dark.

Verify on the target account (after pulling):
```bash
cd ~/pantheon-config && python3 -c 'import sys; from tools import persona_bringup as pb
b = pb.load_binding(pb.DEFAULT_MANIFEST, sys.argv[1])
print(b.controller.fallback_return_enabled, [c.argv[0].rsplit("/",1)[-1] for c in b.controller.post_start])' <persona>
# expect: True ['arm_pane_log.sh', 'telegram_poller_409_patch.sh', 'recovery_boot_kick.sh']
# (the same for recovery: D-10 is ratified)
```

**S2. Pin the memory directory.** Each seat does its own; ORG-035 makes a seat's `settings.json` that agent's domain.
- Claude Code 2.1.283 supports `autoMemoryDirectory` in user settings. It is ignored in project settings, and it defaults to `~/.claude/projects/<sanitized-cwd>/memory/`. This is from the setting's schema string in the installed binary.
- The pin equals today's default, so it is a no-op on the bespoke seat.
- Do it **before** the S-day backups, so the rollback restore keeps it (harmless either way).
```bash
F=~/.claude/settings.json; D=/home/claude/.claude/projects/-home-claude/memory   # recovery: /home/claude-fallback/.claude/projects/-home-claude-fallback/memory
cp -a "$F" "$F.pre-t3650-memdir-$(date -u +%Y%m%dT%H%M%SZ)"
jq --arg d "$D" '.autoMemoryDirectory = $d' "$F" > "$F.tmp" && jq -e type "$F.tmp" >/dev/null && mv "$F.tmp" "$F"
jq -r .autoMemoryDirectory "$F"      # expect: the path in $D
```

**S3. Install the controller template.** It is inert until enabled.
```bash
cp ~/pantheon-config/tools/systemd/pantheon-runtime-controller@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user is-enabled pantheon-runtime-controller@<persona>.service   # expect: disabled
```
Linger is already on for both accounts (`/var/lib/systemd/linger/{claude,claude-fallback}`).

**S4 (Orchestrator only, optional, recommended).** Keep the shared echo cargo cache, which `claude-bot-supervise.sh` exports (T-3937).
- Put the drop-in on **both** launchers, because either one may create the tmux server.
- `umask 002` and the `pantheon-shared` group need no drop-in; §9f-4 has the check.
```bash
for u in pantheon-runtime-controller@orchestrator.service pantheon-persona-consume@orchestrator.service; do
  mkdir -p ~/.config/systemd/user/$u.d
  printf '[Service]\nEnvironment=CARGO_TARGET_DIR=/var/lib/pantheon/echo-target\n' > ~/.config/systemd/user/$u.d/t3650-echo-target.conf
done
systemctl --user daemon-reload
```

**S5. The day before.** Confirm that T-4101 attach has worked live on specialist. Its ticket says "LIVE-VERIFY pending" (`tickets/done/T-4101.md:38`). Recovery's own Gate B also counts as that proof before the Orchestrator move. Run from `claude`:
```bash
PW=$(tr -d '\n' < /home/claude/root.env)
echo "$PW" | sudo -S -k -u claude-research git -C /home/claude-research/pantheon-config merge-base --is-ancestor 4be6cc098 HEAD && echo "specialist clone has T-4101"
echo "$PW" | sudo -S -k -u claude-research grep -c 'controller_start_attached_live_runtime\|controller_start_deferred_lock_contended\|controller_start_respawn_over_foreign_pane' \
  /home/claude-research/.local/state/pantheon/personas/specialist/runtime-controller/controller-evidence.jsonl
# expect: "specialist clone has T-4101", and a count >= 1 after specialist's next natural restart
```

**S-day, immediately before the fire:**
- `STAMP=$(date -u +%Y%m%dT%H%M%SZ)` is the single key for backups, quarantine, and rollback.
- The target's `~/pantheon-config` is on `main`, clean (including untracked files), and at `origin/main`. Nobody writes to it during the window. **Pull it BEFORE arming maintenance**: once the seat is armed its inbox consumer stops pulling (T-4362), so a clone that is behind stays behind, and phase 0 then refuses on the T-4362/T-4371 checks until you run `git -C ~/pantheon-config pull --ff-only` by hand.
- **The target seat** writes `~/HANDOFF.md`. This applies to both seats, since Recovery's session dies at phase 2 too (Recovery review item 5). Everything in the pane's scope dies at phase 2: the Claude process, all Bash-tool background jobs, and any in-process teammates. artisan and mason are on their own accounts and are unaffected.
- Arm maintenance for the target seat (HO decision 6). Since T-4150 (@83e3387fb) the registry is SHARED: `/var/lib/pantheon/seat-maintenance/seat_maintenance.json`, group `pantheon-shared`. An arm from any account on an up-to-date clone reaches the monitors that act on the seat. The maintenance-aware monitors run on claude-fallback: `context_fill_monitor`, `channel_liveness_monitor`, and `continuity_switch_coordinator` (`tools/systemd/README.md:252,60`). **Pull the arming clone first.** A pre-T-4150 CLI still writes the per-account file, and its arm reaches nothing.
  ```bash
  # from any account, on a clone at or after 83e3387fb:
  python3 ~/pantheon-config/tools/notify_policy.py enter-maintenance <persona> --minutes 90 --reason "T-3650 hand-move $STAMP"
  #   expect on stderr: registry: /var/lib/pantheon/seat-maintenance/seat_maintenance.json
  #   a 'PER-ACCOUNT' WARNING means the arm will NOT reach the monitors: stop, pull, re-arm
  #   expect: <persona>: maintenance ARMED until <ts> (90m)
  python3 ~/pantheon-config/tools/notify_policy.py in-maintenance <persona>      # expect: in-maintenance
  ```
  - `OnFailure=` pages (`pantheon-unit-failure-alert@`) are **not** maintenance-aware. A consume exit 1 or a controller start-limit still pages Recovery's triage. Recovery is the driver or observer anyway.
  - Keep maintenance armed until step C3 (§7). If CFM sees the pre-commit-B binding in the gap, it would restart the now-disabled bespoke unit.
  - **Maintenance also pauses the seat's inbox consumer (T-4362, #588 @a85db452e).** While the seat is armed, `pantheon-inbox-consumer@<persona>` skips its whole cycle (`skip:seat_maintenance`): no clone pull and no ticket delivery. That consumer's periodic pull (every 5-10 min) is what moved Recovery's clone mid-window in attempt 2. Consequences: the seat gets no inbox deliveries for as long as maintenance is held, including through the soak (up to the 240 m cap), and the clone pull in C3 is manual and required.
  - The §4 phase 0 **refuses unless the seat is in maintenance** (T-4377), so this arm step can't be silently skipped.
- Save the two scripts from §4 and §6 as `/tmp/handmove-<persona>-$STAMP.sh` and `/tmp/handmove-rollback-<persona>-$STAMP.sh`, mode 0644. They contain no secrets, and the target account must be able to read them.
- Sequencing is unchanged: Recovery first (T-3026), soak, then Orchestrator. Don't stack this with the T-2693 hub-move soak.

---

## 4. The move: seat script (runs as the target account)

Exit codes:
- **0**: moved, and Gates A and B passed.
- **1**: a gate failed, and the rollback ran. Read its output.
- **2**: preflight refused, and **nothing was changed**.

```bash
#!/usr/bin/env bash
# handmove-seat.sh -- T-3650 hand-move of an established seat onto the specialist-shape
# control plane. Runs AS the target seat's account; driver invocations in section 5.
set -uo pipefail
: "${PERSONA:?PERSONA=recovery|orchestrator}" "${STAMP:?STAMP=YYYYmmddTHHMMSSZ}" "${ROLLBACK:?ROLLBACK=path to handmove-rollback.sh}"
cd "$HOME" || exit 2
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
case "$PERSONA" in
  orchestrator) BESPOKE=claude-telegram.service;          SUPERVISE="$HOME/.local/bin/claude-bot-supervise.sh"
         TMUX_SESSION=claude-telegram-bot;          WATCHDOG_TIMER="" ;;
  recovery)  BESPOKE=claude-telegram-fallback.service; SUPERVISE="$HOME/.local/bin/recovery-bot-supervise.sh"
         TMUX_SESSION=claude-telegram-bot-fallback; WATCHDOG_TIMER=recovery-telegram-child-watchdog.timer ;;
  *) echo "unknown persona: $PERSONA" >&2; exit 2 ;;
esac
REPO="$HOME/pantheon-config"
ROOT="$HOME/.local/state/pantheon/personas"
STATE="$ROOT/$PERSONA"
RC="$STATE/runtime-controller"
Q="$STATE/.handmove-quarantine-$STAMP"
REC="$STATE/.handmove-$STAMP.env"
BK=".pre-handmove-$STAMP"
TARGET="$TMUX_SESSION:harness.0"
CONSUME_SVC="pantheon-persona-consume@$PERSONA.service"
CONSUME_TIMER="pantheon-persona-consume@$PERSONA.timer"
CONTROLLER="pantheon-runtime-controller@$PERSONA.service"
SELECTED=claude/interactive-subscription

say()    { printf '[handmove %s %s] %s\n' "$PERSONA" "$(date -u +%H:%M:%SZ)" "$*"; }
refuse() { say "PREFLIGHT REFUSED (nothing changed): $*"; exit 2; }
fail()   { say "GATE FAILED: $*"; say "rolling back via $ROLLBACK"
           PERSONA="$PERSONA" STAMP="$STAMP" bash "$ROLLBACK"; exit 1; }
pane_pid()    { tmux display-message -p -t "$1" '#{pane_pid}' 2>/dev/null; }
pane_scope()  { sed -n 's#^0::.*/\(tmux-spawn-[^/]*\.scope\)$#\1#p' "/proc/$1/cgroup" 2>/dev/null; }
clone_clean() { [ -z "$(git -C "$REPO" status --porcelain=v1 --untracked-files=all)" ]; }

# ---- phase 0: preflight (read-only; any refusal leaves the seat untouched) ----
say "phase 0: preflight"
[ -r "$ROLLBACK" ] || refuse "rollback script $ROLLBACK not readable"
systemctl --user is-active --quiet "$BESPOKE" || refuse "$BESPOKE is not active"
OLD_PANE_PID=$(pane_pid "$TMUX_SESSION") || refuse "no bespoke pane in $TMUX_SESSION"
OLD_PANE_SCOPE=$(pane_scope "$OLD_PANE_PID")
OLD_BOT_PID=$(cat "$HOME/.claude/channels/telegram/bot.pid" 2>/dev/null || true)
# tmux, not readlink /proc/<pid>/cwd: a pane under `sg pantheon-mailbox` carries another gid and
# /proc cwd reads are denied (Recovery review item 1, proven on specialist).
OLD_CWD=$(tmux display-message -p -t "$TMUX_SESSION" '#{pane_current_path}')
# refuse before teardown if the default socket holds anything but the bespoke session: phase 2's
# "no tmux server" check would otherwise guarantee a dark window followed by a rollback (item 4).
[ "$(tmux ls -F '#{session_name}' 2>/dev/null)" = "$TMUX_SESSION" ] \
  || refuse "default socket has other sessions: $(tmux ls -F '#{session_name}' 2>&1 | tr '\n' ' ')"
MEMDIR="$HOME/.claude/projects/$(printf '%s' "$OLD_CWD" | sed 's#[^A-Za-z0-9]#-#g')/memory"
[ -d "$MEMDIR" ] || refuse "live memory dir $MEMDIR not found"
[ "$(jq -r '.autoMemoryDirectory // empty' "$HOME/.claude/settings.json")" = "$MEMDIR" ] \
  || refuse "autoMemoryDirectory is not pinned to $MEMDIR (staging S2)"
[ "$(git -C "$REPO" branch --show-current)" = main ] || refuse "$REPO is not on main"
clone_clean || refuse "$REPO is dirty (bootstrap source provenance needs a clean worktree)"
git -C "$REPO" merge-base --is-ancestor 4be6cc098 HEAD || refuse "$REPO lacks T-4101 (4be6cc098)"
[ -r "$REPO/tools/codex_poller_hold.sh" ] || refuse "$REPO lacks tools/codex_poller_hold.sh (T-4330)"
git -C "$REPO" merge-base --is-ancestor a85db452e HEAD || refuse "$REPO lacks T-4362 (a85db452e, the inbox-consumer maintenance guard); run: git -C $REPO pull --ff-only"
grep -q -- '--render-wait' "$REPO/tools/send_keys_submit.sh" \
  || refuse "$REPO lacks T-4371 (send_keys_submit --render-wait, the fresh-TUI Enter fix for attempt 2's Gate A); run: git -C $REPO pull --ff-only"
# T-4377: the T-4362 guard keeps this clone still only while the seat is in maintenance (shared
# registry), so the arm is checked here, not left to the driver's prose step in section 3. A registry
# read error reads as not-armed and refuses: fail-closed here, while the consumer guard fails open.
grep -q 'def consume_maintenance_bypass' "$REPO/tools/persona_bringup.py" \
  || refuse "$REPO lacks the T-2953 maintenance bypass (consume skips armed seats since T-5282, so phase 4 would launch nothing); run: git -C $REPO pull --ff-only"
python3 "$REPO/tools/notify_policy.py" in-maintenance "$PERSONA" >/dev/null \
  || refuse "$PERSONA is not in maintenance: arm it per section 3 (the shared registry must be readable by $(id -un))"
# T-4335: a hold left by a SIGKILLed or OOM-killed run silently disables this seat's codex
# fallback (a held unit reads "failed", not "active", so the loop below passes). Phase 0 stays
# read-only: refuse, so the driver looks at why that run died before releasing it.
STALE_HOLD=$(bash "$REPO/tools/codex_poller_hold.sh" status) \
  && refuse "a codex poller hold from an earlier run is in place [$STALE_HOLD]; this seat's codex fallback is disabled until: bash $REPO/tools/codex_poller_hold.sh release"
systemctl --user cat pantheon-runtime-controller@.service >/dev/null 2>&1 || refuse "controller template not installed (staging S3)"
for u in "$CONTROLLER" "$CONSUME_TIMER" pantheon-runtime-tmux.scope codex-telegram-poller.service; do
  systemctl --user is-active --quiet "$u" && refuse "$u is already active"
done
EXPECT_ARGV=$(cd "$REPO" && python3 -c '
import sys
from tools import persona_bringup as pb, runtime_controller as rc
b = pb.load_binding(pb.DEFAULT_MANIFEST, sys.argv[1]); t = b.controller.tmux
names = [c.argv[0].rsplit("/", 1)[-1] for c in b.controller.post_start]
assert f"{t.session}:{t.window}.{t.pane}" == sys.argv[2], "manifest tmux target drifted"
assert pb.runtime_reference(b) == sys.argv[3], "manifest primary is not " + sys.argv[3]
assert b.persona != "orchestrator" or b.controller.fallback_return_enabled, "fallback_return missing (staging S1)"
assert "telegram_poller_409_patch.sh" in names, "409 post_start missing (staging S1)"
s = pb.build_adapter(b, harness_home=b.account_home).build_interactive_launch(t, cwd=b.launch_cwd)
print(" ".join(s.argv + ("--settings", str(rc._persona_state(b.persona) / "quota" / "main-session-settings.json"))))
' "$PERSONA" "$TARGET" "$SELECTED") || refuse "manifest preconditions failed (traceback above)"
BACKOFF=$(cd "$REPO" && python3 -c 'import sys; from pathlib import Path; from tools import persona_bringup as pb
print(int(pb.backoff_remaining(Path(sys.argv[1]), sys.argv[2])))' "$ROOT" "$PERSONA")
[ "$BACKOFF" = 0 ] || refuse "consume backoff active for ${BACKOFF}s"
WATCHDOG_WAS=""; [ -n "$WATCHDOG_TIMER" ] && WATCHDOG_WAS=$(systemctl --user is-enabled "$WATCHDOG_TIMER" 2>/dev/null || true)
say "preflight OK: pane $OLD_PANE_PID (${OLD_PANE_SCOPE:-no scope}), bot.pid ${OLD_BOT_PID:-none}, memdir $MEMDIR"
say "expected argv: $EXPECT_ARGV"

# ---- phase 1: record + backups (the rollback reads $REC) ----
say "phase 1: record + backups"
mkdir -p "$Q"
printf 'OLD_PANE_PID=%s\nOLD_PANE_SCOPE=%s\nOLD_BOT_PID=%s\nWATCHDOG_WAS=%s\n' \
  "$OLD_PANE_PID" "$OLD_PANE_SCOPE" "$OLD_BOT_PID" "$WATCHDOG_WAS" > "$REC"
cp -a "$HOME/.claude/settings.json"            "$HOME/.claude/settings.json$BK"
cp -a "$SUPERVISE"                              "$SUPERVISE$BK"
cp -a "$HOME/.config/systemd/user/$BESPOKE"     "$HOME/.config/systemd/user/$BESPOKE$BK"
# recovery: the bun-child watchdog fires recovery_self_restart.sh, whose target follows instance.yaml
# (T-4159). Until the commit marking the seat control-plane lands, that target is still $BESPOKE,
# which would resurrect the bespoke supervisor next to the controller. Off for the move; re-enable
# it once the move has verified and the marker is on main (F4, done: T-4516).
[ "$WATCHDOG_WAS" = enabled ] && systemctl --user disable --now "$WATCHDOG_TIMER"
# T-4330: from here until exit, the codex poller cannot start on this seat's token (a codex
# fallback's first start would confirm the operator's queued messages and deliver none). The trap
# releases the hold on every exit: DONE, fail() (after the rollback also releases it), a signal.
BYPASS="$ROOT/$PERSONA.maintenance-bypass.json"
trap 'rm -f "$BYPASS"; bash "$REPO/tools/codex_poller_hold.sh" release' EXIT
bash "$REPO/tools/codex_poller_hold.sh" hold "$STAMP" || fail "could not hold codex-telegram-poller (T-4330)"

# ---- phase 2: tear down the bespoke supervisor (the seat goes dark here) ----
say "phase 2: teardown"
systemctl --user disable --now "$BESPOKE"
tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
[ -n "$OLD_PANE_SCOPE" ] && systemctl --user stop "$OLD_PANE_SCOPE" 2>/dev/null || true
sleep 3
tmux ls >/dev/null 2>&1 && fail "a tmux server is still up on the default socket: $(tmux ls 2>&1 | head -3)"
# a survivor only if the pid is still a telegram server.ts (pid reuse is not a survivor; item 6)
[ -n "$OLD_BOT_PID" ] && kill -0 "$OLD_BOT_PID" 2>/dev/null \
  && tr '\0' ' ' < "/proc/$OLD_BOT_PID/cmdline" 2>/dev/null | grep -q 'server\.ts' \
  && fail "old telegram poller $OLD_BOT_PID survived teardown"
systemctl --user is-active --quiet codex-telegram-poller.service && fail "codex-telegram-poller is active"

# ---- phase Q: quarantine ALL control-plane residue (move, never delete) ----
# Kept in place: runtime-controller/controller-evidence.jsonl (append-only audit) and
# runtime-controller/runtime-operation.lock (flock anchor shared by consume + controller).
say "phase Q: quarantine -> $Q"
q() { [ -e "$1" ] || return 0; mkdir -p "$2"; mv "$1" "$2/" && say "  moved ${1#"$ROOT"/}"; }
shopt -s nullglob
for rel in runtime.json initial-viability.json initial-viability-invocations transition.json quota; do
  q "$RC/$rel" "$Q/runtime-controller"
done
for f in "$STATE"/staged-cutover-*.json "$STATE/active-runtime.json" "$STATE/mid-session-cap.json" \
         "$STATE/runtimes" "$STATE/overrides" "$STATE/invocations" "$STATE/quota"; do
  q "$f" "$Q/persona-root"
done
for f in "$ROOT/$PERSONA".{json,backoff.json,policyskip.json,softstreak.json,diagstreak.json}; do
  q "$f" "$Q/state-root"
done
shopt -u nullglob
# Workspace trust for the new launch cwd (Recovery review item 2). Claude Code 2.1.283/284 bounds the
# trust walk at the project root and treats home-directory trust as session-only, so a seat whose
# ~/.claude.json lacks projects["$REPO"].hasTrustDialogAccepted blocks on the trust modal at first
# launch -> probe fails -> Gate A fails. Recovery's is false today; orchestrator's is already true (no-op).
# Written here, after teardown, while no claude process is writing the file. umask 077 keeps the
# 0600 mode. The rollback does not need to undo it.
( umask 077; jq --arg p "$REPO" '.projects[$p] = ((.projects[$p] // {}) + {hasTrustDialogAccepted: true})' \
    "$HOME/.claude.json" > "$HOME/.claude.json.t3650" \
  && jq -e --arg p "$REPO" '.projects[$p].hasTrustDialogAccepted == true' "$HOME/.claude.json.t3650" >/dev/null \
  && mv "$HOME/.claude.json.t3650" "$HOME/.claude.json" ) || fail "could not trust $REPO in ~/.claude.json"

# ---- phase 4: ONE consume fire bootstraps the runtime (synchronous, ~1-3 min) ----
say "phase 4: consume bootstrap (launch + provider-completion probe + post_start)"
for _ in $(seq 12); do clone_clean && break; sleep 5; done
clone_clean || fail "$REPO went dirty before bootstrap"
systemctl --user daemon-reload
systemctl --user reset-failed "$CONSUME_SVC" 2>/dev/null || true
# T-2953: the seat is in maintenance (phase 0 requires it), and consume skips armed seats
# (T-5282). This one-shot token lets exactly this fire through; consume deletes it on read
# (on every fire, armed or not). Re-check the arm first: a window that lapsed mid-move would
# leave the monitors unfenced for everything after this point.
python3 "$REPO/tools/notify_policy.py" in-maintenance "$PERSONA" >/dev/null \
  || fail "maintenance for $PERSONA expired mid-move (re-arm with more --minutes and re-run)"
printf '{"persona": "%s", "expires": "%s", "by": "handmove %s"}\n' "$PERSONA" \
  "$(date -u -d '+3 min' +%Y-%m-%dT%H:%M:%SZ)" "$STAMP" > "$BYPASS"
systemctl --user start "$CONSUME_SVC"
[ -e "$BYPASS" ] && { rm -f "$BYPASS"; fail "consume did not read the maintenance bypass token"; }
say "consume: $(systemctl --user show -p Result -p ExecMainStatus "$CONSUME_SVC" | tr '\n' ' ')"

say "gate A"
PANE_PID=$(pane_pid "$TARGET") || fail "no pane at $TARGET (journalctl --user -u $CONSUME_SVC -n 80)"
ARGV=$(tr '\0' ' ' < "/proc/$PANE_PID/cmdline"); ARGV=${ARGV% }
[ "$ARGV" = "$EXPECT_ARGV" ] || fail "pane argv [$ARGV] != expected [$EXPECT_ARGV]"
grep -q 'pantheon-runtime-tmux\.scope' "/proc/$(tmux display-message -p '#{pid}')/cgroup" \
  || fail "tmux server is not in pantheon-runtime-tmux.scope"
[ "$(tmux display-message -p -t "$TARGET" '#{pane_current_path}')" = "$REPO" ] || fail "pane cwd is not $REPO"
(cd "$REPO" && python3 -m tools.runtime_controller initial-viability --persona "$PERSONA" \
   --expect-selected "$SELECTED" --require-source-provenance) || fail "initial-viability is not selected=$SELECTED"
systemctl --user is-active --quiet codex-telegram-poller.service && fail "codex-telegram-poller came up (bootstrap fell to codex)"
BOT_PID=$(cat "$HOME/.claude/channels/telegram/bot.pid" 2>/dev/null || true)
[ -n "$BOT_PID" ] && [ "$BOT_PID" != "$OLD_BOT_PID" ] && tr '\0' ' ' < "/proc/$BOT_PID/cmdline" 2>/dev/null | grep -q 'server\.ts' \
  || fail "telegram receiver not live (bot.pid=${BOT_PID:-none})"
[ "$(pane_scope "$BOT_PID")" = "$(pane_scope "$PANE_PID")" ] || fail "bot.pid $BOT_PID is not in the new pane's scope"
say "gate A passed: pane $PANE_PID"

# ---- phase 5: the controller must ATTACH to that pane, not relaunch it (T-4101) ----
say "phase 5: enable $CONTROLLER"
systemctl --user daemon-reload
systemctl --user enable --now "$CONTROLLER"
sleep 15

say "gate B"
[ "$(systemctl --user show -p ActiveState --value "$CONTROLLER")" = active ] || fail "$CONTROLLER not active"
MAINPID=$(systemctl --user show -p MainPID --value "$CONTROLLER")
[ "${MAINPID:-0}" -gt 0 ] && tr '\0' ' ' < "/proc/$MAINPID/cmdline" | grep -q "controller-main --persona $PERSONA" \
  || fail "controller MainPID ${MAINPID:-0} is not controller-main"
[ "$(pane_pid "$TARGET")" = "$PANE_PID" ] || fail "pane pid changed $PANE_PID -> $(pane_pid "$TARGET"): relaunched, not attached"
python3 -c '
import json, sys
ev = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
st = [e for e in ev if str(e.get("event", "")).startswith("controller_start_")]
ok = bool(st) and st[-1]["event"] == "controller_start_attached_live_runtime" and int(st[-1].get("pid", -1)) == int(sys.argv[2])
sys.exit(0 if ok and json.load(open(sys.argv[3])).get("runtime") == sys.argv[4] else 1)
' "$RC/controller-evidence.jsonl" "$PANE_PID" "$STATE/active-runtime.json" "$SELECTED" \
  || fail "no controller_start_attached_live_runtime for pid $PANE_PID, or active-runtime.json mismatch"
say "gate B passed: controller MainPID $MAINPID attached to pane $PANE_PID"

# ---- phase 6: arm the consume timer last ----
say "phase 6: enable $CONSUME_TIMER"
systemctl --user enable --now "$CONSUME_TIMER"
systemctl --user is-active --quiet "$CONSUME_TIMER" || fail "$CONSUME_TIMER not active"
say "DONE: $PERSONA on the control plane. Next: section 7 verification, completion commit B, soak."
exit 0
```

---

## 5. External driver: exact invocations

The seat's own session dies at phase 2, so it cannot drive its own move. Every form below runs the §4 script **as the target account**.

**Primary form, for BOTH moves: a transient service on the TARGET's user manager** (Recovery review 2026-09-28, item 3).
- A foreground `sudo … | tee` from a Bash tool dies with the DRIVER's session. If the driver's pane restarts mid-move (a context-fill or handoff restart; maintenance covers only the target), sudo and the script die with the driver's tmux-spawn scope. Between phase 2 and phase 4 that leaves the target dark with no rollback run.
- A transient unit belongs to the target's user manager, independent of both sessions. It also sidesteps the Bash-tool timeout.
- `--collect` drops the exit status, so the verdict is the journal line: `DONE`, `GATE FAILED` (the rollback ran), or `PREFLIGHT REFUSED` (nothing changed).

**Recovery's move. The driver is Orchestrator, from `claude`** (`claude` is in the `sudo` group):
```bash
STAMP=<same stamp>; P=recovery
PW=$(tr -d '\n' < /home/claude/root.env)
echo "$PW" | sudo -S -k -u claude-fallback env XDG_RUNTIME_DIR=/run/user/1001 \
  systemd-run --user --unit=pantheon-handmove-$P-$STAMP --collect \
  --setenv=PERSONA=$P --setenv=STAMP=$STAMP --setenv=ROLLBACK=/tmp/handmove-rollback-$P-$STAMP.sh \
  /bin/bash /tmp/handmove-$P-$STAMP.sh
echo "$PW" | sudo -S -k journalctl _UID=1001 _SYSTEMD_USER_UNIT=pantheon-handmove-$P-$STAMP.service -f
```

**Orchestrator's move. The driver is Recovery, from `claude-fallback` (an observing driver).** `claude-fallback` is not in the `sudo` group (`getent group sudo` lists `claude,codex`). It holds `(claude) NOPASSWD: ALL` (`RECOVERY.md:29`). `-n` fails fast if that rule is missing.
```bash
STAMP=<same stamp>; P=orchestrator
sudo -n -u claude env XDG_RUNTIME_DIR=/run/user/1000 \
  systemd-run --user --unit=pantheon-handmove-$P-$STAMP --collect \
  --setenv=PERSONA=$P --setenv=STAMP=$STAMP --setenv=ROLLBACK=/tmp/handmove-rollback-$P-$STAMP.sh \
  /bin/bash /tmp/handmove-$P-$STAMP.sh
# claude-fallback is in systemd-journal, but the group is NOT active in Recovery's session: a plain
# `journalctl _UID=1000 …` prints "-- No entries --" and the observer is blind to DONE / GATE FAILED
# (Recovery review, verified live). Use sudo to the target account, or sg:
sudo -n -u claude journalctl --user -u pantheon-handmove-$P-$STAMP.service -f
#   or: sg systemd-journal -c "journalctl _UID=1000 _SYSTEMD_USER_UNIT=pantheon-handmove-$P-$STAMP.service -f"
```

**Orchestrator's move, self-launched (fallback when Recovery is unavailable).** This is the same transient-service shape, launched from Orchestrator's own shell.
- Do NOT use `--scope`. A `systemd-run --scope` command stays a foreground child of the calling Bash-tool shell. Its stdio are pipes to the Claude process that dies at phase 2, so it would take SIGPIPE mid-move.
- The script's gates roll back on their own, because nobody is at the keyboard.
```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ); P=orchestrator
mkdir -p ~/handmove   # save both scripts here first
systemd-run --user --unit="pantheon-handmove-$P-$STAMP" --collect \
  --setenv=PERSONA=$P --setenv=STAMP=$STAMP \
  --setenv=ROLLBACK="$HOME/handmove/handmove-rollback-$P-$STAMP.sh" \
  /bin/bash "$HOME/handmove/handmove-$P-$STAMP.sh"
journalctl --user -u "pantheon-handmove-$P-$STAMP" -f          # Orchestrator watches until phase 2
# Recovery observes read-only (the systemd-journal group is not active in Recovery's session, so use sudo or sg):
sudo -n -u claude journalctl --user -u "pantheon-handmove-$P-$STAMP.service" -f
```

---

## 6. Manual rollback (HO decision 2; do NOT use `tools/persona_rollback.sh`)

`persona_rollback.sh` aborts on the loaded consume units (`:215-219`) and never touches the controller. The script below is idempotent and safe to re-run. It runs as the target account, through the same driver wrapper as §5 (`bash /tmp/handmove-rollback-<p>-$STAMP.sh`).

```bash
#!/usr/bin/env bash
# handmove-rollback.sh -- T-3650 manual rollback to the bespoke supervisor. Runs AS the target account.
set -uo pipefail
: "${PERSONA:?}" "${STAMP:?}"
cd "$HOME" || exit 2
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
case "$PERSONA" in
  orchestrator) BESPOKE=claude-telegram.service;          SUPERVISE="$HOME/.local/bin/claude-bot-supervise.sh"
         TMUX_SESSION=claude-telegram-bot;          WATCHDOG_TIMER="" ;;
  recovery)  BESPOKE=claude-telegram-fallback.service; SUPERVISE="$HOME/.local/bin/recovery-bot-supervise.sh"
         TMUX_SESSION=claude-telegram-bot-fallback; WATCHDOG_TIMER=recovery-telegram-child-watchdog.timer ;;
  *) echo "unknown persona: $PERSONA" >&2; exit 2 ;;
esac
ROOT="$HOME/.local/state/pantheon/personas"; STATE="$ROOT/$PERSONA"; RC="$STATE/runtime-controller"
Q="$STATE/.handmove-quarantine-$STAMP"; A="$STATE/.handmove-attempt-$STAMP"; REC="$STATE/.handmove-$STAMP.env"
BK=".pre-handmove-$STAMP"
say() { printf '[rollback %s %s] %s\n' "$PERSONA" "$(date -u +%H:%M:%SZ)" "$*"; }
bad=0; must() { say "CHECK FAILED: $*"; bad=1; }
WATCHDOG_WAS=""; [ -r "$REC" ] && . "$REC"

# 1. Both control-plane launchers down FIRST (else Restart=always relaunches the pane killed in step 3).
systemctl --user disable --now "pantheon-persona-consume@$PERSONA.timer" 2>/dev/null || true
systemctl --user disable --now "pantheon-runtime-controller@$PERSONA.service" 2>/dev/null || true
systemctl --user stop "pantheon-persona-consume@$PERSONA.service" 2>/dev/null || true
# 2. Codex poller down and proven absent.
systemctl --user stop codex-telegram-poller.service 2>/dev/null || true
systemctl --user is-active --quiet codex-telegram-poller.service && must "codex-telegram-poller still active"
pgrep -u "$(id -u)" -f 'codex_telegram_bridge\.py exec --mode poller' >/dev/null && must "codex poller process survives"
# T-4330: release the move's poller hold. Both launchers are down, so nothing can start the poller
# now. Inline, not via the repo helper, so the rollback never depends on the clone. Step 7 reloads.
HOLD_DIR="$XDG_RUNTIME_DIR/systemd/user/codex-telegram-poller.service.d"
rm -f "$HOLD_DIR/t4330-handmove-hold.conf"; rmdir "$HOLD_DIR" 2>/dev/null || true
systemctl --user reset-failed codex-telegram-poller.service 2>/dev/null || true
[ -e "$HOLD_DIR/t4330-handmove-hold.conf" ] && must "codex poller hold still present"
# 3. Control-plane pane down: each pane's tmux-spawn scope, then the runtime scope, then the server.
for p in $(tmux list-panes -a -F '#{pane_pid}' 2>/dev/null); do
  s=$(sed -n 's#^0::.*/\(tmux-spawn-[^/]*\.scope\)$#\1#p' "/proc/$p/cgroup" 2>/dev/null)
  [ -n "$s" ] && systemctl --user stop "$s" 2>/dev/null
done
systemctl --user stop pantheon-runtime-tmux.scope 2>/dev/null || true
tmux kill-server 2>/dev/null || true
sleep 3
tmux ls >/dev/null 2>&1 && must "tmux server still up: $(tmux ls 2>&1 | head -3)"
# 4. Park this attempt's control-plane state as evidence, then restore the quarantined residue.
shopt -s nullglob
for rel in runtime.json initial-viability.json initial-viability-invocations transition.json quota; do
  [ -e "$RC/$rel" ] && { mkdir -p "$A/runtime-controller"; mv "$RC/$rel" "$A/runtime-controller/"; }
done
for f in "$STATE/active-runtime.json" "$STATE/mid-session-cap.json" "$STATE/runtimes" \
         "$STATE/overrides" "$STATE/invocations" "$STATE/quota"; do
  [ -e "$f" ] && { mkdir -p "$A/persona-root"; mv "$f" "$A/persona-root/"; }
done
for f in "$ROOT/$PERSONA".{json,backoff.json,policyskip.json,softstreak.json,diagstreak.json}; do
  [ -e "$f" ] && { mkdir -p "$A/state-root"; mv "$f" "$A/state-root/"; }
done
rm -f "$ROOT/$PERSONA.maintenance-bypass.json"   # T-2953: never leave a bypass token behind
for f in "$Q/runtime-controller"/*; do mv "$f" "$RC/"; done
for f in "$Q/persona-root"/*;       do mv "$f" "$STATE/"; done
for f in "$Q/state-root"/*;         do mv "$f" "$ROOT/"; done
shopt -u nullglob
# 5. Bespoke files back from the phase-1 backups.
for f in "$HOME/.claude/settings.json" "$SUPERVISE" "$HOME/.config/systemd/user/$BESPOKE"; do
  [ -e "$f$BK" ] && cp -a "$f$BK" "$f"
done
# 6. Recovery's child watchdog back to its recorded state.
[ -n "$WATCHDOG_TIMER" ] && [ "$WATCHDOG_WAS" = enabled ] && systemctl --user enable --now "$WATCHDOG_TIMER"
# 7. Bespoke supervisor back up.
systemctl --user daemon-reload
systemctl --user reset-failed "$BESPOKE" 2>/dev/null || true
systemctl --user enable --now "$BESPOKE"
sleep 20
systemctl --user is-active --quiet "$BESPOKE" || must "$BESPOKE not active"
tmux has-session -t "$TMUX_SESSION" 2>/dev/null || must "bespoke session $TMUX_SESSION not up"
[ "$bad" = 0 ] && say "ROLLBACK COMPLETE" || say "ROLLBACK INCOMPLETE (see CHECK FAILED lines)"
exit "$bad"
```

**Driver steps after the rollback:**
- If commit B (§7) had already landed, `git revert` it on `main`. Then pull on the seat's clone **and** on claude-fallback's clone **before** `exit-maintenance`. A `supervisor: control-plane` marker on a bespoke seat is the T-3098/T-3082 auto-reset bug.
- Then confirm a Telegram round-trip on the bespoke seat, and run `exit-maintenance` on claude-fallback.

---

## 7. Verification, completion, and soak

**V: run as the target account** (the driver wraps it the same way as §5). `P=orchestrator S=claude-telegram-bot`, or for Recovery `P=recovery S=claude-telegram-bot-fallback`.
```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
systemctl --user show -p ActiveState -p SubState -p MainPID -p NRestarts pantheon-runtime-controller@$P.service
#   ActiveState=active  SubState=running  MainPID=<non-zero>  NRestarts=0
systemctl --user is-enabled pantheon-runtime-controller@$P.service pantheon-persona-consume@$P.timer
#   enabled / enabled
systemctl --user list-timers pantheon-persona-consume@$P.timer --no-pager
#   one row, NEXT <= ~2m20s away
systemctl --user is-active <BESPOKE>; systemctl --user is-enabled <BESPOKE>
#   inactive / disabled
PANE=$(tmux display-message -p -t $S:harness.0 '#{pane_pid}'); tr '\0' ' ' < /proc/$PANE/cmdline; echo; tmux display-message -p -t $S:harness.0 '#{pane_current_path}'
#   <the section-1 argv for this seat> / /home/<acct>/pantheon-config
grep -c pantheon-runtime-tmux.scope /proc/$(tmux display-message -p '#{pid}')/cgroup
#   1
B=$(cat ~/.claude/channels/telegram/bot.pid); tr '\0' ' ' < /proc/$B/cmdline; echo
#   /home/<acct>/.bun/bin/bun server.ts
systemctl --user is-active codex-telegram-poller.service
#   inactive
(cd ~/pantheon-config && python3 -m tools.runtime_controller initial-viability --persona $P \
   --expect-selected claude/interactive-subscription --require-source-provenance)
#   {... "ok": true, "errors": [] ...}  exit 0
busctl --user get-property org.pantheon.PersonaController.$P /org/pantheon/PersonaController/$P \
  org.pantheon.PersonaController1 ActiveRuntime ViabilityStatus
#   s "claude/interactive-subscription"
#   s "viable"
#   (T-2969 health surface; fail-open by design, runtime_controller.py:8574-8599: a missing
#    name is a WARN to note, not a rollback trigger)
journalctl --user -u pantheon-persona-consume@$P.service -n 60 --no-pager | grep -E '"action"|"skipped"|Traceback'
#   after the first timer fire: "action": "noop"|"wake"|"deferred" (recorded runtime reused),
#   or "skipped": "runtime_viability_blocked" once instance.yaml drifts (expected, D-8). Never a Traceback.
```

Then run these in-session checks:
- **Telegram round-trip.** The operator sends one message, and the seat answers on Telegram.
- **Memory check.** Ask the seat to quote the first line of its `MEMORY.md` index. This proves the §3-S2 pin took.
- **Post-start evidence.** `grep post_start_command ~/.local/state/pantheon/personas/$P/runtime-controller/controller-evidence.jsonl | tail -3` shows `arm_pane_log.sh` ok, `telegram_poller_409_patch.sh` ok, and `recovery_boot_kick.sh` launched_background, all timestamped after `$STAMP`.
- **Recovery only.** `bash ~/pantheon-config/tools/verify_startup.sh` is the local health gate. Note that its `AGENT_SERVICE` map (`:95`) still names the bespoke unit (F4).

**C: completion (commit B, the cutover marker).** Land it only after Gate B and V pass. It must not land earlier: a premature marker was reverted twice (T-3098, T-3082).
- **C1. `instance.yaml` agents entry.** Orchestrator is at `:605-637`; Recovery at `:729-767`.
  - Replace the "`supervisor` is INTENTIONALLY ABSENT" comment block with a short "cut over <date> via T-3650" note.
  - Update the `session:` prose to `pantheon-runtime-controller@<p>.service + pantheon-persona-consume@<p>.timer → tmux <session>:0 (window "harness")`.
  - Keep `restart_service`, `restart_mode`, and `restart_pane` as they are. `restart_service` stays as the retired bespoke unit's documentation value, the same way as specialist at `:706-714`.
  - Then add:
  ```diff
       restart_pane: claude-telegram-bot:0
  +    supervisor: control-plane
  +    expected_policy_skip: runtime_viability_blocked
  ```
- **C2. Mirror the fallback exactly.** Guard E, `tools/test_instance_role_grants.py:215`, checks exact dict equality.
  - Update `tools/agent_role_map.py:327-328` (orchestrator) or `:357-358` (recovery) with the same two keys.
  - Flip `test_orchestrator_is_not_control_plane_pre_t2953` (`tools/test_instance_role_grants.py:295-304`), or `test_recovery_is_not_control_plane_pre_t3026` (`:284-293`), to assert `"control-plane"`.
  - Run the guard tests first: `python3 -m pytest -q tools/test_instance_role_grants.py tools/test_fleet_health.py tools/test_context_fill_monitor.py tools/test_restart_drain.py`. Then run the full smoke loop.
- **C3. Pull commit B on the seat's clone and on claude-fallback's clone, by hand.** The monitors read claude-fallback's clone. While the seat is in maintenance, its inbox consumer does not pull (T-4362), so nothing else will bring commit B in.
  - Verify from claude-fallback's clone: `python3 -c 'from tools import context_fill_monitor as c; x=c.AGENT_CONFIG["orchestrator"]; print(x["jsonl_dir"], x.get("restart_service"), x.get("supervisor"))'`. Use `"recovery"` for Recovery.
  - Expect `/home/claude/.claude/projects/-home-claude-pantheon-config pantheon-persona-consume@orchestrator.service control-plane`, the same shape specialist prints today.
  - **Only now** consider `exit-maintenance`. Keep maintenance through the soak if a Codex stint is plausible (T-2959; 240 m cap).

**S: soak.** Before calling the move soaked:
- Three or more scheduled consume fires are clean (no Traceback).
- `NRestarts` stays at 0 for 30 min or more.
- There is one real Telegram round-trip.
- `codex-telegram-poller` is inactive throughout.
- **Recommended proof of swap-on-death on this seat** (reversible; worst case is §6):
  - Run `tmux kill-session -t $S` as the target account.
  - Within ~60 s, expect: a new `pane_pid` at `$S:harness.0` with the same argv; `NRestarts=1`; a new `"outcome": "started"` line in `~/.local/state/pantheon/personas/$P/invocations/$(date -u +%F).jsonl`; `runtimes/claude--interactive-subscription/last-attempt.json` showing `"outcome": "success"` if the killed pane had lived ≥ 300 s. Consume fires in the gap block (`recorded-receiver-not-present`) rather than launch.
  - The relaunched pane gets **no** boot kick (F1). It idles at the composer until `keep-going@orchestrator`, the mailbox waker, or a Telegram message nudges it.
- Keep the `.pre-handmove-$STAMP` backups and the quarantine dir until soaked.

---

## 8. Why phase Q replaced step 3b

The 2026-09-12 step 3b said to leave `initial-viability.json` "[status=selected]" untouched because "the selected-viability record is what keeps orphan-recovery from firing". For Orchestrator that is wrong on both counts:

1. **The premise is false.** Orchestrator's record is `status: exhausted`, recorded 2026-08-16T17:17Z, with both candidates `skipped-cooldown`. It sits next to `runtime.json` = `{}` and seven `initial-viability-invocations/*.json` files. That is exactly the orphan predicate (`runtime_controller.py:3258-3276`). All five of its conjuncts must hold: schema v1; status in {exhausted, selected} (and ≠ selected, via the outer `if`); `runtime.json` exists; `runtime.json` == `{}`; any invocation receipt exists.
2. **Moving `runtime.json` alone would unblock the *first* bootstrap,** because it breaks the "exists" conjunct. It would not keep the block from coming back:
   - If that bootstrap fails, `compensate_failed_initial_candidate` writes `runtime.json = {}` again (`:2940`).
   - Exhaustion records `status: exhausted` again (`:3654-3663`).
   - The seven stale receipts are still present, because new receipts are written only on success (`:3646-3651`).
   - All five conjuncts then hold again, and every later consume fire raises `orphan-runtime-recovery-required`. That is how Orchestrator got here in August (evidence tail: `runtime_viability_candidate_teardown … provider-completion-not-proven`, 2026-08-16T17:06Z).
3. **What `controller_main` needs is none of these files.** It reads only `runtimes/<slug>/last-attempt.json` and `overrides/holds.json` (`:8734-8742`, `:7770-7793`, `:7701-7710`), and neither exists for Orchestrator. The orphan block lives entirely on the consume path (`ensure_viable_runtime` → `_matching_recorded_runtime_selection`, `:3461`).

So step Q moves every control-plane-written file on **both** seats, and keeps only the append-only evidence log and the flock anchor (`_lease.py`: flock-based, so an existing 0-byte lock file is harmless). Orchestrator's set today:
- `runtime-controller/runtime.json` (`{}`)
- `runtime-controller/initial-viability.json` (exhausted)
- `runtime-controller/initial-viability-invocations/` (7 files)
- 19 `staged-cutover-t2953-*.json` at the persona root. Nothing on the runtime path reads these; the writers and readers are the installer and cutover tools (`t2611_recovery_persona_apply.py:102`). They are moved for hygiene per HO decision 4.

Step Q also covers three hazards not present on Orchestrator today but possible on Recovery, whose state is unreadable from here:
- `transition.json` makes consume select the fallback binding (`persona_bringup.py:672-705`) and can open a quota breaker (`runtime_controller.py:2519-2528`).
- A stale `quota/claude-statusline.json` with `authoritative_limit` skips Claude at bootstrap (`:2529-2548`).
- A future-dated `last-attempt.json` cooldown or a `holds.json` makes the controller skip Claude.

On a seat that ran bespoke, any such file is residue by definition.

---

## 9. Installer equivalents and ORG-106 interactions (2026-10-26)

- **Phase Q via the installer (T-5473, T-5474, #1342).** The installer now performs the quarantine and trust halves of phase Q, as activation ops that run after your teardown and before the bootstrap fire; `stage_disabled` does not run them. `service.runtime-residue.quarantine` moves the RESIDUE_* paths into `<STATE>/.installer-quarantine-<UTC stamp>/{runtime-controller,persona-root,state-root}`, never deleting. It leaves `overrides/`, `controller-evidence.jsonl` and `runtime-operation.lock` in place. It runs only when the consume timer is not enabled, and it refuses if `pantheon-runtime-controller@<persona>` or `pantheon-persona-consume@<persona>` is anything but inactive or failed, or is unreadable. On a converged seat (timer enabled) it is a recorded no-op. `harness.claude-workspace-trust` sets `~/.claude.json` projects[<clone>].hasTrustDialogAccepted=true (CAS, merge-preserving, 0600). Rollback moves the quarantined paths back, refusing on any occupied destination; it removes only what the trust op wrote. Keep the manual `q()` loop and the jq write only for a hand-move that does not use the installer.
- **OOM-held seats (ORG-106 L3, T-5526).** Hand-moving a sysorch seat (recovery, watchman) while `/var/lib/pantheon/oom-hold/<persona>` exists, using the phase-4 bypass token, records a `launch_breach` and pages. That is expected: the hold is observed, not refused, and the bypass token never overrides deep-sleep. Wake the seat (`harness <seat> wake`) before the move, or accept the page as the audit trail of a deliberate move.
- **Deep-sleep blocks the move.** Consume skips a deep-sleeping seat, and the bypass token does not apply. A seat that is asleep must be woken before phase 4.

## 10. Follow-ups: code gaps found (not move blockers; suggested homes)

All four are closed. The original text is kept, as found at the time of the move, so the history reads; the line numbers in it are from then.

- **F1** (T-2953 child): `controller_main` should run `binding.controller.post_start` after a non-attach start, as `ensure_runtime` does. At the time, every swap-on-death relaunch came up without the pane log, the 409 re-assert, or the boot kick (`runtime_controller.py:8159-8246` vs `:2745-2760`).
  - **Done**: T-4156, #519 (squash @094556cc3).
- **F2** (T-2950/T-3191, ORG-080(d); or a T-4046 follow-up): the controller path should stop the Codex poller and prove it absent before launching Claude over a Codex pane or record. A consume-path Codex bootstrap starts that poller; the controller path never stopped it (`codex_telegram_actions.py:33-36`).
  - **Done**: T-4158, #523 (merge @34af9e9be).
- **F3** (new ticket): `_rehydrate_recorded_runtime_selection` called `_verify_runtime_input_ready(selected_binding, adapter, started, …)` with three positionals against `(binding, started, *, …)`. That was a `TypeError` every time, confirmed with `inspect.signature(...).bind`.
  - It happened *after* `_start_runtime` had launched, and was masked as `recorded-runtime-rehydrate-failed` once the created scope was stopped (`:3146-3152` vs `:1859-1866`).
  - It was reached only when the record is selected and matching, the pane is absent, and the receiver is present. That is rare for comms seats, because the receiver dies with the pane.
  - **Done**: T-4157, #518 (squash @26c3ff514).
- **F4** (T-3026 / T-2953 cleanup child): re-point Recovery's bun-child watchdog, `recovery_self_restart.sh`, `fleet_reboot.py`, `verify_startup.sh`, and `session_reset_reconcile.py` away from the retired bespoke units for control-plane seats.
  - **Done** (T-4516, 2026-10-05). T-4159 (@c579bb995, landed before recovery's move) already makes all five resolve the target through `agent_role_map.py restart-target`. On recovery that gives `pantheon-persona-consume@recovery.service --supervisor control-plane`. The watchdog timer was re-enabled on recovery. The runtime controller watches only for tmux death and cap swaps, never the Telegram child, so the watchdog is still the only recovery for a mid-session bun-child death.

---

## When to use this

- An **established** Claude persona (recovery, orchestrator) runs its own bespoke Telegram-bot supervisor AND is **install-done**: the consume templates are installed, so `@<persona>` reports `LoadState=loaded` and the automated `persona_cutover.sh` aborts on it.

Do NOT use for:
- A brand-new or from-scratch seat → `migrating-an-existing-persona-onto-the-control-plane.md`.
- Recovery of an already-migrated, wedged daemon → `../remediation/`.

## Coordination + close-out

- The fire is a supervised, maintenance-windowed, externally driven event, not a self-fire. The transient-unit journal line (DONE / GATE FAILED / PREFLIGHT REFUSED) is the verdict.
- **Recovery first** (T-3026; smaller blast radius, not the HO singleton), soak, then orchestrator (T-2953). Don't stack a move on another migration's soak.
- Orchestrator's commit B additionally requires T-4159 (landed @c579bb995), which makes the bespoke-unit consumers follow the `supervisor` marker.
- Post-move live checks: T-4164.

## Boundary

Covers the manual move of an established, install-done Claude seat (recovery, orchestrator) from a bespoke Telegram-bot supervisor onto the specialist-shape control plane. Out of scope:
- from-scratch or new-seat bringup (the automated `migrating-…` SOP);
- any change to the installer or germination engine (out of bounds per the operator's 2026-09-11 steer);
- the code follow-ups F1-F4 (§10), which are tracked as their own tickets.

## Cross-references

- `notes/2026-09-28-t3650-handmove-hc-variant-draft.md`: full reasoning (§9) and DECISIONS D-1..D-15.
- `tools/systemd/README.md`: Runtime controller section (the unit template, and coexistence with consume).
- `sop/standard/migrating-an-existing-persona-onto-the-control-plane.md`: the automated sibling for from-scratch seats.
- `sop/standard/hc-harness-layer-regression-testing.md`: soak criteria.
- Memory pins: `reference_hc_cutover_breaks_on_install_done_loaded_consume_units`, `reference_handmove_fails_on_stale_recorded_runtime_scope` (the 2026-09-12 step-3b diagnosis, now superseded by phase Q; see §8).
- Tickets: T-3650 (this design), T-3026 (the recovery move), T-2953 (the orchestrator move / HC arc), T-4150 (shared maintenance registry), T-4159 (bespoke-consumer re-point), T-4156/T-4157/T-4158 (F1-F3), T-4164 (post-move live checks). The operator steer: TG 2026-09-11 msgs 6931/6932/6934; Option-2 ruling TG 6042.
