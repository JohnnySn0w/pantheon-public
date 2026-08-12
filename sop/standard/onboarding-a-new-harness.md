---
title: Onboarding a new harness as a Pantheon daemon
status: Drafted 2026-06-09 by T-1031 (implementer, dispatched by Orchestrator / HO). Authored per ORG-031 §iii. Adopted 2026-06-16 by Orchestrator (HO) — autonomous per msg-3733 (principles-as-ratification; below-goal-level methodology SOP implementing the already-operator-ratified ORG-031 §iii sub-ticket 1). The operator override-available. The onboarding ACTION (standing up a specific new instance) remains a goal-level call; this SOP is the how, not a decision to onboard.
---

# Onboarding a new harness as a Pantheon daemon

**Rationale:** `sop/standard/adding-restart-capability-to-a-new-agent.md` is the deep-dive install runbook for giving a *claude-code agent* the full ORG-020 restart stack. This SOP is the **harness-agnostic complement**: it covers bringing up *any* supported harness (codex, and future harnesses) as a Pantheon daemon and wiring it into the org's loops. The two SOPs are intended to be used together — this one for the cross-harness onboarding steps, the claude-specific runbook for the claude-specific recovery depth.

**Key insight from ORG-031:** "Onboarding a new harness = implement the 7-surface adapter + 2 bridges + run the wedge-bypass script; the loops attach unchanged."

---

## When to use this

- Bringing up a **new harness** (non-claude CLI tool — codex, or future harnesses) as a persistent Pantheon daemon.
- Adding a 4th+ agent that runs a different underlying harness than Claude Code.
- Generalizing the reference configuration to a new CLI agent tool.

Do NOT use for:
- Adding a new **Claude Code agent** — use `adding-restart-capability-to-a-new-agent.md` directly.
- Migrating an **existing claude persona** off its bespoke telegram-bot supervisor onto the control plane (there is something to migrate FROM, not a fresh bringup) — use `migrating-an-existing-persona-onto-the-control-plane.md` (generic tool `tools/persona_cutover.sh`).
- One-off manual invocations or headless `exec` calls — those are not daemons.
- Recovering a wedged daemon — see `handoff-restart-runbook.md` + `../remediation/recovery-service-recovery.md`.

---

## Who runs this — the human / agent boundary

A capable agent (with sudo on the box) executes most steps. A small set **requires** a human:

| Step | Why it requires a human |
|---|---|
| Device-code / OAuth login (`codex login --device-auth`) | Browser flow; no headless path without an API key |
| Deploy-key upload | Generating the key is scripted; uploading the public key to the repo host is human-gated |
| Persona registration code edit | Modifies `tools/agent_role_map.py` + `instance.yaml`; requires commit + push |
| Onboarding-menu / trust-dir dialog | First-launch interactive TUI prompts in the live tmux pane |
| API key or bot token generation | External credential systems |

Everything else — the bwrap fix, config writes, supervise wiring, systemd units, verification — can be executed by an agent with appropriate sudo.

---

## Prerequisites

Before starting, confirm:
- [ ] The harness CLI is installed and `--version` responds (e.g. `codex --version`).
- [ ] A Unix account exists for the new daemon (`useradd -m -s /bin/bash <account>`).
- [ ] Linger is enabled (`loginctl enable-linger <account>`; verify `user@<uid>.service` is active).
- [ ] The new account has a `pantheon-config` clone with its own deploy keys (see `pantheon-bringup.md` §5).
- [ ] The recovery account (`claude-fallback` / Recovery) has the sudo grants to reach this account if cross-account recovery will be needed.

---

## The three-part model

Onboarding has three composable parts. Each is independent — Parts 2 and 3 are harness-independent and run unchanged regardless of harness. Part 1 is the per-harness engineering work.

```
Part 1 — Per-harness adapter binding
  └─ Implement the 7-surface + 2-bridge adapter interface
     (tools/harness_adapter/ — built under T-1032)

Part 2 — Orthogonal Pantheon wiring (harness-independent)
  └─ inbox consumer, activity monitor, restart stack,
     ledger, Telegram (attach unchanged per this SOP)

Part 3 — Wedge-bypass
  └─ tools/wedge_bypass.sh — fix the unattended-bringup blockers
```

---

## Part 1 — Per-harness adapter binding

### The PantheonHarnessAdapter interface (ORG-031 §i)

The adapter is the single code artifact that captures everything harness-specific. It lives at `tools/harness_adapter/` (implementation: T-1032). For each new harness, implement the following seven surfaces plus two bridge surfaces:

```
PantheonHarnessAdapter
  ├── standing_prompt   Map CLAUDE.md ↔ harness standing-prompt file
  │                     (e.g. AGENTS.md for codex; size cap + fallback list)
  ├── mcp_servers       Per-server config translation (transport, auth, per-tool gating)
  ├── hooks             Per-event handler registration (normalized event names)
  ├── subagents         Spawn-by-role with per-role config layer, depth/concurrency bounds
  ├── session           Resume-by-id (mechanism differs per harness)
  ├── exec              Non-interactive prompt → result
  ├── auth              Delegated to harness CLI
  ├── inbound  *bridge* Poll-and-inject via harness turn API (NOT MCP push)
  └── policy   *org*    Approval/sandbox at org-tier (Option b) — NOT per-harness
```

**Three partitions, each with a rule:**

1. **Generalized (the seven):** one abstract call, mechanical per-harness key/filename translation. For codex: `CLAUDE.md` → `AGENTS.md`; `settings.json mcpServers` → `~/.codex/config.toml [mcp_servers.<name>]`; `hooks/` → `hooks.json`; etc.

2. **Bridge surfaces (the two harness-specific):** `inbound` and hook-trust / approval-defaults require a **real per-harness implementation**, not a thin key rename.
   - **inbound:** For non-claude harnesses, poll-and-inject via the harness's own turn API. For codex: `tmux send-keys` (manual kick) or per-inbound `codex exec`. The `claude/channel` experimental MCP capability codex does NOT implement — do NOT attempt to use it for non-claude harnesses.
   - **hook-trust / approval-defaults:** Pre-answer both trust gates at bringup (see Part 3, wedge `--hooks-trust` and `--approval`).

3. **Not abstracted (configure per-harness, do not paper over):** sandbox/approval, profiles, app-server, cloud, doctor. These are first-party harness surfaces with different semantics. The adapter *documents* them per-harness; it does not unify them.

**For codex specifically:**

| Adapter surface | Codex specifics |
|---|---|
| standing_prompt | `AGENTS.md` (dir-scoped); stronger override: `model_instructions_file`. Rooted at `~` — degrades when git not detected; run `codex doctor -C <repo>` if scope is wrong. |
| mcp_servers | `codex mcp add <name> --url <url>` — writes `[mcp_servers.<name>]` into `~/.codex/config.toml`. **`--url` is streamable-HTTP only** — legacy SSE requires a stdio shim. MCP binary must be in a codex-owned path (`/home/<user>/`). |
| hooks | `hooks.json` or inline `[[hooks.<Event>]]` in `config.toml`; 10 events (8 + SubagentStart + Compact pair); same matcher / JSON-stdin / `permissionDecision` / exit-2-blocks mechanics as claude. |
| subagents | `spawn_agent` / `send_input` / `wait_agent`; roles = `[agents.<name>]` with `config_file` per-role layer; bounds = `agents.max_depth` / `max_threads` / `job_max_runtime_seconds`. No `TeamCreate` analog — per-spawn, not per-team. |
| session | `codex resume --last` (subcommand, not `--resume <id>` flag); sessions in `~/.codex/*.sqlite` — no JSONL, no `message.usage`. |
| exec | `codex exec [PROMPT]` — same prompt→result shape. Note: `required=true` per-MCP causes `codex exec` to exit if an MCP fails (vs interactive graceful-degrade). |
| auth | `codex login --with-api-key` (headless) or `--device-auth` (browser, interactive). Creds at `~/.codex/auth.json` (0600). OAuth session unlocks built-in multimodal tools (`image_gen`) that the API-key path may not expose. |
| inbound (bridge) | No Telegram plugin; inbound via custom MCP server or manual `tmux send-keys` kick. Outbound: direct-curl `sendMessage` with token at `~/.codex/pantheon.env`. |
| policy (org) | `approval_policy = "never"` + `sandbox_mode = "workspace-write"` + `network_access = true` (ORG-031 §ii, live-verified). `trust_level` alone does NOT silence prompts — both axes required. |

---

## Part 2 — Orthogonal Pantheon wiring (harness-independent)

These steps wire the new daemon into Pantheon's loops. They are **identical** regardless of harness — the wiring only needs a long-lived tmux+systemd process that accepts inbox-driven prompts, is tmux-driveable for recovery, and can write to ledger/Telegram via MCP or hooks.

### 2.1 Supervise script + systemd unit

Copy the pattern from `tools/systemd/recovery-bot-supervise.sh` (or `tools/systemd/ancestor-bot-supervise.sh` for a non-claude harness reference). The two key variables to adapt:

```bash
CLAUDE_BIN="$HOME/.npm-global/bin/<harness>"   # e.g. codex
CLAUDE_ARGS=""   # codex takes no --channels flag; leave empty unless harness supports it
```

The rest of the supervise pattern is unchanged: `tmux new-session -d`, `while tmux has-session ... do sleep 5; done`.

```ini
# ~/.config/systemd/user/<agent>-bot-supervise.service
[Service]
ExecStart=%h/.local/bin/<agent>-bot-supervise.sh
ExecStop=-/usr/bin/tmux kill-session -t <session>
Restart=always
UMask=0002
```

The `ExecStop=-` dash is important — keeps the journal clean when tmux exits 1 on a normal cycle.

**`UMask=0002` is REQUIRED, not optional.** Every Pantheon daemon writes the shared `pantheon-config` clone (ledger commits, ticket moves) — a `pantheon-shared`-group multi-writer path. The default `0022` umask creates group-readable-but-not-group-*writable* objects, which break the next uid's writes (the W9 checkout-permission failure below is exactly this class). `UMask=0002` makes every object the daemon creates group-writable, preserving the multi-writer invariant. This is the onboarding-time enforcement of rule (b) in [`shared-repo-multi-writer-permissions.md`](shared-repo-multi-writer-permissions.md) (generalized from the `kino` retrofit, T-0281). Apply it to **any** unit this daemon runs that touches a shared path, not just the supervise unit.

### 2.2 Persona registration (REQUIRED — ticket.py hard-rejects unregistered personas)

`ticket.py` validates every `target:` and `requester:` against `_valid_target_values()` — an unregistered persona is hard-rejected: `unknown target 'X'; expected one of [...]`. This is a **commit + push** step; an agent cannot do it unattended.

**Two code locations — both required:**

1. `AGENT_ROLE_MAP` in `tools/agent_role_map.py` — add `"<persona>": "<audit-role-id>"`. Use an identity entry (`"<persona>": "<persona>"`) if the agent has no distinct audit role-id.
2. `instance.yaml` `roles:` stanza — add the role → persona binding.

After editing + pushing, confirm:
```bash
python3 -c "from tools.ticket import _valid_target_values; print('<persona>' in _valid_target_values())"
# → True
```

Run `python3 -m pytest tools/test_ticket.py tools/test_ticket_lint.py -q` and confirm green.

### 2.3 Inbox consumer (pull-by-kick for non-claude harnesses)

**Do NOT arm `pantheon-inbox-consumer@<agent>.timer` for non-claude harnesses.**

The inbox-consumer's idle sentinels (`? for shortcuts`, `❯`) never match non-claude TUI frames (codex shows `Welcome to Codex`/chat-prompt), so it logs `tmux_unreachable` or wedges each cycle. Auto-delivery is currently claude-coupled.

**Non-claude inbox model: pull-by-kick.** Tickets are picked up by a manual `tmux send-keys` kick. The operator (or HO via Recovery cross-account sudo) sends the ticket content into the pane when delivery is needed. This is the current codex pattern; a per-harness inbox-consumer is follow-up work.

For the `read` and `file` operations (checking the queue, filing new tickets), `ticket.py` works unchanged from any account with repo access.

### 2.4 Activity monitor

`pantheon-activity-monitor@<agent>.timer` runs on Recovery's account and monitors the agent's pane via the shared infrastructure. **It works unchanged** — it monitors tmux pane activity, not harness-specific output. Wire it up the same way as for any other agent.

### 2.5 Restart stack — DO NOT arm the claude-coupled monitors

**Critical rule (T-0822):** `context_fill_monitor.py`, `restart_drain.py`, and `handoff_and_restart.sh` are **inoperative** on non-claude harnesses and must not be armed. Specifically:

- `context_fill_monitor.py` reads `~/.claude/projects/**/*.jsonl` and Anthropic `message.usage` — codex sessions are `~/.codex/*.sqlite`, no JSONL. The monitor finds nothing and bails silently every cycle.
- `restart_drain.py` / `handoff_and_restart.sh` use `CLAUDE_PROCESS_PATTERN`, pane-sentinel regexes (`╭─+╮` / `❯`), `HANDOFF.md`, and claude kick prompts — none of these match a non-claude TUI.
- `ho-resumer` fires `claude --resume <id>` — this is a `claude`-specific flag; codex uses `codex resume --last`.

**Minimum viable recovery for non-claude harnesses:** the bare supervise loop (`Restart=always` in the systemd unit). This handles process death and restart. It loses graceful context-fill restarts, wedge-detection, and graceful-handoff — acceptable for a new harness at pilot stage. **Revisit trigger:** the harness graduates from pilot to load-bearing → build the per-harness recovery layer then.

To install and verify just the bare restart loop:
```bash
systemctl --user enable --now <agent>-bot-supervise.service
# Verify respawn: kill the session and confirm it comes back within ~10s
tmux kill-session -t <session>
sleep 10
systemctl --user is-active <agent>-bot-supervise.service   # → active
tmux ls | grep <session>                                   # → session present
```

**Standing safety constraint — compaction-as-restart MUST pin governance (T-1447 / T-1432; arXiv 2606.22528 "Governance Decay").** Pantheon's current Governance-Decay defense is *restart-before-compaction*: long-running agents restart (reloading the full fresh system prompt + re-firing SessionStart hooks) BEFORE crossing the native auto-compaction floor — `context_fill_monitor.py` fires at min(500k absolute degradation onset, 0.70×window) — e.g. 367,002 tokens on a 512Ki standard-Opus window, structurally below the ~466k compaction floor (T-1497 part B regrounding), guarded by `test_per_agent_windows_match_their_model`. The paper's failure mode (compaction silently evicts in-context safety constraints, 0→30% violation) is thereby avoided AND mitigated in depth by governance being **file-resident** (CLAUDE.md / pinned-rules / principles.md / directives.md, re-injected every session) rather than living only in the compactable conversation body. **The constraint:** if any future harness adopts the Anthropic Compaction API (`compact-2026-01-12`) — or any compact-in-place-as-restart-alternative — it MUST pin the governance block (Constraint Pinning per 2606.22528, which restores 0% violation), because compact-in-place bypasses the restart re-injection this architecture relies on. Do NOT adopt compaction-as-restart for a governance-bearing agent without governance pinning. *(No active exposure today — `compact-2026-01-12` is absent from `tools/` + `hooks/`, audited T-1432.)*

### 2.6 Ledger

`tools/ledger_shard.py` and the JSONL append pattern are **harness-independent** — they write to `ledger/<ISO-year>-W<NN>/<same>.jsonl` on disk via repo file ops. Any agent with repo access and an MCP write tool (or a hook that calls `python3 tools/ledger_shard.py append`) can write ledger entries. Wire this the same way as any other agent: provide the agent's `AGENTS.md` / `CLAUDE.md` with a ledger-append instruction, or register a `PostToolUse` hook that fires on significant actions.

### 2.7 Telegram

**Outbound (status pings, replies):** direct-curl `sendMessage` pattern works unchanged for any harness. Store the bot token at `~/.codex/pantheon.env` (0600) or the harness-appropriate equivalent. Example:
```bash
source ~/.codex/pantheon.env   # exports TELEGRAM_BOT_TOKEN
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
     -d chat_id=<operator-chat-id> \
     -d text="<message>"
```

**Inbound (operator → daemon):** not turnkey for non-claude harnesses (no equivalent of the Claude Code Telegram plugin). Current codex model: operator sends-keys directly into the tmux pane, or uses the pull-by-kick inbox pattern (§2.3). A per-harness inbound MCP server is the architectural path for future harnesses — see ORG-031 §B1.

---

## Part 3 — Wedge-bypass script

`tools/wedge_bypass.sh` automates the host-level and config-level wedges that block unattended operation. Run it **before** starting the service for the first time.

### Wedge catalog

All wedges that may block a new codex daemon (from `knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/recovery-bringup-gap-harvest-2026-06-09.md` §C):

| # | Wedge | Automatable | Script flag |
|---|---|---|---|
| W1 | **bwrap/userns/AppArmor (HIGHEST)** — Ubuntu 24.04 blocks bubblewrap; daemon runs but does ZERO file/git work (deceptive). Fix: AppArmor profile for `/usr/bin/bwrap`. | Yes (root req'd) | `--bwrap` |
| W2 | Approval-prompt — daemon wedges on every tool call unless `approval_policy="never"`. | Yes | `--approval` |
| W3 | Hook-trust gate — new hooks require pre-approval via `requirements.toml`. | Yes | `--hooks-trust` |
| W4 | GPT-5.5 NUX "press enter" — model-availability notice blocks TUI post-restart. (In investigation notes; **not** in bringup-SOP §12 — undocumented gap folded in here.) | Yes | `--nux-config` |
| W5 | Login / device-code — `codex login --device-auth` blocks on browser; never in a systemd unit. Use `--with-api-key` headless or run device-code by hand in tmux before enabling the service. | **No — human required** | (documented step) |
| W6 | Trust-directory prompt + onboarding menu — first-launch TUI prompts require operator at the keyboard. | **No — send-keys by hand** | (documented step) |
| W7 | Bad-auth crash-loop — codex with bad/expired key exits ~300ms; service shows `active` (false positive) while `NRestarts` climbs. Pre-place valid auth; check `NRestarts` is stable. | **No — diagnostic** | (check step) |
| W8 | `codex doctor` git warning — repo not detected in `~`; degrades AGENTS.md scope. Fix: run supervise script with `-C <repo>` flag. | **No — config step** | (documented step) |
| W9 | Shared-repo checkout permissions — `git pull --rebase` aborts on non-group-writable directories from another uid. Fix: `chmod g+w <dir>`. | **No — site-specific** | (documented step) |

### Running the script

```bash
# Preview all fixes without executing (always do this first):
bash tools/wedge_bypass.sh --dry-run --all --harness codex --user <account>

# Apply all automatable wedges (W1–W4). W1 requires root/sudo:
sudo bash tools/wedge_bypass.sh --all --harness codex --user <account>

# Verify post-conditions:
bash tools/wedge_bypass.sh --verify-only --harness codex --user <account>
```

Individual wedge flags: `--bwrap` `--approval` `--hooks-trust` `--nux-config`.

**All config fixes are idempotent.** Re-running the script does not duplicate keys or tables.

### W1: bwrap/AppArmor — what the script does

Writes `/etc/apparmor.d/bwrap` with an unconfined profile that grants `userns` capability to `/usr/bin/bwrap` only (the minimally-scoped fix — preserves Ubuntu's hardening for all other binaries), then calls `apparmor_parser -r`. Verify:

```bash
sudo -u <account> bwrap --ro-bind / / /bin/true
# Must exit 0. If it exits non-zero, the profile did not apply.
```

The blunt alternative (drops the hardening fleet-wide) is `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` — avoid unless the per-binary profile is structurally infeasible on your distro.

### W2: approval_policy — what the script does

Inserts into `~<account>/.codex/config.toml`:
```toml
approval_policy = "never"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

Idempotent: checks each key before inserting; skips if already present. **Critical:** `trust_level = "trusted"` alone does NOT silence approval prompts — `approval_policy` is the required axis (live-verified on the codex daemon, ORG-031 §ii).

### W5 (human): device-code login

In the tmux pane, before enabling the service:
```bash
codex login --device-auth   # follow browser flow
# OR for API-key auth:
printenv OPENAI_API_KEY | codex login --with-api-key
```

Auth lands at `~/.codex/auth.json` (0600). A codex started with no auth idles safely on the login menu; a codex with bad/expired auth exits in ~300ms and crashes the service loop.

### W6 (human): onboarding-menu / trust-dir dialog

On first launch, codex shows interactive TUI prompts that require a human to accept. The send-keys recipe, run in the tmux pane while the session is live:
```bash
# If shown "Allow this directory?" or similar:
tmux send-keys -t <session>:0 "1" Enter
# If shown the onboarding menu, select the default and accept.
```

After the first-launch acceptance, subsequent launches skip this prompt.

### W8 (config): codex doctor git

If `codex doctor` reports a git warning, the AGENTS.md scope degrades. Fix in the supervise script — change the launch command to include `-C <repo>`:

```bash
# In the supervise script ExecStart:
CLAUDE_BIN="$HOME/.npm-global/bin/codex"
CLAUDE_ARGS="-C $HOME/pantheon-config"
```

Or run `codex doctor --skip-git-repo-check` if repo-rooted operation is not needed.

---

## Verification checklist

Run top-to-bottom before declaring the daemon live.

### Host / supervise
- [ ] `wedge_bypass.sh --verify-only` exits 0 (post-condition checks pass).
- [ ] `systemctl --user is-active <service>` → `active`.
- [ ] `tmux ls` shows the expected session.
- [ ] `tmux capture-pane -p -t <session>:0` (full capture, not `| tail -5` — non-claude harnesses pad the bottom with blank lines) shows the expected harness TUI frame (not an auth-failed exit screen).
- [ ] Kill-respawn test: `tmux kill-session -t <session>` → service auto-respawns within ~10s; `NRestarts` increments once and settles.
- [ ] **Bad-auth crash-loop check:** `systemctl --user show <service> -p NRestarts` — if the value is climbing continuously, auth is bad/expired. Stop the service, fix auth, restart.

### Autonomy / config
- [ ] `codex doctor` reports `approval policy: never` (or harness equivalent — confirm via `grep approval_policy ~/.codex/config.toml`).
- [ ] `bwrap --ro-bind / / /bin/true` → exit 0 (as the daemon user; confirms userns unblocked).
- [ ] First real prompt to the daemon completes without an approval pop-up.

### Persona + ticketing
- [ ] `python3 -c "from tools.ticket import _valid_target_values; print('<persona>' in _valid_target_values())"` → `True`.
- [ ] `python3 -m pytest tools/test_ticket.py tools/test_ticket_lint.py -q` → all pass.
- [ ] `tools/ticket.py file --target <persona> --title "test" --intent action` → creates a ticket without `unknown target` error.
- [ ] Confirm `ticket.py read` and `ticket.py file` both work from the daemon's account.

### Recovery
- [ ] **Do NOT arm** `pantheon-context-fill-monitor@<persona>.timer` or `pantheon-restart-drain` for non-claude harnesses.
- [ ] `systemctl --user list-timers pantheon-*` shows no claude-coupled timers armed for this daemon (those timers should NOT be present for non-claude accounts).
- [ ] Bare respawn confirmed via kill-respawn test above.

### Ledger / Telegram
- [ ] Daemon can write a test ledger entry: append a line to `ledger/<current-week>/<shard>.jsonl` and confirm it commits + pushes cleanly.
- [ ] Outbound Telegram: `curl .../sendMessage` with the agent's bot token reaches the operator's device (outbound-only, no inbound required for initial verification).

---

## If the first-fire is not clean → disarm protocol

If the daemon wedges on first startup (stuck at a trust-folder dialog, login menu, or the first prompt is never responded to), the daemon is **not safe to leave running**. Disable and diagnose before proceeding:

```bash
systemctl --user stop <service>
systemctl --user disable <service>
# Investigate: tmux capture-pane, journalctl --user -u <service>
# Fix the blocker (re-run wedge_bypass.sh, complete login, etc.)
# Re-enable only after a clean manual test
```

---

## Codex-specific bringup delta summary

This section consolidates the codex-specific divergences from the claude-code bringup so they are visible in one place. For full detail, see `sop/standard/pantheon-bringup.md` §12.

| Step | Claude Code | Codex delta |
|---|---|---|
| Install | `npm i -g @anthropic-ai/claude-code` | `npm i -g @openai/codex` |
| Auth | `claude login` (interactive OAuth) | `codex login --with-api-key` (headless) or `--device-auth` (browser) |
| Config file | `~/.claude/settings.json` | `~/.codex/config.toml` (TOML, not JSON) |
| Autonomy | `permissions.allow` in settings.json | `approval_policy = "never"` + `sandbox_mode` in config.toml |
| Standing prompt | `CLAUDE.md` | `AGENTS.md` (or `model_instructions_file`) |
| Hooks | `~/.claude/settings.json` hooks + SessionStart/Stop/etc. | `hooks.json` or `[[hooks.<Event>]]` in config.toml; 10 events |
| Session resume | `claude --resume <id>` (flag) | `codex resume --last` (subcommand) |
| Context monitoring | JSONL `~/.claude/projects/**/*.jsonl` | SQLite `~/.codex/*.sqlite` — **no JSONL, no `context_window`** |
| Recovery monitors | `context_fill_monitor` + `restart_drain` | **DO NOT ARM** — claude-coupled, inoperative on codex |
| Inbox consumer timer | `pantheon-inbox-consumer@<persona>.timer` | **Do not arm** — use pull-by-kick instead |
| Chat surface | `--channels plugin:telegram@claude-plugins-official` | No plugin; outbound curl; inbound via custom MCP or send-keys |
| Session SQLite | N/A | `~/.codex/*.sqlite` — multiple databases for sessions/goals/state |

---

## Boundary

Covers: adapter binding guidance, orthogonal Pantheon wiring, wedge-bypass, and the harness-specific verification checklist.

Out of scope: Max-window cap-hit auto-resume (`ho_resumer.py`, claude-only), the full ORG-020 restart stack beyond the bare supervise loop (not built for non-claude harnesses at pilot stage), per-harness recovery layer engineering (follow-up work when the harness graduates from pilot).

---

## Cross-references

- `sop/standard/adding-restart-capability-to-a-new-agent.md` — the claude-specific deep-dive for the full ORG-020 5-capability restart stack.
- `sop/standard/pantheon-bringup.md` §12 — codex daemon variant (full §12 delta doc).
- `tools/wedge_bypass.sh` — the wedge-bypass automation script.
- `tools/harness_adapter/` — the `PantheonHarnessAdapter` implementation (T-1032).
- `adr/ORG-031-harness-abstraction-and-daemon-onboarding.md` — the ADR that ratifies this approach.
- `knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/recovery-bringup-gap-harvest-2026-06-09.md` — the ground-truth harvest this SOP is derived from.
- `sop/standard/handoff-restart-runbook.md` — how to fire a restart (operator surface).
- `sop/standard/cross-account-ops-discipline.md` — Recovery repair-sudo + cross-account patterns.
