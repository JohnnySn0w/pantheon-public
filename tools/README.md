# tools/

Operational scripts the Orchestrator runs on the VPS. Distinct from `rulebook/principles.md`/`adr/` (canonical docs) and `ledger/` (audit chain) — this directory is mechanism, not principle.

## Design discipline — audit prompt

Before adding a new responsibility to any tool here, run the audit prompt from [`lessons/2026-05-19-mini-swe-agent-simplicity-anchor.md`](../lessons/2026-05-19-mini-swe-agent-simplicity-anchor.md): *"What is the per-tool core logic, separated from substrate plumbing (HERMES guard, scope guard, auto-commit, pull-first, push retry, cross-account sudo wrapper, Telegram send, ledger entry)? Does THAT fit on one screen?"* If not, factor or split. The category-mismatch caveat in that note is load-bearing — Pantheon's tools aren't the agent, they're substrate HO operates on; the prompt is about per-responsibility logic shape, not absolute LoC.

## Index

| File | Purpose | Status |
|---|---|---|
| [`usage_monitor.py`](usage_monitor.py) | Token-rate proxy for Max-plan usage (B2). Cron'd every 30m; data collection only until threshold ratified. | shipped 2026-04-28 |
| [`watch_runner.py`](watch_runner.py) | Watch-loop driver. Reads `knowledge/watch/topics.md`, computes due topics by per-topic cadence, dispatches `researcher` agent per due topic. Auto-commits + pushes to origin/main after each fire (gated by HERMES guard, narrow scope). Runs as `claude-research` user. | shipped 2026-04-29; auto-commit landed 2026-05-02 (`bb9a6df`) |
| [`watch_digest.py`](watch_digest.py) | End-of-day summarizer. Walks today's `knowledge/watch/reports/<date>/` tree, classifies above-the-bar items, sends one Telegram digest. | shipped 2026-04-29 |
| [`ticket.py`](ticket.py) | Ticket-system lifecycle CLI (`file / claim / block / resolve / list`). `block` sets `not_before` or `depends_on` frontmatter in-place per ORG-019. Each transition is one narrow-scope commit with auto-push. ORG-003 substrate. | shipped 2026-05-02 (`23c0d8d`); T-0004 bug fix `b2cf699`; T-0453 ORG-019 retirement 2026-05-19 |
| [`ticket_grooming.py`](ticket_grooming.py) | Cron'd staleness scanner. Scans `tickets/active/`, idempotently appends `[grooming] stale` entries past threshold (`4h` default), sends Telegram digest as proof-of-life. Runs under Recovery's user (`claude-fallback`), daily 12:00 UTC. | shipped 2026-05-02 (`694689f`); T-0453 ORG-019 retirement of blocked-state scope 2026-05-19 |
| [`_git_utils.py`](_git_utils.py) | Shared HERMES regex + git-subprocess helpers for auto-commit pipelines. Used by `watch_runner.py`, `ticket.py`, `ticket_grooming.py`. | shipped 2026-05-02 (`9eecce5`) |
| [`systemd/`](systemd/) | systemd-user-timer + service unit templates (canonical copies; installed at `~/.config/systemd/user/`, not version-controlled there). Includes `pantheon-watch.{service,timer}` (08:00 UTC daily, claude-research user) and `pantheon-grooming.{service,timer}` (12:00 UTC daily, claude-fallback/Recovery user). | shipped 2026-04-29; grooming units `3d2bf91` |
| [`hooks/`](hooks/) | Claude Code session hooks owned by pantheon-config. Symlinked into `~/.claude/hooks/` (pattern mirrors `claude-config/agents/` → `~/.claude/agents/`). Currently: `handoff-auto-read.sh` (SessionStart, auto-reads fresh HANDOFF.md). | shipped 2026-05-02 (T-0008) |
| [`handoff_and_restart.sh`](handoff_and_restart.sh) | Graceful HO restart wrapper. Runs as Recovery (claude-fallback): nudges HO via tmux to write `/home/claude/HANDOFF.md` → waits for mtime advance → `systemctl restart` of the prime claude unit → verifies fresh session via `is-active` + tmux pane sentinel. Parameterized: `--tmux-session`, `--systemd-unit`, `--systemctl-mode {user,system}`, `--max-handoff-wait`, `--max-verify-wait`, `--verify-pattern`, `--dry-run`. Exit code = step that failed (1–7); usage error = 64. flock-serialized. | shipped 2026-05-02 (T-0008); deploy under Recovery via follow-on ticket |
| [`test_handoff_and_restart.sh`](test_handoff_and_restart.sh) | Bash smoke test for the wrapper. Sandbox + PATH-override stubs for tmux/systemctl/stat; 9 cases (happy path, lock contention, each step's failure, custom verify pattern, dry-run, usage error). | shipped 2026-05-02 (T-0008) |
| [`inbox_consumer.py`](inbox_consumer.py) | B9 inbox auto-consumer. Per-agent (`--agent <name>`): scans `tickets/inbox/`, idle-gates via tmux pane probe, injects claim+work prompt. Pre-fire git pull added 2026-05-07 (T-0118). Runs as `pantheon-inbox-consumer@<agent>.timer` on HO and Recovery (Specialist rollout in flight per T-0102). | shipped 2026-05-04 (T-0052); pull-fresh T-0118 |
| [`ho_resumer.py`](ho_resumer.py) | B8 auto-resume scheduler. Reads cap-hit state file (written by `hooks/ho_stop_failure.py`), fires `claude --resume <session_id>` once `resets_at + 90s` arrives. Runs every 5 min via `pantheon-ho-resumer.timer` (claude user). | shipped 2026-05-03 (T-0046) |
| [`agent_role_map.py`](agent_role_map.py) | Persona ↔ role-id resolution map. Single source of truth for the `orchestrator`/`specialist`/`recovery` persona ↔ `orchestrator`/`specialist`/`recovery` role-id mapping. Imported by `inbox_consumer.py`. | shipped 2026-05-04 (T-0056) |
| [`activity_digest.py`](activity_digest.py) | B10 system-pulse digest. 5h cycle (00:00/05:00/10:00/15:00/20:00 UTC). Reads pantheon-config + ledger + git logs, posts a short markdown digest to the operator's Telegram via the dedicated digest bot. Runs under Recovery (`claude-fallback`). | shipped 2026-05-04 (B10) |
| [`hermes_history_audit.py`](hermes_history_audit.py) | HERMES byte-sequence whole-history audit. Scans every committed blob (across history) for the unescaped trigger. Belt to the local pre-commit hook. (The server-side audit Action was removed 2026-06-09 per the T-0049 downgrade — ledger -106.) | shipped 2026-05-03 (T-0030) |
| [`hermes_diff_scan.py`](hermes_diff_scan.py) | HERMES unified-diff scanner — was the helper for the server-side audit Action (removed 2026-06-09, T-0049 downgrade); RETAINED pending the 2026-07-01 re-eval. NOT used by the pre-commit hook, which scans staged blobs via `_git_utils._HERMES_PATTERN` directly. | shipped 2026-05-03 |
| [`install_hooks.sh`](install_hooks.sh) | Idempotent activator for `.githooks/` (HERMES pre-commit + commit-msg). Sets `core.hooksPath = .githooks`, verifies executability. See top-level README §Git hooks. | shipped 2026-05-03 (T-0029) |
| `test_*.py` | Per-script unittest suite — `test_<X>.py` mirrors `<X>.py`. Run via `python3 -m unittest tools.test_<X>` (or full suite via `python3 -m unittest discover tools`) from repo root. Currently 12 test files covering every shipped script. | shipped alongside their targets |

---

## usage_monitor.py

Token-rate proxy for Max plan usage (B2 in BACKLOG.md, ratified 2026-04-28).

**What it does:** Walks `~/.claude/projects/*/*.jsonl` transcripts, sums per-turn token usage (input + output + cache read + cache write — all classes count toward Max plan limits) into rolling 1h/5h/24h/7d windows, appends a snapshot to `~/.local/state/pantheon/usage.jsonl`. Optionally alerts via Telegram when the 5h window crosses a threshold.

**Why this approach:** Anthropic's `/usage` slash command has no programmatic surface (no CLI subcommand, no documented endpoint, no cache file). The OAuth token in `~/.claude/.credentials.json` could in theory hit the undocumented `/usage` endpoint, but the opsec is poor (token leakage via tool-call logs, undocumented endpoint instability, TOS gray area). Token-rate proxy is structurally clean — no credentials touched, no third-party dependency to drift on.

**Limitations:** Doesn't tell you "X% of Max plan limit." Tells you "current rate vs. baseline." Sufficient for "alert when burn is anomalous," which is the actual ask.

### Usage

```
# Print current rolling-window summary (no state write):
python3 tools/usage_monitor.py --print-summary

# Cron-style data collection (writes snapshot, no alerting):
python3 tools/usage_monitor.py

# Alert if 5h window > N tokens (dry-run prints to stderr):
python3 tools/usage_monitor.py --alert-tokens 30000000

# Same, but actually send Telegram:
python3 tools/usage_monitor.py --alert-tokens 30000000 --send-telegram

# After ~14+ days of data, propose a tuned threshold:
python3 tools/usage_monitor.py --propose-threshold
```

### Cron

Currently installed via user crontab on this VPS:

```
*/30 * * * * /usr/bin/python3 /home/claude/pantheon-config/tools/usage_monitor.py >> /home/claude/.local/log/pantheon/usage_monitor.log 2>&1
```

Data-collection only at v0 — no `--alert-tokens` flag. Alerting flips on once the operator confirms a threshold (after ~7 days of baseline accumulation, run `--propose-threshold` to get a percentile-based suggestion).

### State

- `~/.local/state/pantheon/usage.jsonl` — append-only snapshot history. One line per cron invocation.
- `~/.local/state/pantheon/alert_cooldown` — single-line ISO timestamp of last sent alert. Suppresses re-alerts within `ALERT_COOLDOWN_HOURS` (default 4h).
- `~/.local/log/pantheon/usage_monitor.log` — cron stderr/stdout for debugging.

State lives outside the git repo (mutable runtime data).

### Telegram delivery

Reads `TELEGRAM_BOT_TOKEN` from `~/.claude/channels/telegram/.env` (same source the MCP plugin uses). Sends to the operator's chat_id (`456474197`, hardcoded — only allowed user per access.json). POSTs directly to `api.telegram.org/bot<TOKEN>/sendMessage` — no plugin/session involvement, so it works from cron without an active Claude session.

### Justifies-via

- **P4** (traceability): every snapshot is logged for audit; baseline is reproducible from state.
- **D3** (autonomy bar): alerting threshold is the operator's call; until set, monitor only collects.
- **P1** (architecture over policy): structural credential isolation — no OAuth tokens read; the bot token is a single narrow capability already granted to the channel.

(Renumbered 2026-04-28 per ORG-002. Original entries used the old P-numbering: P5→P4, P9→D3, P2→P1.)

---

## watch_runner.py

Watch-loop driver. Reads `pantheon-config/knowledge/watch/topics.md`, computes which topics are due based on cadence + last-fire state, dispatches a fresh Claude Code `researcher` session per due topic, writes the report into `knowledge/watch/reports/<YYYY-MM-DD>/<slug>.md`, appends an entry to the day's index.

### Usage

```
# Show what would fire (no dispatch):
python3 tools/watch_runner.py --dry-run

# Fire all topics whose cadence has elapsed:
python3 tools/watch_runner.py

# Fire one topic by slug regardless of cadence:
python3 tools/watch_runner.py --topic anthropic-claude-code-changelog

# Force fire everything (testing only):
python3 tools/watch_runner.py --force
```

### Trigger

Installed via systemd-user-timer (`tools/systemd/pantheon-watch.timer` template). Default fire schedule: daily at 08:00 UTC.

### State

- `~/.local/state/pantheon/watch/state.json` — per-topic `last_fire` ISO date + `last_outcome`. Regenerable from filesystem dates if lost.
- `~/.local/log/pantheon/watch/<date>-<slug>.log` — driver-captured stdout/stderr per dispatch.

### Justifies-via

- **P3** (decisions justify upward): each watch topic carries scope + cadence + budget + seed sources, all hand-curated; the loop materializes briefs deterministically from the spine.
- **D2** (hand-curated docs): `topics.md` is hand-curated; workers don't edit it. The driver reads it but never writes back.
- **P1** (architecture over policy): cadence is a structural property of each topic, not a "remember to check" heuristic.
- **D1** (preserve bandwidth): batched into one Telegram digest per day via `watch_digest.py`, not one ping per dispatch.

---

## watch_digest.py

End-of-day digest. Walks `knowledge/watch/reports/<YYYY-MM-DD>/`, summarizes each report into one line, flags above-the-bar items via keyword detection (CVE / RCE / supply-chain / billing / classifier / breaking change / etc.), POSTs a single Telegram message to the operator.

### Usage

```
# Today's digest (default):
python3 tools/watch_digest.py

# A specific date:
python3 tools/watch_digest.py --date 2026-04-29

# Print to stdout, do not send Telegram:
python3 tools/watch_digest.py --no-send
```

### Trigger

Installed via systemd-user-timer (`tools/systemd/pantheon-watch-digest.timer` template). Default schedule: daily at 18:00 UTC, after the watch driver has had time to land its fires.

### Telegram delivery

Same pattern as `usage_monitor.py`: reads `TELEGRAM_BOT_TOKEN` from `~/.claude/channels/telegram/.env`, POSTs directly to `api.telegram.org/bot<TOKEN>/sendMessage`. No plugin/session involvement; works from cron with no active Claude session.

### Above-the-bar markers

Reports are flagged urgent if their text contains any of: `CVE-`, `RCE`, `supply-chain`, `rugpull`, `deprecated`, `breaking change`, `exploit`, `vulnerability`, `policy change`, `billing`, `classifier`, `ToS update`. The list is conservative — false positives are cheap (one extra ⚠ in the digest) and false negatives are expensive (a missed CVE-class advisory). Tune `ABOVE_BAR_KEYWORDS` in the script as the loop runs and the calibration improves.

### Justifies-via

- **D1** (preserve bandwidth): one digest per day, not one ping per fire. Above-the-bar items are highlighted; below-the-bar items get one-line treatment so the operator can scan and skip.
- **P3**: classifier is conservative + tuneable; rationale for each flagged keyword is documented.

---

## systemd/

Canonical templates for the timer + service units. Install instructions in [`systemd/README.md`](systemd/README.md). The actual installed units live at `~/.config/systemd/user/` and are not version-controlled there (precedent: telegram bot units, per the `project_telegram_two_bot_topology` memory).

## Auto-index

<!-- AUTOGEN-START spine-index -->
| [`activity_digest.py`](activity_digest.py) | Activity digest — 5hr cycle system-pulse signal to the operator's Telegram. |
| [`activity_monitor.py`](activity_monitor.py) | activity_monitor — fire structural poke to an agent's pane when one of: |
| [`adaptive_finish_detect.py`](adaptive_finish_detect.py) | adaptive_finish_detect — Audit-tier Stop hook for narrate-then-exit detection. |
| [`adaptive_finish_digest.py`](adaptive_finish_digest.py) | tools/adaptive_finish_digest.py — weekly Telegram digest of adaptive-finish hits. |
| [`agent_role_map.py`](agent_role_map.py) | Persona-name <-> role-id resolution for Pantheon agents. |
| [`ancestor_consume.py`](ancestor_consume.py) | tools/ancestor_consume.py — the-ancestor daemon's mailbox dispatch loop (T-0765 #2 glue). |
| [`ancestor_liveness_monitor.py`](ancestor_liveness_monitor.py) | tools/ancestor_liveness_monitor.py — light alive-but-wedged recovery for the-ancestor (T-1134). |
| [`artifact_gc.py`](artifact_gc.py) | Artifact-GC — sweep stale ticket-scoped evidence dirs (T-1360). |
| [`auto_commit_contract_lint.py`](auto_commit_contract_lint.py) | auto_commit_contract_lint — structural enforcement of the auto-commit-cron default-safe contract. |
| [`channel_liveness_monitor.py`](channel_liveness_monitor.py) | Channel-liveness monitor — detect a comms-dark-but-session-healthy agent (T-0894). |
| [`codex_modal_monitor.py`](codex_modal_monitor.py) | tools/codex_modal_monitor.py — detect codex wedged on a rate-limit model-switch |
| [`conftest.py`](conftest.py) | Suite-wide safety net: no test may fire a real Telegram notification. |
| [`context_fill_monitor.py`](context_fill_monitor.py) | Context-fill monitor — auto-fire graceful restart on a Pantheon agent's session context-fill threshold. |
| [`continuity_counter.py`](continuity_counter.py) | continuity_counter.py — Pantheon system-continuity day-count tool. |
| [`credential_expiry_monitor.py`](credential_expiry_monitor.py) | Proactive OAuth-credential-expiry monitor for the fleet (T-1216 / Jun-2026 401 outage). |
| [`cycle_stats.py`](cycle_stats.py) | cycle_stats.py — read tool for the ledger-augmented metrics field. |
| [`directive_skill_drift_check.py`](directive_skill_drift_check.py) | Directive<->skill drift guard (T-0796). |
| [`disk_usage_monitor.py`](disk_usage_monitor.py) | Proactive resource monitor for the fleet — disk + RAM + CPU (2026-06-26 the operator ask; RAM/CPU legs T-1632). |
| [`dispatch.py`](dispatch.py) | Pantheon Dispatch contract + dispatch-ledger — schemaful cross-agent delegation audit. |
| [`fleet_health.py`](fleet_health.py) | Read-only fleet-health verification sweep (T-1049 — the *verifiable* half). |
| [`fleet_reboot.py`](fleet_reboot.py) | tools/fleet_reboot.py — Recovery-driven coordinated fleet reboot orchestration (T-1049). |
| [`graveyard.py`](graveyard.py) | tools/graveyard.py — durable HANDOFF archive for Veneration session-selection (T-0766). |
| [`hermes_diff_scan.py`](hermes_diff_scan.py) | Scan git unified-diff output for byte-sequence discipline violations. |
| [`hermes_history_audit.py`](hermes_history_audit.py) | Whole-history HERMES byte-sequence audit. |
| [`ho_resumer.py`](ho_resumer.py) | HO auto-resumer — reads the limit-hit state file and fires claude --resume. |
| [`hook_allowlist_lint.py`](hook_allowlist_lint.py) | Hook-allowlist pairing lint (T-1102). |
| [`inbox_consumer.py`](inbox_consumer.py) | Pantheon inbox auto-consumer. |
| [`keep_going.py`](keep_going.py) | tools/keep_going.py — periodic anti-idle nudge for a standing agent (T-0772 sibling). |
| [`kick_verify.py`](kick_verify.py) | tools/kick_verify.py — post-kick verification + bounded re-fire (T-0899). |
| [`kino_pwa_autodeploy.py`](kino_pwa_autodeploy.py) | kino-pwa auto-redeploy poller (T-0316, the "kino-pull-cron variant"). |
| [`knowledge_sweep.py`](knowledge_sweep.py) | Pantheon scripted knowledge-sweep driver (ORG-012 Phase C, T-0781). |
| [`ledger_shard.py`](ledger_shard.py) | Shared ledger-shard helpers for the weekly-subfolder layout (T-0148). |
| [`links.py`](links.py) | Pantheon typed-link traversal surface — read-only index/query over existing data. |
| [`lint_report_day_index.py`](lint_report_day_index.py) | lint_report_day_index.py — assert watch-report day-index READMEs match disk. |
| [`mailbox.py`](mailbox.py) | tools/mailbox.py — daemonless per-agent file-mailbox (ORG-026, T-0772). |
| [`mailbox_mcp.py`](mailbox_mcp.py) | tools/mailbox_mcp.py — MCP stdio server wrapping tools/mailbox.py (T-1112). |
| [`mailbox_wake.py`](mailbox_wake.py) | tools/mailbox_wake.py — the wake half of the ORG-026 mailbox (T-0772 deployment layer). |
| [`memory_index.py`](memory_index.py) | tools/memory_index.py — keep MEMORY.md's pin inventory drift-free (ORG-035 / T-1130). |
| [`memory_lint.py`](memory_lint.py) | tools/memory_lint.py — validate an agent's auto-memory pins (ORG-035 baseline; T-1129). |
| [`memory_provenance_backfill.py`](memory_provenance_backfill.py) | tools/memory_provenance_backfill.py — add ORG-035 provenance fields to memory pins (T-1131). |
| [`memory_search.py`](memory_search.py) | tools/memory_search.py — rg-backed search over an agent's memory pins (ORG-035 / T-1129). |
| [`migrate_ledger_to_weekly_shards.py`](migrate_ledger_to_weekly_shards.py) | One-shot migration: flat ledger/2026.jsonl → weekly subfolder shards (T-0148). |
| [`mint_telegram_sticker_set.py`](mint_telegram_sticker_set.py) | Mint Telegram sticker set(s) from assets/stickers/manifest.json (T-1002). |
| [`pantheon_config_sync.py`](pantheon_config_sync.py) | pantheon-config bidirectional sync — T-0211 Phase B. |
| [`policy_outflow_sync.py`](policy_outflow_sync.py) | tools/policy_outflow_sync.py — ORG-012 OUTFLOW: keep briefs pointed at changed policy. |
| [`postprocess_ai_sticker_png.py`](postprocess_ai_sticker_png.py) | Convert built-in image_gen chroma-key PNGs into 512x512 RGBA stickers. |
| [`promote_lessons.py`](promote_lessons.py) | Lessons → hooks promotion pipeline — detector (T-0649), increment 2. |
| [`promote_lessons_cron.py`](promote_lessons_cron.py) | Weekly promotion-pipeline runner (T-0649 inc 4). |
| [`provision_context_fill_monitor_dropins.py`](provision_context_fill_monitor_dropins.py) | Provision + verify the context-fill-monitor per-agent enablement drop-ins (T-1214). |
| [`provision_repos.py`](provision_repos.py) | provision_repos.py — T-0761 component 4: clone-on-demand repo provisioning. |
| [`reboot_safety.py`](reboot_safety.py) | Read-only PRE-reboot safety check: is the fleet's work all committed + pushed? |
| [`repo_registry.py`](repo_registry.py) | repo_registry.py — shared repos.yaml loader (T-1136). |
| [`restart_drain.py`](restart_drain.py) | Pantheon restart drain — process tombstone files and fire graceful restarts. |
| [`restart_request.py`](restart_request.py) | Pantheon restart request — operator-facing CLI to trigger a graceful agent restart. |
| [`retro_metrics.py`](retro_metrics.py) | retro_metrics.py — one-time best-effort retro-extraction of the cycle-stats |
| [`s3star_audit_runner.py`](s3star_audit_runner.py) | tools/s3star_audit_runner.py — HO-side scheduler for the ORG-038 System-3* |
| [`seed_genericize.py`](seed_genericize.py) | seed_genericize.py — T-1327 (recurring weekly). |
| [`session_reset_log.py`](session_reset_log.py) | session_reset_log — structured event log for session-reset class events. |
| [`session_reset_reconcile.py`](session_reset_reconcile.py) | session_reset_reconcile — daily cron that lands session-reset jsonl entries |
| [`soak_filer.py`](soak_filer.py) | Pantheon suite-health soak filer (T-0991 option b / T-0992). |
| [`spine_groom_cross_repo.py`](spine_groom_cross_repo.py) | tools/spine_groom_cross_repo.py — extend spine-index grooming to Pantheon-adjacent repos (T-0666). |
| [`spine_index_groom.py`](spine_index_groom.py) | Pantheon spine-index groomer — idempotent sentinel-region updater for all spine READMEs. |
| [`stale_worktree_sweeper.py`](stale_worktree_sweeper.py) | Stale git-worktree sweeper (T-1324 part b / 2026-06-25 VPS disk-full). |
| [`submit_verify.py`](submit_verify.py) | tools/submit_verify.py — harness-aware tmux submit-verify (T-1048). |
| [`tempfile_hygiene_lint.py`](tempfile_hygiene_lint.py) | tempfile_hygiene_lint — catch test temp-DIR creations with no registered cleanup. |
| [`ticket.py`](ticket.py) | Pantheon ticket lifecycle CLI. |
| [`ticket_grooming.py`](ticket_grooming.py) | Pantheon ticket-grooming staleness scanner. |
| [`ticket_lint.py`](ticket_lint.py) | Pantheon ticket-lint — hourly frontmatter format scanner and auto-fixer. |
| [`ticket_state_sweep.py`](ticket_state_sweep.py) | Pantheon ticket-state ↔ ledger consistency sweep. |
| [`usage_monitor.py`](usage_monitor.py) | Pantheon — usage monitor. |
| [`vault_link_check.py`](vault_link_check.py) | Vault link check — verify every [[wikilink]] in the repo resolves for Obsidian (T-1597). |
| [`veneration.py`](veneration.py) | tools/veneration.py — the resume-the-dead recovery harness (T-0766). |
| [`watch_digest.py`](watch_digest.py) | Pantheon watch-loop digest. |
| [`watch_runner.py`](watch_runner.py) | Pantheon watch-loop driver. |
| [`weekly_ticket_summary.py`](weekly_ticket_summary.py) | Pantheon weekly ticket-summary generator. |
<!-- AUTOGEN-END spine-index -->
