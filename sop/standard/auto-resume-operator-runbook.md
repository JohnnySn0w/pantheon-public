# Auto-resume — operator notes

The pattern: HO runs continuously, hits the Max plan's per-window cap, halts cleanly, gets woken by an external timer when the window refreshes. This doc is for the human in the loop — how to disable, inspect, verify, and troubleshoot.

For the architecture rationale + ratification trail, read `BACKLOG.md` §B8 and the source research at `knowledge/episodic/2026-05-03-claude-code-limit-handling/README.md`.

## What runs where

| Component | Path | Where it runs |
|---|---|---|
| StopFailure hook | `tools/hooks/ho_stop_failure.py` | In-process, fires on rate-limit halt; registered in `~/.claude/settings.json` `hooks.StopFailure` |
| Resumer script | `tools/ho_resumer.py` | systemd-cron'd via `pantheon-ho-resumer.timer`, every 5 min on the `claude` user |
| State file | `~/.local/state/pantheon/ho_limit_hit.json` | Written by the hook, read by the resumer, rotated post-fire to `*.consumed.<epoch>` |

## What a clean cycle looks like

1. HO is working. The Max window cap arrives.
2. `StopFailure` fires with `error_type: "rate_limit"`. The hook script writes `ho_limit_hit.json` with `session_id`, `resets_at` (Unix epoch parsed from the human-readable error message; `+5h` fallback if the parser couldn't extract a clean time), `transcript_path`, `error_message`, `written_at`.
3. The resumer's 5-minute timer continues firing. Each fire bails because `now < resets_at + 90s`. Cheap.
4. Once `resets_at + 90s` arrives, the next timer fire passes the time check. Pre-fire health check (`pgrep -f 'claude.*--resume'` empty? state file mtime fresh?) passes. The script fires `claude --resume <session_id> -p "<locked-resumer-prompt>"`.
5. State file rotates to `ho_limit_hit.json.consumed.<epoch>`. Next fire bails because the canonical state file is gone.
6. HO wakes up in the resumed session, reads the resumer prompt, checks the latest ledger entry, picks up where it left off.

## Disable

If you need to stop the auto-resume rhythm (debugging, planned downtime, manual mode):

```bash
# As claude:
systemctl --user stop pantheon-ho-resumer.timer
systemctl --user disable pantheon-ho-resumer.timer
```

To re-enable:

```bash
systemctl --user enable --now pantheon-ho-resumer.timer
```

If you want the hook to stay registered but not write state files (e.g., during a manual halt-and-stay-down test), edit `~/.claude/settings.json` and remove the `StopFailure` block — the resumer becomes a no-op since no state file ever lands.

## Inspect

What's in the state file right now:

```bash
cat ~/.local/state/pantheon/ho_limit_hit.json | python3 -m json.tool
```

How the resumer is doing:

```bash
systemctl --user list-timers pantheon-ho-resumer*
journalctl --user -u pantheon-ho-resumer.service -n 50 --no-pager
```

Past consumed cycles (forensic; one file per resume):

```bash
ls -lh ~/.local/state/pantheon/ho_limit_hit.json.consumed.*
```

## Verify a halt/resume cycle ran cleanly

After a cycle, check:

1. **Halt landed cleanly:** `journalctl --user -u pantheon-ho-resumer.service` shows the resumer firing once, exiting 0. Log line includes `[ho_resumer]` prefix.
2. **State file rotated:** the canonical `ho_limit_hit.json` is absent; a `ho_limit_hit.json.consumed.<epoch>` exists with the recent epoch.
3. **HO resumed:** the latest entry in the current weekly ledger shard (`ledger/<ISO-year>-W<NN>/`, ORG-001; e.g. `ls -t ledger/*-W*/*.jsonl | head -1`) is HO's first post-resume action. Should reference the resumer-prompt's "previous halt was a Max-window cap-hit" framing.
4. **No double-resume:** only one `claude --resume` invocation in the journal for that cycle. The pre-fire `pgrep` check should have caught any second fire.

If any of these are off, see Troubleshooting.

## Reset stuck state

If the state file is present but the resumer keeps bailing (or never fires when it should):

```bash
# Inspect the state file:
stat ~/.local/state/pantheon/ho_limit_hit.json
cat ~/.local/state/pantheon/ho_limit_hit.json | python3 -m json.tool
```

Common stuck states:
- **`resets_at` is in the past, but mtime is fresh** → the resumer SHOULD have fired. Check `pgrep -f 'claude.*--resume'`; if non-empty, a prior `claude --resume` is hung. Kill it; the next 5min poll will fire fresh.
- **`resets_at` is `null` or absent** → the parser fell back without writing the field. Manually compute the desired wake time and edit the file, or rotate it out of the way (`mv ho_limit_hit.json ho_limit_hit.json.manual-bail.<epoch>`) and let the next halt write a fresh one.
- **mtime older than 24 hours** → the resumer's freshness check correctly bails. If you want it to fire anyway (manual recovery), `touch` the file to refresh mtime.

To force a manual resume right now (skip the timer):

```bash
python3 ~/pantheon-config/tools/ho_resumer.py
```

It runs the same logic the timer-driven fire runs. Bails clean if state is missing or not-yet-time.

## Troubleshooting checklist

| Symptom | Check | Fix |
|---|---|---|
| Cap-hit happened but no state file | `~/.claude/settings.json` `hooks.StopFailure` registered? Hook file executable? | Re-apply registration; `chmod +x tools/hooks/ho_stop_failure.py` |
| State file present but resumer never fires | Timer enabled? `systemctl --user is-active pantheon-ho-resumer.timer` | `systemctl --user enable --now pantheon-ho-resumer.timer` |
| Resumer fires but `claude --resume` errors | Session id valid? Check `error_message` field — was the original halt clean? | Manual `claude --resume <id>` from shell to see the actual error |
| State file mtime stuck | `loginctl enable-linger claude` set? (timer needs linger to fire when user not logged in) | `loginctl enable-linger claude` |
| Multiple resumes in same cycle | `pgrep` check failing? race between timer fires? | Check journal for multiple `[ho_resumer]` fires; expected: one fire, one rotation, then bails |

## Supervised first-fire checklist

Per the B8 ratification (msg_396), the first 1-2 cap-hit/resume cycles run with a human in the loop. About ~10 hours of real time; you don't need to screen-watch, just stay reachable.

For the first cycle:

- [ ] **Before the halt:** `journalctl --user -u pantheon-ho-resumer.service -f` in a terminal so you see fires live.
- [ ] **At halt:** state file lands at `~/.local/state/pantheon/ho_limit_hit.json`. Sanity-check the parsed `resets_at` is in the future and looks reasonable (within ~5h).
- [ ] **During the wait:** every 5 min, the journal shows the resumer bailing with `not yet time, resets_at=<epoch>` log line.
- [ ] **At the resume window:** the resumer fires `claude --resume <session_id>`. Journal shows the invocation. State file rotates.
- [ ] **After resume:** open the latest HO ledger entry; confirm HO read the resumer prompt and resumed cleanly.
- [ ] **Smoke pass:** if all of the above land cleanly, run a second cycle the same way.

If anything goes sideways: the failure surface is bounded by the $10/mo extra-usage cap (per msg_396) and the resumer's own pre-fire health checks. You can disable the timer at any moment with `systemctl --user stop pantheon-ho-resumer.timer`.

## Context-fill monitor — preemptive graceful restart (per-agent)

Distinct from the cap-hit cycle above. The cap-hit cycle is **reactive** (an agent already halted, we resume it). The context-fill monitor is **preemptive**: it watches each long-lived agent's session JSONL transcript and fires a graceful restart before context bloat causes degradation. Per T-0178/T-0184 (Orchestrator-only) → T-0179/T-0189 (per-agent generalization).

### Coverage

| Agent | Account | Tmux pane | Restart-service unit | Status |
|---|---|---|---|---|
| Orchestrator | `claude` | `claude-telegram-bot:0` | `claude-telegram.service` (--user) | enabled |
| Specialist | `claude-research` | `claude-research-bot:0` | `claude-telegram-research.service` (--user) | enabled |
| Recovery | `claude-fallback` | `claude-fallback-bot:0` | `claude-telegram-fallback.service` (--user) | **tombstone path** — Recovery writes a tombstone file; Recovery's own `pantheon-restart-drain.timer` (separate unit, ~30s cadence) drains it and fires `handoff_and_restart.sh`. Per ORG-020 Phase 1 (T-0309). |

### What runs where

| Component | Path | Where it runs |
|---|---|---|
| Monitor script | `tools/context_fill_monitor.py --agent <agent>` | systemd template, 5-min cadence, on `claude-fallback` (Recovery) |
| Template unit | `tools/systemd/pantheon-context-fill-monitor@.{service,timer}` | One template; instances per agent (`@orchestrator`, `@specialist`, `@recovery`) |
| Per-agent state file | `~/.local/state/pantheon/context_fill_<agent>_last_restart.json` (Recovery's home) | Written on successful fire or tombstone-write; gates per-agent 30-min cooldown |
| Per-agent config | `AGENT_CONFIG` dict in `tools/context_fill_monitor.py` | jsonl dir + sudo user + restart service + tmux pane per agent |
| Restart wrapper | `tools/handoff_and_restart.sh` | Invoked per-agent via Recovery's NOPASSWD-sudo into the agent's account (Orchestrator/Specialist); local on the agent's own account (Recovery's drain) |
| Recovery tombstone | `~/.local/state/pantheon/restart_requested_recovery.json` (Recovery's home) | Written by monitor when Recovery threshold crossed; drained by Recovery's `pantheon-restart-drain.timer` |
| Drain timer | `tools/systemd/pantheon-restart-drain.{service,timer}` | Installed on each agent's own account; ~30s cadence; reads + drains local tombstones; feature-flagged behind `PANTHEON_RESTART_DRAIN_ENABLED=1` |
| Operator restart CLI | `tools/restart_request.py --agent <recovery\|orchestrator\|specialist>` | Run manually or via `/restart` Telegram command (bridge integration pending); writes operator tombstone; drained by same drain timer |

### Trigger conditions (per agent)

The monitor fires `handoff_and_restart.sh` against the named agent when ALL of:
1. Not in cooldown for that agent (≥30 min since the agent's last successful fire — separate state files per agent).
2. The agent's latest session JSONL exists and is readable via sudo'd `find`/`tac` into the agent's account.
3. Most-recent `message.usage` event sums past the base trigger: min(500k absolute, 0.70×window) tokens — 500k on the current 1M frontier windows (T-1497 part B regrounding; the absolute term is the context-rot degradation onset per the operator msg 2360, the cap keeps a small-window agent's restart below its harness force-compaction floor). A confirmed-IDLE session (per-poll velocity < 31k tokens) may instead ride to the 0.90×window idle ceiling (T-1491).

The 30-min cooldown prevents re-fire loops while the new session warms.

### Disable (per agent or all)

```bash
# As claude-fallback (Recovery):
# Disable a single agent's monitor:
systemctl --user stop pantheon-context-fill-monitor@orchestrator.timer
systemctl --user disable pantheon-context-fill-monitor@orchestrator.timer

# Disable all instances (every agent under coverage):
for t in $(systemctl --user list-units 'pantheon-context-fill-monitor@*.timer' --no-legend | awk '{print $1}'); do
  systemctl --user disable --now "$t"
done
```

### Tune

Threshold + cooldown + context-window are env-overridable globally (apply to every agent). Per-agent jsonl-directory override via `CONTEXT_FILL_PROJECTS_DIR_<AGENT>`. Edit `~/.config/systemd/user/pantheon-context-fill-monitor@.service.d/override.conf` (create if absent — the `@` filename means it applies to every instance):

```ini
[Service]
# T-1497 trigger terms (min(absolute, cap×window)) — the normal tuning knobs:
Environment=CONTEXT_FILL_RESTART_ABSOLUTE_TOKENS=500000
Environment=CONTEXT_FILL_SAFETY_CAP_FRACTION=0.70
Environment=CONTEXT_FILL_IDLE_VELOCITY_TOKENS=31000
Environment=CONTEXT_FILL_COOLDOWN_SECONDS=900
Environment=PANTHEON_CONTEXT_WINDOW=200000
# LEGACY: CONTEXT_FILL_THRESHOLD is an explicit operator FRACTION that
# BYPASSES the min() regrounding entirely — do NOT set it as a routine
# tuning knob; it exists as the operator's msg-2360 escape hatch.
# Per-agent jsonl-dir override (optional):
Environment=CONTEXT_FILL_PROJECTS_DIR_ORCHESTRATOR=/some/test/path
```

For per-instance override (only one agent affected), use `pantheon-context-fill-monitor@orchestrator.service.d/override.conf` instead.

Then `systemctl --user daemon-reload && systemctl --user restart pantheon-context-fill-monitor@orchestrator.timer pantheon-context-fill-monitor@specialist.timer`.

### Inspect

```bash
# As claude-fallback:
systemctl --user list-timers 'pantheon-context-fill-monitor*'
journalctl --user -u pantheon-context-fill-monitor@orchestrator.service -n 50 --no-pager
journalctl --user -u pantheon-context-fill-monitor@specialist.service -n 50 --no-pager

# Per-agent last-restart record:
cat ~/.local/state/pantheon/context_fill_orchestrator_last_restart.json | python3 -m json.tool
cat ~/.local/state/pantheon/context_fill_specialist_last_restart.json | python3 -m json.tool
```

### Verify a fire ran cleanly

After a restart-on-fill event for an agent:

1. **Monitor logged the trigger:** journal for `pantheon-context-fill-monitor@<agent>.service` shows `agent=<agent>` with `threshold crossed (NN.NN% >= 60.00%)` followed by `restart succeeded`.
2. **Per-agent state file landed:** `context_fill_<agent>_last_restart.json` contains `ts` (epoch) + `fill_pct` + `jsonl_path` + `agent`.
3. **Agent restarted:** the steady-state sentinel from `handoff_and_restart.sh` matched (Claude Code prompt frame AND idle-state marker `? for shortcuts` — both required per T-0172). Compare against the wrapper's own logs for the 8-step pipeline (now includes post-step-7 kick that injects the HANDOFF read prompt + Telegram-status directive into the fresh pane).
4. **Ledger entry written:** the monitor records to its journal log only; the ledger entry is the agent's own responsibility post-resume (per D10 same-commit). If you find a state-file fire with no corresponding ledger entry, that's a followup for whichever instance came up.

### Restart drain timer — install + enable (per ORG-020 Phase 1)

The drain timer runs on each agent's own account and handles tombstones written by the context-fill monitor (Recovery case) or `restart_request.py` (operator path). It is feature-flagged — disabled by default; enable explicitly on each account when ready for supervised-first-fire.

**Install (run as the target agent's account user):**

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-restart-drain.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-restart-drain.timer ~/.config/systemd/user/
systemctl --user daemon-reload

# Verify installed + inactive (do NOT start yet — feature flag must be set first):
systemctl --user status pantheon-restart-drain.service --no-pager   # expect: loaded; inactive (dead)
systemctl --user is-enabled pantheon-restart-drain.timer            # expect: disabled
```

**Enable (after supervised-first-fire is ready):**

```bash
# Create the feature-flag drop-in:
mkdir -p ~/.config/systemd/user/pantheon-restart-drain.service.d
cat > ~/.config/systemd/user/pantheon-restart-drain.service.d/enabled.conf <<'EOF'
[Service]
Environment=PANTHEON_RESTART_DRAIN_ENABLED=1
EOF
systemctl --user daemon-reload
systemctl --user enable --now pantheon-restart-drain.timer
loginctl enable-linger <account>  # ensure timer runs when user is not logged in
```

Replace `<account>` with `claude-fallback` (Recovery), `claude` (Orchestrator), or `claude-research` (Specialist).

**Disable:**

```bash
systemctl --user stop pantheon-restart-drain.timer
systemctl --user disable pantheon-restart-drain.timer
# Or just remove the drop-in (drain will bail at feature-flag check):
rm ~/.config/systemd/user/pantheon-restart-drain.service.d/enabled.conf
systemctl --user daemon-reload
```

**Inspect:**

```bash
systemctl --user list-timers pantheon-restart-drain*
journalctl --user -u pantheon-restart-drain.service -n 50 --no-pager
# Check for any pending tombstones:
ls ~/.local/state/pantheon/restart_requested_*.json 2>/dev/null
```

**Operator restart via CLI (no Telegram needed):**

```bash
# Write an operator tombstone for Recovery (drain picks it up within ~30s):
python3 ~/pantheon-config/tools/restart_request.py --agent recovery

# Force if HANDOFF.md is absent or stale:
python3 ~/pantheon-config/tools/restart_request.py --agent recovery --force
```

**Manual fallback (if drain is disabled):**

```bash
# XDG_RUNTIME_DIR is REQUIRED for systemctl --user in non-interactive shells:
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart claude-telegram-fallback.service
```

### Caveats

- **Cross-account dependency:** the monitor runs on `claude-fallback` (Recovery) but reads `claude`'s + `claude-research`'s JSONLs and invokes `handoff_and_restart.sh` against each of their tmux sessions. Recovery's NOPASSWD-sudo into both accounts is load-bearing. If `/etc/sudoers.d/repair-sudo` is rolled back, the affected agent's monitor silently bails (subprocess returncode 1).
- **Per-agent isolation:** state files + cooldowns are per-agent; one agent's fire doesn't affect another's eligibility. A bug in one agent's config doesn't leak into another's monitoring.
- **Sentinel false-positive (resolved 2026-05-10 via T-0172):** the wrapper now requires both the prompt-frame regex AND the idle-state sentinel (`? for shortcuts` / `ctrl+t to hide tasks`) to match before declaring step 7 success. The dev-channels confirm dialog (which reuses prompt-frame chars) can no longer false-pass. Override via `--idle-sentinel <regex>` if a future Claude Code version changes the idle-marker text.
- **No back-off ladder:** firing fails → monitor logs the failure and exits 1. No retry, no escalation. Next 5-min poll retries from scratch (since the failed fire didn't write the cooldown-state file). If `handoff_and_restart.sh` is broken for an extended window for one agent, the monitor will keep retrying without surfacing — check journal regularly during incidents.
- **Recovery tombstone path (per ORG-020):** Recovery's context-fill monitor writes a tombstone instead of directly firing `handoff_and_restart.sh`. Recovery's `pantheon-restart-drain.timer` (separate unit, ~30s cadence) drains it. The drain unit is NOT the unit being restarted — no circular-trigger race. Enable the drain on Recovery's account: install the unit + set `PANTHEON_RESTART_DRAIN_ENABLED=1` in a drop-in override conf (see install instructions below).
- **Manual fallback restart for Recovery (if drain disabled):** `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart claude-telegram-fallback.service` — the `XDG_RUNTIME_DIR` prefix is required for `systemctl --user` from non-interactive shells (ssh sessions, cron, etc.). Omitting it produces a D-Bus "Failed to connect to bus" error. `handoff_and_restart.sh` handles this automatically; manual invocations must include it explicitly.

## Related

- `BACKLOG.md` §B8 (Minimum Viable) — the ratified design.
- `knowledge/episodic/2026-05-03-claude-code-limit-handling/README.md` — source research on Claude Code's limit-signal shape and community resume patterns.
- `tools/hooks/ho_stop_failure.py` + `tools/test_ho_stop_failure.py` — T-0044.
- `tools/ho_resumer.py` + `tools/test_ho_resumer.py` — T-0045.
- `tools/systemd/pantheon-ho-resumer.{service,timer}` — the cap-hit timer.
- `tools/context_fill_monitor.py` + `tools/test_context_fill_monitor.py` — T-0184 (T-0178 sub-A) Orchestrator-only; generalized per-agent in T-0189 (T-0179 sub-A); Recovery tombstone-write added in T-0309.
- `tools/systemd/pantheon-context-fill-monitor@.{service,timer}` — the context-fill template (instances per agent, including `@recovery`).
- `tools/restart_drain.py` + `tools/test_restart_drain.py` — tombstone drain script (T-0309 / ORG-020 Phase 1).
- `tools/restart_request.py` + `tools/test_restart_request.py` — operator-facing restart CLI (T-0309 / ORG-020 Phase 2).
- `tools/systemd/pantheon-restart-drain.{service,timer}` — drain timer template; install on each agent's account.
- `tools/systemd/README.md` §HO auto-resumer — install + verify.
- `rulebook/policies/trust-posture.md` — autonomy framing this runs under.
- `tickets/done/T-0067.md` — graceful wind-down + checkpointing layer; revisit if `--resume` alone surfaces state-loss issues over real cycles. (Renumbered from T-0042 → T-0066 → T-0067 on 2026-05-04 across two CLI-race-condition duplicate-id collisions. The old `blocked/` path is retired — ORG-019; the ticket now lives in `done/`.)
- `tickets/inbox/T-0043.md` — Specialist cap-awareness (separate scope).
