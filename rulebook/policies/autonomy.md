# Autonomy — inbox auto-consumer policy

**Status:** Ratified 2026-05-04 by the operator (BACKLOG §B9 design + Update 2026-05-04T14:45Z); migrated to rulebook/policies/ on 2026-05-10 per T-0191 (ORG-010 three-tier extension).
**Author:** Orchestrator (Orchestrator)
**Date:** 2026-05-04
**Re-evaluate when:** consumer cron mechanism changes; `autonomy_off` off-switch retired; new agent added to consumer coverage; bridge `/health.last_inbound_at` semantics change.
**Justifies via:** D1 (bandwidth preservation), D3 (autonomy bar), P3 (decisions justify upward). *(Citation corrected from drifted "P5 (decisions-driven)" per T-1587, ratified the operator 2026-07-06 msg 4664.)*

## Context

The pattern: a systemd timer fires every 10 minutes, runs `inbox_consumer.py --agent <name>`, checks a series of gating conditions, and — when all pass — injects the next eligible inbox ticket into the agent's tmux session. This is the operational calibration of D1 + D3 — what makes "HO acts autonomously when reasonable" actionable in code, with explicit kill-switches.

For the architecture rationale + ratification trail, see `BACKLOG.md` §B9 + Update 2026-05-04T14:45Z and ticket T-0052.

## Policy

> **Mechanism status (2026-10-18, T-4556):** the pantheon-bridge `/health.last_inbound_at` source named below (the "Bridge /health.last_inbound_at" row, step 4 of the clean cycle, and the `recent_telegram` skip vocab) was decommissioned on 2026-06-04, and the gate silently failed open until T-4205 replaced it. The `recent_telegram` gate now reads this account's own Claude Code session jsonls under `~/.claude/projects/` for the last Telegram inbound (`check_recent_inbound` in `tools/inbox_consumer.py`, via `tools/telegram_inbound.py`). The 5-minute threshold and the fail-open on probe error are unchanged. The decommission fired this policy's "bridge `/health.last_inbound_at` semantics change" re-evaluate trigger, and T-4205 was the repair that answered it. Since T-4556 the weekly re-eval walk (`tools/adr_reeval_walk.py`) also scans `rulebook/policies/` and `sop/` triggers (surface-only: they appear in the weekly summary and never auto-file a ticket). The ratified policy text below is unchanged.

### What runs where

| Component | Path | Where it runs |
|---|---|---|
| Consumer script | `tools/inbox_consumer.py` | systemd-cron'd via `pantheon-inbox-consumer@orchestrator.timer`, every 10 min on the `claude` user |
| Template service | `tools/systemd/pantheon-inbox-consumer@.service` | Installed to `~/.config/systemd/user/`; instantiated as `pantheon-inbox-consumer@orchestrator.service` |
| Template timer | `tools/systemd/pantheon-inbox-consumer@.timer` | Installed to `~/.config/systemd/user/`; instantiated as `pantheon-inbox-consumer@orchestrator.timer` |
| Autonomy off-switch | `~/.local/state/pantheon/autonomy_off` | Checked by consumer; presence = bail. Operator-created via `touch`. |
| Cap-hit interlock (read-side) | `~/.local/state/pantheon/ho_limit_hit.json` | Written by HO stop-failure hook; read by consumer (HO only); inhibits inject during cap-hit window |
| Bridge /health.last_inbound_at (read-side) | `http://127.0.0.1:8765/health` | Consumer GETs this each cycle; bails if recent Telegram inbound < 5 min ago |
| Consumer log | `~/.local/log/pantheon/inbox_consumer.log` | Written by consumer; one line per cycle: `<ISO8601-UTC> <agent> pulled T-NNNN` or `skip:<vocab>` |

## What a clean cycle looks like

1. Timer fires every 10 min (OnUnitActiveSec — starts counting from the previous activation's completion, not wall-clock).
2. Consumer checks `~/.local/state/pantheon/autonomy_off`. File absent → continue.
3. (HO only) Consumer checks `~/.local/state/pantheon/ho_limit_hit.json`. File absent or `resets_at + 90s` is in the past → continue.
4. Consumer GETs `http://127.0.0.1:8765/health`, reads `.last_inbound_at[orchestrator]`. Null or age ≥ 5 min → continue. (Bridge unreachable → fail-open, continue.)
5. Consumer runs `tmux capture-pane -t <target> -p`. Looks for any sentinel in `IDLE_SENTINELS` (allow-list: `? for shortcuts` for no-tasks idle, `ctrl+t to hide tasks` for task-list-visible idle, extensible per T-0065). Any match → agent is idle → continue.
6. Consumer scans `tickets/inbox/` for tickets where `target` resolves to orchestrator (persona name OR role-id `orchestrator` per the agent-role map, T-0056), `state == inbox`, all `depends_on` entries are in state `done`, no `interactive-required` label, and any `not_before` timestamp is in the past (T-0061). Empty set → `skip:empty_inbox`. Bucket precedence on partial-match cases: eligible > dep_blocked > interactive_required > not_before_unmet.
7. Consumer sorts eligible tickets by `created` ascending; lower numeric id as tiebreak. Takes the first (FIFO).
8. Consumer formats the locked inject prompt with the ticket id, title, agent name, and Ask body.
9. Consumer runs `tmux send-keys -t <target> "<formatted_prompt>"`, sleeps `INJECT_ENTER_DELAY` (~0.7s) so the TUI paste-mode settles, then runs `tmux send-keys -t <target> Enter` as a separate call. Non-zero exit on either call → `skip:tmux_unreachable`. Then `tmux capture-pane` verifies; if `[Pasted text` is still visible, → `skip:inject_pending_paste` (T-0059 fix).
10. Consumer appends `<ISO8601-UTC> orchestrator pulled T-NNNN` to `~/.local/log/pantheon/inbox_consumer.log`.
11. Consecutive-skip counter resets.

If any gating check fires, the cycle appends a `skip:<vocab>` line to the log and exits 0. On the 6th consecutive skip (≈ 1 hour at 10-min cadence), a Telegram alert is sent to the operator and the counter resets — so persistent breakage alerts approximately hourly rather than silently or on every cycle.

## Disable

Soft disable (preserves the timer, just stops it):

```bash
# As claude:
systemctl --user stop pantheon-inbox-consumer@orchestrator.timer
```

To re-enable:

```bash
systemctl --user start pantheon-inbox-consumer@orchestrator.timer
```

Backup hard-kill (no systemd access needed — works even if the timer is running):

```bash
touch ~/.local/state/pantheon/autonomy_off
```

The consumer checks this file at the top of every cycle and bails immediately. Remove it when you want to resume:

```bash
rm ~/.local/state/pantheon/autonomy_off
```

To disable and remove the timer permanently (e.g. while reconfiguring):

```bash
systemctl --user disable --now pantheon-inbox-consumer@orchestrator.timer
```

## Inspect skip reasons

Log location: `~/.local/log/pantheon/inbox_consumer.log`

Example commands:

```bash
# Last 20 cycle outcomes:
tail -20 ~/.local/log/pantheon/inbox_consumer.log

# All skips (any agent):
grep skip: ~/.local/log/pantheon/inbox_consumer.log

# Skips for a specific agent in the last hour:
grep orchestrator ~/.local/log/pantheon/inbox_consumer.log | grep skip: | tail -10

# All successful pulls:
grep pulled ~/.local/log/pantheon/inbox_consumer.log
```

Skip reason vocab:

| Vocab | Meaning |
|---|---|
| `off_switch` | `~/.local/state/pantheon/autonomy_off` file exists |
| `cap_hit` | HO cap-hit interlock active (`ho_limit_hit.json` present and `resets_at + 90s` in the future); HO only |
| `recent_telegram` | Last Telegram inbound to this agent was < 5 min ago; bridge `/health.last_inbound_at` |
| `mid_turn` | Agent tmux pane does not match any sentinel in `IDLE_SENTINELS` (allow-list of known idle TUI footers: `? for shortcuts`, `ctrl+t to hide tasks`, …) — agent is mid-turn. New idle states get added to the tuple as discovered (T-0065). |
| `tmux_unreachable` | `tmux capture-pane` or `tmux send-keys` returned non-zero |
| `empty_inbox` | No inbox tickets at all targeted at this agent |
| `dependency_blocked` | Tickets exist but all are blocked on unfinished `depends_on` prereqs; operator action: resolve the blocking ticket |
| `interactive_required` | Tickets exist and unblocked, but all have `interactive-required` label; operator action: pick up manually or remove the label |
| `not_before_unmet` | Tickets exist and are otherwise eligible, but all have a `not_before` ISO 8601 UTC datetime in the future. Auto-clears once the timestamp passes; no operator action required unless soak window needs adjustment. (T-0061, 2026-05-04: soak-ticket-awareness via the `not_before` frontmatter field; design ratified at T-0060.) |
| `inject_pending_paste` | Inject sent the prompt + Enter, but `capture-pane` shows `[Pasted text` still visible — the TUI's paste-mode buffered the prompt instead of submitting it. Operator action: focus the agent's tmux pane and press Enter to clear the pending paste; subsequent fires will retry. (T-0059, 2026-05-04: split-then-verify inject reduces incidence; this skip surfaces the residual cases.) |

## Supervised first-fire checklist

The first time `pantheon-inbox-consumer@orchestrator.timer` runs in production, the operator is reachable (not screen-watching, just on the other end of Telegram) for the first 2-3 cycles (~30 min). This is the acceptance gate for B9 — until the supervised first-fire passes, T-0042 stays active and the timer is not considered green for unattended operation.

**Pre-fire**

```bash
# As claude:
cp ~/pantheon-config/tools/systemd/pantheon-inbox-consumer@.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-inbox-consumer@.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user list-timers | grep inbox-consumer    # expect: timer not yet active
```

Sanity-check the units before enabling:

```bash
systemctl --user cat pantheon-inbox-consumer@orchestrator.service
systemctl --user cat pantheon-inbox-consumer@orchestrator.timer
```

The `%i` in the `[Service]` `ExecStart` should resolve to `--agent orchestrator`; the timer's `Unit=` should point at `pantheon-inbox-consumer@%i.service`.

**Cycle 1 — work in inbox, expect a clean pull**

File a fixture ticket so the first cycle has something to consume. The Ask should be a no-op — the goal is to verify the inject + claim + resolve flow, not to do real work:

```bash
PANTHEON_ACTOR=orchestrator python3 ~/pantheon-config/tools/ticket.py file \
  --requester orchestrator \
  --target role:head_engineering_orchestrator \
  --title "B9 first-fire smoke — no-op ack" \
  --labels "test-fixture,b9-smoke" \
  --stale-after "1d" \
  --ask "echo 'b9 first-fire ack'; resolve immediately."
```

Then enable and start the timer:

```bash
systemctl --user enable --now pantheon-inbox-consumer@orchestrator.timer
```

Watch the log:

```bash
tail -f ~/.local/log/pantheon/inbox_consumer.log
```

Within 10 minutes, expect a line: `<ISO8601-UTC> orchestrator pulled T-NNNN`. The consumer's tmux send-keys will land the locked inject prompt in `claude-telegram-bot:0` (HO's session).

HO will: receive the inject, claim the ticket via `ticket.py claim`, run the trivial Ask (echo + resolve), write a short ledger entry per locked-decision #4 schema. Total HO work: < 2 minutes per fixture.

Pass: pull line in log + ticket moved `inbox/` → `done/` + ledger entry present.

**Cycle 2 — empty inbox, expect a clean skip**

After Cycle 1's fixture is resolved, the inbox is empty. ~10 min after Cycle 1, Cycle 2 fires. Expect a `skip:empty_inbox` log line, no tmux inject. Pass: skip line in log + tmux pane unchanged.

**Cycle 3 — second fixture, verify the loop holds**

File a second fixture (same shape as Cycle 1's) before Cycle 3 fires:

```bash
PANTHEON_ACTOR=orchestrator python3 ~/pantheon-config/tools/ticket.py file \
  --requester orchestrator \
  --target role:head_engineering_orchestrator \
  --title "B9 first-fire smoke 2 — verify loop" \
  --labels "test-fixture,b9-smoke" \
  --stale-after "1d" \
  --ask "echo 'b9 first-fire smoke 2 ack'; resolve immediately."
```

Pass: Cycle 3 pulls, HO works + resolves, ledger entry written.

**Acceptance**

- Three cycles ran with the expected outcome shape (pull → skip:empty_inbox → pull).
- Two fixture tickets in `done/` with HO `orchestrator` actor on the resolve transition.
- Two ledger entries chained (one per pull cycle, action `inbox_auto_consume_<ticket-id>`).
- No `tmux_unreachable`, no `interactive_required`, no `dependency_blocked` skips.
- Consumer log readable + parseable.

When all four pass, T-0042 (B9 design parent) resolves with end-to-end acceptance, and the timer is considered green for unattended operation.

**Fail path**

Any unexpected outcome — no pull when work is present, inject lands but tmux pane shows wrong text, ledger entry malformed, repeated `tmux_unreachable` — halt the timer immediately:

```bash
systemctl --user disable --now pantheon-inbox-consumer@orchestrator.timer
```

Then capture the log + recent ledger entries:

```bash
tail -50 ~/.local/log/pantheon/inbox_consumer.log
tail -3 ~/pantheon-config/ledger/2026.jsonl
```

REBOUND back to the b9-inbox-consumer team (if still alive) with the log + ledger snippets. If the team has been torn down, file a follow-up ticket against `tools/inbox_consumer.py` with the failure mode + the log evidence; HO re-spins a fresh team for the fix.

**Post-acceptance**

After all three cycles pass:

1. Resolve T-0042 with the end-to-end acceptance evidence (`PANTHEON_ACTOR=orchestrator python3 tools/ticket.py resolve T-0042 --with "..."`).
2. Append a B9-arc-completion ledger entry chaining T-0051 + T-0052 + T-0042 ids and the smoke-fire results.
3. Co-author this checklist's content into git so it's preserved (this section was a TODO placeholder until first-fire). Already done if you're reading this in committed form.
4. Shut down the b9-inbox-consumer workers via SendMessage `shutdown_request` to b9-lead and b9-implementer. *(Mechanics re-expressed 2026-07-19, T-1817: TeamCreate/TeamDelete were removed in CC 2.1.178 — teams are session-implicit with no teardown verb; the shutdown concern is "no orphaned background teammate," which the shutdown_request round satisfies on its own.)*
5. Recovery picks up T-0053 (Recovery-side install) on her own flow; her supervised first-fire is a sibling exercise scoped to the claude-fallback user.

## How to handle a stuck inject

A stuck inject is when `tmux send-keys` returned 0 (logged as `pulled T-NNNN`) but the agent session did not process the prompt — either the agent was mid-turn at the moment of inject (race), the terminal is in a state that ate the input, or the prompt was injected but the agent chose to defer.

**Operator steps:**

1. **Check the log.** `tail -5 ~/.local/log/pantheon/inbox_consumer.log` — confirm the pull entry exists with the right ticket id.

2. **Check the tmux pane.** `tmux capture-pane -t <target> -p | tail -30` — look for the injected prompt text. If visible and the agent appears to be working on it, no action needed.

3. **If the prompt is visible but nothing happened** (agent was mid-turn at inject time):
   - Wait for the current turn to complete. The prompt may be in the input buffer and will be submitted on next Enter, or may have been swallowed.
   - If the pane is back at idle (`? for shortcuts`), re-inject manually: `tmux send-keys -t <target> "<full-prompt-text>" Enter`. You can reconstruct the prompt from the ticket body or re-run the consumer with autonomy_off removed and a forced idle state.

4. **If `tmux send-keys` failed (`skip:tmux_unreachable`):**
   - Check that the session exists: `tmux ls`.
   - Check `PANTHEON_HO_TMUX_TARGET` (or `PANTHEON_RECOVERY_TMUX_TARGET` for Recovery) is set to the correct session name.
   - Restart the target session if needed; the next timer fire will retry.

5. **If the ticket remains unclaimed after the next cycle:**
   - The consumer will re-evaluate it on the next fire. If the ticket is still `inbox` and eligible, it will be injected again. No manual re-queue needed.

6. **If you want to force an immediate retry** (skip the 10-min wait):
   ```bash
   python3 ~/pantheon-config/tools/inbox_consumer.py --agent orchestrator
   ```
   This runs the same logic the timer-driven fire runs.

---

# Activity digest — operator notes (T-0063 / B10)

> **RETIRED (retired 2026-09-29, T-4289).** `tools/activity_digest.py` and its units were removed: its pulse was status-only under T-4200 (action-only Telegram). The notes below are historical.

A sibling autonomy service to the inbox auto-consumer. Where the consumer pulls work IN, the digest pushes a "what happened" signal OUT. Cron'd under Recovery (post-T-0064 deploy); fires every 5h at fixed UTC offsets (00 / 05 / 10 / 15 / 20). Reads pantheon-config (`tickets/done/` resolved-this-window, BACKLOG.md for arc-name shortform), classifies tickets by project (repo-as-project heuristic via resolve-text scan) and arc (label match on `b<N>` pattern), renders short Telegram-Markdown, posts to the operator's chat. Quiet windows post a one-line "no notable activity" message rather than silence.

For the architecture rationale + ratification trail: BACKLOG.md §B10 + tickets T-0057 (design parent) + T-0063 (build) + T-0064 (Recovery deploy).

## Useful invocations

```bash
# Dry-run (renders the next digest to stdout without posting or writing ledger):
python3 ~/pantheon-config/tools/activity_digest.py --agent orchestrator --dry-run

# Live fire (used by the systemd timer; cron-context-safe):
python3 ~/pantheon-config/tools/activity_digest.py --agent recovery
```

The `--agent` flag is required (no implicit USER fallback). Persona names (`orchestrator`, `recovery`) flow into the Telegram-rendered surface and the per-fire log line; the corresponding role-id (`orchestrator`, `recovery`) lands in the ledger entry's `agent` field for audit-chain continuity per the agent-role map (T-0056).

## Quiet-window contract

Zero matched tickets in the window → ONE-line `*No notable activity in <window>*` message goes to Telegram. NO ledger entry is written. The log line records `<ts> <agent> <window-start> quiet`.

This is the structural answer to the digest-broken-vs-healthy ambiguity: if you stop receiving the 5h digests entirely, that's the broken signal; if you receive the quiet-window message, the service is healthy and the org just had a quiet window.

## Orchestrator-health auto-resolve (T-0201)

> **Mechanism status (2026-09-29, T-4203 / T-4206; updated 2026-10-18, T-4555):** the keystroke implementation described below, `RECOVERY_AUTO_RESOLVE_ALLOWLIST` in `tools/inbox_consumer.py`, was **retired in T-4203**. It was gated on the decommissioned pantheon-bridge and silent since 2026-06-03. **The ratified outcome is delivered structurally instead**: every seat's `permissions.allow` carries bare `Edit`/`Write` (pantheon-bringup SOP, agent-autonomy allowlist; T-0760, 2026-06-03). So file create/edit never prompts, and that includes paths OUTSIDE the seat's cwd (T-4217: observed on mason/artisan, hundreds of unattended out-of-cwd Write/Edit calls succeeding in under 2s). That is broader than posture C's out-of-cwd-escalate rule below. The operator's 2026-10-13 rulings make that rule moot for Pantheon's configured seat launches: permissions are enforced at the unix user, not by harness prompts (TG 11733); all harnesses get full unlocks (TG 11742); and Claude seats switch to bypassPermissions (TG 11753, see the T-4939 amendment below, rolled out via T-4935). The digest FYI T-4217 planned, and the post-hoc review ticket T-4555 asked for, are not needed: with seats in bypassPermissions there is no out-of-cwd prompt left to tighten or loosen. OS permissions still confine each seat to its own home, /tmp and shared dirs. Only Claude Code's protected paths still prompt (`.git`, `.claude`, shell rc files, and similar), which matches the escalate-always "edit its own settings" class. The ratified policy text below is unchanged. The dev-channels boot dialog is dismissed fail-closed by `verify_startup.sh` and the relaunch scripts; `unknown_dialog_monitor.py` alerts Recovery on unknown dialogs and on stuck tool-permission prompts (detect-only).

When the inbox-consumer's orchestrator-health probe detects a pane wedge sentinel, Recovery checks the codified `RECOVERY_AUTO_RESOLVE_ALLOWLIST` (in `tools/inbox_consumer.py`) before escalating. Allowlist entries pair a sentinel substring with a list of tmux send-keys ops; when matched, Recovery applies the keystrokes immediately + emits an informational Telegram (audit, not approval-gated) and clears the wedge state. Non-allowlisted sentinels go to the existing escalation path (alert to the operator with sentinel + recommended remediation).

Founding allowlist entry (Ratified the operator msg_797 2026-05-09 via T-0142):

- `"I am using this for local development"` → `["1", "Enter"]` — dev-channels confirm dialog that appears post-graceful-restart. Option 1 is safe + correct.

Explicitly disallowed (escalate, never auto-resolve):

- `"Yes, and allow Claude to edit its own settings"` — option 2 expands trust scope.
- `"Yes, and don't ask again for"` — option-2 prefix; expands trust.
- `"Do you want to proceed?"` — destructive-Bash dialog header; varies per command, needs human eyes.

Adding a new auto-resolve rule requires the operator ratification per T-0142 §3. The shape: file a ticket proposing the sentinel + action, surface the rationale (which incident pattern + which previous cases), wait for ratification before landing.

### T-0473 sentinel-catalog evaluation → posture C (context-aware auto-resolve, 2026-05-20)

Specialist's T-0473 research catalogued ~30 Claude Code v2.1.x permission/wedge prompt classes (`knowledge/watch/reports/2026-05-20/claude-code-wedge-sentinels.md`). T-0474 added them to `PANE_WEDGE_SENTINELS` (detect side). HO + Specialist initially recommended default-deny (escalate every new class — T-0475/T-0476); **the operator rejected that posture (msg_1654): "I don't want to block folder adds and edits unless they're actually unsafe. Otherwise, things should be pretty fluid and restriction free. that means a judgement call on restricted actions, but not an auto-no."**

**Ratified direction = posture C: context-aware auto-resolve.** Recovery auto-clears the normal in-context cases and escalates only the genuinely unusual:

- **Auto-resolve when safe-in-context** (per-sentinel predicate sees full pane content): file create/edit/overwrite when the path is in the agent's own worktree/cwd; folder-trust prompts for canonical Pantheon dirs; `"Enter plan mode?"` (read-only). The escalation path, when a predicate returns False, names WHY this specific instance was unusual (e.g. "out-of-cwd write at X").
- **Hard-deny (escalate always — no safe context)**: bypass-permissions, managed-settings, network-egress, out-of-cwd allow, skills, Computer-Use. These stay escalate-always.
- CAO's (`awslabs/cli-agent-orchestrator`) `skipDangerousModePermissionPrompt: true` up-front-bypass pattern stays rejected — posture C intercepts-and-judges rather than eliminating the guard.

Substrate work to implement posture C (extend `RECOVERY_AUTO_RESOLVE_ALLOWLIST` from binary `dict[str, list[str]]` to context-aware rules with predicates) landed under **T-0479** (PR #12: `AutoResolveRule` + `tools/_pane_predicates.py`). Each auto-resolveable sentinel class gets a per-class operator-ratification entry below (HO drafts the predicate boundary; the operator ratifies the diff; then the implementer wires it into the live allowlist). The T-0142 §3 "ratify before landing a new auto-resolve rule" gate stands — posture C changes the rule SHAPE (predicate-bearing), not the ratification requirement.

#### Amendment — Claude seats launch in bypassPermissions (the operator TG 11753, T-4939 / T-4935, 2026-10-13)

The operator's later decision supersedes the bypass-permissions item and the `skipDangerousModePermissionPrompt` line above, for Pantheon's own Claude seat launches only. Per TG 11742, "all harnesses get full unlocks". Per TG 11733, permissions are enforced at the unix user, not by harness prompts.

- **Launch mode.** Claude seats launch with `--permission-mode bypassPermissions`.
- **Dialog consent.** The seat's launch path writes `skipDangerousModePermissionPrompt: true` into the account's user-scope `settings.json` (`tools/bypass_consent.py`). Claude Code does not honor this consent from a `--settings` file. Without it, the one-time bypass warning dialog would stall unattended relaunches.
- **Enforcement is unchanged.** Blocking PreToolUse hooks (worker-guard, mcp-list-guard, background-pipe-gate), `permissions.deny` and `ask` rules all still apply under bypass. T-4935 verified this on the CLI. No seat carried deny or ask rules when this was adopted.
- **Escalate-always still holds** for a bypass prompt appearing on anything other than a configured seat launch, for example an ad-hoc session or a seat whose consent step failed. Recovery escalates those rather than auto-accepting them.

#### First live-predicate batch — Ratified 2026-05-20 by the operator (Telegram msg_1739, T-0480)

The following two predicate boundaries are ratified to go live. Predicate helpers (`path_in_agent_cwd`, `path_in_canonical_pantheon_dirs`) + dialog extractors already exist + are unit-tested in `tools/_pane_predicates.py` (T-0479). Only **option 1** (the in-scope action) is auto-sent; option-2 trust-expansions ("Yes, and don't ask again…", "allow Claude to edit its own settings") stay escalate-always.

| Boundary | Sentinels | Auto-resolve (send opt 1) WHEN | Escalate-with-diagnostic WHEN |
|---|---|---|---|
| **file-ops-in-cwd** | `Do you want to create`, `Do you want to make this edit to`, `Do you want to overwrite` | the file path in the dialog body is within the pane-agent's own cwd / worktree-prefix (`path_in_agent_cwd`) | otherwise → "out-of-cwd \<op\> at \<path\> — operator review" |
| **trust-canonical-dirs** | `Do you trust the files in this folder?`, `Yes, I trust this folder`, `trust this folder`, `Quick safety check: Is this a project you created…?` | the folder path is a canonical Pantheon dir (`path_in_canonical_pantheon_dirs`): `/home/claude/pantheon-config`, `/home/claude-research/pantheon-config`, `/home/claude-fallback/pantheon-config`, `/home/claude/echo-prismatic`, `/home/claude/pantheon-pwa`, `/tmp/<agent>-T-NNNN` worktree prefixes | otherwise → "unexpected folder \<path\> — Recovery declines auto-trust; operator review" |

Deferred: `Enter plan mode?` auto-resolve (Pantheon agents don't hit it in unattended flows). Ratified 2026-05-20 (T-0480); the implementer cycle that wires these two boundaries into the live allowlist is tracked by T-0489; production go-live is a supervised-first-fire.

*Status (2026-10-18, T-4565): T-0489 wired both boundaries (PR #18, f17679e, 2026-05-20). They went live through Recovery's auto-pull, not a supervised first-fire; T-0490, which tracked that first-fire, was closed overtaken-by-events per the operator (msg 2451, 2026-06-04). The keystroke mechanism was later retired in T-4203 (2026-09-29); see the banner under "Orchestrator-health auto-resolve". Nothing here is pending.*

## Boundary

This policy covers the inbox auto-consumer's gating + injection mechanics (the activity-digest cadence it also covered was retired 2026-09-29, T-4289). It does NOT cover:

- HO's discretionary direct-action scope (governed by `rulebook/policies/trust-posture.md`).
- Manual ticket pulls (HO can pull from `tickets/inbox/` directly anytime, bypassing the consumer).
- Specialist's research-watch cron (separate timer + queue; see `knowledge/watch/`).
- Recovery's grooming cadence parameters (those tools' parameters live with the tools, not here).
- Cap-hit auto-resume (preemptive context-fill restart) — see `sop/standard/auto-resume-operator-runbook.md`.

## Re-evaluate when

- Consumer cron mechanism changes (e.g., timer cadence, gating-condition list).
- The `autonomy_off` off-switch is retired or replaced.
- A new agent enters consumer coverage.
- The bridge `/health.last_inbound_at` semantics change.
- Real-world cap-hit incidents reveal interlock behaviour drift.

## Related

- `rulebook/directives.md` §D1 (bandwidth preservation) + §D3 (autonomy bar) — the org-level rules this policy operationalizes.
- `rulebook/policies/trust-posture.md` — calibration of HO's direct-action scope; sister policy.
- `BACKLOG.md` §B9 (inbox auto-consumer) + §B10 (activity digest, retired 2026-09-29, T-4289) — design rationale.
- `tools/inbox_consumer.py` — implementation (`tools/activity_digest.py` retired 2026-09-29, T-4289).
- `sop/standard/auto-resume-operator-runbook.md` — preemptive context-fill restart (sibling auto-rhythm; T-0184/T-0189).
- T-0052 (consumer ticket lineage), T-0057 + T-0063 + T-0064 (digest lineage).
