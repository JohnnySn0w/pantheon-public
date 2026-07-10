# Adding full restart-capability to a new Pantheon agent

**Status:** **Ratified 2026-05-22 by the operator (Telegram msg_1884).** Drafted 2026-05-21 by Recovery (T-0518, child of T-0517 parity epic). **Amendments ratified 2026-05-22 by the operator (T-0546):** capability 3/4 drain mechanics (all-reasons `--skip-ack` per T-0534; detached-cgroup transient unit per T-0535) + the "if the first-fire isn't clean → disarm" protocol (T-0542) + the T-0536 hook-tripwire cross-ref. The amended sections are marked inline and are now fully ratified.

This is the install runbook for giving a NEW always-on Pantheon agent (its own Linux user + `pantheon-config` clone + Telegram-bridge session) the full ORG-020 restart stack. Worked example throughout = **Recovery** (`claude-fallback`), which built and proved the reference impl on 2026-05-21 (T-0509 / T-0510).

For the operator-facing surface of the wrapper itself (how to *fire* a restart), see [`handoff-restart-runbook.md`](handoff-restart-runbook.md) — this SOP is how to *install* the capability, that one is how to *use* it.

## When to use this

- Onboarding a new persistent agent (a 4th sibling beyond HO/Orchestrator/Specialist/Recovery) that runs a long-lived Claude Code session and must self-heal.
- Back-filling a capability gap on an existing agent (the T-0517 parity matrix: bring all 5 capabilities to all 3 of Recovery/Orchestrator/Specialist).

Do NOT use for: one-off manual restarts (just fire the wrapper), or the Max-window cap-hit auto-resume path (separate mechanism — `tools/ho_resumer.py` + `pantheon-ho-resumer.timer`; see [`auto-resume-operator-runbook.md`](auto-resume-operator-runbook.md)).

## The 5 capabilities (what "full restart-capability" means)

Per T-0517. An agent has the full stack when all five are present:

1. **Auto-respawn on process exit** — systemd `Restart=always` + a supervisor wrapper that stays alive as `MainPID` for the session's lifetime.
2. **Context-fill auto-restart** — `context_fill_monitor.py` polls the session JSONL and graceful-restarts at the fill threshold.
3. **Restart drain** — `restart_drain.py` consumes tombstone files and fires `handoff_and_restart.sh` (decoupled from the agent's own session, so the restart survives killing that session).
4. **API-health force-restart** — detect an alive-but-400ing session and FORCE-restart it (`--skip-ack`, no handoff — a poisoned session can't write one).
5. **Fresh-boot org pointers** — the freshly-restarted session reads its `HANDOFF.md` + org context and resumes (SessionStart auto-read hook and/or a post-restart kick).

## End-to-end recipe

Substitute `<agent>` (e.g. `recovery`), `<user>` (e.g. `claude-fallback`), `<service>` (e.g. `claude-telegram-fallback.service`), `<session>` (the tmux session, e.g. `claude-telegram-bot-fallback`) throughout. **Verify `<session>` against the unit's actual `ExecStart` — do NOT guess it (see Gotchas).**

### 1. systemd user unit + supervisor wrapper (capability 1)

The unit must NOT be `Type=oneshot` + `RemainAfterExit=yes` — that reports "active (exited)" forever even after the session dies, defeating `Restart=`. Use a wrapper that blocks as `MainPID`:

```bash
# ~/.local/bin/<agent>-bot-supervise.sh  (runs as <user>)
SESSION="<session>"
CLAUDE_BIN="$HOME/.npm-global/bin/claude"
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official"
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" "$CLAUDE_BIN $CLAUDE_ARGS"
fi
# Block as systemd's MainPID until the session goes away → exit → Restart=always respawns.
while tmux has-session -t "$SESSION" 2>/dev/null; do sleep 5; done
exit 0
```

```ini
# ~/.config/systemd/user/<service>
[Service]
ExecStart=%h/.local/bin/<agent>-bot-supervise.sh
ExecStop=/usr/bin/tmux kill-session -t <session>
Restart=always
```

`systemctl --user enable --now <service>`. (See the RCA at the top of Recovery's `recovery-bot-supervise.sh` for why the wrapper exists.)

**Canonical source (T-0526):** Recovery's wrapper is vendored at `tools/systemd/recovery-bot-supervise.sh` (don't author from scratch — copy + adapt `SESSION`/`CLAUDE_ARGS`). Deploy it to `~/.local/bin/` with the copy-on-deploy script `tools/systemd/deploy-recovery-supervisor.sh` (backs up the live file, installs, `bash -n`-verifies, restores on failure, no-ops when in sync) rather than hand-editing the live path. Copy-on-deploy (not symlink) is deliberate: the live wrapper is the agent's launch path and must not depend on the repo being present at respawn time. Per-agent wrappers for orchestrator/specialist get vendored + a deploy path the same way under T-0517.

### 2. Context-fill monitor (capability 2)

Add the agent's entry to `AGENT_CONFIG` in `tools/context_fill_monitor.py`:

```python
"<agent>": {
    "jsonl_dir": Path("/home/<user>/.claude/projects/-home-<user>"),
    "sudo_user": "<user>",
    "restart_service": "<service>",
    "restart_mode": "user",
    "restart_target_pane": "<session>:0",
    "handoff_file": "/home/<user>/HANDOFF.md",
    "restart_bin": "/home/<user>/pantheon-config/tools/handoff_and_restart.sh",
    "use_tombstone": True,   # route restarts through the drain (see step 3)
},
```

Install + arm the per-agent timer (template units in `tools/systemd/`): `pantheon-context-fill-monitor@<agent>.timer` (cadence `OnCalendar=*-*-* *:0/5:00` = 5 min). Then run a **SUPERVISED first-fire** — synthesize a threshold-crossing (or a synthetic tombstone) and watch one restart land cleanly with an operator observing, before trusting it unattended.

### 3. Restart drain (capability 3)

Add the agent to `DRAIN_AGENT_CONFIG` in `tools/restart_drain.py`:

```python
"<agent>": {
    "restart_service": "<service>",
    "restart_mode": "user",
    "restart_target_pane": "<session>:0",
    "idle_sentinel": "❯|esc to interrupt|\\? for shortcuts|ctrl\\+t to hide tasks",  # see Gotchas
    "handoff_file": "/home/<user>/HANDOFF.md",
    "restart_bin": "/home/<user>/pantheon-config/tools/handoff_and_restart.sh",
    "restart_lockfile": "/tmp/handoff_and_restart_<agent>.lock",
},
```

Install `pantheon-restart-drain.{service,timer}` on the agent's account (cadence `OnCalendar=*-*-* *:*:0/30` = 30 s). Enable via a drop-in (the in-script flag is operator-visible in the journal, unlike a `ConditionEnvironment=`):

```ini
# ~/.config/systemd/user/pantheon-restart-drain.service.d/enable.conf
[Service]
Environment=PANTHEON_RESTART_DRAIN_ENABLED=1
```

`systemctl --user daemon-reload`. **Each account runs its OWN drain** (reads its own tombstones, same-account, no cross-account sudo) — do not build a cross-account drain (the sudo paths only run Recovery→Orchestrator/Specialist, not the reverse).

**How the drain fires the restart (amended 2026-05-22, T-0534 + T-0535; REVISED 2026-06-12, T-0763):**
- **`--skip-ack` is now gated on the tombstone REASON** (T-0763, superseding T-0534's blanket "always skip-ack"). Two paths:
  - **FORCED restart (`reason=api_health_force`) → `--skip-ack`.** A 400ing / poisoned session genuinely *cannot* write a handoff, so skip the in-session ack-wait (steps 2–4) and go straight restart → is-active → steady-state → kick.
  - **GRACEFUL restart (`reason=context_fill_threshold` / `operator` / other) → NO `--skip-ack`.** The agent is HEALTHY (context-fill is a graceful restart of a working session); `handoff_and_restart.sh` steps 2–4 prompt it to refresh `HANDOFF.md` and wait for the mtime-ack BEFORE the kill, so the fresh session boots from a current handoff, not a stale one. The agent writes the handoff autonomously via send-keys — so T-0534's premise ("no interactive operator → can't write a handoff") never actually meant a healthy session *cannot* write one. (T-0534's blanket skip-ack came from the 2026-05-22 T-0469 first-fire blocking on the ack-wait; T-0763 fixes the real bug — a context-fill restart cutting off the handoff-write, leaving the fresh session lost on a stale handoff — without re-breaking the forced path.) **Validated live 2026-06-12:** Recovery's own context-fill graceful restart wrote a fresh `HANDOFF.md` (mtime-ack) and the fresh session booted from it correctly.
- **Detached transient unit** (T-0535). The drain launches `handoff_and_restart.sh` from a **detached `systemd-run --user --collect --unit=drain-restart-<stem>` transient unit** (its own cgroup), mirroring `self_restart.sh` (T-0531). This is what actually makes the restart survive the drain being stopped/disabled or the timer ticking. The earlier "separate unit from the *service* unit, so it survives the restart" reasoning covered the agent-service-restart case but NOT this one: pre-T-0535 the restart ran as a *child of `drain.service`'s cgroup*, so stopping the drain mid-flight SIGTERM-killed the in-progress restart (the 2026-05-22T00:25Z incident). The tombstone is removed on launch-success, closing a double-fire race.

### 4. API-health force-restart (capability 4)

Already generic in the shared code once steps 2–3 are done — it rides the same monitor + drain:

- `context_fill_monitor.py`'s `detect_sustained_api_errors()` writes a tombstone with `reason="api_health_force"` when it sees a sustained leading run of status-400 `isApiErrorMessage` records at the JSONL tail (gated on `use_tombstone` + the `PANTHEON_API_HEALTH_ENABLED` kill-switch).
- `restart_drain.py` runs `handoff_and_restart.sh --skip-ack` for `api_health_force` (a 400ing session can't write a handoff). Note (T-0763, revising T-0534): `--skip-ack` is now used ONLY for the forced `api_health_force` path — graceful reasons (`context_fill_threshold` / `operator` / other) run WITHOUT it so the healthy agent writes a fresh handoff first (see capability 3's "How the drain fires the restart"). `api_health_force` is thus distinguished both by its detection trigger AND by being the skip-ack (no-handoff-beat) path.

Ships **default-OFF**. Enable with a monitor drop-in mirroring the drain's:

```ini
# ~/.config/systemd/user/pantheon-context-fill-monitor@<agent>.service.d/enable.conf
[Service]
Environment=PANTHEON_API_HEALTH_ENABLED=1
```

`systemctl --user daemon-reload`; confirm with `systemctl --user show pantheon-context-fill-monitor@<agent>.service -p Environment`. Run a supervised first-fire (synthetic `restart_requested_<agent>.json` tombstone with `reason=api_health_force`) before trusting it. Detection is silent-when-healthy by design — there is no per-cycle "active" heartbeat; the witnesses are the env-in-unit + the cycle falling through to the context-fill log.

### 5. Fresh-boot org pointers (capability 5)

The fresh session must read its `HANDOFF.md` + org context on boot. Two complementary mechanisms:

- **SessionStart auto-read hook** — injects `Read /home/<user>/HANDOFF.md` as `additionalContext`. NOTE: `additionalContext` does NOT trigger a first turn; it seeds context for when one arrives. (Recovery has no such hook and reads HANDOFF manually per a MEMORY pointer — acceptable for an agent that's reliably kicked.)
- **Post-restart kick** — `handoff_and_restart.sh` step 8 send-keys a directive ("Read HANDOFF.md, then post a status snapshot") into the fresh pane to actually trigger the first turn + an operator-visible surface.

**Kick MUST be delivered via send-keys to the running bridge session — NOT as a launch-arg.** (See Gotchas.)

## Gotchas (learned the hard way — Recovery, 2026-05-21)

- **Stale `--idle-sentinel` → restart loop.** The shared default (`? for shortcuts` / `ctrl+t to hide tasks`) is STALE for Claude Code 2.1.x; the live pane shows `❯` (idle) / `esc to interrupt` (busy). With the stale default, step-7 steady-state never matches → exit 7 → drain marks the fire failed → retries every 30 s → **restart loop.** Always pass the version-robust `idle_sentinel` (matches idle OR busy OR the old markers; still won't match a startup settings-error dialog, preserving the T-0172 guard).
- **Verify the tmux session name against the unit `ExecStart` — never guess.** `claude-fallback-bot:0` was a wrong guess; the real session is `claude-telegram-bot-fallback:0`. A wrong target-pane silently breaks the handoff/capture/kick steps.
- **Same-account vs cross-account.** Same-account ops need no sudo. Cross-account (e.g. Recovery driving Orchestrator's pane) MUST wrap as `sudo -u <user> bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) tmux ...'` — a bare `tmux send-keys -t <session>` runs against the CALLER's tmux server and can prefix-match the caller's own session (silently typing into the wrong pane). Bug surfaced 2026-05-19.
- **`SupplementaryGroups=pantheon-shared` only for shared clones.** Needed only if the agent's `pantheon-config` clone is a kino-shared/group-owned checkout (effectively HO-only today). Single-owner clones (Recovery/Specialist) don't need it.
- **Launch-arg kick injection does NOT work with the `--channels` bridge.** Passing the kick as `claude`'s positional `[prompt]` arg seems attractive (no send-keys Enter race) but is incompatible with bridge mode: `claude --channels … "<prompt>"` does not honor the positional prompt and the session dies within ~5 s, then `Restart=always` brings up a blank "tabula rasa" bot (drop-file already consumed). Verified + reverted 2026-05-21 (T-0519, commit `02d581e`). **Deliver the kick via post-restart `tmux send-keys` into the running session**, with a settle delay between text and Enter (tmux paste-mode batches successive send-keys; without the delay the Enter is swallowed and the text sits unsubmitted — `_apply_auto_resolve_action` uses 0.7 s).
- **Fresh-boot dev-channels dialog wedge.** On first boot a Claude Code dialog (`I am using this for local development` / `Exit`) can block the loop from connecting to the bridge — the agent shows as "not in connected_clients." This sentinel is in `RECOVERY_AUTO_RESOLVE_ALLOWLIST`; the ratified dismiss is send-keys `["1","Enter"]`. (T-0520 made the orchestrator-health monitor auto-resolve this on the disconnect path; T-0521 folds dismiss+kick into the supervisor's auto-respawn.)

## Post-install verification checklist

- [ ] `systemctl --user is-active <service>` → `active`; kill the session and confirm it auto-respawns (capability 1).
- [ ] `systemctl --user list-timers` shows `pantheon-context-fill-monitor@<agent>.timer` and `pantheon-restart-drain.timer` armed.
- [ ] Drain enabled: `journalctl --user -u pantheon-restart-drain.service` shows cycles (not "drain disabled").
- [ ] **Supervised context-fill first-fire**: one threshold-crossing restart lands cleanly (handoff written → restart → fresh session reads HANDOFF → posts status) with an operator watching.
- [ ] **Supervised api-health first-fire** (if enabling capability 4): synthetic `reason=api_health_force` tombstone → drain → `--skip-ack` restart → fresh session up clean; `PANTHEON_API_HEALTH_ENABLED=1` resolves into the monitor unit env.
- [ ] Kick verified: the fresh session actually takes its first turn and posts a status surface (NOT left idle).
- [ ] `idle_sentinel` correct for the installed Claude Code version (no exit-7 loop in the drain journal).

### If the first-fire is NOT clean → disarm, don't trust (protocol, amended 2026-05-22 T-0542)

A supervised first-fire that doesn't land clean (the fresh session wedges — e.g. on the trust-folder / dev-channels boot dialog — or the kick is eaten and it comes up idle) means this agent is **not yet safe to leave armed**: an unattended restart (context-fill / api-health tombstone) would wedge the same way with no auto-recovery. **Disarm the agent's drain until the blocker is fixed:**

```bash
# on the agent's account (XDG_RUNTIME_DIR=/run/user/$(id -u))
systemctl --user disable --now pantheon-restart-drain.timer
rm -f ~/.config/systemd/user/pantheon-restart-drain.service.d/enable.conf
systemctl --user daemon-reload
# verify: is-enabled → disabled, is-active → inactive, DRAIN_ENABLED env absent
```

The drain **units stay installed** (static service + disabled timer) — disarmed, not removed — so re-arming after the fix is just re-adding the drop-in + `enable --now`. **Pre-flight before `disable --now`:** confirm the drain isn't mid-restart (`is-active` inactive/dead, no `handoff_and_restart` process) — `disable --now` SIGTERM-kills an in-flight restart (the T-0535 race). Re-arm only after a clean supervised re-fire. Precedent: 2026-05-22 — Orchestrator (briefly) and Specialist (T-0542, disarmed pending T-0541 boot-dialog auto-dismiss + Specialist recovery coverage).

**Adjacent security capability (T-0536):** the context-fill monitor also carries a **hook-allowlist tripwire** — each cycle it checks the agent's `.claude/settings.json` auto-run hooks against a per-agent allowlist and alerts Recovery on any deviation (injected SessionStart hook = the Mini Shai-Hulud persistence vector). When onboarding a new agent, add its verified-clean hook set to `HOOK_ALLOWLIST` in `context_fill_monitor.py`.

## Boundary

Covers the 5-capability self-heal stack only. Out of scope: Max-window cap-hit auto-resume (`ho_resumer.py`), the activity/wedge monitors (ORG-011), and bridge-health detection (`inbox_consumer.py` orchestrator-health) — those are orthogonal mechanisms with their own runbooks.

## Cross-references

- [`handoff-restart-runbook.md`](handoff-restart-runbook.md) — how to *fire* the wrapper (operator surface).
- [`auto-resume-operator-runbook.md`](auto-resume-operator-runbook.md) — cap-hit auto-resume + context-fill monitor operator surface.
- [`cross-account-ops-discipline.md`](cross-account-ops-discipline.md) — Recovery repair-sudo + cross-account patterns.
- [`../remediation/recovery-service-recovery.md`](../remediation/recovery-service-recovery.md) — what to do when the bot service is down/wedged.
- ADR ORG-020 (context-fill monitor + restart drain; addendum pending under T-0545 for the T-0535 cgroup race), T-0509 / T-0510 (Recovery reference impl), T-0517 (parity epic), T-0519 (launch-arg kick dead-end), T-0520 / T-0521 (dev-channels boot wedge), T-0531 (`self_restart.sh` / detached-transient-unit pattern), T-0534 (all-reasons skip-ack), T-0535 (detached-cgroup drain), T-0536 (hook-allowlist tripwire), T-0542 (Specialist drain disarm precedent).
