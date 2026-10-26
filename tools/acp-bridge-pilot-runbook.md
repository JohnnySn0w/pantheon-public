# ACP inbound bridge — pilot runbook (T-1633, ORG-031)

Supervised-first-fire procedure for piloting ONE agent (suggested: **specialist**, the T-1630 spike owner) onto the mailbox→ACP inbound bridge, replacing tmux send-keys turn-injection with native JSON-RPC `session/prompt`. This is an **HO + the operator co-executed** flip (production-enable coordination); it is NOT auto-installed by any provision script. The tmux send-keys path stays as the rollback fallback until the pilot proves out.

Engine: `tools/acp_bridge.py` (+ `tools/acp_bridge_wetrun.py` empirical-gate driver). Deploy layer: `tools/acp-bridge-supervise.sh`, `tools/systemd/pantheon-acp-bridge@.service`, `tools/systemd/pantheon-acp-bridge-backlog@.service` + `.timer`. Spike ground truth: `knowledge/episodic/2026-07-07-claude-code-acp-spike/notes.md` (all rungs PASS).

Throughout, `<AGENT>` = the piloted persona (e.g. `specialist`), `<AGENT_CWD>` = that agent's REAL working directory (the cwd that makes its transcript land at `~/.claude/projects/<mangled-cwd>/…`, so `context_fill_monitor.py` composes unchanged), `<INBOX>` = the agent's Path-A hub inbox root `/home/post-office/mailbox/<AGENT>` (its `new/` is what the bridge consumes).

---

## Step 0 — pinned wrapper install

Install the EXACT pinned wrapper (NOT `@latest`) on the piloted agent's account:

```
cd ~   # every install/wetrun command in this runbook assumes a readable cwd (see Step 1 note)
npm install -g @zed-industries/claude-code-acp@0.16.2   # wraps Claude Agent SDK 0.2.44, node20
which claude-code-acp && npm ls -g @zed-industries/claude-code-acp   # confirm on PATH + version 0.16.2
```

Why pinned: the spike verified against 0.16.2 specifically; a silent `@latest` bump could change the JSON-RPC surface the engine assumes.

**Paper-cut (T-1689):** `claude-code-acp --version` prints NOTHING (exits 0, no output) — it is not a usable verify step. Use `npm ls -g @zed-industries/claude-code-acp` instead, which prints the resolved version.

## Step 1 — wet-run empirical gate (BEFORE any flip; D11)

The `session/load` wire-shape is implemented per the approved baseline but marked UNVERIFIED in `acp_bridge.py`'s docstring — the engine's any-error→`session/new` fallback makes this safe, and this wet-run is the empirical gate. Run against the REAL wrapper on the piloted account (clean-room; NO ANTHROPIC_*/CLAUDE_CODE_* env):

```
cd ~   # the wrapper CRASHES at import if cwd is unreadable (EACCES realpathSync) — always cd ~ first
export PATH="$HOME/.npm-global/bin:$PATH"   # wrapper lives in the npm prefix; NOT on minimal shells/units' PATH (first-flip find, T-1689)
python3 ~/pantheon-config/tools/acp_bridge_wetrun.py --agent <AGENT> --cwd <AGENT_CWD> --inbox <INBOX>
```

(No `--once` flag — a single drain is the DEFAULT; `--serve` is the looping mode. The supervise script sets its own PATH; this export is for manual wetrun/debug shells.)

**Paper-cut (T-1689):** the wrapper crashes at import time if its process cwd is unreadable (`EACCES` in `realpathSync`). This bit codex live — running from a directory the piloted account can't read (e.g. another account's checkout) fails before the bridge even starts. `cd ~` before every wetrun/install command in this runbook, not just this one.

Observe: `initialize` → `session/new` (first run, no state file) → prompt injection → terminal `stopReason`. Re-run once more to exercise `session/load` (state file now has a sessionId). Confirm either `session/load` succeeds OR it errors and cleanly falls back to `session/new`.

**Empty-session trap (T-1690, live-diagnosed):** the SDK writes NO transcript JSONL until a session completes its first turn, and the wrapper's `loadSession` fails `-32603` for any session without one ("Session not found" internally). So if the inbox is EMPTY during the gate runs, run 2's `session/load` fails for a reason that says nothing about production. **Send one canary turn before testing load** — either have a test message waiting in `new/` for run 1, or note that the load check only counts against a session that has completed a turn. `session/load` against a with-turn session is live-proven working on 0.16.2 (pilot bridge's session id survived a `systemctl restart`).

**Write-back step (follow-up commit):** record the observed `session/load` result into `acp_bridge.py`'s docstring, replacing the "unverified" mark with the empirical finding. This closes the D11 gap the code honestly flagged. (Done for 0.16.2 — the docstring carries the T-1690 resolution; repeat this step on any wrapper version bump.)

## Step 2 — pre-enable checks

1. **Inbox existence (from the ENOENT-as-alert decision):** confirm the inbox is provisioned — `ls -la <INBOX>/{tmp,new,cur}` all exist. The backlog watchdog now treats an ABSENT `new/` as an ALERT (a config error, not benign), so the inbox must genuinely exist before the timer is enabled. If missing, provision it first (`tools/mailbox.py` `provision(<INBOX>)`).
2. **Clean-room (the API-key-shadow money-trap):** confirm the bridge's environment carries NO `ANTHROPIC_*` / `CLAUDE_CODE_*` var (not even empty-string). `acp_bridge.py`'s `assert_clean_room()` hard-exits on a leak, and the supervise script's `compgen -v` strip is defense-in-depth — but verify at setup, because a leak would flip the agent to metered API billing (a money event reserved to the operator).
3. **Auth store:** confirm the agent's `~/.claude/.credentials.json` holds a valid OAuth (subscription) token — the wrapper consumes it directly (spike rung 4). No API key, no second cred store.

## Step 3 — quiesce the old path (EXCLUSIVE consumer — dispositions 1 + 3)

Ordering is LOAD-BEARING. If both the old send-keys/waker path AND the bridge drain `new/` concurrently, delivery splits across two consumers (the split-Maildir hazard). Establish a single consumer before starting the bridge:

1. **Disable (do NOT delete) the waker** — disposition 1:
   ```
   systemctl --user disable --now pantheon-mailbox-wake@<AGENT>.timer
   ```
   Disable-not-delete so rollback is a clean re-enable. (The `@.path` variant is already deprecated/not-installed under Path A; the `.timer` is the live waker.)
2. **Quiesce the agent's in-session consumer:** ensure the agent's existing TUI session is no longer draining `new/` (the bridge + its ACP wrapper becomes the agent's turn-intake for the pilot). Confirm the old session is idle / detached from consumption.
3. **Verify quiescent (strengthened, T-1689):** `ls <INBOX>/new/` shows backlog, not future consumers — it is NOT sufficient proof of single-consumer state. The real proof is:
   ```
   systemctl --user is-active pantheon-mailbox-wake@<AGENT>.timer   # must report: inactive
   ```
   The TUI's consume is **wake-driven**: it only drains `new/` in response to the waker's `send-keys` kick. With the timer `inactive`, there is no spontaneous TUI drain, so timer-inactive (not the `ls` snapshot) is the actual single-consumer proof before starting the bridge.

## Step 4 — start the bridge

```
systemctl --user enable --now pantheon-acp-bridge@<AGENT>.service      # supervise script hosts the bridge in tmux
systemctl --user enable --now pantheon-acp-bridge-backlog@<AGENT>.timer # backlog watchdog
```

Watch the bridge's tmux pane: it shows **per-drain status lines** (processed/errors per consume), **not** raw ACP `session/update` traffic — for wire-level debugging use `tools/acp_bridge_wetrun.py` interactively. The supervise script runs the bridge under `sg veneration -c` (the Path-A hub inbox is 2770 group `veneration`; a user-systemd service otherwise carries only the primary group — T-1015).

## Step 5 — verify the pilot

1. Send a test message: `python3 tools/mailbox.py` `send(<INBOX>, {...}, sender="orchestrator")` (or via the normal inter-agent path).
2. Confirm the bridge injects it: pane shows a drain; the message moves `new/` → `cur/` (acked) ONLY on a terminal `stopReason`.
3. Confirm hooks still fire (SessionStart/UserPromptSubmit/Stop — spike rung 3 verified they compose under the wrapper) and the transcript JSONL lands at the expected `~/.claude/projects/<mangled-cwd>/` path (so `context_fill_monitor.py` is unaffected).

## Auth-error handling (disposition 4)

If a turn returns **−32000 (Authentication required)**, the bridge **RAISES** — the message stays in `new/` (NOT acked, never silently swallowed). This means the agent's OAuth credential is broken. Operator action: re-login per `sop/remediation/agent-oauth-relogin.md`; the stranded message auto-retries on the next consume once auth is restored. A −32000 is a loud, retriable failure by design (it is a money-adjacent signal — a broken sub-cred).

## Rollback

Fully reversible (disposition 1):

```
systemctl --user disable --now pantheon-acp-bridge@<AGENT>.service
tmux kill-session -t acp-bridge-<AGENT>     # belt to the T-1689 supervise-script trap; harmless if already gone
systemctl --user disable --now pantheon-acp-bridge-backlog@<AGENT>.timer
systemctl --user enable  --now pantheon-mailbox-wake@<AGENT>.timer     # restore send-keys wake
```

Any messages the bridge did not ack are still in `new/`; the restored send-keys path drains them on the next wake. No message is lost across the flip or the rollback (the whole ack-only-on-terminal-stopReason contract exists for this).

**Double-delivery caveat (T-1689):** the mailbox contract is at-least-once, not exactly-once. If rollback happens DURING an in-flight turn — the bridge has already injected a message via `session/prompt` but hasn't yet reached a terminal `stopReason` to ack it — that message is still sitting in `new/` when the restored send-keys path comes back up, and the next wake will deliver it AGAIN even though the bridge already showed it to the model once. Before stopping the bridge, check its tmux pane for an active drain in progress; if one is running, let it reach a terminal `stopReason` (or accept the possibility of one duplicated turn) before rolling back.

## Monitoring after flip

The backlog watchdog (`pantheon-acp-bridge-backlog@<AGENT>.timer`, 5-min cadence, `Persistent=true`) alerts the operator directly if `new/` count or oldest-message age crosses threshold, OR if `new/` is unreadable/absent — the signatures of a dead/stuck/misconfigured bridge. It reads the hub inbox under the same `sg veneration -c` wrap; a permission or path regression fails LOUD, not silently-healthy.

## System-prompt budget constraint (T-1730, 2026-07-13)

The 2026-07 billing classifier (hermes-agent#53212, Anthropic closed as-intended) flips subscription-OAuth requests to overage when cumulative NON-Claude-Code system-prompt content passes ~4,500 chars. Verified 2026-07-13 on the live pilot: the bridge passes NO `_meta.systemPrompt` in `session/new`, and claude-code-acp 0.16.2 defaults to the pure `{type: "preset", preset: "claude_code"}` SDK preset with zero hardcoded prompt additions (dist grep clean) — measured non-Claude-Code system-prompt surface is **0 chars**, and a full-surface live turn (24k cache-creation tokens) stamped `service_tier: "standard"` / no `costUSD`, identical to a same-account TUI turn. **Standing constraint:** if any future bridge change passes `_meta.systemPrompt` (string override or `append`), the 4.5k-char budget becomes live — keep any append well under it and re-run the T-1730 stamp check after the change.
