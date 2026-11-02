# tools/

Operational scripts the Orchestrator runs on the VPS. Distinct from `rulebook/principles.md`/`architecture/adr/` (canonical docs) and `ledger/` (audit chain) — this directory is mechanism, not principle.

## Design discipline — audit prompt

Before adding a new responsibility to any tool here, run the audit prompt from [`lessons/2026-05-19-mini-swe-agent-simplicity-anchor.md`](../lessons/2026-05-19-mini-swe-agent-simplicity-anchor.md): *"What is the per-tool core logic, separated from substrate plumbing (HERMES guard, scope guard, auto-commit, pull-first, push retry, cross-account sudo wrapper, Telegram send, ledger entry)? Does THAT fit on one screen?"* If not, factor or split. The category-mismatch caveat in that note is load-bearing — Pantheon's tools aren't the agent, they're substrate HO operates on; the prompt is about per-responsibility logic shape, not absolute LoC.

## Index

| File | Purpose | Status |
|---|---|---|
| [`usage_monitor.py`](usage_monitor.py) | Token-rate proxy for Max-plan usage (B2). Cron'd every 30m; data collection only until threshold ratified. | shipped 2026-04-28 |
| [`watch_runner.py`](watch_runner.py) | Watch-loop driver. Reads `knowledge/watch/topics.md`, computes due topics by per-topic cadence, dispatches `researcher` agent per due topic. Auto-commits + pushes to origin/main after each fire (gated by HERMES guard, narrow scope). Runs as `claude-research` user. | shipped 2026-04-29; auto-commit landed 2026-05-02 (`bb9a6df`) |
| [`watch_premature_termination.py`](watch_premature_termination.py) | Read-only sampler for watch-report "could not determine" / uncertain-negative language. Flags thin negatives and instrument-failure cases before they are treated as true negatives. | T-4971 |
| [`watch_digest.py`](watch_digest.py) | End-of-day summarizer. Walks today's `knowledge/watch/reports/<date>/` tree, classifies above-the-bar items, sends one Telegram digest. | shipped 2026-04-29 |
| [`quant_h2_sampler.py`](quant_h2_sampler.py) | ORG-102 H2 blind-sample sampler/reply parser. Samples source-bearing outcome calls, renders neutral blind paraphrases with authenticated `codex exec`, stores source+prompt/model audit fields, and computes weekly reply verdicts. | T-4780 |
| [`quant_weekly_digest.py`](quant_weekly_digest.py) | ORG-102 weekly digest builder. Writes the on-VPS Markdown digest, a plain-word Telegram pointer, and ensures the same weekly run produces the H2 outbox. | T-4780 |
| [`tool_schema_budget.py`](tool_schema_budget.py) | Read-only worker-profile measurement for MCP/tool-definition schema budget. Resolves profiles from `instance.yaml`, optionally probes configured stdio MCP servers with `tools/list`, estimates schema tokens, and flags the >25K / >50K / >100K tiers. | T-4970 |
| [`mcp_allowlist.yaml`](mcp_allowlist.yaml) | Sole runtime authority for ratified MCP server names, admitted seats, and source-grounded command/transport profiles. Markdown policy parity is test-only. | T-4679 |
| [`mcp_allowlist_registry.py`](mcp_allowlist_registry.py) | Strict, bounded, no-follow loader shared by the MCP audit and both provisioners. | T-4679 |
| [`mcp_allowlist_audit.py`](mcp_allowlist_audit.py) | Secret-free fleet MCP scanner and Recovery monitor with CLEAN/DRIFT/UNKNOWN verdicts, reviewed stdin probes, ORG-026 alerting, and default-dark unit convergence. | T-4679 |
| [`ticket.py`](ticket.py) | Ticket-system lifecycle CLI (`file / claim / block / resolve / list`). `block` sets `not_before` or `depends_on` frontmatter in-place per ORG-019. Each transition is one narrow-scope commit with auto-push. ORG-003 substrate. **Exit codes (T-4496):** `0` ok; `1` fault (ticket.py or its environment failed: git/IO, a guard, a non-main checkout, a corrupt ticket file); `2` usage error (argparse); `3` refusal (the caller's request was invalid and was declined: bad flag value, unknown target/requester/ticket, lineage or appetite gate, state mismatch, stale-view conflict). The `error: ...` stderr line is the same for 1 and 3; wrappers key on the code. `file` also refuses a non-T-NNNN `--depends-on` element and an unknown `--requester` (T-4499). | shipped 2026-05-02 (`23c0d8d`); T-0004 bug fix `b2cf699`; T-0453 ORG-019 retirement 2026-05-19 |
| [`ticket_grooming.py`](ticket_grooming.py) | Cron'd staleness scanner. Scans `tickets/active/`, idempotently appends `[grooming] stale` entries past threshold (`4h` default), sends Telegram digest as proof-of-life. Runs under Recovery's user (`claude-fallback`), daily 12:00 UTC. | shipped 2026-05-02 (`694689f`); T-0453 ORG-019 retirement of blocked-state scope 2026-05-19 |
| [`_git_utils.py`](_git_utils.py) | Shared HERMES regex + git-subprocess helpers for auto-commit pipelines. Used by `watch_runner.py`, `ticket.py`, `ticket_grooming.py`. | shipped 2026-05-02 (`9eecce5`) |
| [`systemd/`](systemd/) | systemd-user-timer + service unit templates (canonical copies; installed at `~/.config/systemd/user/`, not version-controlled there). Includes `pantheon-watch.{service,timer}` (08:00 UTC daily, claude-research user) and `pantheon-grooming.{service,timer}` (12:00 UTC daily, claude-fallback/Recovery user). | shipped 2026-04-29; grooming units `3d2bf91` |
| [`harness_adapter/`](harness_adapter/) | ORG-031 binding/codec layer: typed headless exec/resume descriptors, native event parsing, structural results, durable inbound receipts, and namespaced diagnostics for Claude Code and Codex. Provider policy stays provider-specific. | T-2372 |
| [`persona_bringup.py`](persona_bringup.py) | Resolve persona→role from `instance.yaml`, select or explicitly override the execution harness, diagnose the binding, run/resume structural turns, and consume durable persona mail with correlated replies plus unchanged-failure backoff. | T-2372 |
| [`persona_installer.py`](persona_installer.py) | ORG-049 sibling provisioner: build digest-addressed typed plans from `instance.yaml`; preflight actor/privilege and ownership gates; apply, verify, and rollback through fixed operation backends; preserve credential references without copying values. | T-2381 |
| [`a2a/`](a2a/) | A2A federation rung-1 surface (T-1882 ladder): `specialist_seat_server.py` (loopback JSON-RPC server, stub `research-request` skill), finalized Agent Card, systemd unit template, `PROVISION.md` (install steps + a2a-sdk 1.1.1 surface notes). Runs on the specialist account. | shipped 2026-07-20 (T-1886, PR #73) |
| [`hooks/`](hooks/) | Claude Code session hooks owned by pantheon-config. **Deployment is MIXED per FILE _and_ per ACCOUNT — enumerate, never generalise.** Measured 2026-07-28: `handoff-auto-read.sh` is a regular-file COPY on claude / claude-research / claude-fallback; `verify-hook-integrity.sh` and `ensure-hooks-active.sh` are SYMLINKS into that account's own clone on all three; `backlog-surface.sh` is a symlink on **claude only** and does not exist on the other two. **Use `tools/deploy_hook.py`** — it probes `[ -L ]` per (file, account) as that account, pulls for symlinks, copies for regular files, refuses when it cannot tell, and refuses if the canonical is not yet on origin/main. Deploying by hand with `cp` writes THROUGH a symlink into a peer's git worktree and blocks their `ticket.py` (T-2195 → T-2204); I got this row wrong twice by generalising from one account's listing, including my own. | shipped 2026-05-02 (T-0008); deploy tool 2026-07-28 (T-2195) |
| [`ci_runner_hooks/`](ci_runner_hooks/) | GitHub Actions runner hooks for the pantheon-config runners in CT202 (not Claude Code hooks; those are `hooks/` above). `job-started-fresh-workspace.sh` clears `$GITHUB_WORKSPACE` before every job, so a green run can only have tested a tree that job checked out (T-4385 hole B). It keeps git's object store and rewrites `.git/config` from a clean template, so checkout reuses the repo instead of re-cloning 113 MB per job. Its mode comes only from root-owned `/etc/pantheon-ci/fresh-workspace.conf`: `observe`, or `enforce` (the default when missing). `install.sh status|observe|enforce|rollback` wires it per runner via a systemd drop-in, restarts only an idle runner, and stages the rollback; the deploy one-liner and rollout order are in its header. With `install.sh home-on --users ...` it also empties the job user's HOME per job (user site, pip cache and config, `.gitconfig`), taking the home from passwd, never `$HOME`. home-on refuses any user that owns a non-pantheon-config runner or more than one pc runner, so it only works once each pc runner has its own uid (T-4425/T-4436). `install.sh toolcache-lock --pins "..."` makes `/opt/hostedtoolcache` root-owned and not group/other-writable, so no job can leave a `.pth` or trojaned package for later jobs. It refuses unless every interpreter holds exactly the pins and no stray `.pth`, reading `*.dist-info/METADATA` and never running the toolcache's Python as root. It also moves `AGENT_TOOLSDIRECTORY` into a root-owned drop-in; `toolcache-unlock --owner <user>` is the rollback. Tests: `test_ci_runner_fresh_workspace.sh` (runs under the runner's real `bash -e`, a stricter `-e -o pipefail`, and plain bash) and `test_ci_runner_hooks_install.sh`. | T-4416, T-4425 |
| [`handoff_and_restart.sh`](handoff_and_restart.sh) | Graceful HO restart wrapper. Runs as Recovery (claude-fallback): nudges HO via tmux to write `/home/claude/HANDOFF.md` → waits for mtime advance → `systemctl restart` of the prime claude unit → verifies fresh session via `is-active` + tmux pane sentinel. Parameterized: `--tmux-session`, `--systemd-unit`, `--systemctl-mode {user,system}`, `--max-handoff-wait`, `--max-verify-wait`, `--verify-pattern`, `--dry-run`. Exit code = step that failed (1–7); usage error = 64. flock-serialized. | shipped 2026-05-02 (T-0008); deploy under Recovery via follow-on ticket |
| [`test_handoff_and_restart.sh`](test_handoff_and_restart.sh) | Bash smoke test for the wrapper. Sandbox + PATH-override stubs for tmux/systemctl/stat; 9 cases (happy path, lock contention, each step's failure, custom verify pattern, dry-run, usage error). | shipped 2026-05-02 (T-0008) |
| [`inbox_consumer.py`](inbox_consumer.py) | B9 inbox auto-consumer. Per-agent (`--agent <name>`): scans `tickets/inbox/`, idle-gates via tmux pane probe, injects claim+work prompt. Pre-fire git pull added 2026-05-07 (T-0118). Runs as `pantheon-inbox-consumer@<agent>.timer` on HO and Recovery (Specialist rollout in flight per T-0102). | shipped 2026-05-04 (T-0052); pull-fresh T-0118 |
| [`ho_resumer.py`](ho_resumer.py) | B8 auto-resume scheduler. Reads cap-hit state file (written by `hooks/ho_stop_failure.py`), fires `claude --resume <session_id>` once `resets_at + 90s` arrives. Runs every 5 min via `pantheon-ho-resumer.timer` (claude user). | shipped 2026-05-03 (T-0046) |
| [`agent_role_map.py`](agent_role_map.py) | Persona ↔ role-id resolution map. Single source of truth for persona ↔ ORG-100 role-id ↔ seat-slot resolution (e.g. `orchestrator` ↔ `head_engineering_orchestrator` ↔ `head-engineering-orchestrator`); `ledger_identity()` gives a seat's ledger `agent`/`executor` per ORG-001. Legacy ids (`orchestrator`, `specialist`) resolve as history. Imported by `inbox_consumer.py`. | shipped 2026-05-04 (T-0056) |
| [`hermes_history_audit.py`](hermes_history_audit.py) | HERMES byte-sequence whole-history audit. Scans every committed blob (across history) for the unescaped trigger. Belt to the local pre-commit hook. It delegates `git show -p` parsing to `hermes_diff_scan.scan_diff_stream` (T-4185). | shipped 2026-05-03 (T-0030) |
| [`hermes_diff_scan.py`](hermes_diff_scan.py) | HERMES unified-diff scanner behind the server-side audit Action (`.github/workflows/hermes-audit.yml`, restored 2026-07-01 per T-1216). It frames patches from `@@` hunk counts, so no content line can pass as a file header. A stray `+` line is scanned fail-closed. Lines split on `\n` only, and input is streamed with bounded memory via `scan_diff_stream` (T-4185/T-4187). The push path feeds it a per-commit `git log -p -m` over a high-watermark range (T-4182). NOT used by the pre-commit hook, which scans staged blobs via `_git_utils._HERMES_PATTERN` directly. Known gap: files git renders as binary (T-4211). | shipped 2026-05-03 |
| [`install_hooks.sh`](install_hooks.sh) | Idempotent activator for `.githooks/` (HERMES pre-commit + commit-msg). Sets `core.hooksPath = .githooks`, verifies executability. See top-level README §Git hooks. | shipped 2026-05-03 (T-0029) |
| `test_*.py` | Per-script unittest suite — `test_<X>.py` mirrors `<X>.py`. Run via `python3 -m unittest tools.test_<X>` (or full suite via `python3 -m unittest discover tools`) from repo root; `pytest tools -q` also works and is what CI runs. One test file per shipped script — count deliberately not stated here, it rots (it read "12" until 2026-07-23, when there were 108). Note the full suite takes ~1.5 min and some single modules take 20-40s: a short `timeout` on a probe reads as a hang, see `bugs/short-timeout-probe-reads-slow-suite-as-hang.md`. | shipped alongside their targets |

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

Reads `TELEGRAM_BOT_TOKEN` from `~/.claude/channels/telegram/.env` (same source the MCP plugin uses). Sends to the operator's chat_id (`424242424`, hardcoded — only allowed user per access.json). POSTs directly to `api.telegram.org/bot<TOKEN>/sendMessage` — no plugin/session involvement, so it works from cron without an active Claude session.

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

## mcp_allowlist_audit.py

`tools/mcp_allowlist.yaml` is the sole runtime MCP authority. The scanner
derives seats and homes from `instance.yaml`; it never scrapes the policy
Markdown at runtime. Findings contain only seat, opaque source id, category,
safe server name, and fixed reason code. Command arguments, environment values,
and raw configuration values are never emitted.

```sh
# Human or deterministic JSON one-shot result:
python3 tools/mcp_allowlist_audit.py scan --format human
python3 tools/mcp_allowlist_audit.py scan --format json

# Alert/dedupe monitor; --dry-run performs no delivery or state write:
python3 tools/mcp_allowlist_audit.py monitor --format json --dry-run

# Copy canonical units and daemon-reload only:
python3 tools/mcp_allowlist_audit.py install
```

`scan` exits `0` for CLEAN, `2` for DRIFT, and `3` for UNKNOWN. `monitor`
retains the structured verdict but exits nonzero when mailbox delivery or
dedupe-state acknowledgement fails. `install` creates the owner-only state
directory and copies units without enabling, disabling, starting, or firing
them; later supervised activation is preserved.

## quant_h2_sampler.py

ORG-102 H2 sampler and reply parser. The sampler reads `call_outcomes.jsonl`,
draws a weekly blind sample from all old-enough source-bearing calls whose
outcome is `held`, `reversed`, or `overridden`, renders each sampled source
excerpt into two neutral plain-language sentences, and writes a Telegram-ready
outbox plus auditable JSONL sample rows. The operator is the grader; the renderer
only paraphrases the question text.

Production placement matters: the weekly sample must run on a seat where
`codex exec` is authenticated (currently Mason/Codex, or Consultant if explicitly
assigned). Do not move this sampler to a Recovery-hosted timer or any other host
without Codex auth. If the renderer is unavailable, the command exits nonzero
with `renderer_unavailable`; it must never silently fall back to extractive
rendering, because extractive rendering reintroduces selection bias.

```
python3 tools/quant_h2_sampler.py --out /var/lib/pantheon/quant sample \
  --outcomes /var/lib/pantheon/quant/call_outcomes.jsonl \
  --week-start 2026-10-05
```

Each `call_h2_samples.jsonl` row stores the capped source excerpt, source kind,
renderer prompt version/hash, renderer model/command metadata, render attempts,
and post-check result alongside the blind situation/call text. The scrub/leak
checks run after rendering only; they reject leaked ids, paths, ticket/PR/TG
refs, guidance refs, slash-number fragments, vague pronouns, and leading
judgment wording.

## quant_weekly_digest.py

ORG-102 weekly digest builder. It reads the rebuildable quant feeds under
`/var/lib/pantheon/quant`, writes `digest/weekly/quant-<week_start>.md`, writes
a short plain-word Telegram pointer at
`digest/outbox/quant-<week_start>-telegram.txt`, and ensures that week's H2
outbox exists so Orchestrator can relay both together in one the operator touch.

The canonical first timer host is Mason/Codex because the digest run includes
the Codex-rendered H2 sampler step. The pure-Python aggregation can run
anywhere later, but the first timer must stay where `codex exec` is
authenticated. The canonical templates are
`tools/systemd/pantheon-quant-weekly-digest.{service,timer}` and fire Monday
around 13:30Z; `--week-start` remains overrideable for dry builds and manual
backfills.

```
python3 tools/quant_weekly_digest.py build \
  --out /var/lib/pantheon/quant \
  --week-start 2026-10-05
```

## Auto-index

<!-- AUTOGEN-START spine-index -->
| [`ace_carry.py`](ace_carry.py) | ace_carry.py — ACE phase 1: advisory carry-forward of dropped open items (T-5349). |
| [`ace_curate.py`](ace_curate.py) | ace_curate.py — ACE-style itemized store + deterministic curator for HANDOFF.md. |
| [`acp_bridge.py`](acp_bridge.py) | tools/acp_bridge.py — mailbox -> ACP inbound bridge engine (T-1633, ORG-031 Unit 1). |
| [`acp_bridge_backlog_watchdog.py`](acp_bridge_backlog_watchdog.py) | tools/acp_bridge_backlog_watchdog.py — ACP-bridge mailbox backlog watchdog |
| [`acp_bridge_wetrun.py`](acp_bridge_wetrun.py) | tools/acp_bridge_wetrun.py — T-1633 empirical gate for tools/acp_bridge.py. |
| [`activity_monitor.py`](activity_monitor.py) | activity_monitor — fire structural poke to an agent's pane when one of: |
| [`activity_monitor_drift_check.py`](activity_monitor_drift_check.py) | tools/activity_monitor_drift_check.py — activity_monitor meta-drift alarm |
| [`adaptive_finish_detect.py`](adaptive_finish_detect.py) | adaptive_finish_detect — Audit-tier Stop hook for narrate-then-exit detection. |
| [`adaptive_finish_digest.py`](adaptive_finish_digest.py) | tools/adaptive_finish_digest.py — weekly Telegram digest of adaptive-finish hits. |
| [`adr_parallel_arc_lint.py`](adr_parallel_arc_lint.py) | Detect open tickets whose surface parallels an ADR file being pushed. |
| [`adr_reeval_walk.py`](adr_reeval_walk.py) | ADR / policy / SOP re-evaluation trigger walk (T-2527, design T-2526; T-4556). |
| [`adr_scoping_lint.py`](adr_scoping_lint.py) | Prevent-at-creation lint for ADR build-scoping (T-2806, the operator TG 7590). |
| [`agent_monitor_healthcheck.py`](agent_monitor_healthcheck.py) | Agent-monitor liveness watchdog — structural guard for the node event-loop |
| [`agent_role_map.py`](agent_role_map.py) | Persona-name <-> role-id resolution for Pantheon agents. |
| [`architecture_atlas.py`](architecture_atlas.py) | Architecture atlas derivation and validation contracts. |
| [`artifact_gc.py`](artifact_gc.py) | Artifact-GC — sweep stale ticket-scoped evidence dirs (T-1360). |
| [`assembly_resolver.py`](assembly_resolver.py) | Strict ORG-055 assembly manifest, deterministic lock, and compatibility resolver. |
| [`assembly_transaction.py`](assembly_transaction.py) | tools/assembly_transaction.py — T-3183: the bounded assembly update transaction. |
| [`auto_commit_contract_lint.py`](auto_commit_contract_lint.py) | auto_commit_contract_lint — structural enforcement of the auto-commit-cron default-safe contract. |
| [`backfill_ticket_lineage.py`](backfill_ticket_lineage.py) | One-shot lineage backfill over ORPHAN done tickets (T-3331, follow-up of T-3328). |
| [`backup_staleness_check.py`](backup_staleness_check.py) | backup_staleness_check.py — T-2886: alert when the off-box VPS backup goes stale. |
| [`breakglass_watch.py`](breakglass_watch.py) | Fleet break-glass watcher (T-4392 phase 2a; Recovery's surfacing ruling (d), 2026-09-30). |
| [`bugs_entry_lint.py`](bugs_entry_lint.py) | bugs_entry_lint.py — a NEW bugs/ entry must carry a `## Verification` section (T-4562). |
| [`bugs_frontmatter_lint.py`](bugs_frontmatter_lint.py) | bugs_frontmatter_lint — enforce the bugs/ YAML frontmatter contract. |
| [`build_harness_mirror.py`](build_harness_mirror.py) | Generate a release mirror's payload tree from a pinned pantheon-config sha (T-4768). |
| [`bypass_consent.py`](bypass_consent.py) | Pre-accept Claude Code's one-time bypass-permissions warning dialog (T-4935). |
| [`cap_modal_reaper.py`](cap_modal_reaper.py) | cap_modal_reaper.py (T-4024) — auto-dismiss a seat's spend-limit /rate-limit-options |
| [`cap_mute_driver.py`](cap_mute_driver.py) | cap_mute_driver.py (T-3844; cap-flag source re-pointed T-4041) — auto-mute |
| [`capability_navigator_contract.py`](capability_navigator_contract.py) | Pure ORG-059 catalogue-to-navigator contract (T-3218). |
| [`capability_navigator_engine.py`](capability_navigator_engine.py) | Pure ORG-059 capability navigator engine (T-3219). |
| [`cascade_sweep.py`](cascade_sweep.py) | tools/cascade_sweep.py -- level-triggered ORG-013 cascade sweep (T-4186). |
| [`channel_liveness_monitor.py`](channel_liveness_monitor.py) | Channel-liveness monitor — detect a comms-dark-but-session-healthy agent (T-0894). |
| [`check_dbus_tier_outcomes.py`](check_dbus_tier_outcomes.py) | check_dbus_tier_outcomes.py -- fail unless the Action Broker's D-Bus test tiers EXECUTED (T-4931). |
| [`check_decision_lines.py`](check_decision_lines.py) | WARN-only ORG-102 structured DECISION line check for PR bodies. |
| [`check_public_architecture_maps.py`](check_public_architecture_maps.py) | Validate public architecture diagrams as bounded derivatives of canon. |
| [`check_shell_suite_outcomes.py`](check_shell_suite_outcomes.py) | check_shell_suite_outcomes.py -- every tools/test_*.sh must have PASSED in the pytest report (T-4280). |
| [`check_suite_outcome_floor.py`](check_suite_outcome_floor.py) | check_suite_outcome_floor.py -- the Python suite must have RUN, judged by outcome (T-4372). |
| [`checkout_spec.py`](checkout_spec.py) | tools/checkout_spec.py — T-2951: the declarative checkout-lifecycle surface. |
| [`chronicle.py`](chronicle.py) | tools/chronicle.py — durable HANDOFF archive for Veneration session-selection (T-0766). |
| [`ci_audit_liveness.py`](ci_audit_liveness.py) | tools/ci_audit_liveness.py — alert when the origin-side byte-sequence audit STOPS RUNNING (T-2440). |
| [`ci_fleet_account_reconcile.py`](ci_fleet_account_reconcile.py) | tools/ci_fleet_account_reconcile.py — ORG-066 Rung-1 fleet-account reconciler (T-2219). |
| [`ci_nightly_watch.py`](ci_nightly_watch.py) | tools/ci_nightly_watch.py — a red nightly CI run must reach orchestrator the same day (T-4402). |
| [`ci_runner_disk_monitor.py`](ci_runner_disk_monitor.py) | CI-runner disk monitor: CT202 (pve2 `ci-runner`) rootfs + /ci-cache free space (T-4176). |
| [`ci_watermark_base.py`](ci_watermark_base.py) | tools/ci_watermark_base.py -- high-watermark diff base for range-scanning CI workflows (T-4182). |
| [`cited_hash_lint.py`](cited_hash_lint.py) | cited_hash_lint — find commit hashes cited in tickets that are NOT on origin/main. |
| [`cited_ticket_lint.py`](cited_ticket_lint.py) | cited_ticket_lint — find T-NNNN ids cited in commit messages that did not exist yet. |
| [`claude_exe_sync.py`](claude_exe_sync.py) | T-5038: keep a root-owned copy of a seat's claude.exe under /opt. |
| [`claude_quota_probe.py`](claude_quota_probe.py) | Run one isolated Claude ping/pong turn to refresh subscription quota data. |
| [`claude_statusline_capture.py`](claude_statusline_capture.py) | Persist Claude Code status-line quota data as a minimal atomic observation. |
| [`closure_gate_health_monitor.py`](closure_gate_health_monitor.py) | Closure-gate health monitor (T-2845 / ORG-065 BT5 flip prerequisite). |
| [`codex_context_lifecycle.py`](codex_context_lifecycle.py) | Codex context lifecycle primitives for T-2957. |
| [`codex_delivery.py`](codex_delivery.py) | T-4988 / ORG-103: verified delivery of input to a codex seat via the codex app-server. |
| [`codex_hook_trust_check.py`](codex_hook_trust_check.py) | codex_hook_trust_check.py — is this account's codex worker-guard wired AND trusted? (T-4155) |
| [`codex_modal.py`](codex_modal.py) | tools/codex_modal.py -- shared codex first-run-modal detection + precise |
| [`codex_modal_monitor.py`](codex_modal_monitor.py) | tools/codex_modal_monitor.py — detect codex wedged on a blocking TUI modal, ALERT |
| [`codex_recovery.py`](codex_recovery.py) | codex-recovery broker — ORG-048 §5 privilege surface, increment 1 (T-2805), |
| [`codex_recovery_sudoers.py`](codex_recovery_sudoers.py) | codex_recovery_sudoers — generate the codex-recovery sudoers fragment from a |
| [`codex_schema_contract.py`](codex_schema_contract.py) | ORG-103 L5 (T-5305): pin codex per app-server seat and gate every bump on the schema. |
| [`codex_seat_delivery.py`](codex_seat_delivery.py) | ORG-103 L3 (T-5304): the single automated writer of input to one codex seat. |
| [`codex_telegram_actions.py`](codex_telegram_actions.py) | Shared Telegram receiver handoff action ordering. |
| [`codex_telegram_bridge.py`](codex_telegram_bridge.py) | Target-local Codex Telegram bridge controls for T-2950. |
| [`codex_worker_guard_rollout.py`](codex_worker_guard_rollout.py) | codex_worker_guard_rollout.py: verify or apply the codex worker-guard block upgrade on ONE home (T-4395). |
| [`component_catalogue.py`](component_catalogue.py) | Strict ORG-055 component catalogue, boundary decisions, and generated index. |
| [`confined_load_burner.py`](confined_load_burner.py) | confined_load_burner.py - one CPU burner for confined_load.sh (T-4349). |
| [`conftest.py`](conftest.py) | Suite-wide safety net: no test may fire a real Telegram notification. |
| [`consume_onboard.py`](consume_onboard.py) | T-2679: one-command, persona-parameterized onboarding of a from-scratch, |
| [`consumer_liveness_monitor.py`](consumer_liveness_monitor.py) | tools/consumer_liveness_monitor.py — a live inbox-consumer that SILENTLY stops |
| [`context_fill_hold.py`](context_fill_hold.py) | context_fill_hold — explicit, TTL-bounded hold on context_fill_monitor's |
| [`context_fill_monitor.py`](context_fill_monitor.py) | Context-fill monitor — auto-fire graceful restart on a Pantheon agent's session context-fill threshold. |
| [`continuity_counter.py`](continuity_counter.py) | continuity_counter.py — Pantheon system-continuity day-count tool. |
| [`continuity_response_capture.py`](continuity_response_capture.py) | Candidate response-capture adapter (T-3461). |
| [`continuity_switch_coordinator.py`](continuity_switch_coordinator.py) | External ContinuitySwitchCoordinator (T-3177). |
| [`credential_broker.py`](credential_broker.py) | Provider-neutral credential leases for Pantheon (ORG-051, T-2380). |
| [`credential_broker_cli.py`](credential_broker_cli.py) | Installer-interface CLI over tools/credential_broker.py (ORG-051, T-2868). |
| [`credential_expiry_monitor.py`](credential_expiry_monitor.py) | Proactive OAuth-credential-expiry monitor for the fleet (T-1216 / Jun-2026 401 outage). |
| [`cycle_stats.py`](cycle_stats.py) | cycle_stats.py — read tool for the ledger-augmented metrics field. |
| [`dangling_identifier_scan.py`](dangling_identifier_scan.py) | tools/dangling_identifier_scan.py — find identifiers that ONLY exist in prose (T-2151). |
| [`decision_lines.py`](decision_lines.py) | ORG-102 structured DECISION line parser. |
| [`decision_recall_poc.py`](decision_recall_poc.py) | Decision-recall PoC (T-2816) — offline retrieval-quality eval, NOT a live hook. |
| [`deepseek_seat_monitor.py`](deepseek_seat_monitor.py) | deepseek_seat_monitor — Recovery's dry-run check of the DeepSeek seat (T-5125, T-4924 H3b MR-2). |
| [`deepseek_seat_watcher.py`](deepseek_seat_watcher.py) | deepseek_seat_watcher — one deterministic watcher cycle for the DeepSeek seat (T-5125, T-4924 H3b). |
| [`delegation_route.py`](delegation_route.py) | ORG-059 Child E — typed capability-delegation route adapter (T-3222). |
| [`delivery_reachability.py`](delivery_reachability.py) | tools/delivery_reachability.py — can each seat ACTUALLY receive a ticket right now? (T-2232) |
| [`delivery_rebase_controller.py`](delivery_rebase_controller.py) | Delivery-machine REBASE-crash controller (ORG-096 / RFC-002, ticket T-3651). |
| [`deploy_hook.py`](deploy_hook.py) | deploy_hook — deploy a pantheon-config hook to accounts WITHOUT writing through a symlink. |
| [`direct_code_push_audit.py`](direct_code_push_audit.py) | Direct-code-push audit: alert when code lands on main with no merged PR (T-4226). |
| [`directive_skill_drift_check.py`](directive_skill_drift_check.py) | Directive<->skill drift guard (T-0796). |
| [`disk_health_monitor.py`](disk_health_monitor.py) | disk_health_monitor.py — alert on pve2 disk defects, wear and write rate from RAW SMART (T-4997). |
| [`disk_usage_monitor.py`](disk_usage_monitor.py) | Proactive resource monitor for the fleet — disk + RAM + CPU (2026-06-26 the operator ask; RAM/CPU legs T-1632). |
| [`dispatch.py`](dispatch.py) | Pantheon Dispatch contract + dispatch-ledger — schemaful cross-agent delegation audit. |
| [`doc_traversal_report.py`](doc_traversal_report.py) | Transcript-mining observability — governance-doc traversal report (T-1805). |
| [`durable_commit_lint.py`](durable_commit_lint.py) | durable_commit_lint — structural guard that keeps the T-2334 concurrent-writer |
| [`echo_corpus_lint.py`](echo_corpus_lint.py) | Echo-corpus obligation lint — companion to recovery's pantheon-ticket-lint |
| [`echo_evidence_build.py`](echo_evidence_build.py) | Run an Echo evidence build (review smoke, merge gate) in a per-worktree isolated CARGO_TARGET_DIR (T-4122). |
| [`echo_target_adopt.py`](echo_target_adopt.py) | echo_target_adopt.py — re-own a PEER seat's build-script OUT files in the shared |
| [`echo_target_prune.py`](echo_target_prune.py) | echo_target_prune.py -- bounded, UNIT-granular prune of the shared echo cargo target (T-4852). |
| [`echo_worktree_switch.py`](echo_worktree_switch.py) | echo_worktree_switch.py — stop a shared cargo target from handing one echo |
| [`episode_store.py`](episode_store.py) | ORG-076 episode store -- detect -> one episode -> exactly one repair ticket (T-4707 / BT1). |
| [`failed_unit_reconcile.py`](failed_unit_reconcile.py) | failed_unit_reconcile — find unit failures whose ALERT never arrived (T-2362). |
| [`fleet_clone_currency_check.py`](fleet_clone_currency_check.py) | tools/fleet_clone_currency_check.py — fleet clone-currency check (T-2798, T-2794 option-b). |
| [`fleet_health.py`](fleet_health.py) | Read-only fleet-health verification sweep (T-1049 — the *verifiable* half). |
| [`fleet_host_health.py`](fleet_host_health.py) | tools/fleet_host_health.py — off-site availability poller for the home cluster (T-2454). |
| [`fleet_reboot.py`](fleet_reboot.py) | tools/fleet_reboot.py — Recovery-driven coordinated fleet reboot orchestration (T-1049). |
| [`fleet_snapshot_liveness_check.py`](fleet_snapshot_liveness_check.py) | tools/fleet_snapshot_liveness_check.py — per-persona snapshot-timer liveness (T-3791). |
| [`fleet_usage_gate.py`](fleet_usage_gate.py) | tools/fleet_usage_gate.py — fleet usage gate for the periodic anti-idle pokes (T-4510). |
| [`flip_policy_report.py`](flip_policy_report.py) | T-5451: read-only report of the codex-flip policy per seat (T-5446). |
| [`forgejo_mirror_verify.py`](forgejo_mirror_verify.py) | forgejo_mirror_verify — prove the github-mirror repos actually hold what they claim (T-2217). |
| [`forgejo_repo_rollout.py`](forgejo_repo_rollout.py) | forgejo_repo_rollout — make every GitHub repo exist on Forgejo (ORG-101 §4/§6, T-4690 leg 3). |
| [`forward_ref_gate.py`](forward_ref_gate.py) | Reject a commit message citing a ticket that does not exist yet. |
| [`frontmatter_registry.py`](frontmatter_registry.py) | Ticket-frontmatter field registry, read at run time from ORG-021 (T-4751 S1). |
| [`geo_hook_health_monitor.py`](geo_hook_health_monitor.py) | Recovery-side liveness monitor for the geo-hook service on pve2 (T-3647). |
| [`gepa_eval_extract.py`](gepa_eval_extract.py) | GEPA reviewer-eval candidate extractor (T-2809 Phase 1). |
| [`git_conformance_sweep.py`](git_conformance_sweep.py) | git_conformance_sweep — daily read-only check of the ORG-101 git-redundancy standard (§7, T-4690). |
| [`git_fixture.py`](git_fixture.py) | tools/git_fixture.py — the shared bubble-safe real-git test harness (T-3183 §1). |
| [`git_reconciler.py`](git_reconciler.py) | git_reconciler — ff-only two-way sync between GitHub and Forgejo (ORG-101 §3, T-4690). |
| [`github_companion_watch.py`](github_companion_watch.py) | Query GitHub for small companion-memory entrants. |
| [`goal_registry.py`](goal_registry.py) | Canonical machine-readable goal keys (T-5679). |
| [`grand_project_program.py`](grand_project_program.py) | ORG-061 stage evidence and owned re-evaluation obligation machinery. |
| [`handoff_conservation.py`](handoff_conservation.py) | tools/handoff_conservation.py — detect silent detail loss across a HANDOFF rewrite (T-2638). |
| [`handoff_first.py`](handoff_first.py) | Handoff-first: ask a live session to write its HANDOFF before it is killed (T-5069). |
| [`harness.py`](harness.py) | tools/harness.py — manual-override CLI for the T-2953 harness controller (T-2967 BT4). |
| [`harness_permission_posture.py`](harness_permission_posture.py) | Fixed transitional Codex permission posture helpers. |
| [`harness_state.py`](harness_state.py) | Durable-state schema helpers for the T-2953 harness controller (T-2967 BT4). |
| [`harness_telegram.py`](harness_telegram.py) | tools/harness_telegram.py — /harness command surface for Telegram (T-2967 BT4). |
| [`headless_billing_canary.py`](headless_billing_canary.py) | Daily headless canary for the subscription-reroute class (T-4634, cluster T-3897). |
| [`health_collector.py`](health_collector.py) | health_collector — one sanitized, enum-only fleet health aggregate (T-5395, ORG-068 E10). |
| [`health_export.py`](health_export.py) | health_export — sanitized, enum-only fleet health summary per persona (T-4884 B2). |
| [`health_signal_writer.py`](health_signal_writer.py) | tools/health_signal_writer.py — ORG-097 external-authority health signal-writer. |
| [`health_watcher.py`](health_watcher.py) | health_watcher — the SUBSCRIBING leg of the fleet health collector (T-5409, ORG-080 SS5.5). |
| [`heartbeat_write.py`](heartbeat_write.py) | Write a per-agent liveness heartbeat file (ORG-048 D2a). |
| [`hermes_audit_dispatch.py`](hermes_audit_dispatch.py) | Dispatch the byte-sequence audit on main when main's tip is not yet covered (T-5354). |
| [`hermes_diff_scan.py`](hermes_diff_scan.py) | Scan git unified-diff output for byte-sequence discipline violations. |
| [`hermes_history_audit.py`](hermes_history_audit.py) | Whole-history HERMES byte-sequence audit. |
| [`hermes_local_audit.py`](hermes_local_audit.py) | Local byte-sequence discipline audit — origin-side coverage without a runner (T-2392). |
| [`ho_resumer.py`](ho_resumer.py) | HO auto-resumer — reads the limit-hit state file and fires claude --resume. |
| [`hook_allowlist_lint.py`](hook_allowlist_lint.py) | Hook-allowlist pairing lint (T-1102). |
| [`host_broker_install.py`](host_broker_install.py) | T-2920 U1: the PURE planning core of the ORG-052 host-broker installer. |
| [`host_broker_install_apply.py`](host_broker_install_apply.py) | T-2920 U3a: apply, verify and rollback for the ORG-052 host-broker install plan, DARK. |
| [`host_broker_rehearsal.py`](host_broker_rehearsal.py) | T-2920 U4a: the nspawn REHEARSAL harness for the ORG-052 host-broker installer, DARK. |
| [`host_broker_rehearsal_live.py`](host_broker_rehearsal_live.py) | T-2920 U4b: the ATTESTED rehearsal live layer (the only actuating code in the arc). |
| [`host_plan_producer.py`](host_plan_producer.py) | Unprivileged plan producer for the ORG-052 host broker (T-5533, design (c)). |
| [`inbox_consumer.py`](inbox_consumer.py) | Pantheon inbox auto-consumer. |
| [`json_safe_edit.py`](json_safe_edit.py) | Concurrency-safe read-modify-write for shared JSON config (T-2329). |
| [`keep_going.py`](keep_going.py) | tools/keep_going.py — periodic anti-idle nudge for a standing agent (T-0772 sibling). |
| [`kick_verify.py`](kick_verify.py) | tools/kick_verify.py — post-kick verification + bounded re-fire (T-0899). |
| [`kino_mute_dropbox.py`](kino_mute_dropbox.py) | kino_mute_dropbox.py — kino's Health-section mute toggle without a privilege edge (T-5397). |
| [`kino_pwa_autodeploy.py`](kino_pwa_autodeploy.py) | kino-pwa auto-redeploy poller (T-0316, the "kino-pull-cron variant"). |
| [`knowledge_sweep.py`](knowledge_sweep.py) | Pantheon scripted knowledge-sweep driver (ORG-012 Phase C, T-0781). |
| [`lease_audit.py`](lease_audit.py) | lease_audit — detective check for repairs taken without the repair lease (T-4884 B4b). |
| [`ledger_placement_lint.py`](ledger_placement_lint.py) | Lint ledger entries for correct ISO-week shard placement (T-2077). |
| [`ledger_shard.py`](ledger_shard.py) | Shared ledger-shard helpers for the weekly-subfolder layout (T-0148). |
| [`links.py`](links.py) | Pantheon typed-link traversal surface — read-only index/query over existing data. |
| [`lint_report_day_index.py`](lint_report_day_index.py) | lint_report_day_index.py — assert watch-report day-index READMEs match disk. |
| [`live_harness.py`](live_harness.py) | Resolve a seat's LIVE harness for pane injection (T-4792). |
| [`mail_staleness.py`](mail_staleness.py) | Per-inbox mail-staleness detection — the PURE half (T-2157). |
| [`mail_staleness_monitor.py`](mail_staleness_monitor.py) | mail_staleness_monitor — the impure shell around `mail_staleness.detect()` (T-2157). |
| [`mailbox.py`](mailbox.py) | tools/mailbox.py — daemonless per-agent file-mailbox (ORG-026, T-0772). |
| [`mailbox_integrity_audit.py`](mailbox_integrity_audit.py) | mailbox_integrity_audit — flag mailbox files that did not come through mailbox.send (T-4958). |
| [`mailbox_mcp.py`](mailbox_mcp.py) | tools/mailbox_mcp.py — MCP stdio server wrapping tools/mailbox.py (T-1112). |
| [`mailbox_onboard.py`](mailbox_onboard.py) | T-2683: convergent, manifest-driven full-lifecycle installer for the ORG-026 |
| [`mailbox_traffic_report.py`](mailbox_traffic_report.py) | mailbox_traffic_report — who sends mail to whom, and how often. |
| [`mailbox_wake.py`](mailbox_wake.py) | tools/mailbox_wake.py — the wake half of the ORG-026 mailbox (T-0772 deployment layer). |
| [`mailbox_writable_roots.py`](mailbox_writable_roots.py) | Resolve manifest-declared mailbox writable roots for harness launch. |
| [`main_guard_lint.py`](main_guard_lint.py) | main_guard_lint — catch top-level definitions that sit AFTER a `__main__` guard. |
| [`manifest_pin.py`](manifest_pin.py) | tools/manifest_pin.py — ORG-046 manifest pin LIFECYCLE (T-3764). |
| [`mcp_allowlist_audit.py`](mcp_allowlist_audit.py) | Secret-free, fail-closed fleet MCP allowlist audit (T-4679). |
| [`mcp_allowlist_registry.py`](mcp_allowlist_registry.py) | Strict parser for Pantheon's single machine-authoritative MCP registry. |
| [`memex_failed_unit_check.py`](memex_failed_unit_check.py) | Carry a failed memex unit on LXC 203 to archivist without anyone having to look. |
| [`memory_envelope.py`](memory_envelope.py) | Typed ORG-058 memory envelope + lifecycle schema and trust policy (T-3197). |
| [`memory_index.py`](memory_index.py) | tools/memory_index.py — keep MEMORY.md's pin inventory drift-free (ORG-035 / T-1130). |
| [`memory_lifecycle_ops.py`](memory_lifecycle_ops.py) | ORG-058 memory correction, supersession, and verified-forgetting operations |
| [`memory_lint.py`](memory_lint.py) | tools/memory_lint.py — validate an agent's auto-memory pins (ORG-035 baseline; T-1129). |
| [`memory_lint_sweep.py`](memory_lint_sweep.py) | memory_lint_sweep.py — T-2888 (ORG-068 memory Health leg): lint EVERY pin store. |
| [`memory_policy_evaluator.py`](memory_policy_evaluator.py) | Deny-by-default ORG-058 memory policy evaluator + lint CLI (T-3197). |
| [`memory_provenance_backfill.py`](memory_provenance_backfill.py) | tools/memory_provenance_backfill.py — add ORG-035 provenance fields to memory pins (T-1131). |
| [`memory_search.py`](memory_search.py) | tools/memory_search.py — rg-backed search over an agent's memory pins (ORG-035 / T-1129). |
| [`memory_snapshot_coverage_monitor.py`](memory_snapshot_coverage_monitor.py) | Coverage monitor for the fleet's memory-snapshot guard (T-2838 / T-1871 rot). |
| [`memory_store.py`](memory_store.py) | Disposable, directory-rooted ORG-058 memory store with generation-scoped, |
| [`memory_store_repair.py`](memory_store_repair.py) | memory_store_repair.py - T-4503 (ORG-058): explicit, offline repair of a |
| [`memory_store_teardown.py`](memory_store_teardown.py) | memory_store_teardown.py — T-2888 (ORG-068 memory Remove leg): archive-then-delete a pin store. |
| [`memory_tier_episodic.py`](memory_tier_episodic.py) | ORG-062 tier-1 EPISODIC (warm) view over the governed memory store (T-2518 step 2d). |
| [`memory_tier_semantic.py`](memory_tier_semantic.py) | ORG-062 tier-2 SEMANTIC / RELATIONAL view over the governed memory store (T-2518 step S4). |
| [`operator_decision_sweep.py`](operator_decision_sweep.py) | operator_decision_sweep.py (T-5015 phase 2) — catch the operator rulings with no register row. |
| [`operator_decision_throughline.py`](operator_decision_throughline.py) | operator_decision_throughline.py (T-5015 phase 3) — weekly trigger for the throughline pass. |
| [`migrate_ledger_to_weekly_shards.py`](migrate_ledger_to_weekly_shards.py) | One-shot migration: flat ledger/2026.jsonl → weekly subfolder shards (T-0148). |
| [`mint_telegram_sticker_set.py`](mint_telegram_sticker_set.py) | Mint Telegram sticker set(s) from assets/stickers/manifest.json (T-1002). |
| [`mirror_drift_sweep.py`](mirror_drift_sweep.py) | Weekly mirror drift-sweep (T-1994). |
| [`mirror_leak_gate.py`](mirror_leak_gate.py) | Fail-closed leak gate for the generated release mirrors (T-4768 PR4). |
| [`mirror_state.py`](mirror_state.py) | Mirror publish-state manager (T-1994). |
| [`mutation_gate.py`](mutation_gate.py) | tools/mutation_gate.py — targeted-mutation gate for Amendment H falsification. |
| [`naming_history.py`](naming_history.py) | Pantheon naming-history registry — canonical old→new resolver. |
| [`neutralize_seed.py`](neutralize_seed.py) | Build the pantheon-public seed from a pantheon-config source tree. |
| [`notify_policy.py`](notify_policy.py) | Per-persona notification mute policy — the ORG-076 §7 "Operator plane" control (T-2978). |
| [`org060_device_presence_simulator.py`](org060_device_presence_simulator.py) | Deterministic, fixture-only ORG-060 device and presence simulator. |
| [`org060_presence_contract.py`](org060_presence_contract.py) | ORG-060 replaceable presence projection and interaction contract (T-3227, Child C). |
| [`org060_sensory_actuation_contract.py`](org060_sensory_actuation_contract.py) | ORG-060 sensory/actuation typed contract (T-3226). |
| [`org076_heartbeat_check.py`](org076_heartbeat_check.py) | ORG-076 BT3b -- external-witness heartbeat READER (RFC-004 acceptance 6.9), T-4709. |
| [`org076_meta.py`](org076_meta.py) | ORG-076 BT3b -- in-band store-liveness meta-detector (RFC-004 sections 1, 2b), T-4709. |
| [`org076_reconciler.py`](org076_reconciler.py) | ORG-076 BT3b -- reconciler sweeps (RFC-004 section 2b / acceptance 6.5), T-4709. |
| [`org076_sample_freshness.py`](org076_sample_freshness.py) | ORG-076 BT3b -- per-seat sample FRESHNESS (recovery steward non-blocking item 1), T-4709. |
| [`org076_store_runner.py`](org076_store_runner.py) | ORG-076 BT3a -- recovery-side single-store runner (T-4720). |
| [`organism_capability_health.py`](organism_capability_health.py) | tools/organism_capability_health.py — ORG-056 Unit E (T-3189). |
| [`organism_topology.py`](organism_topology.py) | Strict ORG-056 organism topology contracts, validation, and generation. |
| [`organism_topology_audit.py`](organism_topology_audit.py) | tools/organism_topology_audit.py — ORG-056 Unit F (T-3190). |
| [`pane_log_tap.py`](pane_log_tap.py) | tools/pane_log_tap.py: the reader behind arm_pane_log.sh's pipe-pane tap (T-1636, T-4460). |
| [`pantheon_config_sync.py`](pantheon_config_sync.py) | pantheon-config bidirectional sync — T-0211 Phase B. |
| [`pantheon_metrics_gen.py`](pantheon_metrics_gen.py) | pantheon_metrics_gen.py -- emit COARSE, surface-level fleet metrics JSON for the |
| [`parity_audit_runner.py`](parity_audit_runner.py) | tools/parity_audit_runner.py — scheduled fleet seat-parity audit under recovery's |
| [`parity_remediate.py`](parity_remediate.py) | tools/parity_remediate.py — Recovery's bounded seat-parity self-repair (T-3798). |
| [`parity_render.py`](parity_render.py) | tools/parity_render.py — ORG-099 seat-parity manifest renderer (T-3747 BT2). |
| [`persona_apply_path.py`](persona_apply_path.py) | Shared account-local environment for persona apply/preflight helpers. |
| [`persona_brief.py`](persona_brief.py) | Resolve manifest-declared persona brief paths. |
| [`persona_bringup.py`](persona_bringup.py) | Harness-neutral persona diagnosis and headless bring-up. |
| [`persona_continuity.py`](persona_continuity.py) | Portable, reference-only persona continuity substrate (T-3176). |
| [`persona_controller_health_dbus.py`](persona_controller_health_dbus.py) | D-Bus health surface for the T-2953 harness controller (T-2969 BT3). |
| [`persona_controller_health_dbus_e2e_driver.py`](persona_controller_health_dbus_e2e_driver.py) | Subprocess driver for tools/test_persona_controller_health_dbus_e2e.py (T-5410). |
| [`persona_controller_health_leg.py`](persona_controller_health_leg.py) | pantheon-health/v1 leg for the PersonaController1 emitter (T-5137, ORG-068 E10). |
| [`persona_cutover_resolve.py`](persona_cutover_resolve.py) | T-2633: persona-generic identity resolver for tools/persona_cutover.sh and |
| [`persona_installer.py`](persona_installer.py) | Manifest-driven, fail-closed Pantheon persona installer. |
| [`persona_installer_activation.py`](persona_installer_activation.py) | Installer activation/verification helpers shared with the host executor (T-5516). |
| [`persona_installer_apply.py`](persona_installer_apply.py) | T-5449: the seat-local ``apply`` / ``rollback`` verbs of the persona installer. |
| [`persona_installer_contracts.py`](persona_installer_contracts.py) | Pure installer contract types (T-5516; ORG-052 §1/§3, T-2652). |
| [`persona_installer_verify_seat.py`](persona_installer_verify_seat.py) | T-5449: ``verify-seat`` -- the read-only post-install check for one seat. |
| [`pgrep_predicate_lint.py`](pgrep_predicate_lint.py) | pgrep_predicate_lint — flag cmdline-substring process predicates in repo scripts. |
| [`policy_outflow_sync.py`](policy_outflow_sync.py) | tools/policy_outflow_sync.py — ORG-012 OUTFLOW: keep briefs pointed at changed policy. |
| [`postprocess_ai_sticker_png.py`](postprocess_ai_sticker_png.py) | Convert built-in image_gen chroma-key PNGs into 512x512 RGBA stickers. |
| [`prepare_operator_sticker_assets.py`](prepare_operator_sticker_assets.py) | Prepare the operator's six static sticker PNGs from the accepted source sheet. |
| [`priority_list_health_watchdog.py`](priority_list_health_watchdog.py) | tools/priority_list_health_watchdog.py — priority-list persistent-broken |
| [`priority_list_parse.py`](priority_list_parse.py) | Parse the sentinel-delimited YAML priority-list region (ORG-078 §2 — |
| [`proactive_swap_init.py`](proactive_swap_init.py) | Initiate an operator-directed harness swap for one persona. |
| [`probe_mailbox_lease_holders.py`](probe_mailbox_lease_holders.py) | — |
| [`project_catalogue.py`](project_catalogue.py) | Strict ORG-054 project-record catalogue and generated index. |
| [`promote_lessons.py`](promote_lessons.py) | Lessons → hooks promotion pipeline — detector (T-0649), increment 2. |
| [`promote_lessons_cron.py`](promote_lessons_cron.py) | Weekly promotion-pipeline runner (T-0649 inc 4). |
| [`provision_context_fill_monitor_dropins.py`](provision_context_fill_monitor_dropins.py) | Provision + verify the context-fill-monitor per-agent enablement drop-ins (T-1214). |
| [`provision_repos.py`](provision_repos.py) | provision_repos.py — T-0761 component 4: clone-on-demand repo provisioning. |
| [`pve2_backup_freshness.py`](pve2_backup_freshness.py) | pve2_backup_freshness.py — alert when pve2's or boxon's nightly backups go stale or fail (T-4998, T-5301). |
| [`pve2_heat_monitor.py`](pve2_heat_monitor.py) | pve2_heat_monitor.py — alert when pve2's CPU or GPU runs hot for a sustained stretch (T-5309). |
| [`pwa_mirror_scrub.py`](pwa_mirror_scrub.py) | T-3316: the pwa-release-mirror option-2 scrub (the operator's T-2131 ruling). |
| [`quant_calls_collect.py`](quant_calls_collect.py) | ORG-102 calls collector (step 2). |
| [`quant_delivery_collect.py`](quant_delivery_collect.py) | ORG-102 delivery collector (T-4780 step 1). |
| [`quant_h2_sampler.py`](quant_h2_sampler.py) | ORG-102 H2 weekly blind sampler and reply parser. |
| [`quant_outcomes_collect.py`](quant_outcomes_collect.py) | ORG-102 outcome detector and H1 reporter (step 3). |
| [`quant_weekly_digest.py`](quant_weekly_digest.py) | ORG-102 weekly digest builder (step 5). |
| [`quota_probe_detector.py`](quota_probe_detector.py) | ORG-076 BT2 -- claude quota-probe READABILITY detector (T-4708). |
| [`read_failover.py`](read_failover.py) | tools/read_failover.py — T-1998 phase 2: ORG-046 READ failover for the pull path. |
| [`reboot_safety.py`](reboot_safety.py) | Read-only PRE-reboot safety check: is the fleet's work all committed + pushed? |
| [`recovery_watcher.py`](recovery_watcher.py) | recovery_watcher — deterministic incident classifier over the fleet health export (T-4884 B3). |
| [`remote_resolve.py`](remote_resolve.py) | tools/remote_resolve.py — T-1996: manifest-resolved git remotes (ORG-046). |
| [`repair_evidence.py`](repair_evidence.py) | Evidence redaction + repair route-guard for ORG-076 runtime repair tickets (T-4707 / BT1). |
| [`repo_reconciler.py`](repo_reconciler.py) | tools/repo_reconciler.py — T-2951: the declarative repository-lifecycle reconciler. |
| [`repo_registry.py`](repo_registry.py) | repo_registry.py — shared repos.yaml loader (T-1136). |
| [`repository_evidence.py`](repository_evidence.py) | Repository evidence contract validation for ORG-083. |
| [`repository_extraction.py`](repository_extraction.py) | tools/repository_extraction.py — T-3183 §3: the repository-extraction executor. |
| [`resource_sampler.py`](resource_sampler.py) | resource_sampler — per-process memory attribution, so the NEXT spike is diagnosable (T-2213). |
| [`restart_drain.py`](restart_drain.py) | Pantheon restart drain — process tombstone files and fire graceful restarts. |
| [`restart_request.py`](restart_request.py) | Pantheon restart request — operator-facing CLI to trigger a graceful agent restart. |
| [`retro_metrics.py`](retro_metrics.py) | retro_metrics.py — one-time best-effort retro-extraction of the cycle-stats |
| [`runtime_controller.py`](runtime_controller.py) | Deterministic Pantheon runtime controller for persistent persona harnesses. |
| [`runtime_invocation_receipt.py`](runtime_invocation_receipt.py) | Pure validator for the initial-viability invocation receipt (T-5572). |
| [`s3star_audit_runner.py`](s3star_audit_runner.py) | tools/s3star_audit_runner.py — HO-side scheduler for the ORG-038 System-3* |
| [`s3star_seat_evidence.py`](s3star_seat_evidence.py) | s3star_seat_evidence.py — Recovery-captured per-seat evidence for the S3* auditor (T-4928). |
| [`scheduler_reconcile.py`](scheduler_reconcile.py) | tools/scheduler_reconcile.py — ORG-064 General Scheduler, phase 1: render the |
| [`scheduler_spec.py`](scheduler_spec.py) | tools/scheduler_spec.py — ORG-064 General Scheduler, phase 1: the `schedules/` |
| [`scrub_chronicle_companions.py`](scrub_chronicle_companions.py) | Retroactively scrub credentials from existing Chronicle companion archives (T-1683). |
| [`seat_context_bundle.py`](seat_context_bundle.py) | tools/seat_context_bundle.py — seed a SELECTED set of HO memory pins into a seat's |
| [`seat_exec.py`](seat_exec.py) | seat_exec — the one own-account-safe way to build a per-seat cross-account argv. |
| [`seat_launch_config.py`](seat_launch_config.py) | seat_launch_config.py — the model/effort a seat's role DECLARES in instance.yaml (T-4085). |
| [`seat_sleep.py`](seat_sleep.py) | tools/seat_sleep.py - ORG-106 L3a (T-5526): the launch-side enforcement of DEEP-SLEEP. |
| [`seat_sleep_drain.py`](seat_sleep_drain.py) | tools/seat_sleep_drain.py - ORG-106 L3b (T-5526): the DEEP-SLEEP drain. |
| [`seat_state.py`](seat_state.py) | tools/seat_state.py - ORG-106 L0 (T-5523): seat-state record schema + read-only reader. |
| [`seat_state_janitor.py`](seat_state_janitor.py) | tools/seat_state_janitor.py - ORG-106 L5 (T-5529): seat-state expiry lifecycle. |
| [`seat_state_testkit.py`](seat_state_testkit.py) | Test helper (ORG-106 L4b, T-5527): build EffectiveSeatState values for monitor tests. |
| [`seed_genericize.py`](seed_genericize.py) | seed_genericize.py — T-1327 (recurring weekly). |
| [`seed_rebrand.py`](seed_rebrand.py) | seed_rebrand.py — official Pantheon seed rebrand tool (T-2072). |
| [`self_sudo_guard_lint.py`](self_sudo_guard_lint.py) | self_sudo_guard_lint — flag per-seat `sudo -u <var>` constructions that lack an |
| [`send_sticker.py`](send_sticker.py) | Send a persona sticker via the Telegram Bot API (T-3319 Part B ceremony-reduction). |
| [`session_chronicle.py`](session_chronicle.py) | tools/session_chronicle.py: the session-end Chronicle duty (T-5016). |
| [`session_reset_log.py`](session_reset_log.py) | session_reset_log — structured event log for session-reset class events. |
| [`session_reset_reconcile.py`](session_reset_reconcile.py) | session_reset_reconcile — retired one-shot utility for turning session-reset |
| [`soak_filer.py`](soak_filer.py) | Pantheon suite-health soak filer (T-0991 option b / T-0992). |
| [`spine_groom_cross_repo.py`](spine_groom_cross_repo.py) | tools/spine_groom_cross_repo.py — extend spine-index grooming to Pantheon-adjacent repos (T-0666). |
| [`spine_index_groom.py`](spine_index_groom.py) | Pantheon spine-index groomer — idempotent sentinel-region updater for all spine READMEs. |
| [`staged_content_gate.py`](staged_content_gate.py) | tools/staged_content_gate.py — fail at the WRITER, not in the next nightly (T-4402). |
| [`stale_worktree_sweeper.py`](stale_worktree_sweeper.py) | Stale git-worktree sweeper (T-1324 part b / 2026-06-25 VPS disk-full). |
| [`sticker_mcp.py`](sticker_mcp.py) | tools/sticker_mcp.py — MCP stdio server: send a Pantheon persona sticker (T-1024). |
| [`submit_verify.py`](submit_verify.py) | tools/submit_verify.py — harness-aware tmux submit-verify (T-1048). |
| [`subscription_usability.py`](subscription_usability.py) | Live subscription-usability probing and fallback selection (T-4862). |
| [`supernote_selector_check.py`](supernote_selector_check.py) | Fail loudly when the SuperNote ingest selector reports stale pages it cannot refresh. |
| [`systemd_drift_lint.py`](systemd_drift_lint.py) | systemd_drift_lint — repo unit definitions vs the copies systemd actually loads. |
| [`systemd_drift_sweep.py`](systemd_drift_sweep.py) | tools/systemd_drift_sweep.py — cross-account systemd unit-drift, from ONE seat (T-2444). |
| [`t2381_live_acceptance.py`](t2381_live_acceptance.py) | Disposable real-user-manager acceptance helper for T-2381. |
| [`t2382_live_acceptance.py`](t2382_live_acceptance.py) | Tamper-evident live acceptance recorder for T-2382 tests 55-59. |
| [`t2582_live_telegram_accept.py`](t2582_live_telegram_accept.py) | tools/t2582_live_telegram_accept.py — gated live Telegram send-accept leg (T-2582). |
| [`t2598_live_codex_login_check.py`](t2598_live_codex_login_check.py) | tools/t2598_live_codex_login_check.py — gated live codex login-screen |
| [`t2611_recovery_persona_apply.py`](t2611_recovery_persona_apply.py) | T-2611 driver: install-done precondition + real persona_installer apply |
| [`t2693_config_roots.py`](t2693_config_roots.py) | T-2693: give each old-hub path in a per-account config a new-hub twin, fail-closed. |
| [`t5740_readiness_mutation_gate.py`](t5740_readiness_mutation_gate.py) | Exact selectable Amendment-J mutations for T-5740 readiness. |
| [`tailnet_self.py`](tailnet_self.py) | Is THIS host on the tailnet? One answer for every check that reaches a host over 100.x (T-5385). |
| [`telegram_inbound.py`](telegram_inbound.py) | telegram_inbound.py (T-4205) — when did the operator last message this account's agent? |
| [`tempfile_hygiene_lint.py`](tempfile_hygiene_lint.py) | tempfile_hygiene_lint — catch test temp-DIR creations with no registered cleanup. |
| [`ticket.py`](ticket.py) | Pantheon ticket lifecycle CLI. |
| [`ticket_grooming.py`](ticket_grooming.py) | Pantheon ticket-grooming staleness scanner. |
| [`ticket_lineage.py`](ticket_lineage.py) | Read-only lineage-graph queries over the ticket corpus (T-3332 part (a)). |
| [`ticket_lint.py`](ticket_lint.py) | Pantheon ticket-lint — hourly frontmatter format scanner and auto-fixer. |
| [`ticket_purpose.py`](ticket_purpose.py) | Typed ORG-054 purpose links for Pantheon tickets. |
| [`ticket_registry_gate.py`](ticket_registry_gate.py) | T-5496: refuse a ticket close that would turn `registry validate` red. |
| [`ticket_state_sweep.py`](ticket_state_sweep.py) | Pantheon ticket-state ↔ ledger consistency sweep. |
| [`tool_schema_budget.py`](tool_schema_budget.py) | Measure loaded MCP/tool-definition budget by Pantheon worker profile. |
| [`tui_wedge_sampler.py`](tui_wedge_sampler.py) | Measure how long a HEALTHY agent can look wedged (T-1369, last branch). |
| [`unit_failure_alert.py`](unit_failure_alert.py) | OnFailure handler: route a systemd unit failure into Recovery-triage (T-1868). |
| [`unknown_dialog_monitor.py`](unknown_dialog_monitor.py) | unknown_dialog_monitor.py (T-2553) — DETECT-ONLY backstop for a claude seat parked on an |
| [`urgent_escalation.py`](urgent_escalation.py) | tools/urgent_escalation.py — the escalation-ladder leg of severity=urgent |
| [`usage_monitor.py`](usage_monitor.py) | Pantheon — usage monitor. |
| [`user_systemd.py`](user_systemd.py) | Neutral typed port to a seat's user service manager (T-2917 5c.1, ORG-052 Unit 5). |
| [`vault_link_check.py`](vault_link_check.py) | Vault link check — verify every `[[wikilink]]` in the repo resolves for Obsidian (T-1597). |
| [`verify_hub_cut.py`](verify_hub_cut.py) | Post-cut gate for the T-2156 hub rename: the OLD path must not survive as a |
| [`verify_relocation.py`](verify_relocation.py) | Check that an ADR reshape lost no line (T-4730 / T-4745). |
| [`verify_seed.py`](verify_seed.py) | verify_seed.py — T-1678, independent post-build seed verifier. |
| [`recovery_continuity_bundle.py`](recovery_continuity_bundle.py) | Author Recovery's canonical continuity bundle + capture a verified checkpoint (T-3178 / ORG-057:R1, gate G3). |
| [`recovery_continuity_switch.py`](recovery_continuity_switch.py) | Operator entrypoint for Recovery's ORG-057:R1 continuity switch (T-3178 gate G2). |
| [`watch_digest.py`](watch_digest.py) | Pantheon watch-loop digest. |
| [`watch_pipeline_decommission.py`](watch_pipeline_decommission.py) | T-2887: scripted, default-safe decommission of the watch/research pipeline |
| [`watch_premature_termination.py`](watch_premature_termination.py) | Sample watch reports for uncertain negatives that may hide early stopping. |
| [`watch_runner.py`](watch_runner.py) | Pantheon watch-loop driver. |
| [`weekly_ticket_summary.py`](weekly_ticket_summary.py) | Pantheon weekly ticket-summary generator. |
| [`welltrod_alltime.py`](welltrod_alltime.py) | Well-trod all-time tally, trend and history estimate (T-4490, under T-1811). |
| [`welltrod_stats.py`](welltrod_stats.py) | Well-trod pathing stats — recurring fleet doc-traversal snapshot (T-1811). |
| [`window_executor.py`](window_executor.py) | Pantheon maintenance-window executor — the D13 queue-drainer. |
| [`worker_guard_cutover_smoke.py`](worker_guard_cutover_smoke.py) | worker_guard_cutover_smoke: prove the T-4177 worker-guard shim is decision-identical, then time it. |
| [`worker_guard_v2_replay.py`](worker_guard_v2_replay.py) | Replay validation for worker-guard v2 (T-5686). A tool you run, not a CI test. |
<!-- AUTOGEN-END spine-index -->


## Running this suite (seed note)

Either runner works from the repo root:

```
python3 -m unittest discover tools     # ~1.5 min
pytest tools -q
```

**Expect ~28 failures on a freshly built seed.** They are triaged and expected —
artifacts of neutralization, not defects. The rationale is in
`bugs/seed-own-suite-expected-failures.md`; the machine-readable list is
`bugs/seed-own-suite-expected-failures.manifest.json`.

**Only `pytest` applies that manifest** and renders those cases as skips: the
consumer is a root `conftest.py`, and `unittest` never reads `conftest.py`. Under
`unittest discover` you simply see the ~28 raw. Neither view is wrong — the
pytest one is the curated one.

Failures **outside** the modules named in that manifest are real regressions.

Two timing notes, because both have been misread as hangs: the full suite takes
~1.5 min, and some single modules take 20-40s on their own. A probe with a short
`timeout` will SIGTERM mid-run and look exactly like an indefinite hang.

## Seed-build tooling is not shipped (seed note)

The Index above is the upstream table, and five of its rows —
`neutralize_seed.py`, `seed_genericize.py`, `verify_seed.py` and the two
matching `test_*.py` — have **no file here**. That is deliberate, not a broken
build: those are the tools that produce this seed *from* the upstream repo. They
cannot be neutralized (their substitution tables hold the very tokens they
rewrite, so a self-pass corrupts them), and shipping them un-neutralized would
carry operator identity into this tree. A seed is not rebuilt from itself, so
nothing here needs them. Their absence also means the four
`TestSeedScrubCoversTheAccountClass` cases in `test_account_persona_map.py`
skip, rather than fail, in this tree.
