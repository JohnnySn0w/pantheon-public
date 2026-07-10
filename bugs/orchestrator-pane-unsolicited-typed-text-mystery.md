---
symptom: "Orchestrator's tmux pane (claude-telegram-bot:0) had literal text 'check on the b42 cycle status' typed into the input buffer, prefixed by the prompt arrow, but Enter was never sent — Claude Code's input handler never received the text (no corresponding turn in any jsonl). The operator did NOT type it. Recovery unblocked by manually pressing Enter at 19:36:17Z."
fix: "Unknown — not reproduced. Filed forensic capture for next occurrence."
domain: pantheon, kino-pwa, tmux, send-keys, mystery
---

## Incident

2026-05-12T19:26Z: the operator reported Orchestrator seemed paused (msg_1161). Recovery inspected:

- Last Orchestrator jsonl activity: 2026-05-12T17:59:01.178Z (assistant: "Routine orchestrator idle — nothing to do this turn.").
- Pane state at 19:26Z showed `❯ check on the b42 cycle status` in the input field — text typed but Enter never submitted.
- `grep 'check on the b42 cycle status' /home/claude/.claude/projects/-home-claude/*.jsonl` returned 0 matches → text never reached Claude Code's input handler.
- The operator confirmed (msg_1163) he did NOT type that.

## Forensics

### Known send-keys consumers ruled OUT

1. **activity_monitor.send_poke**: journal sudo entries show P1.a fired at 17:00:47Z (text) + 17:00:48Z (Enter), and again at 19:30:47Z + 19:30:48Z. The prompt text is `[activity-monitor P1.a] You appear idle...` — completely different from `check on the b42 cycle status`. Both Enter steps succeeded.
2. **handoff_and_restart.sh**: only fires on graceful restart; no restart occurred in window.
3. **kino-pwa chat → agent**: kino-pwa chat uses a SOCKET protocol (`/run/kino/agents/<name>.sock`) → JSON message dispatched to kino-agent@<name>.service subprocess. Does NOT use `tmux send-keys` against the claude-telegram-bot pane. Verified at `pantheon-pwa/src/agents/manager.ts:185` — stdin write to subprocess.
4. **Telegram bridge**: bridge state `last_inbound_at.orchestrator = "2026-05-12T17:34:31.626735+00:00"`. If a Telegram message had been routed to the pane at 19:00+, last_inbound_at would have updated. It didn't. So no Telegram inbound during the window. (Bridge log + journal in pantheon-bridge.service window 19:00-19:30 are empty.)

### Not in journal

`journalctl --since "4 hours ago" | grep "claude-telegram-bot"` shows ONLY the activity_monitor pokes (via sudo from claude-fallback). No other sudo'd send-keys to that pane.

If the source was running directly as `claude` (no sudo), it wouldn't appear in the sudo journal stream. Bridge runs as `claude`. But bridge would update `last_inbound_at` on tmux dispatch (no bridge inbound in the window).

### Mystery

No code path is known that would type text into the claude-telegram-bot tmux pane WITHOUT firing Enter. The available paths either:
- send text + Enter atomically (activity_monitor, handoff_and_restart, bridge inbound)
- don't touch the tmux pane at all (kino-pwa chat, internal Claude Code worker dispatches)

## Next time it happens

Capture:
1. Full `tmux capture-pane -p -t claude-telegram-bot:0` immediately.
2. Bridge state file + journal in the 10-min window prior.
3. activity_monitor state file + journal.
4. `ps -ef --forest` showing the full claude-account process tree.
5. `lsof -Uu claude | grep sock` — any unexpected socket connections.

Then check whether the typed text matches:
- An activity_monitor prompt (any predicate variant)
- A bridge-relayed message that's somehow gotten the Enter dropped
- A subagent / orchestrator dispatch echo

## Operational mitigation

Recovery's activity_monitor P1.a (post-T-0283 fix) now correctly catches Orchestrator-idle (Orchestrator-tagged signals only). If the text leaves Orchestrator blocked-but-input-typed again, P1.a fires within ~15min + sends its own prompt. The mystery text gets pushed off the bottom of the prompt buffer when the new prompt lands; whichever fires after the new prompt's Enter wins. This is a defensive backstop, not a fix.

## Related

- T-0284 — investigation ticket. Blocked pending recurrence.
- T-0283 — activity_monitor P1.a signal fix (separately landed 73b443c) — adjacent but distinct bug.
- msg_1161 / msg_1163 / msg_1164 / msg_1165 — the Telegram thread surfacing this.

## Update — 2026-05-13/14/15 cluster

Multiple recurrences observed during 2026-05-13 → 2026-05-15 work cycle. Each instance: text appeared in the input bar of a claude-telegram-bot or claude-research-bot pane WITHOUT Enter being pressed; Recovery submitted Enter manually to clear. Pattern is now clearly cross-agent (affects both claude (Orchestrator) and claude-research (Specialist) panes), and the offending text matches what looks like cross-agent dispatch messages OR handoff_and_restart.sh kick-prompts.

### Recurrence timeline

| Date/Time (approx) | Pane | Text | Notes |
|---|---|---|---|
| 2026-05-13 03:48Z | claude-research-bot:0 (Specialist) | `go with (b)` | Original Specialist instance per HANDOFF |
| 2026-05-14 04:48Z | claude-research-bot:0 (Specialist) | `go with (a)+(c)` | the operator's T-0110 directive |
| 2026-05-14 15:53Z | claude-telegram-bot:0 (Orchestrator) | `ratify T-0330` | HO's own self-directed action |
| 2026-05-14 (during T-0179) | claude-research-bot:0 (Specialist) | `Read /home/claude-research/HANDOFF.md, then post...` | handoff_and_restart.sh kick-prompt; KICK STEP'S Enter was sent but not honored in pane state |
| 2026-05-14 (during T-0110 thread) | claude-research-bot:0 (Specialist) | `run T-0320 now` | Cross-agent dispatch |
| 2026-05-14 (during T-0110 thread) | claude-research-bot:0 (Specialist) | `read PredGen and DDTSR` | Cross-agent dispatch |

Total ≥ 6 recurrences in 48h. Pattern previously thought Orchestrator-specific is now confirmed cross-agent (Orchestrator + Specialist both affected).

### What the recurrences add to the forensic picture

1. **Cross-agent confirmation** — affects both claude (Orchestrator) and claude-research (Specialist) panes. So the offending code path either runs in MULTIPLE places (per-agent) OR runs in a single place that targets multiple panes.
2. **handoff_and_restart.sh kick step IS implicated** — the T-0179 first-fire on Specialist had the kick-prompt land in the pane without auto-submit. This is the FIRST time we've directly observed the canonical send-keys-then-sleep-then-Enter sequence (per `tools/handoff_and_restart.sh:KICK_PROMPT`) failing to deliver Enter. Suggests the bug may be a tmux send-keys race / paste-mode interaction at high latency, NOT a fully-mystery code path.
3. **Cross-agent dispatch matches HO speaking to Specialist** — `go with (b)`, `go with (a)+(c)`, `run T-0320 now`, `read PredGen and DDTSR` are all the SHAPE of HO sending directives to Specialist. Suggests HO has a code path that does cross-agent send-keys (maybe via Agent tool / cross-agent dispatch / TeamCreate). That path needs the same `text + sleep + Enter` discipline that inject() in inbox_consumer.py uses, and may not have it.

### Refined hypothesis

The bug is likely: a cross-agent dispatch path (used by HO when it wants to nudge Specialist or another agent) sends text via `tmux send-keys -t <other-agent-pane> <text>` but EITHER (a) doesn't follow with Enter, OR (b) follows with Enter too quickly and tmux paste-mode swallows it (the same bug T-0008's inject() fix was originally designed to defend against). The cross-agent dispatch may also be affected by `inject_pending_paste` symptom that inject() detects + bails on, but there's no equivalent safeguard outside inject().

### Next steps when investigating

- Grep tools/ + claude-config worker personas for any `tmux send-keys` invocations targeting `-t claude-telegram-bot:0` or `-t claude-research-bot:0` from contexts OTHER than activity_monitor / handoff_and_restart.
- Check whether HO has an Agent / Skill / SlashCommand path that does cross-agent send-keys.
- For handoff_and_restart.sh's kick step specifically (now confirmed implicated): consider porting inject()'s pending-paste verification to the kick path (currently kick only fires send-keys without verification).

---

## Recurrence cluster 2026-05-19 02:18-02:35Z — Recovery pane (scope expansion)

**NEW data point, scope-expanding**: the mystery inputs are NOT Orchestrator-pane-exclusive. Recovery (`claude-telegram-bot-fallback:0`) received 5 successive bare `1` inputs between 02:18Z and 02:32Z. Each appeared as a user message in Recovery's conversation. The operator (Telegram msg_1588): "I'm not doing that."

Bridge diagnostic at 02:34Z:
- Bridge service `active`, polling Telegram normally
- Inputs arrive WITHOUT channel tags (not from Telegram path)
- No tmux client attached to the session (`tmux list-clients` empty)
- Only 2 tmux sessions on system (Recovery's + Specialist's)
- No active `tmux send-keys` process visible in `ps`
- inbox_consumer correctly skips (sees `mid_turn`)
- activity_monitor only targets HO's pane (claude-telegram-bot:0), not Recovery
- blocked_reassessment.send_to_recovery_pane sends summary messages, not bare `1`
- RECOVERY_AUTO_RESOLVE_ALLOWLIST sends `1+Enter` but to ORCHESTRATOR pane, not Recovery

So same class of mystery: text appears in pane from unknown source despite no audit trail. The bug class is now confirmed cross-pane (not Orchestrator-specific).

**Implications for the hypothesis**: the "cross-agent dispatch sends text via tmux send-keys without proper Enter" framing still fits, but now needs to account for cross-CROSS-agent paths — i.e., whatever's sending to Orchestrator pane also sends to Recovery pane occasionally. Or there's a shared substrate (e.g., the harness's `Bash send-keys` operation behaving inconsistently).

**Action**: T-0284 cron-check fires on this file mtime update — should auto-unblock T-0284 on next blocked_reassessment fire (30min cadence).


---

## 2026-05-19 correction: Recovery-pane "1" cluster was NOT a T-0284 recurrence

**Re-classified**. The 5x bare `1` inputs on Recovery's pane between 02:18-02:32Z 2026-05-19 (claimed above as scope-expanding cross-pane recurrence) were actually a separate bug:

- Recovery's `inbox_consumer._apply_auto_resolve_action` ran `tmux send-keys -t claude-telegram-bot:0 "1"` WITHOUT sudo
- tmux prefix-matched the target (`claude-telegram-bot:0`) against recovery's own server, hitting `claude-telegram-bot-fallback:0` (the only session starting with `claude-telegram-bot` on recovery's server)
- "1" + (Enter) went into Recovery's pane every ~10min instead of dismissing Orchestrator's dev-channels prompt
- Orchestrator's prompt persisted → sentinel re-fired → loop ran ~5h
- Each fire ALSO pinged the operator's Telegram with `[recovery auto-resolve]` info-alert (T-0142 ratified design — alerts fire on perceived success)

Fixed in commit `aa78e42` (Recovery 2026-05-19 ~02:55Z): sudo wrapper + 0.7s paste-mode delay.

**Implication for THIS bug (T-0284)**: scope is NOT confirmed expanded to cross-pane. The Orchestrator-pane recurrences remain the genuine mystery class; the Recovery-pane apparent recurrence was self-inflicted by Recovery's own broken auto-resolve. Orchestrator-pane recurrences listed earlier in this file are still unexplained. T-0284 stays open for the actual Orchestrator-pane mystery; Recovery-pane false-positive retracted.

