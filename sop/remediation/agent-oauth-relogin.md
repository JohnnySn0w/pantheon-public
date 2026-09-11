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

## Venue selection — WHERE to run /login (read before touching anything)

The login MUST run in the agent's LIVE pane. **NEVER boot a scratch/second
`claude` on the account to host the login modal** — `enabledPlugins` loads the
telegram plugin on EVERY boot on that account (`--channels` absence does not
protect), and the scratch steals the bot poller and kills the live bridge.
Proven the hard way 2026-07-20: Recovery scratch-booted for exactly this reason and
took down his own bridge mid-arc (fifth incident on memory
`feedback_no_second_claude_with_telegram_plugin`).

Per-agent venue:
- **Another agent's pane** (specialist, orchestrator): Recovery drives it via sudo + send-keys —
  the standard flow below.
- **Your OWN account**: you cannot self-drive (the modal wedges your session with
  nobody at the keyboard). A PEER drives your live pane per this SOP and runs the
  the operator URL/code hop on the peer's own bridge; your session simply holds in the
  modal until the peer pastes the code. Recovery↔orchestrator ran this both directions
  2026-07-20 — it works cleanly.
- **mnemosyne (exocortex)**: operator-direct (tmux-attach and run /login by hand)
  or a peer pane-drive. Do NOT route the interactive flow through the
  telegram-session → mailbox → ACP-session relay: a single runbook turn took
  ~10 min on 2026-07-20 (vs the ~2-3 min the interactive hop needs), the operator
  ended up doing it manually, and the stale runbook turn + queued stand-down
  raced afterward. That relay is fine for async work, wrong for interactive.

## Recommended remediation

Ranked. Recovery does the legwork; the browser auth step is the operator's (his Anthropic
credential — genuinely above the bar to do unilaterally).

1. **Confirm it's auth, not a wedge.** Pane shows the `/login` / 401 sentinel, AND
   `tools/credential_expiry_monitor.py --agent <name> --dry-run` reports
   `NO_REFRESH`/`MALFORMED` (or the cred is expired with no refresh). If instead
   the pane is stuck on a TUI dialog, that's a wedge → use the restart runbook.
2. **Drive `/login` on the agent's pane** (as the agent's unix user, via Recovery's
   sudo + `XDG_RUNTIME_DIR`):
   - **Suppress the wedge auto-restart FIRST (T-2965).** The pane holds the `/login`
     modal for the whole URL/code hop — non-idle with frozen fill, i.e. the wedge
     signature — so the context-fill wedge detector will otherwise dispatch a restart
     INTO the login (it send-keys into the prompt and respawns onto the wiped
     credential; nearly bricked specialist's re-login 2026-08-17). On **Recovery's** account
     (where the per-agent monitors run):
     `python3 ~/pantheon-config/tools/context_fill_monitor.py --agent <name> --suppress-wedge`
     (45-min TTL default; auto-expires, so a forgotten clear can never blind the
     detector). A pane-content sentinel is an automatic backstop, but set the explicit
     flag too — it does not depend on matching the login screen's exact text.
   - Dismiss any survey overlay (`How is Claude doing this session?`) → send `0`.
   - `tmux send-keys -t <pane> -l "/login"` then `Enter`.
   - Menu → select **1. Claude account with subscription** (`Enter`).
   - The pane prints an `https://claude.com/cai/oauth/authorize?...` URL and a
     `Paste code here if prompted >` input. Capture the full (line-wrapped) URL.
   - The URL hard-wraps across pane lines: capture with `-p`/`-J` and JOIN the
     lines from the `https://` line to the blank line — a bare grep gets only the
     first ~80 chars and hands the operator a dead link.
3. **Hand the URL to the operator** via Telegram. He opens it, authorizes with the Max
   account, and sends back the code. **Paste the code** into the waiting prompt:
   `tmux send-keys -t <pane> -l "<code>"` then `Enter`.
   - **Paste-registration gotcha (2026-07-20, specialist):** the first `send-keys -l`
     into the paste prompt sometimes silently doesn't register. After typing,
     capture-pane and confirm the masked `*****` input is NON-EMPTY **before**
     sending Enter; if empty, type it again. An Enter on an empty prompt looks
     like an ignored/failed code when nothing was ever submitted.
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
   - **Clear the wedge-suppress flag** once the pane is idle + healthy (restores wedge
     protection immediately; it also auto-expires if you forget):
     `python3 ~/pantheon-config/tools/context_fill_monitor.py --agent <name> --clear-suppress`
5. **Restart the agent's acp-bridge IF it has one** (check
   `systemctl --user list-units | grep acp-bridge` as the agent — specialist has one;
   mailbox-first seats like recovery + the sub-orchestrators do NOT, they deliver via
   the consume→tmux runtime). The re-login refreshes the credential FILE, but a
   long-running `claude-code-acp` process (the ACP delivery wrapper) keeps its
   pre-wipe token in memory → every ACP delivery (keep_going nudges + mailbox mail)
   then silently fails `-32000: Authentication required` while the interactive pane
   looks perfectly healthy. This ran UNDETECTED FOR HOURS on specialist (2026-08-18)
   until a mail_staleness alert surfaced it — auth-liveness ≠ process-liveness.
   Restart (as the agent's user, `XDG_RUNTIME_DIR` set):
   `tmux kill-session -t acp-bridge-<agent>` (→ supervise exits → `Restart=always`
   respawns fresh) OR `systemctl --user stop pantheon-acp-bridge@<agent>` + `start`.
   A fresh process re-reads the credential. Do NOT delete the acp-bridge state file
   (that is the context-fill lever only — resuming the same ACP session is fine
   here; the token is per-process). Verify the previously-failing delivery now
   succeeds (`errors:[]`, backlog drains). **`systemctl --user restart` alone was a
   NO-OP** in the specialist case (the bridge survives in its detached tmux session
   across a MainPID restart) — use `kill-session` or `stop`+`start`. Detail:
   `sop/remediation/acp-bridge-recovery.md` (AUTH failure class).

Pane invocation template (Specialist shown):
```bash
sudo -n -u claude-research bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) \
  tmux send-keys -t claude-research-bot:0 -l "/login"; \
  XDG_RUNTIME_DIR=/run/user/$(id -u) tmux send-keys -t claude-research-bot:0 Enter'
```

## The ~30-day refresh cliff — pre-empt, don't react

Refresh tokens live a FIXED ~30 days from login (verified byte-stable,
2026-07-20 incident `incidents/2026-07-20-orchestrator-oauth-refresh-rejection/`). A
mass relogin therefore creates a SYNCHRONIZED fleet-wide cliff ~30d later — the
June-22 wave produced a single 07-22 18:12Z expiry for three agents at once.
When the credential monitor shows expiries clustering, run a pre-emptive relogin
arc a day or two early (T-1878 pattern: ~10 min of the operator's time, one URL/code
hop per agent, zero outages) instead of waiting for the 401s. Note the fresh
logins re-synchronize — tonight's arc set the NEXT cluster around 08-16→08-19;
staggering logins across different days would break the cluster if that ever
matters.

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
