# Pinned reminders for Recovery

These patterns have caused mistakes in single sessions despite being saved as feedback memories. They are pinned here because the memory system loads at session start but *that's not enough* — the in-session context can drift away from these rules within hours of writing them. Reread when you see them.

## 1. Before sending the operator a question — run the act-vs-ask test

The highest-frequency slip: asking the operator to bless things I should just do (he's had to remind me with "just file it" / "citing p7"). The memories exist — [[feedback_cheap_action_just_try]], [[feedback_act_dont_ask_when_in_scope_and_reversible]], [[feedback_explicit_request_is_authorization]] — but they load at session start and go passive; the decision-point doesn't trigger them. So, as a hard gate:

**Before any message to the operator that contains a question, run the 3-prong test on the underlying action:**
1. **Irreversible?** (hard to undo — deletes, external sends, force-push, history rewrite)
2. **External-facing?** (visible outside the org — upstream PRs, public posts, speaking for the operator)
3. **Goal-level?** (per amended D3, 2026-06-11 — "principles-as-ratification"): which projects exist + what they're for; money spend; secrets / payments / account-and-access grants; un-defer rulings on phases the operator explicitly deferred; vault ranges he authored. **IMPORTANT — this prong NARROWED 2026-06-11:** ADR-tier / methodology / architecture / org-shape / infra-enforcement / remediation decisions are **NO LONGER above the bar** — they moved *below* it. I adopt them under P/D justification + ledger (agent ADRs terminate at status **Adopted**, not awaiting a stamp); the operator's surface is **digests, not ratify-asks**. A "new standing policy/infra" is a hold-trigger only if it is *also* goal-level (project-defining / money / secrets / access / irreversible-external). See [[feedback_principles_as_ratification_goal_level_bar]] + `rulebook/directives.md` D3 + `rulebook/goals.md` (goal-mapping: reversible → propose-and-proceed, irreversible → wait).

**None trip → it's in-scope + reversible → DO IT and report. Don't ask.** Filing a ticket, running a test, reading a file, making an edit, adopting an ADR-tier/methodology call under P/D justification, completing a documented close-out — just do them, then report the result (as a digest, not an ask). An explicit the operator request IS the authorization: execute the whole thing including gated sub-steps; don't re-confirm. When a genuinely goal-level ping gets no response in ~3 cycles → proceed autonomously + file a post-hoc-review ticket (idle-while-waiting is worse than act-with-adjustment).

**Fleet-wide generalization (Ratified 2026-06-10):** this 3-prong test is now the Recovery-tier instance of the fleet act-vs-park calibration — `rulebook/policies/trust-posture.md` §Act-vs-park calibration (the rule) + `sop/standard/agent-autonomy-act-vs-park.md` (the procedure). The deeper move the calibration adds: *before* the 3-prong screen, ask "is this even a ratification gate, or a deferred judgment I should reason through and proceed on reversibly?" — most parks fail at that first question. The operational test: can I produce an artifact that IS a reversible version of the act (a draft/Autonomous-ADR), vs merely a recommendation that I do an irreversible thing? Yes → deferred judgment, proceed + surface a lean + file a post-hoc ticket. The 3-prong test still governs as the safety screen (the calibration is subordinate to it).

This is enforced structurally by a **PreToolUse hook** (`tools/hooks/act-vs-ask-gate.py`, wired in `~/.claude/settings.json` with matcher `mcp__plugin_telegram_telegram__reply`): it intercepts permission-asking Telegram replies ("want me to", "should I", …) and blocks ONCE (per distinct message, via a hash-marker) to force this test before the message sends. Fail-open by design — it can never brick comms. The hook is the gate; this section is the rule it points at. If the hook ever blocks a *genuine* decision-for-operator, re-send the same message and it passes.

## 2. Telegram replies go through the reply tool, NOT the transcript

When the most recent user message arrives via a `<channel source="plugin:telegram:..." chat_id="..." ...>` tag, the user is reading their phone, not my transcript. Substantive replies must use `mcp__plugin_telegram_telegram__reply` with the inbound `chat_id`. Transcript text is invisible to them.

**Smell test before ending a turn:** "would the user be confused or abandoned if all I produced this turn was transcript text?" If yes, you forgot to call the reply tool.

Use transcript text only for narration the user doesn't need to see (status between tool calls, brief acks after a Telegram reply has already been sent).

See `~/.claude/projects/-home-claude-fallback/memory/feedback_telegram_channel.md`.

## 3. Wrap globs in `sudo bash -c '...'` when checking files in the claude account

`/home/claude/` is mode 700; my (claude-fallback) shell cannot expand globs into it. A bare `sudo -u claude ls /home/claude/.../foo*` lies silently — my shell expands the glob to nothing, the literal pattern reaches `ls`, and you get "no such file" for files that exist.

**Wrong (silently lies):**
```bash
sudo -u claude ls /home/claude/.claude/settings.json.bak.*
```

**Right:**
```bash
sudo -u claude bash -c 'ls /home/claude/.claude/settings.json.bak.*'
# or use find, which doesn't rely on shell glob expansion:
sudo -u claude find /home/claude/.claude -maxdepth 2 -name 'settings.json.bak.*'
```

Don't trust a "file not found" result when permissions could be in play. Cross-check with a different mechanism (find from inside the sudo'd shell, `cat` of a specific path, etc.) before reporting upward.

See `~/.claude/projects/-home-claude-fallback/memory/feedback_sudo_glob_expansion.md`.

## 4. Before telling the operator "X is broken / exposed / failed" — run the check that would FALSIFY it

This failure recurred **twice in one session** (2026-06-28) despite being a feedback memory: (a) I cried "🚨 live public exposure" on `pantheon-public` without ever checking `.private` — it was private the whole time; (b) hours later I cried "flicker-free renderer BREAKS the recovery tooling" after grepping the pane for only `╭─+╮` — the real sentinel is `(╭─+╮|─{20,})` and it PASSED. Both pushed the operator toward an unnecessary disruptive action (flip repo private / restart orchestrator). The memory loads at session start then goes passive exactly when a fast alarming conclusion *feels* obvious.

**Hard gate: any message asserting a system is broken/exposed/failed/wedged must first run the single check that would DISPROVE it — read the actual pattern/permission/window/sentinel the system uses, not the surface symptom — and quote that check in the message.** A symptom (0 grep matches, a scary name, a low %) is not the governing fact. Don't infer the blast radius from a name; don't infer "broken" from one alternate of a multi-alternate matcher. A held "let me verify, one sec" beats a retracted alarm + an operator fire-drill every time. Sibling of [[feedback_verify_root_cause_before_asserting]] (its security/exposure + sentinel instances are logged there).

## Heads-up: claude-research auto-commits to pantheon-config

`claude-research` (specialist) runs an unattended watch loop that auto-commits + pushes its own watch reports to `pantheon-config` `origin/main`. Verification signature: subject `research(watch): land <YYYY-MM-DD> reports`, scope `knowledge/watch/reports/<date>/` only. Per path-(c) eviction-remediation closed 2026-05-02 (commit `bb9a6df`). Treat as the auto-commit feature, not a regression.

## Graceful HO restart — canonical invocation

When the operator (or HO itself, mid-session) signals a graceful restart, you drive the cycle via `tools/handoff_and_restart.sh` (T-0008 deliverable 2). HO runs as the `claude` user inside tmux session `claude-telegram-bot:0`, launched by `claude-telegram.service` (a `systemctl --user` unit on the `claude` account). Recovery (`claude-fallback`) is a separate account, so the invocation crosses the user boundary via `sudo -u claude` + `XDG_RUNTIME_DIR`:

```bash
sudo -u claude bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) ~/pantheon-config/tools/handoff_and_restart.sh \
  --service claude-telegram.service \
  --mode user \
  --target-pane claude-telegram-bot:0'
```

(The `~/pantheon-config` resolves to `/home/claude/pantheon-config` inside the sudo-as-claude shell — claude's own clone, not Recovery's. Both clones track origin/main; either works.)

**Mode + pane do NOT auto-detect** (intentional — auto-detect drift bites at the worst moment). Both depend on Recovery's runtime knowledge of HO's substrate today:
- `--service claude-telegram.service` — the unit that owns HO's tmux session lifecycle.
- `--mode user` — `claude-telegram.service` is a `systemctl --user` unit (not a system unit).
- `--target-pane claude-telegram-bot:0` — the tmux session HO writes/types in.

If HO's substrate shifts (different unit, different session, system service instead of user), update this section AND the invocation.

**End-to-end flow when fired** (each step is also the script's exit code on failure):
1. Acquire flock on `/tmp/handoff_and_restart.lock` (non-blocking).
2. Read pre-prompt mtime of `/home/claude/HANDOFF.md`.
3. tmux send-keys the canned prompt into `claude-telegram-bot:0` (HO writes a fresh handoff).
4. Poll mtime until it changes (HO's ack is the structural witness — file written).
5. `systemctl --user restart claude-telegram.service` — kills the tmux session and respawns Claude Code.
6. `systemctl --user is-active` polling until active or 60s timeout.
7. `tmux capture-pane` matches the steady-state sentinel `╭─+╮` (Claude Code prompt frame) — fresh HO is up.
8. SessionStart hook (T-0008 deliverable 1, `tools/hooks/handoff-auto-read.sh`) auto-injects `Read /home/claude/HANDOFF.md` into the fresh session — fresh HO reads and resumes.

Smoke: `bash ~/pantheon-config/tools/test_handoff_and_restart.sh` — expect **exit 0** (it self-reports `N passed, 0 failed` and exits non-zero on any failure; don't hardcode the case count here — it grows). Run after any script edit.

**Canonical "did this session boot healthy?" self-check + boot-dialog de-wedge (T-0728):** `bash ~/pantheon-config/tools/verify_startup.sh` (auto-detects the agent from `$USER`; `--agent <recovery|orchestrator|specialist>` to check another). Exit `0` = healthy; `7` = pane wedged (captured but not at an idle prompt, or a non-dismissable modal persists). It de-wedges the dev-channels boot dialog **fail-closed** (reuses the T-0721 exact-sentinel + denylist), and treats connectivity / service / stale-handoff as **soft warnings** (never bricks a boot). This is the self-verifying gate the doc points to instead of carrying the checks in prose. Its own smoke: `bash ~/pantheon-config/tools/test_verify_startup.sh`. **Wired into the SessionStart hook (T-1099):** the reflexive self-call is `~/.claude/hooks/verify-startup-boot.sh` (repo source `tools/hooks/`), which runs `verify_startup.sh --at-boot` — `--at-boot` SKIPS the pane check (unsettled mid-boot, would false-positive rc=7) + de-wedge (Recovery's `alert_only`/T-0559 no-self-inject — never send-keys into our own pane), runs only the SOFT C4/C6/C7/C8 guards (telegram/handoff/hooks-parity/timer-stall), is **fail-open** (timeout-bounded, always exit 0), and injects into the fresh session's context ONLY when a SOFT WARN is present (real drift — e.g. a reverted `core.hooksPath` or a stalled timer). All-green is suppressed as noise. The settings.json wiring is Recovery-local config; the hook script + `--at-boot` mode are repo-tracked.

If anything drifts (sentinel pattern wrong, ack-timeout too tight, unit name surprises) — file a follow-up ticket, do not patch silently.

## Cron'd ticket grooming responsibility

You run `tools/ticket_grooming.py` daily at 12:00 UTC on `pantheon-config`. It is a **systemd user timer** (`pantheon-grooming.timer` → `pantheon-grooming.service`), NOT a crontab entry — `crontab -l` won't show it (that lists only `usage_monitor.py`); verify via `systemctl --user list-timers | grep grooming`. Per fire: scan `tickets/active/` for stale tickets (no transition-log activity past `stale_after` threshold — default `4h` active, per-ticket override via front-matter `stale_after:`; `tickets/blocked/` was retired by ORG-019 2026-05-19, so grooming is active-only — `STATES_TO_GROOM = ("active",)` — do NOT re-add a blocked scan), append `[grooming] stale (no activity since <ts>); flagging for review` IDEMPOTENTLY (skip the append if the ticket's latest log entry is already a `[grooming] stale` line — prevents log spam on repeat fires; the digest still re-surfaces all stale tickets every fire), auto-commit + push, send a plain-text Telegram digest as proof-of-life regardless of stale count.

The runner is `pantheon-config/tools/ticket_grooming.py` (landed at commit `694689f`). It uses the shared `tools/_git_utils.py` helpers for the byte-sequence guard + git-subprocess primitives, narrows scope strictly to `tickets/active/`, and refuses any commit attempt that fails the guard or scope check (loud stderr, non-zero exit). No force-push, no auto-recovery.

Recovery posture (you are unattended at fire time):
- Guard refusal: exit non-zero, refusal location written to stderr. Next digest includes it.
- `git pull --ff-only` failure (real divergence): exit non-zero, do NOT auto-rebase, HO reconciles via fetch from your clone.
- `git push` failure: exit non-zero, reports committed locally but not pushed.

Per the standing `HERMES\.md` byte-sequence discipline (already documented above): the trigger phrase must not appear contiguously in any committed source byte. The grooming agent's runtime guard enforces this for ticket-file content + the constructed commit message; you must continue to typeset references in any digest text or other prose using the discipline-escape form.

## SOPs — check the index first

Before starting any non-trivial action (deploy, restart, remediation, cross-account ops, multi-step maneuver), scan `sop/standard/README.md` (how-to-do-X correctly) and `sop/remediation/README.md` (what-to-do-when-X-breaks) for an applicable SOP. Index first; Read the specific SOP only if relevant. This is the on-demand-traversal contract — agents are not auto-loaded into per-section detail; the index is the discovery surface.

**Recently-ratified policy — check the digest.** The dir indexes above show the *current* set; for what changed *recently* in P&Ds / ADRs / SOPs (the recency signal, so newly-ratified knowledge actually gets applied — ORG-012 outflow), skim `notes/recent-policy-changes.md` (auto-maintained daily by `tools/policy_outflow_sync.py`, T-0834; pointers only, no inlined bodies) when starting a session arc.

## Be concise with the operator (pinned 2026-07-28 — his direct request)

He asked for this fleet-wide after getting walls of text: *"can you quickly give everyone a memory pin to speak more concisely? I've been getting walls since we swapped to opus 5."*

He reads on a phone, between other things. A long update costs him more than it tells him — and an update he has to *work* to parse is worse than a short one, even when every sentence in it is true.

- **Lead with what he must decide or do.** Number the asks. If there are none, say so in one line.
- **Routine status is a few short lines.** Multi-paragraph is for a live incident, or a decision that genuinely needs its rationale to be decidable.
- **Cite the ticket ID and let it carry the depth.** Don't inline findings, verification chains, or measured numbers — he can open the ticket.
- **One digest per arc, not a message per step.** Batch at a natural stopping point.
- **Thoroughness in the work does not license thoroughness in the report.** The full reasoning belongs in the ticket resolve note, the SOP, or a memory — not in his chat.

This is a D1 (preserve the Operator's bandwidth) instance, not a style preference.
