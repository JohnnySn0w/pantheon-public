# Venerating ancestors — harvesting a fresh HANDOFF from a dead session

> ## ⚠️ RETIRED 2026-07-28 — DO NOT FOLLOW THIS PROCEDURE
>
> The capability this SOP operates was **decommissioned** under **T-2214** on the operator's
> ruling ("get rid of the claude daemon for ancestor", Telegram 16:52Z). The
> `pantheon-ancestor-daemon.service` session it depends on is stopped and disabled, so
> every step below will fail at the resurrect stage.
>
> **Two independent reasons, both recorded so this is not revisited from memory:**
> 1. **Never used** — `resume_request` had no production caller anywhere in the repo (only
>    its own module and its own test), and the inbox held 2 messages for all time, both
>    45 days old.
> 2. **Uneconomic even if used** — per the operator, resurrecting a session costs *more tokens
>    than simply scraping the dead session from the Chronicle*. The cheaper path already
>    existed.
>
> **If you need a HANDOFF from a dead session, scrape the Chronicle** (`tools/chronicle.py`).
> Retained below for history: the substrate edges and the four `--resume` gotchas remain
> accurate about Claude Code's behaviour and may inform future headless work.
>
> Generalized into `sop/standard/feature-usage-monitoring-and-pruning.md`, which exists
> because this feature ran 45 days unused without anything surfacing it.
>
> **Code removed 2026-10-09 (T-4741, #835):** `tools/veneration.py` and
> `tools/ancestor_consume.py` (and their tests) are gone from the tree; the commands
> below no longer resolve. Recover from git history if the gotchas are ever needed in code.

**Status:** **RETIRED 2026-07-28 (T-2214).** Originally drafted 2026-06-14 by Recovery, authored while stepping through the T-0766 supervised first-fire (the operator watching). Documented the headless-`--print` harvest design the first-fire converged on. Adopted 2026-06-16 (Recovery, principles-as-ratification); routed by Orchestrator (T-1202).

## When to use this

Veneration is the **fallback tier** of agent recovery. Reach for it only when the normal path is exhausted:

1. An agent booted blank, or its predecessor was cut off (forced/api-health restart, crash, OOM, a handoff that was never written or got cut mid-write).
2. `tools/chronicle.py` can't cover it — there is **no usable latest handoff** to read. (Most recovery IS just reading the Chronicle handoff; do that first.)

The dead session is not gone: its full transcript lives in its `.jsonl` on disk, and `claude --resume` reloads the entire context even though the process is dead. Veneration resurrects that dead context **under `the-ancestor` (channel-less)**, prompts it to write a fresh HANDOFF, confirms the write, and releases. The successor then reads the harvested HANDOFF.

**Do NOT use veneration when:** a Chronicle handoff exists (read it instead); the agent is alive-but-wedged (restart it — see `agent-self-restart-with-kick.md` / `handoff-restart-runbook.md`); or the death was api-health **poisoning** (a 400-on-request-shape session may inherit the poison on resume and 400 again — veneration reliably covers crashed/cut-off/OOM, less certainly the poisoned case).

## Procedure

The harvest is **one headless `claude --print` subprocess** — there is no tmux session, no readiness polling, no send-keys injection. `tools/veneration.py` `resurrect()` orchestrates: validate → acquire lease → `harvest()` → confirm HANDOFF written → release.

### 1. Prerequisites (verify before firing)
- **Identify the dead session id.** It is the `.jsonl` basename: `.../<uuid>.jsonl` → `<uuid>`. Find it under the target account's project dir, e.g. `sudo -u the-ancestor find ~/.claude/projects -name '<uuid>.jsonl'`. **Do NOT resume a session that is currently LIVE** — only a dead one (cross-check against running PIDs).
- **`the-ancestor`'s clone is current:** `sudo -u the-ancestor bash -c 'cd ~/pantheon-config && git pull --rebase'`. The harvest runs `the-ancestor`'s copy of `veneration.py`, not Recovery's — a stale clone runs stale code. It also reads the seat's pinned rules from that clone (T-4649), so a stale clone hands the dead session stale rules.
- **`.veneration/` exists** (lease dir): `/home/the-ancestor/.veneration/` (mode 700, owned by `the-ancestor`).

### 2. Fire `resurrect()` — run it AS `the-ancestor`, with `run_as=None`
The lease (`/home/the-ancestor/.veneration/veneration.lock`) is opened **directly** (no sudo), so the Python process must itself be `the-ancestor`. And `the-ancestor` **cannot self-sudo**, so the inner ops must NOT re-sudo → pass `run_as=None`.

```bash
sudo -n -u the-ancestor bash -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) python3 -c "
import sys; sys.path.insert(0, \"/home/the-ancestor/pantheon-config/tools\")
import veneration as ven
res = ven.resurrect(
    agent=\"<persona>\",
    dead_jsonl=\"/home/the-ancestor/.claude/projects/-home-the-ancestor/<uuid>.jsonl\",
    handoff_target=\"/home/the-ancestor/.veneration/<persona>-HANDOFF.md\",
    run_as=None,
)
print(res)
"'
```

Under the hood `harvest()` runs (every operand `shlex`-quoted):
```
cd <the-ancestor home> && exec env XDG_RUNTIME_DIR=/run/user/<uid> \
  <claude> --resume <session-id> --print --safe-mode \
           --permission-mode bypassPermissions "<harvest prompt>"
```
- `--print` → headless, non-interactive (no TUI gates — see Common failure modes).
- `--safe-mode` → no hooks (kills competing SessionStart nudges), no plugins/MCP (reinforces channel-less), no CLAUDE.md re-priming.
- `--permission-mode bypassPermissions` → the harvest Write lands unattended. Headless `--print` has **no** one-time consent screen (the interactive `--dangerously-skip-permissions` flag does — avoid it).
- `cd <home>` (as `the-ancestor`, not in Recovery's process — Recovery can't enter a 700 home) → `--resume <id>` is project-scoped (cwd-hashed), so it must run in the project dir the transcript lives under.
- **Current rules (T-4649).** `--resume` replays the dead session's start-time system prompt and `--safe-mode` skips CLAUDE.md, so the turn would otherwise write the successor's HANDOFF under rules that may be weeks old. The prompt therefore tells it to read `<the-ancestor clone>/pinned-rules/<agent>.md` first, which is why `agent` must be the persona name (recovery, specialist, archivist, mason, artisan, orchestrator-codex). A seat's own `~/CLAUDE.md` is no use here: `the-ancestor` cannot read another seat's home.

**Result contract:** `Result.status ∈ {ok, skipped, failed}`. `ok` = subprocess exited 0 AND the HANDOFF exists non-empty. `skipped` = another veneration holds the lease (back off, don't block). `failed` = subprocess error/timeout, or clean exit with no file written. Default `harvest_timeout` is 300s (large-transcript reload + write).

### 3. Verify
- `Result` is `ok`, and its detail ends `rules: <path>`: the pinned-rules file the turn was told to re-read. `rules: none readable` means no file matched `agent` (a non-persona label, or orchestrator, whose Claude rules live in claude-config). That handoff reflects the dead session's start-time rules, so check it against the current ones before the successor relies on it.
- `cat <handoff_target>` — confirm the content is a **real** handoff (identity, current state, in-flight work, next actions), not a stub. A good harvest also self-attests the constraints (no messages/channels/mutation).
- Clean teardown (should be automatic — the subprocess self-exits): no `veneration-*` tmux session, no orphan `claude --resume` process (`pgrep -u the-ancestor -af claude`), and the lockfile is 0-byte + unlocked (flock released on exit).

### 4. Teardown of the harvest itself
The successor consumes `<handoff_target>` then it can be removed. Remove any throwaway runner/log under `/tmp`. The lease auto-releases on process exit; no manual unlock needed.

## Common failure modes

The interactive-TUI approach (the original MVP: launch tmux → wait_ready sentinel → send-keys inject → mtime-ack → kill) fights **three TUI-only gates** that an unattended one-shot harvest cannot clear. The first-fire hit all three; the headless `--print` design exists to dodge them. Recognize them if you ever revisit a TUI path (e.g. a future interactive v2):

1. **`--dangerously-skip-permissions` consent screen.** The interactive flag shows a one-time "Yes, I accept" dialog before the session is usable → readiness never arrives unattended. *Headless `--print` + `--permission-mode bypassPermissions` has no such screen.*
2. **Bypass-mode footer breaks a readiness sentinel.** Once bypass is on, the idle footer reads "⏵⏵ bypass permissions on…", which a sentinel matching "? for shortcuts" / "ctrl+t to hide tasks" never matches → false "not ready" timeout even when the session IS ready. *Headless has no readiness sentinel at all.*
3. **Inherited SessionStart hooks inject competing nudges.** The resumed session runs the **target account's** hooks; `the-ancestor`'s SessionStart injected a "drain your mailbox" nudge that competed with the harvest prompt → the session did recon and went idle without writing. *`--safe-mode` disables hooks, so nothing competes.*

Other failure modes:
- **Stale clone runs stale code.** Always `git pull --rebase` `the-ancestor`'s clone before firing; the harvest runs its copy.
- **`run_as="the-ancestor"` while already `the-ancestor` fails** — it can't self-sudo. Run the Python as `the-ancestor` with `run_as=None`.
- **`claude` not on `PATH` under non-login `sudo -u`** — pass the absolute `claude_bin` (default `/home/the-ancestor/.npm-global/bin/claude`).
- **api-health-poisoned source** may 400 on resume (see When to use this) → `failed`; fall back to manual reconstruction.

## Boundary

- Covers: harvesting a HANDOFF from a **dead** session when the Chronicle can't. Does NOT cover: live-but-wedged recovery (→ restart SOPs), reading an existing Chronicle handoff (→ `tools/chronicle.py`), or interactive questioning of the dead session (a hypothetical v2 — when built it may re-introduce a TUI path; we do not carry one speculatively).
- Single-holder only: the shared flock lease serializes veneration on the one `the-ancestor` substrate. A second concurrent attempt returns `skipped`.
- Channel-less is the load-bearing safety invariant: the resume holds NO bot token (no `--channels`, and `--safe-mode` disables plugins/MCP), so it can never hijack a live agent's bridge — the whole reason veneration runs under `the-ancestor`, never on an agent's own account.

## Cross-references

- `tools/veneration.py` + `tools/test_veneration.py` — the harness + its tests (`HarvestArgvTest` is the real-argv regression guard).
- `tools/_lease.py` — the shared flock lease (non-blocking acquire, implicit release on exit, stale-takeover).
- T-0766 — the build + supervised first-fire that produced this SOP. Headless pivot at commit `524aed73`.
- `project_veneration_build_state` (Recovery memory) — the-ancestor / Veneration build state + new-daemon bringup gotchas.
- Sibling recovery SOPs: `handoff-restart-runbook.md`, `agent-self-restart-with-kick.md`, `adding-restart-capability-to-a-new-agent.md`.
- `soak-verification-via-time-gated-ticket.md` — get unattended-soak confidence after this supervised first-fire (a `not_before:`-gated ticket).
