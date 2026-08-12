# Pantheon — systemd unit templates

These are templates for the systemd-user-timer that fires the watch-loop driver and end-of-day digest. They live in the repo as **canonical templates**; the actual installed units live at `~/.config/systemd/user/` on each machine and are not version-controlled.

**Convention amendment (2026-05-09 per T-0102 ratification)**: Telegram bot units were originally per-host / not version-controlled (Orchestrator's + Recovery's both live ONLY at `~/.config/systemd/user/` on their respective accounts; no canonical template here). With T-0102 Phase 1 landing, `claude-telegram-research.service` (Specialist's interactive bot unit) IS version-controlled in this directory as a canonical template. Future-normalization-ticket tracked separately if Orchestrator's + Recovery's units should be lifted to canonical templates too; for now, asymmetry stands (research-side templated, prime + fallback per-host).

## Files

### Harness-neutral persona mailbox runner (T-2372)
- [`pantheon-persona-consume@.service`](pantheon-persona-consume@.service) — invokes the harness-neutral runtime controller through `tools/persona_bringup.py`. The manifest resolves persona/role plus harness, model, native permissions, quota source, and stable tmux target. Interactive profiles ensure the persistent TUI and wake the in-session persona without claiming or acknowledging its mail; only an explicitly selected batch profile performs the leased drain itself.
- [`pantheon-persona-consume@.timer`](pantheon-persona-consume@.timer) — two-minute runtime/wake fallback with jitter. Empty mailboxes spend no model turn. Repeated unchanged local launch/config failures back off in persona state (2m → 5m → 15m → 1h → 4h → 6h).

`tools/persona_installer.py` installs these templates from the additive
`persona_installer` manifest contract. It writes them disabled, runs the direct
and service first-fire gates, verifies both mailbox directions, and only then
enables the timer. A failed observation disables the installer-owned timer
before rolling back fingerprint-matched files.

### Watch loop (runs on `claude-research`, the specialist user)
- [`pantheon-watch.service`](pantheon-watch.service) — runs the driver script once.
- [`pantheon-watch.timer`](pantheon-watch.timer) — fires the service on schedule (08:00 UTC daily).
- [`pantheon-watch-digest.service`](pantheon-watch-digest.service) — runs the digest once.
- [`pantheon-watch-digest.timer`](pantheon-watch-digest.timer) — fires the digest at end of day.

### Knowledge sweep (runs on `claude-research`, the specialist user) — ORG-012 Phase C, T-0781
- [`pantheon-knowledge-sweep.service`](pantheon-knowledge-sweep.service) — runs `tools/knowledge_sweep.py --file-sweep-ticket` once. Due-detector: content-fingerprints the corpus (`lessons/ bugs/ incidents/ notes/ sop/ rulebook/ adr/ knowledge/` + accessible `feedback_*.md` pins); if it changed since the last *completed* sweep (and no sweep dispatch is already pending), files ONE `target=specialist` sweep-dispatch ticket carrying the team-SOP review recipe. Fires NO subagent itself — the in-session review fan-out (team-pattern per `team-orchestrator-dispatch-checklist.md`) + VERIFY-against-repo + SOP-shaped filing (`throughline-sweep-result-filing.md`) happen when Specialist actions the ticket; `--land-sweep` then records the new baseline. Default-safe (T-0261); idempotent. Quiet days = no-op.
- [`pantheon-knowledge-sweep.timer`](pantheon-knowledge-sweep.timer) — fires the service daily at 13:00 UTC, offset from `pantheon-spine-index-groom.timer` (12:30) and `pantheon-watch-digest.timer` (18:00) to avoid push contention.

### Ticket grooming (runs on `claude-fallback`, the Recovery user)
- [`pantheon-grooming.service`](pantheon-grooming.service) — runs the staleness scanner once. Per fire: scans `tickets/active/` (only; `tickets/blocked/` retired per ORG-019 2026-05-19), flags stale tickets idempotently in their transition logs, auto-commits via the shared `_git_utils` discipline, and sends a plain-text Telegram digest as proof-of-life.
- [`pantheon-grooming.timer`](pantheon-grooming.timer) — fires the service daily at 12:00 UTC (interleaved with the watch loop's 08:00 UTC fire to avoid dispatch contention).

### Unit-failure alert (installed on `claude-fallback` today; generic template) — T-1868
- [`pantheon-unit-failure-alert@.service`](pantheon-unit-failure-alert@.service) — OnFailure leaf target: instance `%i` = the FAILED unit's full name. Runs `tools/unit_failure_alert.py --unit %i` under `sg pantheon-mailbox` (T-1015 hub-perms lesson): per-unit 6h-cooldown throttle (state `~/.local/state/pantheon/unit_failure_alert_state.json`), journal-tail capture, then a message into Recovery's ORG-026 mailbox (picked up by the ≤5-min `pantheon-mailbox-wake@recovery.timer` sweep — Recovery triages per the msg-2235 failure-routing directive); direct-operator Telegram ONLY as fallback when the mailbox path itself fails. Deliberately no `OnFailure=`/`RemainAfterExit=` on this unit (no recursion, no dropped back-to-back alerts). Referring units add `OnFailure=pantheon-unit-failure-alert@%n.service` to `[Unit]`. **As of T-1928 (2026-07-21) every oneshot template in this directory carries the guard in the REPO copy** — new installs inherit it; no hand-wiring of installed copies (the T-1869 account-local mistake: 11 claude-account units were hand-wired while only one repo template was, so repo-installing accounts inherited silent liveness units). Exclusions, per the T-1869 principle: long-running `Restart=` services (`pantheon-bridge`, `pantheon-acp-bridge@`, `pantheon-ancestor-daemon`, `claude-telegram-research`) — systemd already restarts them, and a restart loop would spam alerts; and this alert template itself (no self-fire/recursion). Wiring history: 2026-07-20 the eleven Recovery-account cron units (T-1868); 2026-07-21 the remaining 23 oneshot templates repo-side + per-account installed-copy reconcile (T-1928). Closes the T-1867 silent-cron-failure gap class.

### Mirror drift-sweep (runs on `claude-fallback`, the Recovery user) — T-1994
- [`pantheon-resource-sampler.service`](pantheon-resource-sampler.service) — runs `tools/resource_sampler.py` once (T-2213). Per-process memory ATTRIBUTION, so the next spike is diagnosable. The 2026-07-28 thrash spiral was reconstructable from sysstat only as far as *what* happened (mem 39→95%, swap 48→100%, %system 8→95% while %user collapsed); the single most useful fact — **which process took ~3GB between 15:20 and 15:30** — was unrecoverable, because sar has no per-process dimension, the kernel journal is root/adm-only, and `/home/ci-runner` is outside recovery's sudo. Records RSS **and swap** per process (RSS alone would have shown the fleet looking unremarkable while swap filled underneath it), top-12 by RSS+swap, ≥20MB. Append-only JSONL at `~/.local/state/pantheon/resource_samples.jsonl`, self-rotating **oldest-first** at 20k lines — during an incident the recent samples ARE the evidence, so trimming those to stay small would defeat the tool. **Deliberately not a monitor:** no alerts, no thresholds, no `OnFailure=` — routing its failures into the mailbox would put an alerting dependency on the one thing that must keep writing while the box is in trouble. Every error path is swallowed and it exits 0, because a sampler that crashes during a memory event destroys the evidence of that event. `Nice=10` + `IOSchedulingClass=idle`.
- [`pantheon-resource-sampler.timer`](pantheon-resource-sampler.timer) — every 2 min (`OnUnitActiveSec=2min`). Cadence is derived, not chosen: the 2026-07-28 climb went 38% → 95% *between two 10-minute sar samples*, so the whole attributable window was invisible at that rate. 2 min puts ~5 samples inside a climb of that shape. **Not** `Persistent=true` — a replayed sample taken at the wrong time is worse than a gap, since it would be read as evidence of the state at its firing slot.
- [`pantheon-mail-staleness.service`](pantheon-mail-staleness.service) — runs `tools/mail_staleness_monitor.py` once (T-2157/T-2200). Per-inbox liveness for the ORG-026 hub, because **a mail-layer failure presents as SILENCE** — specialist's ACP bridge was dead from 14:35 to 16:20 on 2026-07-28 and looked exactly like a quiet inbox. NOT a templated `@`-unit despite the ticket's shorthand: the monitor scans every inbox in one pass (`discover_inboxes` by maildir shape), so templating would run the same full scan N times and multiply the alerts. Runs under `sg pantheon-mailbox` for the same reason mailbox-wake does (hub is 2770 group pantheon-mailbox; `SupplementaryGroups=` fails 216 on `--user`). Two tiers: WARN at 3 samples, COARSE at 12, with coarse alerts relabelled so an UNREADABLE pane is never reported as "observed idle". **Drains are suppressed, stalls are not** (T-2200): the WARN tier gates on the MAIN pane being idle, which is the wrong liveness signal for ACP-backed agents whose consumer is the bridge — so an alert is dropped when the `cur/` DIRECTORY mtime is newer than the queue head, proving the consumer is alive and just slow. Validated against both of that day's real cases (mnemosyne healthy-but-slow → suppressed; specialist genuinely stalled → alerts). Fails LOUD: unknown recency never suppresses. OnFailure→Recovery mailbox, load-bearing here because a silent failure of THIS detector reproduces the defect it exists to catch.
- [`pantheon-mail-staleness.timer`](pantheon-mail-staleness.timer) — fires the service every 5 min (`OnUnitActiveSec=5min`). **The interval is not a free parameter**: the monitor counts SAMPLES, so `WARN_SAMPLES=3`/`COARSE_SAMPLES=12` mean 15/60 min ONLY while the fire interval equals `SAMPLE_INTERVAL_SECONDS=300`. Change one and both thresholds silently become something else while the alert text still says "15 min". Deliberately **not** `Persistent=true` (unlike mirror-verify): a missed window is a gap in a sample SEQUENCE, not a missed job, and replaying one on boot would advance the consecutive-sample counters using a sample taken at the wrong time.
- [`pantheon-forgejo-mirror-verify.service`](pantheon-forgejo-mirror-verify.service) — runs `tools/forgejo_mirror_verify.py --quiet-ok` once (T-2217). Proves the `github-mirror` org's Forgejo pull-mirrors actually HOLD what they claim, because creating a mirror is a proxy: an empty mirror and a quiet healthy one are identical in a repo list. **Two tiers.** SYNC-LIVENESS reads the forge's `mirror_updated` (when the pull *actually ran*, not when it was configured) against a threshold DERIVED from each repo's own `mirror_interval` — needs no GitHub credential, so it still covers the private mirrors recovery cannot `ls-remote`. REF-PARITY compares the FULL ref set (not HEAD — a mirror can share a HEAD and be missing every tag) whenever the upstream is readable. Verdicts: OK / DRIFT / STALE / HOLLOW / UNKNOWN; exit 1 on any of the middle three, which trips OnFailure→Recovery mailbox. **Three false-alarm cases handled deliberately**, because a verifier that cries wolf gets switched off: an empty upstream mirrored as empty is a PASS (`data-mapping` has zero refs on GitHub and my first verifier called it a failure); an unreadable upstream is UNKNOWN not DRIFT (else 40 false alarms the moment the private mirrors land); the staleness threshold is derived, never hardcoded. Supervised first-fire 2026-07-28: 27 mirrors, OK=27, 15s. OnFailure path tested by forcing a non-zero exit — alert confirmed delivered to the recovery mailbox.
- [`pantheon-forgejo-mirror-verify.timer`](pantheon-forgejo-mirror-verify.timer) — fires the service daily (`OnCalendar=*-*-* 21:00:00 UTC`, `Persistent=true`). 21:00 lands a couple of hours AFTER the mirrors' own ~18:50 UTC 24h pull (verifying just before a sync reports yesterday's state as today's) and clear of the 12:00/12:30/13:00 Recovery digest cluster. `Persistent=true` matters: the box went down mid-day 2026-07-28 and a non-persistent daily would have skipped that day silently.
- [`pantheon-mirror-drift.service`](pantheon-mirror-drift.service) — runs `tools/mirror_drift_sweep.py` once. Per canonical source→mirror pair (`mirror-drift-pairs.yaml`), reads the current SOURCE head (recovery can read all four sources) and compares it to the recorded anchor (`source_head_at_last_publish` in `mirror-published-state.yaml`, written by `tools/mirror_state.py` at build/publish time). MIRROR-READ-FREE by design: recovery can't read 3 of the 4 mirrors and the genericized mirrors have disjoint (bundle-published orphan) history, so `rev-list mirror..source` is meaningless — drift is measured against the source anchor. Classifies each pair (drift / built-but-unpushed / coverage-gap / gated / no-drift / read-error), idempotently auto-files a refresh ticket routed to the pair's `refresh_target` (recovery — the builder is first actor, ORG-039) on real drift, and sends a proof-of-life Telegram digest every fire. **ARMED 2026-07-23** (T-1994): ownership map blessed by the operator + supervised first-run verified, so `ExecStart` carries `--commit-prod` — the sweep auto-files refresh tickets on real drift (the proof-of-life digest sends every fire regardless). Revert to dark by dropping `--commit-prod`; disarm entirely via `systemctl --user disable --now pantheon-mirror-drift.timer`. Publish leg is orchestrator for all four pairs (T-1673 push-is-HO-exclusive; recovery has no mirror push path). Gated pairs (`pantheon-public-mirror`, ratify-gated on its initial publish T-1963/T-1970/T-1989) are detect-but-never-file. Default-safe (T-0261); OnFailure→Recovery mailbox (T-1928).
- [`pantheon-mirror-drift.timer`](pantheon-mirror-drift.timer) — fires the service weekly (`OnCalendar=Mon *-*-* 13:00:00 UTC`, `Persistent=true`), offset an hour past the daily 12:00 UTC grooming so the two Recovery digests don't collide.

### Suite-health soak filer (runs on `claude-fallback`, the Recovery user)
- [`pantheon-soak-filer.service`](pantheon-soak-filer.service) — runs `tools/soak_filer.py --file-soak-tickets` once. Files ONE per-account suite-health soak ticket to specialist/orchestrator/recovery (T-0991 option b / T-0992), idempotently (skips any account whose prior leg is still unresolved — scans `tickets/inbox/` + `active/`, matched on the `suite-health-soak-auto` label). Each receiving agent runs its own leg in-session with the guarded invocation `PANTHEON_DISABLE_LIVE_TELEGRAM=1 python3 -m unittest discover -s tools -t .` and resolves with the green count. Central trigger (recovery only); decentralized execution removes the cross-account-sudo dependency. Fires NO suite itself. Default-safe (T-0261).
- [`pantheon-soak-filer.timer`](pantheon-soak-filer.timer) — fires the service monthly on the 1st at 06:00 UTC (`Persistent=true` so a missed fire while the VPS was down runs on next boot).

### Well-trod pathing stats (runs on `claude-fallback`, the Recovery user) — T-1811
- [`pantheon-welltrod.service`](pantheon-welltrod.service) — runs `tools/welltrod_stats.py --collect --commit-prod` once. Aggregates `doc_traversal_report.py` per-session analyses across all three agents over a rolling 14-day window (cap 60 sessions/agent; orchestrator/specialist transcripts via Recovery's NOPASSWD sudo — collection MUST run on the Recovery account, the kino service user has no cross-account grants) into per-doc distinct-session "well-trod" scores + boot-adherence + coverage, then writes `knowledge/stats/welltrod/latest.json` (+ dated `history/`, pruned past 180d) with a scope-guarded commit + push. The kino stats panel serves the committed snapshot read-only (`--format json` behind the pull-gate). Default-safe (T-0261): no mode flag → exit 64.
- [`pantheon-welltrod.timer`](pantheon-welltrod.timer) — fires the service daily at 09:20 UTC (offset from watch 08:00, grooming 12:00, policy-outflow 12:45 to avoid push contention).

### Channel-liveness monitor (runs on `claude-fallback`, the Recovery user)
- [`pantheon-channel-liveness.service`](pantheon-channel-liveness.service) — runs `tools/channel_liveness_monitor.py --all` once. Probes every configured agent's INBOUND Telegram poller (`bot.pid` → live `bun server.ts`; the single-poller-409 death signature is bot.pid present but its PID dead) — distinct from session-liveness, which reads green while an agent is comms-dark (T-0893/T-0894 gap). Alert-only via `route_failure_alert` (others→Recovery, recovery-self→the operator), 30-min per-agent cooldown. Cross-account reads via Recovery's sudo. Auto-remediation + the `mcp list` guard + restart-path hardening are separate gated prongs (T-0894 children).
- [`pantheon-channel-liveness.timer`](pantheon-channel-liveness.timer) — fires the service every 5 minutes (`OnCalendar=*-*-* *:0/5:00`).
- [`pantheon-ancestor-liveness.service`](pantheon-ancestor-liveness.service) — runs `tools/ancestor_liveness_monitor.py` once (T-1134). Detects an alive-but-WEDGED the-ancestor daemon (process up via `Restart=always`, but its `ancestor-daemon` tmux session stuck) — the T-0510-class gap that `Restart=always` can't see. Read-only pane probe (Recovery → sudo): ALIVE iff the live bottom shows the idle (`? for shortcuts`) or mid-turn (`esc to interrupt`) sentinel. DETECT+ALERT live (a confirmed wedge — N=3 consecutive not-alive — routes via `route_failure_alert` → Recovery; the-ancestor is channel-less); auto-RESTART (kill-session → `Restart=always` respawns) is ship-dark behind `PANTHEON_ANCESTOR_REMEDIATE_ENABLED` (default off) pending a supervised first-fire. Cooldown + consecutive-N guard against loops/transients.
- [`pantheon-ancestor-liveness.timer`](pantheon-ancestor-liveness.timer) — fires the service every 10 minutes (`OnCalendar=*-*-* *:0/10:00`); lighter cadence than channel-liveness since the-ancestor is thin/idle.
- [`pantheon-credential-expiry.service`](pantheon-credential-expiry.service) — runs `tools/credential_expiry_monitor.py --all` once. Pure file-read of every agent's `~/.claude/.credentials.json` (no LLM/API) — so it keeps working during a full auth outage and alerts the operator **directly** via the Telegram bot token (independent of Claude OAuth). Flags `NO_REFRESH` (empty/missing refresh token — the exact 2026-06-20→22 Specialist failure: cannot auto-renew → hard 401 at access-token expiry) and `MALFORMED` creds; access-token near/post expiry **with** a valid refresh token is normal auto-renew and is NOT alerted (no nightly false alarms). Per-`(agent,class)` 6h alert cooldown. The complementary present-but-revoked-refresh / actively-401ing case (not file-readable) is covered by the auth-outage detector inside `context_fill_monitor` (`_maybe_fire_auth_outage`, T-1223 — sustained recent 401 run in the session JSONL → direct-operator alert, never restart; ships ON). Re-login runbook: `sop/remediation/agent-oauth-relogin.md`.
- [`pantheon-credential-expiry.timer`](pantheon-credential-expiry.timer) — fires the service hourly (`OnCalendar=*-*-* *:35:00`, `Persistent=true`).
- [`pantheon-disk-usage.service`](pantheon-disk-usage.service) — runs `tools/disk_usage_monitor.py --check` once. Pure `df` read of the root FS (no LLM/API) — alerts the operator **directly** via the Telegram bot token when used% crosses `WARN` (85) / `URGENT` (92), throttled per class (6h cooldown) so the hourly timer nags without spamming. On breach it appends a best-effort per-home size breakdown (cross-account `du` via sudo) so the alert is actionable. Built 2026-06-26 after build/venv caches (a 51G Rust `target/`, a 5.2G `.venv`) pushed the FS to 82% with nothing watching it — a full root FS wedges every agent (can't write session logs/handoffs/commits). Thresholds + watch path are env-overridable (`PANTHEON_DISK_WARN_PCT`, `PANTHEON_DISK_URGENT_PCT`, `PANTHEON_DISK_WATCH_PATH`).
- [`pantheon-disk-usage.timer`](pantheon-disk-usage.timer) — fires the service hourly (`OnCalendar=*-*-* *:50:00`, `Persistent=true`; offset from the `:35` credential timer to spread load).
- [`pantheon-memory-snapshot-coverage.service`](pantheon-memory-snapshot-coverage.service) — runs `tools/memory_snapshot_coverage_monitor.py --all` once. The COVERAGE guard over the T-2424 memory-snapshot timers: a one-time install does not prevent a snapshot timer that later silently STOPS, which is exactly the T-1871 rot (mnemosyne's snapshots quietly ceased ~2026-07-29 and nobody noticed for ~12 days — a stopped snapshot reads as healthy to every process check). Per covered seat (recovery/orchestrator/specialist/atlas/metis via `pantheon-agent-state-snapshot.timer`; mnemosyne via `mnemosyne-state-snapshot.timer`) it keys on snapshot-TIMER health — active+enabled with a near-future fire — NOT on last-commit age. **Why not commit age:** the snapshot scripts commit only when the memory dir changed, so a healthy timer on an idle seat legitimately produces no commit and its HEAD ages; live-proven 2026-08-11 when mnemosyne's HEAD was 13 days old while its hourly timer fired perfectly. Commit age is kept as corroboration + the fallback signal for a seat whose systemd can't be introspected (fail-open). Cross-account systemd + git reads via Recovery's sudo grants. A stall routes to the **Recovery-triage** channel (`_send_alert_via_recovery`), not the recovery-self→the operator carve-out — a stalled snapshot timer is always Recovery-fixable (`systemctl --user restart` the timer). Per-`(seat,class)` 12h cooldown; always exits 0. T-2838 (follow-up to T-2736/T-2424).
- [`pantheon-memory-snapshot-coverage.timer`](pantheon-memory-snapshot-coverage.timer) — fires the service daily (`OnCalendar=*-*-* 06:20:00`, `Persistent=true`; after the nightly ~00:05–00:15 snapshots, so an overnight failure is caught the same morning). Daily is the right cadence — a snapshot rot is a DR-recoverability concern, not a live outage — and the 12h alert cooldown suppresses repeat pages between fires.
- [`pantheon-worktree-sweeper.service`](pantheon-worktree-sweeper.service) — runs `tools/stale_worktree_sweeper.py --clean`. The *cause* side of the 2026-06-25 disk-full (the disk-usage monitor is the symptom side): agents spawn throwaway build worktrees (e.g. `/tmp/echo-T-1312`), each carrying a 10-20G Rust `target/`; post-merge stragglers accumulate until the FS fills. Removes a worktree ONLY when every guard passes — not the main checkout, path under `/tmp`, tracked tree clean, and HEAD an **ancestor of origin/main** (fully merged) — so it can never drop unmerged/in-progress work (mid-arc worktrees are kept). Cross-account via sudo (operates as each repo's owner). Tool defaults to dry-run; the unit passes `--clean`. T-1324b.
- [`pantheon-worktree-sweeper.timer`](pantheon-worktree-sweeper.timer) — fires the sweeper 6-hourly (`OnCalendar=*-*-* 00,06,12,18:40:00`, `Persistent=true`).

### Telegram bun-child watchdog (runs on `claude-fallback`, the Recovery user) — T-1456
- [`recovery-telegram-child-watchdog.service`](recovery-telegram-child-watchdog.service) — runs `~/.local/bin/recovery-telegram-child-watchdog.sh` once (repo source: [`recovery-telegram-child-watchdog.sh`](recovery-telegram-child-watchdog.sh)). Auto-**recovers** the telegram plugin's `bun server.ts` child (== `bot.pid`) dying MID-SESSION — the "comms-dark drop" that only Recovery hits in practice (Recovery runs 12h+ sessions; orchestrator/specialist context-fill-restart every ~1.5-3h so their child rarely lives long enough to hit the transient that kills it). That child is a **combined** MCP-stdio-server + grammy-poller; when it exits, Claude does NOT re-spawn the stdio child in-session, so BOTH directions stay dark until a full session self_restart. `recovery-bot-supervise.sh` only watches the tmux **session** (which stays alive here), so it cannot catch this. Pure pgrep/`bot.pid` check (no LLM/API — works while comms are dark); on a confirmed death it fires the sanctioned `self_restart.sh`, **bounded**: `DEBOUNCE=2` fires (rides out a legitimate restart's transient child-absence) + `BOOT_GRACE=150s` + `COOLDOWN=600s` (one restart per effectiveness window) + a per-hour `CAP=3`, past which it **stops** and alerts the operator directly (bot token, independent of Claude OAuth). Every detection/action is appended to `~/.claude/logs/recovery-telegram-watchdog.log` (the observability half of T-1456). Fail-open (exits 0 on any unexpected error). Complements the fleet-wide, alert-only, cross-account `pantheon-channel-liveness` **detector** above — this is the Recovery-**self** auto-remediation prong (tighter 90s cadence for fast comms recovery; the T-0894 "auto-remediation" idea, Recovery-scoped). Smoke: [`test_recovery_telegram_child_watchdog.sh`](test_recovery_telegram_child_watchdog.sh) (sandbox-`$HOME`, `--simulate-absent --dry-run`; expect `N passed, 0 failed`). **Recovery-per-host** (like `recovery-bot-supervise.sh`): script installed under `~/.local/bin`, units under `~/.config/systemd/user/`; the repo copies are canonical source.
- [`recovery-telegram-child-watchdog.timer`](recovery-telegram-child-watchdog.timer) — fires the service every 90s (`OnUnitActiveSec=90s`, `OnBootSec=2min`); cheap (a pgrep) so a genuine death auto-recovers in ~3min (DEBOUNCE×cadence).

### Spine-index grooming (runs on `claude-fallback`, the Recovery user)
- [`pantheon-spine-index-groom.service`](pantheon-spine-index-groom.service) — runs `tools/spine_index_groom.py` once. Per fire: walks each spine dir (`knowledge/episodic/`, `tools/`, `notes/`, `bugs/`, `incidents/`, `knowledge/watch/reports/`, `adr/`, `ledger/`), reads `<dir>/README.md` for the existing `<!-- AUTOGEN-START spine-index --> ... <!-- AUTOGEN-END spine-index -->` sentinel region, regenerates table rows from disk state, writes back only if rows changed. Mtime-cached to skip unchanged dirs (`~/.local/state/pantheon/index_groom_cache.json`, `CACHE_SCHEMA_VERSION=1`). Auto-commits + pushes via the shared `_git_utils` discipline (HERMES-guarded). Generalizes the now-retired `tools/ledger_index_groom.py` per T-0141. Idempotent: re-runs are no-op when all spine indexes are current. **Production-README sentinel-region migration is gated behind T-0143** (handler tune-up to match production-README curated shape) — until that lands, the script grooms the legacy `ledger/INDEX.md` only.
- [`pantheon-spine-index-groom.timer`](pantheon-spine-index-groom.timer) — fires the service daily at 12:30 UTC, offset from `pantheon-grooming.timer` at 12:00 UTC to avoid simultaneous-push contention.

### Activity digest (runs on `claude-fallback`, the Recovery user)
- [`pantheon-activity-digest.service`](pantheon-activity-digest.service) — runs `tools/activity_digest.py` once. Per fire: gathers tickets resolved in the last 5hr window from `tickets/done/`, classifies by BACKLOG arc + project, posts a short Telegram-Markdown digest to the operator, appends a ledger entry on non-empty windows. Quiet windows post a one-line "no notable activity" message; no ledger write.
- [`pantheon-activity-digest.timer`](pantheon-activity-digest.timer) — fires the service every 5 hours at 00/05/10/15/20 UTC (B10 system-pulse signal per T-0057 / T-0063 / T-0064).

### Non-security apt updates (system-level, runs as root)
- [`pantheon-apt-update.service`](pantheon-apt-update.service) — runs `apt-get update && apt-get -y --without-new-pkgs upgrade`. Daily auto-security path (`apt-daily-upgrade.service` + `50unattended-upgrades`) already covers security updates; this service is the non-security counterpart.
- [`pantheon-apt-update.timer`](pantheon-apt-update.timer) — fires the service weekly Mon 04:00 UTC ± 30min jitter. **System timer**, not user timer (apt requires root).

### HO auto-resumer (runs on `claude`, the prime user)
- [`pantheon-ho-resumer.service`](pantheon-ho-resumer.service) — polls `~/.local/state/pantheon/ho_limit_hit.json` once per fire; if `resets_at + 90s` has arrived and HO isn't already running a `--resume`, fires `claude --resume <session_id> -p "<locked-prompt>"` and rotates the state file. Bail-and-poll design: scripts terminates fast if it's not yet time. See `tools/ho_resumer.py` and the upstream StopFailure hook at `tools/hooks/ho_stop_failure.py`.
- [`pantheon-ho-resumer.timer`](pantheon-ho-resumer.timer) — fires the service every 5 minutes (`OnCalendar=*-*-* *:0/5:00`). Worst-case latency between window-refresh and resume is ~5min; acceptable for the use case (cap-hits are infrequent; Max plan windows are 5h).

### Adaptive-finish digest (runs on `claude`, the prime user — T-0907)
- [`pantheon-adaptive-finish-digest.service`](pantheon-adaptive-finish-digest.service) — runs `tools/adaptive_finish_digest.py` once. Reads the detector's audit log (`~/.local/state/pantheon/adaptive_finish_log.jsonl`) read-only, summarizes the last 7d of narrate-then-exit hits (count, by-agent, by-phrase, samples), and sends a plain-text Telegram digest via the shared `watch_digest` Bot-API helper. **Skip-on-empty by default** (no send on a zero-hit week; the operator is noise-averse per msg_1934) — `--always` to force. Cadence locked by HO in the T-0220 brief (decision #1). Read-only over the detector; the detector code is untouched.
- [`pantheon-adaptive-finish-digest.timer`](pantheon-adaptive-finish-digest.timer) — fires the service weekly (`OnCalendar=Mon *-*-* 09:15:00 UTC`, `Persistent=true`), offset 15min from the weekly ticket-summary (Mon 09:00).

### Policy outflow (runs on `claude`, the prime user — T-0834, ORG-012 application half)
- [`pantheon-policy-outflow.service`](pantheon-policy-outflow.service) — runs `tools/policy_outflow_sync.py` once. Regenerates `notes/recent-policy-changes.md` — a generated, pointer-only digest of P&Ds/ADRs/SOPs (rulebook/+adr/+sop/) changed in the last 30d, from `git log`. The ORG-012 OUTFLOW half (T-0781 is inflow): persona briefs point at this digest for the recency signal so newly-ratified knowledge gets applied. **Default-safe** (commits+pushes only when the digest content changed; README/index files excluded; digest lives in notes/ so it never self-triggers).
- [`pantheon-policy-outflow.timer`](pantheon-policy-outflow.timer) — fires the service daily (`OnCalendar=*-*-* 12:45:00 UTC`, `Persistent=true`), offset from spine-index-groom (12:30) so the digest reflects the day's grooming. Folder-change-trigger upgrade (ORG-013-shaped) is a deferred follow-up.

### Local byte-sequence audit (runs on `claude`, the prime user — T-2392)
- [`pantheon-hermes-local-audit.service`](pantheon-hermes-local-audit.service) — runs `tools/hermes_local_audit.py` once. **Stand-in for the origin-side guard while the pve2 runner is offline.** `.github/workflows/hermes-audit.yml` is pinned `runs-on: [self-hosted, linux, x64, pve2]`; with pve2 off the tailnet its jobs queue against a registered-but-offline runner and expire at 24h (605 deep on 2026-08-03), and because `protect-main` declares **no required status checks**, a dead check and a passing one are indistinguishable at the merge gate. This scans the same surface locally, from a watermark, so the window is covered by something rather than by nothing. It is **not** a replacement: it runs on one seat's clone against `origin/main` and cannot gate a push. Four design points, each bought with a failure: (1) **`git log -p`, never `git diff BASE...HEAD`** — a cumulative three-dot diff shows NET EFFECT, so a line added in one commit and removed in a later one leaves no trace while remaining in history forever; measured, the cumulative diff was **51%** of the per-commit surface (2,190,513 vs 4,293,141 bytes, reproduced by Recovery from a second clone), and the missing half is exactly the class of thing the guard exists to catch. (2) **Refuses a base that is not an ancestor of `origin/main`** — "the last successful audit run" can be a `pull_request` head sha, and diffing from it yields phantom reversals and a meaningless green. (3) **Validates the instrument every run** — fires a runtime-assembled known-POSITIVE and a known-NEGATIVE through the live pattern and FAILS rather than reporting clean if either misbehaves, because a null instrument that reports clean certifies the window. (4) **A violation never advances the watermark**, so the next run re-scans and keeps failing until a human resolves it. Exit codes are distinct: `1` = found a violation, `2` = could not audit at all — never conflated with `0`. OnFailure→Recovery for both, load-bearing because this unit exists precisely because the thing it replaces failed *silently*. No `sg` wrapper (touches only git + its own state); `git fetch` uses the file-based `IdentityFile` and was verified with `SSH_AUTH_SOCK` unset.
- [`pantheon-hermes-local-audit.timer`](pantheon-hermes-local-audit.timer) — fires every 15 min (`OnCalendar=*:0/15`, `Persistent=true`, `RandomizedDelaySec=30s`). Cheap by construction: only the first run scans the backfill (**1.4s** for 622 commits / 4.3 MB) and each clean run advances the watermark, so later runs cover only new commits and are effectively instant no-ops. `Persistent=true` deliberately — a missed window here is a genuine coverage gap, not a stale sample, which is the opposite of the mail-staleness reasoning above. **Retire this pair once `hermes-audit.yml` is confirmed green on origin again** — and ⛔ **the confirming signal is `pantheon-ci-audit-tip-coverage` reporting `COVERED`, NOT `pantheon-ci-audit-liveness` reporting `OK`** (T-2441). Liveness `OK` means the audit is executing, which stays true throughout a backlog in which the tip has never been audited; retiring the stand-in on it is exactly the substitution the detector family was built to prevent. State lives at `~/.local/state/pantheon/hermes_local_audit.json`.

### Fleet clone-currency check (runs on `claude`, the prime user — T-2798, T-2794 option-b)
- [`pantheon-fleet-clone-currency.service`](pantheon-fleet-clone-currency.service) — runs `tools/fleet_clone_currency_check.py --check` once. **Read-only per-seat drift detector.** Derives every fleet seat from `agent_role_map.fleet_accounts()` (no hardcoded list — a new seat bound in `instance.yaml` is covered automatically), resolves each seat's `pantheon-config` HEAD (own account direct; every other via `sudo -S -k -u <account>`, password piped from `/home/claude/root.env` — orchestrator has no NOPASSWD cross-account sudo, unlike recovery's monitors), and computes commits-behind against the RUNNER's OWN `origin/main` (only `rev-parse HEAD` ever reaches a seat clone — never a fetch/pull/mutation). **Four verdicts**: `CURRENT` / `DRIFTED` (over the threshold, default 20, `PANTHEON_FLEET_CLONE_DRIFT_THRESHOLD` or `--threshold`) / `NO_CLONE` (account has no clone — e.g. the mailbox hub — never alerted) / `UNKNOWN` (could not verify — sudo/auth failure, timeout, unresolvable sha — **alerted exactly like DRIFTED**, since an unverifiable seat is the same silent-drift risk T-2743 found: metis 332 behind, unnoticed for weeks). Alerts land in orchestrator's own ORG-026 mailbox listing each flagged seat + behind-count; **remediation (a supervised ff-pull, the T-2743 pattern) stays a human/HO act** — pulls are never automated (T-2794 option-b). A `git fetch origin main` failure in the runner's own clone skips the sweep + sends a distinct alert rather than comparing against a stale ref. `OnFailure`→Recovery.
- [`pantheon-fleet-clone-currency.timer`](pantheon-fleet-clone-currency.timer) — fires the service daily (`OnCalendar=*-*-* 10:15:00 UTC`, `Persistent=true` — a missed window during downtime is a real coverage gap, not a stale sample). 10:15 UTC is clear of every existing daily/hourly slot.

### CI-audit liveness detector (runs on `claude-fallback`/Recovery — T-2440)
- [`pantheon-ci-audit-liveness.service`](pantheon-ci-audit-liveness.service) — runs `tools/ci_audit_liveness.py` once. **The complement to the local stand-in above: that one COVERS the gap, this one DETECTS it.** The 2026-07-30→08-02 outage produced zero alerts because it produced zero runs — a guard that stops executing is invisible to anything watching conclusions. Signal is the RUNNER's own journal (`ssh pve2` → `pct exec 202` → `journalctl -u actions.runner.example-org-pantheon-config.pve2-pantheon-config`), **not** the GitHub API, for two reasons: recovery holds no GitHub auth (only `claude` does), and `gh run list --limit N` is NEWEST-first while the runner takes the OLDEST queued job — during the 2026-08-03 backlog that read 95-queued twice across 45s, a frozen queue, while the runner finished a job every six seconds. Liveness must never be read off a bounded window. **Four verdicts, not a boolean**, because the journal goes quiet for two different incidents: `OK` / `STALE` (commits landed >12h past the last successful audit) / `HOST_DOWN` (pve2 unreachable — chase the host, not the runner; last time the cause was a saturated home uplink) / `UNKNOWN` (reachable but unmeasurable — unit renamed, journal unreadable, git failed). **UNKNOWN alerts**; a monitor whose broken state is indistinguishable from health is the exact failure it was built to catch. A quiet repo with no commits is `OK`, so a weekend never pages. The **authorised-no** is `~/.local/state/pantheon/ci-audit-liveness.disabled`, JSON `{reason, until}` — a mute with a mandatory EXPIRY, since one that outlives its reason is how a monitor dies quietly; expired/malformed/expiry-less all mean NOT muted. Alerts route `route_failure_alert` → Recovery, throttled 6h per verdict, but a CHANGED verdict speaks through the cooldown (HOST_DOWN→STALE is new information). Exits 0 even when alerting — a non-zero exit would fire OnFailure and report the monitor instead of the fault. ⚠️ **`OK` means the audit is EXECUTING, not that the tip has been audited** — with an oldest-first backlog a fresh success can belong to a days-old commit (observed: OK returned with 505 runs still queued). Tip coverage is a different property and lives in the sibling unit below (T-2441), on the seat that holds GitHub auth.
- [`pantheon-ci-audit-tip-coverage.service`](pantheon-ci-audit-tip-coverage.service) — runs `tools/ci_audit_liveness.py --mode tip` once, **on the `claude` seat only**. Answers the question the runner-side probe structurally cannot: **has `hermes-audit.yml` CONCLUDED for the sha that is origin/main's tip right now?** This is the claim CLAUDE.md's Option A caveat and the local stand-in's retire condition both actually need. ⚠️ **It queries by `head_sha`, never a listing.** A per-sha filter returns only that commit's runs, so the answer cannot depend on queue depth or on which end of the queue got sampled — the trap that reported a frozen queue twice across 45s on 2026-08-03. Do not "optimise" it into `gh run list --limit N` and scan. **Five verdicts**: `COVERED` (a run for the tip concluded success) / `AUDIT_FAILED` (terminal non-success — a *cancelled* audit leaves the tip exactly as unaudited as a red one) / `PENDING` (a run exists, queued or in progress; silent under the 4h threshold, alerts past it — that is a real oldest-first backlog and liveness reads `OK` throughout) / `NOT_DISPATCHED` (**no run exists at all**; silent under a 15m dispatch grace, alerts past it — GitHub never created it, so check the trigger, the ruleset or the workflow file, *not* the runner) / `UNKNOWN` (gh missing, unauthenticated, workflow renamed, API error — **alerts**, never green). **Its own mute**, `~/.local/state/pantheon/ci-audit-tip-coverage.disabled`, deliberately separate from the liveness one: a planned runner outage is a reason to silence execution liveness, but "the tip is not audited" stays true and stays worth saying throughout it. Alerts ride orchestrator's lane (`route_failure_alert` → Recovery for triage), 6h cooldown, own state file so the two instruments cannot cross-throttle. Exits 0 even when alerting, same reason as its sibling.
- [`pantheon-ci-audit-tip-coverage.timer`](pantheon-ci-audit-tip-coverage.timer) — fires hourly at `:37`, offset from the liveness probe's `:07` on purpose: the two signals are *meant* to disagree, and sampling them a half hour apart makes a divergence easier to attribute than a simultaneous pair would.
- [`pantheon-ci-audit-liveness.timer`](pantheon-ci-audit-liveness.timer) — fires hourly (`OnCalendar=*-*-* *:07:00`, `Persistent=true`, `RandomizedDelaySec=120`). Hourly against a 12h threshold on purpose: the fault develops over DAYS, so a tighter period only spends two ssh round-trips and a `git fetch` per fire. `Persistent=true` because a probe missed across a reboot is exactly when this has something to say.

### Delivery reachability sweep (runs on `claude-fallback`/Recovery — T-2232)
- [`pantheon-delivery-reachability.service`](pantheon-delivery-reachability.service) — runs `tools/delivery_reachability.py` once. Answers "**can each seat actually receive a ticket right now?**", which T-2144's guard could not: that one asserts every persona is CLASSIFIED (present in `DELIVERY_MODE` or `DELIVERY_EXEMPT`) — a check on the **declaration**. atlas was declared `send-keys`, had no inbox-consumer unit on its account at all, and the guard stayed green for that seat's entire existence. It also never reads consumer OUTCOMES, deliberately and pinned by a test: metis's consumer passed for weeks on `skip:empty_inbox`, exiting before the inject step, so a green run testifies to nothing about the path. **Five states**, and the split between two of them is the whole finding: `READY` / `DISABLED` (unit present, switched off — somebody ran `disable`, a deliberate act, so an authorised no; reported, never alerted) / `MISSING` (declared deliverable, no unit exists at all — nobody decided this; **ALERT**) / `BROKEN` (enabled but its tmux session or hub inbox is gone) / `UNKNOWN` (**alerts**; never silently green). ⭐ `DISABLED` vs `MISSING` is what a pause manifest structurally cannot express — a manifest lists only what was STOPPED, so a seat that never had a unit is absent from it and survives a complete-looking restore. ⚠️ Two traps encoded in the probe, both of which produced a clean and entirely wrong answer first: templated units are **per-account** (querying `pantheon-inbox-consumer@<agent>.timer` from one account reports no unit for every agent, healthy ones included — an instance is not listed until instantiated on ITS OWN manager), and **`sudo -n -u claude-fallback` from claude-fallback is DENIED** (recovery's grant covers the other accounts; sudo-to-self is a separate grant nobody made) — the denial leaves stdout empty, byte-identical to a successful query finding nothing, which made the first version report a fabricated `MISSING` for recovery. So the probe skips sudo for its own seat, and treats **any** non-zero rc as unaskable rather than as absence. Read-only: it never injects, sends, enables or disables. `READY` is worded as an absence of known blockers, not a guarantee — proving delivery means poking a live pane and costing that agent a turn.
- [`pantheon-delivery-reachability.timer`](pantheon-delivery-reachability.timer) — fires 6-hourly (`OnCalendar=*-*-* 03,09,15,21:23:00`, `Persistent=true`, `RandomizedDelaySec=300`). What it watches changes only when a seat is provisioned, paused or retired — a human-timescale event — and atlas went undelivered for its whole existence, so six hours late costs nothing against never.

### Cross-account systemd-drift sweep (runs on `claude-fallback`/Recovery — T-2444)
- [`pantheon-systemd-drift-sweep.service`](pantheon-systemd-drift-sweep.service) — runs `tools/systemd_drift_sweep.py` once. Answers "**has any seat's INSTALLED units drifted from the repo?**" across the whole fleet from one seat, which the per-seat `systemd_drift_lint` live-tree guard structurally cannot: a seat's drift is only visible when THAT seat runs the suite, and the stalest seats — paused, low-activity, non-dev — are the least likely to run it (recovery's own units sat 7 weeks stale, T-1096; specialist is paused). Coverage is proportional to activity, staleness is inverse to it. Recovery is the only seat that can sweep cross-account — its NOPASSWD sudo reads every account's `~/.config/systemd/user`. **Reuses `systemd_drift_lint.check()` wholesale** via an injected sudo-backed `InstalledSource` — one comparison path, not a second copy. **Four per-account verdicts**: `CLEAN` (read, every tracked unit matches; a cosmetic-only diff is allowed) / `DRIFT` (a SUBSTANTIVE difference — a directive systemd acts on) / `UNREACHABLE` (could not read the account at all — **ALERT**) / `NO_UNITS` (read, but zero tracked units installed — reported, not alerted). ⚠️ Three traps, each already paid for once: (a) `sudo -n -u <self>` from that same account is DENIED and leaves stdout empty, byte-identical to a clean empty read — so self skips sudo and **any** non-zero rc is UNVERIFIED, never "no drift"; (b) reachable-but-zero-units is `NO_UNITS`, never `CLEAN`, or a wiped fleet reads green; (c) a seat's units are cp's not symlinks, so drift is a CONTENT difference (the inert-copy IS the whole failure mode). **Severity split** (T-2444 correction #2): a difference confined to inert metadata directives (`Documentation=`/`Description=`) is `cosmetic` — real drift, reported, but does not page — so a doc-path reshuffle on a paused peer neither alerts nor gets tuned out; a real directive change alongside it stays `substantive`. **REPORT, NEVER SYNC**: the repo is canonical for CONTENT but not automatically CORRECT (the recon found one unit whose INSTALLED copy was right and repo copy stale — an auto-sync would have broken a working seat while reporting a fix), so it names drift on others' substrate and a human decides. Known limitation, stated honestly: a `%h`/`%u`-templated directive vs its expanded literal reads as substantive (conservative — a real `Environment=` diff must never be hidden). Alerts route `route_failure_alert` → Recovery; exits 0 even when alerting (a non-zero exit fires OnFailure and reports the monitor, not the drift). Read-only: never writes, enables, reloads or syncs.
- [`pantheon-systemd-drift-sweep.timer`](pantheon-systemd-drift-sweep.timer) — fires daily (`OnCalendar=*-*-* 05:23:00`, `Persistent=true`, `RandomizedDelaySec=300`). Daily against no staleness threshold on purpose: the fault develops over WEEKS (a repo edit that never reached a seat), so a tighter period only spends 8 cross-account sudo reads per fire; a day is soon enough for a 7-week gap.

### Agent-monitor liveness watchdog (runs on `claude`, the prime user — T-1885)
- [`pantheon-agent-state-snapshot.{service,timer}`](pantheon-agent-state-snapshot.service) + [`.sh`](pantheon-agent-state-snapshot.sh) — daily LOCAL git snapshot of orchestrator's memory dir and `HANDOFF.md` (T-2424, sister to mnemosyne's T-2233). Those two carry everything a session knows that is not derivable from the repos (D10) and were single-copy with no VCS and no snapshot; a bad `sed` or a disk event lost them silently. Snapshots **copies** into a dedicated repo at `~/.local/state/pantheon/agent-state-history` rather than git-init'ing the live dirs — the two paths share no parent short of `$HOME` (which holds `root.env` and every project clone), and a HANDOFF copy placed inside the memory dir would pollute the harness's memory recall, which reads that directory. ⛔ **Strictly local, no remote, ever** — the memory dir holds personal facts about the operator, which is why T-2233 rejected folding this into pantheon-config. Off-host replication is a separate, open the operator decision. Fails loudly if a source path is missing rather than committing its disappearance as a clean empty snapshot. Idempotent; run it by hand right after editing HANDOFF.md. On `EXCLUDED` in the failure-alert coverage test, with the reason recorded there.
- ⛔ **DORMANT 2026-08-02** — `agent-monitor.service` and `pantheon-agent-monitor-healthcheck.{service,timer}` are **stopped and disabled** on the operator's retire ruling (TG 6746/6747, closing the TG 6507 fix-vs-retire steer). Nothing consumed the dashboard: no `tailscale serve` config, and the only references were the watchdog's own liveness probe and docs. The watchdog was disabled *with* it because it probes only that service and escalates direct-to-operator every 2 min — leaving it armed would have alerted him about the thing he switched off. Units and checkout left on disk; re-arm with `systemctl --user enable --now agent-monitor.service`. The description below is retained as the re-arm reference and describes the ARMED behaviour, not the current state.
- [`pantheon-agent-monitor-healthcheck.service`](pantheon-agent-monitor-healthcheck.service) — runs `tools/agent_monitor_healthcheck.py --check` once. Pure HTTP probe of `http://127.0.0.1:4820/` (no LLM/API) — structural guard for the documented node event-loop wedge (`bugs/agent-monitor-node-event-loop-wedge-long-uptime.md`, S3STAR-WN-2026-07-20-001): `agent-monitor.service`'s listener stays open but requests stall/time out, and `Restart=on-failure` never trips because the process never exits. Same-account (no sudo, unlike the cross-account monitors above). On N=3 consecutive stalled polls (persisted across timer fires — anti-flap), restarts the unit; 30-min cooldown against a restart storm; escalates direct-to-operator via the Telegram bot token (independent of Claude OAuth, matching `disk_usage_monitor`'s self-account-infra shape) only if a second restart is needed within 2x the cooldown. **SHIP-DARK** (matches `ancestor_liveness_monitor`/`channel_liveness_monitor`): detection runs live unconditionally; the actual restart is gated behind `PANTHEON_AGENT_MONITOR_REMEDIATE_ENABLED` (default OFF) — the shipped unit installs unarmed, pending a supervised first-fire. Restart forensics logged to `~/.local/state/pantheon/agent_monitor_healthcheck_restarts.jsonl`.
- [`pantheon-agent-monitor-healthcheck.timer`](pantheon-agent-monitor-healthcheck.timer) — fires the service every 2 minutes (`OnCalendar=*-*-* *:0/2:00`).

### Telegram bridge (runs on `claude`, the prime user — ORG-004 Phase 1)
- [`pantheon-bridge.service`](pantheon-bridge.service) — persistent foreground service (`Type=simple`, `Restart=always`, `RestartSec=5`). Runs `~/pantheon-bridge/.venv/bin/telegram-bridge serve`, the single owner of Telegram's `getUpdates` poll lock. Multi-client SSE/HTTP exposed on loopback for Orchestrator/Specialist/Recovery to consume. Replaces the in-process Telegram MCP plugin's polling, which contends with subagent dispatches inside the `claude` user account (Issue #46617). EnvironmentFile at `~/.claude/channels/telegram-bridge/.env` (mode 0600; contains `BOT_TOKEN`, per-instance `BRIDGE_TOKEN_*`, `BRIDGE_PORT`). Routing config at `~/.claude/channels/telegram-bridge/routing.yaml` (operator-staged, validated by `telegram-bridge --validate-config <path>` before deploy). State persisted in `~/.claude/channels/telegram-bridge/state.db` (SQLite). **No timer**: the bridge is a long-running daemon, not a cron-style oneshot.

### Specialist long-lived TUI (runs on `claude-research`, the specialist user — T-0102 Phase 1)
- [`claude-telegram-research.service`](claude-telegram-research.service) — one-shot user-systemd unit that bootstraps a long-lived tmux session running Claude Code (bare invocation; **no `--channels` plugin** per the operator msg_784/785 scope-tightening 2026-05-09). Specialist does NOT have an interactive Telegram surface; this unit exists solely so the inbox-consumer cron's `tmux send-keys` injection has a Claude Code session to land on. Outbound Specialist messaging stays on the existing direct-curl POST path (`activity_digest.py` + watch-loop digest); no inbound DM surface. Mirrors the Recovery-side `claude-telegram-fallback.service` shape (Type=oneshot + RemainAfterExit=yes); differs on account name (`claude-research`), tmux session name (`claude-research-bot`), and description. Unit name retained for stability — `claude-telegram-research.service` is a slight misnomer post-scope-tightening (no telegram bridge); future-rename to `claude-tui-research.service` is a follow-up cleanup, not blocking. **Unlike Orchestrator's + Recovery's telegram-bot units which are per-host, this unit IS version-controlled per T-0102 ratification** — see §Files convention amendment above.

## Install

### Watch loop (run as `claude-research`)

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-watch.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-watch.timer ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-watch-digest.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-watch-digest.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pantheon-watch.timer
systemctl --user enable --now pantheon-watch-digest.timer
loginctl enable-linger claude-research  # ensure timers run when the user isn't logged in
```

### Ticket grooming (run as `claude-fallback`, i.e. Recovery)

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-grooming.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-grooming.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pantheon-grooming.timer
loginctl enable-linger claude-fallback  # ensure timers run when the user isn't logged in
```

### Unit-failure alert (any account whose units reference it)

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-unit-failure-alert@.service ~/.config/systemd/user/
systemctl --user daemon-reload
# No enable/start — activated by OnFailure= on the referring units. Re-cp those
# referring units too (installed units are COPIES, not symlinks — a repo-side
# OnFailure= edit is inert until re-copied + daemon-reload; the T-1867 lesson).
```

### Spine-index grooming (run as `claude-fallback`, i.e. Recovery)

```bash
mkdir -p ~/.config/systemd/user
# Migration from the old pantheon-ledger-index-groom (renamed 2026-05-09 per T-0141):
systemctl --user disable --now pantheon-ledger-index-groom.timer 2>/dev/null || true
rm -f ~/.config/systemd/user/pantheon-ledger-index-groom.{service,timer}
# Install the new spine-index-groom units:
cp ~/pantheon-config/tools/systemd/pantheon-spine-index-groom.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-spine-index-groom.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pantheon-spine-index-groom.timer
# linger already enabled for ticket-grooming; no separate enable-linger needed.
```

### Activity digest (run as `claude-fallback`, i.e. Recovery)

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-activity-digest.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-activity-digest.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pantheon-activity-digest.timer
# linger already enabled for ticket-grooming; no separate enable-linger needed.
```

### HO auto-resumer (run as `claude`)

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-ho-resumer.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-ho-resumer.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pantheon-ho-resumer.timer
loginctl enable-linger claude  # ensure timer runs when the user isn't logged in
```

### Restart drain — the self-heal core (run as EACH agent: `claude`, `claude-research`, `claude-fallback`) (T-0783)

Processes restart tombstones → graceful agent restarts. Runs per-account (each account drains its OWN tombstones — same-account self-restart). Ships **default-OFF**; the `enable.conf` drop-in arms it (it bails clean unless `PANTHEON_RESTART_DRAIN_ENABLED=1`), so the first arming should follow a supervised first-fire.

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-restart-drain.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-restart-drain.timer   ~/.config/systemd/user/
# feature-flag drop-in (arms the drain; omit to leave it installed-but-inert):
mkdir -p ~/.config/systemd/user/pantheon-restart-drain.service.d
printf '[Service]\nEnvironment=PANTHEON_RESTART_DRAIN_ENABLED=1\n' \
  > ~/.config/systemd/user/pantheon-restart-drain.service.d/enable.conf
systemctl --user daemon-reload
systemctl --user enable --now pantheon-restart-drain.timer   # ~30s cadence
loginctl enable-linger "$USER"
```

### Context-fill + activity monitors + ticket-lint (run as `claude-fallback`, i.e. Recovery) (T-0783)

Recovery runs the system-wide self-heal monitors (it holds the cross-account sudo to watch the others). The context-fill monitor is parametric (`@<agent>`) — enable one timer instance per agent watched.

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-context-fill-monitor@.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-context-fill-monitor@.timer   ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-activity-monitor@.service     ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-activity-monitor@.timer       ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-ticket-lint.service           ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-ticket-lint.timer             ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-credential-expiry.service     ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-credential-expiry.timer       ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-disk-usage.service            ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-disk-usage.timer              ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-memory-snapshot-coverage.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-memory-snapshot-coverage.timer   ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-worktree-sweeper.service      ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-worktree-sweeper.timer        ~/.config/systemd/user/
systemctl --user daemon-reload

# context-fill monitor: one @-instance per agent watched (every 5 min):
systemctl --user enable --now pantheon-context-fill-monitor@orchestrator.timer
systemctl --user enable --now pantheon-context-fill-monitor@specialist.timer
systemctl --user enable --now pantheon-context-fill-monitor@recovery.timer
# activity monitor (ORG-011 HO-absent queue-poke) — currently watches orchestrator:
systemctl --user enable --now pantheon-activity-monitor@orchestrator.timer
# ticket-lint (hourly :20 frontmatter scan + auto-fix):
systemctl --user enable --now pantheon-ticket-lint.timer
# credential-expiry monitor (hourly :35 — OAuth-credential health; Jun-2026 outage prevention):
systemctl --user enable --now pantheon-credential-expiry.timer
# disk-usage monitor (hourly :50 — root-FS capacity; fleet-wedge-on-full prevention):
systemctl --user enable --now pantheon-disk-usage.timer
# memory-snapshot coverage monitor (daily 06:20 — catch a per-seat snapshot timer that silently stopped; T-2838/T-1871 DR-rot guard):
systemctl --user enable --now pantheon-memory-snapshot-coverage.timer
# stale-worktree sweeper (6-hourly :40 — auto-remove merged throwaway build worktrees; T-1324b):
systemctl --user enable --now pantheon-worktree-sweeper.timer
loginctl enable-linger claude-fallback
```

The context-fill monitor's per-agent detector legs — **api-health force-restart** (`PANTHEON_API_HEALTH_ENABLED`), **wedge/staleness** (`PANTHEON_STALENESS_ENABLED`), and **model-outage alert** (`PANTHEON_MODEL_OUTAGE_ENABLED`) — are each armed by a one-line drop-in on `pantheon-context-fill-monitor@<agent>.service.d/`. These drop-ins live only in Recovery's `~/.config` (not in the unit templates here), so they are **reproducible via `tools/provision_context_fill_monitor_dropins.py`** (T-1214) rather than hand-created:

```bash
# After (re)installing the @-instances above, re-arm the detector legs:
python3 ~/pantheon-config/tools/provision_context_fill_monitor_dropins.py --provision
# Confirm every leg is armed (exits non-zero on any missing):
python3 ~/pantheon-config/tools/provision_context_fill_monitor_dropins.py --verify
```

The tool IS the canonical manifest (agents × detectors); a re-provision that skips it leaves the legs disarmed, which `verify_startup.sh` surfaces as a SOFT WARN at the next boot (C9 guard) instead of letting detection go silently dark. The api-health/staleness/model-outage cutover history (each leg was first-fired supervised before fleet-wide) lives in the ticket trail (T-0857 / T-1126) and the ledger — see also the `context_fill_monitor.py` module docstring. Each leg is removable (delete the `.conf` + `systemctl --user daemon-reload`) to disarm.

### Consumer-liveness monitor (runs on `claude-fallback`, the Recovery user) — T-2605

- [`pantheon-consumer-liveness.service`](pantheon-consumer-liveness.service) — runs `tools/consumer_liveness_monitor.py` once. Watches the slice no queue-keyed detector sees: a live inbox-consumer that **stops COMPLETING cycles while its queue is EMPTY**. Signal = each seat's oneshot service `ExecMainExitTimestamp` (advances every 10-min cycle including `skip:empty_inbox`, freezes when the timer stops firing), read cross-account READ-ONLY via `sudo -u <account> systemctl --user show`, compared against a 30-min (3-cadence) staleness threshold. Verdicts LIVE / STALLED / DISABLED / NO_TIMER / UNKNOWN; ALERT on STALLED (immediate — a real timestamp, not a flapping poll) + UNKNOWN (streak-gated 3, per T-2717). **Deliberately non-overlapping**: a RUN that fails/hangs is caught by the consumer service's own `TimeoutStartSec=5min`→OnFailure; a never-armed timer by `verify_startup` C8; a missing/disabled unit by `delivery_reachability` (NO_TIMER is report-only here, never re-paged). Alert-only via `route_failure_alert` (others→Recovery, recovery-self→the operator), 6h per-verdict cooldown. Authorised-mute: `~/.local/state/pantheon/consumer-liveness.disabled` (fleet) / `consumer-liveness.<agent>.disabled` (one), JSON `{reason, until}`, expiry-checked, fails toward alerting. OnFailure→Recovery mailbox (T-1928).
- [`pantheon-consumer-liveness.timer`](pantheon-consumer-liveness.timer) — fires every 5 min (`OnCalendar=*-*-* *:00/5:00`, `Persistent=true`, `RandomizedDelaySec=30`).

```bash
cp ~/pantheon-config/tools/systemd/pantheon-consumer-liveness.service ~/.config/systemd/user/
cp ~/pantheon-config/tools/systemd/pantheon-consumer-liveness.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pantheon-consumer-liveness.timer
```

### Specialist interactive bot (run as `claude-research`)

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/claude-telegram-research.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram-research.service

# Then enable the inbox-consumer timer for specialist (uses the parametric pantheon-inbox-consumer@.{service,timer} template; no per-instance file needed):
systemctl --user enable --now pantheon-inbox-consumer@specialist.timer

# linger for the user (idempotent; required for user-units to survive logout):
loginctl enable-linger claude-research
```

**Plugin install + Claude Code auth** in `claude-research`'s `~/.claude/` (using existing botfather identity per the operator 2026-05-09 msg_1108) is operator-tier work — Phase 2 of T-0102; tracked at T-0145 (Recovery-target follow-up ticket) since Recovery has the asymmetric-repair-sudo for cross-account install.

### Telegram bridge (run as `claude` — ORG-004 Phase 1, T-0026 install)

**CRITICAL — do NOT auto-start or auto-enable on T-0026 install.** The bridge acquires Telegram's `getUpdates` lock when it starts; doing so while the official Telegram MCP plugin is still active (default state today) evicts the plugin's lock and breaks Orchestrator's MCP inbound. T-0027 owns the live cutover in tight sequence with the per-instance MCP-config flip; T-0026 stops at "unit installed, recognized, inactive."

```bash
# Pre-reqs: ~/pantheon-bridge already cloned + venv built (T-0025);
# ~/.claude/channels/telegram-bridge/{.env,routing.yaml} already staged + 0600.
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/pantheon-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload

# Verify ONLY — do not start, do not enable:
systemctl --user status pantheon-bridge.service --no-pager   # expect: loaded; inactive (dead)
systemctl --user is-enabled pantheon-bridge.service          # expect: disabled
systemd-analyze --user verify pantheon-bridge.service        # expect: clean
```

If you accidentally start it: `systemctl --user stop pantheon-bridge.service` and the official plugin reclaims the `getUpdates` lock on its next poll (~30s). No message loss; brief inbound gap.

### Non-security apt updates (run as `root` — the operator's hands)

This is a **system** timer, not a user timer. Bot accounts (`claude`, `claude-fallback`, `claude-research`) lack root sudo per VPS topology; root operations land via the operator over tailscale SSH.

```bash
# As root:
cp /home/claude/pantheon-config/tools/systemd/pantheon-apt-update.service /etc/systemd/system/
cp /home/claude/pantheon-config/tools/systemd/pantheon-apt-update.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pantheon-apt-update.timer
```

The unit fires Monday 04:00 UTC (low-traffic window, well after `apt-daily-upgrade.service`'s daily fire) with 30min randomized jitter. Output goes to journal; no Telegram digest in v0 (operator polls journal weekly, or wires a `needrestart`-driven notify hook later if drift becomes a real problem). Reboot policy: **skip auto-reboot, surface in journal** — kernel-required-reboot is rare and the operator decides timing.

## Verify

```bash
# Watch loop (claude-research)
systemctl --user list-timers pantheon-watch*
journalctl --user -u pantheon-watch.service -n 50 --no-pager
journalctl --user -u pantheon-watch-digest.service -n 50 --no-pager

# Grooming (claude-fallback)
systemctl --user list-timers pantheon-grooming*
journalctl --user -u pantheon-grooming.service -n 50 --no-pager

# Spine-index grooming (claude-fallback)
systemctl --user list-timers pantheon-spine-index-groom*
journalctl --user -u pantheon-spine-index-groom.service -n 50 --no-pager

# Activity digest (claude-fallback)
systemctl --user list-timers pantheon-activity-digest*
journalctl --user -u pantheon-activity-digest.service -n 50 --no-pager

# Apt updates (system, run as root)
systemctl list-timers pantheon-apt-update*
journalctl -u pantheon-apt-update.service -n 50 --no-pager

# HO auto-resumer (claude)
systemctl --user list-timers pantheon-ho-resumer*
journalctl --user -u pantheon-ho-resumer.service -n 50 --no-pager

# Telegram bridge (claude) — only after T-0027 cutover starts the service
systemctl --user status pantheon-bridge.service --no-pager
journalctl --user -u pantheon-bridge.service -n 50 --no-pager
```

## Manual fire

```bash
# Watch loop (run as claude-research):
systemctl --user start pantheon-watch.service
python3 ~/pantheon-config/tools/watch_runner.py --dry-run    # see what would fire
python3 ~/pantheon-config/tools/watch_runner.py              # fire all due topics
python3 ~/pantheon-config/tools/watch_runner.py --topic anthropic-claude-code-changelog

# Grooming (run as claude-fallback / Recovery):
systemctl --user start pantheon-grooming.service
python3 ~/pantheon-config/tools/ticket_grooming.py --dry-run --no-digest    # passive sanity-check
python3 ~/pantheon-config/tools/ticket_grooming.py --no-digest              # real scan + auto-commit, no Telegram send
python3 ~/pantheon-config/tools/ticket_grooming.py                          # full fire (scan + commit + push + digest)

# Spine-index grooming (run as claude-fallback / Recovery):
systemctl --user start pantheon-spine-index-groom.service
python3 ~/pantheon-config/tools/spine_index_groom.py --dry-run              # scan + print rows-to-append, no write
python3 ~/pantheon-config/tools/spine_index_groom.py --no-push              # write + commit, skip push
python3 ~/pantheon-config/tools/spine_index_groom.py                        # full fire (write + commit + push)

# Activity digest (run as claude-fallback / Recovery):
systemctl --user start pantheon-activity-digest.service
python3 ~/pantheon-config/tools/activity_digest.py --agent recovery --dry-run   # gather + render, no Telegram send
python3 ~/pantheon-config/tools/activity_digest.py --agent recovery             # full fire (gather + render + Telegram + ledger)

# HO auto-resumer (run as claude):
systemctl --user start pantheon-ho-resumer.service
python3 ~/pantheon-config/tools/ho_resumer.py    # manual poll (bails clean if no state file or not-yet-time)
```

## State + log locations

### Watch loop
- State (last-fire dates per topic): `~/.local/state/pantheon/watch/state.json`
- Driver per-fire stdout/stderr capture: `~/.local/log/pantheon/watch/<date>-<slug>.log`
- systemd journal for the units: `journalctl --user -u pantheon-watch.service`

### Grooming
- No state file (the staleness data lives entirely in each ticket's transition log; the grooming agent re-derives it every fire).
- systemd journal for the unit: `journalctl --user -u pantheon-grooming.service`
- Telegram digest history: visible in the chat itself; no local mirror at v0.

### HO auto-resumer
- State file (written by the StopFailure hook): `~/.local/state/pantheon/ho_limit_hit.json`
- Consumed (post-fire archive): `~/.local/state/pantheon/ho_limit_hit.json.consumed.<epoch>`
- systemd journal: `journalctl --user -u pantheon-ho-resumer.service`

**Troubleshooting checklist (post-incident):**
1. State file present? `ls -lh ~/.local/state/pantheon/ho_limit_hit.json`
2. mtime fresh (< 24h)? `stat ~/.local/state/pantheon/ho_limit_hit.json`
3. `resets_at` parseable? `python3 -c "import json,pathlib; print(json.loads(pathlib.Path('~/.local/state/pantheon/ho_limit_hit.json').expanduser().read_text())['resets_at'])"`
4. Timer firing? `systemctl --user list-timers pantheon-ho-resumer*`
5. Journal shows recent fires? `journalctl --user -u pantheon-ho-resumer.service -n 20 --no-pager`
6. claude binary reachable? `which claude` or check `PANTHEON_CLAUDE_BIN` in the service unit.

## Notes

- The `claude` binary path is set via the `PANTHEON_CLAUDE_BIN` env var in the service unit (default: `~/.npm-global/bin/claude`). Adjust if your install layout differs.
- The digest timer runs after the driver (offset ≥ 1h to give last fires time to land). See the `OnCalendar` lines in the timer files.
- Both timers persist `Persistent=true` so missed fires (laptop closed / VPS reboot during fire window) re-run on next boot.

## S3* audit dispatch (ORG-038 pilot)

`pantheon-s3star-audit.timer` → `pantheon-s3star-audit.service` (oneshot, daily 08:30 UTC tick, runs as `claude`/Orchestrator): invokes `tools/s3star_audit_runner.py --file-dispatch-ticket`, which checks both pilot cadences (weekly-narrow / monthly-integrated) and, for any due slot, composes the audit brief from the ORG-038 proposal verbatim and files a SUPERVISED dispatch ticket to orchestrator (no unattended Codex dispatch exists — `--auto-dispatch` deliberately raises; see T-1583). Findings return via `--land-findings`, which files one ticket per finding to domain owners (never to codex). Enable at supervised-first-fire only: `systemctl --user enable --now pantheon-s3star-audit.timer`. State: `~/.local/state/pantheon/s3star_audit/`; log: `~/.local/log/pantheon/s3star_audit_runner.log`.

## ACP inbound bridge (T-1633 Unit 2, ORG-031 pilot)

These templates run on the **piloted agent's own account** (per-agent `%i`), not a fixed account. They are **NOT auto-installed** by any provision script — the pilot flip (install + enable for one agent) is a later, supervised HO+the operator step with its own runbook; disable-not-delete the agent's `pantheon-mailbox-wake@%i` waker first, and quiesce send-keys/waker before the bridge consumes (exclusive consumer — a split Maildir drain is the hazard).

- [`pantheon-acp-bridge@.service`](pantheon-acp-bridge@.service) — per-agent (%i) template. Runs `tools/acp-bridge-supervise.sh` as `Type=simple` MainPID (Restart=always), which hosts `tools/acp_bridge.py`'s production serve-loop inside a detached tmux session (operator surface preserved — `tmux capture-pane`/`send-keys` still work) via the `sg pantheon-mailbox -c` wrap the Path-A hub inbox requires (T-1015). `WorkingDirectory=%h` (NOT `%h/pantheon-config`) — keeps the wrapper JSONL path invariant (`~/.claude/projects/<mangled-cwd>/…`) that `context_fill_monitor.py` reads. Never sets/passes ANTHROPIC_*/CLAUDE_CODE_* env (the API-key-shadow money-trap); `acp_bridge.py`'s `assert_clean_room()` is the hard gate, the supervise script's `compgen -v` strip is defense-in-depth. The operator pane shows per-drain status lines, not raw ACP `session/update` traffic — use `tools/acp_bridge_wetrun.py` for wire-level debugging.
- [`pantheon-acp-bridge-backlog@.service`](pantheon-acp-bridge-backlog@.service) + [`.timer`](pantheon-acp-bridge-backlog@.timer) — per-agent (%i) oneshot + timer, modeled on `pantheon-context-fill-monitor@.timer` (5-min cadence). Runs `tools/acp_bridge_backlog_watchdog.py --agent %i --check`: pure readdir of the piloted agent's Path-A hub `inbox/new/`, alerting (direct-to-operator, same `_send_telegram_alert` primitive as `disk_usage_monitor.py`, per-agent cooldown) if the message count or the oldest-message age exceeds threshold — the signature of a dead/stuck/quiesced bridge process. **NOT auto-installed** by this build.

> Note on verifying these templates: a `%h`-relative `ExecStart` cannot self-verify via `systemd-analyze verify` until the referenced script exists at the real `$HOME` (pre-install the path resolves to a non-existent file). Expected for every `%h`-relative template's first commit — verify against an absolute-path scratch copy if needed; it self-resolves once installed.
