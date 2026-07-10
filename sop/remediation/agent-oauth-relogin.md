# Agent OAuth credential expiry / 401 re-login

## Failure shape

An agent's Claude Code session goes comms-dark while its **process and inbound
Telegram poller stay up** — so process-liveness and channel-liveness both read
GREEN, but the agent silently does nothing because every model call returns
`401 Invalid authentication credentials`. The pane shows `● Please run /login ·
API Error: 401 Invalid authentication credentials`.

Root cause: the account's `~/.claude/.credentials.json` cannot auto-renew its
access token. Two variants:
- **NO_REFRESH** — `refreshToken` is empty/missing, so when the (~8h) access
  token expires there is nothing to renew with → hard 401. (The 2026-06-20→22
  outage: Specialist had an empty refresh token; all three agents were independent
  logins and went dark; it hid ~3 days because nothing checked auth-validity.)
- **REVOKED/STALE** — refresh token present but rejected server-side (rare).

Each agent is an **independent OAuth login** (Orchestrator/Specialist/Recovery each have their own
distinct refresh token, all `sub: max`). Credentials are therefore **not portable
between accounts** — do NOT copy one agent's `.credentials.json` to another, and
note this is a secrets/account-access operation (above the autonomy bar). The fix
is a per-agent re-login, which needs the operator's browser.

## Precedent cases

- **2026-06-20→22 fleet 401 outage** — Specialist `NO_REFRESH`; Recovery + Orchestrator also went
  dark and were re-logged by the operator via `/login`. Recovery drove Specialist's re-login
  through its pane (below) and shipped `tools/credential_expiry_monitor.py` +
  the auth-dead detector so the next occurrence pages the operator pre-emptively.

## Recommended remediation

Ranked. Recovery does the legwork; the browser auth step is the operator's (his Anthropic
credential — genuinely above the bar to do unilaterally).

1. **Confirm it's auth, not a wedge.** Pane shows the `/login` / 401 sentinel, AND
   `tools/credential_expiry_monitor.py --agent <name> --dry-run` reports
   `NO_REFRESH`/`MALFORMED` (or the cred is expired with no refresh). If instead
   the pane is stuck on a TUI dialog, that's a wedge → use the restart runbook.
2. **Drive `/login` on the agent's pane** (as the agent's unix user, via Recovery's
   sudo + `XDG_RUNTIME_DIR`):
   - Dismiss any survey overlay (`How is Claude doing this session?`) → send `0`.
   - `tmux send-keys -t <pane> -l "/login"` then `Enter`.
   - Menu → select **1. Claude account with subscription** (`Enter`).
   - The pane prints an `https://claude.com/cai/oauth/authorize?...` URL and a
     `Paste code here if prompted >` input. Capture the full (line-wrapped) URL.
3. **Hand the URL to the operator** via Telegram. He opens it, authorizes with the Max
   account, and sends back the code. **Paste the code** into the waiting prompt:
   `tmux send-keys -t <pane> -l "<code>"` then `Enter`.
   - **STATE MUST MATCH (2026-06-22 lesson — cost a round-trip).** The returned
     code is `CODE#STATE`. The trailing `STATE` must equal the `state=` of the URL
     **currently live in the pane**, not just any URL you sent earlier. If the operator
     authorizes via a stale link (e.g. you re-triggered `/login` and the pane's
     state changed), the paste fails with `OAuth error: Request failed with status
     code 400`. Recovery: `Enter` to retry → re-select method 1 → capture the FRESH
     URL → send the operator *that exact* URL and tell him earlier links are dead. Before
     pasting, you can sanity-check the code's `#STATE` tail against the pane's
     `state=` to catch a mismatch without burning the attempt.
4. **Verify**: the pane clears the 401 and returns to a normal idle prompt; the
   credential file refreshes (`credential_expiry_monitor --agent <name>` → OK with
   a non-empty refresh token); the agent answers a Telegram ping. Confirm health
   per `confirm-health-after-touching-live-sessions`.

Pane invocation template (Specialist shown):
```bash
sudo -n -u claude-research bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) \
  tmux send-keys -t claude-research-bot:0 -l "/login"; \
  XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t claude-research-bot:0 Enter'
```

## Prevention (live)

- `tools/credential_expiry_monitor.py` (hourly `pantheon-credential-expiry.timer`,
  runs on Recovery): pure file-read of every agent's credential — no LLM/API, so it
  works during a full auth outage — alerts the operator **directly** via the Telegram
  bot token on `NO_REFRESH`/`MALFORMED`. This catches the time-bomb (empty refresh
  token) BEFORE the access token expires, instead of days after.
- Auth-outage detector (`context_fill_monitor._maybe_fire_auth_outage`, T-1223,
  ships ON): the per-agent 5-min monitor flags a SUSTAINED recent run of 401
  "Invalid authentication credentials" in the session JSONL and ALERTS the operator
  directly (never restarts — a restart re-inherits the dead credential). This
  covers the runtime case incl. a present-but-revoked refresh token that the
  file-read cannot see; recency + leading-run-breaker guards mean a recovered
  session never false-fires.

## Autonomous-vs-bubble-up D3 placement

- **Above the bar (the operator's):** the browser OAuth authorization (his account
  credential); copying credentials between accounts (secrets/access — never do it).
- **Below the bar (Recovery executes):** diagnosis; driving `/login` to the code-paste
  prompt; relaying the URL/code; pasting the code; post-fix verification; building
  + running the monitors. An explicit "remediate <agent>" from the operator authorizes
  the whole sequence including the gated browser hand-off.

## Cross-references

- `tools/credential_expiry_monitor.py`, `tools/channel_liveness_monitor.py`
- `sop/remediation/recovery-service-recovery.md`,
  `sop/remediation/telegram-plugin-mcp-drop-recovery.md`
- `confirm-health-after-touching-live-sessions` (memory)
