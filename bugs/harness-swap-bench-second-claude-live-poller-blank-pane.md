---
symptom: "A harness-swap / daemon-bringup bench blanks the pane while standing up a REAL interactive claude as the 'current' runtime - no frame, no composer glyph at 6s AND 14s, readiness times out - even though the same claude binary runs fine as the live daemon"
fix: "Give the bench's current-runtime a HERMETIC claude home (throwaway HOME, onboarding+trust pre-accepted, NO enabledPlugins) and run it on a NON-live-poller seat; never stand up a second plugin-loading interactive claude on a seat whose bot already holds a live getUpdates poll"
domain: claude-code-harness, telegram, plugins, harness-swap, bench, second-claude, blank-pane, 409, silent-failure
---
# Harness-swap bench blank-pane: a second plugin-loading claude collides with the live poller

The T-2572 harness-swap bench (real claude->codex on a disposable systemd scope + tmux socket) fails at `runtime_controller._verify_runtime_input_ready` while standing up the **CURRENT (claude)** runtime, BEFORE the swap itself runs. Symptom: the tmux pane is **BLANK at 6s AND 14s** - no `╭...╮` frame edge, no `❯` composer glyph - so `tools.submit_verify.ready_for_submission` never satisfies its conjunction and readiness times out. Teardown is clean (the safety design works: zero stray scope/socket/process). It *looks* like a timing or glyph-match bug. It is NOT.

## Root cause (the inverse of `new-agent-home-telegram-plugin-provisioning-gotchas`)

`harness_adapter.claude.build_interactive_launch` launches a real interactive `claude` with `cwd=Path.cwd()` and an env of only three `PANTHEON_TMUX_*` vars. `runtime_controller._runtime_command_argv` wraps it as `/usr/bin/env -- <vars> <argv>` - and `env` WITHOUT `-i` **inherits the caller's environment, including HOME**. So the bench claude reads the seat's `~/.claude/settings.json` and loads the account-global telegram plugin (`enabledPlugins`), same as the live daemon.

On any seat whose bot already holds a **live getUpdates poll** - `claude` (Orchestrator) and `claude-fallback` (Recovery), both `ps`-confirmed running a `bun server.ts` poller child - that second claude opens a second poll on the same bot token -> **409** -> the live inbound poller EXITS and the agent goes comms-dark. This is the ratified **no-second-claude** rule (the 2026-06-06/07 bridge-death incident). The `PreToolUse` guard (T-0894 / T-0996 / T-1623) blocks any such `claude` invocation outright; and even absent the guard, the plugin-init collision (or the first-run onboarding/trust surface under an unseeded home) prevents the composer from ever rendering -> the blank pane.

So "bare claude renders blank" is a red herring: Specialist and Recovery run bare `claude` (no `--channels`) and render composers fine - because they ARE the poller, not a second one.

## Fix / the shape a bench must take

The current-runtime claude needs a **hermetic home**:
- a throwaway `HOME` (claude keys config off `$HOME`; `CLAUDE_CONFIG_DIR` did not grep-hit the installed bundle as of 2026-08-05, so `HOME` is the robust lever), seeded with `~/.claude.json` `hasCompletedOnboarding` + `hasTrustDialogAccepted` for the cwd, and a `~/.claude/settings.json` with **NO `enabledPlugins`** - so it loads no plugin poller and shows no onboarding wall, and
- run on a **non-live-poller seat**: the `codex` account runs its own python telegram bridge (no claude-plugin poller); `suborch1`/`suborch2` run claude with no poller (per `ps`).

Validating even that the hermetic home renders a composer means launching claude on such a seat, because the guard correctly blocks any `claude` on the live poller account regardless of how hermetic the invocation actually is.

## Rule of thumb

**Never stand up a second plugin-loading interactive claude on a seat whose bot has a live poll.** Bench / swap / bringup tooling that must exercise a *real* claude runtime gives it a hermetic no-plugin home on a disposable non-poller seat. A bench that shares the live seat's HOME is proving nothing it can reach - it just kills your comms.

## Sibling: codex has the same first-run-modal problem, minus the plugin layer

A live disposable-tmux probe (2026-08-05) showed the deeper shared root: **a fresh agent CLI sits at first-run interactive MODALS before it renders the composer, and `ready_for_submission` correctly rejects each** (a modal has no composer frame). It is not only claude's plugin/409.

- **codex, cwd=/home/claude** -> a **trust-directory dialog** ("Do you trust the contents of this directory?"). The `›` cursor on "1. Yes, continue" is a false-ish glyph hit that the full frame+dialog conjunction still rejects. codex trust is per-project `trust_level` in `~/.codex/config.toml`.
- **codex, cwd under /tmp** (config has `[projects."/tmp"] trust_level = "trusted"`) -> clears trust, then sits at an **"update available" modal** (version-drift-triggered; no config-suppress found — keep codex fleet-current, or have the readiness step dismiss it). Also `codex --no-alt-screen` likely matters for reliable `capture-pane`.

So a robust bench launches EACH harness in a hermetic, **modal-suppressed** config: codex = trusted cwd (the real `--work-root /tmp/...` already is) + update handled + `--no-alt-screen`; claude = hermetic no-plugin onboarding-accepted HOME on a non-poller seat. The readiness predicate is not the bug — the un-suppressed onboarding surface is.

## Refs

- `tickets/*/T-2572.md` transition log - the 2026-08-05 "second structural wall" + codex-probe annotations (source + `ps` + live-probe verified).
- Sibling `bugs/new-agent-home-telegram-plugin-provisioning-gotchas.md` - the make-the-plugin-WORK direction (same surfaces, opposite goal).
- `reference_telegram_channel_diagnosis` memory pin - single-poller rule + `bun`/`--channels`/trust diagnostic ladder.
