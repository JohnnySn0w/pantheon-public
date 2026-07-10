# Agent monthly-spend-limit wedge

## Failure shape

An agent goes silent while its **process and inbound poller stay up** — it looks
idle, but its tmux pane shows a blocking modal:

```
You've hit your monthly spend limit · raise it at claude.ai/settings/usage
```

This is the Claude Code **`/rate-limit-options` modal** that fires when the account
crosses its **monthly spend limit**. It is a TUI dialog ("Wait for limit to reset"
/ raise it), and an unattended agent **cannot self-dismiss it**, so the session
wedges. First seen 2026-06-26 when the fleet's monthly limit was set to **$10** —
far too low for throughput — and Orchestrator + Specialist both wedged on it.

Why nothing caught it before: unlike the auth-outage 401s, the spend-limit modal is
**not an API error in the session JSONL** — it is pane-only. Every JSONL-based
detector (api-health 400, model-outage 404, auth-outage 401) is structurally blind
to it. Detection requires a **pane scrape**.

## Detection (live)

`context_fill_monitor._maybe_fire_spend_limit` (2026-06-26) — runs per-agent on the
5-min cycle, captures the pane (cross-account via sudo) and matches the
`monthly spend limit` sentinel. On a hit it sends a **direct-to-operator** Telegram
alert (raising the limit is his account action — same operator-must-act class as the
auth-outage / credential carve-out), with a 6h per-agent cooldown. Ships ON; disable
via `PANTHEON_SPEND_LIMIT_ENABLED=0`.

## Remediation

Ranked. The limit raise is the operator's (his billing/account); Recovery does the legwork.

1. **Confirm it's the spend-limit modal.** Pane shows the `monthly spend limit`
   sentinel (or `verify_startup.sh --agent <name>` reports a non-idle pane that is
   not a boot dialog). `df` is irrelevant here — this is billing, not disk.
2. **Raise the limit (the operator).** claude.ai → Settings → Usage → raise the monthly
   spend limit (or wait for the monthly reset). No agent can self-remediate this.
3. **Un-wedge the agent (Recovery, after the raise).** The modal does not auto-clear when
   the limit is raised. Dismiss it + poke the session to resume, as the agent's user:
   ```bash
   sudo -n -u <agent-unix-user> bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) \
     tmux send-keys -t <pane> Escape; \
     XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t <pane> -l \
       "Spend limit raised — you are unblocked. Resume your in-flight task or next queued ticket."; \
     XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t <pane> Enter'
   ```
   (Panes: orchestrator `claude-telegram-bot:0`, specialist `claude-research-bot:0`, recovery
   `claude-telegram-bot-fallback:0`.) Then confirm the pane returns to a working
   prompt per `confirm-health-after-touching-live-sessions`.
4. **Prevent recurrence:** keep the monthly limit well above fleet throughput (the
   $10 default was the root trigger). The detector now surfaces a wedge within ~5
   min instead of it hiding as "idle."

## Autonomous-vs-bubble-up D3 placement

- **Above the bar (the operator's):** raising the monthly spend limit (his billing/account).
- **Below the bar (Recovery executes):** diagnosis; the pane-dismiss + poke; building +
  running the detector. An explicit "unblock <agent>" authorizes the un-wedge poke.

## Cross-references

- `tools/context_fill_monitor.py` (`_maybe_fire_spend_limit`, `_capture_pane_text`)
- `sop/remediation/agent-oauth-relogin.md` (sibling operator-must-act class)
- `confirm-health-after-touching-live-sessions` (memory)
